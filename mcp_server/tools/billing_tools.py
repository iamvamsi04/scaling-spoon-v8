"""All mutations are serialized by BEGIN IMMEDIATE and use Decimal totals."""
import functools
import re
import sqlite3
import inspect
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4
from database.connection import transaction, connect, audit_context


class BillingError(ValueError):
    pass


def tool_result(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        token = audit_context.set({'action': fn.__name__,
            'arguments': dict(inspect.signature(fn).bind(*args, **kwargs).arguments)})
        try:
            return {"ok": True, **fn(*args, **kwargs)}
        except (BillingError, InvalidOperation) as exc:
            return {"ok": False, "error": str(exc)}
        except sqlite3.Error:
            return {"ok": False, "error": "Database operation failed; no transaction was committed."}
        finally:
            audit_context.reset(token)
    return wrapped


def money(value):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0 or value > Decimal("99999999.99"):
        raise BillingError("Money must be finite, nonnegative and fit DECIMAL(10,2).")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", value):
        raise BillingError(f"Invalid {label}; use 1-50 letters, digits, underscores or hyphens.")
    return value


def order_row(conn, order_id, customer_id, *, allow_cancelled=False):
    row = conn.execute("SELECT * FROM orders WHERE order_id=? AND customer_id=?",
                       (order_id, customer_id)).fetchone()
    if row is None:
        raise BillingError("Order not found for this customer.")
    if row['order_status'] == 'cancelled' and not allow_cancelled:
        raise BillingError("This order was cancelled. Create a new draft; cancelled orders cannot be reopened.")
    if row['order_status'] == 'draft':
        if row['refund_amount'] is not None or row['refund_status']:
            raise BillingError('Draft has inconsistent refund records. Staff must correct its records before changes.')
        if conn.execute("SELECT 1 FROM order_fulfilled WHERE order_id=? UNION ALL "
                        "SELECT 1 FROM invoice_lines WHERE order_id=? LIMIT 1", (order_id, order_id)).fetchone():
            raise BillingError('Draft has unissued fulfillment or invoice records. Staff must correct its records before changes.')
    return row


def is_placed(conn, order):
    return order['order_status'] in ('placed', 'cancelled') and bool((order['order_invoice'] or '').strip())


def lines_for(conn, order_id):
    return conn.execute("SELECT r.item_code,r.quantity,i.sellingprice,i.availablequantity "
                        "FROM order_requests r JOIN items i ON i.item_code=r.item_code "
                        "WHERE r.order_id=? ORDER BY r.item_code", (order_id,)).fetchall()


def price_for(conn, item, date):
    rows = conn.execute("SELECT unit_price FROM price_history WHERE item_code=? "
                        "AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)",
                        (item["item_code"], date, date)).fetchall()
    if len(rows) > 1:
        raise BillingError("Overlapping price history requires an operator correction.")
    return money(rows[0][0] if rows else item["sellingprice"])


def totals(conn, subtotal, promo_code=None, *, check_expiry=True):
    config = dict(conn.execute("SELECT key,value FROM billing_config"))
    tax_rate = Decimal(config.get("tax_rate", "0"))
    if not tax_rate.is_finite() or not 0 <= tax_rate <= 100:
        raise BillingError("tax_rate must be a percentage between 0 and 100.")
    discount = Decimal(0)
    if promo_code:
        promo = conn.execute("SELECT * FROM promotions WHERE promo_code=?", (promo_code,)).fetchone()
        if not promo:
            raise BillingError("Promotion not found.")
        if check_expiry:
            from datetime import date
            try:
                expiry = date.fromisoformat(promo['expires_on'])
                if expiry.isoformat() != promo['expires_on']:
                    raise ValueError()
            except (ValueError, TypeError):
                raise BillingError('Coupon has no valid expiry date. Ask store staff to correct it.')
            today = date.fromisoformat(conn.execute('SELECT CURRENT_DATE').fetchone()[0])
            if expiry < today:
                raise BillingError('Coupon has expired. It cannot be applied or used at checkout.')
        value = money(promo["discount_value"])
        if promo["discount_type"] == "percent" and value <= 100:
            discount = money(subtotal * value / 100)
        elif promo["discount_type"] == "flat":
            discount = value
        else:
            raise BillingError("Invalid promotion definition.")
    discount = min(discount, subtotal)
    tax = money((subtotal - discount) * tax_rate / 100)
    shipping = money(config.get("shipping_amount", "0"))
    return dict(subtotal=str(money(subtotal)), discount_applied=str(money(discount)),
                tax_amount=str(tax), shipping_amount=str(shipping),
                charged_price=str(money(subtotal - discount + tax + shipping)))


def credit_for_total(conn, order_id, total):
    """Describe draft credit at a proposed total without changing any records."""
    total = money(total)
    row = conn.execute('''SELECT o.order_status,COALESCE(s.credit_requested,0)
        FROM orders o LEFT JOIN order_service s ON s.order_id=o.order_id
        WHERE o.order_id=?''', (order_id,)).fetchone()
    credit = money(row[1]) if row else money(0)
    if row and row[0] == 'draft':
        credit = min(credit, total)
    return {'store_credit_requested': str(credit),
            'remaining_invoice_amount': str(total - credit)}


def update_totals(conn, order_id, values):
    """Keep an existing draft allocation within its revised invoice total."""
    conn.execute("UPDATE orders SET subtotal=:subtotal,discount_applied=:discount_applied,"
                 "tax_amount=:tax_amount,shipping_amount=:shipping_amount,charged_price=:charged_price "
                 "WHERE order_id=:order_id", {**values, "order_id": order_id})
    credit = credit_for_total(conn, order_id, values['charged_price'])
    conn.execute('''UPDATE order_service SET credit_requested=? WHERE order_id=?
        AND EXISTS (SELECT 1 FROM orders o WHERE o.order_id=order_service.order_id
                    AND o.order_status='draft')''',
        (credit['store_credit_requested'], order_id))
    return credit


@tool_result
def request_order(customer_id: str, items: list[dict], order_id: str | None = None) -> dict:
    """Create a draft. items contains item_code and integer quantity. Reuse order_id on retries."""
    identifier(customer_id, "customer ID")
    order_id = identifier(order_id or "ORD-" + uuid4().hex[:20], "order ID")
    audit_context.get()['arguments']['order_id'] = order_id
    if not isinstance(items, list) or not items or len(items) > 100:
        raise BillingError("Provide between 1 and 100 items.")
    quantities = {}
    for item in items:
        if not isinstance(item, dict):
            raise BillingError("Each item must contain item_code and quantity.")
        code = identifier(item.get("item_code"), "item code")
        qty = item.get("quantity")
        if type(qty) is not int or not 1 <= qty <= 100000:
            raise BillingError("Quantity must be an integer between 1 and 100000.")
        quantities[code] = quantities.get(code, 0) + qty
        if quantities[code] > 100000:
            raise BillingError("Combined quantity exceeds 100000.")
    with transaction() as conn:
        if not conn.execute("SELECT 1 FROM customers WHERE customer_id=?", (customer_id,)).fetchone():
            raise BillingError("Customer not found.")
        existing = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if existing:
            order_row(conn, order_id, customer_id)
            previous = dict(conn.execute("SELECT item_code,quantity FROM order_requests WHERE order_id=?", (order_id,)))
            if existing["customer_id"] != customer_id or previous != quantities:
                raise BillingError("Order ID already exists with different request data.")
            return {"order_id": order_id, "already_exists": True, "status": existing['order_status'],
                    'order_status': existing['order_status']}
        date = conn.execute("SELECT CURRENT_DATE").fetchone()[0]
        subtotal = Decimal(0)
        for code, qty in quantities.items():
            item = conn.execute("SELECT * FROM items WHERE item_code=?", (code,)).fetchone()
            if item is None:
                raise BillingError(f"Item {code} not found.")
            if item["availablequantity"] < qty:
                raise BillingError(f"Insufficient inventory for {code}.")
            subtotal += price_for(conn, item, date) * qty
        values = totals(conn, subtotal)
        conn.execute("INSERT INTO orders(order_id,customer_id,subtotal,charged_price) VALUES (?,?,0,0)",
                     (order_id, customer_id))
        conn.executemany("INSERT INTO order_requests(order_id,item_code,quantity) VALUES (?,?,?)",
                         [(order_id, code, qty) for code, qty in quantities.items()])
        update_totals(conn, order_id, values)
        currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()
        conn.execute('INSERT INTO order_details(order_id,currency) VALUES (?,?)',
                     (order_id, currency[0] if currency else 'USD'))
    return {"order_id": order_id, "status": "draft", "order_status": "draft", "estimate": values,
            "currency": currency[0] if currency else 'USD',
            "note": "Inventory is not reserved. Placement recalculates totals using order-date prices."}


@tool_result
def apply_promotion(order_id: str | None, customer_id: str, promo_code: str, action: str = 'apply') -> dict:
    """Check or apply only the coupon code the customer supplied.

    action=validate previews a code without changing anything; order_id may be null
    for a validity check before a draft exists. action=apply requires a draft and
    the customer's request to apply that specific code. Never discover, select or
    automatically apply another code. One unexpired coupon is allowed per order.
    """
    if action not in ('apply', 'validate'):
        raise BillingError('Coupon action must be apply or validate.')
    if not isinstance(promo_code, str):
        raise BillingError('Provide the coupon code you received.')
    promo_code = identifier(promo_code.strip(), 'coupon code')
    if action == 'apply' and not order_id:
        raise BillingError('Create a draft before applying a coupon.')
    with transaction() as conn:
        matches = conn.execute('SELECT * FROM promotions WHERE promo_code=? COLLATE NOCASE',
                               (promo_code,)).fetchmany(2)
        if not matches:
            raise BillingError('Promotion not found.')
        if len(matches) != 1:
            raise BillingError('This coupon code has an ambiguous store definition. Ask store staff to correct it.')
        promo = matches[0]
        promo_code = promo['promo_code']
        # Run the same definition and expiry checks for previews and checkout.
        totals(conn, Decimal(0), promo_code)
        preview = {'promo_code': promo_code, 'valid': True,
                   'discount_type': promo['discount_type'],
                   'discount_value': str(money(promo['discount_value'])),
                   'expires_on': promo['expires_on']}
        if action == 'validate' and not order_id:
            return {**preview, 'applied': False,
                    'note': 'Coupon checked only. No draft or invoice was changed.'}
        order = order_row(conn, order_id, customer_id)
        if is_placed(conn, order):
            raise BillingError("Promotions can only change before order placement.")
        if order['promo_code'] and order['promo_code'] != promo_code:
            raise BillingError('Only one coupon code is allowed per order. A coupon is already applied.')
        lines = lines_for(conn, order_id)
        if not lines:
            raise BillingError("Order has no requested items.")
        subtotal = sum((price_for(conn, line, order["order_date"]) * line["quantity"] for line in lines), Decimal(0))
        values = totals(conn, subtotal, promo_code)
        if action == 'validate':
            return {**preview, 'order_id': order_id, 'applied': False, **values,
                    **credit_for_total(conn, order_id, values['charged_price']),
                    'note': 'Preview only. The coupon has not been applied by this check.'}
        conn.execute("UPDATE orders SET promo_code=? WHERE order_id=?", (promo_code, order_id))
        credit = update_totals(conn, order_id, values)
    return {"order_id": order_id, "promo_code": promo_code, 'applied': True, **values, **credit}


@tool_result
def place_order(order_id: str, customer_id: str) -> dict:
    """Fulfill a confirmed draft atomically; repeat calls return the existing invoice."""
    with transaction() as conn:
        order = order_row(conn, order_id, customer_id)
        service=conn.execute('SELECT fulfillment_status FROM order_service WHERE order_id=?',(order_id,)).fetchone()
        if service and service[0]=='cancelled': raise BillingError('Order is cancelled. Create a new draft instead of placing it again.')
        if is_placed(conn, order):
            return {"order_id": order_id, "invoice_number": order["order_invoice"], "already_placed": True,
                    'status': 'placed', 'order_status': 'placed'}
        lines = lines_for(conn, order_id)
        if not lines:
            raise BillingError("Order has no requested items.")
        subtotal = Decimal(0)
        for line in lines:
            qty, code = line["quantity"], line["item_code"]
            price = price_for(conn, line, order["order_date"])
            amount = money(price * qty)
            changed = conn.execute("UPDATE items SET availablequantity=availablequantity-? "
                                   "WHERE item_code=? AND availablequantity>=?", (qty, code, qty))
            if changed.rowcount != 1:
                raise BillingError(f"Insufficient inventory for {code}.")
            conn.execute("INSERT INTO order_fulfilled(order_id,item_code,quantity) VALUES (?,?,?)", (order_id, code, qty))
            conn.execute("INSERT INTO invoice_lines(order_id,item_code,qty_billed,unit_price_billed,line_total) "
                         "VALUES (?,?,?,?,?)", (order_id, code, qty, str(price), str(amount)))
            subtotal += amount
        values = totals(conn, subtotal, order["promo_code"])
        if any(money(order[key]) != money(value) for key, value in values.items()):
            raise BillingError('Draft prices or charges changed. Refresh the draft with manage_draft and confirm the new totals before placement.')
        configured_currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()
        draft_currency = conn.execute('SELECT currency FROM order_details WHERE order_id=?', (order_id,)).fetchone()
        if draft_currency and draft_currency[0] != (configured_currency[0] if configured_currency else 'USD'):
            raise BillingError('Billing currency changed after this draft. Create a new draft and confirm its currency and totals.')
        from mcp_server.tools.service_workflows import redeem_credit
        redeemed = redeem_credit(conn,order)
        update_totals(conn, order_id, values)
        invoice = "INV-" + uuid4().hex[:24].upper()
        conn.execute("UPDATE orders SET order_invoice=?,order_status='placed' WHERE order_id=?", (invoice, order_id))
        currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()
        customer = dict(conn.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone())
        billing=conn.execute('SELECT * FROM customer_billing_details WHERE customer_id=?',(customer_id,)).fetchone()
        customer.update(billing_address=billing['billing_address'] if billing else '',tax_registration_number=billing['tax_registration_number'] if billing else '')
        conn.execute("INSERT INTO order_details(order_id,currency,due_date,customer_snapshot) "
                     "VALUES (?,?,date('now','+30 days'),?) ON CONFLICT(order_id) DO UPDATE SET "
                     "currency=excluded.currency, customer_snapshot=excluded.customer_snapshot, "
                     "due_date=COALESCE(order_details.due_date,excluded.due_date)",
                     (order_id, currency[0] if currency else 'USD', json.dumps(customer)))
        conn.execute("INSERT INTO order_service(order_id,fulfillment_status) VALUES (?,'placed') "
                     "ON CONFLICT(order_id) DO UPDATE SET fulfillment_status='placed'", (order_id,))
        due_date = conn.execute('SELECT due_date FROM order_details WHERE order_id=?', (order_id,)).fetchone()[0]
    return {"order_id": order_id, "invoice_number": invoice, "status": "placed", 'order_status': 'placed', **values,
            "currency": currency[0] if currency else 'USD', 'due_date': due_date,
            "store_credit_applied":str(redeemed),"remaining_invoice_amount":str(money(values["charged_price"])-redeemed)}


def check_return_window(conn, order):
    from config import settings
    from datetime import date
    try:
        window_days = int(settings.RETURN_WINDOW_DAYS)
        if window_days < 0:
            raise ValueError()
        ordered = date.fromisoformat(order['order_date'])
        if ordered.isoformat() != order['order_date']:
            raise ValueError()
        today = date.fromisoformat(conn.execute('SELECT CURRENT_DATE').fetchone()[0])
    except (ValueError, TypeError):
        raise BillingError('Return-window configuration or order date is invalid; refund rejected for staff review.')
    age_days = (today - ordered).days
    if age_days < 0:
        raise BillingError('Order date is in the future; refund rejected for staff review.')
    if age_days > window_days:
        raise BillingError(f'Order is outside the {window_days}-day return window ({age_days} days since order date). Refund rejected.')


def _refund_items(encoded):
    """Read saved request terms without trusting imported JSON to have the right shape."""
    try:
        items = json.loads(encoded)
    except (TypeError, ValueError):
        raise BillingError('Stored return details are invalid; staff review is required.')
    if not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get('item_code'), str) for item in items):
        raise BillingError('Stored return details are invalid; staff review is required.')
    return sorted(items, key=lambda item: item['item_code'])


@tool_result
def process_refund(order_id: str, customer_id: str, reason: str = '', amount: str | None = None,
                   action: str = 'request', confirmed: bool = False, items: list[dict] | None = None,
                   disposition: str | None = None, target_order_id: str | None = None,
                   return_received: bool = False, waiver_reason: str = '',
                   date_from: str | None = None, date_to: str | None = None) -> dict:
    """Customers request a refund; staff approve an existing pending request with action=approve and confirmed=true in staff mode.

    bulk_preview/bulk_request evaluate all owned orders between inclusive date_from/date_to;
    bulk_request needs confirmed=true and reason and submits each eligible order for approval.
    For bulk actions pass order_id=''. No automatic staff approval occurs.
    action=quote estimates an item/full merchandise return without creating a request; reason may be blank.
    action=status reads local status; escalate records a confirmed staff-review flag with reason.
    items selects billed item_code/quantity; refund is computed with allocated discount/tax, excluding shipping.
    disposition=store_credit optionally links target_order_id to an owned draft.
    Approval requires return_received=true or an admin waiver_reason for physical returns.
    Customers only request. Cashiers approve within the configured currency/limit;
    admins approve any valid amount. Pending requests do not change invoice totals.
    Requests and approvals must be within RETURN_WINDOW_DAYS of order_date (inclusive UTC days).
    Approval records a local ledger refund, never a payment-provider transfer.
    """
    from mcp_server.access import scope
    from config import settings
    access = scope.get() or {}
    role = access.get('mode')
    if role not in ('customer', 'cashier', 'admin'):
        raise BillingError('A trusted execution role is required for refunds.')
    if role == 'customer' and access.get('customer_id') != customer_id:
        raise BillingError('Select the matching customer first.')
    if not isinstance(reason, str) or len(reason) > 2000:
        raise BillingError('A refund reason must be text of at most 2000 characters.')
    if not isinstance(waiver_reason, str) or len(waiver_reason) > 2000:
        raise BillingError('A return waiver must be text of at most 2000 characters.')
    if type(confirmed) is not bool or type(return_received) is not bool:
        raise BillingError('Confirmation and return receipt must be explicit true or false values.')
    if disposition is not None and disposition not in ('original_method', 'store_credit'):
        raise BillingError('Choose original_method or store_credit; another card is not supported.')
    if target_order_id is not None:
        identifier(target_order_id, 'target order ID')
    if action != 'approve' and (return_received or waiver_reason):
        raise BillingError('Only staff approving an existing request can record a return receipt or waiver.')
    if action in ('bulk_preview','bulk_request'):
        if role!='customer': raise BillingError('Only the associated customer can request bulk returns.')
        from mcp_server.tools.service_workflows import bulk_refunds
        return bulk_refunds(customer_id,date_from,date_to,action,reason,confirmed,disposition or 'original_method')
    identifier(order_id, 'order ID')
    identifier(customer_id, 'customer ID')
    if action in ('status','escalate'):
        from mcp_server.tools.service_workflows import refund_status
        return refund_status(order_id,customer_id,action,reason,confirmed)
    if action not in ('request', 'approve','quote'):
        raise BillingError('Use quote, request, approve, status, escalate or a bulk action.')
    if action == 'request' and role != 'customer':
        raise BillingError('Only the associated customer can submit a refund request. Staff cannot create their own requests.')
    if action == 'approve' and role not in ('cashier', 'admin'):
        raise BillingError('Only a cashier or admin can approve refunds.')
    if action == 'approve' and confirmed is not True:
        raise BillingError('Explicit staff confirmation is required before approval.')
    if action!='quote' and not reason.strip():
        raise BillingError('A refund reason of 1-2000 characters is required.')
    with transaction() as conn:
        order = order_row(conn, order_id, customer_id, allow_cancelled=True)
        if not is_placed(conn, order):
            raise BillingError('Only placed orders can be refunded.')
        check_return_window(conn, order)
        if order['refund_status'] or order['refund_amount'] is not None:
            raise BillingError('A refund is already recorded; duplicate refunds are blocked.')
        currency_row = conn.execute("SELECT COALESCE(NULLIF(TRIM(d.currency),''),"
            "(SELECT value FROM billing_config WHERE key='currency'),'USD') "
            "FROM orders o LEFT JOIN order_details d ON d.order_id=o.order_id WHERE o.order_id=?",
            (order_id,)).fetchone()
        currency = currency_row[0].strip().upper()
        pending = conn.execute('SELECT * FROM refund_requests WHERE order_id=?', (order_id,)).fetchone()
        if order['order_status'] == 'cancelled':
            cancellation = conn.execute("SELECT 1 FROM refund_details WHERE order_id=? AND request_kind='cancellation'", (order_id,)).fetchone()
            if action != 'approve' or not pending or not cancellation:
                raise BillingError('Cancelled orders only allow staff approval of their existing cancellation refund request.')
        if action == 'approve' and (not pending or pending['status'] != 'pending'):
            raise BillingError('An existing pending customer refund request is required before approval.')
        if pending and pending['status'] != 'pending':
            raise BillingError('This refund request is no longer pending.')
        from mcp_server.tools.service_workflows import return_quote, check_target, billed_lines
        selected = []
        quoted = None
        if action in ('request','quote'):
            disposition = disposition or 'original_method'
            if disposition=='original_method' and conn.execute("SELECT 1 FROM store_credit_ledger WHERE order_id=? AND kind='order_redemption'",(order_id,)).fetchone():
                raise BillingError('This order used store credit. Choose store_credit for the return; no alternative card transfer is supported.')
            if target_order_id:
                if disposition != 'store_credit': raise BillingError('Only store credit can be linked to a cart.')
                check_target(conn,target_order_id,customer_id)
                from mcp_server.tools.service_workflows import currency_for
                if currency_for(conn,target_order_id) != currency: raise BillingError('Cart and refund currencies must match.')
            if items is not None:
                quoted, selected = return_quote(conn,order,items)
                if amount is not None and money(amount) != quoted: raise BillingError(f'Returned-item refund must equal the calculated amount {quoted}.')
            elif amount is None:
                # Full merchandise return excludes shipping; cancellation refunds have their own workflow.
                all_items=[{'item_code':r['item_code'],'quantity':r['qty']} for r in conn.execute('SELECT item_code,SUM(qty_billed) qty FROM invoice_lines WHERE order_id=? GROUP BY item_code',(order_id,))]
                quoted, selected=return_quote(conn,order,all_items)
            else:
                billed_lines(conn, order)
            if pending and action!='quote':
                prior=conn.execute('SELECT * FROM refund_details WHERE order_id=?',(order_id,)).fetchone()
                if prior and (prior['disposition']!=disposition or prior['target_order_id']!=target_order_id or _refund_items(prior['items_json'])!=sorted(selected, key=lambda item: item['item_code'])):
                    raise BillingError('A request already exists with different return or store credit terms.')
        refund = money(quoted if quoted is not None else (pending['amount'] if pending and amount is None else order['charged_price'] if amount is None else amount))
        if action in ('request', 'quote') and items is None and amount is not None:
            ceiling=money(order['charged_price'])-money(order['shipping_amount'])
            if refund>ceiling: raise BillingError('Return refund exceeds merchandise and tax; shipping is nonrefundable.')
        if refund <= 0 or refund > money(order['charged_price']):
            raise BillingError('Refund must be positive and no greater than the invoice total.')
        if action=='quote':
            return {'order_id':order_id,'order_status':order['order_status'],'quoted_amount':str(refund),'currency':currency,'items':selected,
                    'basis':{'subtotal':str(money(order['subtotal'])),'discount':str(money(order['discount_applied'])),'tax':str(money(order['tax_amount'])),'nonrefundable_shipping':str(money(order['shipping_amount']))},
                    'note':'Estimate only. No refund request was created or approved.'}
        if pending and (refund != money(pending['amount']) or currency != pending['currency']
                        or reason.strip() != pending['reason']):
            raise BillingError('A request already exists. Approve its exact amount, currency and reason; do not replace it.')
        if action == 'approve' and role == 'cashier':
            limit = money(settings.CASHIER_REFUND_LIMIT)
            if currency != settings.CASHIER_REFUND_CURRENCY or refund > limit:
                raise BillingError(f'Admin approval required. Cashier limit is {limit} {settings.CASHIER_REFUND_CURRENCY}.')
        if action == 'approve':
            details=conn.execute('SELECT * FROM refund_details WHERE order_id=?',(order_id,)).fetchone()
            if not details:
                conn.execute('INSERT INTO refund_details(order_id) VALUES (?)',(order_id,))
                details=conn.execute('SELECT * FROM refund_details WHERE order_id=?',(order_id,)).fetchone()
            if ((disposition is not None and disposition != details['disposition'])
                    or (target_order_id is not None and target_order_id != details['target_order_id'])):
                raise BillingError('Approve the customer request as submitted; its refund method and target cart cannot be changed.')
            requested_items = _refund_items(details['items_json'])
            if items is not None:
                _, approval_items = return_quote(conn, order, items)
                if sorted(approval_items, key=lambda item: item['item_code']) != sorted(requested_items, key=lambda item: item['item_code']):
                    raise BillingError('Approve only the returned items in the pending customer request.')
            if requested_items:
                current_quote, current_items = return_quote(conn, order, requested_items)
                if current_quote != refund or sorted(current_items, key=lambda item: item['item_code']) != sorted(requested_items, key=lambda item: item['item_code']):
                    raise BillingError('Billed return details changed after the customer request. Staff review is required before approval.')
            else:
                billed_lines(conn, order)
                if details['request_kind'] == 'return' and refund > money(order['charged_price']) - money(order['shipping_amount']):
                    raise BillingError('Return refund exceeds merchandise and tax; shipping is nonrefundable.')
            if details['request_kind']=='return' and not details['return_received']:
                if waiver_reason:
                    if role!='admin' or len(waiver_reason.strip())<5 or len(waiver_reason)>2000:
                        raise BillingError('Only an admin can waive a return with a documented assessment reason.')
                    conn.execute('UPDATE refund_details SET waiver_reason=? WHERE order_id=?',(waiver_reason.strip(),order_id))
                elif return_received is True:
                    conn.execute('UPDATE refund_details SET return_received=1 WHERE order_id=?',(order_id,))
                else:
                    raise BillingError('Confirm physical return received, or ask admin for a documented return waiver.')
        if not pending:
            conn.execute('INSERT INTO refund_requests(order_id,amount,currency,reason) VALUES (?,?,?,?)',
                         (order_id, str(refund), currency, reason.strip()))
            conn.execute('INSERT INTO refund_details(order_id,items_json,disposition,target_order_id,request_kind) VALUES (?,?,?,?,?)',
                (order_id,json.dumps(selected),disposition,target_order_id,'return'))
        audit_context.get()['arguments'].update(execution_role=role, currency=currency, resolved_amount=str(refund))
        if action == 'request':
            return {'order_id':order_id, 'order_status':order['order_status'], 'requested_amount':str(refund), 'currency':currency,
                    'refund_status':'pending_approval', 'items':selected, 'disposition':disposition,
                    'calculation_basis':{'subtotal':str(money(order['subtotal'])),'discount':str(money(order['discount_applied'])),'tax':str(money(order['tax_amount'])),'nonrefundable_shipping':str(money(order['shipping_amount']))}, 'note':'Awaiting staff approval. No refund has been recorded or transferred.'}
        details=conn.execute('SELECT * FROM refund_details WHERE order_id=?',(order_id,)).fetchone()
        credited_target = None
        target_allocation = money(0)
        if details and details['disposition']=='store_credit':
            conn.execute("INSERT INTO store_credit_ledger(customer_id,order_id,currency,amount,kind) VALUES (?,?,?,?,'refund_credit')",(customer_id,order_id,currency,str(refund)))
            if details['target_order_id']:
                target=conn.execute("SELECT * FROM orders WHERE order_id=? AND customer_id=? AND order_status='draft'",(details['target_order_id'],customer_id)).fetchone()
                from mcp_server.tools.service_workflows import service_action, available_credit, currency_for
                if target and currency_for(conn,target['order_id'])==currency:
                    prior_allocation=conn.execute('SELECT credit_requested FROM order_service WHERE order_id=?',(target['order_id'],)).fetchone()
                    previous=money(prior_allocation[0]) if prior_allocation else money(0)
                    allocated=min(refund+previous,available_credit(conn,customer_id,currency,target['order_id']),money(target['charged_price']))
                    if allocated>0 or previous>0:
                        service_action(conn,target,'apply_store_credit','',str(allocated))
                    if allocated>0:
                        credited_target = target['order_id']
                        target_allocation = allocated
        conn.execute("UPDATE orders SET refund_amount=?,refund_status='recorded',refund_reason=? WHERE order_id=?",
                     (str(refund), reason.strip(), order_id))
        conn.execute("UPDATE refund_requests SET status='approved',approved_at=CURRENT_TIMESTAMP,approved_by_role=? WHERE order_id=?",
                     (role, order_id))
    return {'order_id':order_id, 'order_status':order['order_status'], 'refund_amount':str(refund), 'currency':currency,
            'refund_status':'recorded', 'approved_by_role':role,
            'disposition':details['disposition'] if details else 'original_method',
            'credit_target_order_id':credited_target,
            'credit_target_allocation':str(target_allocation),
            'requested_credit_target_order_id':details['target_order_id'] if details else None,
            'note':'Approved and recorded in the local ledger only. No payment-provider refund was sent.'}


@tool_result
def download_invoice(invoice_number: str, customer_id: str, document_type: str = 'invoice',
                     order_id: str | None = None, action: str = 'generate',
                     billing_address: str | None = None, company: str | None = None,
                     tax_registration_number: str | None = None, confirmed: bool = False) -> dict:
    """Generate invoice, tax or proforma PDF; correct_details saves confirmed invoice contact revisions.

    Proforma needs order_id and invoice_number=''. Tax needs recorded company/address/tax number.
    Contact corrections never change billed quantities, monetary values or original snapshots.
    """
    from mcp_server.tools.service_workflows import invoice_document
    return invoice_document(invoice_number,customer_id,document_type,order_id,action,
                            billing_address,company,tax_registration_number,confirmed)
