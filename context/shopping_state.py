"""Small persistent shopping memory. Preferences are model interpreted; orders are DB facts."""
from copy import deepcopy
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from context.reply_text import plain_reply, reply_envelope
import logging
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from database.connection import connect

log = logging.getLogger(__name__)
ShortText = Annotated[str, Field(min_length=1, max_length=300)]
ProductID = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,50}$')]


class Budget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    scope: Literal['unknown', 'per_item', 'merchandise_total', 'all_in'] = 'unknown'


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['place_order', 'replace_items', 'cancel_draft',
                    'update_customer', 'process_refund', 'cancel_order', 'apply_store_credit',
                    'correct_invoice', 'report_billing_issue', 'escalate_refund', 'bulk_refund', 'apply_promotion',
                    'add_address', 'update_address', 'delete_address', 'set_default_address']
    order_id: ProductID | None = None
    promo_code: ProductID | None = None
    summary: str = Field(min_length=1, max_length=1500)
    arguments: dict | None = None
    delivery: dict | None = None

    @field_validator('arguments')
    @classmethod
    def bounded_arguments(cls, values):
        if values is None:
            return None
        if any(not isinstance(key, str) or key in {'order_id','customer_id','action','confirmed'} or
               key.startswith('execution_') for key in values):
            raise ValueError('Proposal arguments contain business details only, without scope or approval flags.')
        pending = [(values, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 6:
                raise ValueError('Proposal arguments are too deeply nested.')
            if isinstance(value, dict):
                if len(value) > 50 or any(not isinstance(key,str) or not key.strip() or len(key)>100 for key in value):
                    raise ValueError('Proposal arguments require short field names.')
                pending.extend((item, depth+1) for item in value.values())
            elif isinstance(value, list):
                if len(value) > 100:
                    raise ValueError('Proposal arguments contain too many records.')
                pending.extend((item, depth+1) for item in value)
        try:
            encoded = json.dumps(values, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, OverflowError, UnicodeError):
            raise ValueError('Proposal arguments require finite JSON values.') from None
        if len(encoded)>8000:
            raise ValueError('Proposal arguments are too large.')
        return values


class ShoppingUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reset_preferences: bool = False
    needs: list[ShortText] = Field(default_factory=list, max_length=10)
    budget: Budget | None = None
    preferred_features: dict[str, str | None] = Field(default_factory=dict)
    quantity: int | None = Field(default=None, ge=1, le=100000, strict=True)
    displayed_product_ids: list[ProductID] = Field(default_factory=list, max_length=10)
    selected_product_ids: list[ProductID] = Field(default_factory=list, max_length=10)
    active_draft_id: ProductID | None = None
    pending_confirmation: Proposal | None = None
    next_question: str | None = Field(default=None, max_length=500)

    @field_validator('preferred_features')
    @classmethod
    def bounded_features(cls, values):
        if len(values) > 20 or any(not key.strip() or len(key) > 100 or
                (value is not None and len(value) > 300) for key, value in values.items()):
            raise ValueError('Use up to 20 short feature preferences.')
        return values


def empty_state():
    return {'schema_version': 1, 'stage': 'discover', 'needs': [], 'budget': None,
            'preferred_features': {}, 'quantity': None, 'known_products': [],
            'compared_product_ids': [], 'displayed_product_ids': [], 'selected_product_ids': [],
            'current_draft': None, 'last_order': None, 'pending_confirmation': None,
            'next_question': None, 'product_query': None, 'provided_coupon_codes': []}


def shopping_stage(state):
    return ('confirmation' if state.get('pending_confirmation') else
        'quote' if state.get('current_draft') else 'after_sale' if state.get('last_order') else
        'selection' if state.get('selected_product_ids') else
        'comparison' if state.get('compared_product_ids') else
        'browse' if state.get('needs') or state.get('known_products') else 'discover')


def clear_confirmation(state):
    result = deepcopy(state)
    result['pending_confirmation'] = result['next_question'] = None
    result['stage'] = shopping_stage(result)
    return result


def placement_review(order):
    """Render the actual saved quote; model-written amounts never authorize it."""
    currency = order['currency']
    summary = f"Please confirm order {order['order_id']} for {currency} {order['totals']['charged_price']}."
    lines = [summary, 'Items:']
    lines.extend(f"{item['quantity']} x {item['name']}" for item in order['items'])
    for key, label in (('subtotal','Subtotal'), ('discount_applied','Discount'),
                       ('tax_amount','Tax'), ('shipping_amount','Shipping'), ('charged_price','Total')):
        lines.append(f"{label}: {currency} {order['totals'][key]}")
    lines.append(f"Store credit: {currency} {order['store_credit_requested']}")
    lines.append(f"Remaining invoice amount: {currency} {order['remaining_invoice_amount']}")
    return summary, '\n'.join(lines)


def canonical_proposal_arguments(arguments):
    """Normalize only structured business data; never classify user language."""
    if not isinstance(arguments, dict):
        raise ValueError('Proposal arguments must be an object.')
    values = {}
    controls = {'action', 'confirmed', 'order_id', 'customer_id'}
    for key, value in arguments.items():
        if not isinstance(key, str):
            raise ValueError('Proposal argument names must be text.')
        if key in controls or key.startswith('execution_') or value is None:
            continue
        if key in ('amount', 'credit_amount'):
            try:
                if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
                    raise ValueError('A proposal amount must be a money value.')
                amount = Decimal(str(value))
                if not amount.is_finite() or not 0 <= amount <= Decimal('99999999.99'):
                    raise ValueError('A proposal amount must fit DECIMAL(10,2).')
                rounded = amount.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                if rounded != amount:
                    raise ValueError('A proposal amount must not contain fractional cents.')
                value = format(rounded, '.2f')
            except InvalidOperation as exc:
                raise ValueError('A proposal amount must be a money value.') from exc
        values[key] = value
    try:
        values = json.loads(json.dumps(values, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError('Proposal arguments must contain finite JSON values.') from exc
    Proposal.bounded_arguments(values)
    return values


def proposal_details(arguments):
    """Display validated proposal values without interpreting conversational text."""
    values = canonical_proposal_arguments(arguments)
    lines = []

    def show(value, label):
        if isinstance(value, dict):
            if not value:
                lines.append(label + ': empty')
            for key, child in value.items():
                show(child, (label + ' ' + key.replace('_', ' ')).strip())
        elif isinstance(value, list):
            if not value:
                lines.append(label + ': none')
            for index, child in enumerate(value, 1):
                show(child, f'{label} item {index}')
        else:
            shown = ('yes' if value else 'no') if isinstance(value, bool) else str(value)
            lines.append(f'{label[:1].upper() + label[1:]}: {shown}')
        if sum(len(line) + 1 for line in lines) > 5000:
            raise ValueError('Proposed details are too long to show completely. Use a smaller change.')

    for key, value in values.items():
        show(value, key.replace('_', ' '))
    return '\n'.join(lines)


def order_snapshot(order_id, customer_id):
    if not isinstance(customer_id, str) or not customer_id or not isinstance(order_id, str) or not order_id:
        return None
    with closing(connect(readonly=True)) as conn:
        conn.execute('BEGIN')
        row = conn.execute('''SELECT o.*,d.status,d.currency FROM orders o
            LEFT JOIN order_details d ON d.order_id=o.order_id
            WHERE o.order_id=? AND o.customer_id=?''', (order_id, customer_id)).fetchone()
        if row is None:
            return None
        service=conn.execute('SELECT fulfillment_status,credit_requested,shipped_at,delivered_at,cancelled_at FROM order_service WHERE order_id=?',(order_id,)).fetchone()
        state = row['order_status']
        currency = row['currency'] or conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()[0]
        result = {'order_id': row['order_id'], 'order_date': row['order_date'], 'status': state, 'invoice_number': row['order_invoice'],
                  'currency': currency, 'promo_code': row['promo_code'],
                  'totals': {k:format(Decimal(str(row[k])), '.2f') for k in
                             ('subtotal','discount_applied','tax_amount','shipping_amount','charged_price')},
                  'items': [dict(r) for r in conn.execute('''SELECT r.item_code,i.name,r.quantity
                      FROM order_requests r JOIN items i ON i.item_code=r.item_code
                      WHERE r.order_id=? ORDER BY r.item_code''', (order_id,))]}
        credit=Decimal(str(service['credit_requested'])) if service else Decimal(0)
        result['store_credit_requested']=format(credit,'.2f')
        result['remaining_invoice_amount']=format(Decimal(str(row['charged_price']))-credit,'.2f')
        result['fulfillment_status'] = service['fulfillment_status'] if service else None
        result['fulfillment_dates'] = {key: service[key] if service else None for key in
                                       ('shipped_at', 'delivered_at', 'cancelled_at')}
        result['refund'] = {key: row[key] for key in ('refund_amount', 'refund_status', 'refund_reason')}
        refund = conn.execute('''SELECT r.status,r.amount,r.currency,r.reason,d.disposition,d.target_order_id,
            r.requested_at,r.approved_at,d.items_json,d.request_kind,d.return_received,d.waiver_reason,
            d.escalated_at,d.escalation_note
            FROM refund_requests r LEFT JOIN refund_details d ON d.order_id=r.order_id WHERE r.order_id=?''',
            (order_id,)).fetchone()
        result['refund_request'] = dict(refund) if refund else None
        from database.addresses import selected_address
        try:
            delivery = selected_address(conn, order_id, customer_id)
        except ValueError:
            delivery = None
        result['address_id'] = delivery['address_id'] if delivery else None
        result['delivery_address'] = delivery['delivery_address'] if delivery else None
        result['invoice_revision'] = conn.execute('SELECT MAX(id) FROM invoice_revisions WHERE order_id=?',
                                                 (order_id,)).fetchone()[0]
        if row['order_invoice']:
            result['billed_items'] = [dict(line) for line in conn.execute('''SELECT item_code,qty_billed,
                unit_price_billed,line_total FROM invoice_lines WHERE order_id=? ORDER BY item_code''', (order_id,))]
    result['fingerprint'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


class ShoppingTracker:
    def __init__(self, state, customer_id, checkpoint=None):
        defaults = empty_state()
        self.state = {**defaults, **{key: deepcopy(value) for key, value in (state or {}).items()
                                    if key in defaults}}
        self.customer_id = customer_id
        self.checkpoint = checkpoint
        self.completed_actions = set()
        self.refresh()
        # The previous proposal is context for this user message. A new reply must
        # explicitly renew a proposal; an unrelated answer must not leave a stale 'yes'.

    def refresh(self):
        draft = self.state['current_draft']
        if draft:
            current = order_snapshot(draft['order_id'], self.customer_id)
            self.set_order(current)
        proposal = self.state['pending_confirmation']
        if proposal and proposal.get('order_id'):
            current = order_snapshot(proposal['order_id'], self.customer_id)
            if not current or current['fingerprint'] != proposal.get('order_fingerprint'):
                self.state['pending_confirmation'] = None
        self.stage()

    def set_order(self, snapshot):
        if snapshot and snapshot['status'] == 'draft':
            self.state['current_draft'] = snapshot
        else:
            self.state['current_draft'] = None
            if snapshot:
                self.state['last_order'] = {k:snapshot[k] for k in ('order_id','status','invoice_number')}

    def stage(self):
        self.state['stage'] = shopping_stage(self.state)

    def remember_products(self, ids):
        ids = list(dict.fromkeys(i for i in ids if isinstance(i,str)))[:200]
        if not ids:
            return
        with closing(connect(readonly=True)) as conn:
            rows = dict(conn.execute('SELECT item_code,name FROM items WHERE item_code IN (' +
                                    ','.join('?' for _ in ids)+')', ids))
        known = {p['item_code']:p for p in self.state['known_products']}
        for code in ids:
            if code in rows:
                known.pop(code, None)
                known[code] = {'item_code':code, 'name':rows[code][:200]}
        self.state['known_products'] = list(known.values())[-40:]

    def observe(self, name, arguments, result, customer_id):
        if not result.get('ok'):
            return
        self.customer_id = customer_id
        if name == 'compare_products':
            ids = [p['item_code'] for p in result.get('products', [])]
            self.remember_products(ids)
            self.state['compared_product_ids'] = ids
        elif name == 'find_cheaper_alternatives':
            self.remember_products([result['source']['item_code']] +
                                   [p['item_code'] for p in result.get('alternatives', [])])
        elif name == 'execute_sql_query' and 'item_code' in result.get('columns', []):
            self.state['product_query'] = {
                'query': arguments.get('query'),
                'parameters': deepcopy(arguments.get('parameters') or []),
                'row_count': result.get('row_count', 0),
                'truncated': result.get('truncated', False)}
            index = result['columns'].index('item_code')
            self.remember_products([row[index] for row in result.get('rows', [])])
        elif name == 'apply_promotion' and result.get('applied') is False:
            # Checking a code is not a basket change, even when its preview has totals.
            pass
        elif name in ('request_order','manage_draft','apply_promotion','place_order','process_refund'):
            snapshot = order_snapshot(result.get('order_id'), customer_id)
            if name != 'process_refund':
                self.set_order(snapshot)
            elif snapshot:
                self.state['last_order'] = {k:snapshot[k] for k in ('order_id','status','invoice_number')}
            if snapshot:
                self.remember_products([p['item_code'] for p in snapshot['items']])
            if name == 'process_refund':
                action = {'request': 'process_refund', 'escalate': 'escalate_refund',
                          'bulk_request': 'bulk_refund'}.get(arguments.get('action', 'request'))
            elif name == 'manage_draft':
                action = arguments.get('action')
                if action == 'cancel':
                    action = 'cancel_order' if snapshot and snapshot['invoice_number'] else 'cancel_draft'
            else:
                action = name
            # A quote/status/preview is evidence for the next proposal, never an
            # already submitted request. Mark only actual successful mutations.
            if action:
                self.state['pending_confirmation'] = None
                self.completed_actions.add((action, None if action == 'bulk_refund' else result.get('order_id')))
        elif name == 'download_invoice' and arguments.get('action') == 'correct_details':
            self.state['pending_confirmation'] = None
            with closing(connect(readonly=True)) as conn:
                corrected = conn.execute('SELECT order_id FROM orders WHERE order_invoice=? AND customer_id=?',
                                         (arguments.get('invoice_number'), customer_id)).fetchone()
            if corrected:
                self.completed_actions.add(('correct_invoice', corrected[0]))
        elif name == 'manage_customer':
            self.state['pending_confirmation'] = None
            action = arguments.get('action','create')
            self.completed_actions.add((action if action in ('add_address','update_address','delete_address','set_default_address')
                                        else action+'_customer', None))
            if self.state.get('current_draft'):
                self.set_order(order_snapshot(self.state['current_draft']['order_id'], customer_id))
        self.stage()
        if self.checkpoint:
            self.checkpoint(deepcopy(self.state), self.customer_id)

    def apply_update(self, values, reply):
        parsed = ShoppingUpdate.model_validate(values)
        update = parsed.model_dump(mode='json', exclude_unset=True)
        if parsed.budget is not None:
            update['budget'] = parsed.budget.model_dump(mode='json')
        snapshot = None
        if update.get('active_draft_id'):
            snapshot = order_snapshot(update['active_draft_id'], self.customer_id)
            if not snapshot or snapshot['status'] != 'draft':
                raise ValueError('Active draft must belong to the associated customer.')
        proposed = update.get('pending_confirmation')
        proposed_order = order_snapshot(proposed.get('order_id'), self.customer_id) if proposed else None
        # An existing owned order is product evidence even in a brand-new chat.
        # Load its saved lines before validating model-selected product references.
        order_products = [item['item_code'] for order in
                          (self.state['current_draft'], snapshot, proposed_order) if order
                          for item in order['items']]
        # Validate all product references before applying any preference changes.
        allowed = {p['item_code'] for p in self.state['known_products']} | set(order_products)
        for key in ('selected_product_ids','displayed_product_ids','compared_product_ids'):
            allowed.update(self.state[key])
        for key in ('displayed_product_ids','selected_product_ids'):
            if key in update and (len(set(update[key])) != len(update[key]) or not set(update[key]) <= allowed):
                raise ValueError('Product selections must come from observed product results.')
        features = {} if update.get('reset_preferences') else dict(self.state['preferred_features'])
        for key, value in update.get('preferred_features', {}).items():
            if value is None:
                features.pop(key, None)
            else:
                features[key] = value
        if len(features) > 20:
            raise ValueError('Consolidate preferences before adding more than 20 features.')
        self.remember_products(order_products)
        if update.get('reset_preferences'):
            for key in ('needs','budget','preferred_features','quantity','compared_product_ids',
                        'displayed_product_ids','selected_product_ids'):
                self.state[key] = empty_state()[key]
            self.state['last_order'] = None
            self.state['product_query'] = None
        for key in ('needs','budget','quantity','displayed_product_ids','selected_product_ids'):
            if key in update:
                self.state[key] = update[key]
        self.state['preferred_features'] = features
        if 'active_draft_id' in update:
            self.set_order(snapshot)
        self.state['next_question'] = update.get('next_question')
        if proposed and (proposed['action'], proposed.get('order_id')) not in self.completed_actions:
            order_id = proposed.get('order_id')
            order = proposed_order
            action = proposed['action']
            account_action = action in ('update_customer','add_address','update_address','delete_address','set_default_address')
            bulk_action = action == 'bulk_refund'
            expected = ('placed', 'cancelled') if action in ('correct_invoice', 'report_billing_issue', 'escalate_refund') else (
                ('placed',) if action in ('process_refund', 'cancel_order') else ('draft',))
            eligible = bool(order and order['status'] in expected)
            if action == 'place_order':
                eligible = eligible and bool(order['delivery_address'])
            elif action == 'cancel_order':
                eligible = eligible and order['fulfillment_status'] == 'placed' and not order['refund_request'] and order['refund']['refund_amount'] is None
            elif action == 'process_refund':
                eligible = eligible and not order['refund_request'] and order['refund']['refund_amount'] is None
            elif action == 'correct_invoice':
                eligible = eligible and bool(order['invoice_number'])
            elif action == 'escalate_refund':
                eligible = eligible and bool(order['refund_request'])
            elif action == 'apply_promotion':
                eligible = eligible and bool(proposed.get('promo_code')) and proposed['promo_code'].upper() in self.state['provided_coupon_codes']
            if ((account_action and not order_id and self.customer_id) or (bulk_action and not order_id and self.customer_id) or
                    (not account_action and not bulk_action and eligible)):
                proposed['status'] = 'awaiting_customer'
                proposed['order_fingerprint'] = order['fingerprint'] if order else None
                if action == 'place_order':
                    proposed['summary'] = placement_review(order)[0]
                    proposed['arguments'] = None
                    proposed['delivery'] = {'address_id': order['address_id'],
                                            'delivery_address': order['delivery_address']}
                    self.set_order(order)
                self.state['pending_confirmation'] = proposed
        self.stage()

    def finish(self, content):
        self.state['pending_confirmation'] = None
        self.state['next_question'] = None
        raw = content.strip()
        if raw.startswith('```json') and raw.endswith('```'):
            raw = raw[7:-3].strip()
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            envelope = reply_envelope(raw)
            if envelope is None:
                if '"shopping_update"' in raw and (raw.startswith('{') or raw.startswith('```')):
                    raise ValueError('Shopping response JSON is incomplete.') from None
                # Plain text responses remain usable. Never guess preferences from keywords.
                self.stage()
                return plain_reply(content)
        if not isinstance(envelope,dict) or 'shopping_update' not in envelope:
            self.stage()
            return plain_reply(content)
        reply = envelope.get('reply')
        if not isinstance(reply,str) or not reply.strip():
            raise ValueError('Customer reply is missing from the shopping response.')
        reply = plain_reply(reply)
        update = deepcopy(envelope['shopping_update'])
        if isinstance(update, dict):
            proposal = update.get('pending_confirmation')
            if isinstance(proposal, dict) and proposal.get('action') == 'place_order':
                order = order_snapshot(proposal.get('order_id'), self.customer_id)
                if not order or order['status'] != 'draft' or not order['delivery_address']:
                    update['pending_confirmation'] = None
                    if update.get('active_draft_id') == proposal.get('order_id'):
                        update.pop('active_draft_id')
                    if order:
                        self.set_order(order)
                    if order and order['status'] == 'placed' and order['invoice_number']:
                        reply = f"Order {order['order_id']} was placed. Invoice {order['invoice_number']} was issued."
                    elif order and order['status'] == 'cancelled':
                        reply = 'No order was placed. This order is cancelled; create a new draft to purchase these items.'
                    elif order and order['status'] == 'draft':
                        reply = 'Select a delivery address for this draft in Delivery addresses and checkout, then review the complete order before confirming placement.'
                    else:
                        reply = 'No order was placed. This order is unavailable for this account.'
            if isinstance(proposal, dict) and isinstance(proposal.get('summary'), str):
                proposal['summary'] = plain_reply(proposal['summary'])
            if isinstance(update.get('next_question'), str):
                update['next_question'] = plain_reply(update['next_question'])
        saved = deepcopy(self.state)
        try:
            self.apply_update(update, reply)
        except (ValidationError, ValueError, TypeError):
            self.state = saved
            # Invalid optional preferences must not discard a separately valid
            # financial review. Revalidate that structured proposal on its own.
            try:
                self.apply_update({'pending_confirmation': update.get('pending_confirmation')}, reply)
            except (ValidationError, ValueError, TypeError, AttributeError):
                self.state = saved
            self.stage()
            log.warning('Invalid shopping update ignored; persisted facts and preferences retained.')
            if not self.state.get('pending_confirmation'):
                reply += '\n\nI could not save the updated shopping preferences for this reply. Please repeat any changed preferences next time.'
        try:
            return self.confirmation_review(reply)
        except (ValueError, TypeError):
            self.state['pending_confirmation'] = None
            self.stage()
            return 'The proposed details could not be displayed completely. Review a smaller change before confirming.'

    def confirmation_review(self, reply=None):
        """Render the saved proposal, optionally alongside a normal explanation."""
        proposal = self.state.get('pending_confirmation')
        if not proposal:
            return reply or ''
        if proposal['action'] == 'place_order':
            reply = placement_review(self.state['current_draft'])[1]
        elif reply is None:
            reply = proposal['summary']
        elif proposal['summary'] not in reply:
            # Presentation deduplication only, never an approval decision.
            reply += '\n\n' + proposal['summary']
        if proposal.get('arguments'):
            reply += '\n\nDetails for approval:\n' + proposal_details(proposal['arguments'])
        if proposal['action'] == 'place_order':
            delivery = (proposal.get('delivery') or {}).get('delivery_address')
            if delivery:
                fields = (('full_name', 'Full name'), ('phone', 'Phone'),
                          ('address_line1', 'Address line 1'), ('address_line2', 'Address line 2'),
                          ('city', 'City'), ('state', 'State / province / region'),
                          ('postal_code', 'Postal code'), ('country', 'Country'), ('landmark', 'Landmark'))
                lines = [f'{label}: {delivery[key]}' for key, label in fields if delivery.get(key)]
                reply += '\n\nDelivery address for approval:\n' + '\n'.join(lines)
        return reply
