# Response latency

Invoice trend charts now use one billing_analysis report call to aggregate invoice-grain amounts and attach validated charts. This avoids requiring a separate SQL/chart request for standard daily through yearly trends and prevents common month-format/grouping repair loops. A complete read-and-answer still needs two model inferences; the supplied 9.162-second trace spent 9.115 seconds in those calls, versus 0.021 seconds in SQL. These changes do not establish a 4–6 second live latency guarantee.

## Remove speculative answer fallback October 15 2026

Production evidence showed full planning at 3.449 seconds, a compact answer at 3.122 seconds and a full fallback at 2.344 seconds, with one SQL execution taking 0.027 seconds. The compact attempt did not provide a latency benefit in that sample and added an extra inference. Its contents were not logged, so the exact fallback reason (tool request, format or incomplete finish) is unknown.

The compact stage and its helper have been removed. Each required inference now makes one provider call using full instructions. The existing local reply-envelope parser handles supported formatting variations; there is no extra inference merely to reformat a compact reply. Additional calls remain possible when the task genuinely needs more tools. Full chart, schema, financial and shopping policies, guarded read batching and provider max_retries=0 are retained.

Subtracting the discarded 3.122-second call from the reported 9.141-second turn gives about 6.02 seconds as a counterfactual for that trace, not a measured new response time. Azure latency varies; 4–6 seconds is the target, not a guaranteed bound. New regression tests count actual adapter/provider invocations using mocked provider replies and real MCP: one read plus one answer uses two calls, an additional necessary read uses three, and plain/fenced/structured final text does not trigger a duplicate inference.

Logs now contain one billing_model entry per provider call. billing_model_phase phase=compact_answer and billing_compact fallback=full_planning no longer occur. Two custom tool debug prints do not establish two executions; the reported billing_turn tool_calls=1 records one execution.

Deploy agent/billing_agent.py, delete agent/continuation.py if present, and restart the API. No model, reasoning, timeout or database settings changed.

All questions now use the configured model. The fixed-query shortcut and connection diagnostic script have been removed at the user's request. Previous direct-read benchmark figures do not describe the current application. Clear connection error reporting remains; removing the shortcut does not resolve the external Azure connection failure.

## General read batching October 15 2026

GPT-5.4 remains the configured model. Model-selected independent reads can now share a tool round, regardless of the user's wording. For example, product comparison, cheaper alternatives and a stock/count query can be requested in one model response, followed by one final model response. Previously parallel_tool_calls=False requested one tool per model response. The benefit is fewer inference round trips when the model selects a batch; single-tool requests and dependent operations still need their normal rounds.

The model client enables batching only when attached to the execution guard. Before dispatch, the guard checks the entire batch. Batches containing writes, invoice generation or unknown tools are rejected in full without executing any member. The model receives an explicit error and must retry those operations individually. Existing authorization, confirmation, SQL and billing validation still apply. Read dispatch is ordered to preserve shopping navigation and checkpoint state; this optimization batches model planning, not database transactions. Logs report billing_batch calls and allowed status without content.

Regression coverage includes a real MCP flow with three independent reads in exactly two replayed model calls, rejection of mixed/write/unknown batches before MCP dispatch, and deterministic read observation order. These are orchestration checks, not live GPT-5.4 accuracy or latency measurements. A rejected batch may require another model round.

The requested target is 2–3 seconds, with five seconds as the desired maximum for complete answers. It is not achieved or guaranteed for all requests. The supplied GPT-5.4 model timings alone exceed that budget, and variable external inference latency, dependent tool chains and queueing remain. No timeout was shortened to manufacture apparent speed, no model was replaced, and no reasoning or business validation was removed. Live deployment measurements are still required across shopping, order, refund and admin workflows. Official latency guidance describes reducing round trips and batching independent work: https://developers.openai.com/api/docs/guides/latency-optimization .

For this update, deploy agent/billing_agent.py and restart the API. The former direct-read helper has been removed. UI, database and Word document are unchanged.

## Earlier conservative optimizations

The application preserves the configured model and its default reasoning behavior. This update removes admin-only chart instructions and chart argument documentation from Customer mode. Customer prompts retain their business, confirmation, pricing, access and memory rules; Admin retains its full chart contract. No billing results or prices are cached.

Product pages can return COUNT(*) OVER() alongside their rows, avoiding a separate count request for nonempty pages. Empty pages still require a matching count before claiming there are no matches. Model-handled requests are instructed to use this query pattern where appropriate.

The measured empty-memory customer system prompt shrank from 54,570 to 49,571 characters (9.2%, excluding tool schemas). This is a prompt-size comparison, not a live response-time benchmark. The configured Azure deployment is unavailable in this workspace, so no 2–3 second guarantee or production accuracy equivalence is claimed.

## Timing logs

Restart `python app.py` to enable these INFO logs in the API terminal:

- `billing_api queue_wait_seconds`: waiting for the application's active-turn lock.
- `billing_model seconds input_tokens output_tokens`: each Azure SDK call and its returned token usage. A turn may have several calls.
- `billing_tool tool seconds`: time spent awaiting each MCP tool.
- `billing_turn total_seconds lock_wait_seconds tool_calls tool_seconds`: agent-turn summary, excluding the earlier API queue and final response persistence.

The logs contain timing, tool names and token counts, not prompts, customer data, keys or model answers. Tool processing/checkpoint overhead after the MCP return is included in the turn total, not tool_seconds. Token usage is reported by the SDK and does not separately expose cached or reasoning tokens.

If model seconds dominates, the bottleneck is outside local SQL execution. If many model calls dominate, review whether the task can safely use one complete read; never combine dependent writes or skip confirmation. If queue time dominates, requests are waiting behind another turn. The current serialization protects shared tool state and order handling.

## Optional reasoning setting

`AZURE_OPENAI_REASONING_EFFORT=` is blank by default. Blank omits the parameter, preserving the model/deployment default. GPT-5.4 documents none as its default and supports none, low, medium, high and xhigh. Azure deployment/API compatibility must be checked before explicitly overriding this value. Lowering a previously higher effort may affect accuracy and should be evaluated on representative questions before use. No effort override is applied automatically.

There is no hard output cap, smaller-model fallback, history truncation, parallel write execution or skipped tool validation in this update. The existing shared clients, warm MCP process and retries-disabled behavior remain.

## Source guidance

- https://developers.openai.com/api/docs/models/gpt-5.4
- https://developers.openai.com/api/docs/guides/latency-optimization
- https://developers.openai.com/api/docs/guides/prompt-caching

OpenAI's guidance identifies output generation and repeated model round trips as major latency contributors. Shortening input alone generally gives a smaller gain. Stable instructions remain before dynamic customer memory to support provider-side prefix reuse when available; cache hits are not guaranteed by this application.
