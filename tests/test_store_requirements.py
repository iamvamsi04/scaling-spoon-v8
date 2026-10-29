"""The 27 requested service scenarios plus financial and access boundaries."""
from address_support import place_order_with_address, prepare_delivery
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import pytest
from config import settings
from database.connection import connect,transaction
from database.invoice import invoice_path
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import request_order,place_order,apply_promotion,process_refund,download_invoice
from mcp_server.tools.operations_tools import manage_draft,manage_customer
from mcp_server.tools.analytics_tools import billing_analysis
from mcp_server.tools.database import execute_sql_query
from agent.prompts.store_policy import STORE_POLICY

refund=scoped_tool(process_refund)
customer_read=scoped_tool(execute_sql_query)


def order(qty=2,code='ITEM001',placed=True):
    oid=request_order('CUST001',[{'item_code':code,'quantity':qty}])['order_id']
    prepare_delivery(oid, 'CUST001')
    if placed: assert place_order_with_address(oid,'CUST001')['ok']
    return oid


def request(oid,**kwargs):
    return refund(oid,'CUST001','Return',execution_customer_id='CUST001',**kwargs)


def approve(oid,role='admin',**kwargs):
    return refund(oid,'CUST001','Return',action='approve',confirmed=True,execution_mode=role,**kwargs)


def scalar(sql,args=()):
    with closing(connect()) as conn: return conn.execute(sql,args).fetchone()[0]


@pytest.mark.parametrize('case',range(1,28))
def test_required_service_scenarios(case,monkeypatch):
    if case in (12,13,21,23):
        expected={12:['Closed cards','original issuer'],13:['Cash-on-delivery','Never request bank'],21:['captured-not-invoiced','missing capture evidence'],23:['original_method','another card']}[case]
        for phrase in expected: assert phrase in STORE_POLICY
        assert not scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('payments','captures','settlements','credit_notes')")
        return
    oid=order()
    if case==1:
        with transaction() as conn: conn.execute('UPDATE order_requests SET quantity=3 WHERE order_id=?',(oid,))
        result=customer_read('SELECT (SELECT SUM(quantity) FROM order_requests WHERE order_id=?) requested,(SELECT SUM(qty_billed) FROM invoice_lines WHERE order_id=?) billed',parameters=[oid,oid],execution_customer_id='CUST001')
        assert result['rows']==[[3,2]]
        assert 'not proof of an actual charge' in STORE_POLICY
    elif case==2:
        assert manage_customer('CUST001','Alex Morgan','Demo Co','alex@example.com',action='update',confirmed=True,billing_address='10 Demo Road',tax_registration_number='TAX-123')['ok']
        new=order(); inv=scalar('SELECT order_invoice FROM orders WHERE order_id=?',(new,))
        assert download_invoice(inv,'CUST001',document_type='tax')['ready']
        assert invoice_path('TAX-'+inv,'CUST001').read_bytes().startswith(b'%PDF')
    elif case==3:
        inv=scalar('SELECT order_invoice FROM orders WHERE order_id=?',(oid,))
        before=scalar('SELECT customer_snapshot FROM order_details WHERE order_id=?',(oid,))
        assert not download_invoice(inv,'CUST001',action='correct_details',billing_address='Corrected')['ok']
        assert download_invoice(inv,'CUST001',action='correct_details',billing_address='Corrected',confirmed=True)['ready']
        assert scalar('SELECT customer_snapshot FROM order_details WHERE order_id=?',(oid,))==before
        assert json.loads(scalar('SELECT contact_json FROM invoice_revisions'))['billing_address']=='Corrected'
    elif case==4:
        draft=order(placed=False)
        result=download_invoice('','CUST001',document_type='proforma',order_id=draft)
        assert result['ready'] and result['invoice_number']=='PROFORMA-'+draft
        assert scalar('SELECT order_invoice FROM orders WHERE order_id=?',(draft,)) is None
    elif case==5:
        first=manage_draft(oid,'CUST001','cancel',reason='Changed mind',confirmed=True)
        assert first['ok'] and first['refund_status']=='pending_approval'
        stock=scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'")
        assert manage_draft(oid,'CUST001','cancel',reason='Changed mind',confirmed=True)['already_cancelled']
        assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'")==stock
        assert not place_order_with_address(oid,'CUST001')['ok']
        result=refund(oid,'CUST001','Changed mind',action='approve',confirmed=True,execution_mode='admin')
        assert result['ok']
    elif case in (6,7):
        assert not manage_draft(oid,'CUST001','replace_items',items=[{'item_code':'ITEM001','quantity':1 if case==6 else 3}],confirmed=True)['ok']
    elif case in (8,9,18):
        assert request(oid,amount='10.00')['ok']
        if case==8:
            assert approve(oid,return_received=True)['ok']
            result=refund(oid,'CUST001',action='status',execution_customer_id='CUST001')
            assert result['local_status']=='approved'
        else:
            result=refund(oid,'CUST001','No refund received for ten days',action='escalate',confirmed=True,execution_customer_id='CUST001')
            assert result['ok'] and result['review_reference']=='REVIEW-'+oid
            assert result['recorded_amount'] is None and 'Not available' in result['bank_status']
    elif case==10:
        result=request(oid,items=[{'item_code':'ITEM001','quantity':1}])
        assert result['requested_amount']=='79.90' and scalar('SELECT charged_price FROM orders')==159.80
    elif case==11:
        draft=order(qty=3,placed=False)
        with transaction() as conn:
            conn.execute("INSERT INTO promotions(promo_code,discount_type,discount_value,expires_on) VALUES ('B2G','flat',79.90,date('now','+1 day'))")
        assert apply_promotion(draft,'CUST001','B2G')['ok'];assert place_order_with_address(draft,'CUST001')['ok']
        assert request(draft,items=[{'item_code':'ITEM001','quantity':1}])['requested_amount']=='53.27'
    elif case==14:
        assert request(oid,items=[{'item_code':'ITEM001','quantity':1}])['ok']
        assert not approve(oid,role='cashier')['ok']
        assert not approve(oid,role='cashier',waiver_reason='Damage assessed; return waived')['ok']
        assert approve(oid,waiver_reason='Damage assessed; physical return waived')['ok']
    elif case==15:
        result=manage_draft(oid,'CUST001','report_billing_issue',reason='Customer reports duplicate charge',confirmed=True)
        assert result['ok'] and result['review_reference']
        assert 'duplicate charge' in scalar('SELECT review_note FROM order_service')
        assert scalar('SELECT COUNT(*) FROM orders')==1
    elif case==16:
        draft=order(qty=1,placed=False)
        assert request(oid,items=[{'item_code':'ITEM001','quantity':1}],disposition='store_credit',target_order_id=draft)['ok']
        assert scalar('SELECT COUNT(*) FROM store_credit_ledger')==0
        assert approve(oid,return_received=True)['ok']
        assert scalar('SELECT credit_requested FROM order_service WHERE order_id=?',(draft,))==79.90
        result=place_order_with_address(draft,'CUST001');assert result['ok'] and result['remaining_invoice_amount']=='0.00'
        assert place_order_with_address(draft,'CUST001')['already_placed']
        assert scalar("SELECT COUNT(*) FROM store_credit_ledger WHERE kind='order_redemption'")==1
    elif case==17:
        with transaction() as conn: conn.execute("UPDATE items SET sellingprice=89.90 WHERE item_code='ITEM001'")
        second=order()
        result=customer_read('SELECT o.order_id,SUM(l.line_total) merchandise FROM orders o JOIN invoice_lines l ON l.order_id=o.order_id GROUP BY o.order_id',execution_customer_id='CUST001')
        values={row[0]:Decimal(str(row[1])) for row in result['rows']}
        assert values[second]-values[oid]==Decimal('20.00')
    elif case==19:
        assert request(oid,amount='10.00')['ok'];assert approve(oid,return_received=True)['ok']
        with transaction() as conn:
            conn.execute("UPDATE refund_requests SET approved_at=datetime('now','-1 day')")
            conn.execute('UPDATE orders SET refund_amount=9 WHERE order_id=?',(oid,))
        yesterday=scalar("SELECT date('now','-1 day')")
        result=billing_analysis(report='refund_reconciliation',date_from=yesterday,date_to=yesterday)
        assert result['approved_count']==1 and result['count']==1
    elif case==20:
        assert request(oid,amount='159.80')['ok']
        result=billing_analysis(report='pending_refunds')
        assert result['requests'][0]['required_approver']=='admin'
    elif case in (22,27):
        result=customer_read("SELECT email FROM customers WHERE email='alex@example.com'",execution_customer_id='CUST002')
        assert result['rows']==[]
        inv=scalar('SELECT order_invoice FROM orders WHERE order_id=?',(oid,))
        assert not scoped_tool(download_invoice)(inv,'CUST001',execution_customer_id='CUST002')['ok']
        assert 'spouse' in STORE_POLICY.lower()
    elif case==24:
        assert not approve(oid)['ok']
        assert request(oid,items=[{'item_code':'ITEM001','quantity':1}])['ok']
        assert not refund(oid,'CUST001','Return',action='approve',confirmed=True,execution_customer_id='CUST001')['ok']
        assert not approve(oid)['ok']
    elif case==25:
        expired=order()
        with transaction() as conn: conn.execute("UPDATE orders SET order_date=date('now','-31 days') WHERE order_id=?",(expired,))
        assert request(oid,amount='10.00')['ok']
        assert not request(expired,amount='10.00')['ok']
        assert scalar('SELECT COUNT(*) FROM refund_requests')==1 and scalar('SELECT COUNT(*) FROM orders WHERE refund_amount IS NOT NULL')==0
    elif case==26:
        assert request(oid,amount='10.00')['ok'];assert approve(oid,return_received=True)['ok']
        assert not approve(oid,return_received=True)['ok']
        assert scalar('SELECT refund_amount FROM orders')==10


def test_credit_cannot_be_double_spent_across_drafts():
    source=order();assert request(source,amount='79.90',disposition='store_credit')['ok'];assert approve(source,return_received=True)['ok']
    drafts=[order(qty=1,placed=False),order(qty=1,placed=False)]
    assert manage_draft(drafts[0],'CUST001','apply_store_credit',credit_amount='79.90',confirmed=True)['ok']
    assert not manage_draft(drafts[1],'CUST001','apply_store_credit',credit_amount='79.90',confirmed=True)['ok']
    with ThreadPoolExecutor(2) as pool: results=list(pool.map(lambda oid:place_order_with_address(oid,'CUST001'),drafts))
    assert all(r['ok'] for r in results)
    assert sorted(Decimal(r['store_credit_applied']) for r in results)==[Decimal('0.00'),Decimal('79.90')]
    assert scalar('SELECT SUM(amount) FROM store_credit_ledger')==0


def test_fulfillment_transitions_and_shipped_cancellation():
    oid=order(); tool=scoped_tool(manage_draft)
    assert not tool(oid,'CUST001','ship',confirmed=True,execution_customer_id='CUST001')['ok']
    assert not tool(oid,'CUST001','deliver',confirmed=True,execution_mode='admin')['ok']
    assert tool(oid,'CUST001','ship',confirmed=True,execution_mode='admin')['ok']
    assert not manage_draft(oid,'CUST001','cancel',reason='Changed mind',confirmed=True)['ok']
    assert tool(oid,'CUST001','deliver',confirmed=True,execution_mode='admin')['ok']


def test_store_credit_target_owner_and_currency():
    source=order();draft=order(placed=False)
    with transaction() as conn: conn.execute("UPDATE order_details SET currency='EUR' WHERE order_id=?",(draft,))
    assert not request(source,amount='10.00',disposition='store_credit',target_order_id=draft)['ok']
    assert not request(source,amount='10.00',disposition='another_card')['ok']
    assert scalar('SELECT COUNT(*) FROM store_credit_ledger')==0


def test_policy_is_loaded_into_the_agent(monkeypatch):
    import asyncio
    from autogen_ext.models.replay import ReplayChatCompletionClient
    from agent import billing_agent as module
    from agent.prompts.runtime_policy import SERVICE
    replay = ReplayChatCompletionClient(['Hello.'])
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            await engine.chat('Hello', [], None)
            policy = replay.create_calls[0]['messages'][0].content
            assert SERVICE in policy
            for rule in ('pending customer request', 'exact stored amount/reason',
                         'Closed-card routing', 'bulk_preview', 'Tax document requires',
                         'no top-ups', 'not a transfer/restock', 'disposition=store_credit'):
                assert rule.casefold() in policy.casefold()
        finally:
            await engine.close()

    asyncio.run(run())


def test_bulk_requests_check_every_order_and_never_approve():
    eligible=order();expired=order()
    with transaction() as conn: conn.execute("UPDATE orders SET order_date=date('now','-31 days') WHERE order_id=?",(expired,))
    start=scalar("SELECT date('now','-40 days')");end=scalar('SELECT CURRENT_DATE')
    args=dict(order_id='',customer_id='CUST001',execution_customer_id='CUST001',date_from=start,date_to=end)
    preview=refund(action='bulk_preview',**args)
    assert preview['matched_count']==2 and preview['eligible_count']==1 and preview['submitted_count']==0
    assert not refund(action='bulk_request',reason='Confirmed monthly returns',**args)['ok']
    result=refund(action='bulk_request',reason='Confirmed monthly returns',confirmed=True,**args)
    assert result['submitted_count']==1 and scalar('SELECT COUNT(*) FROM refund_requests')==1
    assert scalar('SELECT refund_amount FROM orders WHERE order_id=?',(eligible,)) is None
    assert refund(action='bulk_request',reason='Confirmed monthly returns',confirmed=True,**args)['submitted_count']==0


def test_new_invoice_pdf_content_and_revision_isolation(monkeypatch):
    import reportlab.platypus as pdf
    original=pdf.Paragraph;seen=[]
    def capture(value,*args,**kwargs): seen.append(str(value));return original(value,*args,**kwargs)
    monkeypatch.setattr(pdf,'Paragraph',capture)
    assert manage_customer('CUST001','Alex Morgan','Example Company','alex@example.com',action='update',confirmed=True,billing_address='Original Road',tax_registration_number='TAX-789')['ok']
    draft=order(placed=False)
    assert download_invoice('','CUST001',document_type='proforma',order_id=draft)['ready']
    assert 'PRO FORMA INVOICE' in seen and 'Original Road' in seen
    placed=place_order_with_address(draft,'CUST001');inv=placed['invoice_number']
    assert download_invoice(inv,'CUST001',document_type='tax')['ready']
    assert 'TAX INVOICE' in seen and 'Tax registration: TAX-789' in seen
    seen.clear()
    assert download_invoice(inv,'CUST001',action='correct_details',billing_address='Corrected Road',confirmed=True)['ready']
    assert 'Corrected Road' in seen and 'Original Road' not in seen
    assert 'Original Road' in scalar('SELECT customer_snapshot FROM order_details WHERE order_id=?',(draft,))
    assert download_invoice(inv,'CUST001',action='correct_details',billing_address='Corrected Road',confirmed=True)['ready']
    assert scalar('SELECT COUNT(*) FROM invoice_revisions')==1


def test_api_agent_mcp_proforma_return_credit_and_checkout(monkeypatch):
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from api.main import app
    from test_end_to_end import setup_engine,call,propose,reply
    source=order();target=order(qty=1,placed=False)
    refund_summary=(f'Request a USD 79.90 refund for one ITEM001 keyboard from {source}, '
                    f'reason Return, as local store credit linked to draft {target}? '
                    'This creates a pending request only.')
    checkout_summary=(f'Place draft {target} containing one ITEM001 keyboard for USD 79.90, '
                      'using USD 79.90 local store credit and leaving USD 0.00 of the invoice amount?')
    monkeypatch.setattr(settings,'ADMIN_API_KEY','service-admin-key-12345678')
    setup_engine(monkeypatch,[
        call('download_invoice',invoice_number='',customer_id='CUST001',document_type='proforma',order_id=target),'Your pro forma estimate is ready.',
        call('process_refund',order_id=source,customer_id='CUST001',reason='Return',action='quote',items=[{'item_code':'ITEM001','quantity':1}],disposition='store_credit',target_order_id=target),
        propose('process_refund',source,refund_summary,reason='Return',amount='79.90',items=[{'item_code':'ITEM001','quantity':1}],disposition='store_credit',target_order_id=target),
        call('process_refund',order_id=source,customer_id='CUST001',reason='Return',amount='79.90',items=[{'item_code':'ITEM001','quantity':1}],disposition='store_credit',target_order_id=target),'Your store credit return request is pending staff approval.',
        call('process_refund',order_id=source,customer_id='CUST001',reason='Return',action='approve',confirmed=True,return_received=True),'The received return is approved as USD 79.90 local store credit.',
        call('execute_sql_query',query='SELECT o.order_id,o.charged_price,s.credit_requested FROM orders o LEFT JOIN order_service s ON s.order_id=o.order_id WHERE o.order_id=?',parameters=[target]),
        reply(checkout_summary,active_draft_id=target,pending_confirmation={'action':'place_order','order_id':target,'summary':checkout_summary}),
        call('place_order',order_id=target,customer_id='CUST001'),'Your order is placed with USD 79.90 store credit.'])
    with TestClient(app) as client:
        from api.customer_auth import set_password
        password='Store service test password 2026'
        set_password('CUST001',password)
        login=client.post('/customers/login',json={'customer_id':'CUST001','password':password})
        assert login.status_code==200,login.text
        customer_headers={'X-Customer-Token':login.json()['token']}
        customer=client.post('/conversations',json={'customer_id':'CUST001'},headers=customer_headers).json()['conversation_id']
        def chat(cid,text,headers=None):
            result=client.post('/chat',json={'conversation_id':cid,'request_id':str(uuid4()),'message':text},headers=customer_headers if headers is None else headers)
            assert result.status_code==200,result.text
            return result.json()
        artifact=chat(customer,'Prepare a pro forma for my draft.')['invoices'][0]
        pdf=client.get('/invoices/'+artifact['invoice_number'],params={'conversation_id':customer},headers=customer_headers)
        assert pdf.status_code==200 and pdf.content.startswith(b'%PDF')
        chat(customer,'Return one keyboard as store credit for this cart.')
        assert scalar('SELECT COUNT(*) FROM refund_requests')==0
        chat(customer,'Yes.')
        assert scalar('SELECT COUNT(*) FROM store_credit_ledger')==0
        headers={'X-Admin-Key':settings.ADMIN_API_KEY}
        admin=client.post('/conversations',json={'mode':'admin'},headers=headers).json()['conversation_id']
        chat(admin,f'Approve the existing refund request for {source} as store credit. The returned keyboard is received.',headers)
        assert scalar('SELECT COUNT(*) FROM store_credit_ledger')==1
        assert Decimal(str(scalar('SELECT credit_requested FROM order_service WHERE order_id=?',(target,))))==Decimal('79.90')
        chat(customer,'Show my current draft and prepare it for checkout using the approved store credit.')
        chat(customer,'Yes, place the cart using that credit.')
        assert scalar('SELECT SUM(amount) FROM store_credit_ledger')==0
        assert scalar('SELECT COUNT(*) FROM orders WHERE order_invoice IS NOT NULL')==2


def test_partial_shipment_is_distinguished_from_billing_mismatch():
    from mcp_server.tools.analytics_tools import billing_anomalies
    oid=order()
    with transaction() as conn:
        conn.execute('UPDATE order_requests SET quantity=3 WHERE order_id=?',(oid,))
        conn.execute("UPDATE order_service SET fulfillment_status='shipped' WHERE order_id=?",(oid,))
    report=billing_anomalies(order_id=oid)
    assert any(f['rule']=='PARTIAL_FULFILLMENT' and f['severity']=='warning' for f in report['findings'])
    assert not any(f['rule']=='QUANTITY_MISMATCH' for f in report['findings'])
    with transaction() as conn: conn.execute('UPDATE invoice_lines SET qty_billed=3 WHERE order_id=?',(oid,))
    assert any(f['rule']=='QUANTITY_MISMATCH' and f['severity']=='error' for f in billing_anomalies(order_id=oid)['findings'])


def test_refund_quote_does_not_create_a_request():
    oid=order()
    result=refund(oid,'CUST001',action='quote',items=[{'item_code':'ITEM001','quantity':1}],execution_customer_id='CUST001')
    assert result['ok'] and result['quoted_amount']=='79.90'
    assert scalar('SELECT COUNT(*) FROM refund_requests')==0
    assert scalar("SELECT COUNT(*) FROM billing_audit WHERE action='process_refund'")==0
