"""Refund authorization, immutable request terms, and financial regression checks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import pytest

from database.connection import connect, transaction
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import request_order, place_order, process_refund

refund = scoped_tool(process_refund)


def order(items=None, *, placed=True):
    result = request_order('CUST001', items or [{'item_code': 'ITEM001', 'quantity': 2}])
    assert result['ok'], result
    if placed:
        assert place_order(result['order_id'], 'CUST001')['ok']
    return result['order_id']


def request(oid, **kwargs):
    return refund(oid, 'CUST001', 'Return', execution_customer_id='CUST001', **kwargs)


def approve(oid, role='admin', **kwargs):
    return refund(oid, 'CUST001', 'Return', action='approve', confirmed=True,
                  execution_mode=role, **kwargs)


def scalar(query, parameters=()):
    with closing(connect()) as conn:
        return conn.execute(query, parameters).fetchone()[0]


@pytest.mark.parametrize('role', ['admin', 'cashier'])
@pytest.mark.parametrize('evidence', [{'return_received': True}, {'waiver_reason': 'Manager assessed damage'}])
def test_receipt_or_waiver_cannot_create_customer_request(role, evidence):
    oid = order()
    result = approve(oid, role, **evidence)
    assert not result['ok'] and 'pending customer refund request' in result['error']
    assert scalar('SELECT COUNT(*) FROM refund_requests') == 0
    assert scalar('SELECT refund_amount FROM orders') is None


@pytest.mark.parametrize('action', ['quote', 'request'])
def test_explicit_refund_amount_cannot_include_shipping(action):
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='10' WHERE key='shipping_amount'")
    oid = order()
    result = request(oid, action=action, amount='169.80')
    assert not result['ok'] and 'shipping is nonrefundable' in result['error']
    assert request(oid, action=action, amount='159.80')['ok']


@pytest.mark.parametrize('field,value', [
    ('reason', None), ('reason', 99), ('reason', []),
    ('waiver_reason', True), ('waiver_reason', ['damage']),
    ('return_received', 'true'), ('return_received', 1), ('confirmed', 'yes'),
    ('target_order_id', ''), ('target_order_id', ['order']),
])
def test_invalid_refund_arguments_return_failure_without_writes(field, value):
    oid = order()
    args = dict(order_id=oid, customer_id='CUST001', reason='Return', execution_customer_id='CUST001')
    args[field] = value
    result = refund(**args)
    assert not result['ok'], result
    assert scalar('SELECT COUNT(*) FROM refund_requests') == 0
    assert scalar("SELECT COUNT(*) FROM billing_audit WHERE action='process_refund'") == 0


@pytest.mark.parametrize('evidence', [{'return_received': True}, {'waiver_reason': 'I am the manager'}])
def test_customer_cannot_pre_authorize_own_return(evidence):
    result = request(order(), amount='10', **evidence)
    assert not result['ok']
    assert scalar('SELECT COUNT(*) FROM refund_requests') == 0


@pytest.mark.parametrize('changes', [
    {'disposition': 'original_method'},
    {'disposition': 'another_card'},
    {'items': [{'item_code': 'ITEM001', 'quantity': 2}]},
])
def test_staff_cannot_replace_customer_return_terms(changes):
    oid = order()
    assert request(oid, items=[{'item_code': 'ITEM001', 'quantity': 1}], disposition='store_credit')['ok']
    result = approve(oid, return_received=True, **changes)
    assert not result['ok'], result
    assert scalar('SELECT refund_amount FROM orders') is None
    assert scalar('SELECT return_received FROM refund_details') == 0
    assert scalar('SELECT COUNT(*) FROM store_credit_ledger') == 0
    assert approve(oid, return_received=True)['disposition'] == 'store_credit'


def test_staff_cannot_redirect_credit_to_different_owned_cart():
    oid, target, other = order(), order(placed=False), order(placed=False)
    assert request(oid, amount='10', disposition='store_credit', target_order_id=target)['ok']
    assert not approve(oid, target_order_id=other, return_received=True)['ok']
    assert approve(oid, target_order_id=target, return_received=True)['ok']
    assert scalar('SELECT credit_requested FROM order_service WHERE order_id=?', (target,)) == 10
    assert scalar('SELECT COUNT(*) FROM order_service WHERE order_id=?', (other,)) == 0


def test_retry_return_item_order_is_irrelevant_and_has_no_duplicate_audit():
    items = [{'item_code': 'ITEM001', 'quantity': 1}, {'item_code': 'ITEM002', 'quantity': 1}]
    oid = order(items)
    assert request(oid, items=items)['ok']
    assert request(oid, items=list(reversed(items)))['ok']
    assert scalar("SELECT COUNT(*) FROM billing_audit WHERE action='process_refund'") == 1
    assert approve(oid, items=list(reversed(items)), return_received=True)['ok']


def test_approval_rechecks_billed_return_terms_and_rolls_back_receipt():
    oid = order()
    assert request(oid, items=[{'item_code': 'ITEM001', 'quantity': 1}])['ok']
    with transaction() as conn:
        conn.execute('UPDATE invoice_lines SET line_total=line_total-10 WHERE order_id=?', (oid,))
    result = approve(oid, return_received=True)
    assert not result['ok'], result
    assert scalar('SELECT refund_amount FROM orders') is None
    assert scalar('SELECT return_received FROM refund_details') == 0


@pytest.mark.parametrize('invalid', ['{}', '[null]', '[{}]', '"invalid"'])
def test_invalid_imported_return_details_fail_closed(invalid):
    oid = order()
    assert request(oid, amount='10')['ok']
    with transaction() as conn:
        conn.execute('UPDATE refund_details SET items_json=? WHERE order_id=?', (invalid, oid))
    result = approve(oid, return_received=True)
    assert not result['ok'] and 'Stored return details' in result['error']
    assert scalar('SELECT status FROM refund_requests') == 'pending'


def test_simultaneous_staff_approval_issues_store_credit_once():
    oid = order()
    assert request(oid, amount='79.90', disposition='store_credit')['ok']
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda role: approve(oid, role, return_received=True), ['admin', 'cashier'] * 2))
    assert sum(result['ok'] for result in results) == 1
    assert scalar("SELECT COUNT(*) FROM store_credit_ledger WHERE kind='refund_credit'") == 1
    assert scalar('SELECT amount FROM store_credit_ledger') == 79.90
    assert scalar("SELECT COUNT(*) FROM billing_audit WHERE action='process_refund'") == 2


def test_pending_amount_cannot_override_nonrefundable_shipping():
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='10' WHERE key='shipping_amount'")
    oid = order()
    assert request(oid, amount='159.80')['ok']
    with transaction() as conn:
        conn.execute('UPDATE refund_requests SET amount=169.80 WHERE order_id=?', (oid,))
    result = approve(oid, return_received=True)
    assert not result['ok'] and 'shipping is nonrefundable' in result['error']
    assert scalar('SELECT refund_amount FROM orders') is None


def test_closed_target_cart_is_not_reported_as_credited():
    oid, target = order(), order(placed=False)
    assert request(oid, amount='10', disposition='store_credit', target_order_id=target)['ok']
    assert place_order(target, 'CUST001')['ok']
    result = approve(oid, return_received=True)
    assert result['ok'] and result['disposition'] == 'store_credit'
    assert result['credit_target_order_id'] is None
    assert result['credit_target_allocation'] == '0.00'
    assert result['requested_credit_target_order_id'] == target
    assert scalar("SELECT amount FROM store_credit_ledger WHERE kind='refund_credit'") == 10
