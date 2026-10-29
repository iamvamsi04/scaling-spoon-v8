"""Skip unused report blobs and serve authorized completed retries without queueing."""
from address_support import prepare_delivery
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import main
from config import settings
from context import context_manager as context
from context.shopping_state import ShoppingTracker
from mcp_server.tools.billing_tools import request_order


def finished_conversation(mode='admin', customer_id=None, message='Show the results'):
    cid = context.conversation(mode=mode, customer_id=customer_id)
    rid = str(uuid4())
    context.begin(cid, rid, message)
    saved = {'conversation_id': cid, 'request_id': rid,
             'response': '{"reply":"The results are ready."}',
             'charts': [], 'tables': [], 'reports': [], 'invoices': []}
    context.finish(cid, rid, saved)
    return cid, rid, saved


def test_transcript_history_does_not_read_or_join_response_blobs(monkeypatch):
    cid = context.conversation(mode='admin')
    rid = str(uuid4())
    context.begin(cid, rid, 'Show a complete report')
    context.finish(cid, rid, {'response': 'The report is ready.',
        'charts': [{'type': 'bar', 'title': 'Stock', 'x': 'item', 'y': ['stock'],
                    'data': [{'item': 'Keyboard', 'stock': 4}]}],
        'invoices': [], 'unused_large_payload': 'x' * 1_000_000})
    assert context.history(cid, include_artifacts=True)[-1]['charts']
    original_connection = context.connection
    statements, columns = [], []

    @contextmanager
    def transcript_connection():
        with original_connection() as conn:
            conn.set_trace_callback(statements.append)

            def authorize(action, table, column, database, source):
                if action == sqlite3.SQLITE_READ and table == 'messages':
                    columns.append(column)
                    if column == 'response_json':
                        return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorize)
            yield conn

    monkeypatch.setattr(context, 'connection', transcript_connection)
    assert context.history(cid) == [
        {'role': 'user', 'content': 'Show a complete report'},
        {'role': 'assistant', 'content': 'The report is ready.'}]
    assert 'response_json' not in columns
    assert not any('JOIN' in statement.upper() for statement in statements)
    # This proves the authorizer would catch accidentally fetching the blob.
    with pytest.raises(sqlite3.DatabaseError):
        context.history(cid, include_artifacts=True)


@pytest.mark.parametrize('failed', [False, True])
def test_completed_lookup_never_retries_pending_or_failed_work(failed):
    cid = context.conversation()
    rid = str(uuid4())
    context.begin(cid, rid, 'Create an order')
    if failed:
        context.fail(cid, rid)
    assert context.completed_response(cid, rid, 'Create an order') is None
    with pytest.raises(ValueError, match='pending or failed'):
        context.begin(cid, rid, 'Create an order')
    with pytest.raises(ValueError, match='different content'):
        context.completed_response(cid, rid, 'Create another order')


def test_completed_lookup_preserves_exact_response_and_content_binding():
    cid, rid, saved = finished_conversation()
    assert context.completed_response(cid, rid, 'Show the results') == saved
    assert context.completed_response(cid, str(uuid4()), 'Show the results') is None
    with pytest.raises(ValueError, match='different content'):
        context.completed_response(cid, rid, 'Different results')
    # The existing transactional check still protects the execution path.
    assert context.begin(cid, rid, 'Show the results') == saved


def test_completed_retry_bypasses_unrelated_model_turn_but_not_authorization(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'request-efficiency-admin-key')

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        class Engine:
            calls = 0

            async def chat(self, *args, **kwargs):
                self.calls += 1
                started.set()
                await release.wait()
                return {'response': 'The new results are ready.', 'invoices': []}

        engine = Engine()
        monkeypatch.setattr(main.app.state, 'engine', engine, raising=False)
        monkeypatch.setattr(main.app.state, 'chat_lock', asyncio.Lock(), raising=False)
        old_cid, old_rid, saved = finished_conversation()
        body = main.ChatRequest(conversation_id=old_cid, request_id=old_rid,
                                message='Show the results')
        active = main.ChatRequest(conversation_id=context.conversation(mode='admin'),
                                  request_id=uuid4(), message='Show fresh results')
        task = asyncio.create_task(main.run_chat(active, settings.ADMIN_API_KEY))
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert main.app.state.chat_lock.locked()
            result = await asyncio.wait_for(main.run_chat(body, settings.ADMIN_API_KEY), 1)
            assert result == {**saved, 'response': 'The results are ready.'}
            assert engine.calls == 1 and not release.is_set()
            mismatch = body.model_copy(update={'message': 'Changed request'})
            with pytest.raises(HTTPException) as conflict:
                await asyncio.wait_for(main.run_chat(mismatch, settings.ADMIN_API_KEY), 1)
            assert conflict.value.status_code == 409
            with pytest.raises(HTTPException) as denied:
                await asyncio.wait_for(main.run_chat(body, 'incorrect-admin-key'), 1)
            assert denied.value.status_code == 403
            assert engine.calls == 1
        finally:
            release.set()
            await task
        assert not main.app.state.chat_lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize('mode', ['customer', 'cashier', 'admin'])
def test_completed_reply_requires_current_role_and_customer_authorization(monkeypatch, mode):
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'request-efficiency-admin-key')
    monkeypatch.setattr(settings, 'CASHIER_API_KEY', 'request-efficiency-cashier-key')
    monkeypatch.setattr(main.customer_auth, 'customer_for_token',
        lambda token: 'CUST001' if token == 'matching-customer-token' else 'CUST002')
    cid, rid, saved = finished_conversation(mode=mode,
        customer_id='CUST001' if mode == 'customer' else None)
    body = main.ChatRequest(conversation_id=cid, request_id=rid, message='Show the results')

    async def run():
        with pytest.raises(HTTPException) as denied:
            await main.run_chat(body, None, x_customer_token='other-customer-token')
        assert denied.value.status_code == (401 if mode == 'customer' else 403)
        arguments = ({'x_admin_key': settings.ADMIN_API_KEY} if mode == 'admin' else
                     {'x_admin_key': None, 'x_cashier_key': settings.CASHIER_API_KEY} if mode == 'cashier' else
                     {'x_admin_key': None, 'x_customer_token': 'matching-customer-token'})
        result = await main.run_chat(body, **arguments)
        assert result == {**saved, 'response': 'The results are ready.'}

    asyncio.run(run())


def test_both_cache_paths_use_identical_artifact_and_plain_reply_validation():
    value = {'response': '{"reply":"Ready."}', 'tables': [{'invalid': True}],
             'reports': [{'invalid': True}], 'invoices': [], 'charts': []}
    assert main.cached_chat_response(deepcopy(value)) == {
        **value, 'response': 'Ready.', 'tables': [], 'reports': []}


def owned_pending_conversation(client):
    """Persist a real owned draft proposal under a real authenticated account."""
    password = 'Customer efficiency test password 2026'
    main.customer_auth.set_password('CUST001', password)
    login = client.post('/customers/login', json={'customer_id':'CUST001','password':password})
    assert login.status_code == 200, login.text
    client.headers['X-Customer-Token'] = login.json()['token']
    created = client.post('/conversations', json={'customer_id':'CUST001'})
    assert created.status_code == 200, created.text
    cid = created.json()['conversation_id']
    draft = request_order('CUST001', [{'item_code':'ITEM001','quantity':1}], 'EFF-PENDING')
    assert draft['ok'], draft
    prepare_delivery(draft['order_id'], 'CUST001')
    tracker = ShoppingTracker(None, 'CUST001')
    summary = 'Place draft EFF-PENDING for one keyboard, total USD 79.90?'
    reply = tracker.finish(json.dumps({'reply':summary, 'shopping_update':{
        'needs':['quiet office'], 'pending_confirmation':{
            'action':'place_order','order_id':'EFF-PENDING','summary':summary},
        'next_question':'Please confirm the quoted order.'}}))
    assert tracker.state['pending_confirmation'] and tracker.state['next_question']
    rid = str(uuid4())
    message = 'Please prepare the draft details.'
    context.begin(cid, rid, message)
    saved = {'conversation_id':cid,'request_id':rid,'response':reply,'invoices':[],
             'shopping_state':deepcopy(tracker.state)}
    context.finish(cid, rid, saved)
    return cid, rid, message, saved


@pytest.mark.parametrize('failure_stage', ['configuration','workbench_start','chat'])
def test_early_customer_turn_failure_cannot_revive_a_withdrawn_proposal(monkeypatch, failure_stage):
    monkeypatch.setattr(settings, 'API_KEY', '')

    class Engine:
        cid = None
        calls = 0
        failed_at = failure_stage
        seen_states = []

        def validate_configuration(self):
            stored = context.shopping_state(self.cid)
            assert stored['pending_confirmation'] is None and stored['next_question'] is None
            if self.failed_at == 'configuration':
                raise ValueError('Model configuration unavailable before tracker startup.')

        async def _start_workbench(self):
            if self.failed_at == 'workbench_start':
                raise RuntimeError('MCP startup failed before any shopping checkpoint.')

        async def chat(self, message, history, customer_id, **kwargs):
            self.calls += 1
            self.seen_states.append(deepcopy(kwargs['shopping_state']))
            stored = context.shopping_state(self.cid)
            assert stored['pending_confirmation'] is None and stored['next_question'] is None
            await self._start_workbench()
            if self.failed_at == 'chat':
                raise RuntimeError('Chat failed before any shopping checkpoint.')
            return {'response':'No order change was made.', 'invoices':[],
                    'shopping_state':deepcopy(kwargs['shopping_state'])}

    engine = Engine()
    monkeypatch.setattr(main.app.state, 'engine', engine, raising=False)
    with TestClient(main.app) as client:
        cid, _, _, saved = owned_pending_conversation(client)
        engine.cid = cid
        withdrawal = {'conversation_id':cid,'request_id':str(uuid4()),'message':'No, leave it.'}
        response = client.post('/chat', json=withdrawal)
        assert response.status_code == 503, response.text
        after = context.shopping_state(cid)
        assert after['pending_confirmation'] is None and after['next_question'] is None
        assert after['needs'] == saved['shopping_state']['needs']
        if failure_stage == 'configuration':
            assert engine.calls == 0
        else:
            # The attempted turn still gets the original proposal for interpreting
            # the human response, although it is no longer a future approval target.
            assert engine.seen_states[0] == saved['shopping_state']
        engine.failed_at = None
        later = client.post('/chat', json={'conversation_id':cid,'request_id':str(uuid4()),'message':'Yes.'})
        assert later.status_code == 200, later.text
        assert engine.seen_states[-1]['pending_confirmation'] is None
        assert context.shopping_state(cid)['pending_confirmation'] is None


def test_current_customer_turn_receives_original_proposal_after_persistent_expiry_and_can_renew_it(monkeypatch):
    monkeypatch.setattr(settings, 'API_KEY', '')

    class Engine:
        cid = None
        expected = None
        calls = 0

        async def chat(self, message, history, customer_id, **kwargs):
            self.calls += 1
            assert message == 'Yes.' and customer_id == 'CUST001'
            assert kwargs['shopping_state'] == self.expected
            stored = context.shopping_state(self.cid)
            assert stored['pending_confirmation'] is None and stored['next_question'] is None
            tracker = ShoppingTracker(kwargs['shopping_state'], customer_id)
            summary = 'Confirm the unchanged draft EFF-PENDING, total USD 79.90?'
            reply = tracker.finish(json.dumps({'reply':summary,'shopping_update':{
                'pending_confirmation':{'action':'place_order','order_id':'EFF-PENDING','summary':summary},
                'next_question':'Please confirm the unchanged quote.'}}))
            return {'response':reply,'invoices':[],'shopping_state':tracker.state}

    engine = Engine()
    monkeypatch.setattr(main.app.state, 'engine', engine, raising=False)
    with TestClient(main.app) as client:
        cid, _, _, saved = owned_pending_conversation(client)
        engine.cid, engine.expected = cid, deepcopy(saved['shopping_state'])
        response = client.post('/chat', json={'conversation_id':cid,'request_id':str(uuid4()),'message':'Yes.'})
        assert response.status_code == 200, response.text
        renewed = context.shopping_state(cid)
        assert engine.calls == 1
        assert renewed['pending_confirmation']['order_id'] == 'EFF-PENDING'
        assert renewed['pending_confirmation']['status'] == 'awaiting_customer'
        assert renewed['pending_confirmation']['summary'].startswith('Confirm the unchanged draft')
        assert renewed['next_question'] == 'Please confirm the unchanged quote.'


def test_cached_customer_retry_does_not_expire_or_replace_a_newer_pending_proposal(monkeypatch):
    monkeypatch.setattr(settings, 'API_KEY', '')

    class Engine:
        def validate_configuration(self):
            raise AssertionError('Completed retries must not enter model configuration or turn execution.')

        async def chat(self, *args, **kwargs):
            raise AssertionError('A completed retry must not run a new customer turn.')

    monkeypatch.setattr(main.app.state, 'engine', Engine(), raising=False)
    with TestClient(main.app) as client:
        cid, rid, message, saved = owned_pending_conversation(client)
        next_draft = request_order('CUST001', [{'item_code':'ITEM002','quantity':1}], 'EFF-CURRENT')
        assert next_draft['ok'], next_draft
        prepare_delivery(next_draft['order_id'], 'CUST001')
        tracker = ShoppingTracker(saved['shopping_state'], 'CUST001')
        summary = 'Place the new owned draft EFF-CURRENT, total USD 29.50?'
        reply = tracker.finish(json.dumps({'reply':summary,'shopping_update':{
            'pending_confirmation':{'action':'place_order','order_id':'EFF-CURRENT','summary':summary},
            'next_question':'Please confirm the current quote.'}}))
        newer = str(uuid4())
        context.begin(cid, newer, 'Prepare the other owned draft.')
        context.finish(cid, newer, {'response':reply,'invoices':[],'shopping_state':tracker.state})
        current = deepcopy(context.shopping_state(cid))
        assert current['pending_confirmation']['order_id'] == 'EFF-CURRENT'
        response = client.post('/chat', json={'conversation_id':cid,'request_id':rid,'message':message})
        assert response.status_code == 200, response.text
        assert response.json() == saved
        assert context.shopping_state(cid) == current
