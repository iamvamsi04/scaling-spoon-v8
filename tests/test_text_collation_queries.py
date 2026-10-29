"""Database comparisons ignore label casing without weakening account boundaries."""
from address_support import place_order_with_address, prepare_delivery
from contextlib import closing
import sqlite3

import pytest

from database.connection import connect, transaction
from mcp_server.access import customer_query_connection, scoped_tool
from mcp_server.tools.billing_tools import place_order, process_refund, request_order
from mcp_server.tools.database import execute_sql_query


read = scoped_tool(execute_sql_query)
refund = scoped_tool(process_refund)
ROLES = ['customer', 'cashier', 'admin']


@pytest.fixture
def labelled_records():
    with transaction() as conn:
        conn.execute("INSERT INTO customers VALUES ('cust001','alex morgan',NULL,NULL)")
        conn.executemany('INSERT INTO items VALUES (?,?,?,?,?,?)', [
            ('Case-SKU', 'Case-Item', 'Desk Light', 1, 2, 5),
            ('case-sku', 'case-item', 'desk light', 1, 2, 5),
        ])
        conn.executemany('INSERT INTO product_details VALUES (?,?,?,?)', [
            ('Case-Item', 'Desk Lighting', '', '{}'),
            ('case-item', 'desk lighting', '', '{}'),
        ])
        conn.executemany('INSERT INTO product_attributes VALUES (?,?,?,?,?)', [
            ('Case-Item', 'Wattage', None, 10, 'mW'),
            ('case-item', 'wattage', None, 20, 'MW'),
        ])
    for order_id, customer in [('Case-Order-01', 'CUST001'),
                               ('case-order-01', 'cust001'),
                               ('Case-Order-02', 'CUST001')]:
        draft = request_order(customer, [{'item_code': 'ITEM001', 'quantity': 1}], order_id)
        assert draft['ok'], draft
        assert place_order_with_address(order_id, customer)['ok']
        requested = refund(order_id, customer, 'Return', '5.00',
                           execution_customer_id=customer)
        assert requested['ok'], requested
    approved = refund('Case-Order-02', 'CUST001', 'Return', '5.00', action='approve',
                      confirmed=True, return_received=True, execution_mode='admin')
    assert approved['ok'], approved


def query(sql, mode, parameters=None):
    result = read(sql, parameters=parameters, execution_mode=mode,
                  execution_customer_id='CUST001' if mode == 'customer' else None)
    assert result['ok'], result
    return result['rows']


def pending_ids(mode):
    return [['Case-Order-01']] if mode == 'customer' else [['Case-Order-01'], ['case-order-01']]


@pytest.mark.parametrize('mode', ROLES)
@pytest.mark.parametrize('predicate,parameters', [
    ("r.status='PENDING'", None),
    ("'Pending'=r.status", None),
    ('r.status==?', ['pEnDiNg']),
    ('r.status IN (?,?)', ['PENDING', 'not_a_status']),
    ("r.status!='APPROVED'", None),
    ("r.status NOT IN ('APPROVED')", None),
    ("r.status IS 'PENDING'", None),
    ("r.status IS NOT 'APPROVED'", None),
])
def test_pending_request_comparisons_ignore_casing(labelled_records, mode, predicate, parameters):
    sql = 'SELECT r.order_id FROM refund_requests r WHERE ' + predicate + ' ORDER BY r.order_id'
    assert query(sql, mode, parameters) == pending_ids(mode)


@pytest.mark.parametrize('mode', ROLES)
def test_joined_statuses_use_matching_rules_on_their_own_columns(labelled_records, mode):
    rows = query("SELECT r.order_id FROM refund_requests r "
                 "JOIN orders o ON o.order_id=r.order_id "
                 "JOIN order_details d ON d.order_id=o.order_id "
                 "JOIN order_service s ON s.order_id=o.order_id "
                 "WHERE r.status='PENDING' AND o.order_status='PLACED' "
                 "AND d.status='ACTIVE' AND s.fulfillment_status='PLACED' ORDER BY r.order_id", mode)
    assert rows == pending_ids(mode)


@pytest.mark.parametrize('mode', ROLES)
def test_direct_cte_projection_retains_status_collation(labelled_records, mode):
    assert query('WITH requests AS (SELECT order_id,status AS request_status FROM refund_requests) '
                 'SELECT order_id FROM requests WHERE request_status=? ORDER BY order_id',
                 mode, ['PENDING']) == pending_ids(mode)


@pytest.mark.parametrize('mode', ROLES)
def test_computed_status_expression_has_explicit_case_insensitive_comparison(labelled_records, mode):
    assert query("SELECT order_id FROM refund_requests WHERE "
                 "COALESCE(TRIM(status),'') COLLATE NOCASE=? ORDER BY order_id",
                 mode, ['PENDING']) == pending_ids(mode)


@pytest.mark.parametrize('mode', ROLES)
def test_nullable_refund_status_and_numeric_values_keep_their_meaning(labelled_records, mode):
    assert query("SELECT order_id FROM orders WHERE refund_status='RECORDED'", mode) == [['Case-Order-02']]
    assert query('SELECT COUNT(*) FROM orders WHERE refund_status IS NULL', mode) == [[1 if mode == 'customer' else 2]]
    assert query('SELECT COUNT(*) FROM orders WHERE charged_price=?', mode, [79.9]) == [[2 if mode == 'customer' else 3]]


@pytest.mark.parametrize('mode', ROLES)
def test_customer_name_matching_does_not_expand_customer_scope(labelled_records, mode):
    expected = [['CUST001']] if mode == 'customer' else [['CUST001'], ['cust001']]
    assert query('SELECT customer_id FROM customers WHERE name=? ORDER BY customer_id',
                 mode, ['ALEX MORGAN']) == expected
    rows = query("SELECT name,COUNT(*) AS customers FROM customers WHERE name='aLeX mOrGaN' GROUP BY name", mode)
    assert len(rows) == 1
    assert rows[0][0].casefold() == 'alex morgan'
    assert rows[0][1] == (1 if mode == 'customer' else 2)


@pytest.mark.parametrize('mode', ROLES)
def test_quoted_legacy_column_casing_retains_matching_and_ownership_rules(labelled_records, mode):
    with transaction() as conn:
        conn.execute('ALTER TABLE customers RENAME COLUMN name TO "NaMe"')
    expected = [['CUST001']] if mode == 'customer' else [['CUST001'], ['cust001']]
    assert query('SELECT customer_id FROM customers WHERE "NaMe"=? ORDER BY customer_id',
                 mode, ['ALEX MORGAN']) == expected
    assert query("SELECT customer_id FROM customers WHERE customer_id='CuSt001'", mode) == []


@pytest.mark.parametrize('mode', ROLES)
@pytest.mark.parametrize('table,column,alias,value', [
    ('items', 'name', 'product_name', 'DESK LIGHT'),
    ('product_details', 'category', 'product_category', 'DESK LIGHTING'),
])
def test_product_label_collation_survives_cte_aliases(labelled_records, mode, table, column, alias, value):
    rows = query(f'WITH labels AS (SELECT item_code,{column} AS {alias} FROM {table}) '
                 f'SELECT item_code FROM labels WHERE {alias}=? ORDER BY item_code', mode, [value])
    assert rows == [['Case-Item'], ['case-item']]


@pytest.mark.parametrize('mode', ROLES)
def test_category_grouping_combines_case_variants_without_changing_labels(labelled_records, mode):
    rows = query("SELECT category,COUNT(*) FROM product_details "
                 "WHERE item_code IN ('Case-Item','case-item') GROUP BY category", mode)
    assert len(rows) == 1 and rows[0][0].casefold() == 'desk lighting' and rows[0][1] == 2
    with closing(connect(readonly=True)) as conn:
        labels = conn.execute("SELECT category FROM product_details WHERE item_code IN ('Case-Item','case-item') ORDER BY item_code").fetchall()
        assert [row[0] for row in labels] == ['Desk Lighting', 'desk lighting']


@pytest.mark.parametrize('mode', ROLES)
def test_attribute_names_keep_existing_nocase_but_units_remain_exact(labelled_records, mode):
    assert query('SELECT item_code FROM product_attributes WHERE attribute=? ORDER BY item_code',
                 mode, ['WATTAGE']) == [['Case-Item'], ['case-item']]
    assert query("SELECT item_code FROM product_attributes WHERE attribute='wattage' AND unit='mw'", mode) == []
    assert query("SELECT item_code FROM product_attributes WHERE attribute='WATTAGE' AND unit='mW'", mode) == [['Case-Item']]


@pytest.mark.parametrize('mode', ROLES)
@pytest.mark.parametrize('table,column,wrong_case', [
    ('customers', 'customer_id', 'CuSt001'),
    ('orders', 'order_id', 'CASE-ORDER-01'),
    ('orders', 'order_invoice', 'inv-no-match'),
    ('items', 'item_code', 'CASE-ITEM'),
    ('items', 'sku', 'CASE-SKU'),
])
def test_identifier_filters_remain_exact(labelled_records, mode, table, column, wrong_case):
    if column == 'order_invoice':
        with closing(connect(readonly=True)) as conn:
            invoice = conn.execute("SELECT order_invoice FROM orders WHERE order_id='Case-Order-01'").fetchone()[0]
        wrong_case = invoice.lower()
        assert wrong_case != invoice
    assert query(f'SELECT COUNT(*) FROM {table} WHERE {column}=?', mode, [wrong_case]) == [[0]]


@pytest.mark.parametrize('mode', ROLES)
def test_identifier_joins_do_not_merge_case_distinct_accounts(labelled_records, mode):
    rows = query("SELECT o.order_id,c.customer_id FROM orders o JOIN customers c "
                 "ON c.customer_id=o.customer_id WHERE o.order_id IN ('Case-Order-01','case-order-01') "
                 "ORDER BY o.order_id", mode)
    expected = [['Case-Order-01', 'CUST001']]
    if mode != 'customer':
        expected += [['case-order-01', 'cust001']]
    assert rows == expected


@pytest.mark.parametrize('mode', ROLES)
def test_coupon_identifiers_and_credential_privacy_are_unchanged(labelled_records, mode):
    for table in ('customer_credentials', 'customer_sessions'):
        result = read('SELECT * FROM ' + table, execution_mode=mode,
                      execution_customer_id='CUST001' if mode == 'customer' else None)
        assert not result['ok']
    result = read("SELECT promo_code FROM promotions WHERE promo_code='save10'", execution_mode=mode,
                  execution_customer_id='CUST001' if mode == 'customer' else None)
    if mode == 'customer':
        assert not result['ok']
    else:
        assert result['ok'] and result['rows'] == []
        assert query("SELECT promo_code FROM promotions WHERE promo_code='SAVE10'", mode) == [['SAVE10']]


@pytest.mark.parametrize('staff', [False, True])
def test_isolated_copy_retains_selected_rules_and_exact_identifiers(labelled_records, staff):
    # The scoped copy declares matching rules explicitly, while ownership
    # selection still uses exact identifiers.
    with closing(customer_query_connection('CUST001', staff=staff)) as conn:
        definition = conn.execute("SELECT sql FROM sqlite_master WHERE name='refund_requests'").fetchone()[0]
        assert '"status" COLLATE NOCASE' in definition
        rows = conn.execute("SELECT order_id FROM refund_requests WHERE status='PENDING' ORDER BY order_id").fetchall()
        assert [list(row) for row in rows] == pending_ids('cashier' if staff else 'customer')
        assert conn.execute("SELECT COUNT(*) FROM customers WHERE customer_id='CuSt001'").fetchone()[0] == 0


@pytest.mark.parametrize('statement', [
    "UPDATE orders SET order_status='PLACED' WHERE order_id='Case-Order-01'",
    "UPDATE order_details SET status='ACTIVE' WHERE order_id='Case-Order-01'",
    "UPDATE order_service SET fulfillment_status='PLACED' WHERE order_id='Case-Order-01'",
    "UPDATE refund_requests SET status='PENDING' WHERE order_id='Case-Order-01'",
])
def test_status_writes_still_require_canonical_values(labelled_records, statement):
    with pytest.raises(sqlite3.IntegrityError), transaction() as conn:
        conn.execute(statement)
