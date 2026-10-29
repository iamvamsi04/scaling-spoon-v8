"""Explicit delivery arrangements for pre-existing billing workflow tests."""
from contextlib import closing

from database.addresses import list_addresses, save_address, select_order_address, selected_address
from database.connection import connect
from mcp_server.tools.billing_tools import place_order


ADDRESS_FIELDS = {'full_name': 'Test Customer', 'phone': '+1 202 555 0147',
                  'address_line1': '25 Market Street', 'city': 'Boston', 'state': 'MA',
                  'postal_code': '02108', 'country': 'US'}


def prepare_delivery(order_id, customer_id):
    """Choose a real owned address before an existing test reviews or places its draft.

    Invalid owners, missing orders and cancelled/issued orders are deliberately
    left alone so negative tests exercise their original production validation.
    Already selected delivery facts are validated and never silently replaced.
    """
    with closing(connect(readonly=True)) as conn:
        order = conn.execute('SELECT order_status,order_invoice FROM orders WHERE order_id=? AND customer_id=?',
                             (order_id, customer_id)).fetchone()
        if not order or order['order_status'] != 'draft' or (order['order_invoice'] or '').strip():
            return None
        existing = selected_address(conn, order_id, customer_id)
        if existing:
            return selected_address(conn, order_id, customer_id, require_current=True)
    addresses = list_addresses(customer_id)
    address = addresses[0] if addresses else save_address(customer_id, ADDRESS_FIELDS)
    return select_order_address(order_id, customer_id, address['address_id'])


def place_order_with_address(order_id, customer_id, **kwargs):
    """Explicit setup for tests whose concern is billing rather than address entry."""
    prepare_delivery(order_id, customer_id)
    return place_order(order_id, customer_id, **kwargs)
