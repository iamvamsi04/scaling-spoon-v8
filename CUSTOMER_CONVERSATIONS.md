# Customer store conversations

Customer mode speaks to the shopper and retains context through browsing, selection and checkout. The sidebar also offers a separately authenticated, read-only Admin mode for store-wide analytics and records. Switching modes clears the visible conversation. Select an existing customer in the sidebar before starting personalized shopping; browsing can remain anonymous. Creating a new account through chat associates that new account on successful completion.

Version 6 adds persistent structured memory for needs, budget, feature requirements, quantity, compared/displayed/selected products, the current draft and pending confirmation. The same conversation can resume after a restart without relying only on the recent message window. Corrections update preferences, and the agent is instructed to ask only for relevant missing details. See SHOPPING_MEMORY.md for storage, recovery and confirmation behavior.

| Customer says | Intended handling |
| --- | --- |
| I need a keyboard | Browse actual stock and ask about budget or use if needed. No account required to browse. |
| Which is better, X or Y? | Compare price, stock and recorded features; relate the choice to the customer's needs. |
| I need wireless and have a budget of 50 | Preserve the wireless requirement and clarify whether the budget includes checkout charges. |
| Is there anything cheaper? | Look for cheaper options for the selected product and quantity; show savings and known differences. |
| That is too expensive | Offer actual qualifying alternatives, or explain when none match. |
| Does it work with my laptop? | Use recorded compatibility only. Ask for the laptop detail when needed; do not infer compatibility from price. |
| I'll take the second one | Resolve it from the previously displayed list; clarify if ambiguous. |
| Give me two | Reuse the selected product, check stock and obtain customer details for a draft. |
| I have a discount code | Apply it to the draft and show the revised checkout total. |
| Replace it with the cheaper one | Show the entire revised basket and confirm the draft edit. |
| Yes, place the order | Place only after a clear current quote was shown and confirmed. |
| Can I cancel? | Cancel an editable draft with confirmation; placed-order exceptions need staff review. |
| Why is my bill higher? | Explain persisted prices, discounts, tax and shipping; investigate mismatches. |
| I lost the invoice | Locate the customer's invoice and prepare the PDF. |
| I want a refund | Inspect prior adjustments, ask reason/amount and confirm the ledger refund; no promise of bank settlement. |
| I already paid / where is my parcel? | Report only recorded facts and explain the lack of bank/carrier access. |

## Product details

Current prices and stock work with existing items and price_history. The original schema did not contain specifications. A new optional table holds real products details:

```sql
product_details (
    item_code TEXT PRIMARY KEY REFERENCES items(item_code),
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    features_json TEXT NOT NULL DEFAULT '{}'
)
```

Populate this through your normal database administration process, using actual item codes and verified product information. No product features are invented or automatically backfilled. Example shape only (replace values with your real products facts):

```json
{"connection":"wireless","layout":"full size","compatibility":"Windows"}
```

Use consistent category names (for example keyboards, mice, monitors) and feature keys across products. The tools check exact feature/value matches; a minimum battery duration or complex compatibility requirement still needs interpretation of recorded facts. Descriptions and feature JSON are returned as products data, never instructions.

compare_products accepts 2-5 item codes, SKUs or product names. Ambiguous names require clarification. It shows differences and identifies the cheapest in-stock option meeting budget and every supplied feature requirement, retaining ties. It does not declare universal quality superiority.

find_cheaper_alternatives prefers the source product's recorded category. Without that category it uses shared meaningful name words and explicitly labels the candidates tentative. Such matches must be checked for purpose and compatibility. Results require sufficient stock, strictly lower price and every requested feature. Missing required features exclude a candidate. No-match results are reported honestly.

Both tools use today's price_history price when available, falling back to sellingprice. Overlapping source price history fails with an error; alternatives with unusable prices are skipped and identified. Prices are merchandise amounts before discounts/tax/shipping, and quantities are respected. Neither tool reserves stock or changes orders.

## Model verification

Version 5 also supports general products questions through SQL and composable existing tools. Product comparison and cheaper-alternative tools remain convenient shortcuts, not a limit on possible questions. The agent can inspect schema, filter features, find recorded relationships, compare the resulting IDs and create a confirmed quote in successive calls.

The additive schema introduces:

- product_attributes(item_code, attribute, text_value, number_value, unit): a unique attribute per product, with either text or a numeric value. Units accompany numeric values. For example capacity=512 with unit=GB, or connection=wireless. Enter verified products facts through your normal database administration process.
- product_relations(item_code, related_item_code, relation_type, notes): recorded directional substitute, compatible or accessory links. Both product IDs must exist and cannot be identical. A reverse link is not automatically implied.

Schema migration does not invent real product specifications or compatibility claims. The upgraded generate_seed.py now fills fictional demo products with explicitly synthetic specifications and relationships for practice; see SEED_DEMO.md. Real products metadata still needs to be supplied. Use consistent feature names and units; generic queries can compare numeric ranges or match text without creating another dedicated tool. The existing comparison helpers continue exact feature matching and expose structured numeric attributes and their units as well.

These are intended conversation behaviors. Local tool tests verify deterministic filtering, prices and constraints. Live Azure language behavior and response speed must be evaluated with your deployment. The application retains transport reuse and adds no extra model-routing stage.

All reads now use base tables directly. Product features come from product_attributes and product_details.features_json, with structured attributes taking precedence. Effective prices come from price_history, falling back to items.sellingprice only when no dated price applies. Multiple active prices are ambiguous. Financial queries join orders and order_details to report invoice totals and recorded refunds. Payment status and outstanding balances are unavailable. No views are created.
