# Demo data for all 12 tools

Run from version_6_latest:

```powershell
python generate_seed.py
```

The included database is already upgraded. Running again reports already_seeded and makes no new batch. For an independent practice database:

```powershell
python generate_seed.py --db database/practice.db --seed 42 --as-of 2026-10-13
```

Set BILLING_DB=database/practice.db in .env and restart the API to use that file. All relative paths resolve against this project. No Azure credentials are required to generate data; Azure is needed for the conversational interface.

## What is included

The six original main tables stay in the 300-500 row range: 400 customers, 400 products, 352 orders, 453 requested lines, 401 fulfilled lines and 401 invoice lines. There are 301 placed invoices and 51 drafts. Sixty original invoices have consistent full/partial ledger refunds. Price history has 240 entries and promotions has 12 entries.

The upgrade adds 400 product-detail rows, 400 numeric attributes with units, 480 directional product relationships, 352 order-detail snapshots for the default seed. All attributes and relationships describe fictional demo products; they are not verified specifications for real merchandise. Real merchandise still needs verified product data.

Products span 20 categories. Variants include wired/wireless connections, layouts, screen sizes, storage capacities, battery durations, compatibility facts and accessories. Relationships are directional and substitute candidates still need comparison against the customer's requirements. Prices use nonoverlapping history; original invoice values are not rewritten during upgrade.

FAKE-DEMO-BULK is a valid high-value invoice, with matching request, fulfillment and invoice quantities. It deliberately triggers HIGH_INVOICE_VALUE without corrupting financial arithmetic. Existing refund frequency and stock levels also give the anomaly tool meaningful review suggestions. Invoices include full and partial recorded refund examples. No payment or credit-note records are generated.

## Try every tool

Select Customer mode and Customer ID FAKE-CUST-0001, then start a new conversation. Ask for the current totals before approving any action. These are separate examples: placing or cancelling a draft makes it unavailable for subsequent draft-edit examples.

| Tool | Example request |
| --- | --- |
| execute_sql_query | “Show available external SSDs with at least 512 GB, including item code, price and capacity.” |
| compare_products | “Compare FAKE-ITEM-0001 and FAKE-ITEM-0021 for office work. Explain their recorded differences.” |
| find_cheaper_alternatives | “Find cheaper keyboards than FAKE-ITEM-0201.” For a different random seed, use cheaper_alternatives_source printed by the generator. |
| request_order | “Prepare a draft for one FAKE-ITEM-0001 and one FAKE-ITEM-0002.” |
| manage_draft | “Change FAKE-DEMO-DRAFT to two FAKE-ITEM-0001 and one FAKE-ITEM-0002.” Review and confirm the complete replacement. Cancellation is also supported on a draft. |
| apply_promotion | “Apply FAKE-PERCENT-10 to FAKE-DEMO-DRAFT and show the revised total.” |
| place_order | “Show the current quote for FAKE-DEMO-DRAFT.” After reviewing it: “Yes, place that quoted order.” |
| download_invoice | “Download invoice INV-FAKE-DEMO-BULK.” |
| manage_customer | “Change my company to Demo Retail and keep my other details.” Review and confirm. To try account creation, start a separate anonymous conversation and request customer DEMO-NEW-001 with fictional contact details. |
| process_refund | “Review FAKE-DEMO-BULK for a USD 25 ledger refund because of damaged packaging.” Confirm the proposed ledger operation after reviewing it. Use the configured currency if changed. |
| billing_analysis | In Admin mode: “Summarize store-wide billing, discounts, tax, shipping and estimated costs.” |
| billing_anomalies | In Admin mode: “Find unusual invoices and explain the evidence and cost-review suggestions.” |

Admin visuals use execute_sql_query: “Show monthly billed revenue as a line chart in USD,” or “Show the top 10 products by billed units.” Switch to Admin using the configured admin key in the sidebar; a chat message cannot grant admin access.

Shopping memory can be demonstrated with: “I need one keyboard for office work, budget USD 100 before tax and shipping,” followed by feature preferences, comparison, “the second one,” and a quote. Resume that conversation ID after restarting to continue with the saved preferences. Need-based recommendations still depend on the model and the actual stored product facts.

## Existing data and table cleanup

The generator recognizes the old fake_seed_v1 marker and enriches that batch in place. It preserves original customers, product prices, invoice totals and lines, price history, promotions, existing metadata and imported financial histories. It adds two demo orders and deducts exactly the stock fulfilled by the new bulk invoice. Existing configuration values are retained. New products records are inserted only where missing.

The fake_seed_v2 marker prevents duplicate enrichment. Rerunning does not reset changed contact details, consumed drafts, refunds or sold stock. Use a separate fresh --db path for another clean walkthrough; do not delete seed markers to reset a working database. Custom databases that would exceed the core 500-row limit or contain conflicting fictional IDs are rejected rather than cleared.

Removed tables: billing_cases, billing_case_events and billing_disputes. These belong to retired support/dispute workflows. Invoice generation no longer reads the retired dispute flag. Payments and credit_notes are also removed; populated retired tables are backed up before removal. order_details and billing_audit remain for invoice metadata and tool change history. All three conversation tables remain: conversations, messages and shopping_states, in CONTEXT_DB.

Startup and the generator remove retired tables. Empty retired tables are dropped directly. If any contain records, the migration first makes an integrity-checked SQLite backup under the project parent's previous_versions/database_backups folder, prints its path, then drops the retired tables. Backup failure stops cleanup. Existing references that would be broken also stop and roll back the table cleanup. Schema/data seeding is transactional, but retired-table cleanup is a separate migration and is not undone by a later seed error.

The active billing database now has 14 application tables and no views. SQLite's own sqlite_sequence table is expected and retained.
