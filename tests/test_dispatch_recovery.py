"""Dispatch faults return receipts without hanging, retrying or extra inference."""
import asyncio
import json

import pytest
from autogen_core import CancellationToken, FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_core.tools import ToolResult, TextResultContent
from autogen_ext.models.replay import ReplayChatCompletionClient

from agent import billing_agent as module


@pytest.fixture(autouse=True)
def isolated_db():
    # These tests replace the transport and never open a database or MCP process.
    yield


def workbench():
    wb = module.RecordingWorkbench()
    wb.user_message = 'Create a draft for one keyboard.'
    wb.customer_id = 'CUST001'
    return wb


def test_read_dispatch_exception_returns_structured_failure_without_hanging(monkeypatch):
    calls = []
    async def failed_transport(self, name, arguments, *args, **kwargs):
        calls.append(name)
        raise RuntimeError('Transport failed before a result was received')
    monkeypatch.setattr(module.McpWorkbench, 'call_tool', failed_transport)

    async def run():
        wb = workbench()
        result = await asyncio.wait_for(wb.call_tool('execute_sql_query',
            {'query': 'SELECT item_code FROM items'}), timeout=2)
        data = module.decode_result(result)
        assert result.is_error and data['ok'] is False
        assert 'internal error' in data['error']
        assert calls == ['execute_sql_query'] and wb.tool_calls == 1
        assert wb.failed and wb.transport_failed
        assert not wb.uncertain_change and not wb.completed_response_error
        assert not wb.repair_only
    asyncio.run(run())


def test_interrupted_mutation_is_unverified_and_blocks_the_next_write(monkeypatch):
    calls = []
    async def failed_transport(self, name, arguments, *args, **kwargs):
        calls.append(name)
        raise RuntimeError('Connection closed while the draft result was returning')
    monkeypatch.setattr(module.McpWorkbench, 'call_tool', failed_transport)
    arguments = {'customer_id': 'CUST001', 'order_id': 'RECOVERY-DRAFT',
                 'items': [{'item_code': 'ITEM001', 'quantity': 1}]}

    async def run():
        wb = workbench()
        first = module.decode_result(await asyncio.wait_for(
            wb.call_tool('request_order', arguments), timeout=2))
        assert first['ok'] is False and 'unverified' in first['error']
        assert wb.uncertain_change and wb.repair_only
        assert not wb.completed_response_error and not wb.completed_changes
        second = module.decode_result(await asyncio.wait_for(
            wb.call_tool('request_order', arguments), timeout=2))
        assert second['ok'] is False and 'read-only' in second['error']
        assert calls == ['request_order'] and wb.tool_calls == 1
    asyncio.run(run())


def test_cancellation_propagates_instead_of_becoming_a_tool_error(monkeypatch):
    async def cancelled_transport(self, name, arguments, *args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(module.McpWorkbench, 'call_tool', cancelled_transport)

    async def run():
        wb = workbench()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(wb.call_tool('execute_sql_query',
                {'query': 'SELECT item_code FROM items'}), timeout=2)
        assert not wb.uncertain_change and not wb.completed_response_error
        assert not wb._dispatch_lock.locked()
    asyncio.run(run())


@pytest.mark.parametrize('failure_flag', ['uncertain_change', 'completed_response_error'])
def test_unverified_or_completed_change_skips_another_provider_call(monkeypatch, failure_flag):
    calls = []
    async def forbidden_inference(self, *args, **kwargs):
        calls.append(True)
        raise AssertionError('A provider request cannot repair or replay this write')
    monkeypatch.setattr(module.AzureOpenAIChatCompletionClient, 'create', forbidden_inference)

    async def run():
        wb = workbench()
        setattr(wb, failure_flag, True)
        client = object.__new__(module.TimedAzureClient)
        client.batch_workbench = wb
        result = await asyncio.wait_for(client.create([]), timeout=2)
        assert isinstance(result.content, str) and result.content
        assert result.finish_reason == 'stop'
        assert result.usage.prompt_tokens == result.usage.completion_tokens == 0
        assert wb.model_calls == wb.input_tokens == wb.output_tokens == 0
        assert calls == []
    asyncio.run(run())


def test_presentation_exception_after_success_preserves_completed_receipt(monkeypatch):
    calls = []
    async def successful_transport(self, name, arguments, *args, **kwargs):
        calls.append(name)
        return ToolResult(name=name, is_error=False,
            result=[TextResultContent(content=json.dumps({'ok': True,
                'order_id': 'RECOVERY-COMPLETED'}))])
    monkeypatch.setattr(module.McpWorkbench, 'call_tool', successful_transport)

    async def run():
        wb = workbench()
        def failed_presentation(*args, **kwargs):
            raise RuntimeError('The change committed, but presentation failed')
        monkeypatch.setattr(wb, 'record_result', failed_presentation)
        arguments = {'customer_id': 'CUST001', 'order_id': 'RECOVERY-COMPLETED',
                     'items': [{'item_code': 'ITEM001', 'quantity': 1}]}
        result = await asyncio.wait_for(wb.call_tool('request_order', arguments), timeout=2)
        data = module.decode_result(result)
        assert result.is_error and data['ok'] is False and data['change_completed'] is True
        assert data['receipts'] == ['Draft RECOVERY-COMPLETED was created.']
        assert wb.completed_changes == data['receipts']
        assert wb.completed_response_error and wb.repair_only
        assert not wb.uncertain_change
        await asyncio.wait_for(wb.call_tool('request_order', arguments), timeout=2)
        assert calls == ['request_order'] and wb.tool_calls == 1
    asyncio.run(run())


def model_call(name, arguments):
    return CreateResult(content=[FunctionCall(id='queue-recovery-call', name=name,
        arguments=json.dumps(arguments))], finish_reason='function_calls',
        usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)


def tool_schema(name):
    return [{'name': name, 'description': 'Isolated recovery test tool',
             'parameters': {'type': 'object', 'properties': {}}}]


async def run_agent_without_leaking_tasks(agent, token):
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(agent.run(task='Run the requested action.', cancellation_token=token))
    try:
        return await asyncio.wait_for(task, timeout=1)
    finally:
        token.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pending = [item for item in asyncio.all_tasks()
                   if item not in before and item is not asyncio.current_task()]
        for item in pending:
            item.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def test_safe_agent_finishes_when_per_call_schema_listing_raises(monkeypatch):
    async def run():
        wb = workbench()
        schema_calls, dispatches = [], []
        async def schemas():
            schema_calls.append(True)
            # The initial model request has a schema. Failure happens in the
            # child execution task, outside RecordingWorkbench.call_tool.
            if len(schema_calls) == 2:
                raise RuntimeError('Per-call schema lookup failed')
            return tool_schema('execute_sql_query')
        async def forbidden_dispatch(*args, **kwargs):
            dispatches.append(True)
            raise AssertionError('No tool can dispatch without its schema')
        monkeypatch.setattr(wb, 'list_tools', schemas)
        monkeypatch.setattr(wb, 'call_tool', forbidden_dispatch)
        replay = ReplayChatCompletionClient([
            model_call('execute_sql_query', {'query': 'SELECT item_code FROM items'}),
            'The data could not be retrieved.'])
        token = CancellationToken()
        agent = module.SafeBillingAssistant('queue_recovery', model_client=replay,
            workbench=wb, max_tool_iterations=2, reflect_on_tool_use=True)
        try:
            result = await run_agent_without_leaking_tasks(agent, token)
            assert result.messages[-1].content == 'The data could not be retrieved.'
            assert len(schema_calls) >= 2 and not dispatches
            assert wb.tool_calls == 0 and wb.failed and wb.transport_failed
            assert wb.dispatch_problem and not wb.uncertain_change
            errors = [message for message in result.messages
                      if type(message).__name__ == 'ToolCallExecutionEvent']
            assert errors and errors[-1].content[0].is_error
            assert errors[-1].content[0].call_id == 'queue-recovery-call'
        finally:
            await replay.close()
    asyncio.run(run())


def test_safe_agent_preserves_completed_write_when_result_text_raises(monkeypatch):
    class BrokenTextResult(ToolResult):
        def to_text(self):
            raise RuntimeError('Successful tool result could not be converted to text')

    calls = []
    async def successful_transport(self, name, arguments, *args, **kwargs):
        calls.append(name)
        return BrokenTextResult(name=name, is_error=False, result=[TextResultContent(
            content=json.dumps({'ok': True, 'order_id': 'TEXT-COMPLETED'}))])
    monkeypatch.setattr(module.McpWorkbench, 'call_tool', successful_transport)

    async def run():
        wb = workbench()
        async def schemas():
            return tool_schema('request_order')
        monkeypatch.setattr(wb, 'list_tools', schemas)
        monkeypatch.setattr(wb, 'record_result', lambda *args, **kwargs: None)
        replay = ReplayChatCompletionClient([
            model_call('request_order', {'customer_id': 'CUST001', 'order_id': 'TEXT-COMPLETED',
                'items': [{'item_code': 'ITEM001', 'quantity': 1}]}),
            'The saved draft completed; do not repeat the change.'])
        token = CancellationToken()
        agent = module.SafeBillingAssistant('text_recovery', model_client=replay,
            workbench=wb, max_tool_iterations=2, reflect_on_tool_use=True)
        try:
            result = await run_agent_without_leaking_tasks(agent, token)
            assert 'do not repeat' in result.messages[-1].content
            assert calls == ['request_order'] and wb.tool_calls == 1
            assert wb.completed_changes == ['Draft TEXT-COMPLETED was created.']
            assert wb.completed_response_error and wb.repair_only
            assert not wb.uncertain_change
            errors = [message for message in result.messages
                      if type(message).__name__ == 'ToolCallExecutionEvent']
            assert errors and errors[-1].content[0].is_error
        finally:
            await replay.close()
    asyncio.run(run())


def test_safe_agent_child_schema_wait_obeys_cancellation_token(monkeypatch):
    async def run():
        wb = workbench()
        entered = asyncio.Event()
        async def never_returns():
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(wb, 'list_tools', never_returns)
        token = CancellationToken()
        task = asyncio.create_task(module.SafeBillingAssistant._execute_tool_call(
            tool_call=FunctionCall(id='cancel-schema', name='execute_sql_query', arguments='{}'),
            workbench=[wb], handoff_tools=[], agent_name='cancellation_recovery',
            cancellation_token=token, stream=asyncio.Queue()))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            token.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            assert not wb.uncertain_change and not wb.completed_response_error
        finally:
            token.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
