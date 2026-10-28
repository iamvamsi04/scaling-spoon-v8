"""Structured approvals and domain rules; chat phrasing is interpreted by the model."""
import asyncio
from contextlib import asynccontextmanager, closing
import json
from uuid import uuid4

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from agent import billing_agent as module
from context.shopping_state import ShoppingTracker
from context import context_manager as context
from database.connection import connect, transaction
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import request_order, place_order, process_refund


def call(name, **arguments):
    return CreateResult(content=[FunctionCall(id=str(uuid4()), name=name,
        arguments=json.dumps(arguments))], finish_reason='function_calls',
        usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)


def answer(text):
    return json.dumps({'reply': text, 'shopping_update': {}})


@asynccontextmanager
async def running_engine(monkeypatch, script):
    replay = ReplayChatCompletionClient(script)
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)
    engine = module.BillingAgent()
    await engine.start()
    try:
        yield engine, replay
    finally:
        await engine.close()


def scalar(query, parameters=()):
    with closing(connect()) as conn:
        return conn.execute(query, parameters).fetchone()[0]


def order(order_id, placed=False, item_code='ITEM001'):
    draft = request_order('CUST001', [{'item_code': item_code, 'quantity': 1}], order_id)
    assert draft['ok'], draft
    if placed:
        result = place_order(order_id, 'CUST001')
        assert result['ok'], result
        return result['invoice_number']
    return order_id


def pending(order_id, action='place_order', arguments=None, summary=None):
    tracker = ShoppingTracker(None, 'CUST001')
    summary = summary or f'Place order {order_id}: 1 ITEM001, subtotal and total USD 79.90, with no discount, tax or shipping?'
    proposal = {'action': action, 'order_id': order_id, 'summary': summary}
    if arguments is not None:
        proposal['arguments'] = arguments
    visible = tracker.finish(json.dumps({'reply': summary,
        'shopping_update': {'pending_confirmation': proposal}}))
    assert tracker.state['pending_confirmation'], visible
    return tracker.state, [{'role': 'user', 'content': 'Please prepare those details.'},
                           {'role': 'assistant', 'content': visible}]


def request_refund(order_id, amount='10.00', reason='Return'):
    result = scoped_tool(process_refund)(order_id, 'CUST001', reason, amount,
        action='request', execution_customer_id='CUST001')
    assert result['ok'], result


def staff_review(order_id, amount='20.00', reason='Return'):
    return json.dumps({'reply': f'Review the stored {amount} USD return for {order_id}.',
        'pending_staff_action': {'action': 'approve', 'order_id': order_id,
            'arguments': {'amount': amount, 'reason': reason, 'return_received': True}}})




def test_one_saved_approval_places_once_and_cannot_authorize_a_second_change(monkeypatch):
    order('AUTH-ONCE')
    state, history = pending('AUTH-ONCE')
    script = [call('place_order', order_id='AUTH-ONCE', customer_id='CUST001'),
        call('manage_draft', order_id='AUTH-ONCE', customer_id='CUST001',
             action='cancel', reason='Changed mind', confirmed=True),
        answer('Your order was cancelled.')]

    async def run():
        async with running_engine(monkeypatch, script) as (engine, replay):
            result = await engine.chat('Yes.', history, 'CUST001', shopping_state=state)
            assert engine._workbench.tool_calls == 1
            assert len(replay.create_calls) == 3
            assert 'Order AUTH-ONCE was placed.' in result['response']
            assert 'No additional change was executed.' in result['response']
            assert 'Your order was cancelled.' not in result['response']
            assert result['shopping_state']['pending_confirmation'] is None
    asyncio.run(run())
    assert scalar("SELECT order_status FROM orders WHERE order_id='AUTH-ONCE'") == 'placed'
    assert scalar("SELECT COUNT(*) FROM invoice_lines WHERE order_id='AUTH-ONCE'") == 1
    assert scalar("SELECT availablequantity FROM items WHERE item_code='ITEM001'") == 39
    assert scalar('SELECT COUNT(*) FROM refund_requests') == 0


def test_changed_quote_cannot_reuse_a_saved_structured_approval(monkeypatch):
    order('AUTH-STALE')
    state, history = pending('AUTH-STALE')
    with transaction() as conn:
        conn.execute("UPDATE orders SET tax_amount=1,charged_price=80.90 WHERE order_id='AUTH-STALE'")
    script = [call('place_order', order_id='AUTH-STALE', customer_id='CUST001'), answer('Placed.')]

    async def run():
        async with running_engine(monkeypatch, script) as (engine, _):
            result = await engine.chat('Those terms suit me; proceed.', history, 'CUST001', shopping_state=state)
            assert engine._workbench.tool_calls == 0
            assert 'No change was executed.' in result['response']
            assert result['shopping_state']['pending_confirmation'] is None
    asyncio.run(run())
    assert scalar("SELECT order_invoice FROM orders WHERE order_id='AUTH-STALE'") is None




def test_refund_request_is_bound_to_the_exact_explicit_approved_amount(monkeypatch):
    plans = []
    for oid in ('AUTH-RETURN-OK', 'AUTH-RETURN-MISSING', 'AUTH-RETURN-CHANGED'):
        order(oid, placed=True)
        args = {'reason': 'Return', 'amount': '10.00', 'disposition': 'original_method'}
        state, history = pending(oid, 'process_refund', args,
            f'Request a pending USD 10.00 refund for {oid}, reason Return, to the original method? This records a request only.')
        plans.append((oid, state, history, args))
    script = []
    for index, (oid, _, _, args) in enumerate(plans):
        actual = dict(args)
        if index == 1:
            actual.pop('amount')
        elif index == 2:
            actual['amount'] = '11.00'
        script += [call('process_refund', order_id=oid, customer_id='CUST001',
                        action='request', **actual),
                   answer('The pending refund request was submitted.')]

    async def run():
        async with running_engine(monkeypatch, script) as (engine, _):
            for index, (_, state, history, _) in enumerate(plans):
                result = await engine.chat('Yes.', history, 'CUST001', shopping_state=state)
                assert engine._workbench.tool_calls == (1 if index == 0 else 0)
                if index:
                    assert 'No change was executed.' in result['response']
                    assert 'was submitted' not in result['response']
    asyncio.run(run())
    with closing(connect()) as conn:
        rows = conn.execute('SELECT order_id,amount,status FROM refund_requests').fetchall()
        assert [tuple(row) for row in rows] == [('AUTH-RETURN-OK', 10, 'pending')]
        assert conn.execute('SELECT COUNT(*) FROM orders WHERE refund_amount IS NOT NULL').fetchone()[0] == 0


def test_invoice_correction_binds_the_invoice_to_the_refreshed_owned_proposal(monkeypatch):
    invoice = order('AUTH-INVOICE-A', placed=True)
    order('AUTH-INVOICE-B', placed=True)
    valid_args = {'invoice_number': invoice, 'billing_address': 'New Road 10'}
    valid, valid_history = pending('AUTH-INVOICE-A', 'correct_invoice', valid_args,
        f'Correct the billing address on invoice {invoice} to New Road 10 without changing its prices?')
    # Both invoices belong to the same account. Binding only ownership or the
    # model's invoice argument would still let a proposal target the wrong order.
    bad_args = {'invoice_number': invoice, 'billing_address': 'Wrong Road 20'}
    wrong, wrong_history = pending('AUTH-INVOICE-B', 'correct_invoice', bad_args,
        f'Correct invoice {invoice} to Wrong Road 20?')
    script = []
    for args in (valid_args, bad_args):
        script += [call('download_invoice', customer_id='CUST001',
                        action='correct_details', confirmed=True, **args),
                   answer('The invoice contact details were corrected.')]

    async def run():
        async with running_engine(monkeypatch, script) as (engine, _):
            first = await engine.chat('Yes.', valid_history, 'CUST001', shopping_state=valid)
            assert engine._workbench.tool_calls == 1 and first['invoices']
            second = await engine.chat('Yes.', wrong_history, 'CUST001', shopping_state=wrong)
            assert engine._workbench.tool_calls == 0 and not second['invoices']
            assert 'No change was executed.' in second['response']
    asyncio.run(run())
    assert scalar("SELECT COUNT(*) FROM invoice_revisions WHERE order_id='AUTH-INVOICE-A'") == 1
    assert scalar("SELECT COUNT(*) FROM invoice_revisions WHERE order_id='AUTH-INVOICE-B'") == 0


def test_explicit_staff_approval_still_obeys_pending_request_and_cashier_limit(monkeypatch):
    for oid in ('AUTH-STAFF-OK', 'AUTH-NO-REQUEST', 'AUTH-STAFF-HIGH'):
        order(oid, placed=True, item_code='ITEM003')
    request_refund('AUTH-STAFF-OK')
    request_refund('AUTH-STAFF-HIGH', '100.01')
    cases = [('AUTH-STAFF-OK', '10.00', 'cashier', True),
             ('AUTH-NO-REQUEST', '10.00', 'admin', False),
             ('AUTH-STAFF-HIGH', '100.01', 'cashier', False),
             ('AUTH-STAFF-HIGH', '100.01', 'admin', True)]
    script = []
    for oid, amount, _, success in cases:
        script += [call('process_refund', order_id=oid, amount=amount,
                        reason='Return', action='approve', return_received=True),
                   'The refund was approved in the local ledger.']

    async def run():
        async with running_engine(monkeypatch, script) as (engine, _):
            for oid, _, role, success in cases:
                result = await engine.chat(f'Approve the pending refund for {oid}. The returned goods were received.',
                    [], None, mode=role)
                assert engine._workbench.tool_calls == 1
                assert bool(engine._workbench.receipts) == success
                assert ('approved in the local ledger' in result['response']) == success
    asyncio.run(run())
    with closing(connect()) as conn:
        rows = conn.execute('SELECT order_id,status,approved_by_role FROM refund_requests ORDER BY order_id').fetchall()
        assert [tuple(row) for row in rows] == [
            ('AUTH-STAFF-HIGH', 'approved', 'admin'), ('AUTH-STAFF-OK', 'approved', 'cashier')]
        assert conn.execute("SELECT refund_amount FROM orders WHERE order_id='AUTH-NO-REQUEST'").fetchone()[0] is None


def test_staff_confirmation_uses_stored_terms_persists_and_expires_after_approval(monkeypatch):
    order('AUTH-STAFF-CONTEXT', placed=True)
    request_refund('AUTH-STAFF-CONTEXT', '20.00', 'Projector X-200 failed')
    script = [staff_review('AUTH-STAFF-CONTEXT', reason='Projector X-200 failed'),
        call('process_refund', order_id='AUTH-STAFF-CONTEXT',
             action='approve', amount='20.00', reason='Projector X-200 failed',
             return_received=True),
        'Approved USD 20.00 in the local ledger.']

    async def run():
        async with running_engine(monkeypatch, script) as (engine, _):
            first = await engine.chat('Review whether to approve the pending refund.', [], None, mode='cashier')
            assert '20.00 USD' in first['response']
            assert first['pending_staff_action']['arguments']['amount'] == '20.00'
            assert first['pending_staff_action']['arguments']['return_received'] is True
            cid, rid = context.conversation(mode='cashier'), str(uuid4())
            context.begin(cid, rid, 'Review whether to approve the pending refund.')
            context.finish(cid, rid, first)
            saved = context.latest_action_context(cid)
            assert saved == first['pending_staff_action']
            second = await engine.chat('Yes, received.', context.history(cid), None, mode='cashier',
                                       action_context=saved)
            assert engine._workbench.tool_calls == 1
            assert 'Approved USD 20.00' in second['response']
            assert second['pending_staff_action'] is None
            rid = str(uuid4())
            context.begin(cid, rid, 'Yes, received.')
            context.finish(cid, rid, second)
            assert context.latest_action_context(cid) is None
    asyncio.run(run())
    assert scalar("SELECT amount FROM refund_requests WHERE order_id='AUTH-STAFF-CONTEXT'") == 20
    assert scalar("SELECT approved_by_role FROM refund_requests WHERE order_id='AUTH-STAFF-CONTEXT'") == 'cashier'




@pytest.mark.parametrize('intervening_status', ['failed', 'pending'])
def test_intervening_staff_turn_expires_old_approval_context(intervening_status):
    order('AUTH-STAFF-EXPIRES', placed=True)
    request_refund('AUTH-STAFF-EXPIRES', '20.00')
    visible, proposal = module.staff_confirmation(
        staff_review('AUTH-STAFF-EXPIRES'))
    assert proposal and '20.00 USD' in visible
    cid = context.conversation(mode='cashier')
    first = str(uuid4())
    context.begin(cid, first, 'Review the pending refund.')
    context.finish(cid, first, {'response': visible, 'pending_staff_action': proposal})
    assert context.latest_action_context(cid) == proposal
    intervening = str(uuid4())
    context.begin(cid, intervening, 'Please wait; I need to review something else.')
    if intervening_status == 'failed':
        context.fail(cid, intervening)
    current = str(uuid4())
    context.begin(cid, current, 'Yes, received.')
    assert context.latest_action_context(cid, exclude_request_id=current) is None
    assert context.latest_action_context(cid) is None


def test_staff_proposal_is_read_from_structured_fields_not_question_words():
    order('AUTH-STAFF-STRUCTURED', placed=True)
    request_refund('AUTH-STAFF-STRUCTURED', '20.00')
    visible, proposal = module.staff_confirmation(staff_review('AUTH-STAFF-STRUCTURED'))
    assert proposal and proposal['order_id'] == 'AUTH-STAFF-STRUCTURED'
    assert proposal['arguments']['amount'] == '20.00'
    prose = 'Would you like me to approve the pending refund for AUTH-STAFF-STRUCTURED?'
    unchanged, missing = module.staff_confirmation(prose)
    assert unchanged == prose and missing is None


def test_legacy_pending_refund_without_details_uses_original_method():
    order('AUTH-STAFF-LEGACY', placed=True)
    with transaction() as conn:
        conn.execute('INSERT INTO refund_requests(order_id,amount,currency,reason) VALUES (?,?,?,?)',
                     ('AUTH-STAFF-LEGACY','20.00','USD','Return'))
    content = json.dumps({'reply':'Review the stored refund terms.',
        'pending_staff_action': {'action':'approve','order_id':'AUTH-STAFF-LEGACY',
            'arguments':{'return_received':True}}})
    visible, proposal = module.staff_confirmation(content,mode='cashier')
    assert proposal is not None
    assert proposal['arguments']['amount'] == '20.00'
    assert proposal['arguments']['disposition'] == 'original_method'
    assert '20.00' in visible


def test_changed_staff_refund_terms_need_fresh_review_even_with_omitted_arguments(monkeypatch):
    order('AUTH-STALE-STAFF',placed=True)
    request_refund('AUTH-STALE-STAFF','20.00')
    _, reviewed = module.staff_confirmation(staff_review('AUTH-STALE-STAFF'))
    with transaction() as conn:
        conn.execute('UPDATE refund_requests SET amount=30 WHERE order_id=?',('AUTH-STALE-STAFF',))
    script = [call('process_refund',order_id='AUTH-STALE-STAFF',action='approve'),
              staff_review('AUTH-STALE-STAFF',amount='30.00'),
              call('process_refund',order_id='AUTH-STALE-STAFF',action='approve'),
              'The reviewed 30 USD refund is recorded.']
    async def run():
        async with running_engine(monkeypatch,script) as (engine,_):
            blocked = await engine.chat('Those terms are approved.',[],None,mode='cashier',action_context=reviewed)
            assert engine._workbench.tool_calls == 0
            assert 'terms changed' in blocked['response']
            assert blocked['pending_staff_action']['arguments']['amount'] == '30.00'
            assert scalar("SELECT status FROM refund_requests WHERE order_id='AUTH-STALE-STAFF'") == 'pending'
            accepted = await engine.chat('The updated amount is fine.',[],None,mode='cashier',
                                         action_context=blocked['pending_staff_action'])
            assert engine._workbench.tool_calls == 1
            assert accepted['pending_staff_action'] is None
    asyncio.run(run())
    assert scalar("SELECT refund_amount FROM orders WHERE order_id='AUTH-STALE-STAFF'") == 30


def test_matching_staff_proposal_cannot_change_reviewed_receipt_or_waiver(monkeypatch):
    order('AUTH-STAFF-ARGUMENTS',placed=True)
    request_refund('AUTH-STAFF-ARGUMENTS','20.00')
    _, reviewed = module.staff_confirmation(staff_review('AUTH-STAFF-ARGUMENTS'))
    script = [call('process_refund',order_id='AUTH-STAFF-ARGUMENTS',action='approve',
                   return_received=False,waiver_reason='A different assessment'),
              'The refund was approved.']
    async def run():
        async with running_engine(monkeypatch,script) as (engine,_):
            blocked = await engine.chat('Approve the reviewed terms.',[],None,mode='admin',action_context=reviewed)
            assert engine._workbench.tool_calls == 0
            assert 'arguments differ' in blocked['response']
    asyncio.run(run())
    assert scalar("SELECT status FROM refund_requests WHERE order_id='AUTH-STAFF-ARGUMENTS'") == 'pending'
