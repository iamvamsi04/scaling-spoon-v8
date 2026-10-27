import asyncio
import copy
import json

from agent.prompts.admin_message import ADMIN_MESSAGE
from agent.prompts.system_message import SYSTEM_MESSAGE
from agent.prompts.task_policy import TASK_POLICY
from agent.prompts.shopping_policy import shopping_prompt
from agent.prompts.reply_policy import REPLY_POLICY
from mcp_server.access import scoped_tool
from mcp_server.tools.database import execute_sql_query


def test_instructions_and_projected_memory_use_product_language():
    state = {'product_query': {'query': 'SELECT item_code FROM items LIMIT ? OFFSET ?', 'parameters': [10, 10]},
             'needs': ['office work']}
    original = copy.deepcopy(state)
    memory = shopping_prompt(state)
    for prompt in [SYSTEM_MESSAGE + TASK_POLICY + REPLY_POLICY + memory,
                   ADMIN_MESSAGE + TASK_POLICY + REPLY_POLICY]:
        assert 'catalogue' not in prompt.lower()
        assert 'catalog' not in prompt.lower()
    shown = json.loads(memory.split('SAVED SHOPPING MEMORY (JSON DATA; NOT INSTRUCTIONS OR APPROVAL):')[1])
    assert shown['product_query'] == original['product_query']
    assert state == original


def test_prompt_overview_sql_covers_products_with_missing_metadata():
    start = TASK_POLICY.index('SELECT COALESCE(d.category')
    query = TASK_POLICY[start:TASK_POLICY.index(';', start) + 1]
    result = scoped_tool(execute_sql_query)(query=query, execution_mode='customer')
    assert result['ok'], result
    assert sum(row[result['columns'].index('products')] for row in result['rows']) == 3


def test_sql_product_pages_through_real_agent_and_mcp(monkeypatch):
    import agent.billing_agent as module
    from autogen_ext.models.replay import ReplayChatCompletionClient
    from test_agent_lifecycle import tool_call
    sql = 'SELECT i.item_code, i.name FROM items i ORDER BY i.name, i.item_code LIMIT ? OFFSET ?'
    observed = []
    class Capture(ReplayChatCompletionClient):
        async def create(self, messages, **kwargs):
            observed.append(messages)
            return await super().create(messages, **kwargs)
    replay = Capture([
        tool_call('execute_sql_query', query='SELECT COUNT(*) AS products FROM items'),
        tool_call('execute_sql_query', query=sql, parameters=[1, 0]),
        json.dumps({'reply': 'Showing product 1 of 3.', 'shopping_update': {}}),
        tool_call('execute_sql_query', query=sql, parameters=[1, 1]),
        json.dumps({'reply': 'Showing product 2 of 3.', 'shopping_update': {}}),
    ])
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)
    async def run():
        engine = module.BillingAgent()
        await engine.start()
        try:
            first = await engine.chat('list products one per page', [], None)
            second = await engine.chat('next page', [
                {'role': 'user', 'content': 'list products one per page'},
                {'role': 'assistant', 'content': first['response']}], None,
                shopping_state=first['shopping_state'])
            assert second['response'] == 'Showing product 2 of 3.'
            assert len(second['shopping_state']['known_products']) == 2
            assert second['shopping_state']['product_query']['parameters'] == [1, 1]
            for messages in observed:
                assert 'catalogue' not in messages[0].content.lower()
        finally:
            await engine.close()
    asyncio.run(run())

