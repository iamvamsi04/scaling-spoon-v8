# Billing analytics in version 6

billing_analysis and billing_anomalies are included among the twelve tools and are exposed to authenticated Admin conversations. Customer conversations use the shopping tools and scoped SQL. Start the application with app.py; analytics uses the main agent and MCP server. See WORKFLOWS.md for admin setup.

billing_analysis summarizes invoice face values, discounts, tax, shipping, ledger refunds, top customers/products and current-cost estimates. Filters are customer_id, order_id and inclusive order-date range. top_n limits only the displayed rankings. Drafts and cancelled orders do not contribute to financial totals.

billing_anomalies checks invoice arithmetic, quantities, refund metadata, unusually high invoice amounts and dated prices. It returns evidence and review suggestions for margin pressure, refunds or excess stock. It does not predict fraud or execute cost changes.

Costs use current baseprice; historical purchase cost and overhead are unavailable. Product revenue precedes order-level discounts, taxes, shipping and refunds. Payment and credit-note records are not stored. These totals do not prove bank settlement. Mixed-currency summary selections require narrower filters; trend reports produce a separate chart for each currency.

Customer-facing replies must not reveal internal procurement costs or margins. Shopping comparisons use compare_products and find_cheaper_alternatives, which return public selling prices and recorded product facts. See WORKFLOWS.md and CUSTOMER_CONVERSATIONS.md.

## Pictorial analytics

For invoice trends, the admin agent prefers the existing billing_analysis tool with trend_period (day, week, month, quarter or year), trend_metric and chart_type (line, bar, area or step_line). For example, the request "Show the monthly revenue trend based on order totals as a chart" maps to billing_analysis(trend_period="month", trend_metric="order_total"). The report uses charged_price once per issued, non-cancelled invoice. Refunds are deducted only for net_billed. Other metrics are refunds, subtotal, discount, tax and shipping. Date/customer/order filters apply. Empty selections produce no invented chart or totals.

The tool groups normalized invoice currency after resolving legacy missing snapshots from configuration. One month and currency produce one row, with an ISO period-start coordinate. Weeks start Monday; quarters and years start on their first day. Periods without invoices are omitted rather than presented as recorded zero observations. No additional tool or schema migration is needed. More than 10000 matching invoices or six currencies requires narrower scope.

For other reports the agent uses execute_sql_query with chart metadata. Temporal axes also accept YYYY-MM and YYYY labels, converted locally to period starts; timeline endpoints still require full dates. Duplicate aggregate coordinates are rejected with repair guidance, never silently summed (which would corrupt averages or ratios). For example:

```json
{
  "query": "SELECT strftime('%Y-%m',o.order_date) AS month, COALESCE(d.currency,(SELECT value FROM billing_config WHERE key='currency'),'USD') AS currency, ROUND(SUM(o.charged_price),2) AS billed_revenue FROM orders o LEFT JOIN order_details d ON d.order_id=o.order_id WHERE o.order_invoice IS NOT NULL AND COALESCE(d.status,'active')<>'cancelled' AND COALESCE(d.currency,(SELECT value FROM billing_config WHERE key='currency'),'USD')=? GROUP BY month,COALESCE(d.currency,(SELECT value FROM billing_config WHERE key='currency'),'USD') ORDER BY month",
  "parameters": ["USD"],
  "chart": {"type":"line","title":"Monthly billed revenue (USD)","x":"month","y":["billed_revenue"]}
}
```

The application supports 25 chart types through the existing execute_sql_query tool and Altair renderer. Admin authorization, saved chart attachments and the six-chart limit per response still apply. Ordinary SQL reads use SQL_MAX_ROWS; chart queries may return up to 1000 rows. Larger results must be filtered or aggregated. Histograms and boxplots require complete observations, never a silently truncated sample.

| Type | Required columns and meaning |
| --- | --- |
| bar, grouped_bar, horizontal_bar | x category and y list of 1-4 numeric measures. Multiple measures are grouped. |
| stacked_bar, normalized_bar | x and y list of nonnegative measures; normalized charts show percentage shares and need a positive total at every x. |
| line, step_line | Ordered x and 1-4 numeric y measures. |
| area, stacked_area, normalized_area | Ordered x and 1-4 y measures; stacked and normalized variants require nonnegative measures. |
| pie, donut | x category and one nonnegative y, positive total, at most 12 categories. |
| scatter | Numeric x and one numeric y; optional group column controls color. |
| bubble | Numeric x, one numeric y, nonnegative size column with some positive sizes; optional group. |
| histogram | Numeric x observations, y:[], optional bins from 2 to 50 (maximum bin count). |
| boxplot | Categorical x and one raw numeric y; repeated observations per category; optional group. |
| heatmap | x category, group as the second category, one numeric y; unique x/group pairs. |
| waterfall | Ordered x stages and one signed y change. Running totals are calculated locally. Do not include an extra total row. |
| range | x and y:[lower,upper] with lower <= upper. |
| combo | x and y:[bar_measure,line_measure], with separately labelled vertical axes. |
| timeline | ISO date/time x (start), end column, group task label, y:[]. End cannot precede start. |
| candlestick | x period and y:[open,high,low,close], with low <= open/close <= high. |
| pareto | x category and one nonnegative y. Categories are sorted descending and a cumulative percentage line is added. |
| funnel | Ordered x stages and one nonnegative y; stage values must be nonincreasing and total positive. |
| treemap | Flat category x and one nonnegative y, positive total, up to 50 categories. Area is proportional to value. Zero-value categories have no area. |

Optional x_type is categorical, numeric or temporal for standard bars, lines, areas, ranges and combination charts. Scatter, bubble and histogram use numeric x; timeline uses temporal x. Categories retain query order; temporal and numeric axes preserve their actual scale. Use clear SQL aliases and chart titles including scope, units and any top-N limit.

Chart specifications accept only type, title, x, y, group, size, end, x_type and bins; extra fields are allowed only where relevant to the type. All fields refer to returned query columns. Arbitrary code, URLs and Vega specifications are not accepted. Empty results, invalid values, mixed currencies and truncation produce a chart_error while retaining the query rows. The agent must explain the issue or select a valid supported alternative. Unsupported chart families are not silently relabelled as supported charts.

Examples:

    {"type":"histogram","title":"Invoice amount distribution","x":"invoice_amount","y":[],"bins":20}
    {"type":"heatmap","title":"Units sold by product and month","x":"month","group":"product","y":["units"]}
    {"type":"bubble","title":"Price, units and sales","x":"price","y":["units"],"size":"sales","group":"category"}
    {"type":"timeline","title":"Recorded invoice dates and due dates","x":"order_date","end":"due_date","group":"order_id","y":[]}

Charts use returned values only. Waterfall, Pareto and treemap calculations are deterministic presentation calculations from those saved values. Histograms and boxplots summarize those observations in the chart renderer. Original query-backed data remain available under View chart data. Saved charts are restored on authorized conversation resume; malformed saved charts show a readable message rather than breaking the chat.


All reporting reads base tables directly. No database views are installed. Read orders with order_details for invoice totals, excluding drafts and cancelled orders. Invoice amount after recorded refunds equals charged_price minus COALESCE(refund_amount,0). This is not an amount due, collected revenue, credit balance or evidence of settlement. Paid/unpaid status and overdue balances cannot be established from this schema. Refer reconciliation requests to store staff. billing_audit remains available to admins as the change history.


For product sales, join invoice_lines to orders and items and sum line totals. Merchandise revenue precedes order-level adjustments. Currency comes from order_details.currency, falling back to billing_config.currency for legacy records. Customer SQL remains restricted to the associated customer's rows.
