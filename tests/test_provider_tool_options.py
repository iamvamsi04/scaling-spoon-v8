"""Exercise the pinned AutoGen/OpenAI serializer without contacting Azure."""
import asyncio
import json

import httpx
import pytest
from autogen_core.models import UserMessage
from autogen_core.tools import FunctionTool

from agent import billing_agent as module
from agent.response_contract import ResponseContract
from config import settings


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    # These HTTP-boundary tests execute no database or MCP calls.
    for key, value in {
        'AZURE_OPENAI_ENDPOINT': 'https://billing-test.openai.azure.com',
        'AZURE_OPENAI_API_KEY': 'local-transport-test-key',
        'AZURE_OPENAI_DEPLOYMENT': 'billing-test',
        'AZURE_OPENAI_API_VERSION': '2024-10-21',
        'AZURE_OPENAI_MODEL': 'gpt-5.4',
        'AZURE_REASONING_EFFORT': '',
    }.items():
        monkeypatch.setattr(settings, key, value)


def completion(content='The result is ready.', tool_call=None):
    message = {'role': 'assistant', 'content': content}
    if tool_call is not None:
        message['content'] = None
        message['tool_calls'] = [tool_call]
    return httpx.Response(200, json={
        'id': 'completion-test', 'object': 'chat.completion', 'created': 1,
        'model': 'gpt-5.4',
        'choices': [{'index': 0, 'message': message,
                     'finish_reason': 'tool_calls' if tool_call else 'stop'}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
    })


def azure_tool_option_error(body):
    if not body.get('tools') and 'parallel_tool_calls' in body:
        return httpx.Response(400, json={'error': {
            'message': "Invalid value for 'parallel_tool_calls': 'parallel_tool_calls' is only allowed when 'tools' are specified.",
            'type': 'invalid_request_error', 'param': 'parallel_tool_calls', 'code': None,
        }})
    return None


SQL_TOOL = {
    'name': 'execute_sql_query', 'description': 'Read the current store records.',
    'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                   'required': ['query']},
}


@pytest.mark.parametrize('tool_argument', ['omitted', 'empty_list', 'empty_tuple'])
@pytest.mark.parametrize('caller_parallel', [None, False, True])
@pytest.mark.parametrize('guarded', [False, True])
def test_no_tool_requests_omit_parallel_options_on_the_wire(tool_argument, caller_parallel, guarded):
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        rejected = azure_tool_option_error(body)
        return rejected if rejected is not None else completion()

    async def run():
        client = module.create_model_client(http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        if guarded:
            client.batch_workbench = module.RecordingWorkbench()
        extra = {'user': 'transport-test', 'tool_choice': 'required'}
        if caller_parallel is not None:
            extra['parallel_tool_calls'] = caller_parallel
        original = dict(extra)
        kwargs = {'extra_create_args': extra, 'tool_choice': 'none'}
        if tool_argument != 'omitted':
            kwargs['tools'] = [] if tool_argument == 'empty_list' else ()
        try:
            result = await client.create([UserMessage(content='Describe the available chart options.', source='user')], **kwargs)
            assert result.content == 'The result is ready.'
            assert client._create_args['parallel_tool_calls'] is False
            assert extra == original
        finally:
            await client.close()

    asyncio.run(run())
    assert len(requests) == 1
    assert 'tools' not in requests[0]
    assert 'parallel_tool_calls' not in requests[0]
    assert 'tool_choice' not in requests[0]
    assert requests[0]['user'] == 'transport-test'


@pytest.mark.parametrize('guarded', [False, True])
@pytest.mark.parametrize('choice', ['auto', 'none'])
def test_tool_requests_preserve_caller_choice_and_guarded_batching(guarded, choice):
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        return completion()

    async def run():
        client = module.create_model_client(http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        if guarded:
            workbench = module.RecordingWorkbench()
            # A retrieval classification must not force a tool over the caller's
            # semantic choice, even when a matching read schema is available.
            workbench.contract = ResponseContract('data')
            client.batch_workbench = workbench
        try:
            await client.create([UserMessage(content='Explain what the report means.', source='user')],
                                tools=[SQL_TOOL], tool_choice=choice)
            assert client._create_args['parallel_tool_calls'] is False
        finally:
            await client.close()

    asyncio.run(run())
    assert len(requests) == 1
    assert requests[0]['tools'][0]['function']['name'] == 'execute_sql_query'
    assert requests[0]['parallel_tool_calls'] is guarded
    assert requests[0]['tool_choice'] == choice


def test_actual_agent_reflection_drops_tools_and_parallel_options_together():
    requests = []
    executions = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        rejected = azure_tool_option_error(body)
        if rejected is not None:
            return rejected
        if len(requests) == 1:
            return completion(tool_call={'id': 'stock-read', 'type': 'function',
                'function': {'name': 'execute_sql_query', 'arguments': '{}'}})
        assert len(requests) == 2
        assert any(message['role'] == 'tool' for message in body['messages'])
        return completion('There are two products in stock.')

    async def read_stock() -> str:
        executions.append('read')
        return json.dumps({'ok': True, 'products_in_stock': 2})

    async def run():
        client = module.create_model_client(http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        guard = module.RecordingWorkbench()
        client.batch_workbench = guard
        assistant = module.SafeBillingAssistant('billing_agent', model_client=client,
            tools=[FunctionTool(read_stock, description='Read stock totals.', name='execute_sql_query')],
            system_message='Answer using the supplied stock reader.',
            reflect_on_tool_use=True, max_tool_iterations=1)
        try:
            result = await assistant.run(task='How many products are in stock?')
            assert result.messages[-1].content == 'There are two products in stock.'
            assert guard.model_calls == 2
        finally:
            await client.close()

    asyncio.run(run())
    assert executions == ['read']
    assert len(requests) == 2
    assert requests[0]['tools'] and requests[0]['parallel_tool_calls'] is True
    assert 'tools' not in requests[1]
    assert 'parallel_tool_calls' not in requests[1]
    assert 'tool_choice' not in requests[1]
