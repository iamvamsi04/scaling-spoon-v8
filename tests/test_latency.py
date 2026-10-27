import asyncio
import logging
import json
import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from agent import billing_agent as module
from agent.prompts.task_policy import TASK_POLICY, CUSTOMER_TASK_POLICY
from config import settings
from mcp_server.access import scoped_tool
from mcp_server.tools.database import execute_sql_query


def test_customer_keeps_business_rules_without_admin_chart_contract():
    prefix = TASK_POLICY.split('I. Admin charts and visual analytics',1)[0]
    assert CUSTOMER_TASK_POLICY.startswith(prefix)
    assert 'Chart contract for execute_sql_query:' not in CUSTOMER_TASK_POLICY
    assert 'Chart contract for execute_sql_query:' in TASK_POLICY
    assert 'Customer mode cannot create charts' in CUSTOMER_TASK_POLICY


def test_window_count_and_page_are_consistent():
    result = scoped_tool(execute_sql_query)(
        query='SELECT item_code,COUNT(*) OVER() AS matching_products FROM items ORDER BY name,item_code LIMIT ? OFFSET ?',
        parameters=[1,1],execution_mode='customer')
    assert result['ok']
    assert result['row_count']==1
    assert result['rows'][0][1]==3


def test_mode_switch_keeps_admin_chart_schema_and_customer_scope():
    async def run():
        async with module.RecordingWorkbench() as workbench:
            customer = {t['name']:t for t in await workbench.list_tools()}
            assert len(customer)==10
            assert 'chart' not in customer['execute_sql_query']['parameters']['properties']
            workbench.mode='admin'
            admin = {t['name']:t for t in await workbench.list_tools()}
            assert set(admin)=={'execute_sql_query','billing_analysis','billing_anomalies','download_invoice','process_refund','manage_draft'}
            assert 'chart' in admin['execute_sql_query']['parameters']['properties']
            workbench.mode='cashier'
            cashier = {t['name']:t for t in await workbench.list_tools()}
            assert set(cashier)=={'execute_sql_query','download_invoice','process_refund'}
            assert 'chart' not in cashier['execute_sql_query']['parameters']['properties']
            workbench.mode='customer'
            again = {t['name']:t for t in await workbench.list_tools()}
            assert again==customer
            assert 'execution_customer_id' not in again['execute_sql_query']['parameters']['properties']
    asyncio.run(run())


@pytest.mark.parametrize('effort',['','none','low'])
def test_optional_reasoning_setting_preserves_default(monkeypatch,effort):
    monkeypatch.setattr(settings,'AZURE_OPENAI_ENDPOINT','https://example.openai.azure.com')
    monkeypatch.setattr(settings,'AZURE_OPENAI_API_KEY','test-placeholder')
    monkeypatch.setattr(settings,'AZURE_OPENAI_DEPLOYMENT','test-deployment')
    monkeypatch.setattr(settings,'AZURE_OPENAI_MODEL','gpt-5.4')
    monkeypatch.setattr(settings,'AZURE_OPENAI_API_VERSION','2024-10-21')
    monkeypatch.setattr(settings,'AZURE_REASONING_EFFORT',effort)
    client=module.create_model_client()
    try:
        if effort:
            assert client._create_args['reasoning_effort']==effort
        else:
            assert 'reasoning_effort' not in client._create_args
        assert client._create_args['parallel_tool_calls'] is False
    finally:
        asyncio.run(client.close())


def test_model_timing_does_not_log_prompt_or_reply(monkeypatch,caplog):
    async def create(*args,**kwargs):
        return CreateResult(content='private answer',finish_reason='stop',
            usage=RequestUsage(prompt_tokens=100,completion_tokens=12),cached=False)
    monkeypatch.setattr(module.AzureOpenAIChatCompletionClient,'create',create)
    client=object.__new__(module.TimedAzureClient)
    with caplog.at_level(logging.INFO,logger='uvicorn.error.billing_performance'):
        result=asyncio.run(client.create(['private prompt']))
    assert result.content=='private answer'
    assert 'billing_model seconds=' in caplog.text
    assert 'input_tokens=100 output_tokens=12' in caplog.text
    assert 'private prompt' not in caplog.text and 'private answer' not in caplog.text


def batch_call(name, **arguments):
    return FunctionCall(id=name, name=name, arguments=json.dumps(arguments))


@pytest.mark.parametrize('names', [
    ['execute_sql_query', 'place_order'],
    ['process_refund', 'process_refund'],
    ['manage_customer', 'request_order'],
    ['execute_sql_query', 'download_invoice'],
    ['execute_sql_query', 'unknown_tool'],
])
def test_unsafe_batch_executes_nothing(monkeypatch, names):
    async def unexpected(*args, **kwargs):
        raise AssertionError('An unsafe batch must never reach MCP')
    monkeypatch.setattr(module.McpWorkbench, 'call_tool', unexpected)
    async def run():
        workbench = module.RecordingWorkbench()
        workbench.prepare_batch([batch_call(name) for name in names])
        results = await asyncio.gather(*(workbench.call_tool(name) for name in names))
        assert all(result.is_error for result in results)
        assert all('No tools in this batch' in module.decode_result(r)['error'] for r in results)
        assert workbench.tool_calls == 0
        workbench.prepare_batch([batch_call('place_order')])
        assert not workbench._batch_blocked
    asyncio.run(run())


def test_sdk_batch_enabled_only_with_execution_guard(monkeypatch):
    workbench = module.RecordingWorkbench()
    client = object.__new__(module.TimedAzureClient)
    seen = []
    async def create(*args, **kwargs):
        seen.append(kwargs)
        return CreateResult(content=[batch_call('place_order'), batch_call('execute_sql_query')],
            finish_reason='function_calls', usage=RequestUsage(prompt_tokens=1, completion_tokens=1), cached=False)
    monkeypatch.setattr(module.AzureOpenAIChatCompletionClient, 'create', create)
    async def run():
        await client.create([])
        assert seen[-1]['extra_create_args']['parallel_tool_calls'] is module.NOT_GIVEN
        client.batch_workbench = workbench
        await client.create([], tools=[{'name':'execute_sql_query'}], extra_create_args={'user': 'test'})
        assert seen[-1]['extra_create_args'] == {'user':'test', 'parallel_tool_calls':True}
        assert workbench._batch_blocked
    asyncio.run(run())


def test_independent_reads_share_one_model_round_with_real_mcp(monkeypatch):
    from autogen_ext.models.replay import ReplayChatCompletionClient
    calls = [batch_call('compare_products', products=['ITEM001', 'ITEM002']),
             batch_call('find_cheaper_alternatives', product_reference='ITEM001'),
             batch_call('execute_sql_query', query='SELECT COUNT(*) AS products FROM items')]
    engine = module.BillingAgent()
    class GuardedReplay(ReplayChatCompletionClient):
        async def create(self, *args, **kwargs):
            result = await super().create(*args, **kwargs)
            engine._workbench.prepare_batch(result.content)
            return result
    replay = GuardedReplay([
        CreateResult(content=calls, finish_reason='function_calls',
            usage=RequestUsage(prompt_tokens=1, completion_tokens=1), cached=False),
        json.dumps({'reply':'Comparison and alternatives are ready.', 'shopping_update':{}})])
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)
    async def run():
        await engine.start()
        try:
            result = await engine.chat('Compare the keyboard and mouse, find cheaper options and count products.', [], None)
            assert result['response'] == 'Comparison and alternatives are ready.'
            assert engine._workbench.tool_calls == 3
            assert len(replay.create_calls) == 2
            tool_results = replay.create_calls[-1]['messages'][-1].content
            assert len(tool_results) == 3
            assert all(json.loads(r.content)['ok'] for r in tool_results)
        finally:
            await engine.close()
    asyncio.run(run())


def test_batch_dispatch_keeps_navigation_observation_order(monkeypatch):
    from autogen_core.tools import ToolResult, TextResultContent
    active = 0
    order = []
    async def transport(self, name, arguments, *args, **kwargs):
        nonlocal active
        active += 1
        assert active == 1
        order.append(arguments['query'])
        await asyncio.sleep(0)
        active -= 1
        return ToolResult(name=name, result=[TextResultContent(content='{"ok":true}')])
    monkeypatch.setattr(module.McpWorkbench, 'call_tool', transport)
    async def run():
        workbench = module.RecordingWorkbench()
        workbench.prepare_batch([batch_call('execute_sql_query'), batch_call('execute_sql_query')])
        await asyncio.gather(workbench.call_tool('execute_sql_query', {'query':'first'}),
                             workbench.call_tool('execute_sql_query', {'query':'second'}))
        assert order == ['first', 'second']
        workbench.prepare_batch('Final answer')
        assert not workbench._batch_blocked
    asyncio.run(run())
