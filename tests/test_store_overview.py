"""Store-wide results reconcile without multiplying invoices or mixing currencies."""
from collections import defaultdict
from decimal import Decimal

import pytest

from database.connection import connect, transaction
from mcp_server.access import scoped_tool
from mcp_server.charts import chart_from_result, prepare_chart
from mcp_server.tools.analytics_tools import billing_analysis


@pytest.fixture
def store_records():
    with transaction() as conn:
        conn.executemany('INSERT INTO product_details(item_code,category) VALUES (?,?)',
                         [('ITEM001', 'Power_Banks'), ('ITEM002', ' power banks ')])
        records = [
            ('USD1', '2026-01-10', 'CUST001', 'INV-USD1', 'placed', 100, 9, 5, 10, 104, 10, 'recorded', 'Return'),
            ('USD2', '2026-02-10', 'CUST002', 'INV-USD2', 'placed', 60, 6, 0, 0, 66, None, None, None),
            ('EUR1', '2026-02-15', 'CUST001', 'INV-EUR1', 'placed', 40, 3.6, 2, 4, 41.6, None, None, None),
            ('CANCEL', '2026-01-20', 'CUST003', 'INV-CANCEL', 'cancelled', 100, 0, 0, 0, 100, 100, 'recorded', 'Cancellation'),
            ('DRAFT', '2026-03-01', 'CUST001', None, 'draft', 30, 0, 0, 0, 30, None, None, None),
        ]
        conn.executemany('INSERT INTO orders(order_id,order_date,customer_id,order_invoice,order_status,subtotal,tax_amount,shipping_amount,discount_applied,charged_price,refund_amount,refund_status,refund_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', records)
        conn.executemany('INSERT INTO order_details(order_id,currency) VALUES (?,?)',
                         [('USD1', ' usd '), ('USD2', 'USD'), ('EUR1', 'eur'), ('CANCEL', 'USD'), ('DRAFT', 'USD')])
        conn.executemany('INSERT INTO invoice_lines(order_id,item_code,qty_billed,unit_price_billed,line_total) VALUES (?,?,?,?,?)',
            [('USD1', 'ITEM001', 1, 60, 60), ('USD1', 'ITEM002', 2, 20, 40),
             ('USD2', 'ITEM001', 1, 60, 60), ('EUR1', 'ITEM002', 2, 20, 40), ('CANCEL', 'ITEM003', 1, 100, 100)])
        conn.executemany("INSERT INTO refund_requests(order_id,amount,currency,reason,status,requested_at,approved_at,approved_by_role) VALUES (?,?,?,?,?,?,?,?)", [
            ('USD1', 10, 'USD', 'Return', 'approved', '2026-02-03 10:00:00', '2026-02-05 10:00:00', 'cashier'),
            ('CANCEL', 100, 'USD', 'Cancellation', 'approved', '2026-01-21 10:00:00', '2026-01-22 10:00:00', 'admin'),
            ('EUR1', 5, 'EUR', 'Review', 'pending', '2026-02-17 10:00:00', None, None)])
        conn.executemany('INSERT INTO refund_details(order_id,request_kind,disposition) VALUES (?,?,?)',
                         [('USD1', 'return', 'original_method'), ('CANCEL', 'cancellation', 'store_credit')])
        conn.executemany('INSERT INTO store_credit_ledger(customer_id,order_id,currency,amount,kind) VALUES (?,?,?,?,?)',
                         [('CUST003', 'CANCEL', 'USD', 100, 'refund_credit'), ('CUST002', 'USD2', 'USD', -5, 'order_redemption')])


def overview(**kwargs):
    data = scoped_tool(billing_analysis)(report='store_overview', execution_mode='admin', **kwargs)
    assert data['ok'], data
    return data


def rows(report, name):
    table = next(table for table in report['datasets'] if table['name'] == name)
    assert table['row_count'] == len(table['rows']) and not table['truncated']
    return [dict(zip(table['columns'], row)) for row in table['rows']]


def test_complete_report_separates_currencies_and_invoice_line_grains(store_records):
    report = overview(top_n=1)
    assert report['summary']['matched_orders'] == 5
    assert report['summary']['invoice_count'] == 3
    assert report['summary']['draft_count'] == 1
    assert report['summary']['cancelled_count'] == 1
    totals = {row['currency']: row for row in rows(report, 'invoice_totals')}
    assert totals['USD']['charged_price'] == '170.00'  # USD1 must not be counted twice for its two lines.
    assert totals['USD']['subtotal'] == '160.00'
    assert totals['USD']['refund_amount'] == '10.00'
    assert totals['USD']['net_billed_after_ledger_refunds'] == '160.00'
    assert totals['EUR']['charged_price'] == '41.60'
    assert totals['EUR']['refund_amount'] == '0.00'  # A pending request is not a recorded refund.
    assert rows(report, 'cancelled_invoice_totals')[0]['charged_price'] == '100.00'
    categories = rows(report, 'category_sales')
    assert {(row['category'], row['currency']) for row in categories} == {('power banks', 'USD'), ('power banks', 'EUR')}
    assert next(row for row in categories if row['currency'] == 'USD')['invoice_count'] == 2
    assert sum(Decimal(row['billed_revenue']) for row in rows(report, 'product_sales') if row['currency'] == 'USD') == Decimal('160.00')
    assert len(rows(report, 'product_sales')) == 3  # top_n does not narrow the report.
    assert len(rows(report, 'customer_sales')) == 3
    assert report['coverage']['complete'] and not report['coverage']['top_n_applied']


def test_refund_stages_and_credit_do_not_inflate_or_reduce_sales(store_records):
    report = overview()
    requests = rows(report, 'refund_requests')
    assert len(requests) == 3 and any(row['order_status'] == 'cancelled' for row in requests)
    assert {row['request_status']: row['requested_amount'] for row in rows(report, 'refund_status')} == {'approved': '110.00', 'pending': '5.00'}
    approvals = rows(report, 'refund_approvals')
    assert {(row['period'], row['approved_by_role'], row['approved_amount']) for row in approvals} == {
        ('2026-01-01', 'admin', '100.00'), ('2026-02-01', 'cashier', '10.00')}
    assert len(rows(report, 'recorded_refunds')) == 2
    assert {row['kind']: row['amount'] for row in rows(report, 'store_credit_activity')} == {
        'refund_credit': '100.00', 'order_redemption': '-5.00'}
    usd_total = next(row for row in rows(report, 'invoice_totals') if row['currency'] == 'USD')
    assert usd_total['charged_price'] == '170.00' and usd_total['net_billed_after_ledger_refunds'] == '160.00'


def test_complete_observations_make_distributions_and_scatter_without_sampling(store_records):
    report = overview()
    table = next(table for table in report['datasets'] if table['name'] == 'order_observations')
    selected = [row for row in table['rows'] if row[table['columns'].index('currency')] == 'USD']
    assert len(selected) == 2
    for spec in ({'type': 'histogram', 'x': 'charged_price', 'y': []},
                 {'type': 'boxplot', 'x': 'fulfillment_status', 'y': ['charged_price']},
                 {'type': 'scatter', 'x': 'subtotal', 'y': ['charged_price']}):
        assert prepare_chart(table['columns'], selected, spec)['type'] == spec['type']
    assert len(report['chart_results']) == 4
    assert all(chart_from_result(chart) for chart in report['chart_results'])
    merchandise = rows(report, 'invoice_line_observations')
    assert len(merchandise) == 4
    assert sum(Decimal(row['billed_revenue']) for row in merchandise if row['currency'] == 'USD') == Decimal('160.00')
    assert all('charged_price' not in row for row in merchandise)  # No repeated invoice totals to accidentally sum.


def test_order_date_filters_intersect_and_preserve_current_inventory(store_records):
    report = overview(customer_id='CUST001', date_from='2026-01-01', date_to='2026-01-31')
    assert report['summary']['matched_orders'] == report['summary']['invoice_count'] == 1
    assert [row['order_id'] for row in rows(report, 'order_observations')] == ['USD1']
    assert rows(report, 'monthly_sales')[0]['period'] == '2026-01-01'
    # Refund requested in February is retained because the report filters order dates, explicitly.
    assert [row['order_id'] for row in rows(report, 'refund_requests')] == ['USD1']
    assert len(rows(report, 'inventory')) == 3
    assert {row['category'] for row in rows(report, 'inventory')} == {'power banks', 'Uncategorised'}
    assert overview(order_id='EUR1', date_from='2026-01-01', date_to='2026-01-31')['summary']['matched_orders'] == 0


def test_overview_datasets_support_all_retained_chart_families(store_records):
    report = overview()
    tables = {table['name']: table for table in report['datasets']}
    def chart(name, spec):
        source = tables[name]
        currency_column = source['columns'].index('currency')
        selected = [row for row in source['rows'] if row[currency_column] == 'USD']
        return prepare_chart(source['columns'], selected, spec)
    rendered = []
    for kind in ('bar', 'stacked_bar', 'normalized_bar', 'pie'):
        ys = ['estimated_product_cost', 'estimated_margin_before_order_adjustments'] if kind in ('stacked_bar', 'normalized_bar') else ['billed_revenue']
        rendered.append(chart('category_sales', {'type': kind, 'x': 'category', 'y': ys}))
    rendered.append(chart('monthly_sales', {'type': 'line', 'x': 'period', 'y': ['charged_price'], 'x_type': 'temporal'}))
    rendered.append(chart('order_observations', {'type': 'scatter', 'x': 'subtotal', 'y': ['charged_price']}))
    rendered.append(chart('order_observations', {'type': 'histogram', 'x': 'charged_price', 'y': []}))
    rendered.append(chart('order_observations', {'type': 'boxplot', 'x': 'fulfillment_status', 'y': ['charged_price']}))
    cells = defaultdict(Decimal)
    for row in rows(report, 'invoice_line_observations'):
        if row['currency'] == 'USD':
            cells[(row['category'], row['order_date'][:7])] += Decimal(row['billed_revenue'])
    heat_rows = [[category, period, str(amount), 'USD'] for (category, period), amount in sorted(cells.items())]
    rendered.append(prepare_chart(['category', 'period', 'revenue', 'currency'], heat_rows,
                                  {'type': 'heatmap', 'x': 'period', 'group': 'category', 'y': ['revenue']}))
    month_values = defaultdict(list)
    for row in rows(report, 'order_observations'):
        if row['currency'] == 'USD':
            month_values[row['order_date'][:7]].append(Decimal(row['charged_price']))
    ranges = [[period, str(min(amounts)), str(max(amounts)), 'USD'] for period, amounts in sorted(month_values.items())]
    rendered.append(prepare_chart(['period', 'lower', 'upper', 'currency'], ranges,
                                  {'type': 'range', 'x': 'period', 'y': ['lower', 'upper'], 'x_type': 'temporal'}))
    assert {result['type'] for result in rendered} == {'bar', 'stacked_bar', 'normalized_bar', 'line', 'pie', 'scatter', 'histogram', 'boxplot', 'heatmap', 'range'}


def test_empty_report_remains_complete_and_tabular():
    report = overview()
    assert report['summary']['invoice_count'] == 0 and report['summary']['matched_orders'] == 0
    assert report['chart_results'] == []
    assert all(table['columns'] for table in report['datasets'])
    assert rows(report, 'invoice_totals') == rows(report, 'order_observations') == []
    assert rows(report, 'order_status') == [{'order_status': 'draft', 'order_count': 0},
                                          {'order_status': 'placed', 'order_count': 0},
                                          {'order_status': 'cancelled', 'order_count': 0}]
    assert len(rows(report, 'inventory')) == 3


@pytest.mark.parametrize('kwargs', [{'date_from': '2026-1-01'}, {'date_from': '2026-03-01', 'date_to': '2026-01-01'},
                                   {'customer_id': ''}, {'order_id': 'X' * 51}])
def test_overview_filters_reject_invalid_scope(kwargs):
    data = billing_analysis(report='store_overview', **kwargs)
    assert not data['ok']


def test_overview_is_admin_only_and_read_only(store_records):
    assert not scoped_tool(billing_analysis)(report='store_overview', execution_mode='customer')['ok']
    assert not scoped_tool(billing_analysis)(report='store_overview', execution_mode='cashier')['ok']
    with connect() as conn:
        before = list(conn.execute('SELECT * FROM orders ORDER BY order_id'))
        audit_count = conn.execute('SELECT COUNT(*) FROM billing_audit').fetchone()[0]
    overview()
    with connect() as conn:
        assert list(conn.execute('SELECT * FROM orders ORDER BY order_id')) == before
        assert conn.execute('SELECT COUNT(*) FROM billing_audit').fetchone()[0] == audit_count


def test_overview_fails_closed_instead_of_silently_truncating(store_records, monkeypatch):
    import mcp_server.reporting as reporting
    monkeypatch.setattr(reporting, 'LIMIT', 2)
    report = billing_analysis(report='store_overview')
    assert not report['ok'] and 'complete report' in report['error']


def test_currency_blanks_fall_back_but_invalid_currency_fails(store_records):
    with transaction() as conn:
        conn.execute("UPDATE order_details SET currency='  ' WHERE order_id='USD2'")
    assert next(row for row in rows(overview(), 'invoice_totals') if row['currency'] == 'USD')['charged_price'] == '170.00'
    with transaction() as conn:
        conn.execute("UPDATE order_details SET currency='US-D' WHERE order_id='USD2'")
    assert not billing_analysis(report='store_overview')['ok']


def test_sold_products_without_details_are_not_lost():
    with transaction() as conn:
        conn.execute("INSERT INTO orders(order_id,customer_id,order_invoice,subtotal,charged_price) VALUES ('NO-CATEGORY','CUST001','INV-NO-CATEGORY',249,249)")
        conn.execute("INSERT INTO invoice_lines(order_id,item_code,qty_billed,unit_price_billed,line_total) VALUES ('NO-CATEGORY','ITEM003',1,249,249)")
    report = overview()
    assert rows(report, 'category_sales')[0]['category'] == 'Uncategorised'
    assert rows(report, 'category_sales')[0]['billed_revenue'] == '249.00'
    assert rows(report, 'product_sales')[0]['item_code'] == 'ITEM003'
