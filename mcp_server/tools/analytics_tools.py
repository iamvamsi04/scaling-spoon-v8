"""Read-only billing insights. Uses stored invoice amounts and current cost estimates."""
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
import sqlite3

from database.connection import connect

ZERO = Decimal("0")
CENT = Decimal(".01")
LIMIT = 10000


def dec(value):
    number = Decimal(str(value if value is not None else 0))
    if not number.is_finite():
        raise ValueError("Non-finite monetary value encountered.")
    return number


def cash(value):
    return str(dec(value).quantize(CENT, rounding=ROUND_HALF_UP))


def result(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return {"ok": True, **fn(*args, **kwargs)}
        except (ValueError, InvalidOperation) as exc:
            return {"ok": False, "error": str(exc)}
        except sqlite3.Error:
            return {"ok": False, "error": "Unable to read the billing database."}
    return wrapped


def filters(customer_id, order_id, date_from, date_to):
    clauses, values = ["o.order_status!='cancelled'",
                       "NOT EXISTS (SELECT 1 FROM order_details d WHERE d.order_id=o.order_id AND d.status='cancelled')",
                       "NOT EXISTS (SELECT 1 FROM order_service s WHERE s.order_id=o.order_id AND s.fulfillment_status='cancelled')"], []
    for name, value in (("customer_id", customer_id), ("order_id", order_id)):
        if value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > 50:
                raise ValueError(f"{name} must contain 1-50 characters.")
            clauses.append(f"o.{name}=?")
            values.append(value)
    for name, value, operator in (("date_from", date_from, ">="), ("date_to", date_to, "<=")):
        if value is not None:
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError(f"{name} must use YYYY-MM-DD.") from None
            clauses.append(f"o.order_date{operator}?")
            values.append(value)
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from must be on or before date_to.")
    return " AND ".join(clauses) or "1=1", values


@contextmanager
def snapshot(customer_id, order_id, date_from, date_to):
    where, args = filters(customer_id, order_id, date_from, date_to)
    conn = connect(readonly=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        orders = [dict(r) for r in conn.execute(
            f"SELECT o.*,c.name AS customer_name,s.fulfillment_status FROM orders o LEFT JOIN order_service s ON s.order_id=o.order_id LEFT JOIN customers c "
            f"ON c.customer_id=o.customer_id WHERE {where} ORDER BY o.order_date,o.order_id LIMIT ?",
            [*args, LIMIT + 1])]
        if len(orders) > LIMIT:
            raise ValueError("More than 10000 orders match. Narrow the date range or customer filter.")
        related = {}
        for table in ("invoice_lines", "order_requests", "order_fulfilled"):
            related[table] = [dict(r) for r in conn.execute(
                f"SELECT x.* FROM {table} x JOIN orders o ON o.order_id=x.order_id WHERE {where}", args)]
        # Load only products/history records referenced by this selection.
        item_filter = (
            "SELECT item_code FROM invoice_lines WHERE order_id IN (SELECT o.order_id FROM orders o WHERE " + where + ") "
            "UNION SELECT item_code FROM order_requests WHERE order_id IN (SELECT o.order_id FROM orders o WHERE " + where + ") "
            "UNION SELECT item_code FROM order_fulfilled WHERE order_id IN (SELECT o.order_id FROM orders o WHERE " + where + ")"
        )
        items = {r["item_code"]: dict(r) for r in conn.execute(
            f"SELECT * FROM items WHERE item_code IN ({item_filter})", args * 3)}
        histories = defaultdict(list)
        for row in conn.execute(f"SELECT * FROM price_history WHERE item_code IN ({item_filter})", args * 3):
            histories[row["item_code"]].append(dict(row))
        config = dict(conn.execute("SELECT key,value FROM billing_config"))
        fallback = str(config.get('currency', 'USD')).strip().upper()
        currencies = {r[0] for r in conn.execute(
            f"SELECT DISTINCT COALESCE(NULLIF(UPPER(TRIM(d.currency)),''),?) FROM orders o LEFT JOIN order_details d "
            f"ON d.order_id=o.order_id WHERE o.order_status='placed' AND {where}",
            [fallback, *args])}
        if any(not c or len(c)!=3 or not c.isascii() or not c.isalpha() for c in currencies):
            raise ValueError('An invoice has missing or invalid currency. Correct it before analyzing totals.')
        if len(currencies) > 1:
            raise ValueError('Selection spans multiple currencies. Narrow the selection or query separate currency groups.')
        if currencies:
            config['currency'] = next(iter(currencies))
        else:
            config['currency'] = fallback
        yield orders, related, items, histories, config
    finally:
        conn.close()


def group(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["order_id"]].append(row)
    return grouped


def invoiced(order):
    return order['order_status'] == 'placed' and bool((order['order_invoice'] or '').strip())


def summary(orders):
    fields = ("subtotal", "discount_applied", "tax_amount", "shipping_amount", "charged_price", "refund_amount")
    amounts = {field: sum((dec(order[field]) for order in orders), ZERO) for field in fields}
    return {**{field: cash(value) for field, value in amounts.items()},
            "net_billed_after_ledger_refunds": cash(amounts["charged_price"] - amounts["refund_amount"])}


def costing(orders, lines, items):
    selected = {o["order_id"] for o in orders}
    products = {}
    missing = set()
    for line in lines:
        if line["order_id"] not in selected:
            continue
        code = line["item_code"]
        item = items.get(code)
        if item is None:
            missing.add(code)
            continue
        p = products.setdefault(code, {"item_code": code, "name": item["name"],
            "units_billed": 0, "line_revenue": ZERO, "estimated_product_cost": ZERO,
            "current_unit_cost": cash(item["baseprice"]), "available_quantity": item["availablequantity"]})
        p["units_billed"] += line["qty_billed"]
        p["line_revenue"] += dec(line["line_total"])
        p["estimated_product_cost"] += dec(item["baseprice"]) * line["qty_billed"]
    return products, sorted(missing)


@result
def billing_analysis(customer_id: str | None = None, order_id: str | None = None,
                     date_from: str | None = None, date_to: str | None = None,
                     top_n: int = 10, trend_period: str | None = None,
                     trend_metric: str = 'order_total', chart_type: str = 'line', report: str | None = None,
                     chart_mode: str = 'overview') -> dict:
    """Analyze invoices and break down revenue, discounts, tax, shipping, ledger refunds and estimated costs.

    report=store_overview returns COMPLETE store-wide summary, monthly trends,
    category/product/customer breakdowns, inventory, raw invoice observations,
    refunds and local credit activity as named tabular datasets, without top-N
    sampling. Use it for broad store analytics or an overall store dashboard.
    For store_overview, chart_mode=overview gives the two headline charts;
    dashboard gives a varied visual dashboard; all gives every meaningful supported
    chart family with explicit omissions when its required data is unavailable;
    none returns datasets without charts. All charts use actual complete datasets,
    separate currencies and never use a constant currency as an axis.
    report=pending_refunds/pending_above_threshold lists requests; refund_reconciliation compares local approvals with refunds/credits on approval dates. Never bank settlements.
    Optional filters intersect; dates are inclusive order dates (YYYY-MM-DD).
    Drafts are counted but excluded from financial totals. Costs use CURRENT item
    baseprice, not historical cost. This is a billing ledger, not payment settlement.
    Use for billing analytics or cost breakdown questions; never modifies records.
    For a revenue/order-total trend CHART set trend_period to day, week, month, quarter or year.
    trend_metric: order_total (charged_price, without subtracting refunds), net_billed (after refunds),
    refunds, subtotal, discount, tax or shipping. chart_type: line, bar, grouped_bar
    or horizontal_bar. Other families need their appropriate query data.
    Prefer this validated report for invoice trends: it groups each invoice once, normalizes currency,
    separates currencies and returns chart artifacts. Existing date/customer/order filters apply.
    """
    if chart_mode not in ('overview', 'dashboard', 'all', 'none'):
        raise ValueError('chart_mode must be overview, dashboard, all or none.')
    if chart_mode != 'overview' and report != 'store_overview':
        raise ValueError('chart_mode applies only to report=store_overview.')
    if report is not None:
        if report == 'store_overview':
            from mcp_server.reporting import store_overview
            return store_overview(customer_id, order_id, date_from, date_to, chart_mode)
        if report not in ('pending_refunds','pending_above_threshold','refund_reconciliation'): raise ValueError('Unknown local refund report.')
        from mcp_server.tools.service_workflows import refund_report
        return refund_report(report,date_from,date_to,customer_id,order_id)

    if trend_period is not None:
        return invoice_trend(customer_id, order_id, date_from, date_to, trend_period, trend_metric, chart_type)
    if type(top_n) is not int or not 1 <= top_n <= 25:
        raise ValueError("top_n must be an integer from 1 to 25.")
    with snapshot(customer_id, order_id, date_from, date_to) as (orders, related, items, history, config):
        placed = [o for o in orders if invoiced(o)]
        products, missing = costing(placed, related["invoice_lines"], items)
        estimated = sum((p["estimated_product_cost"] for p in products.values()), ZERO)
        amounts = summary(placed)
        customers = defaultdict(list)
        for order in placed:
            customers[order["customer_id"]].append(order)
        customer_rows = [{"customer_id": cid, "name": group[0]["customer_name"],
                          "invoice_count": len(group), **summary(group)} for cid, group in customers.items()]
        customer_rows.sort(key=lambda row: dec(row["charged_price"]), reverse=True)
        product_rows = []
        for product in sorted(products.values(), key=lambda p: p["line_revenue"], reverse=True)[:top_n]:
            product_rows.append({**product, "line_revenue": cash(product["line_revenue"]),
                                 "estimated_product_cost": cash(product["estimated_product_cost"])})
        return {"filters": {"customer_id": customer_id, "order_id": order_id, "date_from": date_from, "date_to": date_to},
            "currency": config.get("currency", "USD"), "matched_orders": len(orders),
            "invoice_count": len(placed), "draft_count": len(orders)-len(placed),
            "refunded_invoice_count": sum(dec(o["refund_amount"]) > 0 for o in placed),
            "totals": amounts, "average_invoice_total": cash(dec(amounts["charged_price"])/len(placed)) if placed else "0.00",
            "estimated_product_cost": cash(estimated) if not missing else None,
            "estimated_merchandise_margin_before_refunds": cash(dec(amounts["subtotal"])-dec(amounts["discount_applied"])-estimated) if not missing else None,
            "missing_product_items": missing,
            "top_customers": customer_rows[:top_n], "customers_truncated": len(customer_rows)>top_n,
            "top_products": product_rows, "products_truncated": len(products)>top_n,
            "notes": ["All matching invoices contribute to totals; top lists alone are limited.",
                "Cost and margin estimates use current baseprice. Historical purchase cost and operating expenses are unavailable.",
                "Product line revenue is before order-level discounts, tax, shipping and refunds.",
                "Refunds are ledger entries. Net billed does not prove money was collected or returned.",
                "Currency uses invoice snapshots where available; legacy invoices use the current configuration.",
                "These are invoice face-value analytics. Payments and credit notes are not tracked; these totals are not outstanding balances or proof of payment."]}


def invoice_trend(customer_id, order_id, date_from, date_to, period, metric, chart_type):
    """Invoice-grain, decimal-safe report. No guessed joins or cross-currency sums."""
    fields = {'order_total':'charged_price','net_billed':'charged_price','refunds':'refund_amount',
              'subtotal':'subtotal','discount':'discount_applied','tax':'tax_amount','shipping':'shipping_amount'}
    if not isinstance(period,str) or not isinstance(metric,str) or period not in ('day','week','month','quarter','year') or metric not in fields:
        raise ValueError('Choose day/week/month/quarter/year and a supported invoice trend metric.')
    from mcp_server.charts import canonical_chart_type
    chart_type = canonical_chart_type(chart_type)
    if chart_type not in ('line','bar','grouped_bar','horizontal_bar'):
        raise ValueError('Invoice trends support line, bar, grouped_bar or horizontal_bar charts.')
    where, args = filters(customer_id, order_id, date_from, date_to)
    conn = connect(readonly=True)
    try:
        records = conn.execute('SELECT o.*, COALESCE(NULLIF(UPPER(TRIM(d.currency)),\'\'), '
            '(SELECT NULLIF(UPPER(TRIM(value)),\'\') FROM billing_config WHERE key=\'currency\')) AS report_currency '
            'FROM orders o LEFT JOIN order_details d ON d.order_id=o.order_id WHERE '
            + where + " AND o.order_status='placed' ORDER BY o.order_date,o.order_id LIMIT ?", [*args,LIMIT+1]).fetchall()
    finally:
        conn.close()
    if len(records)>LIMIT:
        raise ValueError('Too many invoices for a complete trend. Narrow the date/customer scope.')
    buckets = defaultdict(lambda: defaultdict(lambda: ZERO))
    for row in records:
        if not str(row['order_invoice'] or '').strip():
            raise ValueError('A placed order has no invoice number. Correct it before charting invoice trends.')
        currency = row['report_currency']
        if not currency or len(currency)!=3 or not currency.isascii() or not currency.isalpha():
            raise ValueError('An invoice has missing or invalid currency. Correct the record before charting.')
        day = date.fromisoformat(row['order_date'])
        if period=='week': day -= timedelta(days=day.weekday())
        elif period=='month': day=day.replace(day=1)
        elif period=='quarter': day=day.replace(month=(day.month-1)//3*3+1,day=1)
        elif period=='year': day=day.replace(month=1,day=1)
        amount = dec(row[fields[metric]])
        if metric=='net_billed': amount -= dec(row['refund_amount'])
        buckets[currency][day.isoformat()] += amount
    from mcp_server.charts import prepare_chart
    reports=[]
    for currency, values in sorted(buckets.items()):
        columns=['period',metric,'currency']
        rows=[[key,cash(value),currency] for key,value in sorted(values.items())]
        label={'day':'Daily','week':'Weekly','month':'Monthly','quarter':'Quarterly','year':'Yearly'}[period]
        chart=prepare_chart(columns,rows,{'type':chart_type,'x':'period','y':[metric],
            'x_type':'temporal','title':f'{label} {metric.replace("_"," ")} ({currency})'})
        reports.append({'columns':columns,'rows':rows,'row_count':len(rows),'truncated':False,
                        'chart':{k:v for k,v in chart.items() if k!='data'}})
    return {'report':'invoice_trend','period':period,'metric':metric,'invoice_count':len(records),
        'filters':{'customer_id':customer_id,'order_id':order_id,'date_from':date_from,'date_to':date_to},
        'chart_results':reports,'notes':[
            'Order totals are stored charged_price; refunds are subtracted only for net_billed.',
            'Issued invoices only; cancelled orders excluded. One contribution per order. Currencies remain separate.',
            'Periods start on the plotted date; weeks start Monday. Missing periods are not fabricated as zero.',
            'Legacy invoices without currency snapshots use configured currency. No payment settlement is implied.']}


@result
def billing_anomalies(customer_id: str | None = None, order_id: str | None = None,
                      date_from: str | None = None, date_to: str | None = None,
                      max_findings: int = 25, high_charge_threshold: str = "5000.00") -> dict:
    """Detect inconsistent invoice totals, quantities, refunds and historical prices; suggest cost reviews.

    Read-only rules, not fraud predictions. Returns evidence and review suggestions,
    including refund exposure, current-cost margin pressure and stocked products.
    high_charge_threshold is in the configured currency. Dates filter order_date.
    """
    if type(max_findings) is not int or not 1 <= max_findings <= 100:
        raise ValueError("max_findings must be an integer from 1 to 100.")
    threshold = dec(high_charge_threshold)
    if threshold <= 0:
        raise ValueError("high_charge_threshold must be positive.")
    findings = []
    def flag(rule, order, evidence, severity="warning"):
        findings.append({"rule": rule, "severity": severity, "order_id": order["order_id"], "evidence": evidence})
    with snapshot(customer_id, order_id, date_from, date_to) as (orders, related, items, histories, config):
        billed, fulfilled, requested = (group(related[name]) for name in ("invoice_lines", "order_fulfilled", "order_requests"))
        for order in orders:
            oid = order["order_id"]
            lines, shipped, demand = billed[oid], fulfilled[oid], requested[oid]
            expected = dec(order["subtotal"])-dec(order["discount_applied"])+dec(order["tax_amount"])+dec(order["shipping_amount"])
            if cash(expected) != cash(order["charged_price"]):
                flag("TOTAL_MISMATCH", order, {"expected": cash(expected), "stored": cash(order["charged_price"])}, "error")
            if dec(order["discount_applied"]) > dec(order["subtotal"]):
                flag("EXCESS_DISCOUNT", order, {"discount": cash(order["discount_applied"]), "subtotal": cash(order["subtotal"])}, "error")
            refund = dec(order["refund_amount"])
            if order["refund_amount"] is not None:
                if refund <= 0 or refund > dec(order["charged_price"]) or not invoiced(order):
                    flag("INVALID_REFUND", order, {"refund_amount": cash(refund), "charged_price": cash(order["charged_price"])}, "error")
                if not order["refund_status"] or not order["refund_reason"]:
                    flag("INCOMPLETE_REFUND", order, "Refund has no status or reason.", "error")
            elif order["refund_status"] or order["refund_reason"]:
                flag("INCOMPLETE_REFUND", order, "Refund metadata exists without an amount.", "error")
            if not invoiced(order):
                if lines or shipped:
                    flag("DRAFT_HAS_BILLING_RECORDS", order, "Invoice number missing despite invoice/fulfillment lines.", "error")
                continue
            if not lines:
                flag("INVOICE_WITHOUT_LINES", order, "Invoice has no persisted invoice lines.", "error")
            line_sum = sum((dec(line["line_total"]) for line in lines), ZERO)
            if cash(line_sum) != cash(order["subtotal"]):
                flag("SUBTOTAL_MISMATCH", order, {"line_sum": cash(line_sum), "subtotal": cash(order["subtotal"])}, "error")
            def quantities(records, field):
                counts = Counter()
                for row in records:
                    counts[row["item_code"]] += row[field]
                return dict(counts)
            billed_qty,fulfilled_qty,requested_qty=quantities(lines,'qty_billed'),quantities(shipped,'quantity'),quantities(demand,'quantity')
            if not (billed_qty==fulfilled_qty==requested_qty):
                partial=(order.get('fulfillment_status')=='shipped' and billed_qty==fulfilled_qty and bool(billed_qty)
                         and all(0<qty<=requested_qty.get(code,0) for code,qty in billed_qty.items()))
                flag('PARTIAL_FULFILLMENT' if partial else 'QUANTITY_MISMATCH',order,
                     {'billed':billed_qty,'fulfilled':fulfilled_qty,'requested':requested_qty},'warning' if partial else 'error')
            for line in lines:
                evidence = {"item_code": line["item_code"], "invoice_line_id": line["id"]}
                if line["qty_billed"] <= 0 or dec(line["unit_price_billed"]) < 0 or dec(line["line_total"]) < 0:
                    flag("INVALID_INVOICE_LINE", order, evidence, "error")
                if cash(dec(line["unit_price_billed"])*line["qty_billed"]) != cash(line["line_total"]):
                    flag("LINE_TOTAL_MISMATCH", order, evidence, "error")
                history = [h for h in histories[line["item_code"]] if h["effective_from"] <= order["order_date"]
                           and (not h["effective_to"] or h["effective_to"] >= order["order_date"])]
                if len(history) > 1:
                    flag("OVERLAPPING_PRICE_HISTORY", order, evidence, "error")
                elif history and cash(history[0]["unit_price"]) != cash(line["unit_price_billed"]):
                    flag("HISTORICAL_PRICE_MISMATCH", order, {**evidence,
                         "history_price": cash(history[0]["unit_price"]), "billed_price": cash(line["unit_price_billed"])})
                if line["item_code"] not in items:
                    flag("MISSING_PRODUCT_ITEM", order, evidence, "error")
            if dec(order["charged_price"]) > threshold:
                flag("HIGH_INVOICE_VALUE", order, {"total": cash(order["charged_price"]), "threshold": cash(threshold)}, "review")
        placed = [o for o in orders if invoiced(o)]
        products, missing = costing(placed, related["invoice_lines"], items)
        recommendations = []
        refunds = [o for o in placed if dec(o["refund_amount"]) > 0]
        if placed and refunds:
            rate = Decimal(len(refunds))*100/len(placed)
            if rate >= 15:
                recommendations.append({"action": "Review common refund reasons and product quality before further purchasing.",
                    "evidence": {"refunded_invoices": len(refunds), "invoices": len(placed), "refund_rate_percent": cash(rate),
                                 "ledger_refund_exposure": cash(sum((dec(o["refund_amount"]) for o in refunds), ZERO))},
                    "basis": "15 percent or more of selected invoices have a ledger refund; this is exposure, not guaranteed savings."})
        low_margin = []
        for order in placed:
            lines = billed[order["order_id"]]
            if not lines or any(line["item_code"] not in items for line in lines):
                continue
            cost = sum((dec(items[l["item_code"]]["baseprice"])*l["qty_billed"] for l in lines), ZERO)
            net = dec(order["subtotal"])-dec(order["discount_applied"])
            if net-cost < net*Decimal("0.10"):
                low_margin.append(order["order_id"])
        if low_margin:
            recommendations.append({"action": "Review discounts and supplier costs on invoices with less than 10 percent estimated merchandise margin.",
                "affected_invoice_count": len(low_margin), "example_order_ids": low_margin[:10],
                "basis": "Estimate uses current baseprice and excludes refunds, tax, shipping and operating costs."})
        # Customer/order filters describe only part of product demand: do not infer global overstock from them.
        if not customer_id and not order_id:
            stock = [p for p in products.values() if p["available_quantity"] >= 50
                     and p["available_quantity"] > 4*p["units_billed"]]
            stock.sort(key=lambda p: dec(p["current_unit_cost"])*p["available_quantity"], reverse=True)
            if stock:
                recommendations.append({"action": "Review replenishment for stock large relative to billed units in the selected period.",
                    "affected_product_count": len(stock), "examples": [{"item_code": p["item_code"],
                        "on_hand": p["available_quantity"], "units_billed_in_period": p["units_billed"],
                        "inventory_value_at_current_cost": cash(dec(p["current_unit_cost"])*p["available_quantity"])} for p in stock[:10]],
                    "basis": "At least 50 on hand and over four times period billed units. Review seasonality and demand outside the selected period; inventory value is not savings."})
        findings.sort(key=lambda f: ({"error": 0, "warning": 1, "review": 2}[f["severity"]], f["order_id"], f["rule"]))
        return {"currency": config.get("currency", "USD"), "matched_orders": len(orders),
            "filters": {"customer_id": customer_id, "order_id": order_id, "date_from": date_from, "date_to": date_to},
            "high_charge_threshold": cash(threshold), "finding_count": len(findings),
            "counts_by_rule": dict(Counter(f["rule"] for f in findings)), "findings": findings[:max_findings],
            "findings_truncated": len(findings)>max_findings, "optimization_suggestions": recommendations,
            "notes": ["Rules identify review candidates; they do not establish fraud or guaranteed savings.",
                "Drafts are checked for total/refund/record consistency but excluded from invoice-based recommendations.",
                "Price mismatches compare against currently stored dated price history; missing history is not replaced with today's selling price.",
                "Historical tax rates, shipping costs, purchase costs and promotion versions are unavailable, so they are not inferred.",
                "Inventory suggestions cover products billed in the selected period, not the entire products.",
                "No changes or refunds are executed by this tool."]}
