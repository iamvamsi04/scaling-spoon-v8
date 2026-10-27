import csv
import io
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

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
    class ReportEngine:
        calls = 0
        histories = []

        async def chat(self, message, history, customer_id, **kwargs):
            self.calls += 1
            self.histories.append(history)
            return {'response': 'The report data is ready.', 'reports': [report()]}

    engine = ReportEngine()
    app.state.engine = engine
    with TestClient(app) as client:
        cid = client.post('/conversations', json={}).json()['conversation_id']
        body = {'conversation_id': cid, 'request_id': str(uuid4()), 'message': 'Report'}
        first = client.post('/chat', json=body)
        assert first.status_code == 200
        assert first.json()['reports'] == [report()]
        assert client.post('/chat', json=body).json() == first.json()
        assert engine.calls == 1
        saved = client.get('/conversations/' + cid).json()['messages'][-1]
        assert saved['reports'] == [report()]
        assert client.post('/chat', json={**body, 'request_id': str(uuid4()), 'message': 'Follow up'}).status_code == 200
        assert engine.histories[-1][-1] == {'role': 'assistant', 'content': 'The report data is ready.'}
        assert all('reports' not in entry for entry in context.history(cid))


def test_ui_report_downloads_and_scope_survive_reruns_without_raw_json(monkeypatch):
    def respond(self, method, url, **kwargs):
        return httpx.Response(200, json={'model_configured': True}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, 'request', respond)
    ui = AppTest.from_file(str(settings.ROOT / 'ui/streamlit_app.py'), default_timeout=60)
    ui.session_state['messages'] = [{'role': 'assistant', 'content': 'The report data is ready.',
                                    'reports': [deepcopy(report())]}]
    ui.run()
    assert not ui.exception
    assert len(ui.get('download_button')) == 1
    assert any(entry.label == 'Download report data' for entry in ui.expander)
    assert any(entry.value == '400 rows' for entry in ui.caption)
    assert any(entry.value == dataset()['scope'] for entry in ui.caption)
    assert not any('"datasets"' in entry.value or 'ORDER-0399' in entry.value for entry in ui.text)
    ui.run()
    assert not ui.exception and len(ui.get('download_button')) == 1
