from address_support import place_order_with_address, prepare_delivery
import asyncio
import json

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

import agent.billing_agent as module
from database.connection import transaction
from mcp_server.tools.billing_tools import request_order, place_order


def tool_call(name, **arguments):
    return CreateResult(finish_reason="function_calls", content=[FunctionCall(
        id="1", name=name, arguments=json.dumps(arguments))],
        usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)


def test_reuse_fresh_data_and_separate_contexts(monkeypatch):
    class CapturingReplay(ReplayChatCompletionClient):
        async def create(self, messages, **kwargs):
            inputs.append(list(messages))
            return await super().create(messages, **kwargs)

    inputs, clients = [], []
    replay = CapturingReplay([
        tool_call("execute_sql_query", query="SELECT COUNT(*) FROM items"), "3 products.",
        tool_call("execute_sql_query", query="SELECT COUNT(*) FROM items"), "4 products."])
    def client():
        clients.append(replay)
        return replay
    monkeypatch.setattr(module, "create_model_client", client)

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        workbench = engine._workbench
        actor = workbench._actor
        try:
            await engine.chat("First private question", [{"role": "user", "content": "Private history"}], "CUST001")
            with transaction() as conn:
                conn.execute("INSERT INTO items VALUES ('NEW','NEW','New product',1,2,3)")
            await engine.chat("Second question", [], "CUST002")
            assert len(clients) == 1
            assert engine._workbench is workbench and workbench._actor is actor
            assert "Private history" not in str(inputs[2])
            assert "CUST001" not in str(inputs[2])
            assert "CUST002" in str(inputs[2])
            assert "[[4]]" in str(inputs[3])  # Actual fresh MCP result, not the replay answer.
            schemas = await workbench.list_tools()
            schemas.clear()
            from mcp_server.access import CUSTOMER_TOOLS
            assert len(await workbench.list_tools()) == len(CUSTOMER_TOOLS)
        finally:
            await engine.close()
        assert engine._client is None and engine._workbench is None
        assert workbench._actor is None
    asyncio.run(run())


def test_invoice_attachments_do_not_leak_between_turns(monkeypatch):
    order = request_order("CUST001", [{"item_code": "ITEM001", "quantity": 1}])
    invoice = place_order_with_address(order["order_id"], "CUST001")["invoice_number"]
    replay = ReplayChatCompletionClient([
        tool_call("download_invoice", invoice_number=invoice, customer_id="CUST001"),
        "Your invoice is ready.", "Hello."])
    monkeypatch.setattr(module, "create_model_client", lambda: replay)

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            first = await engine.chat("Invoice please", [], "CUST001")
            second = await engine.chat("Hello", [], "CUST002")
            assert first["invoices"] == [{"invoice_number": invoice, "customer_id": "CUST001"}]
            assert second["invoices"] == []
        finally:
            await engine.close()
    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_turn_discards_resources_without_replaying(monkeypatch, cancelled):
    class BrokenReplay(ReplayChatCompletionClient):
        async def create(self, *args, **kwargs):
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError("Unavailable")

    created = []
    def client():
        replay = BrokenReplay([]) if not created else ReplayChatCompletionClient(["Recovered."])
        created.append(replay)
        return replay
    monkeypatch.setattr(module, "create_model_client", client)

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        original = engine._workbench
        try:
            with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
                await engine.chat("Fail", [], None)
            assert len(created) == 1
            assert engine._client is None and engine._workbench is None
            assert original._actor is None
            assert (await engine.chat("New turn", [], None))["response"] == "Recovered."
            assert len(created) == 2
        finally:
            await engine.close()
    asyncio.run(run())


@pytest.mark.parametrize('recovered', [False, True])
def test_recovered_tool_error_keeps_warm_transport_but_unrecovered_error_discards_it(monkeypatch, recovered):
    # A schema failure comes back as an MCP error, not a disconnected process.
    script = [tool_call('execute_sql_query', query=['invalid query type'])]
    if recovered:
        script.append(tool_call('execute_sql_query', query='SELECT COUNT(*) AS products FROM items'))
    script += ['The request was handled.', 'Hello.']
    replay = ReplayChatCompletionClient(script)
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)

    async def run():
        engine = module.BillingAgent()
        await engine.start()
        workbench, actor = engine._workbench, engine._workbench._actor
        try:
            await engine.chat('First private question', [], 'CUST001')
            assert workbench.failed
            if recovered:
                assert engine._workbench is workbench and workbench._actor is actor
                assert not workbench.transport_failed
                await engine.chat('Hello', [], 'CUST002')
                assert engine._workbench is workbench and workbench._actor is actor
                assert not workbench.failed
                assert not engine._workbench.tables
            else:
                assert engine._workbench is None and engine._client is None
                assert workbench._actor is None
        finally:
            await engine.close()
    asyncio.run(run())
