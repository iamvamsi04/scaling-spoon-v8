"""Order state stays explicit across old imports, retries and cancellation refunds."""
from contextlib import closing
import sqlite3

import pytest

from database.connection import connect, initialize, transaction
from database.migrations import add_order_status
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import request_order, place_order, process_refund
from mcp_server.tools.operations_tools import manage_draft


def scalar(query, args=()):
    with closing(connect()) as conn:
        return conn.execute(query, args).fetchone()[0]


def draft():
    result = request_order('CUST001', [{'item_code': 'ITEM001', 'quantity': 1}])
    assert result['ok'] and result['status'] == result['order_status'] == 'draft'
    return result['order_id']


def test_placement_and_retry_expose_status_and_reserve_stock_once():
    oid = draft()
    before = scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'")
    first = place_order(oid, 'CUST001')
    again = place_order(oid, 'CUST001')
    assert first['ok'] and again['ok'] and again['already_placed']
    assert first['invoice_number'] == again['invoice_number']
    assert first['order_status'] == again['order_status'] == 'placed'
    assert scalar('SELECT order_status FROM orders WHERE order_id=?', (oid,)) == 'placed'
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == before - 1
    assert not manage_draft(oid, 'CUST001', 'replace_items', items=[{'item_code': 'ITEM002', 'quantity': 1}], confirmed=True)['ok']


@pytest.mark.parametrize('invoice', ['', '   ', '\t\r\n'])
def test_blank_invoice_remains_a_draft_and_can_be_placed(invoice):
    oid = draft()
    with transaction() as conn:
        conn.execute('UPDATE orders SET order_invoice=? WHERE order_id=?', (invoice, oid))
    assert scalar('SELECT order_status FROM orders WHERE order_id=?', (oid,)) == 'draft'
    assert place_order(oid, 'CUST001')['ok']
    assert scalar('SELECT order_status FROM orders WHERE order_id=?', (oid,)) == 'placed'


def test_direct_invoice_import_derives_placed_state():
    with transaction() as conn:
        conn.execute("INSERT INTO orders(order_id,customer_id,order_invoice,subtotal,charged_price) "
                     "VALUES ('IMPORTED','CUST001','INV-IMPORTED',10,10)")
    assert scalar("SELECT order_status FROM orders WHERE order_id='IMPORTED'") == 'placed'
    assert place_order('IMPORTED', 'CUST001')['already_placed']


@pytest.mark.parametrize('change', [
    "order_status='draft'", "order_invoice=NULL", "order_invoice=' '", "order_invoice='INV-OTHER'"
])
def test_issued_state_and_invoice_identity_cannot_be_reverted(change):
    oid = draft()
    assert place_order(oid, 'CUST001')['ok']
    with pytest.raises(sqlite3.IntegrityError), transaction() as conn:
        conn.execute('UPDATE orders SET ' + change + ' WHERE order_id=?', (oid,))
    assert scalar('SELECT order_status FROM orders WHERE order_id=?', (oid,)) == 'placed'


def test_cancelled_draft_cannot_be_reopened_or_placed():
    oid = draft()
    result = manage_draft(oid, 'CUST001', 'cancel', reason='Changed mind', confirmed=True)
    assert result['ok'] and result['order_status'] == 'cancelled'
    assert not place_order(oid, 'CUST001')['ok']
    assert manage_draft(oid, 'CUST001', 'cancel', reason='Changed mind', confirmed=True)['already_cancelled']
    with pytest.raises(sqlite3.IntegrityError), transaction() as conn:
        conn.execute("UPDATE orders SET order_status='draft' WHERE order_id=?", (oid,))
    with pytest.raises(sqlite3.IntegrityError), transaction() as conn:
        conn.execute("UPDATE orders SET order_invoice='INV-REOPEN' WHERE order_id=?", (oid,))
    assert scalar('SELECT COUNT(*) FROM refund_requests WHERE order_id=?', (oid,)) == 0


def test_cancelled_issued_order_only_approves_existing_cancellation_request():
    oid = draft()
    assert place_order(oid, 'CUST001')['ok']
    invoice = scalar('SELECT order_invoice FROM orders WHERE order_id=?', (oid,))
    before = scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'")
    result = manage_draft(oid, 'CUST001', 'cancel', reason='Changed mind', confirmed=True)
    assert result['ok'] and result['order_status'] == 'cancelled'
    assert scalar('SELECT order_invoice FROM orders WHERE order_id=?', (oid,)) == invoice
    assert not place_order(oid, 'CUST001')['ok']
    customer = scoped_tool(process_refund)
    assert not customer(oid, 'CUST001', 'Changed mind', execution_customer_id='CUST001')['ok']
    result = scoped_tool(process_refund)(oid, 'CUST001', 'Changed mind', action='approve',
                                        confirmed=True, execution_mode='admin')
    assert result['ok'], result
    status = customer(oid, 'CUST001', action='status', execution_customer_id='CUST001')
    assert status['ok'] and status['order_status'] == 'cancelled' and status['local_status'] == 'approved'
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == before + 1
    assert not scoped_tool(process_refund)(oid, 'CUST001', 'Changed mind', action='approve',
                                          confirmed=True, execution_mode='admin')['ok']


@pytest.mark.parametrize('table,column', [('order_details', 'status'), ('order_service', 'fulfillment_status')])
def test_legacy_cancellation_fields_synchronize_authoritative_state(table, column):
    oid = draft()
    with transaction() as conn:
        if table == 'order_service':
            conn.execute('INSERT INTO order_service(order_id) VALUES (?)', (oid,))
        conn.execute(f"UPDATE {table} SET {column}='cancelled' WHERE order_id=?", (oid,))
    assert scalar('SELECT order_status FROM orders WHERE order_id=?', (oid,)) == 'cancelled'
    assert not place_order(oid, 'CUST001')['ok']


def test_incomplete_legacy_fulfillment_does_not_become_a_placed_order():
    oid = draft()
    with transaction() as conn:
        conn.execute("INSERT INTO order_fulfilled(order_id,item_code,quantity) VALUES (?,'ITEM001',1)", (oid,))
    assert scalar('SELECT order_status FROM orders WHERE order_id=?', (oid,)) == 'draft'
    result = place_order(oid, 'CUST001')
    assert not result['ok'] and 'unissued' in result['error']
    assert not manage_draft(oid, 'CUST001', 'replace_items', items=[{'item_code': 'ITEM002', 'quantity': 1}], confirmed=True)['ok']


def test_draft_with_legacy_refund_cannot_be_bought_or_edited():
    oid = draft()
    with transaction() as conn:
        conn.execute("UPDATE orders SET refund_amount=10,refund_status='recorded' WHERE order_id=?", (oid,))
    assert not place_order(oid, 'CUST001')['ok']
    assert not manage_draft(oid, 'CUST001', 'replace_items', items=[{'item_code': 'ITEM002', 'quantity': 1}], confirmed=True)['ok']
    assert scalar('SELECT order_status FROM orders WHERE order_id=?', (oid,)) == 'draft'


def test_migration_backfills_once_preserving_invoices_and_cancellation_evidence():
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.executescript("""
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT, order_date TEXT,
                                order_invoice TEXT, charged_price NUMERIC);
            CREATE TABLE order_details(order_id TEXT PRIMARY KEY,status TEXT);
            CREATE TABLE order_service(order_id TEXT PRIMARY KEY,fulfillment_status TEXT);
            INSERT INTO orders VALUES ('DRAFT','C1','2026-10-20',NULL,10),
                ('BLANK','C1','2026-10-20',' \t\r\n',20),
                ('ISSUED','C1','2026-10-20','INV-1',30),
                ('CANCEL-D','C1','2026-10-20',NULL,40),
                ('CANCEL-P','C1','2026-10-20','INV-2',50);
            INSERT INTO order_details VALUES ('CANCEL-D','cancelled');
            INSERT INTO order_service VALUES ('CANCEL-P','cancelled');
        """)
        add_order_status(conn)
        assert dict(conn.execute('SELECT order_id,order_status FROM orders')) == {
            'DRAFT': 'draft', 'BLANK': 'draft', 'ISSUED': 'placed',
            'CANCEL-D': 'cancelled', 'CANCEL-P': 'cancelled'}
        assert conn.execute("SELECT order_invoice FROM orders WHERE order_id='BLANK'").fetchone()[0] is None
        snapshot = conn.execute('SELECT * FROM orders ORDER BY order_id').fetchall()
        add_order_status(conn)
        assert conn.execute('SELECT * FROM orders ORDER BY order_id').fetchall() == snapshot
        assert dict(conn.execute('SELECT order_id,charged_price FROM orders')) == {
            'DRAFT': 10, 'BLANK': 20, 'ISSUED': 30, 'CANCEL-D': 40, 'CANCEL-P': 50}


def test_startup_rerun_preserves_terminal_states():
    oid = draft()
    assert manage_draft(oid, 'CUST001', 'cancel', reason='Changed mind', confirmed=True)['ok']
    initialize()
    assert scalar('SELECT order_status FROM orders WHERE order_id=?', (oid,)) == 'cancelled'


def test_large_seed_explicitly_records_all_order_states(tmp_path):
    from datetime import date
    from generate_seed import generate_records
    path = tmp_path / 'store.db'
    generate_records(path, 42, date(2026, 10, 21))
    with closing(sqlite3.connect(path)) as conn:
        assert dict(conn.execute('SELECT order_status,COUNT(*) FROM orders GROUP BY order_status')) == {
            'draft': 51, 'placed': 307}
        assert not conn.execute("SELECT 1 FROM orders WHERE (order_status='placed' AND order_invoice IS NULL) "
                                "OR (order_status='draft' AND order_invoice IS NOT NULL)").fetchone()


def test_analytics_uses_lifecycle_even_without_legacy_cancellation_metadata():
    from mcp_server.tools.analytics_tools import billing_analysis
    active = draft()
    cancelled = draft()
    waiting = draft()
    assert place_order(active, 'CUST001')['ok']
    assert place_order(cancelled, 'CUST001')['ok']
    with transaction() as conn:
        conn.execute("UPDATE orders SET order_status='cancelled' WHERE order_id=?", (cancelled,))
        conn.execute("UPDATE orders SET order_invoice='   ' WHERE order_id=?", (waiting,))
    result = billing_analysis()
    assert result['ok'] and result['invoice_count'] == 1 and result['draft_count'] == 1
    assert result['totals']['charged_price'] == '79.90'
    trend = billing_analysis(trend_period='month')
    assert trend['ok'] and trend['invoice_count'] == 1
    assert trend['chart_results'][0]['rows'][0][1] == '79.90'
