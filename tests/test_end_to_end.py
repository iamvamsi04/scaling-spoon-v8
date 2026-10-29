"""Cross-layer flows with real API, MCP, SQLite and UI; only model replies are scripted."""
import json
from contextlib import closing
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient
from streamlit.testing.v1 import AppTest

from agent import billing_agent as module
from api.main import app
from config import settings
from database.connection import connect


def call(tool_name, **args):
    return CreateResult(finish_reason='function_calls', content=[FunctionCall(
        id=str(uuid4()), name=tool_name, arguments=json.dumps(args))],
        usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)


def reply(text, **update):
    return json.dumps({'reply':text, 'shopping_update':update})


def propose(action, order_id, summary, **arguments):
    proposal = {'action':action, 'order_id':order_id, 'summary':summary}
    if arguments:
        proposal['arguments'] = arguments
    return reply(summary, pending_confirmation=proposal)


def setup_engine(monkeypatch, script):
    monkeypatch.setattr(settings,'azure_configuration_error',lambda:None)
    monkeypatch.setattr(settings,'API_KEY','')
    replay = ReplayChatCompletionClient(script)
    monkeypatch.setattr(module,'create_model_client',lambda:replay)
    original_call = module.RecordingWorkbench.call_tool
    async def checked_call(self, name, *args, **kwargs):
        result = await original_call(self, name, *args, **kwargs)
        decoded = module.decode_result(result)
        if name != 'download_invoice':
            assert decoded.get('ok'), (name, decoded)
        return result
    monkeypatch.setattr(module.RecordingWorkbench,'call_tool',checked_call)
    app.state.engine = module.BillingAgent()


def test_complete_customer_journey_then_admin_reports(monkeypatch):
    setup_engine(monkeypatch,[
        call('execute_sql_query',query="SELECT COALESCE(NULLIF(TRIM(d.category),''),'Uncategorised') AS category, COUNT(*) AS products, SUM(CASE WHEN i.availablequantity>0 THEN 1 ELSE 0 END) AS in_stock_products FROM items i LEFT JOIN product_details d ON d.item_code=i.item_code GROUP BY COALESCE(NULLIF(TRIM(d.category),''),'Uncategorised') ORDER BY category"),
        reply('The store has 3 products across 1 category. All 3 products are currently in stock.'),
        call('execute_sql_query',query='SELECT COUNT(*) AS products FROM items'),
        call('execute_sql_query',query='SELECT item_code,name FROM items ORDER BY name,item_code LIMIT ? OFFSET ?',parameters=[2,0]),reply('Showing products 1 through 2 of 3.'),
        call('execute_sql_query',query='SELECT item_code,name FROM items ORDER BY name,item_code LIMIT ? OFFSET ?',parameters=[2,2]),reply('Showing product 3 of 3.'),
        propose('update_customer',None,'Update your company to Demo Company, keeping the name Demo Shopper and other details unchanged?',name='Demo Shopper',company='Demo Company'),
        call('manage_customer',customer_id='E2E-CUSTOMER',name='Demo Shopper',company='Demo Company',action='update',confirmed=True),reply('Your account is updated.'),
        call('compare_products',products=['ITEM001','ITEM002']),
        call('find_cheaper_alternatives',product_reference='ITEM001'),reply('Here are your options.'),
        call('request_order',customer_id='E2E-CUSTOMER',order_id='E2E-ORDER',items=[{'item_code':'ITEM001','quantity':1}]),
        call('apply_promotion',customer_id='E2E-CUSTOMER',order_id='E2E-ORDER',promo_code='SAVE10'),propose('place_order','E2E-ORDER','One keyboard in E2E-ORDER costs USD 71.91. Shall I place it?'),
        propose('replace_items','E2E-ORDER','Replace the complete E2E-ORDER basket with two ITEM001 keyboards? The quoted total will be USD 143.82.',items=[{'item_code':'ITEM001','quantity':2}]),
        call('manage_draft',customer_id='E2E-CUSTOMER',order_id='E2E-ORDER',action='replace_items',items=[{'item_code':'ITEM001','quantity':2}],confirmed=True),propose('place_order','E2E-ORDER','Two ITEM001 keyboards in E2E-ORDER cost USD 143.82. Shall I place the order?'),
        call('place_order',customer_id='E2E-CUSTOMER',order_id='E2E-ORDER'),reply('Your order is placed.'),
        propose('process_refund','E2E-ORDER','Request a USD 10.00 refund for E2E-ORDER, reason Return adjustment, to the original method? This creates a pending customer request only.',amount='10.00',reason='Return adjustment'),
        call('process_refund',customer_id='E2E-CUSTOMER',order_id='E2E-ORDER',amount='10.00',reason='Return adjustment'),reply('Your refund request for USD 10.00 is pending staff approval.'),
        call('request_order',customer_id='E2E-CUSTOMER',order_id='E2E-CANCEL',items=[{'item_code':'ITEM002','quantity':1}]),
        propose('cancel_draft','E2E-CANCEL','Cancel the unplaced E2E-CANCEL draft containing one ITEM002 mouse, reason Changed my mind? No invoice or stock reservation exists.',reason='Changed my mind'),
        call('manage_draft',customer_id='E2E-CUSTOMER',order_id='E2E-CANCEL',action='cancel',reason='Changed my mind',confirmed=True),reply('The second draft is cancelled.'),
        call('execute_sql_query',query="SELECT o.order_id, o.charged_price, d.currency FROM orders o JOIN order_details d ON d.order_id=o.order_id WHERE o.order_invoice IS NOT NULL",chart={'type':'bar','x':'order_id','y':['charged_price'],'title':'Invoice totals'}),
        call('billing_analysis'),call('billing_anomalies'), 'One invoice is recorded.'
    ])
    monkeypatch.setattr(settings,'ADMIN_API_KEY','e2e-admin-key-123456789')
    with TestClient(app) as client:
        cid=client.post('/conversations',json={}).json()['conversation_id']
        def chat(message, conversation=None, headers=None):
            body={'conversation_id':conversation or cid,'request_id':str(uuid4()),'message':message}
            response=client.post('/chat',json=body,headers=headers)
            assert response.status_code==200, response.text
            assert client.post('/chat',json=body,headers=headers).json()==response.json()
            return response.json()
        overview=chat('what all products do u have?')
        assert overview['response'].startswith('The store has 3 products across 1 category.')
        assert overview['tables'][0]['rows'] == [{'Category':'Uncategorised','Products':3,'In stock products':3}]
        first=chat('list products two per page')
        assert first['response']=='Showing products 1 through 2 of 3.'
        assert first['shopping_state']['product_query']['parameters']==[2,0]
        assert chat('next page')['shopping_state']['product_query']['parameters']==[2,2]
        account=client.post('/customers/register',json={'customer_id':'E2E-CUSTOMER','name':'Demo Shopper','password':'Example-pass-1234'}).json()
        client.headers['X-Customer-Token']=account['token']
        cid=client.post('/conversations',json={'customer_id':'E2E-CUSTOMER'}).json()['conversation_id']
        chat('Update my company to Demo Company.')
        chat('Yes.')
        assert client.get('/conversations/'+cid).json()['customer_id']=='E2E-CUSTOMER'
        chat('Compare the keyboard and mouse and find cheaper alternatives.')
        chat('Quote a keyboard with SAVE10.')
        chat('Change the quote to two keyboards.')
        chat('Yes.')
        chat('Yes.')
        with closing(connect()) as conn:
            invoice=conn.execute("SELECT order_invoice FROM orders WHERE order_id='E2E-ORDER'").fetchone()[0]
            assert conn.execute("SELECT availablequantity FROM items WHERE item_code='ITEM001'").fetchone()[0]==38
        pdf=client.get('/invoices/'+invoice,params={'conversation_id':cid})
        assert pdf.status_code==200 and pdf.content.startswith(b'%PDF')
        from api.customer_auth import set_password,login
        set_password('CUST002','Other-example-pass-1234')
        other_headers={'X-Customer-Token':login('CUST002','Other-example-pass-1234')['token']}
        other=client.post('/conversations',json={'customer_id':'CUST002'},headers=other_headers).json()['conversation_id']
        assert client.get('/invoices/'+invoice,params={'conversation_id':other},headers=other_headers).status_code==404
        chat('Request a USD 10 return adjustment for E2E-ORDER.')
        chat('Yes.')
        chat('Create a mouse draft and prepare to cancel it.')
        chat('Yes.')
        headers={'X-Admin-Key':settings.ADMIN_API_KEY}
        admin=client.post('/conversations',json={'mode':'admin'},headers=headers).json()['conversation_id']
        report=chat('Show invoice totals as a chart and check anomalies.',admin,headers)
        assert len(report['charts'])==1
        assert report['charts'][0]['data'][0]['charged_price']==143.82
        assert client.get('/conversations/'+admin).status_code==403
        with closing(connect()) as conn:
            assert conn.execute("SELECT refund_amount FROM orders WHERE order_id='E2E-ORDER'").fetchone()[0] is None
            assert conn.execute("SELECT status FROM order_details WHERE order_id='E2E-CANCEL'").fetchone()[0]=='cancelled'
            assert conn.execute('SELECT COUNT(*) FROM billing_audit').fetchone()[0]==8
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_streamlit_to_api_to_real_mcp_order(monkeypatch):
    setup_engine(monkeypatch,[
        call('request_order',customer_id='CUST001',order_id='UI-ORDER',items=[{'item_code':'ITEM002','quantity':1}]),propose('place_order','UI-ORDER','One ITEM002 mouse in UI-ORDER costs USD 29.50. Place this order?'),
        call('place_order',customer_id='CUST001',order_id='UI-ORDER'),reply('Your mouse order is placed.')])
    original=httpx.Client.request
    with TestClient(app) as client:
        from api.customer_auth import set_password
        set_password('CUST001','UI-example-pass-1234')
        def bridge(self,method,url,**kwargs):
            if self is client:
                return original(self,method,url,**kwargs)
            return client.request(method,url,**kwargs)
        monkeypatch.setattr(httpx.Client,'request',bridge)
        ui=AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'),default_timeout=60).run()
        next(x for x in ui.text_input if x.label=='Customer ID').set_value('CUST001')
        next(x for x in ui.text_input if x.label=='Password').set_value('UI-example-pass-1234')
        next(x for x in ui.button if x.label=='Sign in').click().run()
        assert not ui.exception and ui.session_state['customer_id']=='CUST001'
        ui.chat_input[0].set_value('Quote one mouse.').run()
        assert not ui.exception
        assert any('USD 29.50' in x.value for x in ui.text)
        ui.chat_input[0].set_value('Yes, place that mouse order.').run()
        assert not ui.exception
        assert any(x.value=='Your mouse order is placed.' for x in ui.text)
        assert ui.session_state['pending'] is None
        with closing(connect()) as conn:
            assert conn.execute("SELECT availablequantity FROM items WHERE item_code='ITEM002'").fetchone()[0]==59
            assert conn.execute("SELECT COUNT(*) FROM invoice_lines WHERE order_id='UI-ORDER'").fetchone()[0]==1


def test_real_http_server_startup_configuration_failure_and_shutdown(monkeypatch):
    import socket
    import threading
    import time
    import uvicorn
    monkeypatch.setattr(settings,'API_KEY','runtime-test-key')
    monkeypatch.setattr(settings,'azure_configuration_error',lambda:'Azure configuration is missing.')
    engine=module.BillingAgent()
    app.state.engine=engine
    listener=socket.socket()
    listener.bind(('127.0.0.1',0))
    listener.listen(128)
    port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True)
    thread.start()
    try:
        deadline=time.monotonic()+40
        while not server.started and thread.is_alive() and time.monotonic()<deadline:
            time.sleep(0.05)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=10) as client:
            assert client.get('/health').status_code==401
            client.headers['X-API-Key']='runtime-test-key'
            health=client.get('/health')
            assert health.status_code==200 and health.json()['model_configured'] is False
            from api.customer_auth import set_password,login
            set_password('CUST001','HTTP-example-pass-1234')
            client.headers['X-Customer-Token']=login('CUST001','HTTP-example-pass-1234')['token']
            cid=client.post('/conversations',json={'customer_id':'CUST001'}).json()['conversation_id']
            body={'conversation_id':cid,'request_id':str(uuid4()),'message':'Hello'}
            assert client.post('/chat',json=body).status_code==503
            assert client.post('/chat',json=body).status_code==503
            assert client.get('/conversations/'+cid).json()['messages']==[]
    finally:
        server.should_exit=True
        thread.join(20)
        listener.close()
    assert not thread.is_alive()
    assert engine._workbench is None and engine._client is None
