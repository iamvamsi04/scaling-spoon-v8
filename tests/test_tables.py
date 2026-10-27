from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from api.main import app
from config import settings
from context import context_manager as context
from context.tables import MAX_ROWS, sanitize_tables, table_artifact


def categories():
    return table_artifact('Products by category', ['Category', 'Products', 'In stock'], [
        {'Category': 'Power banks', 'Products': 20, 'In stock': 18},
        {'Category': 'Projectors', 'Products': 20, 'In stock': 20},
    ], coverage='All product categories, including products currently out of stock.')


def test_table_keeps_column_order_scalars_and_explicit_empty_result():
    table = table_artifact('Totals', ['Currency', 'Revenue', 'Count'], [['USD', 12.5, 3]])
    assert list(table['rows'][0]) == ['Currency', 'Revenue', 'Count']
    assert table['rows'][0] == {'Currency': 'USD', 'Revenue': 12.5, 'Count': 3}
    assert not table['truncated']
    assert table_artifact('Orders', ['Order'], [])['total_rows'] == 0
    assert sanitize_tables([table]) == [table]


@pytest.mark.parametrize('columns,rows', [
    (['Value', 'Value'], [[1, 2]]),
    (['Value'], [{'Other': 1}]),
    (['Value'], [[1, 2]]),
    (['Value'], [[float('nan')]]),
    (['Value'], [[float('inf')]]),
    (['Value'], [[{'nested': 1}]]),
    (['Value'], [['x' * 2001]]),
    ([''], [[1]]),
    ([], []),
    (['x'] * 31, []),
])
def test_invalid_tables_are_rejected_without_breaking_valid_artifacts(columns, rows):
    with pytest.raises(ValueError):
        table_artifact('Report', columns, rows)
    bad = {'title': 'Report', 'columns': columns, 'rows': rows}
    assert sanitize_tables([bad, categories()]) == [categories()]


def test_large_tables_are_bounded_and_truncation_survives_sanitizing():
    total_rows = MAX_ROWS + 300
    table = table_artifact('Order rows', ['Order'], [[number] for number in range(total_rows)])
    assert len(table['rows']) == MAX_ROWS
    assert table['row_count'] == MAX_ROWS and table['total_rows'] == total_rows and table['truncated']
    assert sanitize_tables([table, table]) == [table]
    paged = table_artifact('Orders', ['Order'], [[1]], total_rows=1000)
    assert paged['truncated'] and paged['total_rows'] == 1000
    for total in (-1, True, 0, 1.5):
        with pytest.raises(ValueError):
            table_artifact('Orders', ['Order'], [[1]], total_rows=total)


def test_history_artifacts_do_not_become_model_history():
    cid, rid = context.conversation(), str(uuid4())
    context.begin(cid, rid, 'Show products')
    context.finish(cid, rid, {'response': 'Here are the products.', 'tables': [categories()]})
    plain = context.history(cid)
    assert plain[-1] == {'role': 'assistant', 'content': 'Here are the products.'}
    assert context.history(cid, include_artifacts=True)[-1]['tables'] == [categories()]


def test_api_tables_survive_replay_and_resume_without_repeated_model_call():
    class TableEngine:
        calls = 0

        async def chat(self, *args, **kwargs):
            self.calls += 1
            return {'response': 'Products by category.', 'invoices': [],
                    'tables': [categories(), {'title': 'Bad', 'columns': ['x'], 'rows': [[float('nan')]]}]}

    engine = TableEngine()
    app.state.engine = engine
    with TestClient(app) as client:
        cid = client.post('/conversations', json={}).json()['conversation_id']
        body = {'conversation_id': cid, 'request_id': str(uuid4()), 'message': 'Show products'}
        first = client.post('/chat', json=body)
        assert first.status_code == 200
        assert first.json()['tables'] == [categories()]
        assert client.post('/chat', json=body).json() == first.json()
        assert engine.calls == 1
        resumed = client.get('/conversations/' + cid).json()['messages'][-1]
        assert resumed['tables'] == [categories()]


def test_ui_renders_real_table_once_and_preserves_it_after_resume(monkeypatch):
    cid = str(uuid4())

    def respond(self, method, url, **kwargs):
        if url.endswith('/health'):
            data = {'model_configured': True}
        elif url.endswith('/conversations'):
            data = {'conversation_id': cid}
        elif url.endswith('/chat'):
            data = {'response': 'Products by category.\n{"reply":"Products by category."}',
                    'tables': [categories()], 'invoices': []}
        elif url.endswith('/conversations/' + cid):
            data = {'conversation_id': cid, 'mode': 'customer', 'messages': [
                {'role': 'user', 'content': 'Show products'},
                {'role': 'assistant', 'content': 'Products by category.', 'tables': [categories()]},
            ]}
        else:
            raise AssertionError(url)
        return httpx.Response(200, json=data, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, 'request', respond)
    ui = AppTest.from_file(str(settings.ROOT / 'ui/streamlit_app.py'), default_timeout=60).run()
    ui.chat_input[0].set_value('Show products').run()
    assert not ui.exception
    assert len(ui.dataframe) == 1
    assert ui.dataframe[0].value.to_dict('records') == categories()['rows']
    assert sum(entry.value == 'Products by category.' for entry in ui.text) == 1
    assert not any('"reply"' in entry.value or '"rows"' in entry.value for entry in ui.text)
    ui.run()
    assert len(ui.dataframe) == 1
    next(button for button in ui.button if button.label == 'New conversation').click().run()
    assert not ui.dataframe
    next(field for field in ui.text_input if field.label == 'Resume conversation ID').set_value(cid).run()
    next(button for button in ui.button if button.label == 'Resume').click().run()
    assert not ui.exception and len(ui.dataframe) == 1
    assert ui.dataframe[0].value.to_dict('records') == categories()['rows']


def test_ui_reports_truncation_and_rejects_invalid_tables(monkeypatch):
    table = table_artifact('Orders', ['Order'], [['ORDER-1']], total_rows=500)

    def respond(self, method, url, **kwargs):
        return httpx.Response(200, json={'model_configured': True}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, 'request', respond)
    ui = AppTest.from_file(str(settings.ROOT / 'ui/streamlit_app.py'), default_timeout=60)
    ui.session_state['messages'] = [{'role': 'assistant', 'content': 'Order results.', 'tables': [
        {'title': 'Invalid', 'columns': ['x'], 'rows': [[{'nested': 1}]]}, table]}]
    ui.run()
    assert not ui.exception and len(ui.dataframe) == 1
    assert any(entry.value == 'Showing 1 of 500 rows.' for entry in ui.caption)


def test_source_truncation_preserves_unknown_total_through_saved_table_validation():
    table = table_artifact('Records',['Order'],[['ORDER-1']],truncated=True)
    assert table['truncated'] and table['total_rows'] is None
    assert sanitize_tables([table]) == [table]


def test_unknown_total_and_empty_page_captions_do_not_claim_full_coverage(tmp_path):
    script = tmp_path/'tables_ui.py'
    values = [table_artifact('Records',['Order'],[['ORDER-1']],truncated=True),
              table_artifact('Next page',['Order'],[])]
    script.write_text('from ui.tables import render_tables\nrender_tables('+repr(values)+')',encoding='utf-8')
    ui = AppTest.from_file(str(script),default_timeout=30).run()
    assert not ui.exception and len(ui.dataframe)==2
    captions = [entry.value for entry in ui.caption]
    assert any('total count is not available' in caption for caption in captions)
    assert 'No rows were returned for this request.' in captions
    assert not any('of None' in caption or 'No matching records' in caption for caption in captions)


def test_product_table_hides_internal_columns_without_changing_source_records():
    source = table_artifact('Products', ['SKU', 'Item code', 'name', 'price', 'product_count'],
                            [['SKU-KB', 'ITEM001', 'Mechanical keyboard', '79.90', 1]])
    ui = AppTest.from_string('''
import streamlit as st
from ui.tables import render_tables
render_tables([st.session_state['source']])
''', default_timeout=30)
    ui.session_state['source'] = source
    ui.run()
    assert not ui.exception and len(ui.dataframe) == 1
    assert ui.dataframe[0].value.to_dict('records') == [
        {'name':'Mechanical keyboard', 'price':'79.90', 'product_count':1}]
    assert ui.session_state['source'] == source
    assert source['rows'][0]['Item code'] == 'ITEM001'
    ui.run()
    assert list(ui.dataframe[0].value.columns) == ['name','price','product_count']
