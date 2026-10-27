"""Complete invoice-grain store reports for the existing billing analysis tool."""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date
import re

from database.connection import connect
from mcp_server.tools.analytics_tools import LIMIT, ZERO, cash, dec, filters, summary


def dataset(name, title, grain, rows, columns, scope):
    return {'name': name, 'title': title, 'grain': grain, 'columns': columns,
            'rows': [[row.get(column) for column in columns] for row in rows],
            'row_count': len(rows), 'truncated': False, 'scope': scope}


def category(value):
    """Normalize only presentation category labels; keep identifiers untouched."""
    return re.sub(r'[\s_-]+', ' ', str(value or '').strip()).casefold() or 'Uncategorised'


def currency(value, fallback):
    code = (str(value or '').strip() or str(fallback or '').strip()).upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise ValueError('A report record has missing or invalid currency. Correct it before analyzing totals.')
    return code


def month(value):
    return date.fromisoformat(value).replace(day=1).isoformat()


def amount_rows(groups, dimension_names):
    rows = []
    for dimensions, orders in sorted(groups.items()):
        rows.append({**dict(zip(dimension_names, dimensions)), 'invoice_count': len(orders),
                     **summary(orders),
                     'average_invoice_total': cash(sum((dec(o['charged_price']) for o in orders), ZERO) / len(orders))})
    return rows


def checked_rows(cursor, label):
    rows = [dict(row) for row in cursor.fetchmany(LIMIT + 1)]
    if len(rows) > LIMIT:
        raise ValueError(f'More than {LIMIT} {label} match. Narrow the report scope for a complete report.')
    return rows


def store_overview(customer_id=None, order_id=None, date_from=None, date_to=None, chart_mode='overview'):
    """Read one snapshot; aggregate order and line grains independently."""
    from mcp_server.access import scope as execution_scope
    execution = execution_scope.get()
    if execution is not None and execution.get('mode') != 'admin':
        raise ValueError('Store-wide analytics is available only in admin mode.')
    # Reuse strict filter validation, but keep cancelled rows in operational datasets.
    # Sales below explicitly use placed invoices; cancellation requests must remain visible.
    filters(customer_id, order_id, date_from, date_to)
    clauses, args = [], []
    for field, value, operator in (('customer_id', customer_id, '='), ('order_id', order_id, '='),
                                   ('order_date', date_from, '>='), ('order_date', date_to, '<=')):
        if value is not None:
            clauses.append(f'o.{field}{operator}?')
            args.append(value)
    where = ' AND '.join(clauses) or '1=1'
    with closing(connect(readonly=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        config = dict(conn.execute('SELECT key,value FROM billing_config'))
        stock_currency = currency(config.get('currency'), 'USD')
        orders = checked_rows(conn.execute(
            'SELECT o.*,c.name AS customer_name,d.currency AS invoice_currency,'
            'd.status AS legacy_status,s.fulfillment_status,s.shipped_at,s.delivered_at FROM orders o '
            'LEFT JOIN customers c ON c.customer_id=o.customer_id '
            'LEFT JOIN order_details d ON d.order_id=o.order_id '
            'LEFT JOIN order_service s ON s.order_id=o.order_id '
            f'WHERE {where} ORDER BY o.order_date,o.order_id', args), 'orders')
        lines = checked_rows(conn.execute('SELECT l.* FROM invoice_lines l JOIN orders o '
                                          f'ON o.order_id=l.order_id WHERE {where} ORDER BY l.order_id,l.id', args), 'invoice lines')
        inventory = checked_rows(conn.execute('SELECT i.*,d.category FROM items i LEFT JOIN product_details d '
                                              'ON d.item_code=i.item_code ORDER BY i.item_code'), 'inventory products')
        requests = checked_rows(conn.execute('SELECT r.*,d.request_kind,d.disposition FROM refund_requests r '
                                             'JOIN orders o ON o.order_id=r.order_id LEFT JOIN refund_details d '
                                             f'ON d.order_id=r.order_id WHERE {where} ORDER BY r.requested_at,r.order_id', args), 'refund requests')
        credits = checked_rows(conn.execute('SELECT l.* FROM store_credit_ledger l JOIN orders o '
                                            f'ON o.order_id=l.order_id WHERE {where} ORDER BY l.created_at,l.id', args), 'store credit entries')

    by_id = {row['order_id']: row for row in orders}
    products = {row['item_code']: row for row in inventory}
    sales, cancelled, order_currencies = [], [], {}
    for row in orders:
        order_currencies[row['order_id']] = currency(row['invoice_currency'], stock_currency)
        legacy_cancelled = row['legacy_status'] == 'cancelled' or row['fulfillment_status'] == 'cancelled'
        if row['order_status'] == 'placed' and not legacy_cancelled:
            if not str(row['order_invoice'] or '').strip():
                raise ValueError('A placed order has no invoice number. Correct it before analyzing totals.')
            sales.append(row)
        elif (row['order_status'] == 'cancelled' or legacy_cancelled) and str(row['order_invoice'] or '').strip():
            cancelled.append(row)
    sales_ids = {row['order_id'] for row in sales}
    per_currency, cancelled_currency, per_month, per_customer = (defaultdict(list) for _ in range(4))
    for row in sales:
        code = order_currencies[row['order_id']]
        per_currency[(code,)].append(row)
        per_month[(code, month(row['order_date']))].append(row)
        per_customer[(code, row['customer_id'], row['customer_name'])].append(row)
    for row in cancelled:
        cancelled_currency[(order_currencies[row['order_id']],)].append(row)
    totals = amount_rows(per_currency, ['currency'])
    cancelled_totals = amount_rows(cancelled_currency, ['currency'])
    periods = amount_rows(per_month, ['currency', 'period'])
    customer_rows = amount_rows(per_customer, ['currency', 'customer_id', 'customer_name'])

    # Never join one-to-many lines into invoice totals; these metrics are explicitly line-grain.
    product_sales, category_sales, line_observations = {}, {}, []
    line_counts = defaultdict(lambda: {'line_count': 0, 'units_billed': 0, 'cost': ZERO})
    for line in lines:
        oid = line['order_id']
        if oid not in sales_ids:
            continue
        item = products.get(line['item_code'])
        if item is None:
            raise ValueError('A billed item is missing from inventory. Correct it before analyzing costs.')
        code, label = order_currencies[oid], category(item['category'])
        key = (code, line['item_code'])
        row = product_sales.setdefault(key, {'currency': code, 'item_code': line['item_code'], 'name': item['name'],
            'category': label, 'units_billed': 0, 'billed_revenue': ZERO, 'estimated_product_cost': ZERO, 'orders': set()})
        cost = dec(item['baseprice']) * line['qty_billed']
        line_observations.append({'order_id': oid, 'order_date': by_id[oid]['order_date'],
            'customer_id': by_id[oid]['customer_id'], 'currency': code, 'item_code': line['item_code'],
            'name': item['name'], 'category': label, 'units_billed': line['qty_billed'],
            'unit_price_billed': cash(line['unit_price_billed']), 'billed_revenue': cash(line['line_total']),
            'estimated_product_cost': cash(cost)})
        row['units_billed'] += line['qty_billed']
        row['billed_revenue'] += dec(line['line_total'])
        row['estimated_product_cost'] += cost
        row['orders'].add(oid)
        counts = line_counts[oid]
        counts['line_count'] += 1
        counts['units_billed'] += line['qty_billed']
        counts['cost'] += cost
        c = category_sales.setdefault((code, label), {'currency': code, 'category': label, 'units_billed': 0,
            'billed_revenue': ZERO, 'estimated_product_cost': ZERO, 'orders': set(), 'products': set()})
        c['units_billed'] += line['qty_billed']
        c['billed_revenue'] += dec(line['line_total'])
        c['estimated_product_cost'] += cost
        c['orders'].add(oid)
        c['products'].add(line['item_code'])
    def sales_rows(groups):
        rows = []
        for _, source in sorted(groups.items()):
            row = {k: v for k, v in source.items() if k not in ('orders', 'products')}
            row['invoice_count'] = len(source['orders'])
            if 'products' in source:
                row['product_count'] = len(source['products'])
            row['estimated_margin_before_order_adjustments'] = cash(source['billed_revenue'] - source['estimated_product_cost'])
            for field in ('billed_revenue', 'estimated_product_cost'):
                row[field] = cash(row[field])
            rows.append(row)
        return rows
    product_rows, category_rows = sales_rows(product_sales), sales_rows(category_sales)
    observations = []
    for row in sales:
        counts = line_counts[row['order_id']]
        observations.append({'order_id': row['order_id'], 'order_date': row['order_date'],
            'customer_id': row['customer_id'], 'currency': order_currencies[row['order_id']],
            'fulfillment_status': row['fulfillment_status'] or 'unknown',
            'line_count': counts['line_count'], 'units_billed': counts['units_billed'],
            **{field: cash(row[field]) for field in ('subtotal', 'discount_applied', 'tax_amount', 'shipping_amount', 'charged_price', 'refund_amount')},
            'net_billed_after_ledger_refunds': cash(dec(row['charged_price']) - dec(row['refund_amount'])),
            'estimated_product_cost': cash(counts['cost']) if counts['line_count'] else None})

    stock_rows, stock_categories = [], {}
    for item in inventory:
        label = category(item['category'])
        qty, cost, price = item['availablequantity'], dec(item['baseprice']), dec(item['sellingprice'])
        stock_rows.append({'item_code': item['item_code'], 'name': item['name'], 'category': label,
            'available_quantity': qty, 'current_unit_cost': cash(cost), 'selling_price': cash(price),
            'inventory_value_at_current_cost': cash(cost * qty), 'inventory_value_at_selling_price': cash(price * qty),
            'currency': stock_currency})
        row = stock_categories.setdefault(label, {'category': label, 'product_count': 0, 'in_stock_products': 0,
            'out_of_stock_products': 0, 'available_units': 0, 'inventory_value_at_current_cost': ZERO,
            'inventory_value_at_selling_price': ZERO, 'currency': stock_currency})
        row['product_count'] += 1
        row['in_stock_products' if qty > 0 else 'out_of_stock_products'] += 1
        row['available_units'] += qty
        row['inventory_value_at_current_cost'] += cost * qty
        row['inventory_value_at_selling_price'] += price * qty
    stock_category_rows = []
    for _, row in sorted(stock_categories.items()):
        stock_category_rows.append({**row, **{field: cash(row[field]) for field in ('inventory_value_at_current_cost', 'inventory_value_at_selling_price')}})

    refund_rows, refund_groups, approval_groups = [], {}, {}
    for source in requests:
        code = currency(source['currency'], None)
        row = {**source, 'currency': code, 'amount': cash(source['amount']),
               'order_status': by_id[source['order_id']]['order_status'],
               'customer_id': by_id[source['order_id']]['customer_id'],
               'request_kind': source['request_kind'] or 'unspecified', 'disposition': source['disposition'] or 'unspecified'}
        refund_rows.append(row)
        group = refund_groups.setdefault((code, source['status']), {'currency': code, 'request_status': source['status'],
            'request_count': 0, 'requested_amount': ZERO})
        group['request_count'] += 1
        group['requested_amount'] += dec(source['amount'])
        if source['status'] == 'approved':
            if not source['approved_at']:
                raise ValueError('An approved refund has no approval timestamp. Correct it before reporting.')
            period = month(source['approved_at'][:10])
            key = (code, period, source['approved_by_role'] or 'unknown')
            group = approval_groups.setdefault(key, {'currency': code, 'period': period,
                'approved_by_role': source['approved_by_role'] or 'unknown', 'approved_count': 0, 'approved_amount': ZERO})
            group['approved_count'] += 1
            group['approved_amount'] += dec(source['amount'])
    refund_summary = [{**row, 'requested_amount': cash(row['requested_amount'])} for _, row in sorted(refund_groups.items())]
    approvals = [{**row, 'approved_amount': cash(row['approved_amount'])} for _, row in sorted(approval_groups.items())]
    request_statuses = {row['order_id']: row['status'] for row in requests}
    recorded_refunds = [{'order_id': row['order_id'], 'order_date': row['order_date'], 'customer_id': row['customer_id'],
        'order_status': row['order_status'], 'currency': order_currencies[row['order_id']],
        'recorded_refund': cash(row['refund_amount']), 'refund_status': row['refund_status'],
        'request_status': request_statuses.get(row['order_id'])}
        for row in orders if dec(row['refund_amount']) > 0]
    credit_rows = [{**row, 'currency': currency(row['currency'], None), 'amount': cash(row['amount'])} for row in credits]

    order_scope = 'Inclusive order_date filters; selected customer/order if supplied. Active issued invoices only; cancelled orders excluded.'
    operation_scope = 'Inclusive order_date filters; selected customer/order if supplied. Includes cancelled orders; request and approval timestamps are separate dimensions.'
    stock_scope = 'Current full-store inventory snapshot. Order, customer and date filters do not restrict inventory.'
    amount_columns = ['invoice_count', 'subtotal', 'discount_applied', 'tax_amount', 'shipping_amount', 'charged_price',
                      'refund_amount', 'net_billed_after_ledger_refunds', 'average_invoice_total']
    sales_columns = ['currency', 'invoice_count', 'units_billed', 'billed_revenue', 'estimated_product_cost', 'estimated_margin_before_order_adjustments']
    statuses = Counter('cancelled' if row['legacy_status'] == 'cancelled' or row['fulfillment_status'] == 'cancelled' else row['order_status'] for row in orders)
    datasets = [
        dataset('invoice_totals', 'Invoice totals by currency', 'one row per currency', totals, ['currency', *amount_columns], order_scope),
        dataset('order_status', 'Order lifecycle', 'one row per lifecycle state', [{'order_status': key, 'order_count': statuses[key]} for key in ('draft', 'placed', 'cancelled')], ['order_status', 'order_count'], operation_scope),
        dataset('monthly_sales', 'Monthly invoice totals', 'one row per currency and calendar month', periods, ['currency', 'period', *amount_columns], order_scope),
        dataset('category_sales', 'Billed revenue by product category', 'one row per currency and product category', category_rows, ['category', 'product_count', *sales_columns], order_scope + ' Merchandise line revenue before order-level adjustments.'),
        dataset('product_sales', 'Billed revenue by product', 'one row per currency and item code', product_rows, ['item_code', 'name', 'category', *sales_columns], order_scope + ' Merchandise line revenue before order-level adjustments.'),
        dataset('customer_sales', 'Customer invoice totals', 'one row per currency and customer with matching invoices', customer_rows, ['currency', 'customer_id', 'customer_name', *amount_columns], order_scope),
        dataset('inventory', 'Current inventory', 'one row per product', stock_rows, ['item_code', 'name', 'category', 'currency', 'available_quantity', 'current_unit_cost', 'selling_price', 'inventory_value_at_current_cost', 'inventory_value_at_selling_price'], stock_scope),
        dataset('inventory_categories', 'Current inventory by category', 'one row per category', stock_category_rows, ['category', 'currency', 'product_count', 'in_stock_products', 'out_of_stock_products', 'available_units', 'inventory_value_at_current_cost', 'inventory_value_at_selling_price'], stock_scope),
        dataset('order_observations', 'Individual invoice observations', 'one unaggregated row per active issued invoice', observations, ['order_id', 'order_date', 'customer_id', 'currency', 'fulfillment_status', 'line_count', 'units_billed', 'subtotal', 'discount_applied', 'tax_amount', 'shipping_amount', 'charged_price', 'refund_amount', 'net_billed_after_ledger_refunds', 'estimated_product_cost'], order_scope + ' Complete observations for distributions, scatter plots and ranges; no top-N sampling.'),
        dataset('refund_requests', 'Refund requests and approvals', 'one row per refund request', refund_rows, ['order_id', 'customer_id', 'order_status', 'currency', 'amount', 'status', 'reason', 'requested_at', 'approved_at', 'approved_by_role', 'request_kind', 'disposition'], operation_scope),
        dataset('refund_status', 'Refund request amounts by status', 'one row per currency and request status', refund_summary, ['currency', 'request_status', 'request_count', 'requested_amount'], operation_scope),
        dataset('refund_approvals', 'Monthly local refund approvals', 'one row per currency, approval month and approving role', approvals, ['currency', 'period', 'approved_by_role', 'approved_count', 'approved_amount'], operation_scope),
        dataset('recorded_refunds', 'Recorded invoice refunds', 'one row per order with a recorded refund', recorded_refunds, ['order_id', 'order_date', 'customer_id', 'order_status', 'currency', 'recorded_refund', 'refund_status', 'request_status'], operation_scope + ' Kept separate from requests; do not add requested and recorded amounts.'),
        dataset('store_credit_activity', 'Local store credit activity', 'one row per local credit entry', credit_rows, ['id', 'customer_id', 'order_id', 'currency', 'amount', 'kind', 'created_at'], operation_scope + ' Activity linked to the selected orders, not account balances. Not subtracted from invoice face values.'),
        dataset('cancelled_invoice_totals', 'Cancelled issued invoice history', 'one row per currency', cancelled_totals, ['currency', *amount_columns], operation_scope + ' Historical face values excluded from active invoice totals; not current sales.'),
        dataset('invoice_line_observations', 'Individual billed merchandise observations', 'one unaggregated row per active invoice line', line_observations, ['order_id', 'order_date', 'customer_id', 'currency', 'item_code', 'name', 'category', 'units_billed', 'unit_price_billed', 'billed_revenue', 'estimated_product_cost'], order_scope + ' Merchandise grain for category/product/time combinations and distributions. Order-level amounts are intentionally not repeated on lines.'),
    ]
    charts, chart_omissions, requested_types = overview_charts(datasets, orders, chart_mode)
    return {'report': 'store_overview', 'chart_mode': chart_mode,
        'filters': {'customer_id': customer_id, 'order_id': order_id, 'date_from': date_from, 'date_to': date_to},
        'scope': {'sales': order_scope, 'refunds_and_credit': operation_scope, 'inventory': stock_scope},
        'summary': {'matched_orders': len(orders), 'invoice_count': len(sales), 'draft_count': statuses['draft'],
                    'cancelled_count': statuses['cancelled'], 'refund_request_count': len(requests),
                    'pending_refund_request_count': sum(row['status'] == 'pending' for row in requests),
                    'approved_refund_request_count': sum(row['status'] == 'approved' for row in requests),
                    'recorded_refund_order_count': len(recorded_refunds),
                    'active_customers': len({row['customer_id'] for row in sales}),
                    'inventory_products': len(inventory), 'inventory_categories': len(stock_categories),
                    'in_stock_products': sum(row['availablequantity'] > 0 for row in inventory),
                    'out_of_stock_products': sum(row['availablequantity'] == 0 for row in inventory),
                    'invoice_totals_by_currency': totals},
        'datasets': datasets, 'chart_results': charts,
        'coverage': {'complete': True, 'truncated': False, 'top_n_applied': False,
            'matched_order_date_from': orders[0]['order_date'] if orders else None,
            'matched_order_date_to': orders[-1]['order_date'] if orders else None,
            'dataset_row_counts': {row['name']: row['row_count'] for row in datasets},
            'requested_chart_types': requested_types,
            'generated_chart_types': sorted({chart['chart']['type'] for chart in charts}),
            'chart_omissions': chart_omissions},
        'notes': ['All matching records contribute; no implicit date filter or top-product/customer limit is applied.',
            'Order totals are stored charged_price. Net billed subtracts only recorded refund_amount, not pending requests or local credit redemptions.',
            'Currencies are normalized and reported separately. Never sum monetary values across currencies.',
            'Product/category revenue is billed merchandise line_total before order discounts, tax, shipping and refunds; do not label it net invoice revenue.',
            'Current inventory prices/costs are estimates in configured store currency, independent of historical invoice currencies.',
            'Requests include cancelled orders. Request, approval and recorded-refund datasets describe stages of the same refund and must not be added together.',
            'Date filters apply to order_date throughout this overview, including linked refund and credit activity. Use the dedicated refund report for approval-date filtering.',
            'No payment processing, settlement, outstanding-balance or cash-collected information is available.']}


def overview_charts(datasets, orders, mode):
    """Build chart-ready receipts from the same report snapshot, without model SQL.

    The dashboard chooses different questions rather than decorative variants. The
    all option additionally demonstrates every supported family whose required
    observations exist. Failed prerequisites remain explicit; data is never sampled
    or invented to make a chart appear.
    """
    from mcp_server.charts import CHART_TYPES, prepare_chart
    if mode not in ('overview', 'dashboard', 'all', 'none'):
        raise ValueError('chart_mode must be overview, dashboard, all or none.')
    if mode == 'none':
        return [], [], []
    tables = {table['name']: [dict(zip(table['columns'], row)) for row in table['rows']]
              for table in datasets}
    charts, omissions, requested = [], [], set()

    def omit(kind, reason, code=None):
        # One reason per type keeps metadata complete even across many currencies.
        entry = next((entry for entry in omissions if entry['type'] == kind and entry['reason'] == reason), None)
        if entry is None:
            entry = {'type': kind, 'reason': reason, 'currencies': []}
            omissions.append(entry)
        if code is not None and code not in entry['currencies']:
            entry['currencies'].append(code)

    def emit(kind, title, source, x, ys, **options):
        requested.add(kind)
        code = source[0].get('currency') if source else None
        if not source:
            omit(kind, 'No matching observations for this chart.')
            return
        fields = list(dict.fromkeys([x, *ys, *[options[key] for key in ('group', 'size', 'end') if key in options]]))
        if code is not None:
            fields.append('currency')
        data = [[row.get(field) for field in fields] for row in source]
        spec = {'type': kind, 'title': title + (f' ({code})' if code else ''), 'x': x, 'y': ys, **options}
        try:
            chart = prepare_chart(fields, data, spec)
        except ValueError as exc:
            omit(kind, str(exc), code)
            return
        charts.append({'columns': fields, 'rows': data, 'row_count': len(data), 'truncated': False,
                       'chart': {key: value for key, value in chart.items() if key != 'data'}})

    def selected(name, code):
        return [row for row in tables[name] if row.get('currency') == code]

    def shares(source, dimension, measure, maximum=12):
        """Keep exact totals while grouping the small tail; never top-N sampling."""
        source = sorted(source, key=lambda row: (-dec(row[measure]), str(row[dimension])))
        if len(source) <= maximum:
            return source
        tail = source[maximum - 1:]
        other = 'Other categories (combined)'
        while other in {row[dimension] for row in source}:
            other += ' combined'
        return [*source[:maximum - 1], {dimension: other,
            measure: cash(sum((dec(row[measure]) for row in tail), ZERO)), 'currency': tail[0]['currency']}]

    for code in sorted({row['currency'] for row in tables['invoice_totals']}):
        monthly, categories, observations, products = (selected(name, code) for name in
            ('monthly_sales', 'category_sales', 'order_observations', 'product_sales'))
        emit('line', 'Monthly order totals', monthly, 'period', ['charged_price'], x_type='temporal')
        emit('bar', 'Billed merchandise revenue by category', categories, 'category', ['billed_revenue'])
        if mode == 'overview':
            continue
        components = [{'period': row['period'], 'currency': code,
            'merchandise_after_discount': cash(dec(row['subtotal']) - dec(row['discount_applied'])),
            'tax': row['tax_amount'], 'shipping': row['shipping_amount']} for row in monthly]
        emit('stacked_bar', 'Order total components by month', components, 'period',
             ['merchandise_after_discount', 'tax', 'shipping'], x_type='temporal')
        share_rows = shares(categories, 'category', 'billed_revenue')
        emit('pie', 'Billed merchandise revenue share by category', share_rows, 'category', ['billed_revenue'])
        emit('histogram', 'Distribution of individual invoice totals', observations, 'charged_price', [], bins=16)
        emit('boxplot', 'Invoice totals by current fulfillment status', observations, 'fulfillment_status', ['charged_price'])
        emit('scatter', 'Billed units and invoice totals', observations, 'units_billed', ['charged_price'], group='fulfillment_status')
        cells = defaultdict(lambda: ZERO)
        for row in selected('invoice_line_observations', code):
            cells[(month(row['order_date']), row['category'])] += dec(row['billed_revenue'])
        heat = [{'period': period, 'category': category, 'billed_revenue': cash(value), 'currency': code}
                for (period, category), value in sorted(cells.items())]
        emit('heatmap', 'Monthly billed merchandise revenue by category', heat, 'period', ['billed_revenue'], group='category')
        amounts = defaultdict(list)
        for row in observations:
            amounts[month(row['order_date'])].append(dec(row['charged_price']))
        ranges = [{'period': period, 'minimum_invoice': cash(min(values)), 'maximum_invoice': cash(max(values)), 'currency': code}
                  for period, values in sorted(amounts.items())]
        emit('range', 'Minimum and maximum individual invoice totals by month', ranges, 'period',
             ['minimum_invoice', 'maximum_invoice'], x_type='temporal')
        if mode != 'all':
            continue
        emit('grouped_bar', 'Invoice subtotal and order total by month', monthly, 'period',
             ['subtotal', 'charged_price'], x_type='temporal')
        emit('normalized_bar', 'Percentage composition of order totals by month', components, 'period',
             ['merchandise_after_discount', 'tax', 'shipping'], x_type='temporal')
        emit('bubble', 'Product units and billed merchandise revenue; bubble size is invoice count', products,
             'units_billed', ['billed_revenue'], size='invoice_count', group='category')
        emit('combo', 'Monthly order totals and invoice counts', monthly, 'period',
             ['charged_price', 'invoice_count'], x_type='temporal')

    if mode in ('dashboard', 'all'):
        stocks = tables['inventory_categories']
        emit('grouped_bar', 'Current products in stock and out of stock by category', stocks, 'category',
             ['in_stock_products', 'out_of_stock_products'])
        if mode == 'all':
            emit('horizontal_bar', 'Current inventory value at current unit cost by category',
                 stocks, 'category', ['inventory_value_at_current_cost'])
        emit('bar', 'Current order lifecycle counts', tables['order_status'], 'order_status', ['order_count'])
        for code in sorted({row['currency'] for row in tables['refund_status']}):
            emit('bar', 'Requested refund amounts by current request status', selected('refund_status', code),
                 'request_status', ['requested_amount'])

    if mode == 'all':
        active = {row['order_id'] for row in tables['order_observations']}
        issued = [row for row in orders if row['order_id'] in active]
        completed = [{'order_id': row['order_id'], 'order_date': row['order_date'],
                      'delivered_at': row['delivered_at']} for row in issued
                     if row['fulfillment_status'] == 'delivered' and row['delivered_at']]
        emit('timeline', 'Recorded order-to-delivery intervals for delivered orders', completed,
             'order_date', [], end='delivered_at', group='order_id', x_type='temporal')
        requested.update(CHART_TYPES)
        generated = {chart['chart']['type'] for chart in charts}
        for kind in requested - generated - {entry['type'] for entry in omissions}:
            omit(kind, 'No matching observations with the measures required by this chart.')
    return charts, omissions, sorted(requested)
