# Version 7: audit and customer verification update

Current project: outputs/version_7_latest. These are the files changed by this audit.

## Runtime files

- agent/admin_message.py
- agent/billing_agent.py
- agent/conversation_policy.py
- agent/shopping_policy.py
- agent/store_policy.py
- agent/system_message.py
- agent/task_policy.py
- api/customer_auth.py (new)
- api/main.py
- context/context_manager.py
- context/shopping_state.py
- database/connection.py
- database/invoice.py
- database/migrations.py
- database/service.sql
- generate_seed.py
- mcp_server/access.py
- mcp_server/tools/analytics_tools.py
- mcp_server/tools/billing_tools.py
- mcp_server/tools/database.py
- mcp_server/tools/operations_tools.py
- mcp_server/tools/service_workflows.py
- ui/streamlit_app.py

## Tests

- tests/test_admin.py
- tests/test_api.py
- tests/test_audit_reporting.py (new)
- tests/test_customer_auth.py (new)
- tests/test_edge_cases.py
- tests/test_end_to_end.py
- tests/test_flow_audit.py (new)
- tests/test_refund_audit.py (new)
- tests/test_reply_text.py
- tests/test_seed_demo.py
- tests/test_service_audit.py (new)
- tests/test_shopping_state.py
- tests/test_store_requirements.py
- tests/test_ui.py

This file, CHANGED_FILES.md, was also updated to replace obsolete version 6 instructions.

## Run the update

Restart the API and UI after replacing the runtime files together. API startup applies the schema migration and preserves existing records.

Customers browse as guests and use the sidebar account form to register or sign in. Existing customer IDs cannot be claimed through registration. After verifying an existing account holder, run this from the project folder on the API server:

    python -m api.customer_auth --customer-id CUST001

The command asks for a password twice without displaying it. Repeat for the customer ID being onboarded. There are no default customer passwords. Passwords are never collected by the chat agent.

The twelve billing tools remain. Local store credit remains supported. Payment processing and payment tables are excluded.

Verification: 510 tests passed with three dependency deprecation warnings. API, MCP, database and UI tests run locally; model replies are scripted. Live Azure accuracy and response times are not established by this suite.
