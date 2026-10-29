from address_support import place_order_with_address, prepare_delivery
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import pytest
from database.connection import connect, transaction
from database.migrations import add_promotion_expiry
from mcp_server.tools.billing_tools import request_order, apply_promotion, place_order


def draft():
    oid = request_order('CUST001',[{'item_code':'ITEM001','quantity':2}])['order_id']
    prepare_delivery(oid, 'CUST001')
    return oid


def expiry(offset):
    with transaction() as conn:
        conn.execute("UPDATE promotions SET expires_on=date('now',?) WHERE promo_code='SAVE10'", (offset,))


@pytest.mark.parametrize('offset,valid',[('-1 day',False),('0 days',True),('+1 day',True)])
def test_expiry_boundary(offset,valid):
    expiry(offset)
    oid=draft()
    result=apply_promotion(oid,'CUST001','SAVE10')
    assert result['ok'] is valid
    with closing(connect()) as conn:
        row=conn.execute('SELECT promo_code,discount_applied FROM orders WHERE order_id=?',(oid,)).fetchone()
        assert (row['promo_code']=='SAVE10') is valid
        if not valid: assert row['discount_applied']==0


def test_second_coupon_rejected_and_same_code_never_stacks():
    oid=draft()
    first=apply_promotion(oid,'CUST001','SAVE10')
    repeat=apply_promotion(oid,'CUST001','SAVE10')
    assert first['ok'] and repeat['discount_applied']==first['discount_applied']=='15.98'
    assert not apply_promotion(oid,'CUST001','SAVE500')['ok']
    assert not apply_promotion(oid,'CUST001','SAVE10,SAVE500')['ok']
    with closing(connect()) as conn:
        row=conn.execute('SELECT * FROM orders WHERE order_id=?',(oid,)).fetchone()
        assert row['promo_code']=='SAVE10' and row['discount_applied']==15.98
    assert place_order_with_address(oid,'CUST001')['ok']


def test_checkout_rechecks_expiry_without_partial_fulfillment():
    oid=draft()
    assert apply_promotion(oid,'CUST001','SAVE10')['ok']
    expiry('-1 day')
    result=place_order_with_address(oid,'CUST001')
    assert not result['ok'] and 'expired' in result['error']
    with closing(connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM invoice_lines').fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM order_fulfilled').fetchone()[0]==0
        assert conn.execute("SELECT availablequantity FROM items WHERE item_code='ITEM001'").fetchone()[0]==40
        assert conn.execute('SELECT order_invoice FROM orders WHERE order_id=?',(oid,)).fetchone()[0] is None


def test_expiry_does_not_rewrite_existing_invoices():
    oid=draft()
    assert apply_promotion(oid,'CUST001','SAVE10')['ok']
    placed=place_order_with_address(oid,'CUST001')
    expiry('-1 day')
    assert place_order_with_address(oid,'CUST001')['invoice_number']==placed['invoice_number']
    with closing(connect()) as conn:
        assert conn.execute('SELECT charged_price FROM orders WHERE order_id=?',(oid,)).fetchone()[0]==143.82


@pytest.mark.parametrize('value',[None,'','not-a-date','2026-02-30','2026-13-01','2026-1-01','2026-10-19T12:00:00'])
def test_database_rejects_missing_or_invalid_expiry(value):
    with pytest.raises(sqlite3.IntegrityError):
        with transaction() as conn:
            conn.execute("INSERT INTO promotions(promo_code,discount_type,discount_value,expires_on) VALUES ('INVALID','flat',5,?)",(value,))


def test_migration_preserves_codes_and_disables_undated_legacy_promos():
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE promotions(promo_code TEXT PRIMARY KEY, description TEXT, discount_type TEXT, discount_value NUMERIC)')
        conn.execute("INSERT INTO promotions VALUES ('OLD','Legacy','flat',10)")
        add_promotion_expiry(conn)
        add_promotion_expiry(conn)
        assert conn.execute('SELECT * FROM promotions').fetchone()==('OLD','Legacy','flat',10,'1970-01-01')
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE promotions SET expires_on='2026-02-30'")
        conn.execute("UPDATE promotions SET expires_on='2028-02-29'")


def test_concurrent_different_coupons_only_one_wins():
    oid=draft()
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda code:apply_promotion(oid,'CUST001',code),['SAVE10','SAVE500']))
    assert sum(r['ok'] for r in results)==1
    winner=next(r for r in results if r['ok'])
    with closing(connect()) as conn:
        assert conn.execute('SELECT promo_code FROM orders WHERE order_id=?',(oid,)).fetchone()[0]==winner['promo_code']
