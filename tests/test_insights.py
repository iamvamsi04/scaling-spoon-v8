import json
import sqlite3
import pytest
from config import settings
from database.connection import transaction
from mcp_server.access import scoped_tool
from mcp_server.tools.database import execute_sql_query
from mcp_server.tools.billing_tools import request_order, place_order
from mcp_server.tools.shopping_tools import compare_products
from mcp_server.charts import chart_from_result


def read(query, **kwargs):
    return scoped_tool(execute_sql_query)(query, execution_mode='admin', **kwargs)


def test_generic_attributes_relations_and_current_prices():
    with transaction() as conn:
        conn.execute("INSERT INTO product_details VALUES ('ITEM001','keyboards','Demo',?)",(json.dumps({'connection':'wired','weight':10}),))
        conn.execute("INSERT INTO product_attributes VALUES ('ITEM001','weight',NULL,1.5,'kg')")
        conn.execute("INSERT INTO product_relations VALUES ('ITEM001','ITEM002','accessory','Verified pair')")
        conn.execute("INSERT INTO price_history(item_code,unit_price,effective_from) VALUES ('ITEM001',70,'2000-01-01')")
    result = read("SELECT attribute,text_value,number_value,unit FROM product_attributes WHERE item_code='ITEM001' UNION ALL SELECT j.key,CASE WHEN j.type='text' THEN j.value END,CASE WHEN j.type IN ('integer','real') THEN j.value END,NULL FROM product_details d,json_each(d.features_json) j WHERE d.item_code='ITEM001' AND NOT EXISTS(SELECT 1 FROM product_attributes a WHERE a.item_code=d.item_code AND a.attribute=j.key) ORDER BY attribute")
    assert result['ok'], result
    assert result['rows'] == [['connection','wired',None,None],['weight',None,1.5,'kg']]
    result = read('SELECT CASE WHEN COUNT(h.id)=0 THEN i.sellingprice WHEN COUNT(h.id)=1 THEN MIN(h.unit_price) END AS unit_price FROM items i LEFT JOIN price_history h ON h.item_code=i.item_code AND h.effective_from<=CURRENT_DATE AND (h.effective_to IS NULL OR h.effective_to>=CURRENT_DATE) WHERE i.item_code=? GROUP BY i.item_code',parameters=['ITEM001'])
    assert result['ok'], result
    assert result['rows'] == [[70]]
    assert read("SELECT related_item_code FROM product_relations WHERE item_code='ITEM001' AND relation_type='accessory'")['rows'] == [['ITEM002']]
    assert read("SELECT * FROM product_relations WHERE item_code='ITEM002'")['rows'] == []
    comparison = compare_products(['ITEM001','ITEM002'])
    assert comparison['products'][0]['features']['weight'] == 1.5
    assert comparison['products'][0]['feature_units']['weight'] == 'kg'
    with pytest.raises(sqlite3.IntegrityError), transaction() as conn:
        conn.execute("INSERT INTO product_attributes VALUES ('MISSING','capacity',NULL,50,'GB')")


def test_direct_invoice_queries_and_refunds_remain_scoped():
    from mcp_server.tools.billing_tools import process_refund
    order = request_order('CUST001',[{'item_code':'ITEM001','quantity':2}],'ONE')
    assert place_order(order['order_id'],'CUST001')['ok']
    assert scoped_tool(process_refund)('ONE','CUST001','Partial return','10.00',execution_customer_id='CUST001')['ok']
    assert scoped_tool(process_refund)('ONE','CUST001','Partial return','10.00', action='approve', confirmed=True, return_received=True, execution_mode='admin')['ok']
    query = """SELECT o.charged_price,COALESCE(o.refund_amount,0),
        ROUND(o.charged_price-COALESCE(o.refund_amount,0),2) net_invoice_amount
        FROM orders o LEFT JOIN order_details d ON d.order_id=o.order_id
        WHERE o.order_invoice IS NOT NULL AND COALESCE(d.status,'active')<>'cancelled'"""
    result = read(query)
    assert result['ok'] and result['rows'] == [[159.8,10,149.8]]
    customer = scoped_tool(execute_sql_query)
    assert customer(query,execution_customer_id='CUST002')['rows'] == []
    assert customer(query,execution_customer_id='CUST001')['rows'] == result['rows']
    assert not read('SELECT * FROM payments')['ok']
    assert not read('SELECT * FROM credit_notes')['ok']
    audit = read("SELECT action FROM billing_audit ORDER BY id")
    assert audit['rows'] == [['request_order'],['place_order'],['process_refund'],['process_refund']]
    assert not customer('SELECT * FROM billing_audit',execution_customer_id='CUST001')['ok']


@pytest.mark.parametrize('kind',['bar','line','horizontal_bar','pie','scatter'])
def test_valid_charts_are_materialized_from_executed_rows(kind):
    result = read("SELECT 1 period,10 amount,'USD' currency UNION ALL SELECT 2,20,'USD'",
                  chart={'type':kind,'x':'period','y':['amount'],'title':'Actual query'})
    assert result['ok'] and 'chart' in result, result
    chart = chart_from_result(result)
    assert chart['currency']=='USD' and [r['amount'] for r in chart['data']]==[10,20]
    assert 'data' not in result['chart']  # Avoid duplicating rows in the model tool result.


@pytest.mark.parametrize('query,spec',[
    ("SELECT 'A' x,2 y UNION ALL SELECT 'A',3", {'type':'bar','x':'x','y':['y']}),
    ("SELECT 'A' x,-2 y", {'type':'pie','x':'x','y':['y']}),
    ("SELECT 'A' x,NULL y", {'type':'bar','x':'x','y':['y']}),
    ("SELECT 'A' x,2 y,'USD' currency UNION ALL SELECT 'B',3,'EUR'", {'type':'bar','x':'x','y':['y']}),
    ("SELECT 'A' x,2 y", {'type':'bar','x':'x','y':['invented']}),
    ("SELECT 'A' x,2 y", {'type':'bar','x':'x','y':['y'],'url':'https://invalid.example'}),
])
def test_invalid_charts_preserve_query_results_but_never_attach(query,spec):
    result = read(query,chart=spec)
    assert result['ok'] and 'chart_error' in result and 'chart' not in result


def test_truncation_and_customer_chart_request_rejected(monkeypatch):
    spec = {'type':'bar','x':'x','y':['y']}
    query = "SELECT 'A' x,2 y UNION ALL SELECT 'B',3"
    import mcp_server.charts as charts
    monkeypatch.setattr(charts,'MAX_CHART_ROWS',1)
    result = read(query,chart=spec)
    assert result['truncated'] and 'chart_error' in result
    assert not scoped_tool(execute_sql_query)(query,chart=spec,execution_customer_id='CUST001')['ok']


def test_startup_removes_views_preserves_records_and_stays_idempotent():
    from database.connection import initialize, connect
    from contextlib import closing
    with closing(connect()) as conn:
        before = conn.execute('SELECT * FROM items ORDER BY item_code').fetchall()
        conn.execute('CREATE VIEW legacy_products AS SELECT * FROM items')
        conn.execute('CREATE VIEW legacy_nested AS SELECT * FROM legacy_products')
        conn.commit()
    initialize()
    initialize()
    with closing(connect()) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall() == []
        assert conn.execute('SELECT * FROM items ORDER BY item_code').fetchall() == before
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert not read('SELECT * FROM legacy_products')['ok']
