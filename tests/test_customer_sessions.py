"""Use real sign-in tokens to exercise the stored session digest and UI expiry."""
import hashlib
from contextlib import closing
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from api import customer_auth
from api.main import app
from config import settings
from database.connection import connect, transaction


PASSWORD = 'Customer session test password'


class Engine:
    calls = 0

    async def chat(self, *args, **kwargs):
        self.calls += 1
        return {'response': 'Your account response.'}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, 'API_KEY', '')
    app.state.engine = Engine()
    with TestClient(app) as current:
        yield current


def sign_in(client, customer='CUST001', *, provision=True):
    if provision:
        customer_auth.set_password(customer, PASSWORD)
    response = client.post('/customers/login', json={'customer_id': customer, 'password': PASSWORD})
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def headers(account):
    return {'X-Customer-Token': account['token']}


def test_real_token_matches_stored_hash_but_hash_itself_cannot_authenticate(client):
    account = sign_in(client)
    digest = hashlib.sha256(account['token'].encode()).hexdigest()
    with closing(connect(readonly=True)) as conn:
        stored = dict(conn.execute('SELECT * FROM customer_sessions').fetchone())
    assert stored == {'token_hash': digest, 'customer_id': 'CUST001', 'expires_at': account['expires_at']}
    assert account['expires_in'] == customer_auth.SESSION_SECONDS
    assert digest != account['token']
    for token in (None, '', 'forged-token', digest):
        result = client.get('/customers/session', headers={'X-Customer-Token': token} if token else {})
        assert result.status_code == 401
        assert account['token'] not in result.text and digest not in result.text
    result = client.get('/customers/session', headers=headers(account))
    assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
    assert set(result.json()) == {'customer_id', 'expires_at', 'expires_in'}
    assert result.json()['customer_id'] == 'CUST001'
    assert result.json()['expires_at'] == account['expires_at']
    assert 0 < result.json()['expires_in'] <= account['expires_in']
    assert account['token'] not in result.text and digest not in result.text


def test_sessions_survive_api_restart_and_status_checks_do_not_extend_expiry(client):
    account = sign_in(client)
    with TestClient(app) as restarted:
        status = restarted.get('/customers/session', headers=headers(account))
        assert status.status_code == 200
        assert status.json()['expires_at'] == account['expires_at']
        cid = restarted.post('/conversations', json={'customer_id': 'CUST001'}, headers=headers(account)).json()['conversation_id']
        reply = restarted.post('/chat', json={'conversation_id': cid, 'request_id': str(uuid4()), 'message': 'Show my order'}, headers=headers(account))
        assert reply.status_code == 200
    assert client.get('/customers/session', headers=headers(account)).json()['expires_at'] == account['expires_at']


def test_logout_revokes_only_presented_session_and_password_reset_revokes_owner_sessions(client):
    first = sign_in(client)
    second = sign_in(client, provision=False)
    other = sign_in(client, customer='CUST002')
    assert first['token'] != second['token']
    assert client.post('/customers/logout', headers=headers(first)).status_code == 200
    assert client.get('/customers/session', headers=headers(first)).status_code == 401
    assert client.get('/customers/session', headers=headers(second)).status_code == 200
    assert client.get('/customers/session', headers=headers(other)).status_code == 200
    customer_auth.set_password('CUST001', 'A separate reset password')
    assert client.get('/customers/session', headers=headers(second)).status_code == 401
    assert client.get('/customers/session', headers=headers(other)).status_code == 200


def test_expiry_boundary_rejects_live_check_and_private_routes(client, monkeypatch):
    now = 1800000000
    monkeypatch.setattr(customer_auth.time, 'time', lambda: now)
    account = sign_in(client)
    owner = headers(account)
    cid = client.post('/conversations', json={'customer_id': 'CUST001'}, headers=owner).json()['conversation_id']
    monkeypatch.setattr(customer_auth.time, 'time', lambda: account['expires_at'] - 1)
    assert client.get('/customers/session', headers=owner).json()['expires_in'] == 1
    monkeypatch.setattr(customer_auth.time, 'time', lambda: account['expires_at'])
    assert client.get('/customers/session', headers=owner).status_code == 401
    assert client.get('/conversations/' + cid, headers=owner).status_code == 401
    assert client.post('/conversations', json={'customer_id': 'CUST001'}, headers=owner).status_code == 401
    body = {'conversation_id': cid, 'request_id': str(uuid4()), 'message': 'Show my order'}
    assert client.post('/chat', json=body, headers=owner).status_code == 401
    assert client.post('/chat/stream', json=body, headers=owner).status_code == 401
    assert app.state.engine.calls == 0


@pytest.mark.parametrize('expiry', ['invalid', '2030-01-01T00:00:00Z', 1.5, None])
def test_malformed_expiry_cannot_create_an_unlimited_session(client, expiry):
    account = sign_in(client)
    digest = hashlib.sha256(account['token'].encode()).hexdigest()
    if expiry is None:
        # The schema itself rejects null expiries.
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError), transaction() as conn:
            conn.execute('UPDATE customer_sessions SET expires_at=? WHERE token_hash=?', (expiry, digest))
        return
    with transaction() as conn:
        conn.execute('UPDATE customer_sessions SET expires_at=? WHERE token_hash=?', (expiry, digest))
    assert client.get('/customers/session', headers=headers(account)).status_code == 401
    assert customer_auth.customer_for_token(account['token']) is None


def test_session_status_respects_shared_api_authorization_without_echoing_secrets(client, monkeypatch):
    account = sign_in(client)
    monkeypatch.setattr(settings, 'API_KEY', 'server-api-key-private')
    rejected = client.get('/customers/session', headers=headers(account))
    assert rejected.status_code == 401 and rejected.json()['detail'] == 'Invalid API key.'
    assert account['token'] not in rejected.text
    assert client.get('/customers/session', headers={**headers(account), 'X-API-Key': settings.API_KEY}).status_code == 200


def ui_session(monkeypatch, *, expires_in=3600, session_status=200):
    import time
    actual_monotonic = time.monotonic
    elapsed = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: actual_monotonic() + elapsed[0])
    calls = []

    def respond(self, method, url, **kwargs):
        calls.append((url, kwargs))
        status = 200
        if url.endswith('/health'):
            data = {'model_configured': True}
        elif url.endswith('/customers/login'):
            data = {'customer_id': 'CUST001', 'token': 'x' * 43, 'expires_in': expires_in, 'expires_at': 1800000000}
        elif url.endswith('/customers/session'):
            assert kwargs['headers']['X-Customer-Token'] == 'x' * 43 and kwargs['timeout'] == 2.0
            status = session_status
            data = ({'customer_id': 'CUST001', 'expires_in': expires_in, 'expires_at': 1800000000} if status == 200 else
                    {'detail': 'Invalid API key.' if status == 403 else 'Your customer session ended. Please sign in again.'})
            # Use 403 to select a shared-key 401 response for the UI classification test.
            if status == 403:
                status = 401
        elif url.endswith('/conversations'):
            data = {'conversation_id': str(uuid4())}
        elif url.endswith('/chat'):
            data = {'response': 'Private account response.'}
        else:
            raise AssertionError(url)
        return httpx.Response(status, json=data, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, 'request', respond)
    ui = AppTest.from_file(str(settings.ROOT / 'ui/streamlit_app.py'), default_timeout=60).run()
    next(field for field in ui.text_input if field.label == 'Customer ID').set_value('CUST001')
    next(field for field in ui.text_input if field.label == 'Password').set_value(PASSWORD)
    next(button for button in ui.button if button.label == 'Sign in').click().run()
    assert not ui.exception and ui.session_state['customer_token']
    ui.chat_input[0].set_value('Show my order').run()
    assert not ui.exception and ui.session_state['messages']
    return ui, elapsed, calls


def test_ui_clears_expired_identity_and_private_screen_at_returned_deadline(monkeypatch):
    ui, elapsed, calls = ui_session(monkeypatch, expires_in=10)
    assert not any(url.endswith('/customers/session') for url, _ in calls)
    elapsed[0] += 10.1
    ui.run()
    assert not ui.exception
    assert ui.session_state['customer_token'] == '' and ui.session_state['customer_id'] == ''
    assert ui.session_state['messages'] == [] and ui.session_state['conversation_id'] is None
    assert any('session ended' in error.value for error in ui.error)
    assert not any('Signed in as' in caption.value for caption in ui.caption)


def test_ui_detects_server_revocation_and_does_not_check_each_rerun(monkeypatch):
    ui, elapsed, calls = ui_session(monkeypatch, session_status=401)
    ui.run()
    assert not any(url.endswith('/customers/session') for url, _ in calls)
    elapsed[0] += 15.1
    ui.run()
    assert not ui.exception and ui.session_state['customer_token'] == ''
    assert ui.session_state['messages'] == [] and ui.session_state['pending'] is None
    assert len([url for url, _ in calls if url.endswith('/customers/session')]) == 1
    assert any('session ended' in error.value for error in ui.error)


def test_ui_distinguishes_api_configuration_error_from_revoked_customer_session(monkeypatch):
    ui, elapsed, calls = ui_session(monkeypatch, session_status=403)
    elapsed[0] += 15.1
    ui.run()
    assert not ui.exception and ui.session_state['customer_token'] == 'x' * 43
    assert ui.session_state['messages'] and ui.session_state['customer_id'] == 'CUST001'
    assert any('Unable to check your session' in warning.value for warning in ui.warning)
