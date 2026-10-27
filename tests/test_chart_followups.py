"""Verified analytics recipes survive a chat resume without becoming fresh facts."""
from copy import deepcopy
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from api.main import app
from config import settings
from context import context_manager as context
from test_analytics_gallery import gallery_orders


def overview(**filters):
    return {'kind': 'store_overview', 'chart_mode': 'overview', 'filters': {
        'customer_id': None, 'order_id': None, 'date_from': None, 'date_to': None, **filters}}


def save(cid, recipe, message='Show analytics'):
    rid = str(uuid4())
    context.begin(cid, rid, message)
    context.finish(cid, rid, {'response': 'The verified report is ready.',
                             **({'analytics_context': recipe} if recipe is not None else {})})
    return rid


def test_latest_recipe_preserves_exact_requested_scope_and_survives_reinitialization():
    cid = context.conversation(mode='admin')
    recipe = overview(customer_id='CUST001', date_from='2026-01-01', date_to='2026-09-30')
    save(cid, recipe)
    context.initialize()
    assert context.latest_analytics_context(cid) == recipe
    # The recipe is kept outside the model's transcript, not added as old values.
    assert all('analytics_context' not in message for message in context.history(cid))
    context.begin(cid, str(uuid4()), 'Put it in charts')
    assert context.latest_analytics_context(cid) == recipe


def test_an_unrelated_completed_reply_clears_previous_analytics_reference():
    cid = context.conversation(mode='admin')
    save(cid, overview())
    save(cid, None, 'What does a refund mean?')
    assert context.latest_analytics_context(cid) is None


@pytest.mark.parametrize('mode', ['customer', 'cashier'])
def test_report_context_is_never_taken_from_another_role(mode):
    cid = context.conversation(mode=mode)
    save(cid, overview(customer_id='CUST002'))
    assert context.latest_analytics_context(cid) is None


def test_analytics_reference_does_not_cross_conversations():
    cid = context.conversation(mode='admin')
    other = context.conversation(mode='admin')
    save(cid, overview(customer_id='CUST001'))
    save(other, overview(customer_id='CUST002'))
    assert context.latest_analytics_context(cid)['filters']['customer_id'] == 'CUST001'
    assert context.latest_analytics_context(other)['filters']['customer_id'] == 'CUST002'


@pytest.mark.parametrize('recipe', [
    {'kind': 'invoice_trend', 'arguments': {'date_from': '2026-01-01', 'date_to': '2026-09-30',
        'customer_id': 'CUST001', 'trend_period': 'month', 'trend_metric': 'net_billed', 'chart_type': 'line'}},
    {'kind': 'billing_analysis', 'arguments': {'report': 'refund_reconciliation',
        'date_from': '2026-01-01', 'date_to': '2026-09-30'}},
    {'kind': 'sql', 'sources': [{'query': 'SELECT category,SUM(line_total) AS sales FROM invoice_lines WHERE item_code=? GROUP BY category',
        'parameters': ['ITEM001'], 'columns': ['category', 'sales']}]},
])
def test_non_overview_recipes_preserve_metric_and_query_parameters(recipe):
    cid = context.conversation(mode='admin')
    save(cid, recipe)
    assert context.latest_analytics_context(cid) == recipe


@pytest.mark.parametrize('recipe', [
    None, [], {'kind': 'unknown'},
    {'kind': 'store_overview', 'filters': {'customer_id': 'CUST001'}},
    overview(date_from='2026-02-30'), overview(date_from='2026-10-23', date_to='2026-01-01'),
    overview(customer_id='CUST001; DELETE FROM orders'),
    {**overview(), 'chart_mode': 'unrecognized'},
    {**overview(), 'role': 'admin'},
    {'kind': 'sql', 'sources': [{'query': 'SELECT 1', 'parameters': [], 'columns': ['x', 'x']}]},
    {'kind': 'sql', 'sources': [{'query': 'SELECT ?', 'parameters': [float('nan')], 'columns': ['x']}]},
    {'kind': 'billing_analysis', 'arguments': {'report': 'store_overview', 'confirmed': True}},
    {'kind': 'invoice_trend', 'arguments': {'date_from': '2026-01-01', 'date_to': '2025-01-01'}},
])
def test_invalid_or_permission_bearing_descriptors_are_not_reused(recipe):
    assert context.sanitize_analytics_context(recipe) is None


def test_corrupted_saved_response_does_not_restore_earlier_context():
    cid = context.conversation(mode='admin')
    save(cid, overview())
    rid = save(cid, overview(customer_id='CUST002'))
    with context.connection() as conn:
        conn.execute("UPDATE messages SET response_json=? WHERE conversation_id=? AND request_id=? AND role='user'",
                     ('invalid JSON', cid, rid))
    assert context.latest_analytics_context(cid) is None


class RecipeEngine:
    supports_analytics_context = True

    def __init__(self):
        self.calls = []

    async def chat(self, message, history, customer_id, **kwargs):
        self.calls.append({'message': message, 'history': deepcopy(history), 'kwargs': deepcopy(kwargs)})
        return {'response': 'The report is ready.',
                'analytics_context': overview(date_from='2026-01-01', date_to='2026-09-30')}


def test_api_passes_scoped_recipe_after_authorization_and_replays_without_another_read(monkeypatch):
    monkeypatch.setattr(settings, 'API_KEY', '')
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'chart-followup-admin-key-1234')
    engine = RecipeEngine()
    app.state.engine = engine
    headers = {'X-Admin-Key': settings.ADMIN_API_KEY}
    with TestClient(app) as client:
        cid = client.post('/conversations', json={'mode': 'admin'}, headers=headers).json()['conversation_id']
        body = {'conversation_id': cid, 'request_id': str(uuid4()), 'message': 'Show analytics'}
        first = client.post('/chat', json=body, headers=headers)
        assert first.status_code == 200, first.text
        recipe = first.json()['analytics_context']
        assert engine.calls[0]['kwargs']['analytics_context'] is None
        assert client.post('/chat', json=body, headers=headers).json() == first.json()
        assert len(engine.calls) == 1
        assert client.get('/conversations/' + cid).status_code == 403
        resumed = client.get('/conversations/' + cid, headers=headers)
        assert resumed.status_code == 200
        assert resumed.json()['analytics_context'] == recipe
        context.initialize()
        following = {**body, 'request_id': str(uuid4()), 'message': 'Put it in charts'}
        assert client.post('/chat', json=following).status_code == 403
        assert len(engine.calls) == 1
        response = client.post('/chat', json=following, headers=headers)
        assert response.status_code == 200, response.text
        assert engine.calls[-1]['kwargs']['analytics_context'] == recipe
        assert all('analytics_context' not in item for item in engine.calls[-1]['history'])


@pytest.mark.parametrize('mode', ['customer', 'cashier'])
def test_api_does_not_supply_admin_recipes_to_customer_or_cashier(monkeypatch, mode):
    monkeypatch.setattr(settings, 'API_KEY', '')
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'chart-followup-admin-key-1234')
    monkeypatch.setattr(settings, 'CASHIER_API_KEY', 'chart-followup-cashier-key-1234')
    engine = RecipeEngine()
    app.state.engine = engine
    headers = {'X-Cashier-Key': settings.CASHIER_API_KEY} if mode == 'cashier' else {}
    with TestClient(app) as client:
        cid = client.post('/conversations', json={'mode': mode}, headers=headers).json()['conversation_id']
        save(cid, overview(customer_id='CUST002'))
        body = {'conversation_id': cid, 'request_id': str(uuid4()), 'message': 'Put it in charts'}
        response = client.post('/chat', json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert 'analytics_context' not in engine.calls[-1]['kwargs']
        assert 'analytics_context' not in client.get('/conversations/' + cid, headers=headers).json()


def test_api_chained_gallery_reads_fresh_data_preserves_scope_and_replays(monkeypatch, gallery_orders):
    from database.connection import transaction
    from test_end_to_end import setup_engine, call
    from mcp_server.tools.analytics_tools import billing_analysis
    selected = dict(customer_id='CUST001', date_from='2026-01-01', date_to='2026-03-31')
    applicable = billing_analysis(report='store_overview', chart_mode='all', **selected)
    assert applicable['ok'], applicable
    chart_count = len(applicable['chart_results'])
    setup_engine(monkeypatch, [call('billing_analysis', report='store_overview', chart_mode='overview', **selected),
        'The requested store overview is ready.',
        call('billing_analysis', report='store_overview', chart_mode='dashboard', **selected),
        'The requested dashboard is attached.',
        call('billing_analysis', report='store_overview', chart_mode='all', **selected),
        f'{chart_count} charts are attached.'])
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'gallery-chain-admin-key-12345')
    headers = {'X-Admin-Key': settings.ADMIN_API_KEY}
    with TestClient(app) as client:
        cid = client.post('/conversations', json={'mode': 'admin'}, headers=headers).json()['conversation_id']
        def chat(text):
            body = {'conversation_id': cid, 'request_id': str(uuid4()), 'message': text}
            response = client.post('/chat', json=body, headers=headers)
            assert response.status_code == 200, response.text
            return body, response.json()
        _, first = chat('Show store analytics for customer CUST001 from 2026-01-01 to 2026-03-31')
        assert len(first['charts']) == 2
        first_total = next(table for table in first['tables'] if table['title'] == 'Invoice totals by currency')['rows'][0]['charged_price']
        with transaction() as conn:
            conn.execute("UPDATE orders SET charged_price=charged_price+10,tax_amount=tax_amount+10 WHERE order_id='GALLERY-1'")
        _, dashboard = chat('Put it in charts')
        assert len(dashboard['charts']) > 6
        assert dashboard['tables'] == []
        assert dashboard['analytics_context']['filters'] == {**overview()['filters'], **selected}
        from decimal import Decimal
        updated = next(dataset for dataset in dashboard['reports'][0]['datasets']
                       if dataset['name'] == 'invoice_totals')['rows'][0]['charged_price']
        assert Decimal(updated) == Decimal(first_total) + 10
        body, complete = chat('Show me all charts possible with current data')
        from mcp_server.charts import CHART_TYPES
        assert {chart['type'] for chart in complete['charts']} == set(CHART_TYPES)
        assert complete['tables'] == []
        assert f"{len(complete['charts'])} charts are attached" in complete['response']
        assert 'Unavailable views:' not in complete['response']
        assert complete['analytics_context']['filters'] == dashboard['analytics_context']['filters']
        assert client.post('/chat', json=body, headers=headers).json() == complete
        assert len(app.state.engine._client.create_calls) == 6
        saved = client.get('/conversations/' + cid, headers=headers).json()
        assert saved['messages'][-1]['charts'] == complete['charts']
        assert saved['analytics_context'] == complete['analytics_context']


def test_actual_chat_ui_renders_complete_gallery_and_retains_it_on_rerun(monkeypatch, gallery_orders):
    import httpx
    from streamlit.testing.v1 import AppTest
    from test_end_to_end import setup_engine, call
    setup_engine(monkeypatch, [call('billing_analysis', report='store_overview', chart_mode='all'),
                              'All applicable charts are attached.'])
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'gallery-ui-admin-key-12345')
    original, stream = httpx.Client.request, httpx.Client.stream
    with TestClient(app) as client:
        def bridge(self, method, url, **kwargs):
            return original(self, method, url, **kwargs) if self is client else client.request(method, url, **kwargs)
        def bridge_stream(self, method, url, **kwargs):
            return stream(self, method, url, **kwargs) if self is client else client.stream(method, url, **kwargs)
        monkeypatch.setattr(httpx.Client, 'request', bridge)
        monkeypatch.setattr(httpx.Client, 'stream', bridge_stream)
        ui = AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'), default_timeout=90).run()
        ui.radio[0].set_value('Admin').run()
        next(field for field in ui.text_input if field.label == 'Admin key').set_value(settings.ADMIN_API_KEY)
        next(button for button in ui.button if button.label == 'Unlock admin mode').click().run()
        ui.chat_input[0].set_value('Show me all charts possible with current data').run()
        assert not ui.exception and not ui.error and not ui.warning
        count = len(ui.session_state['messages'][-1]['charts'])
        from mcp_server.charts import CHART_TYPES
        assert {chart['type'] for chart in ui.session_state['messages'][-1]['charts']} == set(CHART_TYPES)
        assert count >= len(CHART_TYPES) and len(ui.get('vega_lite_chart')) == count
        assert len(ui.get('download_button')) == 16
        ui.run()
        assert not ui.exception and len(ui.get('vega_lite_chart')) == count


def test_query_chart_followup_keeps_bound_filters_and_reads_updated_values(monkeypatch):
    from database.connection import transaction
    from test_end_to_end import setup_engine, call
    sql = 'SELECT name,availablequantity AS quantity FROM items WHERE item_code=?'
    setup_engine(monkeypatch, [call('execute_sql_query', query=sql, parameters=['ITEM001']),
        'Here is the stock for the requested item.',
        call('execute_sql_query', query=sql, parameters=['ITEM001'],
             chart={'type':'horizontal_bar','x':'name','y':['quantity']}), 'The chart is attached.'])
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'query-chain-admin-key-12345')
    headers = {'X-Admin-Key': settings.ADMIN_API_KEY}
    with TestClient(app) as client:
        cid = client.post('/conversations', json={'mode':'admin'}, headers=headers).json()['conversation_id']
        def chat(message):
            result = client.post('/chat', json={'conversation_id':cid,'request_id':str(uuid4()),'message':message}, headers=headers)
            assert result.status_code == 200, result.text
            return result.json()
        first = chat('Show the current stock for ITEM001')
        assert first['analytics_context']['kind'] == 'sql'
        with transaction() as conn:
            conn.execute("UPDATE items SET availablequantity=37 WHERE item_code='ITEM001'")
        second = chat('Put that in a horizontal bar chart')
        assert second['charts'][0]['type'] == 'horizontal_bar'
        assert second['charts'][0]['data'][0]['quantity'] == 37
        prompt = app.state.engine._client.create_calls[2]['messages'][0].content
        assert 'PREVIOUS VERIFIED ANALYSIS RECIPE' in prompt and sql in prompt and 'ITEM001' in prompt
