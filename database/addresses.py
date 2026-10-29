"""Owned saved delivery addresses and immutable checkout address snapshots."""
from contextlib import contextmanager
import json
from uuid import uuid4

from database.connection import audit_context, connect, transaction


REQUIRED_FIELDS = ('full_name', 'phone', 'address_line1', 'city', 'state', 'postal_code', 'country')
DELIVERY_FIELDS = (*REQUIRED_FIELDS, 'address_line2', 'landmark')
LIMITS = {'label': 50, 'full_name': 200, 'phone': 30, 'address_line1': 200,
          'address_line2': 200, 'city': 100, 'state': 100, 'postal_code': 20,
          'country': 2, 'landmark': 200}


def _identifier(value, label):
    if (not isinstance(value, str) or not 1 <= len(value) <= 50
            or not all(c.isascii() and (c.isalnum() or c in '_-') for c in value)):
        raise ValueError(f'Invalid {label}.')
    return value


def _validate(fields, old=None):
    if not isinstance(fields, dict) or not fields:
        raise ValueError('Provide the address fields.')
    unknown = set(fields) - set(LIMITS) - {'is_default'}
    if unknown:
        raise ValueError('Only the supported address fields may be changed.')
    values = {name: old[name] if old else '' for name in LIMITS}
    for name, value in fields.items():
        if name == 'is_default':
            if type(value) is not bool:
                raise ValueError('Default address must be true or false.')
            continue
        if (not isinstance(value, str) or len(value) > LIMITS[name]
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError(f'{name.replace("_", " ").capitalize()} must contain at most {LIMITS[name]} characters without line breaks.')
        values[name] = value.strip()
    missing = [name.replace('_', ' ') for name in REQUIRED_FIELDS if not values[name]]
    if missing:
        raise ValueError('Required address fields: ' + ', '.join(missing) + '.')
    country = values['country']
    if len(country) != 2 or not country.isascii() or not country.isalpha():
        raise ValueError('Country must be a two-letter country code, such as US or IN.')
    values['country'] = country.upper()
    phone = values['phone']
    digits = ''.join(c for c in phone if c.isascii() and c.isdigit())
    if not 7 <= len(digits) <= 15 or any(c not in '0123456789+()- ' for c in phone):
        raise ValueError('Phone must contain 7 to 15 digits; spaces, +, hyphens and parentheses are allowed.')
    postal = values['postal_code']
    if not any(c.isalnum() for c in postal) or any(not (c.isalnum() or c in ' -') for c in postal):
        raise ValueError('Postal code must contain letters or digits, with optional spaces or hyphens.')
    return values


def _customer(conn, customer_id):
    _identifier(customer_id, 'customer ID')
    if not conn.execute('SELECT 1 FROM customers WHERE customer_id=?', (customer_id,)).fetchone():
        raise ValueError('Customer not found.')


def _owned(conn, customer_id, address_id):
    _identifier(address_id, 'address ID')
    row = conn.execute('SELECT * FROM customer_addresses WHERE address_id=? AND customer_id=? AND active=1',
                       (address_id, customer_id)).fetchone()
    if row is None:
        raise ValueError('Saved address not found for this customer.')
    return row


def _result(row):
    result = dict(row)
    result['is_default'] = bool(result['is_default'])
    result['active'] = bool(result['active'])
    return result


def address_snapshot(row):
    """Delivery facts only; changing a nickname/default does not change a quote."""
    return {name: row[name] for name in DELIVERY_FIELDS}


@contextmanager
def _audit(action, customer_id, address_id=None, order_id=None):
    # Avoid storing recipient, telephone or street fields in billing audit payloads.
    arguments = {'customer_id': customer_id}
    if address_id:
        arguments['address_id'] = address_id
    if order_id:
        arguments['order_id'] = order_id
    token = audit_context.set({'action': action, 'arguments': arguments})
    try:
        yield
    finally:
        audit_context.reset(token)


def list_addresses(customer_id):
    conn = connect(readonly=True)
    try:
        _customer(conn, customer_id)
        return [_result(row) for row in conn.execute(
            'SELECT * FROM customer_addresses WHERE customer_id=? AND active=1 ORDER BY is_default DESC,address_id',
            (customer_id,))]
    finally:
        conn.close()


def _invalidate_drafts(conn, customer_id, address_id):
    conn.execute('DELETE FROM order_delivery WHERE customer_id=? AND address_id=? '
                 "AND order_id IN (SELECT order_id FROM orders WHERE customer_id=? AND order_status='draft')",
                 (customer_id, address_id, customer_id))


def _draft_order(conn, order_id, customer_id):
    order = conn.execute('SELECT order_status,order_invoice FROM orders WHERE order_id=? AND customer_id=?',
                         (order_id, customer_id)).fetchone()
    if order is None:
        raise ValueError('Order not found for this customer.')
    if order['order_status'] != 'draft' or (order['order_invoice'] or '').strip():
        raise ValueError('Delivery addresses can only be selected for an active draft.')
    if conn.execute("SELECT 1 FROM order_details WHERE order_id=? AND status='cancelled'", (order_id,)).fetchone():
        raise ValueError('This order was cancelled. Select an active draft instead.')
    service = conn.execute('SELECT fulfillment_status FROM order_service WHERE order_id=?', (order_id,)).fetchone()
    if service and service[0] != 'unknown':
        raise ValueError('This draft has inconsistent fulfillment records. Ask store staff to review it before checkout.')


def save_address(customer_id, fields, address_id=None):
    updating = address_id is not None
    if address_id is not None:
        _identifier(address_id, 'address ID')
    address_id = address_id or 'ADDR-' + uuid4().hex[:24]
    with _audit('save_address', customer_id, address_id), transaction() as conn:
        _customer(conn, customer_id)
        existing = _owned(conn, customer_id, address_id) if updating else None
        values = _validate(fields, existing)
        first = not conn.execute('SELECT 1 FROM customer_addresses WHERE customer_id=? AND active=1', (customer_id,)).fetchone()
        default = fields.get('is_default', bool(existing['is_default']) if existing else False) or first
        if existing is not None and address_snapshot(existing) != address_snapshot(values):
            _invalidate_drafts(conn, customer_id, address_id)
        if default:
            conn.execute('UPDATE customer_addresses SET is_default=0 WHERE customer_id=? AND is_default=1', (customer_id,))
        conn.execute('INSERT INTO customer_addresses(address_id,customer_id,' + ','.join(LIMITS)
                     + ',is_default,active) VALUES (' + ','.join('?' for _ in range(len(LIMITS) + 4)) + ') '
                     'ON CONFLICT(address_id) DO UPDATE SET '
                     + ','.join(name + '=excluded.' + name for name in LIMITS) + ',is_default=excluded.is_default',
                     (address_id, customer_id, *(values[name] for name in LIMITS), int(default), 1))
        if not conn.execute('SELECT 1 FROM customer_addresses WHERE customer_id=? AND active=1 AND is_default=1', (customer_id,)).fetchone():
            conn.execute('UPDATE customer_addresses SET is_default=1 WHERE address_id='
                         '(SELECT address_id FROM customer_addresses WHERE customer_id=? AND active=1 '
                         'ORDER BY (address_id=?),address_id LIMIT 1)', (customer_id,address_id))
        return _result(_owned(conn, customer_id, address_id))


def delete_address(customer_id, address_id):
    with _audit('delete_address', customer_id, address_id), transaction() as conn:
        _customer(conn, customer_id)
        row = _owned(conn, customer_id, address_id)
        _invalidate_drafts(conn, customer_id, address_id)
        conn.execute('UPDATE customer_addresses SET active=0,is_default=0 WHERE address_id=? AND customer_id=?',
                     (address_id, customer_id))
        if row['is_default']:
            conn.execute('UPDATE customer_addresses SET is_default=1 WHERE address_id='
                         '(SELECT address_id FROM customer_addresses WHERE customer_id=? AND active=1 ORDER BY address_id LIMIT 1)',
                         (customer_id,))
        return {'address_id': address_id, 'deleted': True}


def set_default_address(customer_id, address_id):
    with _audit('set_default_address', customer_id, address_id), transaction() as conn:
        _customer(conn, customer_id)
        _owned(conn, customer_id, address_id)
        conn.execute('UPDATE customer_addresses SET is_default=0 WHERE customer_id=? AND is_default=1', (customer_id,))
        conn.execute('UPDATE customer_addresses SET is_default=1 WHERE address_id=? AND customer_id=?', (address_id, customer_id))
        return _result(_owned(conn, customer_id, address_id))


def select_order_address_conn(conn, order_id, customer_id, address_id):
    _customer(conn, customer_id)
    _identifier(order_id, 'order ID')
    row = _owned(conn, customer_id, address_id)
    _draft_order(conn, order_id, customer_id)
    snapshot = address_snapshot(_validate(address_snapshot(row)))
    conn.execute('INSERT INTO order_delivery(order_id,customer_id,address_id,address_snapshot) VALUES (?,?,?,?) '
                 'ON CONFLICT(order_id) DO UPDATE SET address_id=excluded.address_id,address_snapshot=excluded.address_snapshot',
                 (order_id, customer_id, address_id, json.dumps(snapshot, sort_keys=True)))
    return {'order_id': order_id, 'address_id': address_id, 'delivery_address': snapshot, 'order_status': 'draft'}


def select_order_address(order_id, customer_id, address_id):
    with _audit('select_order_address', customer_id, address_id, order_id), transaction() as conn:
        return select_order_address_conn(conn, order_id, customer_id, address_id)


def selected_address(conn, order_id, customer_id, *, require_current=False):
    if require_current:
        _draft_order(conn, order_id, customer_id)
    row = conn.execute('SELECT * FROM order_delivery WHERE order_id=? AND customer_id=?', (order_id, customer_id)).fetchone()
    if row is None:
        if require_current:
            raise ValueError('Select a saved delivery address for this order before checkout, then review and confirm the order.')
        return None
    try:
        snapshot = json.loads(row['address_snapshot'])
        validated = _validate(snapshot)
    except (ValueError, TypeError):
        raise ValueError('The selected delivery address is invalid. Select a valid saved address and review the order again.') from None
    if require_current:
        current = _owned(conn, customer_id, row['address_id'])
        if address_snapshot(current) != address_snapshot(validated):
            raise ValueError('The selected delivery address changed. Select it again and review the current order before checkout.')
    return {'address_id': row['address_id'], 'delivery_address': address_snapshot(validated)}
