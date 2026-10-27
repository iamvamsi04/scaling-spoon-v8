"""Native business tables from known, authorized tool result fields."""
import re
import unicodedata

from context.tables import table_artifact


AMOUNTS = ('subtotal','discount_applied','tax_amount','shipping_amount','charged_price',
           'refund_amount','net_billed_after_ledger_refunds')


def title(key):
    return key.replace('_', ' ').capitalize()


def evidence_text(value, depth=0):
    """Readable evidence cells, without turning a tool payload into visible JSON."""
    if depth > 3:
        return 'Additional detail requires review'
    if isinstance(value, dict):
        return '; '.join(f'{title(str(key))}: {evidence_text(cell,depth+1)}'
                         for key,cell in value.items())
    if isinstance(value, list):
        return '; '.join(evidence_text(cell,depth+1) for cell in value)
    return 'Not recorded' if value is None else str(value)


def tool_tables(name, data, arguments=None):
    """Display whitelisted records only. Never query, convert units or infer totals."""
    if not isinstance(data, dict) or not data.get('ok'):
        return []
    tables = []

    def add(label, columns, rows, *, coverage=None, total=None, truncated=False):
        tables.append(table_artifact(label, columns, rows, coverage=coverage,
                                     total_rows=total, truncated=truncated))

    def records(label, rows, fields, *, coverage=None, total=None, truncated=False):
        add(label, [title(field) for field in fields],
            [[row.get(field) for field in fields] for row in rows],
            coverage=coverage,total=total,truncated=truncated)

    if name in ('compare_products', 'find_cheaper_alternatives'):
        alternative = name == 'find_cheaper_alternatives'
        products = data.get('alternatives', []) if alternative else data.get('products', [])
        price_fields = ('item_code','name','unit_price','quantity','merchandise_total','available_quantity')

        def prices(label, rows, **kwargs):
            add(label, ['Item code','Product','Currency','Unit price','Quantity','Merchandise total','Available units'],
                [[p.get('item_code'),p.get('name'),data.get('currency'),
                  *[p.get(field) for field in price_fields[2:]]] for p in rows], **kwargs)

        coverage = 'Current merchandise prices before discounts, tax and shipping. Price does not establish overall quality or compatibility.'
        if alternative:
            source = data.get('source')
            if isinstance(source, dict):
                prices('Reference product',[source],coverage='Reference for price comparison; this is not one of the cheaper alternatives.')
            coverage += f" Showing {len(products)} of {data.get('matched_count',len(products))} qualifying alternatives."
        prices('Cheaper alternatives' if alternative else 'Product comparison', products,
               coverage=coverage,total=data.get('matched_count') if alternative else None,
               truncated=bool(data.get('truncated')) if alternative else False)
        displayed = ([data['source']] if alternative and isinstance(data.get('source'),dict) else []) + products
        def normal(value):
            return ' '.join(re.findall(r'[^\W_]+',unicodedata.normalize('NFKC',str(value)).casefold()))
        feature_names = {}
        for product in displayed:
            for key in product.get('features',{}):
                feature_names.setdefault(normal(key),key)
        feature_rows, checks = [], []
        for product in displayed:
            for feature_key, feature in sorted(feature_names.items()):
                ambiguous = {normal(key) for key in product.get('ambiguous_features',[])}
                recorded_key = next((key for key in product.get('features',{}) if normal(key)==feature_key),None)
                value = ('Conflicting records' if feature_key in ambiguous else
                         product.get('features',{}).get(recorded_key,'Not recorded'))
                # Feature objects/lists are explanatory text; scalar units remain separate.
                if isinstance(value,(dict,list)):
                    value = evidence_text(value)
                feature_rows.append([product.get('item_code'),product.get('name'),feature,value,
                                     product.get('feature_units',{}).get(recorded_key)])
            checks.append([product.get('item_code'),product.get('enough_stock'),product.get('within_budget'),
                           ', '.join(product.get('matched_features',[])) or 'None specified',
                           ', '.join(product.get('unknown_features',[])) or 'None',
                           ', '.join(product.get('different_features',[])) or 'None'])
        if feature_rows:
            add('Recorded product features',['Item code','Product','Feature','Recorded value','Unit'],feature_rows,
                coverage='Missing and conflicting records are not established specifications. Units are preserved without conversion.')
        if checks:
            add('Requested criteria',['Item code','Enough stock','Within merchandise budget','Matched features','Unrecorded requirements','Different requirements'],checks,
                coverage='Budget and feature checks refer only to the requirements supplied for this comparison.')
    elif name == 'billing_analysis':
        report = data.get('report')
        if report == 'store_overview':
            return []  # Full dataset attachments have their own validated presentation.
        if report == 'invoice_trend':
            for source in data.get('chart_results',[]):
                add(source.get('chart',{}).get('title','Invoice trend'),source['columns'],source['rows'],
                    coverage='Issued invoice observations grouped once per period and currency; missing periods are not assumed zero.',
                    truncated=source.get('truncated',False))
        elif report in ('pending_refunds','pending_above_threshold'):
            records('Pending refund requests',data.get('requests',[]),
                ('order_id','customer_id','amount','currency','status','reason','requested_at','required_approver'),
                coverage='Customer requests awaiting staff approval. No refund is approved by displaying this list.',total=data.get('count'))
        elif report == 'refund_reconciliation':
            records('Refund reconciliation',data.get('mismatches',[]),('order_id','issue'),
                coverage=data.get('scope'),total=data.get('count'))
            if data.get('undated_records'):
                records('Refund records without approval dates',data['undated_records'],('order_id','issue'),
                    coverage='Separate undated history gaps; these are not attributed to the selected approval-date range.',total=data.get('undated_count'))
        elif isinstance(data.get('totals'),dict):
            counts = ('matched_orders','invoice_count','draft_count','refunded_invoice_count')
            add('Invoice counts',['Measure','Count'],[[title(k),data[k]] for k in counts if k in data])
            money = {**{k:data['totals'][k] for k in AMOUNTS if k in data['totals']},
                     **{k:data[k] for k in ('average_invoice_total','estimated_product_cost',
                        'estimated_merchandise_margin_before_refunds') if k in data}}
            add('Billing amounts',['Measure','Amount','Currency'],[[title(k),v,data.get('currency')] for k,v in money.items()],
                coverage='Issued invoice amounts only. Product cost and margin use current unit costs; these are not payment balances.')
            for key,fields,flag in (
                ('top_products',('item_code','name','units_billed','line_revenue','current_unit_cost','estimated_product_cost','available_quantity'),'products_truncated'),
                ('top_customers',('customer_id','name','invoice_count',*AMOUNTS),'customers_truncated')):
                if key in data:
                    records(title(key),data[key],fields,
                        coverage='Ranked preview for the requested scope. All matching issued invoices contribute to the summary totals. Amounts use '+str(data.get('currency','the recorded currency'))+'.',
                        truncated=bool(data.get(flag)))
    elif name == 'billing_anomalies':
        add('Billing findings',['Order id','Rule','Severity','Evidence'],
            [[r.get('order_id'),r.get('rule'),r.get('severity'),evidence_text(r.get('evidence'))]
             for r in data.get('findings',[])],total=data.get('finding_count'),truncated=bool(data.get('findings_truncated')),
            coverage='Review candidates for the selected scope, not proof of fraud or permission to change records.')
        if data.get('optimization_suggestions'):
            add('Review suggestions',['Suggestion','Basis'],
                [[r.get('action'),r.get('basis')] for r in data['optimization_suggestions']],
                coverage='Suggestions require review; amounts are not guaranteed savings.')
    elif name in ('request_order','manage_draft') and isinstance(data.get('estimate'),dict):
        add('Draft estimate',['Measure','Amount'],[[title(k),data['estimate'][k]]
            for k in AMOUNTS if k in data['estimate']],coverage='Draft estimate only. The order is not placed by displaying this table.'
            + (' Currency: '+str(data['currency'])+'.' if data.get('currency') else ' Currency is not included in this result; check the draft details.'))
    elif name == 'process_refund':
        if isinstance(data.get('items'),list):
            records('Return items',data['items'],('item_code','quantity'),
                    coverage='Items recorded in the tool result; an estimate is not an approved refund.')
        if isinstance(data.get('orders'),list):
            records('Return request review',data['orders'],
                ('order_id','eligible','amount','currency','required_approver','submitted','reason'),coverage=data.get('note'),total=data.get('matched_count'))
    return tables
