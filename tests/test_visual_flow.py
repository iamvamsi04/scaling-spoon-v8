import asyncio
import json
from uuid import uuid4
from fastapi.testclient import TestClient
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient
from agent import billing_agent as module
from api.main import app
from config import settings
from context import context_manager as context


def call(name,**arguments):
    return CreateResult(finish_reason='function_calls',content=[FunctionCall(id=str(uuid4()),name=name,arguments=json.dumps(arguments))],
                        usage=RequestUsage(prompt_tokens=0,completion_tokens=0),cached=False)


def test_composed_tool_turn_attaches_chart_then_clears_it(monkeypatch):
    replay = ReplayChatCompletionClient([
        call('execute_sql_query',query='PRAGMA table_info(items)'),
        call('execute_sql_query',query='SELECT name,availablequantity FROM items ORDER BY name',
             chart={'type':'bar','title':'Stock available','x':'name','y':['availablequantity']}),
        'Here is current stock.','Welcome back.'])
    monkeypatch.setattr(module,'create_model_client',lambda:replay)
    async def run():
        agent = module.BillingAgent()
        await agent.start()
        try:
            result = await agent.chat('Chart stock by product',[],None,mode='admin')
            assert len(result['charts']) == 1
            assert sorted(r['availablequantity'] for r in result['charts'][0]['data']) == [15,40,60]
            followup = await agent.chat('Hi',[],'CUST001')
            assert followup['charts'] == []
        finally:
            await agent.close()
    asyncio.run(run())


def test_saved_charts_require_admin_and_are_excluded_from_model_history(monkeypatch):
    cid = context.conversation(mode='admin')
    rid = str(uuid4())
    chart = {'type':'bar','title':'Stock','x':'item','y':['qty'],'currency':None,'data':[{'item':'Keyboard','qty':40}]}
    response = {'response':'Recorded stock','charts':[chart],'invoices':[]}
    context.begin(cid,rid,'Chart stock')
    context.finish(cid,rid,response)
    assert context.history(cid)[-1] == {'role':'assistant','content':'Recorded stock'}
    assert context.history(cid,include_artifacts=True)[-1]['charts'] == [chart]
    class Engine:
        pass
    app.state.engine = Engine()
    monkeypatch.setattr(settings,'ADMIN_API_KEY','test-admin-key-123456789')
    with TestClient(app) as client:
        assert client.get('/conversations/'+cid).status_code == 403
        saved = client.get('/conversations/'+cid,headers={'X-Admin-Key':settings.ADMIN_API_KEY}).json()
        assert saved['messages'][-1]['charts'] == [chart]


def test_every_requested_chart_is_attached_and_delivery_count_is_accurate():
    async def run():
        async with module.RecordingWorkbench() as wb:
            wb.mode = 'admin'
            for i in range(7):
                result = module.decode_result(await wb.call_tool('execute_sql_query',{
                    'query':"SELECT 'A' name,? qty",'parameters':[i+1],
                    'chart':{'type':'bar','title':f'Chart {i}','x':'name','y':['qty']}}))
            assert len(wb.charts) == 7
            assert 'chart' in result and 'chart_error' not in result
            assert result['chart_delivery']['attached_this_call'] == 1
            assert result['chart_delivery']['total_attached_this_turn'] == 7
    asyncio.run(run())


def test_each_chart_reports_invalid_data_and_recovers_on_next_tool_call():
    import copy
    from mcp_server.charts import CHART_TYPES
    from test_chart_types import sample
    from ui.charts import build_chart

    def query_for(columns, rows):
        def literal(value):
            if value is None:
                return 'NULL'
            return str(value) if isinstance(value,(int,float)) else "'"+value.replace("'","''")+"'"
        return ' UNION ALL '.join('SELECT '+','.join(
            literal(value)+' AS "'+columns[i]+'"' for i,value in enumerate(row)) for row in rows)

    async def run():
        async with module.RecordingWorkbench() as wb:
            wb.mode = 'admin'
            for kind in CHART_TYPES:
                columns, rows, spec = copy.deepcopy(sample(kind))
                invalid = copy.deepcopy(rows)
                field = spec['y'][0] if spec['y'] else spec['x']
                invalid[0][columns.index(field)] = None
                wb.charts.clear()
                failed = module.decode_result(await wb.call_tool('execute_sql_query',
                    {'query':query_for(columns,invalid),'chart':spec}))
                assert failed['ok'] and failed['rows'] and failed.get('chart_error'), (kind,failed)
                assert 'chart' not in failed and not wb.charts
                assert failed['chart_delivery']['attached_this_call'] == 0
                assert failed['chart_delivery']['errors']
                recovered = module.decode_result(await wb.call_tool('execute_sql_query',
                    {'query':query_for(columns,rows),'chart':spec}))
                assert recovered['ok'] and not recovered.get('chart_error'), (kind,recovered)
                assert recovered['chart_delivery']['attached_this_call'] == 1
                assert len(wb.charts) == 1 and wb.charts[0]['type'] == kind
                build_chart(wb.charts[0]).to_dict(validate=True)
    asyncio.run(run())
