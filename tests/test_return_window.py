from address_support import place_order_with_address, prepare_delivery
from contextlib import closing
import pytest
from config import settings
from database.connection import connect, transaction
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import request_order, place_order, process_refund

refund=scoped_tool(process_refund)


def order(age):
    oid=request_order('CUST001',[{'item_code':'ITEM001','quantity':2}])['order_id']
    assert place_order_with_address(oid,'CUST001')['ok']
    with transaction() as conn:
        conn.execute("UPDATE orders SET order_date=date('now',?) WHERE order_id=?",(f'{-age:+d} days',oid))
    return oid


def request(oid):
    return refund(oid,'CUST001','Return','10.00',execution_customer_id='CUST001')


def approve(oid,role):
    return refund(oid,'CUST001','Return',action='approve',confirmed=True,return_received=True,execution_mode=role)


@pytest.mark.parametrize('age,valid',[(0,True),(29,True),(30,True),(31,False),(365,False),(-1,False)])
def test_request_window_boundary(age,valid,monkeypatch):
    monkeypatch.setattr(settings,'RETURN_WINDOW_DAYS','30')
    oid=order(age)
    result=request(oid)
    assert result['ok'] is valid, result
    with closing(connect()) as conn:
        assert conn.execute('SELECT refund_amount FROM orders WHERE order_id=?',(oid,)).fetchone()[0] is None
        assert conn.execute('SELECT COUNT(*) FROM refund_requests').fetchone()[0]==int(valid)
        assert conn.execute("SELECT COUNT(*) FROM billing_audit WHERE action='process_refund'").fetchone()[0]==int(valid)


@pytest.mark.parametrize('role',['cashier','admin'])
@pytest.mark.parametrize('age,allowed',[(30,True),(31,False)])
def test_approval_rechecks_window_and_no_role_override(role,age,allowed,monkeypatch):
    monkeypatch.setattr(settings,'RETURN_WINDOW_DAYS','30')
    oid=order(29)
    assert request(oid)['ok']
    with transaction() as conn:
        conn.execute("UPDATE orders SET order_date=date('now',?) WHERE order_id=?",(f'-{age} days',oid))
    result=approve(oid,role)
    assert result['ok'] is allowed, result
    if not allowed: assert 'outside the 30-day return window' in result['error']
    with closing(connect()) as conn:
        assert (conn.execute('SELECT refund_amount FROM orders').fetchone()[0] is not None) is allowed
        assert conn.execute('SELECT status FROM refund_requests').fetchone()[0]==('approved' if allowed else 'pending')


@pytest.mark.parametrize('setting',['-1','invalid','1.5',''])
def test_invalid_policy_fails_closed(setting,monkeypatch):
    monkeypatch.setattr(settings,'RETURN_WINDOW_DAYS',setting)
    assert not request(order(0))['ok']


def test_configurable_window_and_same_day_policy(monkeypatch):
    monkeypatch.setattr(settings,'RETURN_WINDOW_DAYS','7')
    assert request(order(7))['ok']
    assert not request(order(8))['ok']
    monkeypatch.setattr(settings,'RETURN_WINDOW_DAYS','0')
    assert request(order(0))['ok']
    assert not request(order(1))['ok']


def test_real_mcp_uses_configured_window(monkeypatch):
    import asyncio
    import json
    from agent.billing_agent import RecordingWorkbench, decode_result
    from context.shopping_state import ShoppingTracker
    monkeypatch.setattr(settings,'RETURN_WINDOW_DAYS','7')
    oid=order(8)
    tracker=ShoppingTracker(None,'CUST001')
    summary=f'Request a USD 10.00 return refund for {oid}, reason Return, to the original method?'
    tracker.finish(json.dumps({'reply':summary,'shopping_update':{'pending_confirmation':{
        'action':'process_refund','order_id':oid,'summary':summary,
        'arguments':{'reason':'Return','amount':'10.00'}}}}))
    assert tracker.state['pending_confirmation']
    async def run():
        async with RecordingWorkbench() as wb:
            wb.customer_id='CUST001'
            wb.user_message='Yes.'
            wb.action_proposal=tracker.state['pending_confirmation']
            result=decode_result(await wb.call_tool('process_refund',dict(order_id=oid,customer_id='CUST001',reason='Return',amount='10.00')))
            assert not result['ok'] and '7-day return window' in result['error']
    asyncio.run(run())
