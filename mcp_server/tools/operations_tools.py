"""Customer and draft workflows. No external money movement or message delivery."""
from contextlib import contextmanager
from decimal import Decimal
import re

from database.connection import connect, transaction
from mcp_server.tools.billing_tools import (BillingError, tool_result, identifier, money,
    order_row, is_placed, totals, update_totals, price_for)


def confirm(confirmed):
    if confirmed is not True:
        raise BillingError('Show the proposed change and obtain explicit confirmation first.')


def text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BillingError(f'{name} must contain 1-{maximum} characters.')
    return value.strip()


@contextmanager
def reading():
    conn = connect(readonly=True)
    try:
        conn.execute('BEGIN')
        yield conn
    finally:
        conn.close()


def ensure_customer(conn, customer_id):
    if not conn.execute('SELECT 1 FROM customers WHERE customer_id=?', (customer_id,)).fetchone():
        raise BillingError('Customer not found.')


@tool_result
def manage_customer(customer_id: str, name: str, company: str | None = None, email: str | None = None,
                    action: str = 'create', confirmed: bool = False, billing_address: str | None = None,
                    tax_registration_number: str | None = None) -> dict:
    """Create or update a customer after confirmation. Show the proposed contact changes first.

    Updates preserve omitted company/email fields. Explicit empty strings clear those fields.

    Existing issued invoice contact snapshots are preserved. Use SQL to look up a customer before editing.
    """
    confirm(confirmed)
    identifier(customer_id, 'customer ID')
    name = text(name, 'Name', 200)
    if company is not None and (not isinstance(company, str) or len(company) > 200):
        raise BillingError('Company must contain at most 200 characters.')
    if email is not None and (not isinstance(email, str) or len(email) > 200 or (email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email))):
        raise BillingError('Provide a valid email address or leave it empty.')
    with transaction() as conn:
        existing = conn.execute('SELECT * FROM customers WHERE customer_id=?', (customer_id,)).fetchone()
        company = (existing['company'] or '') if company is None and action == 'update' and existing else (company or '')
        email = (existing['email'] or '') if email is None and action == 'update' and existing else (email or '')
        if action == 'create':
            if existing:
                from mcp_server.access import scope
                execution = scope.get()
                if execution and execution.get('mode') == 'customer' and not execution.get('customer_id'):
                    raise BillingError('Existing customer: start a conversation with that customer selected.')
                if (existing['name'], existing['company'] or '', existing['email'] or '') != (name, company, email):
                    raise BillingError('Customer ID exists with different details. Use update after confirmation.')
                profile = conn.execute('SELECT * FROM customer_billing_details WHERE customer_id=?', (customer_id,)).fetchone()
                for field, value in (('billing_address', billing_address), ('tax_registration_number', tax_registration_number)):
                    if value is not None and (not isinstance(value, str) or value.strip() != (profile[field] if profile else '')):
                        raise BillingError('Customer ID exists with different billing details. Use update after confirmation.')
                return {'customer_id': customer_id, 'already_exists': True}
            conn.execute('INSERT INTO customers VALUES (?,?,?,?)', (customer_id, name, company, email))
        elif action == 'update':
            ensure_customer(conn, customer_id)
            conn.execute('UPDATE customers SET name=?,company=?,email=? WHERE customer_id=?', (name, company, email, customer_id))
        else:
            raise BillingError('Action must be create or update.')
        if billing_address is not None or tax_registration_number is not None:
            old=conn.execute('SELECT * FROM customer_billing_details WHERE customer_id=?',(customer_id,)).fetchone()
            address=billing_address if billing_address is not None else old['billing_address'] if old else ''
            tax_id=tax_registration_number if tax_registration_number is not None else old['tax_registration_number'] if old else ''
            if not isinstance(address,str) or len(address)>2000 or not isinstance(tax_id,str) or len(tax_id)>100:
                raise BillingError('Billing address must be at most 2000 characters; tax number at most 100.')
            conn.execute('INSERT INTO customer_billing_details VALUES (?,?,?) ON CONFLICT(customer_id) DO UPDATE SET billing_address=excluded.billing_address,tax_registration_number=excluded.tax_registration_number',(customer_id,address.strip(),tax_id.strip()))
    return {'customer_id': customer_id, 'name': name, 'company': company, 'email': email, 'action': action}


@tool_result
def manage_draft(order_id: str, customer_id: str, action: str, items: list[dict] | None = None,
                 reason: str = '', confirmed: bool = False, credit_amount: str | None = None) -> dict:
    """After confirmation: replace_items edits drafts; cancel cancels drafts or whole unshipped issued orders.
    apply_store_credit allocates credit_amount to an owned draft; report_billing_issue stores reason for review.
    Authenticated admin only: ship/deliver update issued-order fulfillment status.

    items is the complete desired list of item_code and integer quantity. Issued invoice lines and totals are immutable.
    Cancellation retains records; only whole unshipped issued orders restore their fulfilled stock.
    Set credit_amount to zero to clear a draft's store-credit allocation.
    """
    confirm(confirmed)
    from mcp_server.access import scope
    role=(scope.get() or {}).get('mode','customer')
    if role=='admin' and action not in ('ship','deliver'): raise BillingError('Admin order access permits only confirmed shipment/delivery transitions.')
    if action in ('ship','deliver') and role!='admin': raise BillingError('Only authenticated admin may update fulfillment status.')
    with transaction() as conn:
        existing_meta = conn.execute('SELECT order_status FROM orders WHERE order_id=?', (order_id,)).fetchone()
        if existing_meta and existing_meta['order_status'] == 'cancelled' and action == 'cancel':
            owner = conn.execute('SELECT customer_id FROM orders WHERE order_id=?', (order_id,)).fetchone()
            if owner and owner[0] == customer_id:
                return {'order_id': order_id, 'status': 'cancelled', 'order_status': 'cancelled', 'already_cancelled': True}
        order = order_row(conn, order_id, customer_id)
        if action in ('ship','deliver'):
            from mcp_server.tools.service_workflows import fulfillment_transition
            return fulfillment_transition(conn,order,action)
        if action in ('report_billing_issue','apply_store_credit'):
            from mcp_server.tools.service_workflows import service_action
            return service_action(conn,order,action,reason,credit_amount)
        if is_placed(conn, order) and action=='cancel':
            from mcp_server.tools.service_workflows import cancel_placed
            return cancel_placed(conn,order,text(reason,'Cancellation reason'))
        if is_placed(conn, order):
            raise BillingError('Issued invoices cannot be edited. Review the supported refund workflow with store staff.')
        if action == 'cancel':
            reason = text(reason, 'Cancellation reason')
            currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()[0]
            conn.execute("INSERT INTO order_details(order_id,currency,status,cancellation_reason) VALUES (?,?,'cancelled',?) "
                         "ON CONFLICT(order_id) DO UPDATE SET status='cancelled',cancellation_reason=excluded.cancellation_reason",
                         (order_id, currency, reason))
            conn.execute("UPDATE orders SET order_status='cancelled' WHERE order_id=?", (order_id,))
            return {'order_id': order_id, 'status': 'cancelled', 'order_status': 'cancelled', 'reason': reason}
        if action != 'replace_items' or not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise BillingError('Use cancel or replace_items with 1-100 line items.')
        quantities = {}
        for line in items:
            if not isinstance(line, dict):
                raise BillingError('Each item must contain item_code and quantity.')
            code = identifier(line.get('item_code'), 'item code')
            qty = line.get('quantity')
            if type(qty) is not int or not 1 <= qty <= 100000:
                raise BillingError('Quantity must be an integer from 1 to 100000.')
            quantities[code] = quantities.get(code, 0) + qty
            if quantities[code] > 100000:
                raise BillingError('Combined quantity exceeds 100000.')
        subtotal = Decimal(0)
        for code, qty in quantities.items():
            item = conn.execute('SELECT * FROM items WHERE item_code=?', (code,)).fetchone()
            if not item or item['availablequantity'] < qty:
                raise BillingError(f'Item {code} is unavailable or has insufficient stock.')
            subtotal += price_for(conn, item, order['order_date']) * qty
        values = totals(conn, subtotal, order['promo_code'])
        conn.execute('DELETE FROM order_requests WHERE order_id=?', (order_id,))
        conn.executemany('INSERT INTO order_requests(order_id,item_code,quantity) VALUES (?,?,?)',
                         [(order_id, code, qty) for code, qty in quantities.items()])
        update_totals(conn, order_id, values)
        return {'order_id': order_id, 'status': 'draft', 'order_status': 'draft', 'estimate': values}


