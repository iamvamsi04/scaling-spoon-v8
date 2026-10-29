"""Regression checks for service migrations and invoice-level reporting."""
from address_support import place_order_with_address, prepare_delivery
from contextlib import closing
from decimal import Decimal
import sqlite3

from database.connection import connect, initialize, transaction
from mcp_server.tools.billing_tools import request_order, place_order
from mcp_server.tools.operations_tools import manage_draft
from mcp_server.tools.analytics_tools import billing_analysis


def placed():
    order = request_order('CUST001', [{'item_code': 'ITEM001', 'quantity': 1}])
    assert order['ok']
    assert place_order_with_address(order['order_id'], 'CUST001')['ok']
    return order['order_id']


def test_general_analysis_normalizes_same_currency_without_combining_different_ones():
    orders = [placed() for _ in range(3)]
    with transaction() as conn:
        for oid, currency in zip(orders, ['USD', ' usd ', '']):
            conn.execute('UPDATE order_details SET currency=? WHERE order_id=?', (currency, oid))
    result = billing_analysis()
    assert result['ok'] and result['currency'] == 'USD'
    assert result['invoice_count'] == 3
    assert Decimal(result['totals']['charged_price']) == Decimal('239.70')
    with transaction() as conn:
        conn.execute("UPDATE order_details SET currency='EUR' WHERE order_id=?", (orders[-1],))
    assert not billing_analysis()['ok']


def test_cancelled_service_order_is_excluded_consistently_from_invoice_reports():
    cancelled, active = placed(), placed()
    result = manage_draft(cancelled, 'CUST001', 'cancel', reason='Changed mind', confirmed=True)
    assert result['ok'], result
    report = billing_analysis()
    assert report['ok'] and report['invoice_count'] == 1
    assert Decimal(report['totals']['charged_price']) == Decimal('79.90')
    trend = billing_analysis(trend_period='month')
    assert trend['ok'] and trend['invoice_count'] == 1
    assert sum(Decimal(r[1]) for chart in trend['chart_results'] for r in chart['rows']) == Decimal('79.90')


def test_service_upgrade_preserves_records_and_is_repeatable():
    oid = placed()
    with transaction() as conn:
        original = dict(conn.execute('SELECT * FROM order_service WHERE order_id=?', (oid,)).fetchone())
        definition = conn.execute("SELECT sql FROM sqlite_master WHERE name='order_service'").fetchone()[0]
        legacy = definition.replace("DEFAULT 'unknown'", "DEFAULT 'placed'").replace("'unknown',", '')
        conn.execute('DROP TABLE order_service')
        conn.execute(legacy)
        columns = ','.join(original)
        conn.execute(f'INSERT INTO order_service ({columns}) VALUES ({",".join("?" for _ in original)})', list(original.values()))
    initialize()
    initialize()
    with closing(connect()) as conn:
        assert dict(conn.execute('SELECT * FROM order_service WHERE order_id=?', (oid,)).fetchone()) == original
        conn.execute("UPDATE order_service SET fulfillment_status='unknown' WHERE order_id=?", (oid,))
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        conn.rollback()


def test_failed_service_upgrade_rolls_back_table_and_records():
    from database.migrations import add_unknown_fulfillment
    oid = placed()
    with transaction() as conn:
        definition = conn.execute("SELECT sql FROM sqlite_master WHERE name='order_service'").fetchone()[0]
        legacy = definition.replace("DEFAULT 'unknown'", "DEFAULT 'placed'").replace("'unknown',", '')
        conn.execute('DROP TABLE order_service')
        conn.execute(legacy)
        conn.execute('INSERT INTO order_service(order_id) VALUES (?)', (oid,))
        conn.execute('CREATE TABLE order_service_upgrade (sentinel TEXT)')
        try:
            add_unknown_fulfillment(conn)
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError('Conflicting migration table must fail safely')
        assert conn.execute('SELECT order_id FROM order_service').fetchone()[0] == oid
        assert "'unknown'" not in conn.execute("SELECT sql FROM sqlite_master WHERE name='order_service'").fetchone()[0]
