import asyncio
import httpx
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest
from api.main import app
from config import settings
from database.connection import transaction
from agent.billing_agent import RecordingWorkbench, decode_result
from test_end_to_end import setup_engine, call


def test_category_revenue_chart_reaches_actual_chat_ui(monkeypatch):
    with transaction() as conn:
        conn.execute("INSERT INTO orders(order_id,customer_id,order_invoice,subtotal,charged_price) VALUES('BAR-1','CUST001','INV-BAR-1',189.30,189.30)")
        conn.execute("INSERT INTO order_details(order_id,currency) VALUES('BAR-1','USD')")
        conn.executemany('INSERT INTO invoice_lines(order_id,item_code,qty_billed,unit_price_billed,line_total) VALUES(?,?,?,?,?)',[
            ('BAR-1','ITEM001',2,79.90,159.80),('BAR-1','ITEM002',1,29.50,29.50)])
        conn.executemany('INSERT INTO product_details(item_code,category) VALUES(?,?)',[
            ('ITEM001','Keyboards'),('ITEM002','Mice')])
    query="""SELECT COALESCE(d.category,'Uncategorised') AS category, SUM(l.line_total) AS billed_revenue,
        d.category as group, TRIM(UPPER(COALESCE(od.currency,(SELECT value FROM billing_config WHERE key='currency'),'USD'))) AS currency
        FROM invoice_lines l JOIN orders o ON o.order_id = l.order_id
        JOIN order_details od ON od.order_id = o.order_id
        LEFT JOIN items i ON i.item_code = l.item_code
        LEFT JOIN product_details d ON d.item_code = l.item_code
        WHERE o.order_invoice IS NOT NULL AND COALESCE(od.status, 'active') <> 'cancelled'
        GROUP BY COALESCE(d.category,'Uncategorised'), currency ORDER BY billed_revenue DESC"""
    setup_engine(monkeypatch,[call('execute_sql_query',query=query,
        chart={'type':'bar','x':'category','y':['billed_revenue'],'group':'category','title':'Billed revenue by category'}),
        'The bar chart shows billed merchandise revenue by category.'])
    monkeypatch.setattr(settings,'ADMIN_API_KEY','chart-delivery-test-key')
    original=httpx.Client.request
    original_stream=httpx.Client.stream
    with TestClient(app) as client:
        responses=[]
        def bridge(self,method,url,**kwargs):
            if self is client:
                return original(self,method,url,**kwargs)
            response=client.request(method,url,**kwargs)
            if url.endswith('/chat'):
                responses.append(response.json())
            return response
        monkeypatch.setattr(httpx.Client,'request',bridge)
        def stream_bridge(self,method,url,**kwargs):
            if self is client:
                return original_stream(self,method,url,**kwargs)
            return client.stream(method,url,**kwargs)
        monkeypatch.setattr(httpx.Client,'stream',stream_bridge)
        ui=AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'),default_timeout=60).run()
        ui.radio[0].set_value('Admin').run()
        next(x for x in ui.text_input if x.label=='Admin key').set_value(settings.ADMIN_API_KEY)
        next(x for x in ui.button if x.label=='Unlock admin mode').click().run()
        ui.chat_input[0].set_value('Show billed revenue by product category as a bar chart.').run()
        assert not ui.exception and not ui.warning
        saved=client.get('/conversations/'+ui.session_state['conversation_id'],
            headers={'X-Admin-Key':settings.ADMIN_API_KEY}).json()['messages'][-1]
        assert len(saved['charts'])==1
        assert saved['charts'][0]['data']==[
            {'category':'Keyboards','billed_revenue':159.8},{'category':'Mice','billed_revenue':29.5}]
        assert len(ui.get('vega_lite_chart'))==1
        assert len(ui.dataframe)==1
        ui.run()
        assert len(ui.get('vega_lite_chart'))==1


def test_attachment_receipt_distinguishes_data_failure_and_visual():
    async def run():
        async with RecordingWorkbench() as wb:
            wb.mode='admin'
            args={'query':"SELECT 'Keyboards' AS category, 159.80 AS revenue"}
            data=decode_result(await wb.call_tool('execute_sql_query',args))
            assert data['chart_delivery']['total_attached_this_turn']==0
            data=decode_result(await wb.call_tool('execute_sql_query',{**args,
                'chart':{'type':'bar','x':'category','y':['missing']}}))
            assert data['chart_delivery']['errors'] and not wb.charts
            data=decode_result(await wb.call_tool('execute_sql_query',{**args,
                'chart':{'type':'bar','x':'category','y':['revenue']}}))
            assert data['chart_delivery']=={'attached_this_call':1,'total_attached_this_turn':1,'errors':[]}
    asyncio.run(run())
