import sqlite3
from uuid import uuid4
from fastapi.testclient import TestClient
from api.main import app
from config import settings
from context import context_manager as context
from mcp_server.access import scoped_tool
from mcp_server.tools.database import execute_sql_query
from mcp_server.tools.billing_tools import request_order
from mcp_server.tools.operations_tools import manage_customer


KEY = 'test-admin-secret-123456789'


class Engine:
    async def chat(self, message, history, customer_id, mode='customer', **kwargs):
        return {'response': mode, 'invoices': []}


def test_admin_required_on_create_read_chat_and_cached_replies(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', KEY)
    app.state.engine = Engine()
    with TestClient(app) as client:
        assert client.get('/admin/access').status_code == 403
        assert client.post('/conversations',json={'mode':'admin'}).status_code == 403
        headers = {'X-Admin-Key':KEY}
        cid = client.post('/conversations',json={'mode':'admin'},headers=headers).json()['conversation_id']
        body = {'conversation_id':cid,'request_id':str(uuid4()),'message':'Show revenue'}
        assert client.post('/chat',json=body,headers=headers).json()['response'] == 'admin'
        assert client.post('/chat',json=body).status_code == 403
        assert client.get('/conversations/'+cid).status_code == 403
        assert client.get('/conversations/'+cid,headers=headers).json()['mode'] == 'admin'
        assert 'shopping_state' not in client.get('/conversations/'+cid,headers=headers).json()
        with sqlite3.connect(settings.CONTEXT_DB) as conn:
            assert conn.execute('SELECT count(*) FROM shopping_states WHERE conversation_id=?',(cid,)).fetchone()[0] == 0
        assert client.get('/invoices/INV1',params={'conversation_id':cid,'customer_id':'CUST001'}).status_code == 403
        customer = client.post('/conversations',json={}).json()['conversation_id']
        assert client.post('/chat',json={**body,'conversation_id':customer,'request_id':str(uuid4()),'mode':'admin'}).json()['response'] == 'customer'


def test_admin_disabled_or_same_as_shared_key(monkeypatch):
    app.state.engine = Engine()
    with TestClient(app) as client:
        monkeypatch.setattr(settings,'ADMIN_API_KEY','')
        assert client.get('/admin/access').status_code == 503
        monkeypatch.setattr(settings,'ADMIN_API_KEY',KEY)
        monkeypatch.setattr(settings,'API_KEY',KEY)
        assert client.get('/admin/access',headers={'X-API-Key':KEY,'X-Admin-Key':KEY}).status_code == 503


def test_customer_queries_are_physically_scoped_and_costs_hidden():
    read = scoped_tool(execute_sql_query)
    args = {'execution_customer_id':'CUST001'}
    assert read('SELECT COUNT(*) FROM customers',**args)['rows'] == [[1]]
    assert read('WITH customers AS (SELECT * FROM main.customers) SELECT customer_id FROM customers',**args)['rows'] == [['CUST001']]
    assert read('SELECT COUNT(*) FROM customers')['rows'] == [[0]]
    assert not read('SELECT baseprice FROM items',**args)['ok']
    assert not read('SELECT * FROM billing_audit',**args)['ok']
    assert read('SELECT COUNT(*) FROM customers',execution_mode='admin')['rows'] == [[4]]
    assert not read('DELETE FROM customers',execution_mode='admin')['ok']


def test_server_blocks_admin_writes_and_cross_customer_writes():
    order = scoped_tool(request_order)
    args = {'customer_id':'CUST001','items':[{'item_code':'ITEM001','quantity':1}]}
    assert not order(**args,execution_mode='admin')['ok']
    assert not order(**args,execution_customer_id='CUST002')['ok']
    assert not order(**args)['ok']
    assert order(**args,execution_customer_id='CUST001')['ok']
    customer = scoped_tool(manage_customer)
    assert not customer('CUST001','Alex Morgan',confirmed=True)['ok']
    assert not customer('NEW','New customer',confirmed=True)['ok']
    assert scoped_tool(execute_sql_query)("SELECT COUNT(*) FROM customers WHERE customer_id='NEW'",execution_mode='admin')['rows']==[[0]]


def test_legacy_conversation_migration_requires_admin(tmp_path,monkeypatch):
    db = tmp_path/'old_context.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY, customer_id TEXT, created_at TEXT)')
    conn.execute("INSERT INTO conversations VALUES ('old',NULL,'2020-01-01')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings,'CONTEXT_DB',db)
    context.initialize()
    context.initialize()
    assert context.get_conversation('old')['mode'] == 'admin'
    cid = context.conversation()
    assert context.get_conversation(cid)['mode'] == 'customer'


def test_real_workbench_overrides_forged_scope_and_separates_schemas():
    import asyncio
    from agent.billing_agent import RecordingWorkbench, decode_result
    from mcp_server.access import ADMIN_TOOLS, CUSTOMER_TOOLS
    async def run():
        async with RecordingWorkbench() as wb:
            wb.customer_id = 'CUST001'
            schemas = await wb.list_tools()
            assert {s['name'] for s in schemas} == CUSTOMER_TOOLS
            assert 'execution_mode' not in str(schemas)
            result = decode_result(await wb.call_tool('execute_sql_query', {
                'query':'SELECT COUNT(*) FROM customers', 'execution_mode':'admin', 'execution_customer_id':'CUST002'}))
            assert result['rows'] == [[1]]
            wb.mode = 'admin'
            assert {s['name'] for s in await wb.list_tools()} == ADMIN_TOOLS
            result = decode_result(await wb.call_tool('request_order', {
                'customer_id':'CUST001','items':[{'item_code':'ITEM001','quantity':1}]}))
            assert not result['ok']
            wb.mode = 'customer'
            assert {s['name'] for s in await wb.list_tools()} == CUSTOMER_TOOLS
    asyncio.run(run())
