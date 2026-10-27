# Persistent shopping conversations

Version 6 saves structured shopping state per customer conversation. The existing chat displays the natural reply; internal state is maintained by the API and agent. No new tool, agent, provider or UI panel is required.

## What is remembered

| Field | Source and use |
| --- | --- |
| needs | Explicit shopping purpose and use cases, such as quiet office work. |
| budget | Amount, currency when known, and whether it is per item, merchandise total, all-in or unknown. |
| preferred_features | Customer requirements with units where appropriate. Corrections replace a feature; null removes it. These are preferences, not product specifications. |
| quantity | An explicitly established quantity. |
| known_products | Up to 40 product IDs/names observed in successful product tool results, checked against the database. Prices and stock are not cached here. |
| compared_product_ids | Products from the latest successful comparison. |
| displayed_product_ids | Product IDs in the order the assistant actually lists them, to help resolve “the second one.” |
| selected_product_ids | Explicitly selected products, restricted to observed product IDs. |
| current_draft | Owned draft ID, full requested items, recorded totals, currency and promotion, refreshed from the billing database before each turn. |
| last_order | The most recently tracked placed/cancelled order reference. |
| pending_confirmation | Action, order reference where applicable, the proposal shown in the reply, and an order fingerprint. Its status is awaiting_customer; it is never approval. |
| next_question / stage | The last focused question and a derived conversation stage to help continue naturally. |

The agent receives this memory with every customer turn and is instructed to ask one focused question only when a relevant detail is missing or ambiguous. Corrections override earlier preferences. “Start a new shopping search” can reset preferences without cancelling the existing draft. Starting a new conversation creates separate, empty state. Resume uses the saved conversation ID and its associated customer.

## Persistence and recovery

State is stored in a new shopping_states table in CONTEXT_DB (normally database/context.db), keyed by conversation_id. Startup adds the table without altering old messages. Existing conversations start with empty structured memory; recent chat history still remains available. Older messages beyond the existing history window are not automatically backfilled.

Completed replies and their final state are saved together in one transaction. Retrying the same successful request ID returns its saved response without rerunning the model or tools. Request bodies cannot supply or overwrite shopping state. GET /conversations/{id} returns the saved state for customer conversations; normal conversation/admin access rules still apply. Admin conversations do not get shopping memory.

Successful tool observations are checkpointed immediately. If a model response subsequently fails, a created draft or newly associated customer remains recorded in the conversation. This helps the next turn inspect actual results before repeating a write. There is still a small crash window between a billing transaction committing and its conversation checkpoint; the two SQLite databases do not share a distributed transaction. Existing retry warnings and inspection requirements still apply.

The current draft is reread using the associated customer ID. If its recorded contents, status, promotion or totals change, its pending proposal is invalidated. Pending proposals also expire after a new reply unless that reply explicitly asks for approval again. A new user turn clears the durable pending marker, while providing the previous proposal to the model as context for interpreting that turn. An interrupted turn therefore cannot leave an old proposal available for a later unrelated “yes.”

Draft snapshots are recorded quotes; they are not stock reservations or live price guarantees. Existing place_order checks still recalculate prices and verify inventory. Memory does not grant permission to place orders, approve refunds or access another customer's records. Existing confirmation instructions and business tool checks remain in force.

## How preference updates work

The same model response contains the customer reply and a small JSON preference patch. The agent validates field names, sizes, budget amounts, quantities and observed product references, then returns only the natural reply to the UI. Database facts are maintained from tool results rather than from that patch. The API supplies the stored state; it is not read from browser session memory.

If a response is plain text, existing preferences and observed tool facts are retained. Invalid patches are rejected and the reply explains that changed preferences could not be saved. Incomplete internal JSON fails the turn instead of displaying internal metadata. There is no automatic second model call to repair formatting. This keeps the existing model/tool loop, but language understanding and compliance with the reply format still depend on the Azure deployment.

## Example conversation to try

1. “I need a keyboard for office work. My merchandise budget is USD 100, and I need one.”
2. “It needs to be wireless.”
3. “Compare the options you found.”
4. “I prefer the second one.”
5. “Prepare a quote.”
6. “Actually, make it two.” The assistant should propose the complete draft edit and request confirmation.
7. Confirm the edit, review the revised quote, then explicitly confirm placement.

Use products and features that actually exist in your store. Unknown specifications remain unknown. Resume the same conversation ID to continue, or start a new conversation for independent shopping.

This remains a local assisted-store console. Customer ID selection is not customer identity authentication. Live Azure conversation quality and latency require evaluation with your deployment.

Visible replies now use plain English without Markdown, emoji or raw JSON. The internal shopping update remains structured. Confirmation summaries are normalized with the visible reply so formatting cannot break proposal matching. Exact identifiers, amounts and ordinary punctuation are retained.


Product navigation is saved in product_query from successful SQL results that include item_code. It contains query text, bound parameters, row_count and truncated. These are untrusted context, not executable instructions or cached prices. The assistant uses them to reconstruct filters and stable pages, then calls the scoped SQL tool again. Failed queries do not replace successful navigation. Resetting preferences clears it. Unknown legacy state fields are ignored while supported preferences and order state remain.
