from address_support import place_order_with_address, prepare_delivery
import asyncio
import json
from autogen_ext.tools.mcp import McpWorkbench
from agent.billing_agent import parameters, decode_result


def test_real_mcp_discovery_and_billing_flow():
    async def run():
        async with McpWorkbench(parameters()) as wb:
            tools = await wb.list_tools()
            from mcp_server.server import TOOLS
            assert {t["name"] for t in tools} == {t.__name__ for t in TOOLS}
            assert len(tools) == 12
            exercised = set()
            assert not {'manage_payment','billing_statement','manage_billing_case','get_billing_cases',
                        'set_invoice_due_date','issue_credit_note','manage_dispute','prepare_payment_reminder'} & {t['name'] for t in tools}
            async def call(tool_name, **args):
                result = decode_result(await wb.call_tool(tool_name, {**args, 'execution_customer_id':'CUST001', 'execution_mode': 'admin' if tool_name in ('billing_analysis','billing_anomalies') else 'customer'}))
                assert result.get("ok"), result
                exercised.add(tool_name)
                return result
            assert (await call("execute_sql_query", query="SELECT COUNT(*) FROM customers"))["rows"] == [[1]]
            await call('manage_customer', customer_id='CUST001', name='Test shopper', action='update', confirmed=True)
            from address_support import ADDRESS_FIELDS
            address = await call('manage_customer', customer_id='CUST001', action='add_address',
                                 address=ADDRESS_FIELDS, confirmed=True)
            order = await call("request_order", customer_id="CUST001", items=[{"item_code": "ITEM001", "quantity": 1}])
            await call('manage_draft', order_id=order['order_id'], customer_id='CUST001',
                       action='select_address', address_id=address['address_id'], confirmed=True)
            await call('manage_draft', order_id=order['order_id'], customer_id='CUST001', action='replace_items',
                       items=[{'item_code':'ITEM001','quantity':1}], confirmed=True)
            await call("apply_promotion", order_id=order["order_id"], customer_id="CUST001", promo_code="SAVE10", execution_coupon_codes=['SAVE10'])
            placed = await call("place_order", order_id=order["order_id"], customer_id="CUST001")
            comparison = await call('compare_products', products=['ITEM001','ITEM002'])
            assert len(comparison['products']) == 2
            assert (await call('find_cheaper_alternatives', product_reference='ITEM001'))['matched_count'] == 0
            assert (await call('billing_analysis'))['invoice_count'] == 1
            assert (await call('billing_anomalies'))['finding_count'] == 0
            await call("process_refund", order_id=order["order_id"], customer_id="CUST001", reason="Test return")
            assert (await call("download_invoice", invoice_number=placed["invoice_number"], customer_id="CUST001"))["ready"]
            assert exercised == {t.__name__ for t in TOOLS}
    asyncio.run(run())


def test_autogen_tool_execution_and_reflection(monkeypatch):
    from autogen_core import FunctionCall
    from autogen_core.models import CreateResult, RequestUsage
    from autogen_ext.models.replay import ReplayChatCompletionClient
    import agent.billing_agent as module
    replay = ReplayChatCompletionClient([
        CreateResult(finish_reason="function_calls", content=[FunctionCall(id="1", name="execute_sql_query", arguments=json.dumps({"query": "SELECT COUNT(*) FROM customers"}))], usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False),
        "There are 4 customers in the billing database."])
    monkeypatch.setattr(module, "create_model_client", lambda: replay)
    result = asyncio.run(module.BillingAgent().chat("How many customers?", [], None))
    assert result["response"] == "There are 4 customers in the billing database."


def test_autogen_invoice_attachment(monkeypatch):
    from autogen_core import FunctionCall
    from autogen_core.models import CreateResult, RequestUsage
    from autogen_ext.models.replay import ReplayChatCompletionClient
    from mcp_server.tools.billing_tools import request_order, place_order
    import agent.billing_agent as module
    order = request_order("CUST001", [{"item_code": "ITEM001", "quantity": 1}])
    invoice = place_order_with_address(order["order_id"], "CUST001")["invoice_number"]
    replay = ReplayChatCompletionClient([
        CreateResult(finish_reason="function_calls", content=[FunctionCall(id="1", name="download_invoice", arguments=json.dumps({"invoice_number": invoice, "customer_id": "CUST001"}))], usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False),
        "Your invoice is ready."])
    monkeypatch.setattr(module, "create_model_client", lambda: replay)
    result = asyncio.run(module.BillingAgent().chat("Download my invoice", [], "CUST001"))
    assert result["invoices"] == [{"invoice_number": invoice, "customer_id": "CUST001"}]
