"""One semantic approval executes an unchanged reviewed order, without phrase gates.

Replay controls the model's decision only. SQLite, MCP, proposal checks and invoice
placement use production code; these checks do not measure live Azure semantics.
"""
import asyncio
from contextlib import closing
from copy import deepcopy
import json
from uuid import uuid4

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from address_support import prepare_delivery
from agent import billing_agent as module
from context.shopping_state import ShoppingTracker, order_snapshot
from database.addresses import list_addresses, save_address, select_order_address
from database.connection import connect, transaction
from mcp_server.tools.billing_tools import request_order


def answer(reply, **update):
    return json.dumps({'reply': reply, 'shopping_update': update})


def call(tool, **arguments):
    return CreateResult(finish_reason='function_calls', content=[FunctionCall(
        id=str(uuid4()), name=tool, arguments=json.dumps(arguments))],
        usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)


def reviewed_order(order_id='CONFIRM-ONCE', *, arguments=None, visible=None):
    created = request_order('CUST001', [{'item_code':'ITEM001','quantity':1}], order_id)
    assert created['ok'], created
    prepare_delivery(order_id, 'CUST001')
    tracker = ShoppingTracker(None, 'CUST001')
    summary = f'Place one keyboard in {order_id} for the current total of 79.90 USD?'
    proposal = {'action':'place_order', 'order_id':order_id, 'summary':summary}
    if arguments is not None:
        proposal['arguments'] = arguments
    shown = tracker.finish(answer(visible or summary, active_draft_id=order_id,
                                 pending_confirmation=proposal))
    return tracker, shown, summary


def scalar(query, parameters=()):
    with closing(connect(readonly=True)) as conn:
        return conn.execute(query, parameters).fetchone()[0]


@pytest.mark.parametrize('metadata', [
    {'items':[{'item_code':'ITEM001','quantity':1}]},
    {'amount':'79.90','currency':'USD'},
    {'subtotal':'79.90','tax_amount':'0.00','shipping_amount':'0.00'},
])
def test_display_metadata_is_not_a_second_editable_placement_argument_gate(metadata):
    tracker, _, _ = reviewed_order(arguments=metadata)
    proposal = tracker.state['pending_confirmation']
    assert proposal
    proposal['arguments'] = metadata  # Previously persisted proposals must also remain executable.
    error = module.customer_terms_error('place_order',
        {'order_id':'CONFIRM-ONCE','customer_id':'CUST001'}, proposal)
    assert error is None
    assert module.customer_terms_error('place_order',
        {'order_id':'OTHER-ORDER','customer_id':'CUST001'}, proposal) is not None


@pytest.mark.parametrize('message', [
    'Those details are correct. Please go ahead with the purchase.',
    'I confirm the quoted order and delivery details.',
])
def test_one_approval_places_reviewed_order_despite_redundant_quote_metadata(monkeypatch, message):
    tracker, visible, _ = reviewed_order(arguments={'amount':'79.90','currency':'USD',
        'items':[{'item_code':'ITEM001','quantity':1}]})
    assert tracker.state['pending_confirmation']
    replay = ReplayChatCompletionClient([
        call('place_order', order_id='CONFIRM-ONCE', customer_id='CUST001'),
        answer('Your order was placed and its invoice was issued.'),
    ])
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)
    dispatched = []

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            result = await engine.chat(message, [{'role':'assistant','content':visible}], 'CUST001',
                shopping_state=tracker.state, mutation_callback=lambda:dispatched.append(True))
            assert engine._workbench.tool_calls == 1
            assert not engine._workbench.action_blocks
            assert result['shopping_state']['pending_confirmation'] is None
            assert result['shopping_state']['current_draft'] is None
            assert result['shopping_state']['last_order']['status'] == 'placed'
            assert result['response'] == 'Your order was placed and its invoice was issued.'
            assert len(replay.create_calls) == 2  # Planning and reflection; no classifier.
            assert dispatched == [True]
        finally:
            await engine.close()
    asyncio.run(run())
    assert scalar("SELECT order_status FROM orders WHERE order_id='CONFIRM-ONCE'") == 'placed'
    assert scalar("SELECT COUNT(*) FROM invoice_lines WHERE order_id='CONFIRM-ONCE'") == 1
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == 39


def test_structured_proposal_remains_reviewable_when_model_paraphrases_its_summary():
    tracker, visible, _ = reviewed_order(visible='Here is the current keyboard order. Shall I submit it?')
    proposal = tracker.state['pending_confirmation']
    assert proposal is not None
    assert proposal['summary'] in visible
    current = order_snapshot('CONFIRM-ONCE', 'CUST001')
    assert proposal['order_fingerprint'] == current['fingerprint']
    assert current['delivery_address']['address_line1'] in visible
    assert scalar("SELECT order_status FROM orders WHERE order_id='CONFIRM-ONCE'") == 'draft'


def test_placement_review_uses_current_database_terms_instead_of_wrong_model_metadata():
    tracker, visible, _ = reviewed_order(arguments={
        'amount':'1.00', 'currency':'EUR', 'items':[{'item_code':'ITEM002','quantity':5}]},
        visible='Place five mice for 1.00 EUR?')
    proposal = tracker.state['pending_confirmation']
    current = order_snapshot('CONFIRM-ONCE', 'CUST001')
    assert proposal is not None
    assert proposal['summary'] in visible
    assert '79.90' in visible and 'USD' in visible
    assert 'keyboard' in visible.casefold()
    assert 'five mice' not in visible and '1.00 EUR' not in visible
    assert not proposal.get('arguments')
    assert proposal['order_fingerprint'] == current['fingerprint']
    assert proposal['delivery']['delivery_address'] == current['delivery_address']
    assert scalar("SELECT order_status FROM orders WHERE order_id='CONFIRM-ONCE'") == 'draft'


def test_completed_placement_cannot_be_displayed_as_another_confirmation(monkeypatch):
    tracker, visible, _ = reviewed_order()
    summary = 'Please confirm this order once again.'
    replay = ReplayChatCompletionClient([
        call('place_order', order_id='CONFIRM-ONCE', customer_id='CUST001'),
        answer(summary, pending_confirmation={'action':'place_order',
            'order_id':'CONFIRM-ONCE', 'summary':summary}),
    ])
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            result = await engine.chat('Please proceed.', [{'role':'assistant','content':visible}],
                                       'CUST001', shopping_state=tracker.state)
            invoice = scalar("SELECT order_invoice FROM orders WHERE order_id='CONFIRM-ONCE'")
            assert invoice
            assert engine._workbench.tool_calls == 1
            assert result['shopping_state']['pending_confirmation'] is None
            assert summary not in result['response']
            assert invoice in result['response']
            assert 'placed' in result['response']
            assert len(replay.create_calls) == 2
        finally:
            await engine.close()
    asyncio.run(run())
    assert scalar("SELECT COUNT(*) FROM invoice_lines WHERE order_id='CONFIRM-ONCE'") == 1


@pytest.mark.parametrize('first_step', ['same_address','read'])
def test_reviewed_placement_survives_unchanged_address_selection_or_a_read(monkeypatch, first_step):
    tracker, visible, _ = reviewed_order()
    reviewed = tracker.state['pending_confirmation']
    if first_step == 'same_address':
        first = call('manage_draft', action='select_address', order_id='CONFIRM-ONCE',
                     customer_id='CUST001', address_id=reviewed['delivery']['address_id'])
    else:
        first = call('execute_sql_query', query='SELECT order_id,order_status,charged_price FROM orders WHERE order_id=?',
                     parameters=['CONFIRM-ONCE'])
    replay = ReplayChatCompletionClient([first,
        call('place_order', order_id='CONFIRM-ONCE', customer_id='CUST001'),
        answer('Your reviewed order was placed.'),
    ])
    monkeypatch.setattr(module, 'create_model_client', lambda:replay)
    changes = []

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            result = await engine.chat('Please proceed with the reviewed order.',
                [{'role':'assistant','content':visible}], 'CUST001', shopping_state=tracker.state,
                mutation_callback=lambda:changes.append(True))
            assert engine._workbench.tool_calls == 2
            assert not engine._workbench.action_blocks
            assert result['shopping_state']['last_order']['status'] == 'placed'
            assert result['shopping_state']['pending_confirmation'] is None
            assert result['response'] == 'Your reviewed order was placed.'
            assert len(replay.create_calls) == 3
            assert changes == [True]
        finally:
            await engine.close()
    asyncio.run(run())
    assert scalar("SELECT COUNT(*) FROM invoice_lines WHERE order_id='CONFIRM-ONCE'") == 1
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == 39


@pytest.mark.parametrize('change', ['amount','address'])
def test_changed_reviewed_order_still_needs_fresh_approval(monkeypatch, change):
    tracker, visible, _ = reviewed_order(arguments={'amount':'79.90','currency':'USD'})
    saved = deepcopy(tracker.state)
    if change == 'amount':
        with transaction() as conn:
            conn.execute("UPDATE orders SET tax_amount=1,charged_price=80.90 WHERE order_id='CONFIRM-ONCE'")
    else:
        fields = {key:value for key,value in list_addresses('CUST001')[0].items()
                  if key not in ('address_id','customer_id','active','is_default')}
        fields.update(label='Work', address_line1='80 New Street')
        address = save_address('CUST001', fields)
        select_order_address('CONFIRM-ONCE', 'CUST001', address['address_id'])
    replay = ReplayChatCompletionClient([
        call('place_order', order_id='CONFIRM-ONCE', customer_id='CUST001'), answer('Placed.'),
    ])
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            result = await engine.chat('Please proceed with those details.',
                [{'role':'assistant','content':visible}], 'CUST001', shopping_state=saved)
            assert engine._workbench.tool_calls == 0
            assert engine._workbench.action_blocks
            assert result['shopping_state']['pending_confirmation'] is None
        finally:
            await engine.close()
    asyncio.run(run())
    assert scalar("SELECT order_status FROM orders WHERE order_id='CONFIRM-ONCE'") == 'draft'
    assert scalar("SELECT COUNT(*) FROM invoice_lines WHERE order_id='CONFIRM-ONCE'") == 0
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == 40


@pytest.mark.parametrize('message', [
    'No, please leave it as a draft.',
    'Change the order to two keyboards before I decide.',
    'Before placing anything, explain the return window.',
])
def test_pending_proposal_never_executes_without_a_model_tool_decision(monkeypatch, message):
    tracker, visible, _ = reviewed_order()
    replay = ReplayChatCompletionClient([answer('The order is still a draft.')])
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            result = await engine.chat(message, [{'role':'assistant','content':visible}], 'CUST001',
                                       shopping_state=tracker.state)
            assert engine._workbench.tool_calls == 0
            assert result['shopping_state']['pending_confirmation'] is None
            assert len(replay.create_calls) == 1
        finally:
            await engine.close()
    asyncio.run(run())
    assert scalar("SELECT order_status FROM orders WHERE order_id='CONFIRM-ONCE'") == 'draft'
