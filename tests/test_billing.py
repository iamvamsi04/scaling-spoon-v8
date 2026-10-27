from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import pytest
from database.connection import connect, transaction
from database.invoice import invoice_path
from mcp_server.tools.billing_tools import request_order, place_order, apply_promotion, process_refund, download_invoice
from mcp_server.tools.database import execute_sql_query
from mcp_server.access import scoped_tool
from functools import partial
request_refund = partial(scoped_tool(process_refund), execution_customer_id="CUST001")
process_refund = partial(scoped_tool(process_refund), action='approve', confirmed=True, return_received=True, execution_mode='admin')


def draft(items=None, order_id=None):
    result = request_order("CUST001", items or [{"item_code": "ITEM001", "quantity": 2}], order_id)
    assert result["ok"], result
    return result["order_id"]


def scalar(sql):
    conn = connect()
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("query", ["DELETE FROM customers", "UPDATE items SET availablequantity=0",
    "DROP TABLE orders", "ATTACH DATABASE ':memory:' AS extra", "PRAGMA writable_schema=ON",
    "PRAGMA journal_mode=WAL", "SELECT load_extension('x')", "SELECT * FROM sqlite_master",
    "SELECT 1; DELETE FROM customers", "WITH x AS (SELECT 1) DELETE FROM customers",
    "SELECT randomblob(1000000000)", "PRAGMA table_info(sqlite_master)", "VACUUM", "BEGIN"])
def test_sql_denied(query):
    assert not execute_sql_query(query)["ok"]
    assert scalar("SELECT COUNT(*) FROM customers") == 4


def test_read_queries():
    assert execute_sql_query("SELECT COUNT(*) FROM customers")["rows"] == [[4]]
    assert execute_sql_query("WITH c AS (SELECT * FROM customers) SELECT name FROM c")["ok"]
    assert execute_sql_query("PRAGMA table_info(orders)")["ok"]
    assert not execute_sql_query("SELECT * FROM missing")["ok"]
    result = execute_sql_query("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<300) SELECT * FROM n")
    assert result["truncated"] and result["row_count"] == 200


@pytest.mark.parametrize("quantity", [0, -1, 1.5, "2", True, 100001])
def test_bad_quantity(quantity):
    assert not request_order("CUST001", [{"item_code": "ITEM001", "quantity": quantity}])["ok"]
    assert scalar("SELECT COUNT(*) FROM orders") == 0


def test_invalid_customer_item_stock():
    assert not request_order("missing", [{"item_code": "ITEM001", "quantity": 1}])["ok"]
    assert not request_order("CUST001", [{"item_code": "missing", "quantity": 1}])["ok"]
    assert not request_order("CUST001", [{"item_code": "ITEM001", "quantity": 41}])["ok"]


def test_order_lifecycle_and_retries():
    oid = draft(order_id="ORD-test")
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == 40
    assert request_order("CUST001", [{"item_code": "ITEM001", "quantity": 2}], oid)["already_exists"]
    assert not request_order("CUST001", [{"item_code": "ITEM001", "quantity": 3}], oid)["ok"]
    placed = place_order(oid, "CUST001")
    assert placed["ok"] and placed["charged_price"] == "159.80"
    assert place_order(oid, "CUST001")["already_placed"]
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == 38
    assert scalar("SELECT COUNT(*) FROM invoice_lines") == 1


def test_duplicate_items_combined():
    oid = draft([{"item_code": "ITEM001", "quantity": 2}, {"item_code": "ITEM001", "quantity": 3}])
    assert place_order(oid, "CUST001")["charged_price"] == "399.50"
    assert scalar("SELECT qty_billed FROM invoice_lines") == 5


def test_transaction_rolls_back_all_lines():
    oid = draft([{"item_code": "ITEM001", "quantity": 2}, {"item_code": "ITEM002", "quantity": 2}])
    with transaction() as conn:
        conn.execute("UPDATE items SET availablequantity=0 WHERE item_code='ITEM002'")
    assert not place_order(oid, "CUST001")["ok"]
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == 40
    assert scalar("SELECT COUNT(*) FROM invoice_lines") == 0
    assert scalar("SELECT COUNT(*) FROM order_fulfilled") == 0


def test_concurrent_placement_cannot_oversell():
    one = draft([{"item_code": "ITEM003", "quantity": 10}])
    two = draft([{"item_code": "ITEM003", "quantity": 10}])
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda oid: place_order(oid, "CUST001"), [one, two]))
    assert sum(r["ok"] for r in results) == 1
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM003'") == 5


def test_promotion_tax_shipping_and_lock():
    oid = draft()
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='7.5' WHERE key='tax_rate'")
        conn.execute("UPDATE billing_config SET value='5' WHERE key='shipping_amount'")
    assert not apply_promotion(oid, "CUST001", "missing")["ok"]
    promo = apply_promotion(oid, "CUST001", "SAVE10")
    assert promo["discount_applied"] == "15.98"
    assert promo["tax_amount"] == "10.79" and promo["charged_price"] == "159.61"
    assert apply_promotion(oid, "CUST001", "SAVE10")["charged_price"] == "159.61"
    assert place_order(oid, "CUST001")["charged_price"] == "159.61"
    assert not apply_promotion(oid, "CUST001", "SAVE500")["ok"]


def test_flat_promo_capped_at_subtotal():
    oid = draft()
    assert apply_promotion(oid, "CUST001", "SAVE500")["charged_price"] == "0.00"


def test_historical_price_and_overlap():
    with transaction() as conn:
        conn.execute("INSERT INTO price_history(item_code,unit_price,effective_from) VALUES ('ITEM001',50,'2020-01-01')")
    oid = draft()
    assert place_order(oid, "CUST001")["charged_price"] == "100.00"
    with transaction() as conn:
        conn.execute("INSERT INTO price_history(item_code,unit_price,effective_from) VALUES ('ITEM001',60,'2021-01-01')")
    assert not request_order("CUST001", [{"item_code": "ITEM001", "quantity": 1}])["ok"]


@pytest.mark.parametrize("amount", ["0", "-1", "99999", "NaN", "Infinity"])
def test_invalid_refund(amount):
    oid = draft()
    place_order(oid, "CUST001")
    assert not request_refund(oid, "CUST001", "Returned", amount)["ok"]


def test_refund_and_pdf_owner():
    oid = draft()
    assert not process_refund(oid, "CUST001", "Return")["ok"]
    assert not place_order(oid, "CUST002")["ok"]
    invoice = place_order(oid, "CUST001")["invoice_number"]
    assert not process_refund(oid, "CUST002", "Return")["ok"]
    assert request_refund(oid, "CUST001", "Return", "10.00")["ok"]
    refund = process_refund(oid, "CUST001", "Return", "10.00")
    assert refund["ok"] and refund["refund_status"] == "recorded"
    assert not process_refund(oid, "CUST001", "Return", "10.00")["ok"]
    assert not download_invoice(invoice, "CUST002")["ok"]
    assert download_invoice(invoice, "CUST001")["ready"]
    assert invoice_path(invoice, "CUST001").read_bytes().startswith(b"%PDF")


def test_invoice_reports_refunds_without_inventing_payment_balance(monkeypatch):
    import reportlab.platypus as pdf
    original = pdf.Paragraph
    text = []
    def capture(value, *args, **kwargs):
        text.append(str(value))
        return original(value, *args, **kwargs)
    monkeypatch.setattr(pdf,'Paragraph',capture)
    draft = request_order('CUST001',[{'item_code':'ITEM001','quantity':1}])
    invoice = place_order(draft['order_id'],'CUST001')['invoice_number']
    assert request_refund(draft['order_id'],'CUST001','Return','10.00')['ok']
    assert process_refund(draft['order_id'],'CUST001','Return','10.00')['ok']
    assert download_invoice(invoice,'CUST001')['ready']
    assert 'Invoice total after recorded refunds: USD 69.90' in text
    assert not any(label in value for value in text for label in ('Balance due:','Customer credit:','Recorded receipts:','Credit notes:'))
    assert any('Payment status and outstanding balance are not tracked' in value for value in text)
