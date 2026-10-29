"""Delivery selection, privacy and checkout invariants using real SQLite transactions."""
from contextlib import closing
from datetime import date
import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from api import customer_auth
from api.main import app
from config import settings
from context.shopping_state import ShoppingTracker, order_snapshot
from database.connection import connect, transaction
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import request_order, place_order
from mcp_server.tools.operations_tools import manage_customer, manage_draft


FIELDS = {
    'full_name': 'Alex Morgan', 'phone': '+1 202 555 0142',
    'address_line1': '16 Pine Street', 'address_line2': 'Apartment 4',
    'city': 'Seattle', 'state': 'Washington', 'postal_code': '98101',
    'country': 'US', 'landmark': '', 'label': 'Home',
}
REQUIRED = ('full_name', 'phone', 'address_line1', 'city', 'state', 'postal_code', 'country')


@pytest.fixture(autouse=True)
def empty_addresses(isolated_db):
    # Keep the negative checkout cases independent of any seeded address book.
    with transaction() as conn:
        conn.execute('DELETE FROM order_delivery')
        conn.execute('DELETE FROM customer_addresses')


def address(customer='CUST001', **changes):
    from database.addresses import save_address
    return save_address(customer, {**FIELDS, **changes})


def draft(order_id='DELIVERY-ORDER', customer='CUST001'):
    result = request_order(customer, [{'item_code': 'ITEM001', 'quantity': 1}], order_id)
    assert result['ok'], result
    return order_id


def select(order_id, saved, customer='CUST001'):
    from database.addresses import select_order_address
    return select_order_address(order_id, customer, saved['address_id'])


def scalar(sql, args=()):
    with closing(connect(readonly=True)) as conn:
        return conn.execute(sql, args).fetchone()[0]


def checkout_state(order_id):
    with closing(connect(readonly=True)) as conn:
        return {
            'order': tuple(conn.execute('SELECT * FROM orders WHERE order_id=?', (order_id,)).fetchone()),
            'inventory': [tuple(row) for row in conn.execute('SELECT item_code,availablequantity FROM items ORDER BY item_code')],
            'fulfillment': [tuple(row) for row in conn.execute('SELECT * FROM order_fulfilled ORDER BY id')],
            'lines': [tuple(row) for row in conn.execute('SELECT * FROM invoice_lines ORDER BY id')],
            'credit': [tuple(row) for row in conn.execute('SELECT * FROM store_credit_ledger ORDER BY id')],
        }


@pytest.mark.parametrize('field', REQUIRED)
@pytest.mark.parametrize('value', ['', '   ', None, 123, True, 'x' * 10001])
def test_required_address_fields_are_validated_atomically(field, value):
    from database.addresses import save_address, list_addresses
    with pytest.raises(ValueError):
        save_address('CUST001', {**FIELDS, field: value})
    assert list_addresses('CUST001') == []


@pytest.mark.parametrize('field', REQUIRED)
def test_missing_required_address_field_is_rejected(field):
    from database.addresses import save_address
    fields = dict(FIELDS)
    fields.pop(field)
    with pytest.raises(ValueError):
        save_address('CUST001', fields)


def test_optional_fields_can_be_omitted_and_international_names_preserved():
    from database.addresses import save_address
    fields = {key: FIELDS[key] for key in REQUIRED}
    fields.update(full_name='  José Núñez  ', city='Montréal', state='Québec',
                  country='ca', postal_code='H2X 1Y4')
    saved = save_address('CUST001', fields)
    assert saved['full_name'] == 'José Núñez'
    assert saved['city'] == 'Montréal' and saved['country'] == 'CA'


@pytest.mark.parametrize('changes', [
    {'is_default': 1}, {'is_default': 'true'}, {'country': 'USA'},
    {'country': '12'}, {'phone': 'call me later'}, {'customer_id': 'CUST002'},
    {'address_id': 'ATTACKER-CHOSEN-ID'}, {'active': False},
])
def test_address_fields_do_not_accept_scope_or_internal_state(changes):
    from database.addresses import save_address
    with pytest.raises(ValueError):
        save_address('CUST001', {**FIELDS, **changes})
    assert scalar('SELECT COUNT(*) FROM customer_addresses') == 0


def test_multiple_addresses_default_switch_and_delete_promote_one_default():
    from database.addresses import list_addresses, set_default_address, delete_address
    first = address()
    second = address(label='Work', address_line1='40 Lake Avenue')
    assert first['address_id'] != second['address_id']
    assert sum(bool(row['is_default']) for row in list_addresses('CUST001')) == 1
    assert next(row for row in list_addresses('CUST001') if row['is_default'])['address_id'] == first['address_id']
    set_default_address('CUST001', second['address_id'])
    assert next(row for row in list_addresses('CUST001') if row['is_default'])['address_id'] == second['address_id']
    delete_address('CUST001', second['address_id'])
    remaining = list_addresses('CUST001')
    assert len(remaining) == 1 and remaining[0]['address_id'] == first['address_id']
    assert remaining[0]['is_default']
    delete_address('CUST001', first['address_id'])
    assert list_addresses('CUST001') == []


def test_an_address_default_does_not_silently_choose_a_checkout_destination():
    from database.addresses import set_default_address
    oid = draft()
    first = address()
    set_default_address('CUST001', first['address_id'])
    before = checkout_state(oid)
    result = place_order(oid, 'CUST001')
    assert not result['ok'] and 'address' in result['error'].lower()
    assert checkout_state(oid) == before
    assert scalar('SELECT COUNT(*) FROM order_delivery WHERE order_id=?', (oid,)) == 0


def test_unsetting_default_prefers_another_address_without_changing_selected_delivery():
    from database.addresses import list_addresses, save_address
    first = address()
    second = address(label='Work', address_line1='40 Lake Avenue')
    oid = draft()
    select(oid, first)
    original = order_snapshot(oid, 'CUST001')
    save_address('CUST001', {'is_default': False}, first['address_id'])
    rows = list_addresses('CUST001')
    assert sum(row['is_default'] for row in rows) == 1
    assert next(row for row in rows if row['is_default'])['address_id'] == second['address_id']
    assert order_snapshot(oid, 'CUST001')['fingerprint'] == original['fingerprint']


@pytest.mark.parametrize('extra', [{'name':'Changed name'}, {'company':'Changed company'},
                                   {'email':'other@example.com'}, {'billing_address':'Other billing address'}])
def test_address_tools_reject_silently_ignored_profile_changes(extra):
    from mcp_server.tools.operations_tools import manage_customer
    result = manage_customer('CUST001', action='add_address', address=FIELDS, confirmed=True, **extra)
    assert not result['ok']
    assert scalar('SELECT COUNT(*) FROM customer_addresses') == 0


def test_address_selection_rejects_combined_basket_changes():
    oid = draft()
    saved = address()
    result = manage_draft(oid, 'CUST001', 'select_address', address_id=saved['address_id'],
                          items=[{'item_code':'ITEM002','quantity':2}], confirmed=True)
    assert not result['ok']
    assert scalar('SELECT COUNT(*) FROM order_delivery') == 0
    assert scalar('SELECT item_code FROM order_requests WHERE order_id=?', (oid,)) == 'ITEM001'


def test_missing_address_checkout_changes_neither_stock_invoice_nor_credit():
    oid = draft()
    before = checkout_state(oid)
    assert not place_order(oid, 'CUST001')['ok']
    assert checkout_state(oid) == before


def test_selected_address_places_atomically_and_placement_retry_is_idempotent():
    oid = draft()
    selected = select(oid, address())
    assert selected['address_id']
    first = place_order(oid, 'CUST001')
    assert first['ok'], first
    after = checkout_state(oid)
    second = place_order(oid, 'CUST001')
    assert second['ok'] and second['already_placed']
    assert second['invoice_number'] == first['invoice_number']
    assert checkout_state(oid) == after
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == 39


@pytest.mark.parametrize('operation', ['select', 'update', 'delete', 'default'])
def test_customer_cannot_modify_or_select_another_customers_address(operation):
    from database.addresses import (save_address, delete_address, set_default_address,
                                    list_addresses, select_order_address)
    oid = draft()
    other = address('CUST002')
    actions = {
        'select': lambda: select_order_address(oid, 'CUST001', other['address_id']),
        'update': lambda: save_address('CUST001', {**FIELDS, 'city': 'Portland'}, other['address_id']),
        'delete': lambda: delete_address('CUST001', other['address_id']),
        'default': lambda: set_default_address('CUST001', other['address_id']),
    }
    with pytest.raises(ValueError):
        actions[operation]()
    assert list_addresses('CUST001') == []
    assert list_addresses('CUST002')[0]['city'] == FIELDS['city']
    assert scalar('SELECT COUNT(*) FROM order_delivery') == 0


def test_owned_address_cannot_select_another_customers_order():
    from database.addresses import select_order_address
    other_order = draft('OTHER-ORDER', 'CUST002')
    own = address()
    with pytest.raises(ValueError):
        select_order_address(other_order, 'CUST001', own['address_id'])
    assert scalar('SELECT COUNT(*) FROM order_delivery') == 0


def test_database_owner_constraints_reject_cross_customer_delivery_links():
    oid = draft()
    other = address('CUST002')
    with pytest.raises(sqlite3.IntegrityError):
        with transaction() as conn:
            conn.execute('INSERT INTO order_delivery(order_id,customer_id,address_id,address_snapshot) VALUES (?,?,?,?)',
                         (oid, 'CUST001', other['address_id'], json.dumps(FIELDS)))
    assert scalar('SELECT COUNT(*) FROM order_delivery') == 0


@pytest.mark.parametrize('mutation', ['update', 'delete'])
def test_address_changes_invalidate_draft_selection_and_prior_confirmation(mutation):
    from database.addresses import save_address, delete_address
    oid = draft()
    saved = address()
    select(oid, saved)
    initial = order_snapshot(oid, 'CUST001')
    tracker = ShoppingTracker(None, 'CUST001')
    summary = 'Place the keyboard order for USD 79.90 to Alex Morgan, 16 Pine Street, Apartment 4, Seattle, Washington 98101, US, phone +1 202 555 0142?'
    tracker.finish(json.dumps({'reply': summary, 'shopping_update': {'active_draft_id': oid,
        'pending_confirmation': {'action': 'place_order', 'order_id': oid, 'summary': summary}}}))
    assert tracker.state['pending_confirmation']
    if mutation == 'update':
        save_address('CUST001', {**FIELDS, 'address_line1': '88 Forest Road'}, saved['address_id'])
    else:
        delete_address('CUST001', saved['address_id'])
    assert order_snapshot(oid, 'CUST001')['fingerprint'] != initial['fingerprint']
    assert ShoppingTracker(tracker.state, 'CUST001').state['pending_confirmation'] is None
    before = checkout_state(oid)
    assert not place_order(oid, 'CUST001')['ok']
    assert checkout_state(oid) == before


def test_switching_selected_address_changes_fingerprint_and_repeating_it_does_not():
    oid = draft()
    first, second = address(), address(address_line1='19 Cedar Lane')
    select(oid, first)
    fingerprint = order_snapshot(oid, 'CUST001')['fingerprint']
    select(oid, first)
    assert order_snapshot(oid, 'CUST001')['fingerprint'] == fingerprint
    select(oid, second)
    assert order_snapshot(oid, 'CUST001')['fingerprint'] != fingerprint


@pytest.mark.parametrize('mutation', ['update', 'delete'])
def test_placed_order_retains_immutable_delivery_snapshot_after_address_book_changes(mutation):
    from database.addresses import save_address, delete_address, select_order_address
    oid = draft()
    saved = address()
    select(oid, saved)
    placed = place_order(oid, 'CUST001')
    assert placed['ok'], placed
    snapshot = scalar('SELECT address_snapshot FROM order_delivery WHERE order_id=?', (oid,))
    fingerprint = order_snapshot(oid, 'CUST001')['fingerprint']
    if mutation == 'update':
        save_address('CUST001', {**FIELDS, 'address_line1': '88 Forest Road'}, saved['address_id'])
    else:
        delete_address('CUST001', saved['address_id'])
    assert scalar('SELECT address_snapshot FROM order_delivery WHERE order_id=?', (oid,)) == snapshot
    assert order_snapshot(oid, 'CUST001')['fingerprint'] == fingerprint
    with pytest.raises(ValueError):
        select_order_address(oid, 'CUST001', address()['address_id'])
    assert place_order(oid, 'CUST001')['already_placed']


def test_cancelled_draft_does_not_accept_a_delivery_selection():
    from database.addresses import select_order_address
    oid = draft()
    assert manage_draft(oid, 'CUST001', 'cancel', reason='Duplicate', confirmed=True)['ok']
    with pytest.raises(ValueError):
        select_order_address(oid, 'CUST001', address()['address_id'])


def test_out_of_band_address_edit_cannot_ship_to_an_unreviewed_snapshot():
    oid = draft()
    saved = address()
    select(oid, saved)
    with transaction() as conn:
        conn.execute('UPDATE customer_addresses SET address_line1=? WHERE address_id=?', ('Unreviewed street', saved['address_id']))
    before = checkout_state(oid)
    assert not place_order(oid, 'CUST001')['ok']
    assert checkout_state(oid) == before


def test_checkout_uses_exact_reviewed_delivery_terms_and_rolls_back_changed_destination():
    oid = draft()
    first, second = address(), address(address_line1='19 Cedar Lane')
    reviewed = select(oid, first)
    expected = {key: reviewed[key] for key in ('address_id', 'delivery_address')}
    select(oid, second)
    before = checkout_state(oid)
    result = place_order(oid, 'CUST001', expected_delivery=expected)
    assert not result['ok'] and 'address' in result['error'].lower()
    assert checkout_state(oid) == before
    current = select(oid, second)
    current = {key: current[key] for key in ('address_id', 'delivery_address')}
    assert place_order(oid, 'CUST001', expected_delivery=current)['ok']


def test_address_audit_records_exclude_private_street_and_phone_values():
    oid = draft()
    selected = select(oid, address())
    reviewed = {key: selected[key] for key in ('address_id', 'delivery_address')}
    assert place_order(oid, 'CUST001', expected_delivery=reviewed)['ok']
    with closing(connect(readonly=True)) as conn:
        audit = '\n'.join(row[0] for row in conn.execute('SELECT details_json FROM billing_audit'))
    assert FIELDS['address_line1'] not in audit
    assert FIELDS['phone'] not in audit


def test_existing_customer_tool_can_manage_addresses_without_requiring_profile_name():
    tool = scoped_tool(manage_customer)
    assert not tool('CUST001', action='add_address', address=FIELDS,
                    execution_customer_id='CUST001')['ok']
    added = tool('CUST001', action='add_address', address=FIELDS, confirmed=True,
                 execution_customer_id='CUST001')
    assert added['ok'], added
    aid = added['address_id']
    changed = tool('CUST001', action='update_address', address_id=aid,
                   address={'label': 'Office'}, confirmed=True, execution_customer_id='CUST001')
    assert changed['ok'] and changed['label'] == 'Office'
    assert not tool('CUST001', action='delete_address', address_id=aid, confirmed=True,
                    execution_customer_id='CUST002')['ok']
    assert not tool('CUST001', action='delete_address', address_id=aid, confirmed=True,
                    execution_mode='admin')['ok']
    assert tool('CUST001', action='delete_address', address_id=aid, confirmed=True,
                execution_customer_id='CUST001')['ok']


def test_existing_draft_tool_selects_owned_address_without_placing_order():
    oid = draft()
    saved = address()
    result = scoped_tool(manage_draft)(oid, 'CUST001', action='select_address',
        address_id=saved['address_id'], confirmed=True, execution_customer_id='CUST001')
    assert result['ok'] and result['address_id'] == saved['address_id']
    assert scalar('SELECT order_status FROM orders WHERE order_id=?', (oid,)) == 'draft'
    assert scalar('SELECT COUNT(*) FROM invoice_lines') == 0
    assert not scoped_tool(manage_draft)(oid, 'CUST001', action='select_address',
        address_id=saved['address_id'], confirmed=True, execution_mode='admin')['ok']


class Engine:
    async def chat(self, *args, **kwargs):
        return {'response': 'Ready.'}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, 'API_KEY', '')
    app.state.engine = Engine()
    with TestClient(app) as current:
        yield current


def sign_in(client, customer='CUST001'):
    password = 'Delivery customer password'
    customer_auth.set_password(customer, password)
    result = client.post('/customers/login', json={'customer_id': customer, 'password': password})
    assert result.status_code == 200, result.text
    return {'X-Customer-Token': result.json()['token']}


@pytest.mark.parametrize('method,path,body', [
    ('get', '/customers/addresses', None),
    ('post', '/customers/addresses', FIELDS),
    ('put', '/customers/addresses/UNKNOWN', FIELDS),
    ('delete', '/customers/addresses/UNKNOWN', None),
    ('post', '/customers/addresses/UNKNOWN/default', None),
    ('get', '/customers/checkout', None),
    ('put', '/customers/orders/UNKNOWN/delivery-address', {'address_id': 'UNKNOWN'}),
])
def test_address_routes_require_customer_session_even_with_staff_headers(client, method, path, body):
    options = {'headers': {'X-Admin-Key': 'staff-is-not-customer', 'X-Cashier-Key': 'also-not-customer'}}
    if body is not None:
        options['json'] = body
    result = client.request(method.upper(), path, **options)
    assert result.status_code == 401


def test_address_api_crud_selection_and_cross_customer_privacy(client):
    owner = sign_in(client)
    other = sign_in(client, 'CUST002')
    oid = draft()
    added = client.post('/customers/addresses', json=FIELDS, headers=owner)
    assert added.status_code == 201, added.text
    assert added.headers['cache-control'] == 'no-store'
    aid = added.json()['address']['address_id']
    assert client.get('/customers/addresses', headers=other).json()['addresses'] == []
    assert client.get('/customers/checkout', headers=other).json()['orders'] == []
    assert client.put(f'/customers/orders/{oid}/delivery-address', json={'address_id': aid}, headers=other).status_code == 409
    assert client.put(f'/customers/addresses/{aid}', json={**FIELDS, 'city': 'Portland'}, headers=other).status_code == 409
    assert client.delete(f'/customers/addresses/{aid}', headers=other).status_code == 409
    assert client.post(f'/customers/addresses/{aid}/default', headers=other).status_code == 409
    selected = client.put(f'/customers/orders/{oid}/delivery-address', json={'address_id': aid}, headers=owner)
    assert selected.status_code == 200, selected.text
    assert selected.json()['delivery_address']['address_line1'] == FIELDS['address_line1']
    checkout = client.get('/customers/checkout', headers=owner)
    assert checkout.status_code == 200 and checkout.headers['cache-control'] == 'no-store'
    assert checkout.json()['orders'][0]['address_id'] == aid
    updated = client.put(f'/customers/addresses/{aid}', json={**FIELDS, 'city': 'Portland'}, headers=owner)
    assert updated.status_code == 200, updated.text
    assert client.get('/customers/checkout', headers=owner).json()['orders'][0]['address_id'] is None
    assert client.delete(f'/customers/addresses/{aid}', headers=owner).status_code == 200
    assert client.get('/customers/addresses', headers=owner).json()['addresses'] == []


def test_address_api_uses_token_owner_and_does_not_echo_invalid_private_input(client):
    owner = sign_in(client)
    result = client.post('/customers/addresses', json={**FIELDS, 'customer_id': 'CUST002'}, headers=owner)
    assert result.status_code == 422
    private = 'Private personal address value'
    result = client.post('/customers/addresses', json={**FIELDS, 'phone': {'secret': private}}, headers=owner)
    assert result.status_code == 422
    assert private not in result.text


def test_expired_customer_token_cannot_change_saved_addresses(client):
    owner = sign_in(client)
    saved = address()
    client.post('/customers/logout', headers=owner)
    assert client.put(f"/customers/addresses/{saved['address_id']}", json=FIELDS, headers=owner).status_code == 401
    assert client.get('/customers/addresses', headers=owner).status_code == 401


def test_semantic_address_selection_is_a_mutation_and_clears_old_placement_proposal(monkeypatch):
    # Replay supplies a semantic tool decision; actual MCP/SQL/approval code runs.
    from agent import billing_agent as module
    oid = draft()
    first, second = address(), address(address_line1='19 Cedar Lane')
    select(oid, first)
    tracker = ShoppingTracker(None, 'CUST001')
    summary = 'Place the keyboard order for USD 79.90 to Alex Morgan, 16 Pine Street, Apartment 4, Seattle, Washington 98101, US, phone +1 202 555 0142?'
    tracker.finish(json.dumps({'reply': summary, 'shopping_update': {'active_draft_id': oid,
        'pending_confirmation': {'action': 'place_order', 'order_id': oid, 'summary': summary}}}))
    assert tracker.state['pending_confirmation']
    tool = CreateResult(content=[FunctionCall(id=str(uuid4()), name='manage_draft', arguments=json.dumps({
        'order_id': oid, 'customer_id': 'CUST001', 'action': 'select_address', 'address_id': second['address_id']}))],
        finish_reason='function_calls', usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)
    replay = ReplayChatCompletionClient([tool, json.dumps({'reply': 'Delivery address selected. Please review the updated order before placement.', 'shopping_update': {}})])
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)
    changes = []

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            result = await engine.chat('Use my work address for this order instead.', [], 'CUST001',
                shopping_state=tracker.state, mutation_callback=lambda: changes.append(True))
            assert result['shopping_state']['pending_confirmation'] is None
            assert engine._workbench.tool_calls == 1
            assert changes == [True]
        finally:
            await engine.close()
    asyncio.run(run())
    assert scalar('SELECT address_id FROM order_delivery WHERE order_id=?', (oid,)) == second['address_id']
    assert scalar('SELECT order_invoice FROM orders WHERE order_id=?', (oid,)) is None


@pytest.mark.parametrize('forged_delivery', [False, True])
def test_approved_customer_checkout_uses_trusted_delivery_binding_through_real_mcp(monkeypatch, forged_delivery):
    from agent import billing_agent as module
    oid = draft()
    saved = address()
    selection = select(oid, saved)
    tracker = ShoppingTracker(None, 'CUST001')
    summary = 'Place one mechanical keyboard for USD 79.90 to Alex Morgan, 16 Pine Street, Apartment 4, Seattle, Washington 98101, US, phone +1 202 555 0142?'
    proposed = {'action': 'place_order', 'order_id': oid, 'summary': summary}
    if forged_delivery:
        proposed['delivery'] = {'address_id': 'FORGED-PROPOSAL', 'delivery_address': {'city': 'Invented'}}
    visible = tracker.finish(json.dumps({'reply': summary, 'shopping_update': {'active_draft_id': oid,
        'pending_confirmation': proposed}}))
    proposal = tracker.state['pending_confirmation']
    assert proposal and proposal['delivery'] == {key: selection[key] for key in ('address_id', 'delivery_address')}
    arguments = {'order_id': oid, 'customer_id': 'CUST001'}
    if forged_delivery:
        arguments['expected_delivery'] = {'address_id': 'FORGED-ADDRESS',
            'delivery_address': {**FIELDS, 'address_line1': 'Unapproved destination'}}
    tool = CreateResult(content=[FunctionCall(id=str(uuid4()), name='place_order', arguments=json.dumps(arguments))],
        finish_reason='function_calls', usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)
    replay = ReplayChatCompletionClient([tool, json.dumps({'reply': 'Your order was placed.', 'shopping_update': {}})])
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)
    dispatched = []

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            schemas = await engine._workbench.list_tools()
            checkout = next(schema for schema in schemas if schema['name'] == 'place_order')
            assert 'expected_delivery' not in checkout['parameters']['properties']
            assert 'expected_delivery' not in checkout['parameters'].get('required', [])
            result = await engine.chat('Those details are correct; please proceed.',
                [{'role': 'assistant', 'content': visible}], 'CUST001', shopping_state=tracker.state,
                mutation_callback=lambda: dispatched.append(True))
            assert engine._workbench.tool_calls == 1
            assert not engine._workbench.action_blocks
            assert result['shopping_state']['pending_confirmation'] is None
            assert dispatched == [True]
            assert len(replay.create_calls) == 2
        finally:
            await engine.close()
    asyncio.run(run())
    assert scalar('SELECT order_status FROM orders WHERE order_id=?', (oid,)) == 'placed'
    assert scalar('SELECT address_id FROM order_delivery WHERE order_id=?', (oid,)) == saved['address_id']
    assert json.loads(scalar('SELECT address_snapshot FROM order_delivery WHERE order_id=?', (oid,)))['address_line1'] == FIELDS['address_line1']
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == 39


def test_unselected_address_cannot_create_a_placement_proposal_or_dispatch_checkout(monkeypatch):
    from agent import billing_agent as module
    oid = draft()
    address()  # Having a default in the address book is not a checkout selection.
    tracker = ShoppingTracker(None, 'CUST001')
    summary = 'Place the keyboard order for USD 79.90?'
    tracker.finish(json.dumps({'reply': summary, 'shopping_update': {'active_draft_id': oid,
        'pending_confirmation': {'action': 'place_order', 'order_id': oid, 'summary': summary}}}))
    assert tracker.state['pending_confirmation'] is None
    before = checkout_state(oid)
    tool = CreateResult(content=[FunctionCall(id=str(uuid4()), name='place_order', arguments=json.dumps({
        'order_id': oid, 'customer_id': 'CUST001'}))], finish_reason='function_calls',
        usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)
    replay = ReplayChatCompletionClient([tool, json.dumps({'reply': 'Your order was placed.', 'shopping_update': {}})])
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            result = await engine.chat('Please proceed.', [], 'CUST001', shopping_state=tracker.state)
            assert engine._workbench.tool_calls == 0
            assert engine._workbench.action_blocks
            assert result['shopping_state']['pending_confirmation'] is None
            assert 'Your order was placed.' not in result['response']
        finally:
            await engine.close()
    asyncio.run(run())
    assert checkout_state(oid) == before


def test_placement_review_always_displays_the_trusted_selected_address_when_model_omits_it():
    oid = draft()
    saved = address(landmark='Near the central library')
    selected = select(oid, saved)
    tracker = ShoppingTracker(None, 'CUST001')
    summary = 'Place one mechanical keyboard for USD 79.90?'
    reply = tracker.finish(json.dumps({'reply': summary, 'shopping_update': {'active_draft_id': oid,
        'pending_confirmation': {'action': 'place_order', 'order_id': oid, 'summary': summary,
            'delivery': {'address_id': 'UNTRUSTED', 'delivery_address': {'address_line1': 'Wrong street'}}}}}))
    expected = ('Delivery address for approval:\nFull name: Alex Morgan\nPhone: +1 202 555 0142\n'
                'Address line 1: 16 Pine Street\nAddress line 2: Apartment 4\nCity: Seattle\n'
                'State / province / region: Washington\nPostal code: 98101\nCountry: US\n'
                'Landmark: Near the central library')
    assert reply.startswith(tracker.state['pending_confirmation']['summary'] + '\nItems:\n')
    assert 'Total: USD 79.90' in reply and expected in reply
    assert 'Wrong street' not in reply and 'UNTRUSTED' not in reply
    assert tracker.state['pending_confirmation']['delivery'] == {
        key: selected[key] for key in ('address_id', 'delivery_address')}
    resumed = ShoppingTracker(json.loads(json.dumps(tracker.state)), 'CUST001')
    assert resumed.state['pending_confirmation']['delivery'] == tracker.state['pending_confirmation']['delivery']
    assert resumed.state['pending_confirmation']['order_fingerprint'] == order_snapshot(oid, 'CUST001')['fingerprint']


def test_new_address_tables_keep_customer_reads_isolated_and_cashier_address_book_hidden():
    from mcp_server.tools.database import execute_sql_query, schema_hint
    own, other = address(), address('CUST002', full_name='Jamie Chen')
    own_order, other_order = draft('OWN-ADDRESS'), draft('OTHER-ADDRESS', 'CUST002')
    select(own_order, own)
    select(other_order, other, 'CUST002')
    query = scoped_tool(execute_sql_query)
    customer = query('SELECT address_id,full_name FROM customer_addresses', execution_customer_id='CUST001')
    assert customer['ok'] and customer['rows'] == [[own['address_id'], FIELDS['full_name']]]
    delivery = query('SELECT order_id,address_id FROM order_delivery', execution_customer_id='CUST001')
    assert delivery['ok'] and delivery['rows'] == [[own_order, own['address_id']]]
    assert query('SELECT address_id FROM customer_addresses', execution_customer_id=None)['rows'] == []
    assert not query('SELECT address_id FROM customer_addresses', execution_mode='cashier')['ok']
    assert 'customer_addresses(' not in schema_hint('cashier')
    assert 'customer_addresses(' in schema_hint('customer')
    assert 'order_delivery(' in schema_hint('customer')


def test_record_generator_adds_multiple_address_choices_and_preserves_them_on_repeat(tmp_path):
    from generate_seed import generate_records
    path = tmp_path / 'address_records.db'
    first = generate_records(path, 42, date(2026, 10, 29))
    assert first['status'] == 'inserted'
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM customer_addresses').fetchone()[0] == 500
        assert conn.execute('SELECT COUNT(DISTINCT customer_id) FROM customer_addresses').fetchone()[0] == 400
        assert conn.execute('SELECT COUNT(*) FROM (SELECT customer_id FROM customer_addresses GROUP BY customer_id HAVING COUNT(*)=2)').fetchone()[0] == 100
        assert conn.execute('SELECT COUNT(*) FROM (SELECT customer_id FROM customer_addresses GROUP BY customer_id HAVING SUM(is_default)<>1)').fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM orders o JOIN order_delivery d ON d.order_id=o.order_id WHERE o.order_status='draft'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM orders o LEFT JOIN order_delivery d ON d.order_id=o.order_id WHERE o.order_status='placed' AND d.order_id IS NULL").fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM order_delivery').fetchone()[0] > 0
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        before = {table: conn.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                  for table in ('customer_addresses', 'order_delivery')}
        assert conn.execute('SELECT COUNT(*) FROM customer_credentials').fetchone()[0] == 0
    repeated = generate_records(path, 84, date(2027, 1, 1))
    assert repeated['status'] == 'already_seeded'
    with closing(sqlite3.connect(path)) as conn:
        after = {table: conn.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                 for table in before}
        assert after == before
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('mutation', ['update', 'delete'])
def test_invoice_pdf_uses_frozen_delivery_content_after_saved_address_change(monkeypatch, mutation):
    import reportlab.platypus
    from database.addresses import save_address, delete_address
    from database.invoice import generate_invoice, invoice_path
    oid = draft()
    saved = address()
    select(oid, saved)
    placed = place_order(oid, 'CUST001')
    assert placed['ok'], placed
    original_paragraph = reportlab.platypus.Paragraph
    rendered_text = []

    def paragraph(text, *args, **kwargs):
        rendered_text.append(str(text))
        return original_paragraph(text, *args, **kwargs)

    monkeypatch.setattr(reportlab.platypus, 'Paragraph', paragraph)
    if mutation == 'update':
        save_address('CUST001', {'address_line1': 'Unreviewed new destination'}, saved['address_id'])
    else:
        delete_address('CUST001', saved['address_id'])
    generated = generate_invoice(placed['invoice_number'], 'CUST001')
    assert generated['ready']
    path = invoice_path(placed['invoice_number'], 'CUST001')
    assert path.read_bytes().startswith(b'%PDF-') and path.stat().st_size > 1000
    assert 'Delivery address' in rendered_text
    assert FIELDS['address_line1'] in rendered_text and FIELDS['phone'] in rendered_text
    assert 'Unreviewed new destination' not in rendered_text
