import asyncio
import json
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from api.main import app, stream_chat, ChatRequest
from config import settings
from context import context_manager as context
from agent.billing_agent import RecordingWorkbench, McpWorkbench


def test_chart_event_precedes_final_answer_and_retry_does_not_repeat(monkeypatch):
    monkeypatch.setattr(settings,'ADMIN_API_KEY','speed-test-admin-key')
    async def run():
        release=asyncio.Event()
        class Engine:
            calls=0
            async def chat(self,*args,chart_callback=None,**kwargs):
                self.calls+=1
                charts=[{'type':'bar','title':'Stock','x':'item','y':['stock'],
                         'data':[{'item':'A','stock':10}]}]
                chart_callback(charts)
                await release.wait()
                return {'response':'Stock is shown.','charts':charts,'invoices':[]}
        engine=Engine()
        monkeypatch.setattr(app.state,'engine',engine,raising=False)
        monkeypatch.setattr(app.state,'chat_lock',asyncio.Lock(),raising=False)
        cid=context.conversation(mode='admin')
        body=ChatRequest(conversation_id=cid,request_id=uuid4(),message='Show stock as a chart')
        response=await stream_chat(body,settings.ADMIN_API_KEY)
        first=json.loads(await asyncio.wait_for(anext(response.body_iterator),2))
        assert first['event']=='charts' and not release.is_set()
        release.set()
        final=json.loads(await anext(response.body_iterator))
        assert final['event']=='result' and final['result']['charts']==first['charts']
        await response.body_iterator.aclose()
        assert context.history(cid,include_artifacts=True)[-1]['charts']==first['charts']
        retry=await stream_chat(body,settings.ADMIN_API_KEY)
        assert json.loads(await anext(retry.body_iterator))==final
        await retry.body_iterator.aclose()
        assert engine.calls==1
        with pytest.raises(HTTPException) as denied:
            await stream_chat(body,None)
        assert denied.value.status_code==403
    asyncio.run(run())


def test_stream_error_is_terminal_and_disconnect_releases_lock(monkeypatch):
    monkeypatch.setattr(settings,'ADMIN_API_KEY','speed-test-admin-key')
    async def run():
        class Engine:
            async def chat(self,*args,chart_callback=None,**kwargs):
                chart_callback([{'type':'bar'}])
                await asyncio.Event().wait()
        monkeypatch.setattr(app.state,'engine',Engine(),raising=False)
        monkeypatch.setattr(app.state,'chat_lock',asyncio.Lock(),raising=False)
        cid=context.conversation(mode='admin')
        body=ChatRequest(conversation_id=cid,request_id=uuid4(),message='Chart')
        response=await stream_chat(body,settings.ADMIN_API_KEY)
        assert json.loads(await anext(response.body_iterator))['event']=='charts'
        await response.body_iterator.aclose()
        assert not app.state.chat_lock.locked()
        class Failing:
            async def chat(self,*args,**kwargs):
                raise TimeoutError()
        monkeypatch.setattr(app.state,'engine',Failing())
        body.request_id=uuid4()
        response=await stream_chat(body,settings.ADMIN_API_KEY)
        event=json.loads(await anext(response.body_iterator))
        assert event['event']=='error' and event['status']==503
        await response.body_iterator.aclose()
    asyncio.run(run())


def test_tool_payload_compaction_preserves_every_value(monkeypatch):
    captured=[]
    original=McpWorkbench.call_tool
    async def capture(self,*args,**kwargs):
        result=await original(self,*args,**kwargs)
        captured.append([b.content for b in result.result if isinstance(getattr(b,'content',None),str)])
        return result
    monkeypatch.setattr(McpWorkbench,'call_tool',capture)
    async def run():
        async with RecordingWorkbench() as wb:
            result=await wb.call_tool('execute_sql_query',{'query':
                "WITH RECURSIVE n(id) AS (SELECT 1 UNION ALL SELECT id+1 FROM n WHERE id<200) SELECT id,'Product name' AS name, id*1.25 AS price FROM n"})
            before=captured[0][0]
            after=result.result[0].content
            payload=json.loads(after)
            assert payload.pop('table_delivery') == {'total_attached_this_turn':1,'errors':[]}
            assert json.loads(before)==payload
            assert json.loads(after)['row_count']==200
            compact_values=json.dumps(payload,ensure_ascii=False,separators=(',',':'))
            assert len(compact_values)<=len(before)
            # Delivery is new, bounded metadata, not duplicate result rows.
            assert len(after)-len(compact_values)<=100
            wb.mode='admin'
            result=await wb.call_tool('execute_sql_query',{'query':
                "WITH RECURSIVE n(id) AS (SELECT 1 UNION ALL SELECT id+1 FROM n WHERE id<200) SELECT id,'Product name' AS name, id*1.25 AS price FROM n"})
            after=result.result[0].content
            # Previous admin attachment handling serialized this same object
            # with json.dumps defaults, including spaces after every separator.
            before=json.dumps(json.loads(after))
            assert json.loads(before)==json.loads(after)
            import tiktoken
            tokenizer=tiktoken.get_encoding('cl100k_base')
            before_tokens=len(tokenizer.encode(before)); after_tokens=len(tokenizer.encode(after))
            print(f'PAYLOAD_BENCHMARK before_tokens={before_tokens} after_tokens={after_tokens} reduction={1-after_tokens/before_tokens:.1%}')
    asyncio.run(run())


def test_chart_stream_arrives_over_http_before_answer_finishes(monkeypatch):
    import socket
    import threading
    import time
    import httpx
    import uvicorn
    release=threading.Event()
    finished=threading.Event()
    class Engine:
        async def chat(self,*args,chart_callback=None,**kwargs):
            charts=[{'type':'bar','title':'Stock','x':'item','y':['stock'],'data':[{'item':'A','stock':10}]}]
            chart_callback(charts)
            while not release.is_set():
                await asyncio.sleep(.01)
            finished.set()
            return {'response':'Stock is shown.','charts':charts,'invoices':[]}
    monkeypatch.setattr(app.state,'engine',Engine(),raising=False)
    monkeypatch.setattr(settings,'API_KEY','')
    monkeypatch.setattr(settings,'ADMIN_API_KEY','speed-test-admin-key')
    listener=socket.socket()
    listener.bind(('127.0.0.1',0))
    port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True)
    thread.start()
    try:
        deadline=time.monotonic()+15
        while not server.started and time.monotonic()<deadline:
            time.sleep(.02)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=5,
                headers={'X-Admin-Key':settings.ADMIN_API_KEY}) as client:
            cid=client.post('/conversations',json={'mode':'admin'}).json()['conversation_id']
            with client.stream('POST','/chat/stream',json={'conversation_id':cid,
                    'request_id':str(uuid4()),'message':'Show stock as a chart'}) as response:
                assert response.status_code==200
                lines=response.iter_lines()
                assert json.loads(next(lines))['event']=='charts'
                assert not finished.is_set()
                release.set()
                assert json.loads(next(lines))['event']=='result'
    finally:
        release.set()
        server.should_exit=True
        thread.join(10)
        listener.close()
    assert not thread.is_alive()
