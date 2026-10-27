import pytest
from mcp_server.tools.database import quote_reserved_aliases, execute_sql_query
from mcp_server.access import scoped_tool
from mcp_server.charts import prepare_chart
from ui.charts import build_chart


def test_alias_repair_preserves_literals_comments_and_quoted_names():
    query='''SELECT 'AS group' AS description, 1 AS /* AS order */ group,
        2 AS "order", 3 AS [group], 4 AS `group` -- AS limit
        FROM items WHERE name=?'''
    assert quote_reserved_aliases(query)==query.replace('*/ group','*/ "group"')
    assert quote_reserved_aliases('SELECT CAST(1 AS INTEGER) AS amount')=='SELECT CAST(1 AS INTEGER) AS amount'


@pytest.mark.parametrize('alias',['group','order','limit'])
def test_reserved_alias_repair_retains_readonly_boundary(alias):
    read=scoped_tool(execute_sql_query)
    result=read(query=f'SELECT name AS {alias},sellingprice FROM items',
        chart={'type':'bar','x':alias,'y':['sellingprice']},execution_mode='admin')
    assert result['ok'] and 'chart' in result and 'query_note' in result
    assert not read(query='DELETE FROM items',chart={'type':'bar','x':'name','y':[]},execution_mode='admin')['ok']
    assert not read(query='SELECT * FROM sqlite_master',chart={'type':'bar','x':'name','y':[]},execution_mode='admin')['ok']


@pytest.mark.parametrize('kind',['bar','grouped_bar','Bar Chart','box plot','heat map','pie chart'])
def test_common_chart_names_and_redundant_group(kind):
    spec={'type':kind,'x':'category','y':'amount'}
    columns=['category','amount','period']
    rows=[['Keyboards',100,'Jan'],['Mice',50,'Jan']]
    if kind in ('bar','grouped_bar','Bar Chart','box plot'):
        spec['group']='category'
    if kind=='heat map':
        spec['group']='period'
    chart=prepare_chart(columns,rows,spec)
    build_chart(chart).to_dict(validate=True)
    if kind in ('bar','grouped_bar','Bar Chart','box plot'):
        assert 'group' not in chart


def test_grouped_bar_retains_distinct_series_and_saved_compatibility():
    chart=prepare_chart(['month','category','amount'],[['Jan','A',10],['Jan','B',20]],
        {'type':'grouped_bar','x':'month','y':['amount'],'group':'category'})
    assert chart['type']=='grouped_bar' and chart['group']=='category'
    assert 'xOffset' in build_chart(chart).to_dict()['encoding']
    assert 'xOffset' in build_chart({**chart,'type':'grouped_bar'}).to_dict()['encoding']


def test_heatmap_identical_dimensions_still_rejected():
    with pytest.raises(ValueError):
        prepare_chart(['category','amount'],[['A',10]],
            {'type':'heatmap','x':'category','y':['amount'],'group':'category'})


@pytest.mark.parametrize('kind',[
    'horizontal_bar','stacked_bar','normalized_bar'])
def test_supported_bar_layouts_retain_distinct_chart_types(kind):
    from mcp_server.charts import CHART_TYPES
    assert kind in CHART_TYPES
    spec={'type':kind,'x':'category','y':['amount']}
    chart=prepare_chart(['category','amount'],[['A',10],['B',20]],spec)
    assert chart['type']==kind
    build_chart(chart).to_dict(validate=True)


def test_duplicate_alias_and_title_do_not_add_another_attachment():
    import asyncio
    from agent.billing_agent import RecordingWorkbench, decode_result
    async def run():
        async with RecordingWorkbench() as wb:
            wb.mode='admin'
            for kind,title in [('pie','Shares'),('pie','Another title')]:
                data=decode_result(await wb.call_tool('execute_sql_query',{
                    'query':"SELECT 'A' AS category,10 AS amount UNION ALL SELECT 'B',20",
                    'chart':{'type':kind,'x':'category','y':['amount'],'title':title}}))
            assert len(wb.charts)==1
            assert data['chart_delivery']['attached_this_call']==0
            assert data['chart_delivery']['total_attached_this_turn']==1
            wb.charts.clear()
            for customer in ['CUST001','CUST002']:
                await wb.call_tool('execute_sql_query',{
                    'query':"SELECT 'Customers' AS category, COUNT(*) AS amount FROM customers WHERE customer_id=?",
                    'parameters':[customer],
                    'chart':{'type':'bar','x':'category','y':['amount'],'title':customer}})
            assert len(wb.charts)==2  # Same plotted values, distinct customer scopes.
    asyncio.run(run())
