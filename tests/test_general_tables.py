"""General explanations may use native tables without exposing transport JSON."""
import json

import pytest

from context.reply_text import plain_reply, split_reply_tables
from context.tables import MAX_ROWS, MAX_TABLES, sanitize_tables


COMPARISON = ('Draft and placed orders\n\n'
              '| Feature | Draft | Placed order |\n'
              '| :--- | ---: | :---: |\n'
              '| Inventory | Not reserved | Deducted on placement |\n'
              '| Invoice | Not issued | Issued |\n\n'
              'Ask to review a draft before placing it.')


def test_explanatory_comparison_becomes_a_native_table_without_duplicate_rows():
    cleaned, tables = split_reply_tables(COMPARISON)
    assert len(tables) == 1 and sanitize_tables(tables) == tables
    table = tables[0]
    assert table['title'] == 'Draft and placed orders'
    assert table['columns'] == ['Feature', 'Draft', 'Placed order']
    assert table['rows'][0] == {'Feature': 'Inventory', 'Draft': 'Not reserved',
                                'Placed order': 'Deducted on placement'}
    assert 'General explanation' in table['coverage']
    assert 'Ask to review a draft' in cleaned
    assert 'Not reserved' not in cleaned and 'Deducted on placement' not in cleaned
    assert '|' not in cleaned


def test_customer_envelope_preserves_shopping_state_and_other_fields_exactly():
    update = {'budget': '0.10', 'preferred_features': {'storage': '64 GB'},
              'known_products': [{'item_code': 'ITEM_005', 'name': 'José’s adapter'}],
              'pending_confirmation': None}
    source = {'reply': COMPARISON, 'shopping_update': update, 'unrelated_transport_field': {'version': 2}}
    content = json.dumps(source, ensure_ascii=False)
    cleaned, tables = split_reply_tables(content)
    parsed = json.loads(cleaned)
    assert parsed['shopping_update'] == update
    assert parsed['unrelated_transport_field'] == source['unrelated_transport_field']
    assert 'Not reserved' not in parsed['reply']
    assert 'shopping_update' not in plain_reply(cleaned)
    assert len(tables) == 1
    assert json.loads(content) == source


def test_response_alias_transport_also_survives():
    cleaned, tables = split_reply_tables(json.dumps({'response': COMPARISON, 'shopping_update': {'budget': None}}))
    parsed = json.loads(cleaned)
    assert 'response' in parsed and 'reply' not in parsed
    assert parsed['shopping_update'] == {'budget': None}
    assert len(tables) == 1


def test_plain_reply_legacy_behavior_is_unchanged():
    assert plain_reply('Choices\n| Product | Price |\n|---|---|\n| X | USD 10 |\n| Y | USD 20 |\nWhich one?') == (
        'Choices\nProduct: X; Price: USD 10.\nProduct: Y; Price: USD 20.\nWhich one?')


@pytest.mark.parametrize('content', [
    'Normal prose with no table.',
    'The symbol a | b is a text example.',
    '| Feature | Description |\n| Option | Details |',
    '| Feature | Description |\n| -- | -- |\n| Option | Details |',
    '| Feature | Feature |\n| --- | --- |\n| Option | Details |',
    '| Feature | Description |\n| --- | --- |\n| Option | Details | Extra |',
    '| Feature | Description |\n| --- | --- |\n| Correct | Row |\n| Incorrect | Row | Extra |',
    '| | Description |\n| --- | --- |\n| Option | Details |',
    '| Feature | Description |\n| --- | --- |\n| --- | --- |',
    '```markdown\n| Feature | Description |\n| --- | --- |\n| Option | Details |\n```',
    '~~~text\n| Feature | Description |\n| --- | --- |\n| Option | Details |\n~~~',
    '{"reply": null, "response": "| A | B |\\n|---|---|\\n| 1 | 2 |"}',
])
def test_non_tables_malformed_tables_and_code_examples_remain_unchanged(content):
    assert split_reply_tables(content) == (content, [])


def test_escaped_pipes_are_values_not_new_columns():
    cleaned, tables = split_reply_tables('| Choice | Description |\n| --- | --- |\n| A \\| B | Uses A or B |')
    assert tables[0]['rows'] == [{'Choice': 'A | B', 'Description': 'Uses A or B'}]
    assert cleaned == 'The explanation is shown in the table.'


def test_table_cells_preserve_ids_amount_spelling_unicode_units_and_literal_expressions():
    content = ('| Reference | Value |\n| --- | --- |\n'
               '| STORE-CUST-0005 | USD 00012.50 |\n'
               '| ITEM_005 | 0.10 |\n'
               '| Width | 12.5 cm |\n'
               '| Name | José 東京 |\n'
               '| Literal | =1+1 |\n'
               '| Infinity word | NaN |')
    cleaned, tables = split_reply_tables(content)
    values = [row['Value'] for row in tables[0]['rows']]
    assert values == ['USD 00012.50', '0.10', '12.5 cm', 'José 東京', '=1+1', 'NaN']
    assert tables[0]['rows'][1]['Reference'] == 'ITEM_005'
    assert cleaned == 'The explanation is shown in the table.'


@pytest.mark.parametrize('heading,title', [
    ('## Order stages', 'Order stages'),
    ('**Order stages**', 'Order stages'),
    ('Order stages:', 'Order stages'),
    ('Here is an explanation of order stages.', 'Explanation'),
])
def test_nearest_short_heading_supplies_the_table_title(heading, title):
    _, tables = split_reply_tables(heading + '\n\n| Stage | Meaning |\n|---|---|\n| Draft | Reviewable |')
    assert tables[0]['title'] == title


def test_valid_tables_after_code_fences_and_invalid_tables_still_extract():
    content = ('```\n| A | B |\n|---|---|\n| Hidden | Example |\n```\n\n'
               '| Bad | Bad |\n|---|---|\n| Duplicate | Header |\n\n'
               'Comparison\n| Option | Meaning |\n|---|---|\n| Customer | Shop |')
    cleaned, tables = split_reply_tables(content)
    assert len(tables) == 1
    assert tables[0]['rows'] == [{'Option': 'Customer', 'Meaning': 'Shop'}]
    assert 'Hidden' in cleaned and 'Duplicate' in cleaned
    assert '| Customer | Shop |' not in cleaned


def test_empty_explanation_table_is_valid_and_explicit():
    cleaned, tables = split_reply_tables('| Option | Meaning |\n|---|---|')
    assert tables[0]['rows'] == [] and tables[0]['total_rows'] == 0
    assert plain_reply(cleaned) == 'The explanation is shown in the table.'


def test_overlong_cells_are_rejected_without_discarding_the_original_answer():
    content = '| Feature | Description |\n|---|---|\n| Option | ' + 'x' * 2001 + ' |'
    assert split_reply_tables(content) == (content, [])


def test_display_row_limit_is_disclosed_rather_than_silently_losing_rows():
    content = '| Option | Meaning |\n|---|---|\n' + '\n'.join(
        f'| Option {index} | Meaning {index} |' for index in range(MAX_ROWS + 2))
    _, tables = split_reply_tables(content)
    assert tables[0]['row_count'] == MAX_ROWS
    assert tables[0]['total_rows'] == MAX_ROWS + 2 and tables[0]['truncated']


def test_table_count_limit_leaves_remaining_content_for_plain_reply():
    content = '\n\n'.join(f'| Option | Meaning |\n|---|---|\n| Choice {index} | General |'
                          for index in range(MAX_TABLES + 1))
    cleaned, tables = split_reply_tables(content)
    assert len(tables) == MAX_TABLES
    assert f'Choice {MAX_TABLES}' in cleaned
    assert f'Option: Choice {MAX_TABLES}' in plain_reply(cleaned)


def test_multiple_tables_keep_intervening_explanation_in_order():
    content = ('First comparison\n| Stage | Meaning |\n|---|---|\n| Draft | Review |\n\n'
               'This separates ordering from the return process.\n\n'
               'Return stages\n| Stage | Meaning |\n|---|---|\n| Requested | Awaiting approval |\n\n'
               'Only staff approve an eligible request.')
    cleaned, tables = split_reply_tables(content)
    assert [table['title'] for table in tables] == ['First comparison', 'Return stages']
    assert cleaned.index('This separates') < cleaned.index('Only staff')
    assert 'Awaiting approval' not in cleaned


def test_explanation_table_renders_once_as_native_streamlit_dataframe():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
import streamlit as st
from context.reply_text import split_reply_tables, plain_reply
from ui.tables import render_tables
content, tables = split_reply_tables(st.session_state['answer'])
st.text(plain_reply(content))
render_tables(tables)
''', default_timeout=30)
    app.session_state['answer'] = json.dumps({'reply': COMPARISON, 'shopping_update': {}})
    app.run()
    assert not app.exception and not app.error
    assert len(app.dataframe) == 1
    assert app.dataframe[0].value.to_dict('records')[0]['Draft'] == 'Not reserved'
    assert not any('Not reserved' in entry.value or 'shopping_update' in entry.value for entry in app.text)
    app.run()
    assert len(app.dataframe) == 1


@pytest.mark.parametrize('mode', ['customer', 'admin'])
def test_real_chat_ui_general_permissions_table_uses_one_model_call_and_retains_memory(monkeypatch, mode):
    import httpx
    from fastapi.testclient import TestClient
    from streamlit.testing.v1 import AppTest
    from api.main import app
    from config import settings
    from test_end_to_end import setup_engine

    explanation = ('Store permissions\n\n| Mode | Typical access |\n|---|---|\n'
                   '| Customer | Own verified account and shopping |\n'
                   '| Cashier | Review requests within the refund limit |\n'
                   '| Admin | Store analytics and authorized approvals |\n\n'
                   'Refund approval requires an existing customer request.')
    update = {'needs': ['Understand customer and staff permissions']}
    content = json.dumps({'reply': explanation, 'shopping_update': update}) if mode == 'customer' else explanation
    setup_engine(monkeypatch, [content])
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'general-tables-admin-key-12345')
    original, original_stream = httpx.Client.request, httpx.Client.stream
    with TestClient(app) as client:
        def bridge(self, method, url, **kwargs):
            return original(self, method, url, **kwargs) if self is client else client.request(method, url, **kwargs)

        def stream_bridge(self, method, url, **kwargs):
            return original_stream(self, method, url, **kwargs) if self is client else client.stream(method, url, **kwargs)

        monkeypatch.setattr(httpx.Client, 'request', bridge)
        monkeypatch.setattr(httpx.Client, 'stream', stream_bridge)
        ui = AppTest.from_file(str(settings.ROOT / 'ui/streamlit_app.py'), default_timeout=60).run()
        if mode == 'admin':
            ui.radio[0].set_value('Admin').run()
            next(field for field in ui.text_input if field.label == 'Admin key').set_value(settings.ADMIN_API_KEY)
            next(button for button in ui.button if button.label == 'Unlock admin mode').click().run()
        ui.chat_input[0].set_value('Explain customer, cashier and admin permissions in a table').run()
        assert not ui.exception and not ui.error
        assert len(ui.dataframe) == 1
        assert ui.dataframe[0].value.to_dict('records') == [
            {'Mode': 'Customer', 'Typical access': 'Own verified account and shopping'},
            {'Mode': 'Cashier', 'Typical access': 'Review requests within the refund limit'},
            {'Mode': 'Admin', 'Typical access': 'Store analytics and authorized approvals'},
        ]
        answer = ui.session_state['messages'][-1]
        assert len(answer['tables']) == 1
        assert 'Own verified account and shopping' not in answer['content']
        assert '|' not in answer['content'] and 'shopping_update' not in answer['content']
        assert 'Refund approval requires' in answer['content']
        assert len(app.state.engine._client.create_calls) == 1
        assert app.state.engine._workbench.tool_calls == 0
        headers = {'X-Admin-Key': settings.ADMIN_API_KEY} if mode == 'admin' else {}
        saved = client.get('/conversations/' + ui.session_state['conversation_id'], headers=headers).json()
        assert saved['messages'][-1]['tables'] == answer['tables']
        if mode == 'customer':
            assert saved['shopping_state']['needs'] == update['needs']
        ui.run()
        assert not ui.exception and len(ui.dataframe) == 1
        assert len(app.state.engine._client.create_calls) == 1
