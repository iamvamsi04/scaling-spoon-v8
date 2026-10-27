import asyncio
import json
import pytest
from autogen_ext.models.replay import ReplayChatCompletionClient
from agent import billing_agent as agent_module
from mcp_server.access import scoped_tool
from mcp_server.tools.database import execute_sql_query, schema_hint
from mcp_server.charts import prepare_chart, SERIES_TYPES
from ui.charts import build_chart
from test_agent_lifecycle import tool_call


def test_schema_has_column_owners_and_foreign_keys_without_customer_private_fields():
    admin = schema_hint('admin')
    customer = schema_hint('customer')
    assert 'product_details(item_code, category' in admin
    assert 'product_details.item_code = items.item_code' in admin
    assert 'baseprice' in admin and 'billing_audit(' in admin
    assert 'baseprice' not in customer and 'billing_audit(' not in customer
    items_line = next(line for line in admin.splitlines() if line.startswith('items('))
    assert 'category' not in items_line


def test_wrong_table_column_returns_usable_schema_then_join_succeeds():
    read = scoped_tool(execute_sql_query)
    bad = read(query='SELECT category, SUM(availablequantity) AS stock FROM items GROUP BY category', execution_mode='admin')
    assert not bad['ok'] and 'product_details(' in bad['schema_hint']
    good = read(query='SELECT COALESCE(d.category, \'Uncategorised\') AS category, SUM(i.availablequantity) AS stock '
                'FROM items i LEFT JOIN product_details d ON d.item_code=i.item_code GROUP BY d.category',
                chart={'type':'bar','x':'category','y':['stock']}, execution_mode='admin')
    assert good['ok'] and 'chart' in good
    assert sum(row[1] for row in good['rows']) == 115
    private = read(query='SELECT baseprice FROM items', execution_mode='customer')
    assert not private['ok'] and 'baseprice' not in private.get('schema_hint','')


@pytest.mark.parametrize('kind', sorted(SERIES_TYPES))
def test_long_series_chart_preserves_values_and_renders(kind):
    rows = [['Jan','A',10],['Jan','B',0],['Feb','A',4],['Feb','B',6]]
    chart = prepare_chart(['month','category','amount'], rows,
        {'type':kind,'x':'month','group':'category','y':['amount']})
    assert [r['amount'] for r in chart['data']] == [10,0,4,6]
    spec = build_chart(chart).to_dict(validate=True)
    assert spec['encoding']['color']['title'] == 'category'
    if kind in ('bar','grouped_bar'):
        assert 'xOffset' in spec['encoding']


def test_long_series_rejects_duplicate_pairs_and_zero_totals():
    spec = {'type':'normalized_bar','x':'month','group':'category','y':['amount']}
    with pytest.raises(ValueError, match='duplicate'):
        prepare_chart(['month','category','amount'], [['Jan','A',1],['Jan','A',2]], spec)
    with pytest.raises(ValueError, match='positive'):
        prepare_chart(['month','category','amount'], [['Jan','A',0],['Jan','B',0]], spec)


def test_single_point_line_has_visible_marker():
    chart = prepare_chart(['date','total'], [['2026-01-01',10]],
                          {'type':'line','x':'date','y':['total'],'x_type':'temporal'})
    assert build_chart(chart).to_dict()['mark']['point'] is True


def test_render_failure_is_caught(monkeypatch):
    import ui.charts as ui
    warnings = []
    monkeypatch.setattr(ui.st,'subheader',lambda *a,**k:None)
    monkeypatch.setattr(ui.st,'warning',warnings.append)
    def fail(*args, **kwargs):
        raise RuntimeError('renderer failed')
    monkeypatch.setattr(ui.st,'altair_chart',fail)
    chart = prepare_chart(['x','y'], [['A',1]], {'type':'bar','x':'x','y':['y']})
    assert ui.render_chart(chart) is False and len(warnings) == 1


def test_streamlit_still_validates_corrupt_chart_schema(monkeypatch,tmp_path):
    import altair as alt
    import ui.charts as ui
    from streamlit.testing.v1 import AppTest
    invalid=alt.Chart(alt.Data(values=[{'x':'A','y':1}])).mark_bar(opacity=0.5).encode(x='x:O',y='y:Q')
    # Simulate a corrupted chart object after Altair's constructor validation.
    invalid.mark._kwds['opacity']='invalid'
    monkeypatch.setattr(ui,'build_chart',lambda chart:invalid)
    script=tmp_path/'invalid_chart_schema.py'
    script.write_text("from ui.charts import render_chart\nrender_chart({'title':'Sales'})",encoding='utf-8')
    app=AppTest.from_file(str(script),default_timeout=30).run()
    assert not app.exception and len(app.warning)==1
    assert not app.get('vega_lite_chart')


def test_schema_is_available_before_model_sql_and_chart_reaches_agent(monkeypatch):
    seen = []
    class CapturingReplay(ReplayChatCompletionClient):
        async def create(self, messages, **kwargs):
            seen.append(messages)
            return await super().create(messages, **kwargs)
    sql = ('SELECT COALESCE(d.category, \'Uncategorised\') AS category, SUM(i.availablequantity) AS stock '
           'FROM items i LEFT JOIN product_details d ON d.item_code=i.item_code GROUP BY d.category')
    replay = CapturingReplay([tool_call('execute_sql_query',query=sql,
        chart={'type':'bar','x':'category','y':['stock']}), 'Here is stock by category.'])
    monkeypatch.setattr(agent_module,'create_model_client',lambda:replay)
    async def run():
        engine = agent_module.BillingAgent()
        result = await engine.chat('Chart stock by product category',[],None,mode='admin')
        assert len(result['charts']) == 1
        assert sum(r['stock'] for r in result['charts'][0]['data']) == 115
        assert 'product_details(item_code, category' in seen[0][0].content
        assert len(replay.create_calls) == 2  # No separate schema-discovery model round.
    asyncio.run(run())
