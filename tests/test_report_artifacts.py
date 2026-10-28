import asyncio
import csv
import io
import json
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest
from autogen_core.tools import TextResultContent, ToolResult
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from agent import billing_agent as agent
from api.main import app
from config import settings
from context import context_manager as context
from context.reports import csv_bytes, report_artifact, sanitize_reports


def dataset():
    return {'name': 'orders', 'title': 'Invoice observations',
            'columns': ['order_id', 'currency', 'charged_price'],
            'rows': [[f'ORDER-{number:04}', 'USD', float(number)] for number in range(400)],
            'scope': 'Active placed invoices. All matching dates.',
            'grain': 'One row per invoice.', 'truncated': False}


def report():
    return report_artifact('Store analytics', [dataset()],
        coverage={'complete': True, 'truncated': False, 'top_n_applied': False,
                  'matched_order_date_from': '2026-01-01', 'matched_order_date_to': '2026-10-23',
                  'dataset_row_counts': {'orders': 400}, 'chart_omissions': []},
        notes=['Currencies are reported separately.'])


def scoped_reports():
    first, second = report(), report()
    for value, start, end in [(first, '2026-01-01', '2026-03-31'),
                              (second, '2026-04-01', '2026-06-30')]:
        value['coverage']['filters'] = {'customer_id': None, 'order_id': None,
                                        'date_from': start, 'date_to': end}
    return [first, second]


def test_distinct_scopes_and_values_survive_while_only_complete_duplicates_are_removed():
    first, second = scoped_reports()
    changed = deepcopy(first)
    changed['datasets'][0]['rows'][0]['charged_price'] = 12.5
    reordered = deepcopy(first)
    reordered['coverage'] = dict(reversed(list(first['coverage'].items())))
    assert sanitize_reports([first, second, deepcopy(first), changed, reordered]) == [first, second, changed]


@pytest.mark.parametrize('invalid', [None, {}, {'title': 'Broken', 'datasets': []}])
def test_malformed_first_report_does_not_hide_a_valid_later_report(invalid):
    first, second = scoped_reports()
    assert sanitize_reports([invalid, first, invalid, second]) == [first, second]
    with pytest.raises((TypeError, ValueError)):
        sanitize_reports([invalid, first], strict=True)


def test_combined_report_budget_is_explicit_and_never_silently_delivers_a_subset(monkeypatch):
    import context.reports as reports
    first, second = scoped_reports()
    size = len(json.dumps(first, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    monkeypatch.setattr(reports, 'MAX_REPORT_BYTES', size + size // 2)
    assert sanitize_reports([first], strict=True) == [first]
    assert sanitize_reports([first, deepcopy(first)], strict=True) == [first]
    with pytest.raises(ValueError, match='combined report attachments'):
        sanitize_reports([first, second], strict=True)
    assert sanitize_reports([first, second]) == []


def test_multiple_reports_are_not_limited_by_an_arbitrary_report_count():
    reports = [report_artifact('Report', [{'name': 'orders', 'title': 'Orders',
        'columns': ['count'], 'rows': [[number]]}]) for number in range(20)]
    assert sanitize_reports(reports, strict=True) == reports


def test_complete_reports_are_not_limited_to_display_table_size():
    saved = report()
    assert saved['datasets'][0]['row_count'] == 400
    assert len(saved['datasets'][0]['rows']) == 400
    assert not saved['datasets'][0]['truncated']
    assert sanitize_reports([saved]) == [saved]
    exported = list(csv.reader(io.StringIO(csv_bytes(saved['datasets'][0]).decode('utf-8-sig'))))
    assert len(exported) == 401
    assert exported[0] == dataset()['columns']
    assert exported[-1] == ['ORDER-0399', 'USD', '399.0']


@pytest.mark.parametrize('change', [
    lambda value: value.update(columns=['x', 'x']),
    lambda value: value.update(rows=[[float('nan'), 'USD', 1]]),
    lambda value: value.update(rows=[{'wrong_column': 1}]),
    lambda value: value.update(rows=[[{'nested': 1}, 'USD', 1]]),
    lambda value: value.update(name='../orders'),
    lambda value: value.update(rows=[[1, 'USD', 1]] * 10_001),
    lambda value: value.update(truncated='false'),
])
def test_malformed_report_data_is_never_persisted_or_rendered(change):
    raw = dataset()
    change(raw)
    with pytest.raises(ValueError):
        report_artifact('Analytics', [raw])
    assert sanitize_reports([{'title': 'Analytics', 'datasets': [raw]}]) == []


def test_report_names_limits_truncation_and_size_are_explicit(monkeypatch):
    raw = dataset()
    with pytest.raises(ValueError):
        report_artifact('Analytics', [raw, raw])
    with pytest.raises(ValueError):
        report_artifact('Analytics', [raw] * 17)
    raw.update(total_rows=1000)
    saved = report_artifact('Analytics', [raw], coverage={'complete': True, 'truncated': False})
    assert saved['datasets'][0]['truncated']
    assert saved['coverage'] == {'complete': False, 'truncated': True}
    import context.reports as reports
    monkeypatch.setattr(reports, 'MAX_REPORT_BYTES', 100)
    with pytest.raises(ValueError):
        report_artifact('Analytics', [raw])


def test_download_text_cannot_execute_formulas_and_numeric_values_stay_numeric():
    saved = report_artifact('Report', [{
        'name': 'products', 'title': 'Products', 'columns': ['name', 'amount'],
        'rows': [['=2+2', -5.5], ['\t@SUM(1,2)', 10], ['Power bank', 12.75],
                 ['Local credit redemption', '-5.00'], ['-1+2', '+3.50']],
    }])
    rows = list(csv.reader(io.StringIO(csv_bytes(saved['datasets'][0]).decode('utf-8-sig'))))
    assert rows == [['name', 'amount'], ["'=2+2", '-5.5'], ["'\t@SUM(1,2)", '10'], ['Power bank', '12.75'],
                    ['Local credit redemption', '-5.00'], ["'-1+2", '+3.50']]
    assert saved['datasets'][0]['rows'][0]['name'] == '=2+2'


def test_report_datasets_survive_api_replay_resume_and_stay_out_of_model_history():
    expected = scoped_reports()
    class ReportEngine:
        calls = 0
        histories = []

        async def chat(self, message, history, customer_id, **kwargs):
            self.calls += 1
            self.histories.append(history)
            return {'response': 'The report data is ready.', 'reports': deepcopy(expected)}

    engine = ReportEngine()
    app.state.engine = engine
    with TestClient(app) as client:
        cid = client.post('/conversations', json={}).json()['conversation_id']
        body = {'conversation_id': cid, 'request_id': str(uuid4()), 'message': 'Report'}
        first = client.post('/chat', json=body)
        assert first.status_code == 200
        assert first.json()['reports'] == expected
        assert client.post('/chat', json=body).json() == first.json()
        assert engine.calls == 1
        saved = client.get('/conversations/' + cid).json()['messages'][-1]
        assert saved['reports'] == expected
        assert client.post('/chat', json={**body, 'request_id': str(uuid4()), 'message': 'Follow up'}).status_code == 200
        assert engine.histories[-1][-1] == {'role': 'assistant', 'content': 'The report data is ready.'}
        assert all('reports' not in entry for entry in context.history(cid))


def test_ui_report_downloads_and_scope_survive_reruns_without_raw_json(monkeypatch):
    def respond(self, method, url, **kwargs):
        return httpx.Response(200, json={'model_configured': True}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, 'request', respond)
    ui = AppTest.from_file(str(settings.ROOT / 'ui/streamlit_app.py'), default_timeout=60)
    ui.session_state['messages'] = [{'role': 'assistant', 'content': 'The report data is ready.',
                                    'reports': scoped_reports()}]
    ui.run()
    assert not ui.exception
    assert len(ui.get('download_button')) == 2
    assert any(entry.label == 'Download report data' for entry in ui.expander)
    assert any(entry.value == '400 rows' for entry in ui.caption)
    assert any(entry.value == dataset()['scope'] for entry in ui.caption)
    assert not any('"datasets"' in entry.value or 'ORDER-0399' in entry.value for entry in ui.text)
    ui.run()
    assert not ui.exception and len(ui.get('download_button')) == 2


def overview_result(arguments):
    saved = report()
    filters = {key: arguments.get(key) for key in ('customer_id', 'order_id', 'date_from', 'date_to')}
    return {'ok': True, 'report': 'store_overview', 'chart_mode': 'none',
            'filters': filters, 'coverage': saved['coverage'], 'notes': saved['notes'],
            'summary': {'matched_orders': 400, 'invoice_count': 400, 'draft_count': 0, 'cancelled_count': 0},
            'datasets': saved['datasets'], 'chart_results': []}


def test_workbench_preserves_identical_observations_from_distinct_executed_scopes(monkeypatch):
    async def transport(self, name, arguments, *args, **kwargs):
        return ToolResult(name=name, is_error=False, result=[TextResultContent(
            content=json.dumps(overview_result(arguments)))])

    monkeypatch.setattr(agent.McpWorkbench, 'call_tool', transport)

    async def run():
        wb = agent.RecordingWorkbench()
        wb.mode = 'admin'
        first = {'report': 'store_overview', 'chart_mode': 'none',
                 'date_from': '2026-01-01', 'date_to': '2026-03-31'}
        second = {**first, 'date_from': '2026-04-01', 'date_to': '2026-06-30'}
        for arguments in [first, second, first]:
            result = await wb.call_tool('billing_analysis', arguments)
            assert agent.decode_result(result)['ok']
        assert len(wb.reports) == 2
        assert wb.reports[0]['datasets'] == wb.reports[1]['datasets']
        assert [value['coverage']['filters']['date_from'] for value in wb.reports] == ['2026-01-01', '2026-04-01']
        assert all('rows' not in value for value in agent.decode_result(result)['datasets'])
    asyncio.run(run())


def test_workbench_combined_overflow_returns_tool_error_before_attaching_or_claiming_next_report(monkeypatch):
    import context.reports as reports

    async def transport(self, name, arguments, *args, **kwargs):
        return ToolResult(name=name, is_error=False, result=[TextResultContent(
            content=json.dumps(overview_result(arguments)))])

    monkeypatch.setattr(agent.McpWorkbench, 'call_tool', transport)

    async def run():
        wb = agent.RecordingWorkbench()
        wb.mode = 'admin'
        first = {'report': 'store_overview', 'chart_mode': 'none', 'date_from': '2026-01-01'}
        result = await wb.call_tool('billing_analysis', first)
        assert agent.decode_result(result)['ok']
        before = deepcopy(wb.reports)
        size = len(json.dumps(before[0], ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        monkeypatch.setattr(reports, 'MAX_REPORT_BYTES', size + size // 2)
        result = await wb.call_tool('billing_analysis', {**first, 'date_from': '2026-04-01'})
        error = agent.decode_result(result)
        assert result.is_error and error['ok'] is False
        assert 'combined report attachments' in error['error']
        assert wb.reports == before
        assert len(wb.receipts) == 1
        assert len(wb.tables) == 1
    asyncio.run(run())


def test_real_mcp_multiple_report_scopes_survive_one_model_selected_batch(monkeypatch):
    first = {'report': 'store_overview', 'chart_mode': 'none',
             'date_from': '2026-01-01', 'date_to': '2026-03-31'}
    second = {**first, 'date_from': '2026-04-01', 'date_to': '2026-06-30'}
    replay = ReplayChatCompletionClient([
        CreateResult(finish_reason='function_calls', content=[FunctionCall(
            id=str(uuid4()), name='billing_analysis', arguments=json.dumps(arguments))
            for arguments in [first, second, first]],
            usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False),
        'The two requested scopes are available as separate report downloads.'])
    monkeypatch.setattr(agent, 'create_model_client', lambda: replay)

    async def run():
        engine = agent.BillingAgent()
        await engine.start()
        try:
            result = await engine.chat('Compare the first and second quarters as separate reports',
                                       [], None, mode='admin')
            assert len(result['reports']) == 2
            assert len(result['reports'][0]['datasets']) == 16
            assert len(result['reports'][1]['datasets']) == 16
            assert [saved['coverage']['filters']['date_from'] for saved in result['reports']] == [
                '2026-01-01', '2026-04-01']
            assert engine._workbench.tool_calls == 3
            assert len(replay.create_calls) == 2
        finally:
            await engine.close()
    asyncio.run(run())
