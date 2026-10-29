"""Conversation meaning comes from the model; billing constraints remain executable.

The replay client controls only model decisions. Draft creation below uses the real
MCP subprocess, scope wrapper, SQLite transactions and shopping-state observation.
These checks do not claim to measure live Azure language understanding or latency.
"""
from address_support import place_order_with_address, prepare_delivery
import ast
import asyncio
from contextlib import closing
import json
from pathlib import Path

import pytest
from autogen_ext.models.replay import ReplayChatCompletionClient

import agent.billing_agent as module
from config import settings
from database.connection import connect, transaction
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import request_order, place_order, process_refund
from test_agent_lifecycle import tool_call


def answer(text):
    return json.dumps({'reply': text, 'shopping_update': {}})


def test_varied_order_requests_execute_without_phrase_gate_or_classifier(monkeypatch):
    requests = [
        'I would like one mechanical keyboard.',
        'Could you prepare a basket containing a single mechanical keyboard?',
        'That keyboard suits me. Put one aside as a quote.',
        'Please make an order request for one mechanical keyboard.',
        'One mechanical keyboard is my selection; start the draft please.',
        'Let us go with one mechanical keyboard for my purchase.',
        'Can I have a mechanical keyboard in my cart, quantity one?',
    ]
    script = []
    for index in range(len(requests)):
        script += [tool_call('request_order', customer_id='CUST001',
            order_id=f'SEMANTIC-{index}', items=[{'item_code': 'ITEM001', 'quantity': 1}]),
            answer('Your keyboard draft is ready for review.')]
    replay = ReplayChatCompletionClient(script)
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            for index, message in enumerate(requests):
                result = await engine.chat(message, [], 'CUST001')
                assert result['response'] == 'Your keyboard draft is ready for review.'
                assert result['shopping_state']['current_draft']['order_id'] == f'SEMANTIC-{index}'
                assert engine._workbench.tool_calls == 1
                assert not engine._workbench.action_blocks
            # One planning call and one answer call per turn; no separate
            # classification/authorization model request was introduced.
            assert len(replay.create_calls) == 2 * len(requests)
        finally:
            await engine.close()

    asyncio.run(run())
    with closing(connect(readonly=True)) as conn:
        rows = conn.execute('SELECT order_status,order_invoice FROM orders').fetchall()
        assert len(rows) == len(requests)
        assert all(row['order_status'] == 'draft' and row['order_invoice'] is None for row in rows)
        assert conn.execute("SELECT availablequantity FROM items WHERE item_code='ITEM001'").fetchone()[0] == 40


def test_informational_model_answer_does_not_trigger_an_order_shortcut(monkeypatch):
    questions = [
        'How does requesting an order work?',
        'Do not start a cart; explain what a draft means.',
        'What would happen if I bought the keyboard later?',
    ]
    replay = ReplayChatCompletionClient([answer('A draft is an unplaced estimate.') for _ in questions])
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            for message in questions:
                result = await engine.chat(message, [], 'CUST001')
                assert result['response'] == 'A draft is an unplaced estimate.'
                assert engine._workbench.tool_calls == 0
            assert len(replay.create_calls) == len(questions)
        finally:
            await engine.close()

    asyncio.run(run())
    with closing(connect(readonly=True)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0


@pytest.mark.parametrize('owner,quantity', [(None, 1), ('CUST002', 1), ('CUST001', 0),
                                         ('CUST001', True), ('CUST001', 41)])
def test_order_domain_rules_still_reject_invalid_scope_quantity_or_stock(owner, quantity):
    result = scoped_tool(request_order)('CUST001', [{'item_code': 'ITEM001', 'quantity': quantity}],
        order_id='INVALID-DRAFT', execution_mode='customer', execution_customer_id=owner)
    assert not result['ok'], result
    with closing(connect(readonly=True)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0
        assert conn.execute("SELECT availablequantity FROM items WHERE item_code='ITEM001'").fetchone()[0] == 40


def test_refund_pending_request_threshold_and_window_remain_backend_rules(monkeypatch):
    monkeypatch.setattr(settings, 'CASHIER_REFUND_LIMIT', '100.00')
    monkeypatch.setattr(settings, 'RETURN_WINDOW_DAYS', '30')
    draft = request_order('CUST001', [{'item_code': 'ITEM001', 'quantity': 2}], order_id='RETURN-TERMS')
    assert draft['ok'] and place_order_with_address(draft['order_id'], 'CUST001')['ok']
    refund = scoped_tool(process_refund)
    terms = dict(order_id=draft['order_id'], customer_id='CUST001', reason='Return', amount='120.00')
    no_request = refund(**terms, action='approve', confirmed=True, return_received=True, execution_mode='admin')
    assert not no_request['ok'] and 'pending customer refund request' in no_request['error']
    assert refund(**terms, execution_customer_id='CUST001')['ok']
    wrong_amount = refund(**{**terms, 'amount': '100.00'}, action='approve', confirmed=True,
        return_received=True, execution_mode='admin')
    assert not wrong_amount['ok']
    above_limit = refund(**terms, action='approve', confirmed=True, return_received=True, execution_mode='cashier')
    assert not above_limit['ok'] and 'Admin approval required' in above_limit['error']
    with transaction() as conn:
        conn.execute("UPDATE orders SET order_date=date('now','-31 days') WHERE order_id=?", (draft['order_id'],))
    expired = refund(**terms, action='approve', confirmed=True, return_received=True, execution_mode='admin')
    assert not expired['ok'] and 'return window' in expired['error']
    with closing(connect(readonly=True)) as conn:
        assert conn.execute('SELECT refund_amount FROM orders').fetchone()[0] is None
        assert conn.execute('SELECT status FROM refund_requests').fetchone()[0] == 'pending'


def test_live_orchestrator_has_no_lexical_conversation_gate():
    source = Path(module.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    assert not (settings.ROOT / 'agent/action_policy.py').exists()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != 'agent.action_policy'
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name not in {'action_error', '_draft_requested', '_affirmative'}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == 're':
                assert node.func.attr not in {'match', 'fullmatch', 'search', 'findall', 'finditer', 'split', 'sub'}
