"""The model selects the task; verified tool results select its attachments.

Only inference is replayed. API authorization, saved context, MCP tools, SQLite
reads and chart artifacts run normally against the isolated databases.
"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config import settings
from database.connection import transaction
from mcp_server.charts import CHART_TYPES
from test_analytics_gallery import gallery_orders
from test_end_to_end import setup_engine, call


def admin_access(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'semantic-charts-admin-key-12345')
    return {'X-Admin-Key': settings.ADMIN_API_KEY}


def new_conversation(client, headers):
    response = client.post('/conversations', json={'mode': 'admin'}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()['conversation_id']


def chat(client, headers, cid, message):
    body = {'conversation_id': cid, 'request_id': str(uuid4()), 'message': message}
    response = client.post('/chat', json=body, headers=headers)
    assert response.status_code == 200, response.text
    return body, response.json()


@pytest.mark.parametrize('question', [
    'just list the names of charts which u can generate',
    'What visual formats are available? Give their names only.',
    'How does a bar chart differ from a scatter plot?',
    'Which chart would you recommend to explain seasonality?',
])
@pytest.mark.parametrize('after_report', [False, True])
def test_chart_discussion_does_not_force_queries_or_reuse_old_attachments(
        monkeypatch, gallery_orders, question, after_report):
    answer = ('Supported chart types: ' + ', '.join(CHART_TYPES) + '.'
              if 'names' in question or 'formats' in question else
              'A line chart can show changes over time; a scatter plot compares numeric measurements.')
    script = ([call('billing_analysis', report='store_overview', chart_mode='overview'),
               'The requested store overview is ready.']
              if after_report else []) + [answer]
    setup_engine(monkeypatch, script)
    headers = admin_access(monkeypatch)
    with TestClient(app) as client:
        cid = new_conversation(client, headers)
        if after_report:
            _, previous = chat(client, headers, cid, 'Show the overall store report')
            assert previous['charts'] and previous['tables'] and previous['reports']
        body, result = chat(client, headers, cid, question)
        assert result['response'] == answer
        assert result['charts'] == result['tables'] == result['reports'] == []
        assert app.state.engine._workbench.tool_calls == 0
        assert len(app.state.engine._client.create_calls) == len(script)
        assert client.post('/chat', json=body, headers=headers).json() == result
        assert len(app.state.engine._client.create_calls) == len(script)
        saved = client.get('/conversations/' + cid, headers=headers).json()
        assert saved['messages'][-1]['charts'] == []
        assert saved['messages'][-1]['tables'] == []
        assert saved['messages'][-1]['content'] == answer


def test_plain_data_question_does_not_trigger_an_unrequested_repair_call(monkeypatch):
    # This is an intentionally incomplete model answer. It tests the application
    # boundary: the controller must not invent a query from the user's words or
    # force a dashboard after the model has returned plain text.
    answer = 'Which period and measure should the analysis cover?'
    setup_engine(monkeypatch, [answer])
    headers = admin_access(monkeypatch)
    with TestClient(app) as client:
        cid = new_conversation(client, headers)
        _, result = chat(client, headers, cid, 'Could we look at the revenue?')
        assert result['response'] == answer
        assert result['charts'] == result['tables'] == result['reports'] == []
        assert app.state.engine._workbench.tool_calls == 0
        assert len(app.state.engine._client.create_calls) == 1


@pytest.mark.parametrize('mode,question', [
    ('overview', 'Show an overview of the store.'),
    ('dashboard', 'Present a varied dashboard of sales and stock.'),
    ('all', 'Show every meaningful supported visualization of the store data.'),
    ('none', 'Give the store analysis as numbers without visualizations.'),
])
def test_structured_model_report_mode_and_scope_are_not_rewritten(
        monkeypatch, gallery_orders, mode, question):
    selected = dict(customer_id='CUST001', date_from='2026-01-01', date_to='2026-03-31')
    setup_engine(monkeypatch, [call('billing_analysis', report='store_overview',
                                   chart_mode=mode, **selected),
                              'The requested store report is ready.'])
    headers = admin_access(monkeypatch)
    with TestClient(app) as client:
        cid = new_conversation(client, headers)
        _, result = chat(client, headers, cid, question)
        receipt = app.state.engine._workbench.receipts[-1]
        assert receipt[0] == 'billing_analysis'
        assert receipt[1]['chart_mode'] == mode
        assert all(receipt[1][key] == value for key, value in selected.items())
        assert result['analytics_context']['chart_mode'] == mode
        assert all(result['analytics_context']['filters'][key] == value for key, value in selected.items())
        assert len(app.state.engine._client.create_calls) == 2
        assert len(result['reports'][0]['datasets']) == 16
        if mode in ('dashboard', 'all'):
            assert result['tables'] == []
        else:
            assert len(result['tables']) == 12
        if mode == 'none':
            assert result['charts'] == []
        elif mode == 'overview':
            assert len(result['charts']) == 2
        elif mode == 'all':
            assert {chart['type'] for chart in result['charts']} == set(CHART_TYPES)
        else:
            assert len(result['charts']) > 6


def test_single_chart_after_dashboard_keeps_selected_query_filter_and_style(monkeypatch, gallery_orders):
    sql = 'SELECT name,availablequantity AS quantity FROM items WHERE item_code=?'
    setup_engine(monkeypatch, [call('billing_analysis', report='store_overview', chart_mode='all'),
        'All applicable store charts are attached.',
        call('execute_sql_query', query=sql, parameters=['ITEM001'],
             chart={'type': 'horizontal_bar', 'title': 'Keyboard stock', 'x': 'name', 'y': ['quantity']}),
        'The selected product stock is shown.'])
    headers = admin_access(monkeypatch)
    with TestClient(app) as client:
        cid = new_conversation(client, headers)
        _, first = chat(client, headers, cid, 'Show every applicable chart of the store data')
        assert len(first['charts']) > 6
        with transaction() as conn:
            conn.execute("UPDATE items SET availablequantity=37 WHERE item_code='ITEM001'")
        _, result = chat(client, headers, cid,
                         'Now only show ITEM001 stock as one horizontal bar chart, without a table.')
        assert len(result['charts']) == 1
        assert result['charts'][0]['type'] == 'horizontal_bar'
        assert result['charts'][0]['data'][0]['quantity'] == 37
        assert result['tables'] == result['reports'] == []
        receipt = app.state.engine._workbench.receipts[-1]
        assert receipt[0] == 'execute_sql_query'
        assert receipt[1]['query'] == sql and receipt[1]['parameters'] == ['ITEM001']
        assert result['analytics_context']['kind'] == 'sql'
        assert result['analytics_context']['sources'][0]['parameters'] == ['ITEM001']
        assert len(app.state.engine._client.create_calls) == 4


def test_followup_model_can_change_period_without_controller_changing_customer_scope(monkeypatch, gallery_orders):
    first_scope = dict(customer_id='CUST001', date_from='2026-01-01', date_to='2026-03-31')
    next_scope = {**first_scope, 'date_from': '2026-02-01', 'date_to': '2026-02-28'}
    setup_engine(monkeypatch, [call('billing_analysis', report='store_overview',
                                   chart_mode='overview', **first_scope),
                              'The first quarter report is ready.',
                              call('billing_analysis', report='store_overview',
                                   chart_mode='none', **next_scope),
                              'The February report for the same customer is ready.'])
    headers = admin_access(monkeypatch)
    with TestClient(app) as client:
        cid = new_conversation(client, headers)
        chat(client, headers, cid, 'Show the first quarter report for CUST001')
        _, result = chat(client, headers, cid, 'Use February instead and give only the numbers.')
        assert result['charts'] == []
        assert result['analytics_context']['filters'] == {**next_scope, 'order_id': None}
        assert app.state.engine._workbench.receipts[-1][1]['chart_mode'] == 'none'
        assert len(app.state.engine._client.create_calls) == 4
        prompt = app.state.engine._client.create_calls[-1]['messages'][0].content
        assert 'PREVIOUS VERIFIED ANALYSIS RECIPE' in prompt
        assert 'CUST001' in prompt and '2026-01-01' in prompt and '2026-03-31' in prompt


def test_a_complete_report_does_not_end_a_mixed_request_before_the_next_tool(monkeypatch, gallery_orders):
    answer = 'There is one pending refund request for GALLERY-1, amount 10.00 USD.'
    setup_engine(monkeypatch, [
        call('billing_analysis', report='store_overview', chart_mode='none'),
        call('billing_analysis', report='pending_refunds'),
        answer,
    ])
    headers = admin_access(monkeypatch)
    with TestClient(app) as client:
        cid = new_conversation(client, headers)
        _, result = chat(client, headers, cid, 'Show the store analytics and the pending customer refund requests.')
        assert result['response'] == answer
        assert result['charts'] == []
        assert app.state.engine._workbench.tool_calls == 2
        assert len(app.state.engine._client.create_calls) == 3
        receipts = app.state.engine._workbench.receipts
        assert [arguments['report'] for _, arguments, _ in receipts] == ['store_overview', 'pending_refunds']
        assert receipts[-1][2]['count'] == 1
        assert receipts[-1][2]['requests'][0]['order_id'] == 'GALLERY-1'
