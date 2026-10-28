"""Validate and present executed report results without classifying user wording.

The model chooses the relevant tools and presentation from the conversation.
These helpers operate only on structured tool arguments and verified results;
they never turn a word in the user's message into a tool call or dashboard.
"""
from dataclasses import dataclass
from datetime import date
import re


READ_TOOLS = frozenset({'execute_sql_query', 'billing_analysis', 'billing_anomalies',
                        'compare_products', 'find_cheaper_alternatives'})
FILTER_KEYS = ('customer_id', 'order_id', 'date_from', 'date_to')
CATEGORY_EXPRESSION = "COALESCE(NULLIF(TRIM(d.category),''),'Uncategorised')"
INVENTORY_QUERY = (f'SELECT {CATEGORY_EXPRESSION} AS category, COUNT(*) AS products, '
    'SUM(CASE WHEN i.availablequantity>0 THEN 1 ELSE 0 END) AS in_stock_products '
    'FROM items i LEFT JOIN product_details d ON d.item_code=i.item_code '
    f'GROUP BY {CATEGORY_EXPRESSION} ORDER BY category')


@dataclass(frozen=True)
class ResponseContract:
    """The shape of an executed result, not an interpretation of a question."""
    kind: str = 'conversation'
    scoped: bool = False
    domains: frozenset[str] = frozenset()
    filter_items: tuple[tuple[str, str], ...] = ()
    chart_mode: str = 'overview'

    @property
    def needs_evidence(self):
        return self.kind != 'conversation'

    @property
    def report(self):
        return self.kind in ('inventory_overview', 'store_overview')

    @property
    def tool(self):
        return {'inventory_overview': 'execute_sql_query', 'store_overview': 'billing_analysis'}.get(self.kind)

    @property
    def expected_filters(self):
        return {key: dict(self.filter_items).get(key) for key in FILTER_KEYS}

    def prompt(self):
        # Retained for callers using older policy builders. An observed report
        # must never become instructions to execute a different request.
        return ''


def request_contract(*args, **kwargs):
    """Compatibility only: user wording no longer chooses execution or scope."""
    return ResponseContract()


def chart_intent(*args, **kwargs):
    """Compatibility only; chart metadata is selected in structured tool calls."""
    return None


def table_intent(*args, **kwargs):
    """Compatibility only; presentation is no longer inferred by matching words."""
    return False


def general_explanation(*args, **kwargs):
    """Compatibility only; the model interprets the conversation's meaning."""
    return True


def result_contract(name, arguments, data):
    """Recognize complete report shapes from the exact read that actually ran.

    This can be used to format trustworthy report summaries locally. It grants
    no authority to dispatch tools, expand scope, force chart modes, or replace
    a model's interpretation of the user's current request.
    """
    if not isinstance(arguments, dict) or not isinstance(data, dict) or data.get('ok') is not True:
        return ResponseContract()
    if name == 'execute_sql_query' and inventory_rows(arguments, data) is not None:
        return ResponseContract('inventory_overview', domains=frozenset({'products'}))
    if name != 'billing_analysis' or data.get('report') != 'store_overview':
        return ResponseContract()
    if arguments.get('report') != 'store_overview':
        return ResponseContract()
    coverage = data.get('coverage')
    filters = data.get('filters')
    mode = data.get('chart_mode')
    summary = data.get('summary')
    if (not isinstance(coverage, dict) or coverage.get('complete') is not True
            or not isinstance(filters, dict) or set(filters) != set(FILTER_KEYS)
            or mode not in ('overview', 'dashboard', 'all', 'none')
            or mode != arguments.get('chart_mode', 'overview')
            or not isinstance(summary, dict)):
        return ResponseContract()
    for key in ('matched_orders','invoice_count','draft_count','cancelled_count'):
        if type(summary.get(key)) is not int or summary[key] < 0:
            return ResponseContract()
    for key, value in filters.items():
        if value != arguments.get(key):
            return ResponseContract()
        if value is None:
            continue
        if not isinstance(value, str):
            return ResponseContract()
        if key.startswith('date_'):
            try:
                if date.fromisoformat(value).isoformat() != value:
                    return ResponseContract()
            except ValueError:
                return ResponseContract()
        elif not re.fullmatch(r'[A-Za-z0-9_-]{1,50}', value):
            return ResponseContract()
    if (filters['date_from'] or '') > (filters['date_to'] or '9999-12-31'):
        return ResponseContract()
    values = tuple((key, filters[key]) for key in FILTER_KEYS if filters[key] is not None)
    return ResponseContract('store_overview', bool(values), frozenset({'finance'}), values, mode)


def sql_text(query):
    """Remove data literals/comments before reading referenced SQL table names."""
    return re.sub(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'", ' ', str(query)).casefold()


def _inventory_recipe(query, columns):
    # Prove a small family of equivalent one-row-per-item queries. Arbitrary
    # joins or CTEs cannot establish complete inventory from aliases alone.
    tokens = re.findall(r"'(?:''|[^'])*'|--[^\n]*|/\*[\s\S]*?\*/|[^']+?", str(query))
    clean = ''.join(token for token in tokens if not token.startswith(('--', '/*')))
    clean = re.sub(r'\s+', '', clean).casefold().rstrip(';')
    clean = re.sub(r'["`\[\]]', '', clean)
    expression = r"coalesce\((?:nullif\((?:trim\(d\.category\)|d\.category),''\)|d\.category),'uncategorised'\)"
    count = r'count\((?:\*|distincti\.item_code)\)'
    projection = (rf'{count}asproduct_count' if columns == ['category', 'product_count'] else
        rf'{count}asproducts,sum\(casewheni\.availablequantity>0then1else0end\)asin_stock_products')
    pattern = (rf'select{expression}ascategory,{projection}'
        r'fromitems(?:as)?ileft(?:outer)?joinproduct_details(?:as)?dond\.item_code=i\.item_code'
        rf'groupby(?:category|{expression})orderbycategory(?:asc)?')
    return re.fullmatch(pattern, clean) is not None


def category_rows(data):
    """Validate displayed category rows independently of full-inventory coverage."""
    if (not isinstance(data, dict) or data.get('ok') is not True
            or type(data.get('truncated', False)) is not bool):
        return None
    columns = data.get('columns')
    if (columns not in (['category', 'product_count'], ['category', 'products', 'in_stock_products'])
            or not isinstance(data.get('rows'), list)):
        return None
    result, seen = [], set()
    for row in data['rows']:
        if not isinstance(row, list) or len(row) != len(columns):
            return None
        category, count = row[:2]
        if (not isinstance(category, str) or not category.strip()
                or len(category) > 2000 or category in seen):
            return None
        if type(count) is not int or count < 0:
            return None
        if len(row) == 3 and (type(row[2]) is not int or not 0 <= row[2] <= count):
            return None
        seen.add(category)
        result.append(list(row))
    return result


def inventory_rows(arguments, data):
    if data.get('truncated') or not _inventory_recipe(arguments.get('query', ''), data.get('columns')):
        return None
    result = category_rows(data)
    if result is None:
        return None
    return sorted(result, key=lambda row: row[0].casefold())


def _receipt_domains(name, arguments, data):
    if not data.get('ok'):
        return frozenset()
    if name == 'billing_analysis':
        report = data.get('report')
        if report == 'store_overview':
            return frozenset({'finance', 'orders', 'invoices', 'refunds', 'refund_requests', 'customers', 'products'})
        if report in ('pending_refunds', 'pending_above_threshold'):
            return frozenset({'orders', 'refunds', 'refund_requests'})
        if report == 'refund_reconciliation':
            return frozenset({'refunds'})
        return frozenset({'finance', 'orders', 'invoices', 'refunds'})
    if name == 'billing_anomalies':
        return frozenset({'anomalies'})
    if name in ('compare_products', 'find_cheaper_alternatives'):
        return frozenset({'products'})
    if name != 'execute_sql_query':
        return frozenset()
    query = sql_text(arguments.get('query', ''))
    if re.match(r'\s*pragma\b', query):
        return frozenset()
    tables = set(re.findall(r'\b(?:from|join)\s+["`\[]?(?:main\.)?([a-z_][a-z_0-9]*)', query))
    owners = {
        'products': {'items','product_details','product_attributes','product_relations','price_history'},
        'orders': {'orders','order_details','order_service','order_requests','order_fulfilled'},
        'invoices': {'orders','invoice_lines','invoice_revisions'},
        'refunds': {'refund_requests','refund_details'},
        'refund_requests': {'refund_requests'},
        'finance': {'orders','invoice_lines'},
        'customers': {'customers','customer_billing_details'},
        'credit': {'store_credit_ledger'}, 'anomalies': {'billing_audit'},
    }
    result = {domain for domain, sources in owners.items() if sources & tables}
    refund_fields = {'refund_amount', 'refund_status', 'refund_reason'}
    columns = data.get('columns', [])
    if 'orders' in tables and (re.search(r'\brefund_(?:amount|status|reason)\b', query) or
            isinstance(columns, list) and any(isinstance(column, str) and column.casefold() in refund_fields for column in columns)):
        result.add('refunds')
    business_tables = set().union(*owners.values(), {'billing_config', 'promotions'})
    if business_tables & tables:
        result.add('records')
    return frozenset(result)


def evidence_for(contract, name, arguments, data):
    if not data.get('ok'):
        return False
    if contract.kind == 'inventory_overview':
        return name == 'execute_sql_query' and inventory_rows(arguments, data) is not None
    if contract.kind == 'store_overview':
        actual = data.get('filters', {})
        return (name == 'billing_analysis' and data.get('report') == 'store_overview'
                and data.get('coverage', {}).get('complete') is True and isinstance(actual, dict)
                and {key: actual.get(key) for key in FILTER_KEYS} == contract.expected_filters
                and not any(value for key, value in actual.items() if key not in FILTER_KEYS)
                and data.get('chart_mode', 'overview') == contract.chart_mode)
    observed = _receipt_domains(name, arguments, data)
    return bool(observed & contract.domains) if contract.domains else bool(observed)


def evidence_satisfied(contract, receipts):
    if contract.kind != 'data':
        return any(evidence_for(contract, *receipt) for receipt in receipts)
    observed = frozenset().union(*(_receipt_domains(*receipt) for receipt in receipts))
    return contract.domains <= observed if contract.domains else bool(observed)


def report_reply(contract, receipt):
    """Stable presentation of verified reports; never reuse model-made numbers."""
    name, arguments, data = receipt
    if contract.kind == 'inventory_overview':
        rows = inventory_rows(arguments, data)
        total = sum(row[1] for row in rows)
        if not rows:
            return 'There are no products in the store. The category table is empty.'
        stock = ''
        if data['columns'] == ['category', 'products', 'in_stock_products']:
            available = sum(row[2] for row in rows)
            stock = f'{available} product' + ('s are' if available != 1 else ' is') + ' currently in stock. '
        return (f'The store has {total} product' + ('s' if total != 1 else '')
                + f' across {len(rows)} categor' + ('ies' if len(rows) != 1 else 'y') + '. '
                + stock + 'The table lists every category. Product counts refer to distinct products, not stock units.')
    summary = data['summary']
    reply = (f"The report covers {summary['matched_orders']} orders: {summary['invoice_count']} active issued invoices, "
            f"{summary['draft_count']} drafts and {summary['cancelled_count']} cancelled orders. "
            'It includes invoice totals, monthly sales, product and category sales, '
            'customer summaries, inventory and refund activity. Monetary totals are separated by currency. '
            'The tables show current results; complete datasets are available with the report. '
            'Product revenue is before order-level discounts, tax, shipping and refunds. '
            'Inventory is a current full-store snapshot.').strip()
    if contract.chart_mode in ('dashboard', 'all'):
        delivery = data.get('chart_delivery', {})
        count = delivery.get('attached_this_call', 0)
        reply += (f' {count} charts are attached.' if count else ' No charts could be generated from the matching data.')
        omissions = data.get('coverage', {}).get('chart_omissions', [])
        reasons = list(dict.fromkeys(f"{item.get('type', 'view')}: {item['reason']}" for item in omissions
                                    if isinstance(item, dict) and item.get('reason')))
        errors = delivery.get('errors', [])
        if reasons or errors:
            reply += ' Unavailable views: ' + '; '.join(dict.fromkeys([*reasons, *errors])) + '.'
    return reply
