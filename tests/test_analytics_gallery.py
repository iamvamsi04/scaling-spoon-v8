"""Real report snapshots produce complete, useful visualizations without an LLM."""
from decimal import Decimal

import pytest

from database.connection import connect, transaction
from mcp_server.access import scoped_tool
from mcp_server.charts import CHART_TYPES, chart_from_result
from mcp_server.tools.analytics_tools import billing_analysis


@pytest.fixture
def gallery_orders():
    with transaction() as conn:
        conn.executemany('INSERT INTO product_details(item_code,category) VALUES (?,?)',
                         [('ITEM001', 'Accessories'), ('ITEM002', 'Accessories'), ('ITEM003', 'Electronics')])
        for index, amount in enumerate((60, 90, 249), 1):
            oid = f'GALLERY-{index}'
            day = f'2026-0{index}-02'
            conn.execute('INSERT INTO orders(order_id,order_date,customer_id,order_invoice,order_status,'
                         'subtotal,discount_applied,tax_amount,shipping_amount,charged_price) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (oid, day, 'CUST001', f'INV-{oid}', 'placed', amount, 5, 4, 2, amount + 1))
            conn.execute('INSERT INTO order_details(order_id,currency) VALUES (?,?)', (oid, ' usd '))
            conn.execute('INSERT INTO invoice_lines(order_id,item_code,qty_billed,unit_price_billed,line_total) '
                         'VALUES (?,?,?,?,?)', (oid, f'ITEM00{index}', index, str(Decimal(amount) / index), amount))
            conn.execute('INSERT INTO order_service(order_id,fulfillment_status,shipped_at,delivered_at) '
                         'VALUES (?,?,?,?)', (oid, 'delivered', f'2026-0{index}-03', f'2026-0{index}-05'))
        conn.execute("INSERT INTO refund_requests(order_id,amount,currency,reason,status) "
                     "VALUES ('GALLERY-1',10,'USD','Return requested','pending')")


def overview(mode):
    result = scoped_tool(billing_analysis)(execution_mode='admin', report='store_overview', chart_mode=mode)
    assert result['ok'], result
    return result


def test_all_gallery_covers_every_retained_chart_family(gallery_orders):
    report = overview('all')
    assert report['chart_mode'] == 'all'
    assert len(report['datasets']) == 16
    charts = [chart_from_result(result) for result in report['chart_results']]
    assert len(charts) > 6
    assert {chart['type'] for chart in charts} == set(CHART_TYPES)
    assert set(report['coverage']['requested_chart_types']) == set(CHART_TYPES)
    assert report['coverage']['chart_omissions'] == []
    for chart in charts:
        assert chart['x'] != 'currency' and chart.get('group') != 'currency'
        assert 'currency' not in chart['y']


def test_dashboard_is_varied_and_none_preserves_the_complete_same_snapshot(gallery_orders):
    dashboard, plain, none = overview('dashboard'), overview('overview'), overview('none')
    assert len(plain['chart_results']) == 2
    assert len(dashboard['chart_results']) > 6
    assert len({result['chart']['type'] for result in dashboard['chart_results']}) >= 10
    assert none['chart_results'] == [] and none['coverage']['requested_chart_types'] == []
    assert dashboard['datasets'] == none['datasets'] == plain['datasets']
    assert dashboard['summary'] == plain['summary'] == none['summary']


def test_compositions_and_combo_reconcile_once_at_invoice_grain(gallery_orders):
    report = overview('all')
    charts = [chart_from_result(row) for row in report['chart_results']]
    composition = next(chart for chart in charts if chart['type'] == 'normalized_bar')
    total = report['summary']['invoice_totals_by_currency'][0]
    assert sum((Decimal(str(row[measure])) for row in composition['data']
                for measure in composition['y']), Decimal(0)) == Decimal(total['charged_price'])
    combo = next(chart for chart in charts if chart['type'] == 'combo')
    assert sum(Decimal(str(row['charged_price'])) for row in combo['data']) == Decimal(total['charged_price'])
    assert sum(row['invoice_count'] for row in combo['data']) == 3
    timeline = next(chart for chart in charts if chart['type'] == 'timeline')
    assert len(timeline['data']) == 3
    assert all(row['order_date'][:10].endswith('-02') and row['delivered_at'][:10].endswith('-05')
               for row in timeline['data'])


def test_horizontal_inventory_view_is_distinct_and_keeps_complete_category_values(gallery_orders):
    report = overview('all')
    chart = next(chart_from_result(row) for row in report['chart_results']
                 if row['chart']['type'] == 'horizontal_bar')
    inventory = next(row for row in report['datasets'] if row['name'] == 'inventory_categories')
    expected = [dict(zip(inventory['columns'], row)) for row in inventory['rows']]
    assert chart['y'] == ['inventory_value_at_current_cost']
    assert {row['category']: Decimal(str(row['inventory_value_at_current_cost'])) for row in chart['data']} == {
        row['category']: Decimal(str(row['inventory_value_at_current_cost'])) for row in expected}
    assert len(chart['data']) == len(expected)


def test_more_than_six_currency_charts_are_separate_and_complete():
    codes = ('USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'INR', 'NZD')
    with transaction() as conn:
        for index, code in enumerate(codes):
            oid = f'CURRENCY-{index}'
            conn.execute('INSERT INTO orders(order_id,customer_id,order_invoice,subtotal,charged_price) '
                         'VALUES (?,?,?,?,?)', (oid, 'CUST001', f'INV-{oid}', 60, 60))
            conn.execute('INSERT INTO order_details(order_id,currency) VALUES (?,?)', (oid, code))
            conn.execute("INSERT INTO invoice_lines(order_id,item_code,qty_billed,unit_price_billed,line_total) "
                         "VALUES (?, 'ITEM001', 1, 60, 60)", (oid,))
    report = overview('overview')
    assert len(report['chart_results']) == 16
    assert {chart_from_result(row)['currency'] for row in report['chart_results']} == set(codes)
    trend = scoped_tool(billing_analysis)(execution_mode='admin', trend_period='month')
    assert trend['ok'] and len(trend['chart_results']) == 8
    assert sum(Decimal(row['rows'][0][1]) for row in trend['chart_results']) == Decimal(480)


def test_inapplicable_types_are_explained_and_financial_records_unchanged(gallery_orders):
    with transaction() as conn:
        conn.execute('UPDATE order_service SET delivered_at=NULL')
    with connect() as conn:
        before = [tuple(row) for row in conn.execute('SELECT * FROM orders ORDER BY order_id')]
    report = overview('all')
    assert not any(row['chart']['type'] == 'timeline' for row in report['chart_results'])
    assert any(row['type'] == 'timeline' and 'No matching' in row['reason'] for row in report['coverage']['chart_omissions'])
    with connect() as conn:
        assert before == [tuple(row) for row in conn.execute('SELECT * FROM orders ORDER BY order_id')]


@pytest.mark.parametrize('mode', ['random', '', None, 2])
def test_chart_mode_rejects_unknown_modes(mode):
    assert not billing_analysis(report='store_overview', chart_mode=mode)['ok']


def test_chart_mode_cannot_silently_change_a_different_report():
    result = billing_analysis(report='pending_refunds', chart_mode='all')
    assert not result['ok'] and 'only' in result['error']
