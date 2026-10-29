"""Capitalized presentation never changes source data or hides full cell text."""
from copy import deepcopy
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from context.tables import table_artifact
from ui import tables


@pytest.mark.parametrize('value,expected', [
    ('power banks', 'Power banks'), ('pending', 'Pending'),
    ('USB-C with API support', 'USB-C with API support'),
    ('usb-C with API support', 'Usb-C with API support'),
    ('  "projectors"', '  "Projectors"'), ('écran USB', 'Écran USB'),
    ('27 inch monitor', '27 Inch monitor'), ('123.45', '123.45'),
    ('', ''), ('中文', '中文'),
])
def test_only_first_cased_character_changes(value, expected):
    assert tables.capital_start(value) == expected


@pytest.mark.parametrize('column,value', [
    ('Customer ID', 'store-Cust-0005'), ('order_id', 'order-a1'),
    ('Invoice number code', 'invoice-code'), ('Promo code', 'saveAbC'),
    ('Unit', 'mW'), ('Units', 'kg'), ('Currency', 'usd'),
    ('Email', 'alex@example.com'), ('URL', 'https://example.com/aB'),
    ('Reference', 'refCaseSensitive'), ('Hash', 'abcdef1234'),
    ('File name', 'fileCaseSensitive.pdf'), ('Description', 'https://example.com/aB'),
    ('Contact', 'alex@example.com'), ('Recorded value', 'mW'),
    ('Recorded value', 'abc123'), ('Recorded value', 'gpt-5.4'),
    ('Invoice path', '/home/customer/invoice.pdf'),
])
def test_identifiers_units_codes_and_addresses_retain_exact_case(column, value):
    assert tables.display_cell(column, value) == value


def test_display_copy_preserves_types_and_original_rows_and_column_keys():
    columns = ['category', 'Status', 'Unit', 'Count', 'Amount', 'Missing', 'Enough stock']
    source = [{'category': 'power banks', 'Status': 'pending', 'Unit': 'mW',
               'Count': 4, 'Amount': 12.5, 'Missing': None, 'Enough stock': True}]
    original = deepcopy(source)
    displayed = tables.display_rows(columns, source)
    assert source == original and list(displayed[0]) == columns
    assert displayed == [{'category': 'Power banks', 'Status': 'Pending', 'Unit': 'mW',
                          'Count': 4, 'Amount': 12.5, 'Missing': None, 'Enough stock': True}]


def test_unit_header_case_is_preserved_while_readable_headers_start_capitalized():
    assert tables.display_heading('mW') == 'mW'
    assert tables.display_heading('category') == 'Category'
    html = tables.full_table_html('Measurements', ['mW'], [{'mW': 3}])
    assert '<th scope="col">mW</th>' in html and '<th scope="col">MW</th>' not in html


class Content(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.tags = []

    def handle_data(self, data):
        self.text.append(data)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_full_scroll_view_keeps_long_multiline_and_last_rows_and_escapes_html():
    long_text = 'a' * 1700 + '\nlast line <script>alert("x")</script> & content'
    rows = [{'Description': long_text, 'Order ID': f'order-{i}'} for i in range(200)]
    html = tables.full_table_html('Orders "onfocus=alert(1)', ['Description', 'Order ID'], rows)
    parsed = Content()
    parsed.feed(html)
    assert long_text in ''.join(parsed.text)
    assert 'order-199' in parsed.text
    assert len([tag for tag, _ in parsed.tags if tag == 'tr']) == 201
    assert not any(tag in {'script', 'iframe'} for tag, _ in parsed.tags)
    assert '&lt;script&gt;' in html and 'white-space:pre-wrap' in html
    assert 'overflow:auto' in html and 'overflow-wrap:anywhere' in html
    assert 'text-overflow:ellipsis' not in html and 'overflow:hidden' not in html
    region = next(attrs for tag, attrs in parsed.tags if tag == 'div')
    assert region['tabindex'] == '0' and region['role'] == 'region'
    assert region['aria-label'] == 'Orders "onfocus=alert(1) full table content'


def test_render_passes_every_row_to_native_grid_and_full_text_view(monkeypatch):
    artifact = table_artifact('products', ['Item code', 'name', 'price', 'details'],
        [[f'item-{i}', 'usb-C device', 12.5, 'first line\n' + 'z' * 1000] for i in range(200)])
    original = deepcopy(artifact)
    recorded = {}

    class Expander:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def dataframe(frame, **kwargs):
        recorded.update(frame=frame, parameters=kwargs)

    monkeypatch.setattr(tables, 'st', SimpleNamespace(
        text=lambda text: recorded.update(title=text), caption=lambda text: None,
        dataframe=dataframe, html=lambda html: recorded.update(html=html),
        expander=lambda title: Expander(),
        column_config=SimpleNamespace(Column=lambda label, **kwargs: {'label': label, **kwargs})))
    tables.render_tables([artifact])
    assert artifact == original
    assert list(recorded['frame'].columns) == ['name', 'price', 'details']
    assert len(recorded['frame']) == 200
    assert recorded['frame'].iloc[-1]['name'] == 'Usb-C device'
    assert recorded['frame'].iloc[-1]['details'].endswith('z' * 1000)
    assert recorded['parameters']['height'] == 480
    assert recorded['parameters']['use_container_width'] is True
    assert recorded['parameters']['column_config']['name']['label'] == 'Name'
    assert recorded['parameters']['column_config']['details']['width'] == 'large'
    assert '<th scope="col">Item code</th>' not in recorded['html']
    assert 'item-199' not in recorded['html']
    assert 'z' * 1000 in recorded['html']


def test_streamlit_grid_and_full_content_view_render_without_modifying_artifact():
    source = table_artifact('store categories', ['category', 'product_count'],
                            [['power banks', 20], ['USB accessories', 18]])
    ui = AppTest.from_string('''
import streamlit as st
from ui.tables import render_tables
render_tables([st.session_state['source']])
''', default_timeout=30)
    ui.session_state['source'] = source
    ui.run()
    assert not ui.exception and len(ui.dataframe) == 1
    assert ui.dataframe[0].value.to_dict('records') == [
        {'category': 'Power banks', 'product_count': 20},
        {'category': 'USB accessories', 'product_count': 18}]
    assert any(expander.label == 'Read full table content' for expander in ui.expander)
    assert ui.session_state['source'] == source
