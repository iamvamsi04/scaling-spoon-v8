"""Credit remains consistent when an existing draft's invoice total changes."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from context.shopping_state import order_snapshot
from database.connection import connect, transaction
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import (
    apply_promotion, place_order, process_refund, request_order,
)
from mcp_server.tools.operations_tools import manage_draft
from mcp_server.tools.service_workflows import available_credit


def draft(customer='CUST001'):
    result = request_order(customer, [{'item_code': 'ITEM001', 'quantity': 1}])
    assert result['ok'], result
    return result['order_id']


def grant_credit(amount='50.00', customer='CUST001', currency='USD', target=None):
    source = draft(customer)
    with transaction() as conn:
        previous_currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()[0]
        conn.execute("UPDATE billing_config SET value=? WHERE key='currency'", (currency,))
        conn.execute('UPDATE order_details SET currency=? WHERE order_id=?', (currency, source))
    try:
        assert place_order(source, customer)['ok']
    finally:
        with transaction() as conn:
            conn.execute("UPDATE billing_config SET value=? WHERE key='currency'", (previous_currency,))
    refund = scoped_tool(process_refund)
    requested = refund(source, customer, reason='Return', amount=amount,
        disposition='store_credit', action='request', target_order_id=target, execution_customer_id=customer)
    assert requested['ok'], requested
    approved = refund(source, customer, reason='Return', amount=amount,
        action='approve', confirmed=True, return_received=True, execution_mode='admin')
    assert approved['ok'], approved
    return approved


def allocation(order_id):
    with closing(connect(readonly=True)) as conn:
        row = conn.execute('''SELECT o.charged_price,COALESCE(s.credit_requested,0),o.promo_code
            FROM orders o LEFT JOIN order_service s ON s.order_id=o.order_id
            WHERE o.order_id=?''', (order_id,)).fetchone()
    return Decimal(str(row[0])), Decimal(str(row[1])), row[2]


def balance():
    with closing(connect(readonly=True)) as conn:
        return sum((Decimal(str(row[0])) for row in conn.execute(
            'SELECT amount FROM store_credit_ledger WHERE customer_id=?', ('CUST001',))), Decimal(0))


@pytest.mark.parametrize('code,quantity,total,credit,remaining', [
    ('ITEM002', 1, '29.50', '29.50', '0.00'),
    ('ITEM001', 2, '159.80', '50.00', '109.80'),
])
def test_basket_change_reconciles_credit_and_returns_current_quote(code, quantity, total, credit, remaining):
    grant_credit()
    target = draft()
    assert manage_draft(target, 'CUST001', 'apply_store_credit',
                        credit_amount='50.00', confirmed=True)['ok']
    result = manage_draft(target, 'CUST001', 'replace_items',
                         items=[{'item_code': code, 'quantity': quantity}], confirmed=True)
    assert result['ok'], result
    assert result['estimate']['charged_price'] == total
    assert result['store_credit_requested'] == credit
    assert result['remaining_invoice_amount'] == remaining
    assert allocation(target)[:2] == (Decimal(total), Decimal(credit))
    snapshot = order_snapshot(target, 'CUST001')
    assert snapshot['store_credit_requested'] == credit
    assert snapshot['remaining_invoice_amount'] == remaining
    assert balance() == Decimal('50.00')
    placed = place_order(target, 'CUST001')
    assert placed['ok'], placed
    assert placed['store_credit_applied'] == credit
    assert placed['remaining_invoice_amount'] == remaining
    # The post-redemption total update must preserve the applied amount.
    assert allocation(target)[1] == Decimal(credit)
    assert balance() == Decimal('50.00') - Decimal(credit)


def test_coupon_clamps_existing_credit_before_checkout():
    grant_credit('79.90')
    target = draft()
    assert manage_draft(target, 'CUST001', 'apply_store_credit',
                        credit_amount='79.90', confirmed=True)['ok']
    result = apply_promotion(target, 'CUST001', 'SAVE10')
    assert result['ok'], result
    assert result['charged_price'] == result['store_credit_requested'] == '71.91'
    assert result['remaining_invoice_amount'] == '0.00'
    assert allocation(target) == (Decimal('71.91'), Decimal('71.91'), 'SAVE10')
    placed = place_order(target, 'CUST001')
    assert placed['ok'], placed
    assert placed['store_credit_applied'] == '71.91'
    assert balance() == Decimal('7.99')


def test_coupon_preview_reconciles_proposed_credit_without_mutating_allocation():
    grant_credit('79.90')
    target = draft()
    assert manage_draft(target, 'CUST001', 'apply_store_credit',
                        credit_amount='79.90', confirmed=True)['ok']
    before = allocation(target)
    result = apply_promotion(target, 'CUST001', 'SAVE10', action='validate')
    assert result['ok'] and result['applied'] is False
    assert result['charged_price'] == result['store_credit_requested'] == '71.91'
    assert result['remaining_invoice_amount'] == '0.00'
    assert allocation(target) == before == (Decimal('79.90'), Decimal('79.90'), None)
    assert balance() == Decimal('79.90')


def test_zero_total_coupon_clears_credit_without_redeeming_it():
    grant_credit()
    target = draft()
    assert manage_draft(target, 'CUST001', 'apply_store_credit',
                        credit_amount='50.00', confirmed=True)['ok']
    result = apply_promotion(target, 'CUST001', 'SAVE500')
    assert result['ok'], result
    assert result['charged_price'] == result['store_credit_requested'] == '0.00'
    assert result['remaining_invoice_amount'] == '0.00'
    placed = place_order(target, 'CUST001')
    assert placed['ok'] and placed['store_credit_applied'] == '0.00'
    assert balance() == Decimal('50.00')


def test_failed_basket_change_preserves_total_and_credit():
    grant_credit()
    target = draft()
    assert manage_draft(target, 'CUST001', 'apply_store_credit',
                        credit_amount='50.00', confirmed=True)['ok']
    before = allocation(target)
    result = manage_draft(target, 'CUST001', 'replace_items',
        items=[{'item_code': 'ITEM002', 'quantity': 1000}], confirmed=True)
    assert not result['ok']
    assert allocation(target) == before
    assert balance() == Decimal('50.00')


def test_unallocated_draft_quote_reports_zero_credit():
    target = draft()
    result = manage_draft(target, 'CUST001', 'replace_items',
        items=[{'item_code': 'ITEM002', 'quantity': 1}], confirmed=True)
    assert result['ok']
    assert result['store_credit_requested'] == '0.00'
    assert result['remaining_invoice_amount'] == '29.50'


def reserve(order_id, amount, customer='CUST001'):
    return manage_draft(order_id, customer, 'apply_store_credit', credit_amount=amount, confirmed=True)


def available(customer='CUST001', currency='USD', exclude=None):
    with closing(connect(readonly=True)) as conn:
        return available_credit(conn, customer, currency, exclude)


def test_second_draft_cannot_reserve_credit_already_held_by_first():
    grant_credit()
    first, second = draft(), draft()
    assert reserve(first, '50.00')['ok']
    assert not reserve(second, '0.01')['ok']
    assert allocation(second)[1] == 0
    assert available() == 0
    assert available(exclude=first) == Decimal('50.00')
    assert reserve(first, '30.00')['ok']
    assert reserve(second, '20.00')['ok']
    assert not reserve(first, '30.01')['ok']
    assert balance() == Decimal('50.00')


def test_concurrent_draft_reservations_are_serialized():
    grant_credit()
    targets = [draft(), draft()]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda oid: reserve(oid, '50.00'), targets))
    assert sum(result['ok'] for result in results) == 1
    assert sum((allocation(oid)[1] for oid in targets), Decimal(0)) == Decimal('50.00')
    assert balance() == Decimal('50.00') and available() == 0


@pytest.mark.parametrize('change,released', [
    ('reduce', '20.00'), ('clear', '50.00'), ('cancel', '50.00'),
    ('basket', '20.50'), ('coupon', '7.99'),
])
def test_released_credit_becomes_available_to_other_drafts(change, released):
    initial = '79.90' if change == 'coupon' else '50.00'
    grant_credit(initial)
    first, second = draft(), draft()
    assert reserve(first, initial)['ok']
    if change in ('reduce', 'clear'):
        result = reserve(first, '30.00' if change == 'reduce' else '0.00')
    elif change == 'cancel':
        result = manage_draft(first, 'CUST001', 'cancel', reason='Changed plans', confirmed=True)
    elif change == 'basket':
        result = manage_draft(first, 'CUST001', 'replace_items',
            items=[{'item_code': 'ITEM002', 'quantity': 1}], confirmed=True)
    else:
        result = apply_promotion(first, 'CUST001', 'SAVE10')
    assert result['ok'], result
    assert available() == Decimal(released)
    assert reserve(second, released)['ok']
    assert available() == 0


def test_placement_redeems_current_reservation_without_consuming_other_cart_credit():
    grant_credit()
    first, second, third = draft(), draft(), draft()
    assert reserve(first, '30.00')['ok'] and reserve(second, '20.00')['ok']
    assert place_order(first, 'CUST001')['ok']
    assert balance() == Decimal('20.00') and available() == 0
    assert not reserve(third, '0.01')['ok']
    assert place_order(second, 'CUST001')['ok']
    assert balance() == available() == 0


def test_reservations_are_isolated_by_customer_and_normalized_currency():
    grant_credit('50.00', currency=' usd ')
    grant_credit('40.00', currency='EUR')
    grant_credit('30.00', customer='CUST002')
    usd, eur, other = draft(), draft(), draft('CUST002')
    with transaction() as conn:
        conn.execute("UPDATE order_details SET currency=' UsD ' WHERE order_id=?", (usd,))
        conn.execute("UPDATE order_details SET currency=' eur ' WHERE order_id=?", (eur,))
    assert reserve(usd, '50.00')['ok']
    assert reserve(eur, '40.00')['ok']
    assert reserve(other, '30.00', 'CUST002')['ok']
    assert available(currency=' uSd ') == available(currency='EUR') == available('CUST002') == 0
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value=' eur ' WHERE key='currency'")
    assert place_order(eur, 'CUST001')['ok']
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='USD' WHERE key='currency'")
    assert place_order(other, 'CUST002')['ok']
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value=' UsD ' WHERE key='currency'")
    assert place_order(usd, 'CUST001')['ok']


def test_reservation_currency_uses_same_fallback_for_missing_order_details():
    grant_credit()
    first, second = draft(), draft()
    assert reserve(first, '50.00')['ok']
    with transaction() as conn:
        conn.execute('DELETE FROM order_details WHERE order_id=?', (first,))
        conn.execute("UPDATE billing_config SET value=' usd ' WHERE key='currency'")
    assert available() == 0
    assert not reserve(second, '0.01')['ok']


def test_legacy_overallocated_drafts_cannot_redeem_another_carts_reservation():
    grant_credit()
    first, second = draft(), draft()
    assert reserve(first, '50.00')['ok']
    with transaction() as conn:
        conn.execute('INSERT INTO order_service(order_id,credit_requested) VALUES (?,50)', (second,))
    assert not place_order(first, 'CUST001')['ok']
    assert balance() == Decimal('50.00')
    assert reserve(second, '0.00')['ok']
    assert place_order(first, 'CUST001')['ok']


def test_refund_target_allocation_respects_other_reservations_without_losing_refund():
    grant_credit()
    other, target = draft(), draft()
    assert reserve(other, '40.00')['ok']
    # Preserve an older over-allocation while the new refund adds usable credit.
    with transaction() as conn:
        conn.execute('INSERT INTO order_service(order_id,credit_requested) VALUES (?,30)', (target,))
    result = grant_credit('10.00', target=target)
    assert result['refund_status'] == 'recorded'
    assert allocation(other)[1] == Decimal('40.00')
    assert allocation(target)[1] == Decimal('20.00')
    assert balance() == Decimal('60.00') and available() == 0


def test_refund_clears_legacy_target_reservation_when_other_cart_holds_full_balance():
    grant_credit()
    other, target = draft(), draft()
    with transaction() as conn:
        conn.executemany('INSERT INTO order_service(order_id,credit_requested) VALUES (?,?)',
                         [(other, '70.00'), (target, '30.00')])
    result = grant_credit('20.00', target=target)
    assert result['refund_status'] == 'recorded'
    assert result['credit_target_order_id'] is None
    assert Decimal(result['credit_target_allocation']) == 0
    assert allocation(other)[1] == Decimal('70.00')
    assert allocation(target)[1] == 0
    assert balance() == Decimal('70.00') and available() == 0
