from fastmcp import FastMCP
from mcp_server.access import scoped_tool
from mcp_server.tools.database import execute_sql_query
from mcp_server.tools.billing_tools import request_order, place_order, apply_promotion, process_refund, download_invoice
from mcp_server.tools.analytics_tools import billing_analysis, billing_anomalies
from mcp_server.tools.operations_tools import manage_customer, manage_draft
from mcp_server.tools.shopping_tools import compare_products, find_cheaper_alternatives

mcp = FastMCP("Billing tools")
TOOLS = (execute_sql_query, request_order, place_order, apply_promotion, process_refund, download_invoice,
         billing_analysis, billing_anomalies, manage_customer, manage_draft,
         compare_products, find_cheaper_alternatives)
for tool in TOOLS:
    mcp.tool(scoped_tool(tool))

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
