from address_support import place_order_with_address, prepare_delivery
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from config import settings
from api.main import app
from database.connection import connect, transaction, initialize
from mcp_server.access import scoped_tool, CASHIER_TOOLS
from mcp_server.tools.billing_tools import request_order, place_order, process_refund
from mcp_server.tools.database import execute_sql_query

refund = scoped_tool(process_refund)
ADMIN = 'test-admin-secret-123456789'
CASHIER = 'test-cashier-secret-123456789'


def order():
    result = request_order('CUST001', [{'item_code':'ITEM001','quantity':2}])
    assert result['ok']
    assert place_order_with_address(result['order_id'], 'CUST001')['ok']
    return result['order_id']


def approve(oid, amount='10.00', role='cashier', **kwargs):
    return refund(oid,'CUST001','Return',amount,action='approve',confirmed=True,return_received=True,execution_mode=role,**kwargs)


@pytest.mark.parametrize('role,amount,allowed', [
    ('cashier','99.99',True),('cashier','100.00',True),('cashier','100.01',False),
    ('cashier','159.80',False),('admin','99.99',True),('admin','100.00',True),
    ('admin','100.01',True),('admin','159.80',True),('admin','159.81',False),
    ('customer','1.00',False)])
def test_approval_boundaries(role,amount,allowed,monkeypatch):
    monkeypatch.setattr(settings,'CASHIER_REFUND_LIMIT','100.00')
    oid=order()
    requested = refund(oid,'CUST001','Return',amount,execution_customer_id='CUST001')
    assert requested['ok'] is (amount != '159.81')
    result=approve(oid,amount,role,execution_customer_id='CUST001')
    assert result['ok'] is allowed, result
    with closing(connect()) as conn:
        saved=conn.execute('SELECT refund_amount FROM orders WHERE order_id=?',(oid,)).fetchone()[0]
        assert (saved is not None) is allowed
        assert conn.execute('SELECT COUNT(*) FROM refund_requests').fetchone()[0] == int(requested['ok'])
        if allowed:
            audit=json.loads(conn.execute("SELECT details_json FROM billing_audit WHERE action='process_refund' ORDER BY id DESC LIMIT 1").fetchone()[0])
            assert audit['execution_role']==role


def test_pending_handoff_and_cannot_reduce_amount():
    oid=order()
    args=dict(execution_customer_id='CUST001')
    result=refund(oid,'CUST001','Return','120.00',**args)
    assert result['refund_status']=='pending_approval'
    assert refund(oid,'CUST001','Return','120.00',**args)['ok'] # retry is idempotent
    assert not approve(oid,'100.00')['ok']
    assert not approve(oid,None)['ok']
    with closing(connect()) as conn:
        assert conn.execute('SELECT refund_amount FROM orders').fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM billing_audit WHERE action='process_refund'").fetchone()[0]==1
    result=approve(oid,None,'admin')
    assert result['refund_amount']=='120.00'
    assert not approve(oid,None,'admin')['ok']
    with closing(connect()) as conn:
        row=conn.execute('SELECT * FROM refund_requests').fetchone()
        assert row['status']=='approved' and row['approved_at'] and row['approved_by_role']=='admin'


@pytest.mark.parametrize('limit',['NaN','Infinity','-1','bad'])
def test_invalid_limit_fails_closed(limit,monkeypatch):
    monkeypatch.setattr(settings,'CASHIER_REFUND_LIMIT',limit)
    oid=order()
    assert refund(oid,'CUST001','Return','10.00',execution_customer_id='CUST001')['ok']
    assert not approve(oid)['ok']


def test_configurable_limit_and_currency(monkeypatch):
    oid=order()
    monkeypatch.setattr(settings,'CASHIER_REFUND_LIMIT','5.00')
    assert refund(oid,'CUST001','Return','5.01',execution_customer_id='CUST001')['ok']
    assert not approve(oid,'5.01')['ok']
    oid=order()
    with transaction() as conn:
        conn.execute("UPDATE order_details SET currency='EUR' WHERE order_id=?",(oid,))
    assert refund(oid,'CUST001','Return','5.00',execution_customer_id='CUST001')['ok']
    assert not approve(oid,'5.00')['ok']
    assert approve(oid,'5.00','admin')['ok']


def test_trusted_scope_confirmation_and_owner_required():
    oid=order()
    assert not process_refund(oid,'CUST001','Return','10',action='approve',confirmed=True,return_received=True)['ok']
    assert not refund(oid,'CUST001','Return','10',action='approve',execution_mode='cashier')['ok']
    assert not refund(oid,'CUST001','Return','10',execution_customer_id='CUST002')['ok']
    assert not refund(oid,'CUST002','Return','10',action='approve',confirmed=True,return_received=True,execution_mode='admin')['ok']
    assert not refund(oid,'CUST001','Return','10',action='invalid',execution_mode='admin')['ok']


def test_concurrent_approvals_commit_once():
    oid=order()
    assert refund(oid,'CUST001','Return','10.00',execution_customer_id='CUST001')['ok']
    with ThreadPoolExecutor(4) as pool:
        results=list(pool.map(lambda _:approve(oid),range(4)))
    assert sum(r['ok'] for r in results)==1
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM billing_audit WHERE action='process_refund'").fetchone()[0]==2


def test_customer_request_visibility_and_cashier_read_scope():
    oid=order()
    assert refund(oid,'CUST001','Return','10',execution_customer_id='CUST001')['ok']
    read=scoped_tool(execute_sql_query)
    assert read('SELECT count(*) FROM refund_requests',execution_customer_id='CUST002')['rows']==[[0]]
    assert read('SELECT count(*) FROM refund_requests',execution_customer_id='CUST001')['rows']==[[1]]
    assert read('SELECT count(*) FROM refund_requests',execution_mode='cashier')['rows']==[[1]]
    assert not read('SELECT baseprice FROM items',execution_mode='cashier')['ok']
    assert not read('SELECT * FROM billing_audit',execution_mode='cashier')['ok']
    assert not read('UPDATE orders SET refund_amount=10',execution_mode='cashier')['ok']
    initialize() # additive, repeatable migration retains pending state
    assert read('SELECT count(*) FROM refund_requests',execution_mode='cashier')['rows']==[[1]]


class Engine:
    async def chat(self,message,history,customer_id,mode='customer',**kwargs):
        return {'response':mode,'invoices':[],'charts':[]}


def test_cashier_api_authorization_all_routes(monkeypatch):
    monkeypatch.setattr(settings,'ADMIN_API_KEY',ADMIN)
    monkeypatch.setattr(settings,'CASHIER_API_KEY',CASHIER)
    app.state.engine=Engine()
    headers={'X-Cashier-Key':CASHIER}
    with TestClient(app) as client:
        assert client.get('/cashier/access').status_code==403
        assert client.get('/cashier/access',headers=headers).status_code==200
        assert client.get('/admin/access',headers={'X-Admin-Key':CASHIER}).status_code==403
        assert client.post('/conversations',json={'mode':'cashier'}).status_code==403
        cid=client.post('/conversations',json={'mode':'cashier'},headers=headers).json()['conversation_id']
        body={'conversation_id':cid,'request_id':str(uuid4()),'message':'Review refunds'}
        assert client.post('/chat',json=body,headers=headers).json()['response']=='cashier'
        assert client.post('/chat',json=body).status_code==403 # cached reply protected
        assert client.post('/chat/stream',json=body).status_code==403
        assert client.get('/conversations/'+cid).status_code==403
        assert client.get('/invoices/INV',params={'conversation_id':cid,'customer_id':'CUST001'}).status_code==403
        assert client.get('/conversations/'+cid,headers={'X-Admin-Key':ADMIN}).status_code==200
        monkeypatch.setattr(settings,'ADMIN_API_KEY',CASHIER)
        assert client.get('/admin/access',headers={'X-Admin-Key':CASHIER}).status_code==503
        assert client.get('/cashier/access',headers=headers).status_code==503


def test_real_mcp_cashier_schema_and_forged_admin_scope():
    from agent.billing_agent import RecordingWorkbench, decode_result
    oid=order()
    assert refund(oid,'CUST001','Return','100.01',execution_customer_id='CUST001')['ok']
    eligible=order()
    assert refund(eligible,'CUST001','Return','100.00',execution_customer_id='CUST001')['ok']
    async def run():
        async with RecordingWorkbench() as wb:
            wb.mode='cashier'
            assert {t['name'] for t in await wb.list_tools()}==CASHIER_TOOLS
            wb.user_message=f'Approve the pending refund for {oid}. The returned goods were received.'
            result=decode_result(await wb.call_tool('process_refund',dict(order_id=oid,customer_id='CUST001',
                reason='Return',amount='100.01',action='approve',confirmed=True,return_received=True,execution_mode='admin')))
            assert not result['ok'] and 'Admin approval required' in result['error']
            wb.user_message=f'Approve the pending refund for {eligible}. The returned goods were received.'
            result=decode_result(await wb.call_tool('process_refund',dict(order_id=eligible,customer_id='CUST001',
                reason='Return',amount='100.00',action='approve',confirmed=True,return_received=True)))
            assert result['ok'] and result['approved_by_role']=='cashier'
    asyncio.run(run())


def test_cashier_ui_to_api_to_real_mcp_approval(monkeypatch):
    import httpx
    from streamlit.testing.v1 import AppTest
    from test_end_to_end import setup_engine, call
    oid=order()
    assert refund(oid,'CUST001','Return','100.00',execution_customer_id='CUST001')['ok']
    monkeypatch.setattr(settings,'ADMIN_API_KEY',ADMIN)
    monkeypatch.setattr(settings,'CASHIER_API_KEY',CASHIER)
    setup_engine(monkeypatch,[call('process_refund',order_id=oid,customer_id='CUST001',reason='Return',
        amount='100.00',action='approve',confirmed=True,return_received=True),'Approved USD 100.00 in the local ledger. No money was transferred.'])
    original=httpx.Client.request
    with TestClient(app) as client:
        def bridge(self,method,url,**kwargs):
            if self is client:
                return original(self,method,url,**kwargs)
            return client.request(method,url,**kwargs)
        monkeypatch.setattr(httpx.Client,'request',bridge)
        ui=AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'),default_timeout=60).run()
        ui.radio[0].set_value('Cashier').run()
        assert not ui.chat_input
        next(x for x in ui.text_input if x.label=='Cashier key').set_value(CASHIER)
        next(x for x in ui.button if x.label=='Unlock cashier mode').click().run()
        assert not ui.exception
        ui.chat_input[0].set_value(f'Approve the pending USD 100.00 refund for {oid}, CUST001, reason Return. The returned goods were received.').run()
        assert not ui.exception
        assert any('Approved USD 100.00' in x.value for x in ui.text)
        with closing(connect()) as conn:
            assert conn.execute('SELECT refund_amount FROM orders WHERE order_id=?',(oid,)).fetchone()[0]==100
            assert conn.execute('SELECT approved_by_role FROM refund_requests').fetchone()[0]=='cashier'
        next(x for x in ui.button if x.label=='Log out of cashier mode').click().run()
        assert not ui.exception
        assert ui.session_state['cashier_key']=='' and ui.session_state['messages']==[]
        assert ui.radio[0].value=='Customer'


@pytest.mark.parametrize('role',['cashier','admin'])
def test_staff_cannot_initiate_refund_or_self_create_request(role):
    oid=order()
    for action in ('approve','request'):
        result=refund(oid,'CUST001','Return','10.00',action=action,confirmed=True,execution_mode=role)
        assert not result['ok'], result
    with closing(connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM refund_requests').fetchone()[0]==0
        assert conn.execute('SELECT refund_amount FROM orders').fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM billing_audit WHERE action='process_refund'").fetchone()[0]==0


@pytest.mark.parametrize('role',['cashier','admin'])
def test_staff_cannot_change_requested_reason_or_amount(role):
    oid=order()
    assert refund(oid,'CUST001','Return','10.00',execution_customer_id='CUST001')['ok']
    assert not approve(oid,'9.00',role)['ok']
    assert not approve(oid,'11.00',role)['ok']
    assert not refund(oid,'CUST001','Different reason','10.00',action='approve',confirmed=True,return_received=True,execution_mode=role)['ok']
    assert approve(oid,None,role)['ok']
