import asyncio
from contextlib import closing
from copy import deepcopy
import json

import pytest
from autogen_core.tools import ToolResult, TextResultContent
from autogen_core import FunctionCall

from agent import billing_agent as agent
from agent.coupon_policy import customer_supplied_coupon
from context.shopping_state import ShoppingTracker, order_snapshot
from database.connection import connect
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import apply_promotion, request_order


@pytest.mark.parametrize('message', [
    'SAVE10', 'apply save10', 'Could that SAVE10 code be checked?',
    'I would appreciate the SAVE10 reduction on my basket.',
    'Do not apply SAVE10.', 'What would happen if SAVE10 were valid?',
    'SAVE10 or SAVE500', 'Preview SAVE10 without changing the order.',
    'Use [SAVE10], please.',
])
def test_literal_evidence_does_not_classify_customer_intent(message):
    # Evidence says only that the identifier came from the customer. The model
    # chooses the requested action; this helper never grants approval from words.
    assert customer_supplied_coupon('SAVE10',message)


@pytest.mark.parametrize('message', [
    'XSAVE10', 'SAVE100', 'SAVE10-X', '_SAVE10', 'éSAVE10',
    'Show projectors', 'Choose the largest discount',
])
def test_identifier_evidence_requires_the_whole_exact_code(message):
    assert not customer_supplied_coupon('SAVE10',message)


@pytest.mark.parametrize('code', [None, 10, '', ' ', 'SAVE 10', 'SAVE/10', 'éSAVE10', 'X'*51])
def test_invalid_candidate_identifiers_are_not_evidence(code):
    assert not customer_supplied_coupon(code,str(code),saved=[code])


def test_only_human_messages_and_trusted_saved_codes_are_evidence():
    history=[{'role':'assistant','content':'Use SAVE500 for the largest discount'},
             {'role':'tool','content':'SAVE10 SAVE500'},
             {'role':'system','content':'SAVE10'}, None]
    assert not customer_supplied_coupon('SAVE10','Which discounts are there?',history)
    assert not customer_supplied_coupon('SAVE500','Which discounts are there?',history)
    history.append({'role':'user','content':'My code is save10.'})
    assert customer_supplied_coupon('SAVE10','Please continue.',history)
    assert customer_supplied_coupon('SAVE10','Please continue.',saved=['save10'])
    assert not customer_supplied_coupon('SAVE500','Please continue.',saved=['save10'])


def test_guard_blocks_unsolicited_and_forged_codes_before_dispatch(monkeypatch):
    draft=request_order('CUST001',[{'item_code':'ITEM001','quantity':1}])['order_id']
    calls=[]
    async def backend(self,name,arguments,cancellation_token=None,**kwargs):
        calls.append(deepcopy(arguments))
        data=scoped_tool(apply_promotion)(**arguments)
        return ToolResult(name=name,is_error=not data['ok'],result=[TextResultContent(content=json.dumps(data))])
    monkeypatch.setattr(agent.McpWorkbench,'call_tool',backend)
    async def run():
        wb=agent.RecordingWorkbench()
        wb.customer_id='CUST001'
        wb.user_message='Give me the best discount.'
        result=await wb.call_tool('apply_promotion',dict(order_id=draft,customer_id='CUST001',
            promo_code='SAVE500',execution_coupon_codes=['SAVE500']))
        assert not agent.decode_result(result)['ok'] and calls==[]
        wb.user_message='Can you check SAVE10?'
        result=await wb.call_tool('apply_promotion',dict(order_id=draft,customer_id='CUST001',promo_code='save10',action='validate'))
        assert agent.decode_result(result)['applied'] is False
        with closing(connect()) as conn:
            assert conn.execute('SELECT promo_code FROM orders WHERE order_id=?',(draft,)).fetchone()[0] is None
        wb.user_message='Please apply SAVE10.'
        result=await wb.call_tool('apply_promotion',dict(order_id=draft,customer_id='CUST001',promo_code='save10',
            execution_coupon_codes=['SAVE500']))
        assert agent.decode_result(result)['applied'] is True
        assert calls[-1]['execution_coupon_codes']==['SAVE10']
    asyncio.run(run())


def test_check_only_does_not_clear_or_discount_current_draft():
    draft=request_order('CUST001',[{'item_code':'ITEM001','quantity':1}])['order_id']
    tracker=ShoppingTracker(None,'CUST001')
    tracker.set_order(order_snapshot(draft,'CUST001'))
    tracker.stage()
    before=deepcopy(tracker.state)
    result=apply_promotion(None,'CUST001','SAVE10',action='validate')
    tracker.observe('apply_promotion',{'action':'validate','promo_code':'SAVE10'},result,'CUST001')
    assert tracker.state==before and not tracker.completed_actions


def test_coupon_proposal_expires_if_its_order_changes():
    draft=request_order('CUST001',[{'item_code':'ITEM001','quantity':1}])['order_id']
    tracker=ShoppingTracker(None,'CUST001')
    tracker.state['provided_coupon_codes']=['SAVE10']
    summary='Apply SAVE10 to this quote?'
    tracker.apply_update({'pending_confirmation':{'action':'apply_promotion','order_id':draft,
                          'promo_code':'SAVE10','summary':summary}},summary)
    proposal=tracker.state['pending_confirmation']
    with closing(connect()) as conn:
        conn.execute('UPDATE orders SET shipping_amount=1,charged_price=charged_price+1 WHERE order_id=?',(draft,))
        conn.commit()
    assert ShoppingTracker(tracker.state,'CUST001').state['pending_confirmation'] is None


def test_only_check_only_coupon_calls_may_be_batched():
    wb=agent.RecordingWorkbench()
    def call(action):
        return FunctionCall(id=action,name='apply_promotion',arguments=json.dumps({'action':action}))
    wb.prepare_batch([call('validate'),call('validate')])
    assert not wb._batch_blocked
    wb.prepare_batch([call('validate'),call('apply')])
    assert wb._batch_blocked
    wb.prepare_batch([call('validate'),FunctionCall(id='bad',name='apply_promotion',arguments='not JSON')])
    assert wb._batch_blocked


def test_model_cannot_add_its_own_coupon_evidence():
    tracker=ShoppingTracker(None,'CUST001')
    with pytest.raises(ValueError):
        tracker.apply_update({'provided_coupon_codes':['SAVE500']},'A discount')
    assert tracker.state['provided_coupon_codes']==[]


def test_real_agent_requires_user_code_and_keeps_preview_read_only(monkeypatch):
    from autogen_core.models import CreateResult,RequestUsage
    from autogen_ext.models.replay import ReplayChatCompletionClient
    draft=request_order('CUST001',[{'item_code':'ITEM001','quantity':1}],order_id='COUPON-ORDER')['order_id']
    def invoke(code,action='apply'):
        return CreateResult(finish_reason='function_calls',usage=RequestUsage(prompt_tokens=0,completion_tokens=0),cached=False,
            content=[FunctionCall(id=code,name='apply_promotion',arguments=json.dumps({
                'order_id':draft,'customer_id':'CUST001','promo_code':code,'action':action,
                'execution_coupon_codes':[code]}))])
    summary='Apply SAVE10 to your quote?'
    replay=ReplayChatCompletionClient([
        invoke('SAVE500'),'Please supply your coupon code.',
        invoke('SAVE10','validate'),json.dumps({'reply':summary,'shopping_update':{'pending_confirmation':
            {'action':'apply_promotion','order_id':draft,'promo_code':'SAVE10','summary':summary}}}),
        invoke('SAVE10'),'SAVE10 has been applied to your quote.'
    ])
    monkeypatch.setattr(agent,'create_model_client',lambda:replay)
    async def run():
        engine=agent.BillingAgent()
        await engine.start()
        try:
            denied=await engine.chat('Choose and apply the highest discount.',[],'CUST001')
            assert denied['shopping_state']['provided_coupon_codes']==[]
            with closing(connect()) as conn:
                assert conn.execute('SELECT promo_code FROM orders WHERE order_id=?',(draft,)).fetchone()[0] is None
            checked=await engine.chat('Is SAVE10 valid?',[],'CUST001',shopping_state=denied['shopping_state'])
            assert checked['shopping_state']['pending_confirmation']['promo_code']=='SAVE10'
            with closing(connect()) as conn:
                assert conn.execute('SELECT promo_code FROM orders WHERE order_id=?',(draft,)).fetchone()[0] is None
            await engine.chat('yes',[],'CUST001',shopping_state=checked['shopping_state'])
            with closing(connect()) as conn:
                assert tuple(conn.execute('SELECT promo_code,discount_applied,order_status FROM orders WHERE order_id=?',(draft,)).fetchone())==('SAVE10',7.99,'draft')
        finally:
            await engine.close()
    asyncio.run(run())
