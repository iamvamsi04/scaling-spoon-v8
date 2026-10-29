"""Conversation state follows committed work and keeps account scope fixed."""
from address_support import place_order_with_address, prepare_delivery
import asyncio
from contextlib import closing
import json

import pytest
from autogen_core.tools import ToolResult, TextResultContent

from agent import billing_agent as agent
from agent.prompts.system_message import SYSTEM_MESSAGE
from context.shopping_state import ShoppingTracker, order_snapshot
from database.connection import connect, transaction
from mcp_server.access import scoped_tool
from mcp_server.tools.billing_tools import request_order, place_order, process_refund, download_invoice
from mcp_server.tools.operations_tools import manage_customer


def order(placed=True):
    result = request_order('CUST001', [{'item_code': 'ITEM001', 'quantity': 1}])
    assert result['ok']
    prepare_delivery(result['order_id'], 'CUST001')
    if placed:
        assert place_order_with_address(result['order_id'], 'CUST001')['ok']
    return result['order_id']


def proposal(tracker, action, order_id):
    summary = 'Please confirm this exact service action.'
    reply = tracker.finish(json.dumps({'reply': summary, 'shopping_update': {
        'pending_confirmation': {'action': action, 'order_id': order_id, 'summary': summary}}}))
    if action == 'place_order' and tracker.state['pending_confirmation']:
        assert reply.startswith(tracker.state['pending_confirmation']['summary'] + '\nItems:\n')
        assert 'Delivery address for approval:' in reply
        delivery = order_snapshot(order_id, tracker.customer_id)['delivery_address']
        assert tracker.state['pending_confirmation']['delivery']['delivery_address'] == delivery
        assert all(value in reply for value in delivery.values() if value)
    else:
        assert reply == summary
    return tracker.state['pending_confirmation']


@pytest.mark.parametrize('action', ['quote', 'status', 'bulk_preview'])
def test_read_only_refund_results_do_not_complete_a_request(action):
    oid = order()
    tracker = ShoppingTracker(None, 'CUST001')
    args = {'order_id': oid, 'customer_id': 'CUST001', 'action': action}
    if action == 'status':
        assert scoped_tool(process_refund)(oid, 'CUST001', 'Return', confirmed=True,
                                          execution_customer_id='CUST001')['ok']
    if action == 'bulk_preview':
        with closing(connect()) as conn:
            day = conn.execute('SELECT CURRENT_DATE').fetchone()[0]
        args.update(order_id='', date_from=day, date_to=day)
    result = scoped_tool(process_refund)(**args, execution_customer_id='CUST001')
    assert result['ok'], result
    tracker.observe('process_refund', args, result, 'CUST001')
    assert tracker.completed_actions == set()
    pending = proposal(tracker, {'bulk_preview':'bulk_refund', 'quote':'process_refund', 'status':'escalate_refund'}[action],
                       None if action == 'bulk_preview' else oid)
    assert pending and pending['status'] == 'awaiting_customer'
    with closing(connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM refund_requests').fetchone()[0] == (1 if action == 'status' else 0)


@pytest.mark.parametrize('action', ['cancel_order', 'correct_invoice', 'report_billing_issue',
                                  'escalate_refund', 'apply_store_credit', 'bulk_refund'])
def test_service_proposals_are_saved_for_eligible_owned_state(action):
    oid = order(placed=action != 'apply_store_credit')
    if action == 'escalate_refund':
        result = scoped_tool(process_refund)(oid, 'CUST001', 'Return', action='request',
            confirmed=True, execution_customer_id='CUST001')
        assert result['ok'], result
    tracker = ShoppingTracker(None, 'CUST001')
    pending = proposal(tracker, action, None if action == 'bulk_refund' else oid)
    assert pending and pending['action'] == action


def test_shipping_invalidates_cancellation_proposal():
    oid = order()
    tracker = ShoppingTracker(None, 'CUST001')
    assert proposal(tracker, 'cancel_order', oid)
    with transaction() as conn:
        conn.execute("UPDATE order_service SET fulfillment_status='shipped' WHERE order_id=?", (oid,))
    resumed = ShoppingTracker(tracker.state, 'CUST001')
    assert resumed.state['pending_confirmation'] is None
    assert proposal(resumed, 'cancel_order', oid) is None


@pytest.mark.parametrize('change', ['refund_request', 'invoice_revision', 'credit_allocation'])
def test_service_changes_invalidate_old_proposals(change):
    oid = order(placed=change != 'credit_allocation')
    tracker = ShoppingTracker(None, 'CUST001')
    action = 'apply_store_credit' if change == 'credit_allocation' else 'report_billing_issue'
    assert proposal(tracker, action, oid)
    if change == 'refund_request':
        result = scoped_tool(process_refund)(oid, 'CUST001', 'Return', amount='10',
            confirmed=True, execution_customer_id='CUST001')
        assert result['ok'], result
    else:
        with transaction() as conn:
            if change == 'invoice_revision':
                conn.execute("INSERT INTO invoice_revisions(order_id,contact_json) VALUES (?,?)",
                             (oid, '{"billing_address":"New Road"}'))
            else:
                conn.execute('INSERT INTO order_service(order_id,credit_requested) VALUES (?,10) '
                    'ON CONFLICT(order_id) DO UPDATE SET credit_requested=10', (oid,))
    assert ShoppingTracker(tracker.state, 'CUST001').state['pending_confirmation'] is None


@pytest.mark.parametrize('role', ['admin', 'cashier'])
def test_staff_invoice_download_cannot_correct_customer_contact(role):
    oid = order()
    invoice = order_snapshot(oid, 'CUST001')['invoice_number']
    result = scoped_tool(download_invoice)(invoice, 'CUST001', action='correct_details',
        billing_address='Changed by staff', confirmed=True, execution_mode=role)
    assert not result['ok'] and 'customer must confirm' in result['error']
    with closing(connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM invoice_revisions').fetchone()[0] == 0


@pytest.mark.parametrize('customer', [None, 'CUST001'])
def test_chat_tools_cannot_create_or_adopt_customer_accounts(customer):
    result = scoped_tool(manage_customer)('CHAT-NEW', 'New shopper', confirmed=True,
                                        execution_customer_id=customer)
    assert not result['ok'] and 'registration form' in result['error']
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers WHERE customer_id='CHAT-NEW'").fetchone()[0] == 0
    tracker = ShoppingTracker(None, customer)
    reply = tracker.finish(json.dumps({'reply':'Register this account?', 'shopping_update': {
        'pending_confirmation': {'action':'create_customer','order_id':None,'summary':'Register this account?'}}}))
    assert tracker.state['pending_confirmation'] is None


def test_tool_response_cannot_change_verified_customer_scope(monkeypatch):
    async def fake_result(self, name, arguments, *args, **kwargs):
        return ToolResult(name=name, result=[TextResultContent(content=json.dumps({
            'ok':True, 'action':'create', 'customer_id':'OTHER-CUSTOMER'}))])
    monkeypatch.setattr(agent.McpWorkbench, 'call_tool', fake_result)
    async def run():
        workbench = agent.RecordingWorkbench()
        workbench.customer_id = 'CUST001'
        await workbench.call_tool('manage_customer', {'customer_id':'OTHER-CUSTOMER'})
        assert workbench.customer_id == 'CUST001'
        workbench.customer_id = None
        await workbench.call_tool('manage_customer', {'customer_id':'OTHER-CUSTOMER'})
        assert workbench.customer_id is None
    asyncio.run(run())


def test_customer_schema_requires_explicit_profile_update():
    async def run():
        workbench = agent.RecordingWorkbench()
        workbench._tool_schemas = [{'name':'manage_customer', 'description':'Create or update',
            'parameters':{'type':'object', 'properties':{'action':{'type':'string','default':'create'}},
                          'required':['customer_id','name']}}]
        schema = (await workbench.list_tools())[0]
        action = schema['parameters']['properties']['action']
        assert action['enum'] == ['update','add_address','update_address','delete_address','set_default_address'] and 'default' not in action
        assert 'action' in schema['parameters']['required']
    asyncio.run(run())


def test_prompts_require_verified_identity_and_keep_shipment_facts_distinct():
    assert 'verifies customer credentials outside chat' in SYSTEM_MESSAGE
    assert 'newly created account after the tool succeeds' not in SYSTEM_MESSAGE
    assert 'selecting a customer id provides record context' not in SYSTEM_MESSAGE.lower()
    assert 'recorded fulfillment' in SYSTEM_MESSAGE
