from address_support import place_order_with_address, prepare_delivery
from database.connection import transaction, connect
from mcp_server.tools.billing_tools import request_order, place_order, apply_promotion
from mcp_server.tools.operations_tools import manage_customer, manage_draft
from mcp_server.tools.analytics_tools import billing_analysis, billing_anomalies

def test_cancel_and_replace_drafts_keep_stock_and_history():
    oid = request_order('CUST001',[{'item_code':'ITEM001','quantity':1}],'DRAFT')['order_id']
    before = billing_analysis()['draft_count']
    result = manage_draft(oid,'CUST001','replace_items',[{'item_code':'ITEM002','quantity':3}],confirmed=True)
    assert result['estimate']['charged_price'] == '88.50'
    assert not manage_draft(oid,'CUST001','replace_items',[{'item_code':'ITEM002','quantity':999}],confirmed=True)['ok']
    assert manage_draft(oid,'CUST001','cancel',reason='Duplicate request',confirmed=True)['ok']
    assert manage_draft(oid,'CUST001','cancel',reason='Duplicate request',confirmed=True)['already_cancelled']
    assert not place_order_with_address(oid,'CUST001')['ok']
    assert not apply_promotion(oid,'CUST001','SAVE10')['ok']
    assert not request_order('CUST001',[{'item_code':'ITEM002','quantity':3}],oid)['ok']
    assert billing_analysis()['draft_count'] == before-1
    assert billing_anomalies()['finding_count'] == 0
    conn = connect()
    try:
        assert conn.execute("SELECT availablequantity FROM items WHERE item_code='ITEM002'").fetchone()[0] == 60
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 1
        assert conn.execute('SELECT quantity FROM order_requests WHERE order_id=?',(oid,)).fetchone()[0] == 3
    finally:
        conn.close()

def test_changed_quote_requires_reconfirmation_and_rolls_back_inventory():
    oid = request_order('CUST001',[{'item_code':'ITEM001','quantity':2}],'QUOTE')['order_id']
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='5' WHERE key='shipping_amount'")
    assert not place_order_with_address(oid,'CUST001')['ok']
    conn = connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM invoice_lines').fetchone()[0] == 0
        assert conn.execute("SELECT availablequantity FROM items WHERE item_code='ITEM001'").fetchone()[0] == 40
    finally:
        conn.close()
    refreshed = manage_draft(oid,'CUST001','replace_items',[{'item_code':'ITEM001','quantity':2}],confirmed=True)
    assert refreshed['estimate']['charged_price'] == '164.80'
    assert place_order_with_address(oid,'CUST001')['charged_price'] == '164.80'

def test_changed_draft_currency_requires_new_draft():
    oid = request_order('CUST001',[{'item_code':'ITEM001','quantity':1}],'QUOTE')['order_id']
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='EUR' WHERE key='currency'")
    assert not place_order_with_address(oid,'CUST001')['ok']

def test_customer_creation_validation_and_idempotency():
    assert not manage_customer('NEW','New Customer',confirmed=False)['ok']
    assert not manage_customer('NEW','New Customer',email='bad-email',confirmed=True)['ok']
    assert manage_customer('NEW','New Customer',email='new@example.com',confirmed=True)['ok']
    assert manage_customer('NEW','New Customer',email='new@example.com',confirmed=True)['already_exists']
    assert not manage_customer('NEW','Different',confirmed=True)['ok']
