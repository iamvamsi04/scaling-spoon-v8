# Version 6 end-to-end verification

Updated October 15, 2026.

Current verification: 320 tests passed in the full suite (279.02 seconds), plus one separately run direct-SQL monthly/all-USD regression added during that run. No failures; the full suite reported three existing dependency warnings. All twelve tools were exercised through real MCP. Earlier suite summaries below describe previous revisions.

Invoice trend regression tests now use realistic invoices with and without order_details, an all-USD selection, case/whitespace currency variations, separate currencies, refunds, drafts, cancelled orders and inclusive date/customer filters. They verify daily through yearly aggregation at invoice grain, monthly SQL date-bucket conversion, exact totals, duplicate rejection, chart artifacts through real MCP/API persistence, access checks, six-chart limits and actual Streamlit elements. The exact reported monthly question is replayed through the API with a billing_analysis call and an answer (two scripted model calls). Model choice of tools and live Azure response time require separate production validation.

Latest full suite after removal of speculative answer fallback: 296 passed, zero failures, in 266.20 seconds, with three existing dependency warnings. New actual-adapter/provider-mock regressions verify exactly two provider calls for a one-read answer, three for a necessary second read, local handling of plain/structured/fenced output, preserved product-reference state and admin chart attachments. All 12 real MCP tools and existing chart/financial/UI regressions passed. Compact-only tests were removed with the feature, so counts differ from earlier results. The 4–6 second production target is not established by these deterministic tests; the observed extra compact provider call is structurally removed. Earlier results below describe superseded revisions.

Latest full suite after compact continuation: 313 passed, zero failures, in 219.05 seconds, with three existing dependency warnings. Coverage includes all 12 real MCP tools, existing charts/SQL/financial and UI checks, preserving all non-system messages, retaining full context for errors/truncation/mutations, preventing compact-proposed tool execution before full replanning, rejecting malformed compact output, and real MCP chart/customer-memory flows through the actual TimedAzureClient adapter with its provider method mocked. Local tokenizer checks include tool schemas and show compact second-call inputs below 60 percent of the equivalent full second-call context in both modes. Live provider latency and language-model accuracy remain unverified by these deterministic tests. Prior suite results below are historical.

Latest full suite after chart/schema fixes: 291 passed, zero failures, in 317.35 seconds, with three existing dependency deprecation warnings. The real MCP smoke flow now invokes every one of the 12 registered tools and asserts complete coverage. New checks cover actual column owners/join keys, customer metadata restrictions, wrong-table SQL feedback and corrected chart queries, long-form grouped charts, duplicate/zero-total rejection, single-point line visibility, renderer failure handling and schema availability before the first model call. Existing chart validation/rendering, API, shopping, invoice, refund and admin tests also pass. Model responses are scripted; this is not a live GPT-5.4 accuracy or latency benchmark. Earlier results below are historical.

After removal: 31 focused tests passed in 42.39 seconds with one existing dependency warning. These cover API errors, the customer/admin end-to-end flow, Streamlit integration, agent lifecycle and guarded batching. The overview question now exercises model-selected SQL. Tests use replayed model responses and real local MCP/SQLite; the external Azure connection is not verified. The full suite was not rerun for this removal.

The fixed-query shortcut, its dedicated tests and the connection diagnostic script/tests have been removed. All questions now use the configured model. Historical full-suite result before these removals: 295 passed in 161.89 seconds; that count is not the current test inventory. Earlier shortcut latency measurements no longer apply.

Earlier October 14 baseline: 255 passed, zero failures, in 179.21 seconds. Run from version_6_latest:

    python -m pytest -q

The successful run used Python 3.13, isolated workspace test databases and permission for local MCP subprocesses and loopback services. Initial execution from the parent workspace collected archived versions and was stopped; those collection errors were unrelated to the current application.

## Coverage

- SQL product browsing: complete counts/categories for 400 products, all forty pages without missing or duplicate IDs, exhausted pages, and no views.
- Tool contract: execute_sql_query accepts only query, chart and parameters. Product navigation records the last successful item query; failure leaves the previous context intact and preference reset clears it.
- Customer journey through real API and MCP: product counts/pages, next-page continuation, account creation, comparison, alternatives, draft, promotion, basket replacement, placement, invoice PDF, partial refund and draft cancellation.
- Admin: access checks, read-only analytics, anomaly review, invoice access and visual reports.
- State: preference corrections, product references, confirmation proposals, resume/restart, request caching and recovery after committed actions.
- Boundaries: customer isolation, hidden costs, SQL authorizer, invalid requests, stock/refund constraints, UI recovery and plain-English output cleanup.
- Fresh initialization and seeding: required product details are initialized from schema.sql; seed reruns preserve existing records.
- Existing chart validation and Streamlit rendering checks remain in the full suite.

## Existing data

A read-only check of the included billing.db returned integrity_check=ok, no foreign-key violations, 400 items, 400 product-detail records and zero views. No live business records were deleted. All Python files parsed successfully. Removed feature modules and their obsolete bytecode are absent.

## Limits

Model outputs in integration tests use ReplayChatCompletionClient. The application, API, MCP, SQLite and UI paths run for real, but live Azure interpretation, authentication and latency are not established by these tests. All questions now use the model path, including whole-store overviews and product pages. A finite suite does not prove every possible conversation or external failure.

## Latency revision October 14 2026

Earlier regression suite after the October 14 latency changes: 262 passed, zero failures, in 145.17 seconds, with the same three dependency deprecation warnings. New checks cover Customer/Admin chart-schema separation across mode switches, full shared business-rule retention, window-count pagination, unchanged default reasoning configuration, supported explicit overrides and timing logs that omit message content.

The test duration is not an Azure response benchmark. Azure is not configured locally; model responses remain replayed. See PERFORMANCE.md for the 9.2% customer prompt character reduction, per-model/tool/queue timing logs and the outstanding live-deployment measurements. The Word design document was not updated, as requested.
