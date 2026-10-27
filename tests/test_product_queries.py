from datetime import date
import inspect
from contextlib import closing

from config import settings
from database.connection import connect
from context.shopping_state import ShoppingTracker
from mcp_server.access import scoped_tool
from mcp_server.tools.database import execute_sql_query
import generate_seed


def read(query, parameters=None):
    result = scoped_tool(execute_sql_query)(query=query, parameters=parameters, execution_mode='customer')
    assert result['ok'], result
    return result


def test_all_400_products_are_reachable_through_sql(tmp_path, monkeypatch):
    path = tmp_path / 'products.db'
    generate_seed.generate(path, 42, date.today())
    monkeypatch.setattr(settings, 'BILLING_DB', path)
    assert read('SELECT COUNT(*) FROM items')['rows'] == [[400]]
    categories = read('''SELECT COALESCE(d.category,'Uncategorised') AS category, COUNT(*) AS products
        FROM items i LEFT JOIN product_details d ON d.item_code=i.item_code
        GROUP BY COALESCE(d.category,'Uncategorised') ORDER BY category''')['rows']
    assert len(categories) == 20 and sum(row[1] for row in categories) == 400
    seen = []
    for offset in range(0,400,10):
        result = read('SELECT item_code,name,availablequantity FROM items ORDER BY name,item_code LIMIT ? OFFSET ?', [10,offset])
        assert len(result['rows']) == 10
        seen.extend(row[0] for row in result['rows'])
    assert len(set(seen)) == 400
    assert read('SELECT item_code FROM items ORDER BY name,item_code LIMIT ? OFFSET ?', [10,400])['rows'] == []
    with closing(connect(readonly=True)) as conn:
        assert set(seen) == {row[0] for row in conn.execute('SELECT item_code FROM items')}
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'").fetchone()[0] == 0


def test_sql_contract_and_old_memory_cleanup():
    assert set(inspect.signature(execute_sql_query).parameters) == {'query','chart','parameters'}
    state = ShoppingTracker({'needs':['office'], 'retired_navigation':{'page':2}}, None).state
    assert state['needs'] == ['office']
    assert 'retired_navigation' not in state
    assert state['product_query'] is None


def test_navigation_tracks_only_successful_item_reads_and_resets():
    tracker = ShoppingTracker(None,None)
    query = 'SELECT item_code,name FROM items ORDER BY name,item_code LIMIT ? OFFSET ?'
    args = {'query':query,'parameters':[1,0]}
    result = read(query,args['parameters'])
    tracker.observe('execute_sql_query',args,result,None)
    assert tracker.state['product_query']['parameters'] == [1,0]
    args['parameters'][1] = 99
    assert tracker.state['product_query']['parameters'] == [1,0]
    tracker.observe('execute_sql_query', {'query':'bad'}, {'ok':False},None)
    assert tracker.state['product_query']['query'] == query
    tracker.apply_update({'reset_preferences':True},'Starting again.')
    assert tracker.state['product_query'] is None
