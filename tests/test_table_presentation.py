"""Native tables retain verified facts and model-selected presentation."""
from copy import deepcopy
import json

import pytest

from agent.table_presentation import tool_tables
from context.tables import sanitize_tables
from database.connection import transaction
from mcp_server.access import scope
from mcp_server.tools.analytics_tools import billing_analysis, billing_anomalies
from mcp_server.tools.billing_tools import place_order, process_refund, request_order
from mcp_server.tools.shopping_tools import compare_products, find_cheaper_alternatives


def cells(tables):
    return [value for table in tables for row in table['rows'] for value in row.values()]


def text(tables):
    return ' '.join(str(value) for value in cells(tables))


def valid(tables):
    assert tables
    assert sanitize_tables(tables) == tables
    assert all(isinstance(row, dict) for table in tables for row in table['rows'])
    return tables


@pytest.fixture
def order():
    draft = request_order('CUST001', [{'item_code': 'ITEM001', 'quantity': 2}], 'TABLE-ORDER')
    assert draft['ok'], draft
    placed = place_order('TABLE-ORDER', 'CUST001')
    assert placed['ok'], placed
    return placed


def test_actual_analysis_summary_and_ranked_lists_are_native_tables(order):
    data = billing_analysis(customer_id='CUST001')
    assert data['ok'], data
    tables = valid(tool_tables('billing_analysis', data))
    shown = cells(tables)
    assert data['totals']['charged_price'] in shown
    assert data['totals']['net_billed_after_ledger_refunds'] in shown
    assert data['top_products'][0]['item_code'] in shown
    assert data['top_products'][0]['line_revenue'] in shown
    assert data['top_customers'][0]['customer_id'] in shown


def test_actual_invoice_trend_exposes_the_exact_period_and_amount(order):
    data = billing_analysis(trend_period='month', trend_metric='order_total')
    assert data['ok'], data
    tables = valid(tool_tables('billing_analysis', data))
    shown = cells(tables)
    for source in data['chart_results']:
        for row in source['rows']:
            assert all(value in shown for value in row)


def test_pending_refund_table_preserves_request_amount_and_approval_role(order):
    token = scope.set({'mode': 'customer', 'customer_id': 'CUST001'})
    try:
        result = process_refund('TABLE-ORDER', 'CUST001', reason='Item no longer needed', action='request')
    finally:
        scope.reset(token)
    assert result['ok'], result
    data = billing_analysis(report='pending_refunds')
    assert data['ok'] and data['count'] == 1, data
    tables = valid(tool_tables('billing_analysis', data))
    shown = cells(tables)
    assert 'TABLE-ORDER' in shown
    assert data['requests'][0]['amount'] in shown
    assert data['requests'][0]['required_approver'] in shown
    assert 'pending' in shown


@pytest.mark.parametrize('report,key', [
    ('pending_refunds', 'requests'),
    ('pending_above_threshold', 'requests'),
    ('refund_reconciliation', 'mismatches'),
])
def test_empty_refund_report_is_an_explicit_empty_table(report, key):
    data = billing_analysis(report=report)
    assert data['ok'] and data[key] == [], data
    tables = valid(tool_tables('billing_analysis', data))
    assert any(table['row_count'] == 0 and table['total_rows'] == 0 for table in tables)


def test_reconciliation_separates_undated_gaps_from_dated_mismatches():
    data = {'ok': True, 'report': 'refund_reconciliation', 'approved_count': 3,
            'mismatches': [{'order_id': 'CURRENT-GAP', 'issue': 'Approved request and invoice refund differ'}],
            'count': 1, 'undated_records': [{'order_id': 'UNDATED-GAP', 'issue': 'Legacy refund has no request/approval history'}],
            'undated_count': 1,
            'scope': 'Local approved refunds on approval dates; no bank settlements or transfers verified.'}
    tables = valid(tool_tables('billing_analysis', data))
    assert any('CURRENT-GAP' in row.values() for table in tables for row in table['rows'])
    assert any('UNDATED-GAP' in row.values() for table in tables for row in table['rows'])
    assert not any({'CURRENT-GAP', 'UNDATED-GAP'} <= set(cells([table])) for table in tables)


def test_actual_anomaly_facts_are_scalar_english_cells_not_tool_json(order):
    with transaction() as conn:
        conn.execute("UPDATE orders SET charged_price=charged_price+1 WHERE order_id='TABLE-ORDER'")
    data = billing_anomalies(order_id='TABLE-ORDER')
    assert data['ok'] and data['findings'], data
    tables = valid(tool_tables('billing_anomalies', data))
    assert 'TABLE-ORDER' in cells(tables)
    mismatch = next(finding for finding in data['findings'] if finding['rule'] == 'TOTAL_MISMATCH')
    assert mismatch['evidence']['expected'] in text(tables)
    assert mismatch['evidence']['stored'] in text(tables)
    assert all(not isinstance(value, (dict, list)) for value in cells(tables))
    assert not any(str(value).startswith(('{', '[')) for value in cells(tables))


def test_anomaly_pagination_is_disclosed_without_claiming_full_findings():
    data = {'ok': True, 'currency': 'USD', 'finding_count': 10,
            'findings': [{'order_id': 'ONE', 'rule': 'TOTAL_MISMATCH', 'severity': 'error',
                          'evidence': {'expected': '0.10', 'stored': '0.30'}}],
            'findings_truncated': True, 'counts_by_rule': {'TOTAL_MISMATCH': 10},
            'optimization_suggestions': []}
    tables = valid(tool_tables('billing_anomalies', data))
    finding_table = next(table for table in tables if 'ONE' in cells([table]))
    assert finding_table['truncated'] or any(word in finding_table.get('coverage', '').lower()
                                          for word in ('limited', 'preview', 'showing', 'top'))
    assert '0.10' in text(tables) and '0.30' in text(tables)


@pytest.fixture
def feature_products():
    with transaction() as conn:
        conn.executemany('INSERT INTO product_details(item_code,category,features_json) VALUES (?,?,?)', [
            ('ITEM001', 'Devices', json.dumps({'storage': 64, 'connection': 'USB-C'})),
            ('ITEM002', 'Devices', json.dumps({'storage': 0.064})),
            ('ITEM003', 'Devices', json.dumps({'storage': 128})),
        ])
        conn.executemany('INSERT INTO product_attributes(item_code,attribute,number_value,unit) VALUES (?,?,?,?)', [
            ('ITEM001', 'storage', 64, 'GB'), ('ITEM002', 'storage', 0.064, 'TB')])


def test_comparison_tables_show_recorded_features_units_and_missing_values(feature_products):
    data = compare_products(['ITEM001', 'ITEM002'], budget='60.00', required_features={'connection': 'USB-C'})
    assert data['ok'], data
    tables = valid(tool_tables('compare_products', data))
    shown = cells(tables)
    assert {'ITEM001', 'ITEM002', 'GB', 'TB', 'USB-C'} <= set(shown)
    assert 'Not recorded' in shown
    assert data['products'][0]['unit_price'] in shown
    assert data['products'][1]['unit_price'] in shown
    assert '64' in {str(value) for value in shown} or '64.0' in {str(value) for value in shown}
    assert '0.064' in {str(value) for value in shown}
    # Units remain distinct; no unit conversion or overall-quality verdict is invented.
    storage = [row for table in tables for row in table['rows']
               if row.get('Item code') == 'ITEM001' and row.get('Feature') == 'storage']
    assert len(storage) == 1 and storage[0]['Unit'] == 'GB'


def test_conflicting_product_feature_remains_explicitly_uncertain(feature_products):
    data = compare_products(['ITEM001', 'ITEM002'])
    assert data['ok'], data
    data['products'][0]['ambiguous_features'] = ['storage']
    tables = valid(tool_tables('compare_products', data))
    assert 'Conflicting records' in cells(tables)


def test_alternative_source_is_distinguished_from_paginated_choices(feature_products):
    data = find_cheaper_alternatives('ITEM003', limit=1)
    assert data['ok'] and data['matched_count'] == 2 and data['truncated'], data
    tables = valid(tool_tables('find_cheaper_alternatives', data))
    source_table = next(table for table in tables if table['title'] == 'Reference product')
    assert 'ITEM003' in cells([source_table])
    choices = [table for table in tables if table['title'] != 'Reference product'
               and any(value == data['alternatives'][0]['item_code'] for value in cells([table]))]
    assert choices
    assert any('1' in table.get('coverage', '') and '2' in table.get('coverage', '') for table in choices)


def test_no_cheaper_alternatives_has_reference_and_empty_choices(feature_products):
    data = find_cheaper_alternatives('ITEM002')
    assert data['ok'] and data['matched_count'] == 0 and data['alternatives'] == [], data
    tables = valid(tool_tables('find_cheaper_alternatives', data))
    assert any(table['title'] == 'Reference product' and 'ITEM002' in cells([table]) for table in tables)
    assert any(table['row_count'] == 0 for table in tables if table['title'] != 'Reference product')


def test_precision_and_original_tool_payload_are_preserved():
    data = {'ok': True, 'currency': 'USD', 'matched_orders': 1, 'invoice_count': 1,
            'draft_count': 0, 'refunded_invoice_count': 0,
            'totals': {'subtotal': '999999999999.01', 'discount_applied': '0.10',
                       'tax_amount': '0.03', 'shipping_amount': '0.00',
                       'charged_price': '999999999998.94', 'refund_amount': '0.00',
                       'net_billed_after_ledger_refunds': '999999999998.94'},
            'top_customers': [], 'top_products': [],
            'customers_truncated': False, 'products_truncated': False}
    before = deepcopy(data)
    tables = valid(tool_tables('billing_analysis', data))
    assert data == before
    assert '999999999999.01' in cells(tables)
    assert '0.10' in cells(tables)
    assert '0.03' in cells(tables)


@pytest.mark.parametrize('name', ['billing_analysis', 'billing_anomalies', 'compare_products', 'find_cheaper_alternatives'])
def test_failed_tools_do_not_publish_success_tables(name):
    assert tool_tables(name, {'ok': False, 'error': 'Denied',
                             'products': [{'name': 'STALE-PRIVATE'}]}) == []


def test_unknown_tools_and_unrelated_private_payloads_are_not_flattened(order):
    assert tool_tables('unrecognized_internal_tool', {'ok': True, 'credentials': [{'password_hash': 'PRIVATE-HASH'}]}) == []
    data = billing_analysis()
    data['credentials'] = [{'password_hash': 'PRIVATE-HASH', 'session_token': 'PRIVATE-TOKEN'}]
    data['unrelated_private_payload'] = {'secret': 'PRIVATE-SECRET'}
    tables = valid(tool_tables('billing_analysis', data))
    assert not any(marker in text(tables) for marker in ('PRIVATE-HASH', 'PRIVATE-TOKEN', 'PRIVATE-SECRET'))


@pytest.mark.parametrize('columns,rows,message', [
    (['name'], [['Mechanical keyboard'], ['Wireless mouse']], 'List product names'),
    (['name', 'sellingprice'], [['Mechanical keyboard', '79.90']], 'Show products'),
])
def test_sql_lists_are_attached(columns, rows, message):
    from agent.billing_agent import RecordingWorkbench
    workbench = RecordingWorkbench()
    workbench.user_message = message
    workbench.record_result('execute_sql_query', {'query': 'SELECT name FROM items'},
                            {'ok': True, 'columns': columns, 'rows': rows, 'truncated': False})
    assert len(workbench.tables) == 1
    assert workbench.tables[0]['columns'] == columns
    assert len(workbench.tables[0]['rows']) == len(rows)


def test_explicit_scalar_table_uses_the_final_presentation_without_a_word_router(monkeypatch):
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from api.main import app
    from test_end_to_end import setup_engine, call, reply

    setup_engine(monkeypatch, [call('execute_sql_query', query='SELECT COUNT(*) AS products FROM items'),
        reply('Product count\n| Products |\n|---|\n| 3 |')])
    with TestClient(app) as client:
        cid = client.post('/conversations', json={}).json()['conversation_id']
        response = client.post('/chat', json={'conversation_id':cid,'request_id':str(uuid4()),
                                              'message':'Show the number of products in a table'})
        assert response.status_code == 200, response.text
        tables = response.json()['tables']
        assert len(tables) == 1 and tables[0]['row_count'] == 1
        assert str(tables[0]['rows'][0]['Products']) == '3'
        assert len(app.state.engine._client.create_calls) == 2


def test_single_scalar_answer_does_not_add_an_unnecessary_table():
    from agent.billing_agent import RecordingWorkbench
    workbench = RecordingWorkbench()
    workbench.user_message = 'How many products are there?'
    workbench.record_result('execute_sql_query', {'query': 'SELECT COUNT(*) FROM items'},
                            {'ok': True, 'columns': ['count'], 'rows': [[3]], 'truncated': False})
    assert workbench.tables == []


def test_explicit_chart_and_table_retains_both_presentations():
    from agent.billing_agent import RecordingWorkbench
    workbench = RecordingWorkbench()
    workbench.mode = 'admin'
    workbench.user_message = 'Show revenue by month as a chart and a table'
    workbench.record_result('execute_sql_query', {'query': 'SELECT month,revenue FROM orders'},
                            {'ok': True, 'columns': ['month', 'revenue'],
                             'rows': [['2026-10', '123.45']], 'truncated': False,
                             'chart': {'type': 'bar', 'x': 'month', 'y': ['revenue']}})
    assert len(workbench.tables) == 1
    assert workbench.tables[0]['rows'][0] == {'month': '2026-10', 'revenue': '123.45'}


def test_inventory_table_followup_retrieves_fresh_counts_with_the_same_presentation(monkeypatch):
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from api.main import app
    from agent.response_contract import INVENTORY_QUERY
    from test_end_to_end import setup_engine, call

    with transaction() as conn:
        conn.executemany('INSERT INTO product_details(item_code,category) VALUES (?,?)',
                         [('ITEM001', 'Keyboards'), ('ITEM002', 'Mice'), ('ITEM003', 'Monitors')])
    setup_engine(monkeypatch, [call('execute_sql_query', query=INVENTORY_QUERY),
                              'There are 3 products across 3 categories. 3 products are currently in stock.',
                              call('execute_sql_query', query=INVENTORY_QUERY),
                              'There are 3 products across 3 categories. 2 products are currently in stock.'])
    with TestClient(app) as client:
        cid = client.post('/conversations', json={}).json()['conversation_id']

        def chat(message):
            response = client.post('/chat', json={'conversation_id': cid, 'request_id': str(uuid4()), 'message': message})
            assert response.status_code == 200, response.text
            return response.json()

        first = chat('What products do you have?')
        with transaction() as conn:
            conn.execute("UPDATE items SET availablequantity=0 WHERE item_code='ITEM001'")
        second = chat('Put that in a table')
        assert len(first['tables']) == len(second['tables']) == 1
        assert first['tables'][0]['columns'] == second['tables'][0]['columns']
        assert first['tables'][0]['rows'][0]['In stock products'] == 1
        assert second['tables'][0]['rows'][0] == {'Category': 'Keyboards', 'Products': 1, 'In stock products': 0}
        assert '3 products across 3 categories' in second['response']
        assert '2 products are currently in stock' in second['response']
        assert len(app.state.engine._client.create_calls) == 4
        saved = client.get('/conversations/' + cid).json()['messages'][-1]
        assert saved['tables'] == second['tables']


def test_admin_table_followup_preserves_report_scope_and_uses_fresh_data_without_charts(monkeypatch, order):
    from decimal import Decimal
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from api.main import app
    from config import settings
    from test_end_to_end import setup_engine, call

    selected = {'customer_id': 'CUST001', 'date_from': '2026-01-01', 'date_to': '2099-12-31'}
    setup_engine(monkeypatch, [call('billing_analysis', report='store_overview', **selected),
                              'The selected customer report is shown.',
                              call('billing_analysis', report='store_overview', chart_mode='none', **selected),
                              'The same scope is shown as tables with fresh totals.'])
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'native-table-followup-admin-12345')
    headers = {'X-Admin-Key': settings.ADMIN_API_KEY}
    with TestClient(app) as client:
        cid = client.post('/conversations', json={'mode': 'admin'}, headers=headers).json()['conversation_id']

        def chat(message):
            response = client.post('/chat', json={'conversation_id': cid, 'request_id': str(uuid4()), 'message': message}, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()

        first = chat('Show store analytics for customer CUST001 from 2026-01-01 to 2099-12-31')
        assert first['analytics_context']['filters'] == {**selected, 'order_id': None}
        first_total = next(table for table in first['tables'] if table['title'] == 'Invoice totals by currency')['rows'][0]['charged_price']
        with transaction() as conn:
            conn.execute("UPDATE orders SET charged_price=charged_price+1,tax_amount=tax_amount+1 WHERE order_id='TABLE-ORDER'")
        second = chat('Put that in a table')
        assert second['analytics_context']['filters'] == first['analytics_context']['filters']
        assert second['analytics_context']['chart_mode'] == 'none'
        assert second['charts'] == [] and len(second['tables']) == 12
        total = next(table for table in second['tables'] if table['title'] == 'Invoice totals by currency')['rows'][0]['charged_price']
        assert Decimal(total) == Decimal(first_total) + 1
        assert len(app.state.engine._client.create_calls) == 4
        saved = client.get('/conversations/' + cid, headers=headers).json()
        assert saved['messages'][-1]['tables'] == second['tables']
        assert saved['analytics_context'] == second['analytics_context']


def test_customer_api_single_column_table_contains_only_authenticated_customer_orders(monkeypatch, order):
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from api.main import app
    from api.customer_auth import set_password, login
    from test_end_to_end import setup_engine, call, reply

    assert request_order('CUST002', [{'item_code': 'ITEM002', 'quantity': 1}], 'OTHER-CUSTOMER-ORDER')['ok']
    setup_engine(monkeypatch, [call('execute_sql_query', query='SELECT order_id FROM orders ORDER BY order_id'),
                              reply('Your order IDs are shown in the table.\n\n| order_id |\n|---|\n| TABLE-ORDER |')])
    set_password('CUST001', 'Native-table-test-pass-1234')
    headers = {'X-Customer-Token': login('CUST001', 'Native-table-test-pass-1234')['token']}
    with TestClient(app) as client:
        cid = client.post('/conversations', json={'customer_id': 'CUST001'}, headers=headers).json()['conversation_id']
        response = client.post('/chat', json={'conversation_id': cid, 'request_id': str(uuid4()),
                                             'message': 'List my order IDs in a table'}, headers=headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data['tables']) == 1
        assert data['tables'][0]['columns'] == ['order_id']
        assert data['tables'][0]['rows'] == [{'order_id': 'TABLE-ORDER'}]
        assert 'OTHER-CUSTOMER-ORDER' not in str(data)
        assert len(app.state.engine._client.create_calls) == 2
        assert 'shopping_update' not in data['response']
        saved = client.get('/conversations/' + cid, headers=headers).json()['messages'][-1]
        assert saved['tables'] == data['tables']


@pytest.mark.parametrize('name,arguments,data,expected_table_error', [
    ('process_refund', {'order_id': 'APPROVED-RETURN', 'action': 'approve'},
     {'ok': True, 'status': 'approved', 'refund_amount': '15.00', 'items': [None]}, True),
    ('compare_products', {'item_codes': ['ITEM001', 'ITEM002']},
     {'ok': True, 'currency': 'USD', 'products': [None]}, True),
    ('billing_analysis', {'trend_period': 'month'},
     {'ok': True, 'report': 'invoice_trend',
      'chart_results': [{'chart': {'title': 'Monthly revenue'}, 'rows': [['2026-10', '15.00']]}]}, False),
    ('process_refund', {'order_id': 'APPROVED-RETURN', 'action': 'approve'},
     {'ok': True, 'status': 'approved', 'refund_amount': '15.00',
      'items': [{'item_code': 'X' * 2001, 'quantity': 1}]}, True),
])
def test_failed_table_presentation_preserves_the_successful_tool_receipt(name, arguments, data, expected_table_error):
    """A display failure cannot turn an approved mutation into a failed operation."""
    from agent.billing_agent import RecordingWorkbench

    workbench = RecordingWorkbench()
    original = deepcopy(data)
    workbench.record_result(name, arguments, data)

    assert workbench.receipts == [(name, arguments, original)]
    assert workbench.receipts[0][2]['ok'] is True
    assert workbench.tables == []
    # Chart-only results do not attempt a duplicate table presentation.
    assert bool(workbench.table_errors) is expected_table_error
    assert data == original
    # Recording a completed operation performs no further tool invocation.
    assert workbench.tool_calls == 0
