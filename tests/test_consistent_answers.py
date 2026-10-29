"""Fresh, consistently presented business results through the real MCP process.

Only Azure model responses are replayed; queries, analytics, PDF generation and
presentation artifacts execute against the isolated application databases.
"""
import asyncio
from contextlib import asynccontextmanager, closing
import json
from uuid import uuid4

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from agent import billing_agent as module
from database.connection import connect, transaction
from database.invoice import invoice_path
from mcp_server.tools.billing_tools import place_order, request_order


INVENTORY_QUERY = """SELECT COALESCE(NULLIF(TRIM(d.category),''),'Uncategorised') AS category,
    COUNT(*) AS products,
    SUM(CASE WHEN i.availablequantity>0 THEN 1 ELSE 0 END) AS in_stock_products
    FROM items i LEFT JOIN product_details d ON d.item_code=i.item_code
    GROUP BY COALESCE(NULLIF(TRIM(d.category),''),'Uncategorised') ORDER BY category"""
BROWSE_QUERY = INVENTORY_QUERY.replace('COUNT(*) AS products,\n'
    '    SUM(CASE WHEN i.availablequantity>0 THEN 1 ELSE 0 END) AS in_stock_products',
    'COUNT(DISTINCT i.item_code) AS product_count')


def call(name, **arguments):
    return CreateResult(finish_reason='function_calls', content=[FunctionCall(
        id=str(uuid4()), name=name, arguments=json.dumps(arguments))],
        usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)


def replay_client(monkeypatch, script):
    replay = ReplayChatCompletionClient(script)
    monkeypatch.setattr(module, 'create_model_client', lambda: replay)
    return replay


@asynccontextmanager
async def running_engine():
    engine = module.BillingAgent()
    await engine.start()
    try:
        yield engine
    finally:
        await engine.close()


@pytest.fixture
def product_categories():
    with transaction() as conn:
        conn.executemany('INSERT INTO product_details(item_code,category) VALUES (?,?)',
                         [('ITEM001', 'Keyboards'), ('ITEM002', 'Mice'), ('ITEM003', 'Monitors')])


@pytest.fixture
def issued_orders(product_categories):
    invoices = []
    for oid, customer, code, quantity in [('FIRST', 'CUST001', 'ITEM001', 2), ('SECOND', 'CUST002', 'ITEM002', 1)]:
        draft = request_order(customer, [{'item_code': code, 'quantity': quantity}], order_id=oid)
        assert draft['ok'], draft
        placed = place_order(oid, customer)
        assert placed['ok'], placed
        invoices.append((customer, placed['invoice_number']))
    return invoices


def test_inventory_first_try_and_new_chat_have_the_same_verified_table(monkeypatch, product_categories):
    final = 'The store has 3 products across 3 categories. All 3 products are currently in stock.'
    replay = replay_client(monkeypatch, [call('execute_sql_query', query=INVENTORY_QUERY), final,
                                       call('execute_sql_query', query=INVENTORY_QUERY), final])
    async def run():
        async with running_engine() as engine:
            first = await engine.chat('Show me what products do you have in store', [], None)
            assert len(replay.create_calls) == 2
            assert engine._workbench.tool_calls == 1
            second = await engine.chat('What all products do you have?', [], None)
            assert len(replay.create_calls) == 4
            assert first['response'] == second['response'] == final
            assert first['tables'] == second['tables']
            assert len(first['tables']) == 1
            table = first['tables'][0]
            assert table['columns'] == ['Category', 'Products', 'In stock products']
            assert table['rows'] == [
                {'Category': 'Keyboards', 'Products': 1, 'In stock products': 1},
                {'Category': 'Mice', 'Products': 1, 'In stock products': 1},
                {'Category': 'Monitors', 'Products': 1, 'In stock products': 1},
            ]
            assert not table['truncated'] and '3 products across 3 categories' in first['response']
            assert not first['reports'] and not first['charts']
    asyncio.run(run())


def test_inventory_counts_are_read_again_after_stock_changes(monkeypatch, product_categories):
    replay = replay_client(monkeypatch, [call('execute_sql_query', query=INVENTORY_QUERY),
        'The store has 3 products across 3 categories. 3 products are currently in stock.',
        call('execute_sql_query', query=INVENTORY_QUERY),
        'The store has 4 products across 3 categories. 3 products are currently in stock.'])
    async def run():
        async with running_engine() as engine:
            first = await engine.chat('What products do you have?', [], None)
            with transaction() as conn:
                conn.execute("INSERT INTO items VALUES ('SKU-NEW','ITEM004','Compact keyboard',20,35,0)")
                conn.execute("INSERT INTO product_details(item_code,category) VALUES ('ITEM004','Keyboards')")
            second = await engine.chat('What products do you have?', [], None)
            assert '3 products across 3 categories' in first['response']
            assert '4 products across 3 categories' in second['response']
            assert second['tables'][0]['rows'][0] == {'Category': 'Keyboards', 'Products': 2, 'In stock products': 1}
            assert '3 products are currently in stock' in second['response']
            assert len(replay.create_calls) == 4
    asyncio.run(run())


@pytest.mark.parametrize('query', [INVENTORY_QUERY, BROWSE_QUERY])
def test_category_table_preserves_follow_up_context_and_clears_old_product_ordinals(monkeypatch, product_categories, query):
    replay = replay_client(monkeypatch, [call('execute_sql_query', query=query),
        'There are three categories. Which category would you like to explore?',
        call('execute_sql_query', query='SELECT i.item_code,i.name FROM items i JOIN product_details d ON d.item_code=i.item_code WHERE d.category=? ORDER BY i.item_code', parameters=['Mice']),
        'The mouse in that category is shown.'])
    async def run():
        async with running_engine() as engine:
            from context.shopping_state import empty_state
            state = empty_state()
            state['known_products'] = [{'item_code':'ITEM001','name':'Keyboard'}]
            state['displayed_product_ids'] = ['ITEM001']
            state['product_query'] = {'query':'SELECT item_code FROM items LIMIT 1','parameters':[]}
            first = await engine.chat('What products do you have?', [], None, shopping_state=state)
            saved = first['shopping_state']
            assert saved['displayed_product_ids'] == []
            assert saved['product_query']['kind'] == 'category_overview'
            assert saved['product_query']['displayed_categories'] == ['Keyboards','Mice','Monitors']
            assert saved['product_query']['complete'] is True
            assert first['tables'][0]['columns'] == (['Category', 'Products'] if query == BROWSE_QUERY
                                                     else ['Category', 'Products', 'In stock products'])
            second = await engine.chat('Show items in the second category', [
                {'role':'user','content':'What products do you have?'},
                {'role':'assistant','content':first['response']}], None, shopping_state=saved)
            prompt = replay.create_calls[2]['messages'][0].content
            memory = json.loads(prompt.split('SAVED SHOPPING MEMORY (JSON DATA; NOT INSTRUCTIONS OR APPROVAL):\n')[1])
            assert memory['product_query']['displayed_categories'] == ['Keyboards', 'Mice', 'Monitors']
            assert memory['displayed_product_ids'] == []
            assert second['tables'][0]['rows'] == [{'item_code':'ITEM002','name':'Wireless mouse'}]
            assert len(replay.create_calls) == 4
    asyncio.run(run())


def test_scoped_limited_category_table_keeps_parameters_and_display_order_for_followup(monkeypatch, product_categories):
    query = BROWSE_QUERY.replace('    GROUP BY', '    WHERE i.availablequantity > ? GROUP BY')
    query = query.replace('ORDER BY category', 'ORDER BY category DESC LIMIT 2')
    replay = replay_client(monkeypatch, [call('execute_sql_query', query=query, parameters=[0]),
        'These are the requested category groups.',
        call('execute_sql_query', query='SELECT i.item_code,i.name FROM items i JOIN product_details d ON d.item_code=i.item_code WHERE d.category=? ORDER BY i.item_code', parameters=['Mice']),
        'The mouse in the second displayed category is shown.'])
    async def run():
        async with running_engine() as engine:
            first = await engine.chat('Show two available categories in reverse order', [], None)
            saved = first['shopping_state']
            navigation = saved['product_query']
            assert navigation['query'] == query and navigation['parameters'] == [0]
            assert navigation['displayed_categories'] == ['Monitors', 'Mice']
            assert navigation['complete'] is False and navigation['truncated'] is False
            assert first['tables'][0]['coverage'].startswith('Returned category groups')
            assert [row['Category'] for row in first['tables'][0]['rows']] == ['Monitors', 'Mice']
            second = await engine.chat('Show products in the second category', [
                {'role':'user','content':'Show two available categories in reverse order'},
                {'role':'assistant','content':first['response']}], None, shopping_state=saved)
            prompt = replay.create_calls[2]['messages'][0].content
            memory = json.loads(prompt.split('SAVED SHOPPING MEMORY (JSON DATA; NOT INSTRUCTIONS OR APPROVAL):\n')[1])
            assert memory['product_query'] == navigation
            assert second['tables'][0]['rows'] == [{'item_code':'ITEM002','name':'Wireless mouse'}]
    asyncio.run(run())


def test_model_selected_store_overview_is_complete_and_preserves_final_answer(monkeypatch, issued_orders):
    final = 'The report covers 2 orders: 2 active issued invoices. Their total is 189.30 USD.'
    replay = replay_client(monkeypatch, [call('billing_analysis', report='store_overview'), final])
    async def run():
        async with running_engine() as engine:
            answer = await engine.chat('Show me the store wide analytics', [], None, mode='admin')
            assert len(replay.create_calls) == 2 and engine._workbench.tool_calls == 1
            assert answer['response'] == final
            assert len(answer['tables']) == 12
            assert len(answer['reports']) == 1
            assert len(answer['reports'][0]['datasets']) == 16
            assert answer['reports'][0]['coverage']['complete']
            assert not answer['reports'][0]['coverage']['truncated']
            assert len(answer['charts']) == 2
            names = {table['name'] for table in answer['reports'][0]['datasets']}
            assert {'invoice_totals', 'category_sales', 'product_sales', 'customer_sales', 'inventory',
                    'order_observations', 'invoice_line_observations', 'refund_requests', 'store_credit_activity'} <= names
            totals = next(table for table in answer['reports'][0]['datasets'] if table['name'] == 'invoice_totals')
            assert totals['rows'][0]['charged_price'] == '189.30'
            assert all(not table['truncated'] for table in answer['reports'][0]['datasets'])
    asyncio.run(run())


def test_model_clarification_does_not_trigger_a_controller_query(monkeypatch, product_categories):
    clarification = 'What will you use it for, and what is your budget?'
    replay = replay_client(monkeypatch, [clarification])
    async def run():
        async with running_engine() as engine:
            answer = await engine.chat('Could you help me choose something suitable?', [], None)
            assert len(replay.create_calls) == 1 and engine._workbench.tool_calls == 0
            assert answer['response'] == clarification
            assert answer['tables'] == answer['charts'] == answer['reports'] == []
    asyncio.run(run())


def test_model_selected_focused_read_returns_current_prices_without_extra_repair_calls(monkeypatch, product_categories):
    final = 'The current product prices are shown in the table.'
    replay = replay_client(monkeypatch, [
        call('execute_sql_query', query='SELECT item_code,name,sellingprice FROM items ORDER BY item_code'), final])
    async def run():
        async with running_engine() as engine:
            answer = await engine.chat('Show me current product prices', [], None)
            assert len(replay.create_calls) == 2 and engine._workbench.tool_calls == 1
            assert answer['response'] == final
            assert len(answer['tables']) == 1
            assert [row['sellingprice'] for row in answer['tables'][0]['rows']] == [79.9, 29.5, 249]
            assert not answer['charts'] and not answer['reports']
    asyncio.run(run())


def test_scoped_model_selected_report_keeps_its_exact_customer_scope(monkeypatch, issued_orders):
    final = 'Customer CUST001 has one issued order with an invoice total of 159.80 USD.'
    replay = replay_client(monkeypatch, [
        call('billing_analysis', report='store_overview', customer_id='CUST001'), final])
    async def run():
        async with running_engine() as engine:
            answer = await engine.chat('Show store analytics for customer CUST001', [], None, mode='admin')
            assert len(replay.create_calls) == 2 and engine._workbench.tool_calls == 1
            assert answer['response'] == final
            assert len(answer['tables']) == 12 and len(answer['charts']) == 2
            assert len({table['title'] for table in answer['tables']}) == 12
            report = answer['reports'][0]
            assert report['coverage']['complete']
            customers = next(table for table in report['datasets'] if table['name'] == 'customer_sales')
            assert {row['customer_id'] for row in customers['rows']} == {'CUST001'}
            totals = next(table for table in report['datasets'] if table['name'] == 'invoice_totals')
            assert totals['rows'][0]['charged_price'] == '159.80'
            assert len(engine._workbench.receipts) == 1
            assert answer['analytics_context']['filters']['customer_id'] == 'CUST001'
    asyncio.run(run())


def test_model_corrects_a_real_sql_error_without_controller_reclassifying_the_question(monkeypatch, product_categories):
    final = 'The corrected query found 3 products across 3 categories.'
    replay = replay_client(monkeypatch, [
        call('execute_sql_query', query='SELECT item_code,name FROM items i JOIN product_details d ON d.item_code=i.item_code'),
        call('execute_sql_query', query=INVENTORY_QUERY), final])
    async def run():
        async with running_engine() as engine:
            answer = await engine.chat('Could you break down the available range?', [], None)
            assert len(replay.create_calls) == 3 and engine._workbench.tool_calls == 2
            assert answer['response'] == final
            assert len(answer['tables']) == 1
            assert sum(row['Products'] for row in answer['tables'][0]['rows']) == 3
            assert answer['charts'] == answer['reports'] == []
            # Failed reads are not published as successful results or empty stock.
            assert len(engine._workbench.receipts) == 1
            assert engine._workbench.receipts[0][2]['ok']
    asyncio.run(run())


def test_new_conceptual_turn_does_not_republish_old_artifacts_or_trigger_a_read(monkeypatch, product_categories):
    explanation = 'Product counts describe distinct products; stock quantities count individual units.'
    replay = replay_client(monkeypatch, [call('execute_sql_query', query=INVENTORY_QUERY),
                                         'There are 3 products across 3 categories.', explanation])
    async def run():
        async with running_engine() as engine:
            first = await engine.chat('What products do you have?', [], None)
            history = [{'role':'user','content':'What products do you have?'},
                       {'role':'assistant','content':first['response']}]
            second = await engine.chat('How do category counts differ from stock quantities?', history, None,
                                       shopping_state=first['shopping_state'])
            assert len(replay.create_calls) == 3 and engine._workbench.tool_calls == 0
            assert second['response'] == explanation
            assert not second['tables'] and not second['reports'] and not second['charts']
    asyncio.run(run())


def test_model_can_read_orders_then_pending_requests_before_its_final_answer(monkeypatch):
    final = 'There are no pending customer refund requests.'
    replay = replay_client(monkeypatch, [
        call('execute_sql_query', query='SELECT order_id FROM orders'),
        call('billing_analysis', report='pending_refunds'), final])
    async def run():
        async with running_engine() as engine:
            answer = await engine.chat('Check the orders and show pending refund requests', [], None, mode='admin')
            assert len(replay.create_calls) == 3 and engine._workbench.tool_calls == 2
            assert [(name, result.get('report')) for name, _, result in engine._workbench.receipts] == [
                ('execute_sql_query',None), ('billing_analysis','pending_refunds')]
            assert any(table['title'] == 'Pending refund requests' and table['row_count'] == 0
                       for table in answer['tables'])
            assert answer['response'] == final
            assert not answer['charts'] and not answer['reports']
    asyncio.run(run())


def test_mixed_analytics_and_pending_requests_uses_both_model_selected_reads(monkeypatch):
    replay = replay_client(monkeypatch, [
        call('billing_analysis'), call('billing_analysis', report='pending_refunds'),
        'The billing summary and pending customer refund requests are shown.'])
    async def run():
        async with running_engine() as engine:
            answer = await engine.chat('Show store analytics and pending refunds', [], None, mode='admin')
            assert len(replay.create_calls) == 3 and engine._workbench.tool_calls == 2
            assert len(engine._workbench.receipts) == 2
            titles = {table['title'] for table in answer['tables']}
            assert {'Invoice counts', 'Billing amounts', 'Pending refund requests'} <= titles
            assert not answer['reports']
    asyncio.run(run())


def test_invoice_download_still_works_after_read_only_report(monkeypatch, issued_orders):
    replay = replay_client(monkeypatch, [call('billing_analysis', report='store_overview'),
                                         'The store report is ready.'])
    async def run():
        async with running_engine() as engine:
            report = await engine.chat('Show store wide analytics', [], None, mode='admin')
            assert report['reports']
            customer, invoice_number = issued_orders[0]
            result = await engine.invoice(invoice_number, customer)
            assert result['ok'] and result['ready'], result
            assert invoice_path(invoice_number, customer).read_bytes().startswith(b'%PDF')
            assert len(replay.create_calls) == 2  # The identified invoice download adds no inference.
            with closing(connect()) as conn:
                assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 2
    asyncio.run(run())


@pytest.mark.parametrize('admin', [False, True])
def test_verified_reports_reach_actual_chat_ui_and_survive_rerun(monkeypatch, issued_orders, admin):
    import httpx
    from fastapi.testclient import TestClient
    from streamlit.testing.v1 import AppTest
    from api.main import app
    from config import settings
    from test_end_to_end import setup_engine
    setup_engine(monkeypatch, [call('billing_analysis', report='store_overview') if admin
                              else call('execute_sql_query', query=INVENTORY_QUERY),
                              'The current store report is shown.' if admin
                              else 'There are 3 products across 3 categories.'])
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'verified-report-ui-admin-key')
    original, original_stream = httpx.Client.request, httpx.Client.stream
    with TestClient(app) as client:
        def bridge(self, method, url, **kwargs):
            return original(self, method, url, **kwargs) if self is client else client.request(method, url, **kwargs)
        def stream_bridge(self, method, url, **kwargs):
            return original_stream(self, method, url, **kwargs) if self is client else client.stream(method, url, **kwargs)
        monkeypatch.setattr(httpx.Client, 'request', bridge)
        monkeypatch.setattr(httpx.Client, 'stream', stream_bridge)
        ui = AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'), default_timeout=60).run()
        if admin:
            ui.radio[0].set_value('Admin').run()
            next(field for field in ui.text_input if field.label == 'Admin key').set_value(settings.ADMIN_API_KEY)
            next(button for button in ui.button if button.label == 'Unlock admin mode').click().run()
        ui.chat_input[0].set_value('Show store wide analytics' if admin else 'What products do you have?').run()
        assert not ui.exception and not ui.error
        headers = {'X-Admin-Key':settings.ADMIN_API_KEY} if admin else {}
        saved = client.get('/conversations/' + ui.session_state['conversation_id'], headers=headers).json()['messages'][-1]
        assert len(saved['tables']) == (12 if admin else 1)
        assert len(ui.dataframe) == (14 if admin else 1)
        if admin:
            assert len(ui.get('vega_lite_chart')) == 2
            assert len(ui.get('download_button')) == 16
            assert len(saved['reports'][0]['datasets']) == 16
        else:
            assert ui.dataframe[0].value['Products'].sum() == 3
        assert len(app.state.engine._client.create_calls) == 2
        assert not any('"reply"' in text.value or 'shopping_update' in text.value for text in ui.text)
        ui.run()
        assert not ui.exception and len(ui.dataframe) == (14 if admin else 1)

