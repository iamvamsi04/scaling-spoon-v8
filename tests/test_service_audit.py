"""Adversarial service cases: no fabricated eligibility, hidden writes or cross-owner data."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import pytest

from database.connection import connect, transaction
from mcp_server.access import scoped_tool, scope
from mcp_server.tools.billing_tools import request_order, place_order, process_refund, download_invoice
from mcp_server.tools.operations_tools import manage_draft, manage_customer
from mcp_server.tools.analytics_tools import billing_analysis


def scalar(sql, args=()):
    with closing(connect()) as conn:
        return conn.execute(sql, args).fetchone()[0]


def order(customer='CUST001', placed=True):
    result = request_order(customer, [{'item_code': 'ITEM001', 'quantity': 1}])
    assert result['ok'], result
    if placed:
        result = place_order(result['order_id'], customer)
        assert result['ok'], result
    return result['order_id']


def request(oid, **kwargs):
    return scoped_tool(process_refund)(oid, 'CUST001', 'Return', execution_customer_id='CUST001', **kwargs)


def approve(oid, **kwargs):
    return scoped_tool(process_refund)(oid, 'CUST001', 'Return', action='approve', confirmed=True,
                                      return_received=True, execution_mode='admin', **kwargs)


def cancel(oid):
    return manage_draft(oid, 'CUST001', 'cancel', reason='Changed mind', confirmed=True)


def test_reporting_billing_issue_never_manufactures_unshipped_state():
    oid = order()
    with transaction() as conn:
        conn.execute('DELETE FROM order_service WHERE order_id=?', (oid,))
    before = scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'")
    assert manage_draft(oid, 'CUST001', 'report_billing_issue', reason='Possible duplicate charge', confirmed=True)['ok']
    assert scalar('SELECT fulfillment_status FROM order_service WHERE order_id=?', (oid,)) == 'unknown'
    assert not cancel(oid)['ok']
    assert not scoped_tool(manage_draft)(oid, 'CUST001', 'ship', confirmed=True, execution_mode='admin')['ok']
    assert scalar('SELECT COUNT(*) FROM refund_requests') == 0
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == before


@pytest.mark.parametrize('mutation', [
    'UPDATE orders SET refund_status=\'recorded\' WHERE order_id=?',
    'UPDATE orders SET subtotal=subtotal+10 WHERE order_id=?',
    'UPDATE orders SET discount_applied=subtotal+1 WHERE order_id=?',
    'UPDATE orders SET charged_price=charged_price+10 WHERE order_id=?',
    'UPDATE invoice_lines SET line_total=line_total+1 WHERE order_id=?',
    'UPDATE order_fulfilled SET quantity=quantity+1 WHERE order_id=?',
])
def test_cancellation_rejects_inconsistent_records_without_stock_or_refund_writes(mutation):
    oid = order()
    with transaction() as conn:
        conn.execute(mutation, (oid,))
    before = scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'")
    assert not cancel(oid)['ok']
    assert scalar('SELECT COUNT(*) FROM refund_requests') == 0
    assert scalar('SELECT fulfillment_status FROM order_service WHERE order_id=?', (oid,)) == 'placed'
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == before


def test_cancel_race_restocks_and_requests_once_then_retry_is_safe_after_window():
    oid = order()
    before = scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(cancel, [oid, oid]))
    assert all(r['ok'] for r in results)
    assert sum(bool(r.get('already_cancelled')) for r in results) == 1
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == before + 1
    assert scalar('SELECT COUNT(*) FROM refund_requests') == 1
    with transaction() as conn:
        conn.execute("UPDATE orders SET order_date=date('now','-90 days') WHERE order_id=?", (oid,))
    assert cancel(oid)['already_cancelled']
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == before + 1


def test_existing_customer_create_cannot_ignore_different_billing_fields():
    result = manage_customer('CUST001', 'Alex Morgan', 'Demo Co', 'alex@example.com',
                             confirmed=True, billing_address='New billing address')
    assert not result['ok']
    assert scalar('SELECT COUNT(*) FROM customer_billing_details') == 0


@pytest.mark.parametrize('changes,company,email', [
    ({}, None, None),
    ({'company': 'Updated company'}, 'Updated company', None),
    ({'email': 'updated@example.com'}, None, 'updated@example.com'),
    ({'company': None, 'email': None}, None, None),
    ({'company': '', 'email': ''}, '', ''),
])
def test_profile_updates_preserve_unspecified_fields_and_only_clear_explicitly(changes, company, email):
    with closing(connect()) as conn:
        original=conn.execute("SELECT company,email FROM customers WHERE customer_id='CUST001'").fetchone()
    company=original['company'] if company is None else company
    email=original['email'] if email is None else email
    result = manage_customer('CUST001', 'Alex Updated', action='update', confirmed=True, **changes)
    assert result['ok'], result
    assert (result['company'], result['email']) == (company, email)
    with closing(connect()) as conn:
        row = conn.execute("SELECT name,company,email FROM customers WHERE customer_id='CUST001'").fetchone()
        assert tuple(row) == ('Alex Updated', company, email)


def test_new_profile_can_omit_optional_contact_fields():
    result = manage_customer('NEW-OPTIONAL', 'New customer', confirmed=True)
    assert result['ok'] and result['company'] == result['email'] == ''


def test_anonymous_creation_rechecks_customer_existence_inside_transaction():
    # Simulate another request inserting this account after the transport precheck.
    token = scope.set({'mode': 'customer', 'customer_id': None})
    try:
        result = manage_customer('CUST001', 'Alex Morgan', 'Demo Co', 'alex@example.com', confirmed=True)
    finally:
        scope.reset(token)
    assert not result['ok']
    assert not result.get('already_exists')


def test_credit_allocation_can_be_cleared_without_spending_credit():
    source = order()
    assert request(source, disposition='store_credit')['ok']
    assert approve(source)['ok']
    target = order(placed=False)
    assert manage_draft(target, 'CUST001', 'apply_store_credit', credit_amount='79.90', confirmed=True)['ok']
    assert manage_draft(target, 'CUST001', 'apply_store_credit', credit_amount='0', confirmed=True)['ok']
    result = place_order(target, 'CUST001')
    assert result['ok'] and result['store_credit_applied'] == '0.00'
    assert Decimal(str(scalar('SELECT SUM(amount) FROM store_credit_ledger'))) == Decimal('79.90')


def test_proforma_correction_rejected_before_changing_issued_invoice():
    oid = order()
    inv = scalar('SELECT order_invoice FROM orders WHERE order_id=?', (oid,))
    draft = order(placed=False)
    result = download_invoice(inv, 'CUST001', document_type='proforma', order_id=draft,
                              action='correct_details', billing_address='Unexpected rewrite', confirmed=True)
    assert not result['ok']
    assert scalar('SELECT COUNT(*) FROM invoice_revisions') == 0


def test_failed_tax_invoice_correction_rolls_back_revision():
    oid = order()
    inv = scalar('SELECT order_invoice FROM orders WHERE order_id=?', (oid,))
    result = download_invoice(inv, 'CUST001', document_type='tax', action='correct_details',
                              billing_address='Only address, no company tax registration', confirmed=True)
    assert not result['ok']
    assert scalar('SELECT COUNT(*) FROM invoice_revisions') == 0


def test_pdf_render_failure_rolls_back_correction(monkeypatch):
    import reportlab.platypus as pdf
    oid = order()
    inv = scalar('SELECT order_invoice FROM orders WHERE order_id=?', (oid,))
    def fail(*args, **kwargs):
        raise OSError('Simulated full output device')
    monkeypatch.setattr(pdf.SimpleDocTemplate, 'build', fail)
    with pytest.raises(OSError):
        download_invoice(inv, 'CUST001', action='correct_details', billing_address='New road', confirmed=True)
    assert scalar('SELECT COUNT(*) FROM invoice_revisions') == 0


@pytest.mark.parametrize('field', ['subtotal=subtotal+10', 'charged_price=charged_price+10', 'discount_applied=subtotal+1'])
def test_return_quote_rejects_corrupt_financial_basis(field):
    oid = order()
    with transaction() as conn:
        conn.execute('UPDATE orders SET '+field+' WHERE order_id=?', (oid,))
    result = request(oid, action='quote', items=[{'item_code': 'ITEM001', 'quantity': 1}])
    assert not result['ok']
    assert scalar('SELECT COUNT(*) FROM refund_requests') == 0


def test_bulk_checks_refund_status_and_exposes_per_order_approver():
    good = order()
    bad = order()
    with transaction() as conn:
        conn.execute("UPDATE orders SET refund_status='recorded' WHERE order_id=?", (bad,))
    today = scalar('SELECT CURRENT_DATE')
    result = scoped_tool(process_refund)('', 'CUST001', action='bulk_preview', date_from=today,
                                         date_to=today, execution_customer_id='CUST001')
    rows = {r['order_id']: r for r in result['orders']}
    assert rows[good]['eligible'] and rows[good]['required_approver'] == 'cashier_or_admin'
    assert not rows[bad]['eligible']


def test_reconciliation_filters_legacy_gaps_and_reports_unapproved_refunds():
    own = order()
    other = order('CUST002')
    with transaction() as conn:
        conn.execute("UPDATE orders SET refund_amount=10,refund_status='recorded' WHERE order_id IN (?,?)", (own, other))
    result = billing_analysis(report='refund_reconciliation', customer_id='CUST001')
    assert [r['order_id'] for r in result['mismatches']] == [own]
    assert billing_analysis(report='refund_reconciliation', order_id=other)['mismatches'][0]['order_id'] == other
    with transaction() as conn:
        conn.execute("INSERT INTO refund_requests(order_id,amount,currency,reason) VALUES (?,10,'USD','Unapproved')", (own,))
    result = billing_analysis(report='refund_reconciliation', customer_id='CUST001')
    assert result['count'] == 1 and 'without staff approval' in result['mismatches'][0]['issue']
    today = scalar('SELECT CURRENT_DATE')
    dated = billing_analysis(report='refund_reconciliation', customer_id='CUST001', date_from=today, date_to=today)
    assert dated['approved_count'] == 0 and dated['undated_count'] == 1
    assert dated['undated_records'][0]['order_id'] == own


def test_reconciliation_date_validation_is_a_controlled_error():
    result = billing_analysis(report='refund_reconciliation', date_from='yesterday')
    assert not result['ok']


@pytest.mark.parametrize('filters', [
    {'customer_id': ''}, {'order_id': ' '}, {'date_from': ''}, {'date_to': ''},
])
def test_empty_refund_report_filters_cannot_silently_expand_to_all_records(filters):
    assert not billing_analysis(report='refund_reconciliation', **filters)['ok']
