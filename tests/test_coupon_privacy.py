from contextlib import closing

import pytest

from database.connection import connect, transaction
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import apply_promotion, request_order
from mcp_server.tools.database import execute_sql_query, schema_hint


def draft(customer_id='CUST001'):
    return request_order(customer_id, [{'item_code': 'ITEM001', 'quantity': 1}])['order_id']


@pytest.mark.parametrize('customer_id', [None, 'CUST001'])
@pytest.mark.parametrize('query', [
    'SELECT promo_code FROM promotions',
    'SELECT promo_code FROM main.promotions ORDER BY discount_value DESC LIMIT 1',
    'WITH codes AS (SELECT * FROM promotions) SELECT group_concat(promo_code) FROM codes',
])
def test_customer_cannot_list_or_select_store_coupon_codes(customer_id, query):
    result = scoped_tool(execute_sql_query)(query, execution_customer_id=customer_id)
    assert not result['ok']
    assert 'SAVE10' not in str(result) and 'SAVE500' not in str(result)


def test_coupon_table_and_metadata_are_hidden_from_customer_but_available_to_staff():
    assert 'promotions(' not in schema_hint('customer')
    assert 'promotions(' in schema_hint('admin')
    read = scoped_tool(execute_sql_query)
    assert read('PRAGMA table_info(promotions)', execution_customer_id='CUST001')['rows'] == []
    for mode in ('admin', 'cashier'):
        result = read('SELECT COUNT(*) FROM promotions', execution_mode=mode)
        assert result['ok'] and result['rows'][0][0] > 0


def test_customer_cannot_learn_another_customers_used_coupon():
    own, other = draft(), draft('CUST002')
    assert apply_promotion(own, 'CUST001', 'SAVE10')['ok']
    assert apply_promotion(other, 'CUST002', 'SAVE500')['ok']
    read = scoped_tool(execute_sql_query)
    result = read('SELECT DISTINCT promo_code FROM orders WHERE promo_code IS NOT NULL',
                  execution_customer_id='CUST001')
    assert result['rows'] == [['SAVE10']]
    assert read('SELECT promo_code FROM orders')['rows'] == []


@pytest.mark.parametrize('action', ['apply', 'validate'])
def test_customer_coupon_calls_need_trusted_supplied_code_evidence(action):
    oid = draft()
    tool = scoped_tool(apply_promotion)
    kwargs = dict(order_id=oid, customer_id='CUST001', promo_code='SAVE500', action=action,
                  execution_customer_id='CUST001')
    for supplied in (None, [], ['SAVE10'], 'SAVE500', [None, 500]):
        result = tool(**kwargs, execution_coupon_codes=supplied)
        assert not result['ok'] and 'explicitly supplied' in result['error']
    with closing(connect()) as conn:
        assert conn.execute('SELECT promo_code FROM orders WHERE order_id=?', (oid,)).fetchone()[0] is None


def test_supplied_coupon_is_case_insensitive_and_same_code_never_stacks():
    oid = draft()
    tool = scoped_tool(apply_promotion)
    kwargs = dict(order_id=oid, customer_id='CUST001', promo_code=' save10 ',
                  execution_customer_id='CUST001', execution_coupon_codes=['SaVe10'])
    first = tool(**kwargs)
    repeated = tool(**kwargs)
    assert first['ok'] and first['promo_code'] == 'SAVE10'
    assert first['discount_applied'] == repeated['discount_applied']
    denied = tool(**{**kwargs, 'promo_code': 'SAVE500', 'execution_coupon_codes': ['SAVE500']})
    assert not denied['ok'] and 'one coupon' in denied['error']


def test_coupon_validation_without_draft_never_changes_orders():
    with closing(connect()) as conn:
        before = conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
    result = scoped_tool(apply_promotion)(None, 'CUST001', 'save10', action='validate',
        execution_customer_id='CUST001', execution_coupon_codes=['save10'])
    assert result['ok'] and result['valid'] and result['applied'] is False
    assert result['discount_type'] == 'percent' and result['promo_code'] == 'SAVE10'
    with closing(connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == before


def test_coupon_validation_with_draft_is_only_a_preview_and_checks_expiry():
    oid = draft()
    result = apply_promotion(oid, 'CUST001', 'save10', action='validate')
    assert result['ok'] and result['applied'] is False
    with closing(connect()) as conn:
        row = conn.execute('SELECT promo_code,discount_applied FROM orders WHERE order_id=?', (oid,)).fetchone()
        assert row['promo_code'] is None and row['discount_applied'] == 0
    with transaction() as conn:
        conn.execute("UPDATE promotions SET expires_on=date('now','-1 day') WHERE promo_code='SAVE10'")
    expired = apply_promotion(None, 'CUST001', 'SAVE10', action='validate')
    assert not expired['ok'] and 'expired' in expired['error']


def test_ambiguous_coupon_case_variants_are_not_guessed():
    with transaction() as conn:
        conn.execute("INSERT INTO promotions(promo_code,discount_type,discount_value,expires_on) "
                     "VALUES ('save10','flat',20,date('now','+30 days'))")
    result = apply_promotion(draft(), 'CUST001', 'SAVE10')
    assert not result['ok'] and 'ambiguous' in result['error']
