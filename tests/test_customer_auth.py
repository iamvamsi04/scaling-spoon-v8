"""Customer identity must be checked before private billing work starts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config import settings
from database.connection import connect
from database.invoice import invoice_path
from mcp_server.access import scoped_tool
from mcp_server.tools.database import execute_sql_query, schema_hint


PASSWORD = 'Test customer password 2026'
OTHER_PASSWORD = 'Different customer password 2026'


class Engine:
    def __init__(self):
        self.calls = []
        self.invoice_calls = []

    async def chat(self, message, history, customer_id, **kwargs):
        self.calls.append((message, customer_id))
        return {'response': 'Customer ' + str(customer_id), 'invoices': [], 'charts': []}

    async def invoice(self, number, owner):
        self.invoice_calls.append((number, owner))
        path = invoice_path(number, owner)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'%PDF-1.4\nPrivate invoice fixture')
        return {'ok': True}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, 'API_KEY', '')
    app.state.engine = Engine()
    with TestClient(app) as current:
        yield current


def provision(client, customer='CUST001', password=PASSWORD):
    from api.customer_auth import set_password
    set_password(customer, password)
    response = client.post('/customers/login', json={'customer_id': customer, 'password': password})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['customer_id'] == customer and result['token']
    return {'X-Customer-Token': result['token']}


def conversation(client, headers, customer='CUST001'):
    response = client.post('/conversations', json={'customer_id': customer}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()['conversation_id']


def message(cid):
    return {'conversation_id': cid, 'request_id': str(uuid4()), 'message': 'Show my last order'}


def test_existing_customer_cannot_be_claimed_by_registration(client):
    for customer in ('CUST001', 'CUST002'):
        response = client.post('/customers/register', json={
            'customer_id': customer, 'name': 'Claimed identity', 'password': PASSWORD,
            'email': 'attacker@example.com'})
        assert response.status_code == 409, response.text
        assert client.post('/customers/login', json={
            'customer_id': customer, 'password': PASSWORD}).status_code == 401
    with closing(connect()) as conn:
        original = conn.execute("SELECT name,email FROM customers WHERE customer_id='CUST001'").fetchone()
        assert tuple(original) == ('Alex Morgan', 'alex@example.com')
        assert conn.execute('SELECT COUNT(*) FROM customer_credentials').fetchone()[0] == 0


def test_new_registration_is_atomic_and_requires_token_for_customer_scope(client):
    data = {'customer_id': 'NEW-CUSTOMER', 'name': 'New Customer', 'password': PASSWORD,
            'company': 'Example Company', 'email': 'new@example.com'}
    response = client.post('/customers/register', json=data)
    assert response.status_code in (200, 201), response.text
    registered = response.json()
    assert registered['customer_id'] == 'NEW-CUSTOMER'
    headers = {'X-Customer-Token': registered['token']}
    cid = conversation(client, headers, 'NEW-CUSTOMER')
    assert client.post('/chat', json=message(cid), headers=headers).status_code == 200
    assert app.state.engine.calls == [('Show my last order', 'NEW-CUSTOMER')]
    assert client.post('/customers/register', json=data).status_code == 409
    assert client.post('/customers/login', json={
        'customer_id': 'NEW-CUSTOMER', 'password': PASSWORD}).status_code == 200
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers WHERE customer_id='NEW-CUSTOMER'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM customer_credentials WHERE customer_id='NEW-CUSTOMER'").fetchone()[0] == 1


@pytest.mark.parametrize('password', ['', 'shortpass', 'x' * 129])
def test_registration_password_boundaries_leave_no_account(client, password):
    response = client.post('/customers/register', json={
        'customer_id': 'INVALID-PASSWORD', 'name': 'Invalid', 'password': password})
    assert response.status_code == 422, response.text
    if password:
        assert password not in response.text
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers WHERE customer_id='INVALID-PASSWORD'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM customer_credentials WHERE customer_id='INVALID-PASSWORD'").fetchone()[0] == 0


@pytest.mark.parametrize('password', ['x' * 10, 'x' * 128])
def test_registration_accepts_documented_password_lengths(client, password):
    response = client.post('/customers/register', json={
        'customer_id': 'BOUNDARY', 'name': 'Boundary Customer', 'password': password})
    assert response.status_code in (200, 201), response.text
    assert client.post('/customers/login', json={'customer_id': 'BOUNDARY', 'password': password}).status_code == 200


def test_login_errors_do_not_identify_existing_customer(client):
    provision(client)
    existing = client.post('/customers/login', json={'customer_id': 'CUST001', 'password': OTHER_PASSWORD})
    missing = client.post('/customers/login', json={'customer_id': 'NO-SUCH-CUSTOMER', 'password': OTHER_PASSWORD})
    assert existing.status_code == missing.status_code == 401
    assert existing.json() == missing.json()
    assert PASSWORD not in existing.text and OTHER_PASSWORD not in existing.text


def test_seeded_customers_have_no_default_password_and_hashes_use_random_salts(client):
    with closing(connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM customer_credentials').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM customer_sessions').fetchone()[0] == 0
    provision(client, 'CUST001', PASSWORD)
    provision(client, 'CUST002', PASSWORD)
    with closing(connect()) as conn:
        encoded = [row[0] for row in conn.execute('SELECT password_hash FROM customer_credentials ORDER BY customer_id')]
    assert len(set(encoded)) == 2
    for hashed in encoded:
        method, rounds, salt, digest = hashed.split('$')
        assert method == 'pbkdf2_sha256' and int(rounds) >= 600000
        assert len(salt) >= 32 and len(digest) == 64


def test_customer_id_without_verification_cannot_open_private_scope(client):
    for headers in ({}, {'X-Customer-Token': 'made-up-token'}):
        response = client.post('/conversations', json={'customer_id': 'CUST001'}, headers=headers)
        assert response.status_code in (401, 403), response.text
    assert app.state.engine.calls == []


def test_customer_cannot_select_another_customer_with_valid_token(client):
    headers = provision(client)
    result = client.post('/conversations', json={'customer_id': 'CUST002'}, headers=headers)
    assert result.status_code in (401, 403), result.text
    assert app.state.engine.calls == []


@pytest.mark.parametrize('route', ['history', 'chat', 'stream', 'invoice'])
@pytest.mark.parametrize('intruder', ['anonymous', 'fake', 'other_customer'])
def test_every_private_route_rechecks_owner_before_work(client, route, intruder):
    owner = provision(client)
    cid = conversation(client, owner)
    headers = {} if intruder == 'anonymous' else {'X-Customer-Token': 'forged'}
    if intruder == 'other_customer':
        headers = provision(client, 'CUST002', OTHER_PASSWORD)
    if route == 'history':
        result = client.get('/conversations/' + cid, headers=headers)
    elif route == 'invoice':
        result = client.get('/invoices/INV-PRIVATE-001',
                            params={'conversation_id': cid, 'customer_id': 'CUST001'}, headers=headers)
    else:
        result = client.post('/chat' + ('/stream' if route == 'stream' else ''),
                             json=message(cid), headers=headers)
    assert result.status_code in (401, 403), result.text
    assert app.state.engine.calls == []
    assert app.state.engine.invoice_calls == []
    assert client.get('/conversations/' + cid, headers=owner).json()['messages'] == []


def test_own_history_chat_stream_and_invoice_work(client):
    headers = provision(client)
    cid = conversation(client, headers)
    assert client.post('/chat', json=message(cid), headers=headers).status_code == 200
    assert client.get('/conversations/' + cid, headers=headers).status_code == 200
    streamed = client.post('/chat/stream', json=message(cid), headers=headers)
    assert streamed.status_code == 200 and '"event":"result"' in streamed.text
    invoice = client.get('/invoices/INV-PRIVATE-001',
                         params={'conversation_id': cid, 'customer_id': 'CUST001'}, headers=headers)
    assert invoice.status_code == 200 and invoice.content.startswith(b'%PDF')
    # Even a verified customer cannot override invoice ownership in the URL.
    assert client.get('/invoices/INV-PRIVATE-002',
                      params={'conversation_id': cid, 'customer_id': 'CUST002'}, headers=headers).status_code == 403
    assert app.state.engine.invoice_calls == [('INV-PRIVATE-001', 'CUST001')]


def test_cached_reply_requires_live_session_and_logout_revokes_all_routes(client):
    headers = provision(client)
    cid = conversation(client, headers)
    body = message(cid)
    first = client.post('/chat', json=body, headers=headers)
    assert first.status_code == 200
    assert client.post('/chat', json=body, headers=headers).json() == first.json()
    assert len(app.state.engine.calls) == 1
    assert client.post('/customers/logout', headers=headers).status_code == 200
    assert client.post('/chat', json=body, headers=headers).status_code in (401, 403)
    assert client.post('/chat/stream', json=message(cid), headers=headers).status_code in (401, 403)
    assert client.get('/conversations/' + cid, headers=headers).status_code in (401, 403)
    assert client.get('/invoices/INV-PRIVATE-001', params={'conversation_id': cid},
                      headers=headers).status_code in (401, 403)
    assert len(app.state.engine.calls) == 1 and not app.state.engine.invoice_calls
    fresh = client.post('/customers/login', json={'customer_id': 'CUST001', 'password': PASSWORD})
    assert fresh.status_code == 200 and fresh.json()['token'] != headers['X-Customer-Token']


def test_password_reset_revokes_old_sessions(client):
    headers = provision(client)
    cid = conversation(client, headers)
    from api.customer_auth import set_password
    set_password('CUST001', OTHER_PASSWORD)
    assert client.get('/conversations/' + cid, headers=headers).status_code in (401, 403)
    assert client.post('/customers/login', json={'customer_id': 'CUST001', 'password': PASSWORD}).status_code == 401
    assert client.post('/customers/login', json={'customer_id': 'CUST001', 'password': OTHER_PASSWORD}).status_code == 200


def test_session_expiry_enforced_before_private_work(client, monkeypatch):
    from api import customer_auth
    now = 1800000000
    monkeypatch.setattr(customer_auth.time, 'time', lambda: now)
    headers = provision(client)
    cid = conversation(client, headers)
    with closing(connect()) as conn:
        expiry = conn.execute('SELECT expires_at FROM customer_sessions').fetchone()[0]
    assert expiry == now + 12 * 60 * 60
    monkeypatch.setattr(customer_auth.time, 'time', lambda: expiry - 1)
    assert client.get('/conversations/' + cid, headers=headers).status_code == 200
    monkeypatch.setattr(customer_auth.time, 'time', lambda: expiry)
    assert client.post('/chat', json=message(cid), headers=headers).status_code in (401, 403)
    assert client.post('/chat/stream', json=message(cid), headers=headers).status_code in (401, 403)
    assert app.state.engine.calls == []


def test_failed_login_lock_prevents_password_guessing_then_recovers(client, monkeypatch):
    from api import customer_auth
    now = 1800000000
    monkeypatch.setattr(customer_auth.time, 'time', lambda: now)
    headers = provision(client)
    assert headers
    for _ in range(customer_auth.LOGIN_ATTEMPTS):
        assert client.post('/customers/login', json={
            'customer_id': 'CUST001', 'password': OTHER_PASSWORD}).status_code == 401
    assert client.post('/customers/login', json={
        'customer_id': 'CUST001', 'password': PASSWORD}).status_code == 401
    monkeypatch.setattr(customer_auth.time, 'time', lambda: now + customer_auth.LOCK_SECONDS)
    assert client.post('/customers/login', json={
        'customer_id': 'CUST001', 'password': PASSWORD}).status_code == 200


def test_anonymous_model_cannot_adopt_an_existing_account(client):
    class ClaimingEngine(Engine):
        async def chat(self, *args, **kwargs):
            return {'response': 'Customer found', 'associated_customer_id': 'CUST001', 'invoices': [], 'charts': []}
    app.state.engine = ClaimingEngine()
    cid = client.post('/conversations', json={}).json()['conversation_id']
    result = client.post('/chat', json=message(cid))
    assert result.status_code in (200, 403, 503)
    saved = client.get('/conversations/' + cid)
    assert saved.status_code == 200
    assert saved.json()['customer_id'] is None


def test_auth_material_is_private_and_not_stored_in_cleartext(client):
    headers = provision(client)
    token = headers['X-Customer-Token']
    with closing(connect()) as conn:
        credentials = [list(row) for row in conn.execute('SELECT * FROM customer_credentials')]
        sessions = [list(row) for row in conn.execute('SELECT * FROM customer_sessions')]
        assert credentials and sessions
        stored = repr(credentials) + repr(sessions)
        assert PASSWORD not in stored and token not in stored
    read = scoped_tool(execute_sql_query)
    for role in ('customer', 'cashier', 'admin'):
        for table in ('customer_credentials', 'customer_sessions'):
            for query in ('SELECT * FROM ' + table,
                          'SELECT * FROM main.' + table,
                          'WITH private AS (SELECT * FROM ' + table + ') SELECT * FROM private'):
                result = read(query, execution_mode=role, execution_customer_id='CUST001')
                assert not result['ok'], (role, query, result)
                assert PASSWORD not in str(result) and token not in str(result)
            assert table not in schema_hint(role)


def test_simultaneous_registration_cannot_claim_one_customer_twice(client):
    def register(index):
        return client.post('/customers/register', json={
            'customer_id': 'CONCURRENT', 'name': 'Customer ' + str(index),
            'password': PASSWORD + str(index)})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(register, range(4)))
    winners = [(index, result) for index, result in enumerate(results) if result.status_code in (200, 201)]
    assert len(winners) == 1, [result.status_code for result in results]
    winner = winners[0][0]
    assert all(result.status_code == 409 for index, result in enumerate(results) if index != winner)
    for index in range(4):
        result = client.post('/customers/login', json={'customer_id': 'CONCURRENT', 'password': PASSWORD + str(index)})
        assert result.status_code == (200 if index == winner else 401)
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers WHERE customer_id='CONCURRENT'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM customer_credentials WHERE customer_id='CONCURRENT'").fetchone()[0] == 1
