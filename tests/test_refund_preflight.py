"""Refund approval checks saved customer requests before any ledger mutation."""
from contextlib import closing
import asyncio
import json
from uuid import uuid4

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from address_support import place_order_with_address
from config import settings
from database.connection import connect, transaction
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import request_order, process_refund
from mcp_server.tools.operations_tools import manage_draft


refund = scoped_tool(process_refund)


def order(customer='CUST001', quantity=1):
    created = request_order(customer, [{'item_code':'ITEM001','quantity':quantity}])
    assert created['ok'], created
    placed = place_order_with_address(created['order_id'], customer)
    assert placed['ok'], placed
    return created['order_id']


def requested(oid, **arguments):
    return refund(oid,'CUST001','Return',execution_customer_id='CUST001',**arguments)


def status(oid, role='admin'):
    result = refund(oid,'CUST001',action='status',execution_mode=role)
    assert result['ok'], result
    return result['approval_eligibility']


def approve(oid, role='admin', reason='Return', **arguments):
    return refund(oid,'CUST001',reason,action='approve',confirmed=True,execution_mode=role,**arguments)


def snapshot():
    with closing(connect()) as conn:
        return {table:conn.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()
                for table in ('orders','invoice_lines','order_fulfilled','items','refund_requests',
                              'refund_details','store_credit_ledger','billing_audit')}


@pytest.mark.parametrize('amount', [None,'10.00','87.89'])
def test_shipping_order_has_valid_merchandise_return_and_admin_can_approve(amount):
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='10' WHERE key IN ('shipping_amount','tax_rate')")
    oid = order()
    arguments = {'amount':amount} if amount is not None else {}
    result = requested(oid, **arguments)
    assert result['ok'], result
    before = snapshot()
    checked = status(oid)
    assert checked['valid'] and not checked['ready_to_approve']
    assert checked['nonrefundable_shipping'] == '10.00'
    assert checked['eligible_amount'] == '87.89'
    assert checked['return_receipt_required']
    assert snapshot() == before
    approved = approve(oid,return_received=True)
    assert approved['ok'] and approved['refund_amount'] == result['requested_amount']


def test_admin_preflight_rejects_pending_return_including_shipping_without_changing_it():
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='10' WHERE key='shipping_amount'")
    oid = order()
    assert requested(oid,amount='79.90')['ok']
    with transaction() as conn:
        conn.execute('UPDATE refund_requests SET amount=89.90 WHERE order_id=?',(oid,))
    before = snapshot()
    checked = status(oid)
    assert not checked['valid'] and not checked['ready_to_approve']
    assert checked['eligible_amount'] == '79.90'
    assert 'shipping is nonrefundable' in checked['reasons'][0]
    result = approve(oid,return_received=True)
    assert not result['ok'] and result['error'] == checked['reasons'][0]
    assert snapshot() == before


def test_whole_unshipped_cancellation_can_refund_original_shipping_charges():
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='10' WHERE key='shipping_amount'")
    oid = order()
    cancelled = manage_draft(oid,'CUST001','cancel',reason='Cancel my order',confirmed=True)
    assert cancelled['ok'], cancelled
    checked = status(oid)
    assert checked['valid'] and checked['ready_to_approve']
    assert checked['request_kind'] == 'cancellation'
    assert checked['eligible_amount'] == '89.90'
    assert checked['nonrefundable_shipping'] == '0.00'
    result = approve(oid,reason='Cancel my order')
    assert result['ok'] and result['refund_amount'] == '89.90'


def test_staff_preflight_explains_missing_customer_request_without_creating_one():
    oid = order()
    before = snapshot()
    result = refund(oid,'CUST001',action='status',execution_mode='admin')
    assert result['ok'] and result['local_status'] == 'not_requested'
    assert not result['approval_eligibility']['valid']
    assert 'pending customer refund request' in result['approval_eligibility']['reasons'][0]
    assert not approve(oid,return_received=True)['ok']
    assert snapshot() == before


def test_staff_preflight_keeps_customer_read_behavior_private():
    oid = order()
    assert not refund(oid,'CUST001',action='status',execution_customer_id='CUST001')['ok']
    assert requested(oid,amount='10')['ok']
    customer = refund(oid,'CUST001',action='status',execution_customer_id='CUST001')
    assert customer['ok'] and 'approval_eligibility' not in customer


@pytest.mark.parametrize('case', ['expired','invoice_changed','duplicate','different_currency','invalid_items'])
def test_preflight_and_actual_approval_reject_same_invalid_saved_request(case,monkeypatch):
    monkeypatch.setattr(settings,'RETURN_WINDOW_DAYS','30')
    oid = order()
    assert requested(oid,amount='10')['ok']
    with transaction() as conn:
        if case == 'expired':
            conn.execute("UPDATE orders SET order_date=date('now','-31 days') WHERE order_id=?",(oid,))
        elif case == 'invoice_changed':
            conn.execute('UPDATE invoice_lines SET line_total=1 WHERE order_id=?',(oid,))
        elif case == 'duplicate':
            conn.execute("UPDATE orders SET refund_amount=10,refund_status='recorded' WHERE order_id=?",(oid,))
        elif case == 'different_currency':
            conn.execute("UPDATE refund_requests SET currency='EUR' WHERE order_id=?",(oid,))
        else:
            conn.execute('UPDATE refund_details SET items_json=? WHERE order_id=?',(json.dumps([{}]),oid))
    before = snapshot()
    checked = status(oid)
    assert not checked['valid'] and checked['reasons']
    result = approve(oid,return_received=True)
    assert not result['ok'] and result['error'] == checked['reasons'][0]
    assert snapshot() == before


def test_cashier_limit_is_checked_before_approval_but_admin_may_approve_valid_larger_request(monkeypatch):
    monkeypatch.setattr(settings,'CASHIER_REFUND_LIMIT','100.00')
    oid = order(quantity=2)
    assert requested(oid,amount='120')['ok']
    cashier = status(oid,'cashier')
    assert not cashier['valid'] and 'Admin approval required' in cashier['reasons'][0]
    assert not approve(oid,'cashier',return_received=True)['ok']
    assert status(oid,'admin')['valid']
    assert approve(oid,return_received=True)['ok']


def test_valid_request_still_needs_receipt_or_documented_admin_waiver():
    oid = order()
    assert requested(oid,amount='10')['ok']
    checked = status(oid)
    assert checked['valid'] and not checked['ready_to_approve']
    assert not approve(oid)['ok']
    assert not approve(oid,'cashier',waiver_reason='Assessed damaged packaging')['ok']
    assert approve(oid,waiver_reason='Assessed damaged packaging')['ok']


def test_preflight_rechecks_requested_items_against_original_billed_allocation():
    oid = order(quantity=2)
    assert requested(oid,items=[{'item_code':'ITEM001','quantity':1}])['ok']
    checked = status(oid)
    assert checked['valid'] and checked['eligible_amount'] == '79.90'
    with transaction() as conn:
        conn.execute('UPDATE refund_requests SET amount=80 WHERE order_id=?',(oid,))
    before = snapshot()
    checked = status(oid)
    assert not checked['valid']
    assert 'Billed return details changed' in checked['reasons'][0]
    assert not approve(oid,return_received=True)['ok']
    assert snapshot() == before


def test_normalized_currency_has_consistent_preflight_and_approval():
    oid = order()
    assert requested(oid,amount='10')['ok']
    with transaction() as conn:
        conn.execute("UPDATE refund_requests SET currency=' usd ' WHERE order_id=?",(oid,))
    assert status(oid)['valid']
    assert approve(oid,return_received=True)['ok']


def test_target_cart_of_other_customer_is_invalid_without_changing_credit_destination():
    oid = order()
    target = request_order('CUST001',[{'item_code':'ITEM001','quantity':1}])['order_id']
    other = request_order('CUST002',[{'item_code':'ITEM001','quantity':1}])['order_id']
    assert requested(oid,amount='10',disposition='store_credit',target_order_id=target)['ok']
    with transaction() as conn:
        conn.execute('UPDATE refund_details SET target_order_id=? WHERE order_id=?',(other,oid))
    before = snapshot()
    checked = status(oid)
    assert not checked['valid'] and 'same customer' in checked['reasons'][0]
    assert not approve(oid,return_received=True)['ok']
    assert snapshot() == before


@pytest.mark.parametrize('case,tool_count,approved', [
    ('valid_merchandise',1,True), ('includes_shipping',0,False),
    ('missing_request',0,False), ('wrong_amount',1,False),
    ('invalid_items',0,False),
])
def test_real_mcp_staff_approval_checks_eligibility_before_dispatch(monkeypatch,case,tool_count,approved):
    # The model decision is replayed; eligibility, ownership, MCP and SQLite run.
    from agent import billing_agent as agent
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='10' WHERE key='shipping_amount'")
    oid = order()
    if case != 'missing_request':
        assert requested(oid,amount='79.90')['ok']
    if case == 'includes_shipping':
        with transaction() as conn:
            conn.execute('UPDATE refund_requests SET amount=89.90 WHERE order_id=?',(oid,))
    elif case == 'invalid_items':
        with transaction() as conn:
            conn.execute("UPDATE refund_details SET items_json='[null]' WHERE order_id=?",(oid,))
    arguments = {'order_id':oid,'action':'approve','return_received':True}
    if case == 'wrong_amount':
        arguments['amount'] = '20.00'
    decision = CreateResult(content=[FunctionCall(id=str(uuid4()),name='process_refund',arguments=json.dumps(arguments))],
                            finish_reason='function_calls',usage=RequestUsage(prompt_tokens=0,completion_tokens=0),cached=False)
    replay = ReplayChatCompletionClient([decision,'This refund was approved.'])
    monkeypatch.setattr(agent,'create_model_client',lambda:replay)
    before = snapshot()
    dispatched = []

    async def run():
        engine = agent.BillingAgent()
        await engine.start()
        try:
            response = await engine.chat('Approve the pending return; I have received the returned goods.',
                [],None,mode='admin',mutation_callback=lambda:dispatched.append(True))
            assert engine._workbench.tool_calls == tool_count
            assert len(replay.create_calls) == 2
            assert response['pending_staff_action'] is None
            if not approved:
                assert 'This refund was approved.' not in response['response']
                assert engine._workbench.action_blocks
        finally:
            await engine.close()
    asyncio.run(run())
    assert len(dispatched) == tool_count
    if not approved:
        assert snapshot() == before
    else:
        with closing(connect()) as conn:
            assert conn.execute('SELECT refund_amount FROM orders WHERE order_id=?',(oid,)).fetchone()[0] == 79.9
            assert conn.execute('SELECT status FROM refund_requests WHERE order_id=?',(oid,)).fetchone()[0] == 'approved'
            assert conn.execute('SELECT COUNT(*) FROM billing_audit').fetchone()[0] == len(before['billing_audit']) + 1


def test_invalid_refund_is_rejected_before_persisting_a_staff_approval_proposal():
    from agent.billing_agent import staff_confirmation
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='10' WHERE key='shipping_amount'")
    oid = order()
    assert requested(oid,amount='79.90')['ok']
    with transaction() as conn:
        conn.execute('UPDATE refund_requests SET amount=89.90 WHERE order_id=?',(oid,))
    before = snapshot()
    reply, proposal = staff_confirmation(json.dumps({'reply':'Confirm the refund?',
        'pending_staff_action':{'action':'approve','order_id':oid,
                                'arguments':{'return_received':True}}}),'admin')
    assert proposal is None
    assert 'shipping is nonrefundable' in reply
    assert snapshot() == before


@pytest.mark.parametrize('request_exists', [False, True])
def test_invalid_approval_review_displays_the_rejection_instead_of_a_confirmation(monkeypatch, request_exists):
    import asyncio
    from autogen_ext.models.replay import ReplayChatCompletionClient
    from agent import billing_agent as agent
    with transaction() as conn:
        conn.execute("UPDATE billing_config SET value='10' WHERE key='shipping_amount'")
    oid = order()
    if request_exists:
        assert requested(oid, amount='79.90')['ok']
        with transaction() as conn:
            conn.execute('UPDATE refund_requests SET amount=89.90 WHERE order_id=?', (oid,))
    before = snapshot()
    replay = ReplayChatCompletionClient([json.dumps({'reply':'Please confirm this refund approval.',
        'pending_staff_action':{'action':'approve','order_id':oid,'arguments':{'return_received':True}}})])
    monkeypatch.setattr(agent, 'create_model_client', lambda: replay)

    async def run():
        engine = agent.BillingAgent()
        await engine.start()
        try:
            result = await engine.chat('Review the refund for this order.', [], None, mode='admin')
            assert result['pending_staff_action'] is None
            assert 'Please confirm this refund approval.' not in result['response']
            expected = 'shipping is nonrefundable' if request_exists else 'pending customer refund request is required'
            assert expected in result['response']
            assert engine._workbench.tool_calls == 0
            assert len(replay.create_calls) == 1
        finally:
            await engine.close()
    asyncio.run(run())
    assert snapshot() == before


@pytest.mark.parametrize('disposition', ['store_credit','original_method'])
def test_existing_credit_grant_blocks_another_refund_even_when_invoice_flags_are_missing(disposition):
    oid = order()
    assert requested(oid,amount='10',disposition=disposition)['ok']
    with transaction() as conn:
        conn.execute("INSERT INTO store_credit_ledger(customer_id,order_id,currency,amount,kind) VALUES ('CUST001',?,'USD',10,'refund_credit')",(oid,))
    before = snapshot()
    checked = status(oid)
    assert not checked['valid'] and 'already recorded' in checked['reasons'][0]
    assert not approve(oid,return_received=True)['ok']
    assert snapshot() == before
