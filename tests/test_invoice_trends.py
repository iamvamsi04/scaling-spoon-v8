import asyncio
import json
from decimal import Decimal
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from autogen_ext.models.replay import ReplayChatCompletionClient
from agent import billing_agent as agent_module
from api.main import app
from config import settings
from database.connection import transaction
from mcp_server.charts import prepare_chart, chart_from_result
from mcp_server.tools.analytics_tools import billing_analysis
from mcp_server.access import scoped_tool
from ui.charts import build_chart
from test_agent_lifecycle import tool_call


@pytest.fixture
def invoice_records():
    rows=[('A','2026-10-03',100,10,None,'active',True,'CUST001'),
          ('B','2026-10-19',150,20,'USD','active',True,'CUST002'),
          ('C','2026-10-20',50,0,' usd ','active',True,'CUST001'),
          ('D','2026-09-30',40,5,'USD','active',True,'CUST001'),
          ('E','2026-10-03',70,0,'EUR','active',True,'CUST001'),
          ('F','2026-10-03',900,0,'USD','cancelled',True,'CUST001'),
          ('G','2026-10-03',800,0,'USD','active',False,'CUST001')]
    with transaction() as conn:
        for oid,day,total,refund,currency,status,issued,customer in rows:
            conn.execute('INSERT INTO orders(order_id,order_date,customer_id,order_invoice,subtotal,charged_price,refund_amount) VALUES(?,?,?,?,?,?,?)',
                         (oid,day,customer,'INV-'+oid if issued else None,total,total,refund or None))
            if currency is not None:
                conn.execute('INSERT INTO order_details(order_id,currency,status) VALUES(?,?,?)',(oid,currency,status))


@pytest.mark.parametrize('period',['day','week','month','quarter','year'])
@pytest.mark.parametrize('metric',['order_total','net_billed','refunds'])
def test_real_invoice_grain_currency_and_refund_semantics(invoice_records,period,metric):
    report=billing_analysis(trend_period=period,trend_metric=metric)
    assert report['ok'] and report['invoice_count']==5
    by_currency={r['rows'][0][2]:r for r in report['chart_results']}
    assert set(by_currency)=={'USD','EUR'}
    expected={'order_total':Decimal('340'),'net_billed':Decimal('305'),'refunds':Decimal('35')}
    assert sum(Decimal(row[1]) for row in by_currency['USD']['rows'])==expected[metric]
    for result in report['chart_results']:
        assert len({row[0] for row in result['rows']})==len(result['rows'])
        build_chart(chart_from_result(result)).to_dict(validate=True)
    if period=='month' and metric=='order_total':
        assert by_currency['USD']['rows']==[['2026-09-01','40.00','USD'],['2026-10-01','300.00','USD']]


def test_report_filters_empty_and_customer_access(invoice_records):
    report=billing_analysis(customer_id='CUST001',date_from='2026-10-01',date_to='2026-10-20',trend_period='month')
    assert report['invoice_count']==3
    assert sum(Decimal(row[1]) for r in report['chart_results'] for row in r['rows'] if row[2]=='USD')==150
    assert billing_analysis(date_from='2027-01-01',trend_period='month')['chart_results']==[]
    assert not scoped_tool(billing_analysis)(trend_period='month',execution_mode='customer')['ok']


def test_month_coordinates_normalize_without_merging_duplicates():
    spec={'type':'line','x':'month','y':['total'],'x_type':'temporal'}
    chart=prepare_chart(['month','total'],[['2026-04',12],['2026-10',18]],spec)
    assert chart['data'][0]['month']=='2026-04-01T00:00:00'
    build_chart(chart).to_dict(validate=True)
    with pytest.raises(ValueError,match='duplicate'):
        prepare_chart(['month','total'],[['2026-10',12],['2026-10-01',18]],spec)
    with pytest.raises(ValueError):
        prepare_chart(['month','total'],[['2026-13',12]],spec)
    with pytest.raises(ValueError):
        prepare_chart(['start','end','task'],[['2026-04','2026-05','Delivery']],
                      {'type':'timeline','x':'start','end':'end','group':'task','y':[]})


def test_all_usd_legacy_and_current_invoices_form_one_month_group(invoice_records):
    with transaction() as conn:
        conn.execute("UPDATE order_details SET currency='USD'")
        conn.execute("UPDATE billing_config SET value='USD' WHERE key='currency'")
    report=billing_analysis(trend_period='month')
    assert report['ok'] and len(report['chart_results'])==1
    assert report['chart_results'][0]['rows']==[
        ['2026-09-01','40.00','USD'],['2026-10-01','370.00','USD']]


def test_monthly_sql_chart_with_all_usd_and_legacy_details(invoice_records):
    from mcp_server.tools.database import execute_sql_query
    with transaction() as conn:
        conn.execute("UPDATE order_details SET currency='USD'")
    query="""WITH invoice_values AS (
        SELECT strftime('%Y-%m',o.order_date) AS month,
          COALESCE(d.currency,(SELECT value FROM billing_config WHERE key='currency')) AS resolved_currency,
          o.charged_price
        FROM orders o LEFT JOIN order_details d ON d.order_id=o.order_id
        WHERE o.order_invoice IS NOT NULL AND COALESCE(d.status,'active')<>'cancelled')
        SELECT month,resolved_currency AS currency,SUM(charged_price) AS total
        FROM invoice_values GROUP BY month,resolved_currency ORDER BY month"""
    data=scoped_tool(execute_sql_query)(query=query,execution_mode='admin',
        chart={'type':'line','x':'month','y':['total'],'x_type':'temporal'})
    assert data['ok'] and 'chart_error' not in data
    chart=chart_from_result(data)
    assert [(r['month'],r['total']) for r in chart['data']]==[
        ('2026-09-01T00:00:00',40),('2026-10-01T00:00:00',370)]


@pytest.mark.parametrize('kind',['line','bar','grouped_bar','horizontal_bar'])
def test_trend_renders_in_streamlit(invoice_records,kind,tmp_path):
    from streamlit.testing.v1 import AppTest
    charts=[chart_from_result(r) for r in billing_analysis(trend_period='month',chart_type=kind)['chart_results']]
    script=tmp_path/'render_trend.py'
    script.write_text('from ui.charts import render_chart\nfor chart in '+repr(charts)+':\n    render_chart(chart)\n',encoding='utf-8')
    app=AppTest.from_file(str(script)).run(timeout=20)
    assert not app.exception and not app.warning
    assert len(app.get('vega_lite_chart'))==2


@pytest.mark.parametrize('kind',['area','step_line'])
def test_removed_trend_styles_fail_clearly_without_substituting_another_chart(invoice_records,kind):
    result = billing_analysis(trend_period='month',chart_type=kind)
    assert result['ok'] is False
    assert 'line' in result['error'] and not result.get('chart_results')


def test_monthly_request_attaches_persists_and_renders_real_tool_results(invoice_records,monkeypatch):
    monkeypatch.setattr(settings,'azure_configuration_error',lambda:None)
    monkeypatch.setattr(settings,'API_KEY','')
    monkeypatch.setattr(settings,'ADMIN_API_KEY','trend-test-admin-key-123')
    replay=ReplayChatCompletionClient([tool_call('billing_analysis',trend_period='month',trend_metric='order_total'),
                                     'Monthly order totals are shown separately for USD and EUR.'])
    monkeypatch.setattr(agent_module,'create_model_client',lambda:replay)
    app.state.engine=agent_module.BillingAgent()
    headers={'X-Admin-Key':settings.ADMIN_API_KEY}
    with TestClient(app) as client:
        cid=client.post('/conversations',json={'mode':'admin'},headers=headers).json()['conversation_id']
        reply=client.post('/chat',json={'conversation_id':cid,'request_id':str(uuid4()),
            'message':'Show the monthly revenue trend based on order totals as a chart'},headers=headers)
        assert reply.status_code==200,reply.text
        charts=reply.json()['charts']
        assert len(charts)==2 and len(replay.create_calls)==2
        assert app.state.engine._workbench.tool_calls==1
        for chart in charts:
            build_chart(chart).to_dict(validate=True)
        saved=client.get('/conversations/'+cid,headers=headers).json()
        assert saved['messages'][-1]['charts']==charts
        usd=next(c for c in charts if c['currency']=='USD')
        assert sum(r['order_total'] for r in usd['data'])==340
        assert client.get('/conversations/'+cid).status_code==403


def test_all_requested_analytics_artifacts_are_attached_without_a_six_chart_cap(invoice_records):
    async def run():
        async with agent_module.RecordingWorkbench() as wb:
            wb.mode='admin'
            for period in ['day','week','month','quarter']:
                data=agent_module.decode_result(await wb.call_tool('billing_analysis',{'trend_period':period}))
            assert len(wb.charts)==8
            assert all('chart' in r and 'chart_error' not in r for r in data['chart_results'])
            assert data['chart_delivery']['total_attached_this_turn']==8
    asyncio.run(run())
