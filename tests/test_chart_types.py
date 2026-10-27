import asyncio
import json
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest
from config import settings
from mcp_server.charts import CHART_TYPES, prepare_chart, chart_from_result, validate_saved_chart
from ui.charts import build_chart
from agent.billing_agent import RecordingWorkbench, decode_result


def sample(kind):
    columns,rows=['category','amount','other'],[['A',10,3],['B',5,2]]
    spec={'type':kind,'title':kind,'x':'category','y':['amount']}
    if kind in ('grouped_bar','stacked_bar','normalized_bar','combo'):
        spec['y']=['amount','other']
    if kind=='range':
        spec['y']=['other','amount']
    if kind in ('scatter','bubble','histogram'):
        columns,rows=['distance','amount','size','category'],[[1,10,4,'A'],[2,5,2,'B']]
        spec.update(x='distance',y=[] if kind=='histogram' else ['amount'])
        if kind=='bubble':
            spec.update(size='size',group='category')
    if kind=='histogram':
        spec['bins']=5
    if kind=='boxplot':
        rows=[['A',10,3],['A',12,4],['A',5,2],['B',8,5]]
    if kind=='heatmap':
        columns,rows=['category','region','amount'],[['A','North',10],['A','South',5]]
        spec['group']='region'
    if kind=='timeline':
        columns,rows=['start','finish','task'],[['2026-01-01','2026-01-03','Order 1'],['2026-01-02','2026-01-05','Order 2']]
        spec.update(x='start',y=[],end='finish',group='task')
    return columns,rows,spec


@pytest.mark.parametrize('kind',CHART_TYPES)
def test_every_chart_validates_round_trips_and_renders(kind,tmp_path):
    columns,rows,spec=sample(kind)
    chart=prepare_chart(columns,rows,spec)
    result={'columns':columns,'rows':rows,'chart':{k:v for k,v in chart.items() if k!='data'}}
    assert chart_from_result(result)==chart
    grammar=build_chart(chart).to_dict(validate=True)
    assert '$schema' in grammar
    script=tmp_path/'chart_ui.py'
    script.write_text('import sys\nsys.path.insert(0,'+repr(str(settings.ROOT))+')\nfrom ui.charts import render_chart\nrender_chart('+repr(chart)+')\n',encoding='utf-8')
    ui=AppTest.from_file(str(script),default_timeout=30).run()
    assert not ui.exception and not ui.warning
    assert len(ui.get('vega_lite_chart'))==1
    assert len(ui.dataframe)==1


def test_all_chart_types_through_real_mcp_and_attachment_limits():
    async def run():
        async with RecordingWorkbench() as wb:
            wb.mode='admin'
            for kind in CHART_TYPES:
                columns,rows,spec=sample(kind)
                def literal(value):
                    return str(value) if isinstance(value,(int,float)) else "'"+value.replace("'","''")+"'"
                query=' UNION ALL '.join('SELECT '+','.join(literal(v)+' AS "'+columns[i]+'"' for i,v in enumerate(row)) for row in rows)
                wb.charts.clear()
                result=decode_result(await wb.call_tool('execute_sql_query',{'query':query,'chart':spec}))
                assert result.get('ok') and 'chart' in result,(kind,result)
                assert len(wb.charts)==1 and wb.charts[0]['type']==kind
                build_chart(wb.charts[0]).to_dict(validate=True)
    asyncio.run(run())


@pytest.mark.parametrize('kind',CHART_TYPES)
def test_every_chart_rejects_empty_and_truncated_data(kind):
    columns,rows,spec=sample(kind)
    with pytest.raises(ValueError):
        prepare_chart(columns,[],spec)
    with pytest.raises(ValueError):
        prepare_chart(columns,rows,spec,truncated=True)


@pytest.mark.parametrize('kind',CHART_TYPES)
def test_every_chart_rejects_mixed_currency(kind):
    columns,rows,spec=sample(kind)
    with pytest.raises(ValueError,match='currency'):
        prepare_chart(columns+['currency'],[row+['USD' if i==0 else 'EUR'] for i,row in enumerate(rows)],spec)


@pytest.mark.parametrize('kind,change',[
    ('bubble',{'size':'missing'}),('histogram',{'bins':True}),('histogram',{'bins':51}),
    ('heatmap',{'group':'amount'}),('timeline',{'end':'missing'}),
    ('scatter',{'x_type':'temporal'}),('pie',{'size':'other'}),('combo',{'y':['amount']}),
    ('bar',{'url':'https://example.invalid'}),('bar',{'type':'unsupported'}),
])
def test_bad_specs_fail_clearly(kind,change):
    columns,rows,spec=sample(kind)
    with pytest.raises(ValueError):
        prepare_chart(columns,rows,{**spec,**change})


@pytest.mark.parametrize('kind,rows',[
    ('pie',[['A',-1,0]]),('pie',[['A',0,0]]),('normalized_bar',[['A',0,0]]),
    ('range',[['A',1,5]]),('stacked_bar',[['A',-1,0]]),
    ('line',[['A',float('inf'),0]]),('bar',[['A',1,0],['A',2,0]]),
])
def test_unsuitable_measure_values_are_rejected(kind,rows):
    columns,_,spec=sample(kind)
    with pytest.raises(ValueError):
        prepare_chart(columns,rows,spec)


def test_timeline_rejects_reversed_or_mixed_zone_intervals():
    columns,_,spec=sample('timeline')
    for rows in [[['2026-01-03','2026-01-01','Task']], [['2026-01-01','2026-01-02Z','Task']], [['bad date','2026-01-02','Task']]]:
        with pytest.raises(ValueError):
            prepare_chart(columns,rows,spec)


def test_ordered_trends_and_numeric_coordinates_preserve_meaning():
    chart=prepare_chart(['month','value'],[['2026-02-01',3],['2026-10-01',4]],{'type':'line','x':'month','y':['value'],'x_type':'temporal'})
    assert chart['data'][0]['month'].startswith('2026-02')
    assert build_chart(chart).to_dict()['encoding']['x']['type']=='temporal'
    chart=prepare_chart(['x','y'],[[2,3],[10,4]],{'type':'line','x':'x','y':['y'],'x_type':'numeric'})
    assert chart['data'][1]['x']==10


def test_fifteen_types_and_range_bounds_render():
    assert set(CHART_TYPES)=={'bar','grouped_bar','horizontal_bar','stacked_bar','normalized_bar',
        'line','pie','scatter','bubble','histogram','boxplot','heatmap','range','combo','timeline'}
    columns,rows,spec=sample('range')
    chart=prepare_chart(columns,rows,spec)
    before=json.dumps(chart)
    grammar=build_chart(chart).to_dict(validate=True)
    assert grammar['layer'][0]['encoding']['y']['field']=='v0'
    assert grammar['layer'][0]['encoding']['y2']['field']=='v1'
    assert json.dumps(chart)==before


@pytest.mark.parametrize('kind',['bubble','combo','timeline'])
def test_multidimensional_charts_keep_the_requested_type_and_original_observations(kind):
    columns,rows,spec=sample(kind)
    chart=prepare_chart(columns,rows,spec)
    assert chart['type']==kind
    before=json.dumps(chart)
    build_chart(chart).to_dict(validate=True)
    assert json.dumps(chart)==before


def test_distribution_queries_include_full_demo_sized_data_and_reject_truncation():
    from mcp_server.access import scoped_tool
    from mcp_server.tools.database import execute_sql_query
    read=scoped_tool(execute_sql_query)
    spec={'type':'histogram','x':'amount','y':[],'bins':10}
    for limit in (500,1001):
        query=f'WITH RECURSIVE n(amount) AS (SELECT 1 UNION ALL SELECT amount+1 FROM n WHERE amount<{limit}) SELECT amount FROM n'
        result=read(query,chart=spec,execution_mode='admin')
        assert result['ok']
        if limit==500:
            assert result['row_count']==500 and 'chart' in result
            assert len(chart_from_result(result)['data'])==500
        else:
            assert result['truncated'] and 'chart_error' in result and 'chart' not in result


def test_corrupt_saved_chart_fails_gracefully_in_ui(tmp_path):
    script=tmp_path/'invalid_chart.py'
    script.write_text('import sys\nsys.path.insert(0,'+repr(str(settings.ROOT))+')\nfrom ui.charts import render_chart\nrender_chart({"type":"unsupported","x":"x","y":["y"]})',encoding='utf-8')
    ui=AppTest.from_file(str(script),default_timeout=30).run()
    assert not ui.exception and ui.warning
    assert not ui.get('vega_lite_chart')


def test_invalid_bubbles_heatmap_cells_and_percentage_overflow():
    for kind,rows in [
        ('bubble',[[1,2,-1,'A']]),('bubble',[[1,2,0,'A']]),
        ('heatmap',[['A','North',1],['A','North',2]]),
        ('normalized_bar',[['A',1e308,1e308]]),
    ]:
        columns,_,spec=sample(kind)
        with pytest.raises(ValueError):
            prepare_chart(columns,rows,spec)


def test_count_and_money_labels_are_distinct_and_currency_is_never_an_axis():
    chart = prepare_chart(['period','charged_price','invoice_count','currency'],
        [['2026-01',100,2,' usd '],['2026-02',150,3,'USD']],
        {'type':'combo','x':'period','y':['charged_price','invoice_count']})
    spec = build_chart(chart).to_dict(validate=True)
    assert spec['layer'][0]['encoding']['y']['title'] == 'Charged price (USD)'
    assert spec['layer'][1]['encoding']['y']['title'] == 'Invoice count'
    assert spec['layer'][0]['encoding']['tooltip'][-1]['title'] == 'Invoice count'
    with pytest.raises(ValueError, match='Currency is metadata'):
        prepare_chart(['label','value','currency'], [['USD',100,'USD']],
                      {'type':'bar','x':'label','y':['value']})
    with pytest.raises(ValueError, match='Currency is metadata'):
        prepare_chart(['category','money_group','value','currency'], [['A','USD',100,'USD']],
                      {'type':'heatmap','x':'category','group':'money_group','y':['value']})


def test_monthly_period_axes_use_month_labels_instead_of_intermediate_days():
    chart = prepare_chart(['month','revenue','currency'],
        [['2026-01',100,'USD'],['2026-02',120,'USD'],['2026-03',90,'USD']],
        {'type':'line','x':'month','y':['revenue'],'x_type':'temporal'})
    spec = build_chart(chart).to_dict(validate=True)
    assert spec['encoding']['x']['axis']['format'] == '%b %Y'
    assert spec['encoding']['x']['axis']['tickCount'] == {'interval':'month','step':1}


def test_zero_duration_intervals_and_single_point_trends_stay_visible():
    timeline = prepare_chart(['start','finish','order'], [['2026-01-01','2026-01-01','ORDER-1']],
        {'type':'timeline','x':'start','y':[],'end':'finish','group':'order'})
    assert any(layer['mark']['type'] == 'circle' for layer in build_chart(timeline).to_dict(validate=True)['layer'])
    line = prepare_chart(['year','amount'], [['2026',10]],
        {'type':'line','x':'year','y':['amount'],'x_type':'temporal'})
    grammar = build_chart(line).to_dict(validate=True)
    assert grammar['mark']['type'] == 'line' and grammar['mark']['point'] is True


REMOVED_CHARTS = ('donut','step_line','area','stacked_area','normalized_area',
                  'candlestick','waterfall','pareto','funnel','treemap')


def test_removed_chart_requests_preserve_sql_results_and_recover_through_real_mcp():
    async def run():
        async with RecordingWorkbench() as wb:
            wb.mode = 'admin'
            query = "SELECT 'Keyboards' AS category,100 AS amount UNION ALL SELECT 'Mice',50"
            for kind in REMOVED_CHARTS:
                result = decode_result(await wb.call_tool('execute_sql_query',{
                    'query':query,'chart':{'type':kind,'x':'category','y':['amount']}}))
                assert result['ok'] and result['columns'] == ['category','amount']
                assert result['rows'] == [['Keyboards',100],['Mice',50]]
                assert 'supported' in result['chart_error'].lower()
                assert 'chart' not in result and not wb.charts
                assert result['chart_delivery']['attached_this_call'] == 0
                assert result['chart_delivery']['total_attached_this_turn'] == 0
                assert result['chart_delivery']['errors'] == [result['chart_error']]
            result = decode_result(await wb.call_tool('execute_sql_query',{
                'query':query,'chart':{'type':'bar','x':'category','y':['amount']}}))
            assert result['ok'] and 'chart_error' not in result
            assert len(wb.charts) == 1 and wb.charts[0]['type'] == 'bar'
            assert result['chart_delivery']['attached_this_call'] == 1
            build_chart(wb.charts[0]).to_dict(validate=True)
    asyncio.run(run())


@pytest.mark.parametrize('kind', REMOVED_CHARTS)
@pytest.mark.parametrize('spelling', ['canonical','human','chart_suffix'])
def test_removed_chart_names_cannot_be_generated_or_restored(kind, spelling):
    name = kind if spelling == 'canonical' else kind.replace('_',' ').title()
    if spelling == 'chart_suffix':
        name += ' Chart'
    columns, rows, spec = sample('bar')
    spec['type'] = name
    with pytest.raises(ValueError, match='chart type|Chart type|supported'):
        prepare_chart(columns, rows, spec)
    saved = prepare_chart(columns, rows, {**spec,'type':'bar'})
    saved['type'] = name
    with pytest.raises(ValueError):
        validate_saved_chart(saved)
    with pytest.raises(ValueError):
        build_chart(saved)


@pytest.mark.parametrize('name', ['step','candle stick','water fall','tree map'])
def test_old_aliases_do_not_restore_removed_chart_families(name):
    columns, rows, spec = sample('bar')
    with pytest.raises(ValueError):
        prepare_chart(columns, rows, {**spec,'type':name})
    saved = {**prepare_chart(columns, rows, spec),'type':name}
    with pytest.raises(ValueError):
        validate_saved_chart(saved)
    with pytest.raises(ValueError):
        build_chart(saved)


def test_removed_saved_charts_show_a_safe_warning_without_breaking_the_ui(tmp_path):
    script = tmp_path/'removed_saved_charts.py'
    charts = [{**prepare_chart(*sample('bar')), 'type':kind} for kind in REMOVED_CHARTS]
    script.write_text('import sys\nsys.path.insert(0,'+repr(str(settings.ROOT))+')\n'
        'from ui.charts import render_chart\n'
        'for chart in '+repr(charts)+':\n    render_chart(chart)\n',encoding='utf-8')
    ui = AppTest.from_file(str(script),default_timeout=30).run()
    assert not ui.exception
    assert len(ui.warning) == len(REMOVED_CHARTS)
    assert not ui.get('vega_lite_chart')
