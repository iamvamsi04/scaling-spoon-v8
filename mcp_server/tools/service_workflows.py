"""Local store service records. No payment, card or settlement integration."""
import json
from contextlib import closing
from datetime import date
from decimal import Decimal
from database.connection import connect, transaction
from mcp_server.access import scope
from mcp_server.tools.billing_tools import BillingError, money, order_row, is_placed, tool_result


def currency_for(conn, oid):
    row=conn.execute("SELECT COALESCE(NULLIF(TRIM(d.currency),''),(SELECT value FROM billing_config WHERE key='currency'),'USD') FROM orders o LEFT JOIN order_details d ON d.order_id=o.order_id WHERE o.order_id=?",(oid,)).fetchone()
    return row[0].strip().upper()


def credit_balance(conn, customer, currency):
    return sum((Decimal(str(r[0])) for r in conn.execute('SELECT amount FROM store_credit_ledger WHERE customer_id=? AND currency=?',(customer,currency))),Decimal(0))


def check_target(conn, target, customer):
    row=order_row(conn,target,customer)
    if is_placed(conn,row): raise BillingError('Store credit target must be an active draft owned by this customer.')
    return row


def billed_lines(conn, order):
    """Reject inconsistent invoice records before calculating a refund or restocking."""
    lines = conn.execute('SELECT * FROM invoice_lines WHERE order_id=?', (order['order_id'],)).fetchall()
    if not lines:
        raise BillingError('Invoice has no billed lines. Staff must correct its records before a refund.')
    subtotal = Decimal(0)
    quantities = {}
    values = {}
    for line in lines:
        qty = line['qty_billed']
        if type(qty) is not int or qty <= 0 or money(money(line['unit_price_billed']) * qty) != money(line['line_total']):
            raise BillingError('Invoice line amounts or quantities are inconsistent. Staff review is required.')
        subtotal += money(line['line_total'])
        code = line['item_code']
        quantities[code] = quantities.get(code, 0) + qty
        values[code] = values.get(code, Decimal(0)) + money(line['line_total'])
    discount = money(order['discount_applied'])
    if subtotal != money(order['subtotal']) or discount > subtotal or money(subtotal-discount+money(order['tax_amount'])+money(order['shipping_amount'])) != money(order['charged_price']):
        raise BillingError('Invoice totals do not reconcile with billed lines. Staff review is required.')
    return quantities, values


def return_quote(conn, order, items):
    """Allocate actual discounted merchandise and recorded tax by returned billed value."""
    if not isinstance(items,list) or not items or len(items)>100: raise BillingError('Select 1-100 returned item lines.')
    quantities, values = billed_lines(conn, order)
    seen=set(); gross=Decimal(0); saved=[]
    for entry in items:
        if not isinstance(entry,dict): raise BillingError('Return lines need item_code and quantity.')
        code=entry.get('item_code'); qty=entry.get('quantity')
        if not isinstance(code,str) or code in seen or type(qty) is not int or qty<1: raise BillingError('Use unique returned item codes and positive integer quantities.')
        seen.add(code)
        billed_qty = quantities.get(code, 0)
        if not billed_qty or qty>billed_qty: raise BillingError('Returned quantity exceeds the billed quantity for this item.')
        portion=values[code]*qty/billed_qty; gross+=portion
        saved.append({'item_code':code,'quantity':qty,'billed_quantity':billed_qty,'returned_billed_value':str(money(portion))})
    subtotal=money(order['subtotal'])
    if subtotal<=0: raise BillingError('No positive billed merchandise is available to return.')
    value=money((subtotal-money(order['discount_applied'])+money(order['tax_amount']))*gross/subtotal)
    if value<=0 or value>money(order['charged_price']): raise BillingError('No positive eligible refund for these returned items.')
    return value,saved


def refund_status(order_id,customer_id,action,reason='',confirmed=False):
    if action=='escalate' and (confirmed is not True or not reason.strip() or len(reason)>2000):
        raise BillingError('Confirm escalation and provide a review reason of at most 2000 characters.')
    with transaction() as conn:
        order=order_row(conn,order_id,customer_id,allow_cancelled=True)
        request=conn.execute('SELECT * FROM refund_requests WHERE order_id=?',(order_id,)).fetchone()
        if not request and order['refund_amount'] is None: raise BillingError('No refund request or recorded refund exists for this order.')
        if action=='escalate':
            if not request: raise BillingError('This legacy refund needs manual staff review; no request reference is stored.')
            conn.execute('INSERT OR IGNORE INTO refund_details(order_id) VALUES (?)',(order_id,))
            conn.execute('UPDATE refund_details SET escalated_at=COALESCE(escalated_at,CURRENT_TIMESTAMP),escalation_note=? WHERE order_id=?',(reason.strip(),order_id))
        details=conn.execute('SELECT * FROM refund_details WHERE order_id=?',(order_id,)).fetchone()
        return {'order_id':order_id,'order_status':order['order_status'],'local_status':request['status'] if request else 'legacy_recorded',
            'requested_amount':str(money(request['amount'])) if request else None,
            'recorded_amount':str(money(order['refund_amount'])) if order['refund_amount'] is not None else None,
            'approved_at':request['approved_at'] if request else None,
            'requested_at':request['requested_at'] if request else None,
            'disposition':details['disposition'] if details else 'original_method',
            'request_kind':details['request_kind'] if details else None,
            'review_reference':f'REVIEW-{order_id}' if details and details['escalated_at'] else None,
            'escalated_at':details['escalated_at'] if details else None,
            'bank_status':'Not available. Local records do not verify funds sent or received.',
            'policy_estimate':'5-10 business days after staff handles an approved original-method refund.'}


def cancel_placed(conn,order,reason):
    from mcp_server.tools.billing_tools import check_return_window
    oid=order['order_id']; service=conn.execute('SELECT * FROM order_service WHERE order_id=?',(oid,)).fetchone()
    if not service or service['fulfillment_status']=='unknown': raise BillingError('Shipment status is not recorded. Staff must verify it before cancellation.')
    state=service['fulfillment_status']
    if state=='cancelled': return {'order_id':oid,'status':'cancelled','order_status':'cancelled','already_cancelled':True}
    check_return_window(conn,order)
    if state in ('shipped','delivered'): raise BillingError('This order has shipped. Cancellation is unavailable; request a return instead.')
    if order['refund_status'] or order['refund_amount'] is not None or conn.execute('SELECT 1 FROM refund_requests WHERE order_id=?',(oid,)).fetchone():
        raise BillingError('A refund or return request already exists; staff must review it before cancellation.')
    if money(order['charged_price'])<=0: raise BillingError('This zero-value order requires staff cancellation review; no positive refund is due.')
    quantities, _ = billed_lines(conn, order)
    fulfilled = {}
    for line in conn.execute('SELECT item_code,quantity FROM order_fulfilled WHERE order_id=?', (oid,)):
        fulfilled[line['item_code']] = fulfilled.get(line['item_code'], 0) + line['quantity']
    if quantities != fulfilled:
        raise BillingError('Fulfilled and billed quantities do not match. Staff review is required before cancellation.')
    conn.execute("INSERT INTO order_service(order_id,fulfillment_status,cancelled_at,cancellation_restocked) VALUES (?,'cancelled',CURRENT_TIMESTAMP,1) ON CONFLICT(order_id) DO UPDATE SET fulfillment_status='cancelled',cancelled_at=CURRENT_TIMESTAMP,cancellation_restocked=1",(oid,))
    conn.execute("UPDATE orders SET order_status='cancelled' WHERE order_id=?", (oid,))
    for line in conn.execute('SELECT item_code,quantity FROM order_fulfilled WHERE order_id=?',(oid,)).fetchall():
        conn.execute('UPDATE items SET availablequantity=availablequantity+? WHERE item_code=?',(line['quantity'],line['item_code']))
    currency=currency_for(conn,oid)
    conn.execute('INSERT INTO refund_requests(order_id,amount,currency,reason) VALUES (?,?,?,?)',(oid,str(money(order['charged_price'])),currency,reason))
    used_credit=conn.execute("SELECT 1 FROM store_credit_ledger WHERE order_id=? AND kind='order_redemption'",(oid,)).fetchone()
    conn.execute("INSERT INTO refund_details(order_id,request_kind,disposition) VALUES (?,'cancellation',?)",(oid,'store_credit' if used_credit else 'original_method'))
    return {'order_id':oid,'status':'cancelled','order_status':'cancelled','refund_status':'pending_approval','requested_amount':str(money(order['charged_price'])),
            'currency':currency,'note':'Whole unshipped order cancelled. Invoice retained; refund awaits staff approval. Policy estimate is 5-10 business days after staff handling; no transfer was made.'}


def service_action(conn,order,action,reason,credit_amount):
    oid=order['order_id']; currency=currency_for(conn,oid)
    if action=='report_billing_issue':
        if not reason.strip() or len(reason)>2000: raise BillingError('Provide a billing review reason of 1-2000 characters.')
        conn.execute('INSERT INTO order_service(order_id,review_note,review_requested_at) VALUES (?,?,CURRENT_TIMESTAMP) ON CONFLICT(order_id) DO UPDATE SET review_note=excluded.review_note,review_requested_at=CURRENT_TIMESTAMP',(oid,reason.strip()))
        return {'order_id':oid,'order_status':order['order_status'],'review_reference':f'BILLING-REVIEW-{oid}','note':'Customer-reported issue recorded for staff review. Actual charges are not verified.'}
    if is_placed(conn,order): raise BillingError('Store credit can only be applied to an active draft.')
    amount=money(credit_amount) if credit_amount is not None else None
    if amount is None or amount>money(order['charged_price']): raise BillingError('Credit amount must be nonnegative and no greater than the draft total; use zero to clear it.')
    if amount>credit_balance(conn,order['customer_id'],currency): raise BillingError('Insufficient store credit in this currency.')
    conn.execute('INSERT INTO order_service(order_id,credit_requested) VALUES (?,?) ON CONFLICT(order_id) DO UPDATE SET credit_requested=excluded.credit_requested',(oid,str(amount)))
    return {'order_id':oid,'order_status':'draft','currency':currency,'credit_requested':str(amount), 'remaining_invoice_amount':str(money(order['charged_price'])-amount),
            'note':'Credit allocation saved on draft; available balance is rechecked and redeemed once at placement.'}


def redeem_credit(conn,order):
    row=conn.execute('SELECT credit_requested FROM order_service WHERE order_id=?',(order['order_id'],)).fetchone()
    amount=money(row[0]) if row else Decimal(0)
    if amount:
        currency=currency_for(conn,order['order_id'])
        if amount>money(order['charged_price']) or amount>credit_balance(conn,order['customer_id'],currency): raise BillingError('Requested store credit exceeds the current draft total or available credit. Refresh the allocation.')
        conn.execute("INSERT INTO store_credit_ledger(customer_id,order_id,currency,amount,kind) VALUES (?,?,?,?,'order_redemption')",(order['customer_id'],order['order_id'],currency,str(-amount)))
    return amount


def refund_report(report,date_from=None,date_to=None,customer_id=None,order_id=None):
    clauses=[]; args=[]
    for name,value in (('customer_id',customer_id),('order_id',order_id)):
        if value is not None and (not isinstance(value,str) or not value.strip() or len(value)>50):
            raise BillingError(f'{name} must contain 1-50 characters.')
    for value,op in ((date_from,'>='),(date_to,'<=')):
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat()!=value: raise ValueError()
            except (ValueError,TypeError): raise BillingError('Use ISO dates.')
            clauses.append(f'date(r.approved_at){op}?');args.append(value)
    if date_from and date_to and date_from>date_to: raise BillingError('Date range is reversed.')
    from config import settings
    with closing(connect(readonly=True)) as conn:
        conn.execute('BEGIN')
        if report in ('pending_refunds','pending_above_threshold'):
            rows=[]
            pending_clauses=[];pending_args=[]
            for column,value,operator in (('o.customer_id',customer_id,'='),('r.order_id',order_id,'='),('date(r.requested_at)',date_from,'>='),('date(r.requested_at)',date_to,'<=')):
                if value:
                    pending_clauses.append(column+operator+'?');pending_args.append(value)
            pending_where=' AND '+' AND '.join(pending_clauses) if pending_clauses else ''
            for r in conn.execute("SELECT r.*,o.customer_id FROM refund_requests r JOIN orders o ON o.order_id=r.order_id WHERE r.status='pending'"+pending_where+" ORDER BY r.requested_at,r.order_id",pending_args):
                if customer_id and r['customer_id']!=customer_id: continue
                if order_id and r['order_id']!=order_id: continue
                if date_from and r['requested_at'][:10]<date_from: continue
                if date_to and r['requested_at'][:10]>date_to: continue
                role='admin' if r['currency'].strip().upper()!=settings.CASHIER_REFUND_CURRENCY or money(r['amount'])>money(settings.CASHIER_REFUND_LIMIT) else 'cashier_or_admin'
                if report=='pending_above_threshold' and role!='admin': continue
                rows.append({**dict(r),'required_approver':role})
            return {'report':report,'requests':rows,'count':len(rows)}
        if customer_id: clauses.append('o.customer_id=?'); args.append(customer_id)
        if order_id: clauses.append('o.order_id=?'); args.append(order_id)
        where=' AND '+' AND '.join(clauses) if clauses else ''
        rows=conn.execute("SELECT r.*,o.refund_amount,o.refund_status,o.customer_id,d.disposition,c.amount credit_amount,c.currency credit_currency,c.customer_id credit_customer FROM refund_requests r JOIN orders o ON o.order_id=r.order_id LEFT JOIN refund_details d ON d.order_id=r.order_id LEFT JOIN store_credit_ledger c ON c.order_id=r.order_id AND c.kind='refund_credit' WHERE r.status='approved'"+where,args).fetchall()
        mismatches=[]
        for row in rows:
            if row['refund_amount'] is None or money(row['refund_amount'])!=money(row['amount']) or row['refund_status']!='recorded':
                mismatches.append({'order_id':row['order_id'],'issue':'Approved request and invoice refund differ'})
            if row['disposition']=='store_credit' and (row['credit_amount'] is None or money(row['credit_amount'])!=money(row['amount']) or row['credit_currency'].strip().upper()!=row['currency'].strip().upper() or row['credit_customer']!=row['customer_id']):
                mismatches.append({'order_id':row['order_id'],'issue':'Store credit allocation differs from approved request'})
            if row['disposition']!='store_credit' and row['credit_amount'] is not None: mismatches.append({'order_id':row['order_id'],'issue':'Unexpected store credit for original-method request'})
        # Refunds lacking request history are explicit gaps, not assumed bank failures.
        legacy_clauses=[];legacy_args=[]
        for column,value in (('o.customer_id',customer_id),('o.order_id',order_id)):
            if value: legacy_clauses.append(column+'=?');legacy_args.append(value)
        legacy_where=' AND '+' AND '.join(legacy_clauses) if legacy_clauses else ''
        undated_records=[]
        for row in conn.execute("SELECT o.order_id,r.status FROM orders o LEFT JOIN refund_requests r ON r.order_id=o.order_id WHERE (o.refund_amount IS NOT NULL OR NULLIF(o.refund_status,'') IS NOT NULL) AND (r.order_id IS NULL OR r.status!='approved')"+legacy_where,legacy_args):
            gap={'order_id':row[0],'issue':'Legacy refund has no request/approval history' if row['status'] is None else 'Invoice refund recorded without staff approval'}
            if not date_from and not date_to: mismatches.append(gap)
            else: undated_records.append(gap)
        return {'report':report,'approved_count':len(rows),'mismatches':mismatches,'count':len(mismatches),
                'undated_records':undated_records,'undated_count':len(undated_records),
                'scope':'Local approved refunds on approval dates; no bank settlements or transfers verified.'}


def invoice_document(invoice_number,customer_id,document_type,order_id,action,address,company,tax_id,confirmed):
    from database.invoice import generate_invoice
    if document_type not in ('invoice','tax','proforma'): raise BillingError('Choose invoice, tax or proforma.')
    if action not in ('generate','correct_details'): raise BillingError('Choose generate or correct_details.')
    if action=='correct_details' and document_type=='proforma': raise BillingError('Pro forma billing details come from the customer profile. Update that profile after confirmation.')
    if document_type=='proforma':
        if not order_id: raise BillingError('Pro forma needs an identified owned draft order_id.')
        identifier='PROFORMA-'+order_id
    elif document_type=='tax': identifier='TAX-'+invoice_number
    else: identifier=invoice_number
    if action=='correct_details':
        if confirmed is not True: raise BillingError('Confirm the exact invoice billing corrections first.')
        if not any(value is not None for value in (address,company,tax_id)): raise BillingError('Provide the corrected billing fields.')
        with transaction() as conn:
            order=conn.execute('SELECT o.*,d.customer_snapshot FROM orders o LEFT JOIN order_details d ON d.order_id=o.order_id WHERE o.order_invoice=? AND o.customer_id=?',(invoice_number,customer_id)).fetchone()
            if not order: raise BillingError('Issued invoice not found for this customer.')
            original=json.loads(order['customer_snapshot']) if order['customer_snapshot'] else dict(conn.execute('SELECT * FROM customers WHERE customer_id=?',(customer_id,)).fetchone())
            last=conn.execute('SELECT contact_json FROM invoice_revisions WHERE order_id=? ORDER BY id DESC LIMIT 1',(order['order_id'],)).fetchone()
            contact=json.loads(last[0]) if last else dict(original)
            for key,value,maximum in (('billing_address',address,2000),('company',company,200),('tax_registration_number',tax_id,100)):
                if value is not None:
                    if not isinstance(value,str) or len(value)>maximum or not value.strip(): raise BillingError('Corrected fields must contain valid nonempty text.')
                    contact[key]=value.strip()
            if contact!= (json.loads(last[0]) if last else original):
                conn.execute('INSERT INTO invoice_revisions(order_id,contact_json) VALUES (?,?)',(order['order_id'],json.dumps(contact)))
            return generate_invoice(identifier,customer_id,connection=conn)
    return generate_invoice(identifier,customer_id)


def fulfillment_transition(conn,order,action):
    if not is_placed(conn,order): raise BillingError('Only issued orders can be shipped or delivered.')
    if order['refund_status'] or order['refund_amount'] is not None: raise BillingError('Refunded orders require staff review and cannot be fulfilled again.')
    oid=order['order_id']; row=conn.execute('SELECT fulfillment_status FROM order_service WHERE order_id=?',(oid,)).fetchone()
    state=row[0] if row else 'unknown'
    if state=='unknown': raise BillingError('Shipment status is unknown. Staff must verify the recorded state before advancing fulfillment.')
    target='shipped' if action=='ship' else 'delivered'
    if state==target: return {'order_id':oid,'order_status':order['order_status'],'fulfillment_status':target,'already_recorded':True}
    if (state,target) not in (('placed','shipped'),('shipped','delivered')): raise BillingError('Invalid fulfillment transition; delivery requires shipment and cancelled orders cannot ship.')
    if conn.execute("SELECT 1 FROM refund_requests WHERE order_id=? AND status='pending'",(oid,)).fetchone(): raise BillingError('Pending refund must be reviewed before fulfillment.')
    conn.execute('INSERT OR IGNORE INTO order_service(order_id) VALUES (?)',(oid,))
    column='shipped_at' if action=='ship' else 'delivered_at'
    conn.execute(f'UPDATE order_service SET fulfillment_status=?,{column}=CURRENT_TIMESTAMP WHERE order_id=?',(target,oid))
    return {'order_id':oid,'order_status':order['order_status'],'fulfillment_status':target}


def bulk_refunds(customer_id,date_from,date_to,action,reason,confirmed,disposition):
    from mcp_server.tools.billing_tools import check_return_window,process_refund
    if not date_from or not date_to: raise BillingError('Bulk return requires an explicit inclusive date range.')
    try:
        if date.fromisoformat(date_from).isoformat()!=date_from or date.fromisoformat(date_to).isoformat()!=date_to or date_from>date_to: raise ValueError()
    except (ValueError,TypeError): raise BillingError('Use a valid inclusive YYYY-MM-DD range.')
    if disposition not in ('original_method','store_credit'): raise BillingError('Choose original_method or store_credit.')
    if action=='bulk_request' and (confirmed is not True or not reason.strip() or len(reason)>2000): raise BillingError('Confirm the reviewed bulk return and supply a reason.')
    with closing(connect(readonly=True)) as conn:
        orders=conn.execute('SELECT * FROM orders WHERE customer_id=? AND order_date BETWEEN ? AND ? ORDER BY order_date,order_id',(customer_id,date_from,date_to)).fetchall()
        rows=[]
        for row in orders:
            record={'order_id':row['order_id'],'eligible':False}
            try:
                order_row(conn,row['order_id'],customer_id)
                if not is_placed(conn,row): raise BillingError('Draft is not an issued order.')
                check_return_window(conn,row)
                if row['refund_status'] or row['refund_amount'] is not None: raise BillingError('Already refunded or recorded refund status needs staff review.')
                if conn.execute('SELECT 1 FROM refund_requests WHERE order_id=?',(row['order_id'],)).fetchone(): raise BillingError('Refund request already exists.')
                if disposition=='original_method' and conn.execute("SELECT 1 FROM store_credit_ledger WHERE order_id=? AND kind='order_redemption'",(row['order_id'],)).fetchone(): raise BillingError('Order used credit; choose store_credit.')
                items=[{'item_code':r['item_code'],'quantity':r['qty']} for r in conn.execute('SELECT item_code,SUM(qty_billed) qty FROM invoice_lines WHERE order_id=? GROUP BY item_code',(row['order_id'],))]
                value,_=return_quote(conn,row,items)
                from config import settings
                currency=currency_for(conn,row['order_id'])
                approver='admin' if currency!=settings.CASHIER_REFUND_CURRENCY or value>money(settings.CASHIER_REFUND_LIMIT) else 'cashier_or_admin'
                record.update(eligible=True,amount=str(value),currency=currency,required_approver=approver)
            except BillingError as exc: record['reason']=str(exc)
            rows.append(record)
    submitted=0
    if action=='bulk_request':
        for record in rows:
            if record['eligible']:
                result=process_refund(record['order_id'],customer_id,reason,action='request',disposition=disposition)
                record['submitted']=bool(result.get('ok'));submitted+=int(record['submitted'])
                if not result.get('ok'): record['reason']=result.get('error')
    return {'action':action,'orders':rows,'matched_count':len(rows),'eligible_count':sum(r['eligible'] for r in rows),
            'submitted_count':submitted,'note':'Requests require staff approval. No refunds were approved or sent.'}
