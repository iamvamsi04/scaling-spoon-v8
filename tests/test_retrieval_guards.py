"""Trusted tool boundaries never infer execution from words in a question."""
import asyncio
import pytest
from autogen_core.models import CreateResult, RequestUsage
import agent.billing_agent as module
from agent.response_contract import ResponseContract


@pytest.mark.parametrize('question,mode', [
    ('List the available chart names without generating anything.', 'admin'),
    ('show me some projectors', 'customer'),
    ('show the store wide analytics', 'admin'),
    ('what products do you have in store', 'customer'),
])
def test_provider_does_not_force_tools_or_scope_from_user_words(monkeypatch, question, mode):
    wb = module.RecordingWorkbench()
    wb.mode = mode
    wb.user_message = question
    client = object.__new__(module.TimedAzureClient)
    client.batch_workbench = wb
    seen = []
    async def create(*args, **kwargs):
        seen.append(kwargs)
        return CreateResult(content='ignored', finish_reason='stop',
            usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)
    monkeypatch.setattr(module.AzureOpenAIChatCompletionClient, 'create', create)
    names = module.CUSTOMER_TOOLS if mode == 'customer' else module.ADMIN_TOOLS
    async def run():
        await client.create([], tools=[{'name':name} for name in names])
    asyncio.run(run())
    assert 'tool_choice' not in seen[0]
    assert {schema['name'] for schema in seen[0]['tools']} == names
    assert 'tool_choice' not in seen[0].get('extra_create_args', {})


def test_successful_current_evidence_does_not_force_another_read(monkeypatch):
    wb = module.RecordingWorkbench()
    wb.contract = ResponseContract('data')
    wb.record_result('execute_sql_query', {'query':'SELECT order_id,charged_price FROM orders'},
        {'ok':True,'columns':['order_id','charged_price'],'rows':[],'truncated':False})
    assert wb.has_evidence
    client = object.__new__(module.TimedAzureClient)
    client.batch_workbench = wb
    seen = []
    async def create(*args, **kwargs):
        seen.append(kwargs)
        return CreateResult(content='No orders match.', finish_reason='stop',
            usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)
    monkeypatch.setattr(module.AzureOpenAIChatCompletionClient, 'create', create)
    asyncio.run(client.create([], tools=[{'name':'execute_sql_query'}]))
    assert 'tool_choice' not in seen[0]


def test_interrupted_write_recovery_rejects_mutations_before_mcp(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('A read request dispatched a mutation')
    monkeypatch.setattr(module.McpWorkbench, 'call_tool', forbidden)
    wb = module.RecordingWorkbench()
    wb.contract = ResponseContract()
    wb.repair_only = True
    result = asyncio.run(wb.call_tool('process_refund', {'action':'approve','confirmed':True}))
    assert not module.decode_result(result)['ok']
    assert wb.tool_calls == 0
