from address_support import place_order_with_address, prepare_delivery
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from api.main import app
from config import settings
from database.connection import connect, transaction
from mcp_server.tools.billing_tools import request_order, place_order, process_refund
from mcp_server.tools.database import execute_sql_query
from mcp_server.access import scoped_tool
from functools import partial
request_refund = partial(scoped_tool(process_refund), execution_customer_id="CUST001")
process_refund = partial(scoped_tool(process_refund), action='approve', confirmed=True, return_received=True, execution_mode='admin')


def scalar(sql):
    with closing(connect()) as conn:
        return conn.execute(sql).fetchone()[0]


def test_invalid_non_ascii_api_key_is_rejected_without_server_error(monkeypatch):
    monkeypatch.setattr(settings,'API_KEY','correct-key')
    class Engine:
        pass
    app.state.engine=Engine()
    with TestClient(app,raise_server_exceptions=False) as client:
        assert client.get('/health',headers={b'X-API-Key':b'\xff'}).status_code==401


@pytest.mark.parametrize('value',[2**63, -(2**63)-1, float('nan'), float('inf')])
def test_invalid_sql_numeric_parameters_return_a_tool_error(value):
    result=execute_sql_query('SELECT ?',parameters=[value])
    assert result['ok'] is False


def test_audit_failure_rolls_back_order_and_stock():
    oid=request_order('CUST001',[{'item_code':'ITEM001','quantity':1}])['order_id']
    prepare_delivery(oid, 'CUST001')
    with transaction() as conn:
        conn.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON billing_audit WHEN NEW.action='place_order' BEGIN SELECT RAISE(ABORT,'Storage failure'); END")
    assert not place_order_with_address(oid,'CUST001')['ok']
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'")==40
    assert scalar('SELECT COUNT(*) FROM invoice_lines')==0
    assert scalar('SELECT COUNT(*) FROM order_fulfilled')==0
    assert scalar('SELECT order_invoice FROM orders') is None
    assert scalar('SELECT COUNT(*) FROM billing_audit')==3


def test_concurrent_identical_placements_and_refunds_are_single_writes():
    oid=request_order('CUST001',[{'item_code':'ITEM001','quantity':1}])['order_id']
    prepare_delivery(oid, 'CUST001')
    with ThreadPoolExecutor(4) as pool:
        results=list(pool.map(lambda _:place_order_with_address(oid,'CUST001'),range(4)))
    assert all(r['ok'] for r in results)
    assert len({r['invoice_number'] for r in results})==1
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'")==39
    assert request_refund(oid,'CUST001','Return','10.00')['ok']
    with ThreadPoolExecutor(4) as pool:
        results=list(pool.map(lambda _:process_refund(oid,'CUST001','Return','10.00'),range(4)))
    assert sum(r['ok'] for r in results)==1
    assert scalar('SELECT COUNT(*) FROM billing_audit')==6


@pytest.mark.parametrize('tax,shipping',[('NaN','0'),('Infinity','0'),('-1','0'),('101','0'),('0','-1'),('0','NaN')])
def test_invalid_billing_configuration_cannot_create_partial_drafts(tax,shipping):
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value=? WHERE key='tax_rate'",(tax,))
        conn.execute("UPDATE billing_config SET value=? WHERE key='shipping_amount'",(shipping,))
    assert not request_order('CUST001',[{'item_code':'ITEM001','quantity':1}])['ok']
    assert scalar('SELECT COUNT(*) FROM orders')==0
    assert scalar('SELECT COUNT(*) FROM billing_audit')==0


def test_timeout_after_commit_does_not_replay_mutation(monkeypatch):
    class Engine:
        calls=0
        async def chat(self,*args,**kwargs):
            self.calls+=1
            assert request_order('CUST001',[{'item_code':'ITEM001','quantity':1}],'TIMEOUT')['ok']
            await asyncio.sleep(10)
    engine=Engine()
    app.state.engine=engine
    monkeypatch.setattr(settings,'CHAT_TIMEOUT',0.05)
    with TestClient(app) as client:
        from api.customer_auth import set_password,login
        set_password('CUST001','Timeout-example-pass-1234')
        client.headers['X-Customer-Token']=login('CUST001','Timeout-example-pass-1234')['token']
        cid=client.post('/conversations',json={'customer_id':'CUST001'}).json()['conversation_id']
        body={'conversation_id':cid,'request_id':str(uuid4()),'message':'Create a draft.'}
        assert client.post('/chat',json=body).status_code==503
        assert client.post('/chat',json=body).status_code==409
        assert engine.calls==1 and scalar('SELECT COUNT(*) FROM orders')==1


@pytest.mark.parametrize('message',['', ' '*5, 'x'*10001])
def test_invalid_chat_messages_never_reach_engine(message):
    class Engine:
        async def chat(self,*args,**kwargs):
            pytest.fail('Invalid input reached engine')
    app.state.engine=Engine()
    with TestClient(app) as client:
        cid=client.post('/conversations',json={}).json()['conversation_id']
        assert client.post('/chat',json={'conversation_id':cid,'request_id':str(uuid4()),'message':message}).status_code==422


def test_missing_model_configuration_can_retry_after_correction():
    class Engine:
        configured=False
        def validate_configuration(self):
            if not self.configured:
                raise ValueError('Configure the model.')
        async def chat(self,*args,**kwargs):
            return {'response':'Ready.','invoices':[]}
    engine=Engine()
    app.state.engine=engine
    with TestClient(app) as client:
        cid=client.post('/conversations',json={}).json()['conversation_id']
        body={'conversation_id':cid,'request_id':str(uuid4()),'message':'Hello'}
        assert client.post('/chat',json=body).status_code==503
        engine.configured=True
        assert client.post('/chat',json=body).status_code==200


@pytest.mark.parametrize('query',['SELECT 1e999', 'SELECT -1e999'])
def test_nonfinite_query_results_are_reported_as_errors(query):
    assert not execute_sql_query(query)['ok']


@pytest.mark.parametrize('value',[-(2**63),2**63-1,0,0.25])
def test_valid_numeric_parameter_boundaries(value):
    assert execute_sql_query('SELECT ?',parameters=[value])['rows']==[[value]]
