import json
import pytest
from database.connection import transaction, connect
from mcp_server.tools.shopping_tools import compare_products, find_cheaper_alternatives


def products():
    with transaction() as conn:
        conn.executemany('INSERT INTO items VALUES (?,?,?,?,?,?)', [
            ('BASIC','KEY-BASIC','Basic keyboard',10,30,10),
            ('WIRELESS','KEY-WIRELESS','Wireless keyboard',15,40,10),
            ('EMPTY','KEY-EMPTY','Compact keyboard',5,20,0),
            ('CABLE','CABLE','Keyboard cable',1,3,20)])
        for code, category, connection in [('ITEM001','keyboards','wired'),
                ('KEY-BASIC','keyboards','wired'),('KEY-WIRELESS','keyboards','wireless'),
                ('KEY-EMPTY','keyboards','wired'),('CABLE','cables','wired')]:
            conn.execute('INSERT INTO product_details VALUES (?,?,?,?)',
                         (code,category,'Recorded products detail',json.dumps({'connection':connection})))


def test_compare_constraints_and_no_private_costs():
    products()
    result = compare_products(['ITEM001','KEY-WIRELESS'],quantity=2,budget='100',required_features={'connection':'wireless'})
    assert result['ok'] and result['lowest_price_matching_items'] == ['KEY-WIRELESS']
    assert result['products'][1]['merchandise_total'] == '80.00'
    assert 'baseprice' not in str(result) and 'current_unit_cost' not in str(result)
    assert not compare_products(['ITEM001','KEY-WIRELESS'], required_features={'battery':'20h'})['lowest_price_matching_items']


def test_cheaper_same_category_stock_budget_and_features():
    products()
    result = find_cheaper_alternatives('ITEM001',quantity=2,budget='100',required_features={'connection':'wireless'})
    assert [p['item_code'] for p in result['alternatives']] == ['KEY-WIRELESS']
    assert result['alternatives'][0]['savings_for_quantity'] == '79.80'
    assert find_cheaper_alternatives('ITEM001',budget='20')['matched_count'] == 0


def test_dated_prices_match_quote_and_do_not_reserve_stock():
    products()
    with transaction() as conn:
        conn.execute("INSERT INTO price_history(item_code,unit_price,effective_from) VALUES ('KEY-BASIC',90,'2000-01-01')")
    result = find_cheaper_alternatives('ITEM001')
    assert [p['item_code'] for p in result['alternatives']] == ['KEY-WIRELESS']
    with connect() as conn:
        assert conn.execute("SELECT availablequantity FROM items WHERE item_code='KEY-WIRELESS'").fetchone()[0] == 10


def test_ambiguous_names_and_same_item_aliases():
    products()
    assert not compare_products(['keyboard','ITEM002'])['ok']
    assert not compare_products(['ITEM001','SKU-KB'])['ok']
    assert not find_cheaper_alternatives("' OR 1=1 --")['ok']


def test_without_features_uses_only_labelled_tentative_matches():
    with transaction() as conn:
        conn.execute("INSERT INTO items VALUES ('CHEAP','CHEAP','Budget mechanical keyboard',10,20,5)")
    result = find_cheaper_alternatives('ITEM001')
    assert result['matched_count'] == 1
    assert result['alternatives'][0]['match_basis'] == 'shared_name_words_only'
    assert not find_cheaper_alternatives('ITEM001',required_features={'connection':'wired'})['alternatives']


@pytest.mark.parametrize('kwargs', [{'quantity':0},{'budget':'NaN'},{'limit':0}])
def test_invalid_requests(kwargs):
    assert not find_cheaper_alternatives('ITEM001',**kwargs)['ok']


def test_ties_are_not_arbitrarily_ranked_and_bad_prices_reported():
    products()
    with transaction() as conn:
        conn.execute("UPDATE items SET sellingprice=30 WHERE item_code='KEY-WIRELESS'")
    assert set(compare_products(['KEY-BASIC','KEY-WIRELESS'])['lowest_price_matching_items']) == {'KEY-BASIC','KEY-WIRELESS'}
    with transaction() as conn:
        conn.executemany("INSERT INTO price_history(item_code,unit_price,effective_from) VALUES ('KEY-BASIC',20,?)", [('2000-01-01',),('2001-01-01',)])
    assert find_cheaper_alternatives('ITEM001')['items_with_unusable_prices'] == ['KEY-BASIC']
