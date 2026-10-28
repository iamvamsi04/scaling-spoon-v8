"""Retry only when durable dispatch evidence proves no change was attempted."""
import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_core.tools import TextResultContent, ToolResult
from autogen_ext.models.replay import ReplayChatCompletionClient
from fastapi.testclient import TestClient
from openai import APIConnectionError

from agent import billing_agent as agent
from api.main import app
from config import settings
from context import context_manager as context
from database.connection import connect


def request_record(cid, rid):
    with context.connection() as conn:
        return dict(conn.execute("SELECT * FROM messages WHERE conversation_id=? "
                                 "AND request_id=? AND role='user'", (cid, rid)).fetchone())


@pytest.mark.parametrize('read_first', [False, True])
def test_api_failed_read_can_retry_same_id_without_repeating_a_completed_turn(read_first):
    class ReadEngine:
        supports_request_progress = True
        calls = 0

        async def chat(self, message, history, customer_id, **kwargs):
            self.calls += 1
            assert callable(kwargs['mutation_callback'])
            if read_first:
                with connect(readonly=True) as conn:
                    assert conn.execute('SELECT COUNT(*) FROM items').fetchone()[0] > 0
            if self.calls == 1:
                raise ConnectionError('Provider failed before a final answer')
            return {'response': 'The current products are ready.'}

    engine = ReadEngine()
    app.state.engine = engine
    with TestClient(app) as client:
        cid = client.post('/conversations', json={}).json()['conversation_id']
        body = {'conversation_id': cid, 'request_id': str(uuid4()), 'message': 'Browse products'}
        assert client.post('/chat', json=body).status_code == 503
        assert request_record(cid, body['request_id'])['may_have_changed'] == 0
        result = client.post('/chat', json=body)
        assert result.status_code == 200
        assert client.post('/chat', json=body).json() == result.json()
        assert engine.calls == 2
        assert len(context.history(cid)) == 2


@pytest.mark.parametrize('write_completes', [False, True])
def test_api_failed_dispatched_change_cannot_retry_even_when_reflection_failed(write_completes):
    class WriteEngine:
        supports_request_progress = True
        calls = 0

        async def chat(self, message, history, customer_id, **kwargs):
            self.calls += 1
            kwargs['mutation_callback']()
            assert request_record(cid, rid)['may_have_changed'] == 1
            if write_completes:
                with connect() as conn:
                    conn.execute('UPDATE items SET availablequantity=availablequantity-1 WHERE item_code=?', ('ITEM001',))
                    conn.commit()
            raise ConnectionError('Response was lost')

    engine = WriteEngine()
    app.state.engine = engine
    with TestClient(app) as client:
        cid = client.post('/conversations', json={}).json()['conversation_id']
        rid = str(uuid4())
        body = {'conversation_id': cid, 'request_id': rid, 'message': 'Change the draft'}
        assert client.post('/chat', json=body).status_code == 503
        assert client.post('/chat', json=body).status_code == 409
        assert engine.calls == 1


@pytest.mark.parametrize('capability', [None, 1, 'yes'])
def test_undeclared_or_truthy_unknown_engine_is_not_retry_safe(capability):
    class UnknownEngine:
        supports_request_progress = capability
        calls = 0

        async def chat(self, *args, **kwargs):
            self.calls += 1
            assert 'mutation_callback' not in kwargs
            raise ConnectionError('Unknown execution progress')

    engine = UnknownEngine()
    app.state.engine = engine
    with TestClient(app) as client:
        cid = client.post('/conversations', json={}).json()['conversation_id']
        body = {'conversation_id': cid, 'request_id': str(uuid4()), 'message': 'Read the order'}
        assert client.post('/chat', json=body).status_code == 503
        assert client.post('/chat', json=body).status_code == 409
        assert engine.calls == 1


def test_safe_retry_rejects_pending_changed_message_or_an_intervening_turn():
    cid = context.conversation()
    rid = str(uuid4())
    context.begin(cid, rid, 'Original', may_have_changed=False)
    with pytest.raises(ValueError):
        context.begin(cid, rid, 'Original', may_have_changed=False)
    context.fail(cid, rid)
    with pytest.raises(ValueError):
        context.begin(cid, rid, 'Different', may_have_changed=False)
    newer = str(uuid4())
    context.begin(cid, newer, 'A later turn', may_have_changed=False)
    context.fail(cid, newer)
    with pytest.raises(ValueError):
        context.begin(cid, rid, 'Original', may_have_changed=False)
    assert request_record(cid, rid)['status'] == 'failed'


def test_two_simultaneous_retries_can_reopen_a_failed_read_only_once():
    cid, rid = context.conversation(), str(uuid4())
    context.begin(cid, rid, 'Read', may_have_changed=False)
    context.fail(cid, rid)

    def attempt(_):
        try:
            context.begin(cid, rid, 'Read', may_have_changed=False)
            return 'started'
        except ValueError:
            return 'blocked'

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ['blocked', 'started']
    assert request_record(cid, rid)['status'] == 'pending'


def test_migration_treats_old_failed_turns_as_uncertain(tmp_path, monkeypatch):
    path = tmp_path / 'old-context.db'
    cid, rid = str(uuid4()), str(uuid4())
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY,
            customer_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'done', response_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(conversation_id,request_id,role));''')
        conn.execute('INSERT INTO conversations(conversation_id) VALUES (?)', (cid,))
        conn.execute("INSERT INTO messages(conversation_id,request_id,role,content,status) "
                     "VALUES (?,?,'user','Read','failed')", (cid, rid))
    monkeypatch.setattr(settings, 'CONTEXT_DB', path)
    context.initialize()
    context.initialize()
    assert request_record(cid, rid)['may_have_changed'] == 1
    with pytest.raises(ValueError):
        context.begin(cid, rid, 'Read', may_have_changed=False)


def test_progress_cannot_mark_a_missing_or_finished_request():
    cid, rid = context.conversation(), str(uuid4())
    with pytest.raises(ValueError):
        context.mark_request_changed(cid, rid)
    context.begin(cid, rid, 'Read', may_have_changed=False)
    context.finish(cid, rid, {'response': 'Ready'})
    with pytest.raises(ValueError):
        context.mark_request_changed(cid, rid)
    assert context.begin(cid, rid, 'Read') == {'response': 'Ready'}


def test_dispatch_marks_once_before_transport_but_never_for_reads_or_blocked_changes(monkeypatch):
    events = []

    async def transport(self, name, arguments, *args, **kwargs):
        events.append(name)
        if name == 'request_order':
            assert events[0] == 'marked'
        return ToolResult(name=name, is_error=False, result=[TextResultContent(
            content=json.dumps({'ok': True, 'order_id': 'PROGRESS-DRAFT'}))])

    monkeypatch.setattr(agent.McpWorkbench, 'call_tool', transport)

    async def run():
        wb = agent.RecordingWorkbench()
        wb.mutation_callback = lambda: events.append('marked')
        monkeypatch.setattr(wb, 'record_result', lambda *args: None)
        await wb.call_tool('execute_sql_query', {'query': 'SELECT name FROM items'})
        assert events == ['execute_sql_query']
        events.clear()
        blocked = agent.decode_result(await wb.call_tool('apply_promotion',
            {'order_id': 'PROGRESS-DRAFT', 'promo_code': 'NOT-SUPPLIED'}))
        assert not blocked['ok'] and events == []
        arguments = {'order_id': 'PROGRESS-DRAFT', 'customer_id': 'CUST001',
                     'items': [{'item_code': 'ITEM001', 'quantity': 1}]}
        await wb.call_tool('request_order', arguments)
        await wb.call_tool('request_order', arguments)
        assert events == ['marked', 'request_order', 'request_order']
    asyncio.run(run())


def test_failed_progress_checkpoint_prevents_transport_dispatch(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('Transport must not run without a durable checkpoint')

    monkeypatch.setattr(agent.McpWorkbench, 'call_tool', forbidden)

    async def run():
        wb = agent.RecordingWorkbench()

        def failed_checkpoint():
            raise RuntimeError('Context storage unavailable')

        wb.mutation_callback = failed_checkpoint
        result = await wb.call_tool('request_order', {'order_id': 'NOT-DISPATCHED',
            'customer_id': 'CUST001', 'items': [{'item_code': 'ITEM001', 'quantity': 1}]})
        assert result.is_error and not wb._change_recorded
    asyncio.run(run())


def test_managed_engine_clears_progress_callback_after_failure_and_between_turns(monkeypatch):
    async def run():
        engine = agent.BillingAgent()
        wb = agent.RecordingWorkbench()
        engine._managed = True
        engine._workbench = wb
        callbacks = [lambda: None, lambda: None]
        observed = []

        async def ready():
            engine._workbench = wb
            return wb

        async def stopped():
            pass

        def failed_client():
            observed.append(wb.mutation_callback)
            assert wb._change_recorded is False
            raise ConnectionError('Provider setup failed')

        monkeypatch.setattr(engine, '_get_workbench', ready)
        monkeypatch.setattr(wb, 'stop', stopped)
        monkeypatch.setattr(agent, 'create_model_client', failed_client)
        for callback in callbacks:
            wb._change_recorded = True
            with pytest.raises(ConnectionError):
                await engine.chat('Review the current inventory', [], None, mode='admin',
                                  mutation_callback=callback)
            assert wb.mutation_callback is None and not wb._change_recorded
            assert engine._workbench is None
        assert observed == callbacks
    asyncio.run(run())


def test_successful_managed_turn_clears_progress_callback_without_resetting_transport(monkeypatch):
    async def run():
        engine = agent.BillingAgent()
        wb = agent.RecordingWorkbench()
        engine._managed = True
        engine._workbench = wb
        engine._client = object()
        callback = lambda: None

        async def ready():
            return wb

        class Answer:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                assert wb.mutation_callback is callback
                wb._change_recorded = True
                return SimpleNamespace(messages=[agent.TextMessage(content='Ready', source='billing_agent')])

        monkeypatch.setattr(engine, '_get_workbench', ready)
        monkeypatch.setattr(agent, 'SafeBillingAssistant', Answer)
        result = await engine.chat('Explain the available reports', [], None, mode='admin',
                                   mutation_callback=callback)
        assert result['response'] == 'Ready'
        assert engine._workbench is wb
        assert wb.mutation_callback is None and not wb._change_recorded
    asyncio.run(run())


def test_real_api_agent_read_recovers_same_request_after_first_model_connection_failure(monkeypatch):
    class UnavailableReplay(ReplayChatCompletionClient):
        attempts = 0

        async def create(self, *args, **kwargs):
            self.attempts += 1
            raise APIConnectionError(request=httpx.Request('POST', 'https://example.invalid/model'))

    query = 'SELECT COUNT(*) AS product_count FROM items WHERE availablequantity>0'
    read = CreateResult(content=[FunctionCall(id=str(uuid4()), name='execute_sql_query',
        arguments=json.dumps({'query': query}))], finish_reason='function_calls',
        usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)
    created = []

    def model_client():
        replay = UnavailableReplay([]) if not created else ReplayChatCompletionClient([
            read, json.dumps({'reply': 'There are three products in stock.', 'shopping_update': {}})])
        created.append(replay)
        return replay

    monkeypatch.setattr(settings, 'azure_configuration_error', lambda: None)
    monkeypatch.setattr(settings, 'API_KEY', '')
    monkeypatch.setattr(agent, 'create_model_client', model_client)
    engine = agent.BillingAgent()
    monkeypatch.setattr(app.state, 'engine', engine, raising=False)
    with TestClient(app) as client:
        cid = client.post('/conversations', json={}).json()['conversation_id']
        rid = str(uuid4())
        body = {'conversation_id': cid, 'request_id': rid,
                'message': 'How many products are in stock?'}
        failed = client.post('/chat', json=body)
        assert failed.status_code == 503
        assert failed.json()['detail']['code'] == 'azure_connection_failed'
        assert request_record(cid, rid)['may_have_changed'] == 0
        assert engine._client is None and engine._workbench is None
        recovered = client.post('/chat', json=body)
        assert recovered.status_code == 200, recovered.json()
        assert recovered.json()['response'] == 'There are three products in stock.'
        assert len(created) == 2 and created[0].attempts == 1
        assert len(created[1].create_calls) == 2
        assert engine._workbench.tool_calls == 1
        tool, arguments, data = engine._workbench.receipts[0]
        assert tool == 'execute_sql_query' and arguments['query'] == query
        assert data['ok'] and data['rows'] == [[3]]
        assert request_record(cid, rid)['may_have_changed'] == 0
        assert client.post('/chat', json=body).json() == recovered.json()
        assert len(created) == 2 and len(created[1].create_calls) == 2
        assert len(context.history(cid)) == 2


def test_real_api_agent_completed_draft_is_not_retried_after_reflection_connection_failure(monkeypatch):
    order_id = 'SAFE-RETRY-DRAFT'
    draft_call = CreateResult(content=[FunctionCall(id=str(uuid4()), name='request_order',
        arguments=json.dumps({'order_id': order_id, 'customer_id': 'CUST001',
            'items': [{'item_code': 'ITEM001', 'quantity': 1}]}))], finish_reason='function_calls',
        usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)

    class FailedReflectionReplay(ReplayChatCompletionClient):
        attempts = 0

        async def create(self, *args, **kwargs):
            self.attempts += 1
            if self.create_calls:
                raise APIConnectionError(request=httpx.Request('POST', 'https://example.invalid/model'))
            return await super().create(*args, **kwargs)

    replay = FailedReflectionReplay([draft_call])
    created = []

    def model_client():
        created.append(replay)
        return replay

    monkeypatch.setattr(settings, 'azure_configuration_error', lambda: None)
    monkeypatch.setattr(settings, 'API_KEY', '')
    monkeypatch.setattr(agent, 'create_model_client', model_client)
    engine = agent.BillingAgent()
    monkeypatch.setattr(app.state, 'engine', engine, raising=False)
    with TestClient(app) as client:
        from api.customer_auth import set_password
        password = 'A separate customer retry test password'
        set_password('CUST001', password)
        signed_in = client.post('/customers/login', json={'customer_id': 'CUST001', 'password': password})
        assert signed_in.status_code == 200
        client.headers['X-Customer-Token'] = signed_in.json()['token']
        cid = client.post('/conversations', json={'customer_id': 'CUST001'}).json()['conversation_id']
        rid = str(uuid4())
        body = {'conversation_id': cid, 'request_id': rid,
                'message': 'Prepare a draft for one mechanical keyboard.'}
        failed = client.post('/chat', json=body)
        assert failed.status_code == 503
        assert failed.json()['detail']['code'] == 'azure_connection_failed'
        assert request_record(cid, rid)['may_have_changed'] == 1
        with connect(readonly=True) as conn:
            orders = conn.execute('SELECT order_id,order_status,order_invoice FROM orders').fetchall()
            assert [tuple(row) for row in orders] == [(order_id, 'draft', None)]
            assert conn.execute('SELECT quantity FROM order_requests WHERE order_id=?', (order_id,)).fetchone()[0] == 1
        assert replay.attempts == 2 and len(created) == 1
        assert engine._client is None and engine._workbench is None
        assert client.post('/chat', json=body).status_code == 409
        assert replay.attempts == 2 and len(created) == 1
        with connect(readonly=True) as conn:
            assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 1
