import json

import pytest

from database.connection import transaction
from database.product_search import normalize_product_filters, product_matches, product_text
from mcp_server.access import scoped_tool
from mcp_server.tools.database import execute_sql_query
from mcp_server.tools.shopping_tools import compare_products, find_cheaper_alternatives


@pytest.fixture
def power_banks():
    with transaction() as conn:
        conn.executemany('INSERT INTO items VALUES (?,?,?,?,?,?)', [
            ('PB-PRO', 'PB001', 'Travel Power-Bank Pro', 10, 60, 20),
            ('PB-STD', 'PB002', 'Compact power_bank', 5, 30, 15),
            ('PB-ALT', 'PB003', 'Compact power banks', 5, 35, 15),
            ('CABLE-ONE', 'CAB001', 'Power bank cable', 1, 2, 50),
        ])
        conn.executemany('INSERT INTO product_details VALUES (?,?,?,?)', [
            ('PB001', 'power_banks', '', '{}'),
            ('PB002', 'Power Banks', '', '{}'),
            ('PB003', 'power-bank', '', '{}'),
            ('CAB001', 'cables', '', '{}'),
        ])


@pytest.mark.parametrize('label', ['power banks', 'POWER_BANKS', 'Power-Bank', 'power bank', '  Power   Banks  '])
@pytest.mark.parametrize('mode', ['customer', 'cashier', 'admin'])
def test_category_search_counts_and_lists_all_separator_and_case_variants(power_banks, label, mode):
    query = scoped_tool(execute_sql_query)
    result = query(
        'SELECT i.item_code FROM items i JOIN product_details d ON d.item_code=i.item_code '
        'WHERE d.category=? ORDER BY i.item_code', parameters=[label], execution_mode=mode)
    assert result['ok'], result
    assert result['rows'] == [['PB001'], ['PB002'], ['PB003']]
    assert 'product_search_note' in result
    count = query('SELECT COUNT(*) FROM product_details WHERE category=?', parameters=[label], execution_mode=mode)
    assert count['rows'] == [[3]]


@pytest.mark.parametrize('predicate,parameters', [
    ('d.category = ?', ['power banks']),
    ('lower(d.category)=lower(?)', ['POWER BANK']),
    ('upper(trim(d.category))=upper(trim(?))', ['POWER BANKS']),
    ("d.category LIKE '%power banks%'", []),
    ('d.category LIKE ?', ['%POWER-BANK%']),
    ('d.category IN (?,?)', ['POWER BANKS', 'not a category']),
    ('product_matches(d.category, ?)', ['power banks']),
    ('product_text(d.category) IN (product_text(?))', ['Power Banks']),
])
def test_parameter_and_literal_queries_use_same_matching(power_banks, predicate, parameters):
    result = execute_sql_query('SELECT d.item_code FROM product_details AS d WHERE ' + predicate + ' ORDER BY d.item_code', parameters=parameters)
    assert result['ok'], result
    assert result['rows'] == [['PB001'], ['PB002'], ['PB003']]


def test_nested_query_uses_explicit_match_function_without_rewriting_scope(power_banks):
    sql = ('SELECT i.item_code FROM items i WHERE i.item_code IN '
           '(SELECT d.item_code FROM product_details d WHERE product_matches(d.category,?)) ORDER BY i.item_code')
    assert normalize_product_filters(sql) == sql
    result = execute_sql_query(sql, parameters=['Power Banks'])
    assert result['rows'] == [['PB001'], ['PB002'], ['PB003']]


def test_name_search_normalizes_separators_and_preserves_ambiguity(power_banks):
    result = compare_products(['travel power banks pro', 'PB002'])
    assert result['ok'], result
    assert result['products'][0]['item_code'] == 'PB001'
    assert result['same_recorded_category']
    ambiguous = compare_products(['compact POWER BANK', 'PB001'])
    assert not ambiguous['ok'] and 'ambiguous' in ambiguous['error'].lower()
    no_partial_words = compare_products(['trav', 'PB002'])
    assert not no_partial_words['ok']
    # Identifiers are resolved exactly, never changed to lower case or stemmed.
    assert not compare_products(['pb001', 'PB002'])['ok']


def test_alternatives_compare_normalized_category_only(power_banks):
    result = find_cheaper_alternatives('PB001')
    assert result['ok'], result
    assert [item['item_code'] for item in result['alternatives']] == ['PB002', 'PB003']
    assert all(item['category'] != 'cables' for item in result['alternatives'])


def test_missing_category_fallback_compares_normalized_name_words():
    with transaction() as conn:
        conn.executemany('INSERT INTO items VALUES (?,?,?,?,?,?)', [
            ('STUDIO', 'MON001', 'Studio Monitors', 20, 200, 10),
            ('BUDGET', 'MON002', 'Affordable monitor', 10, 100, 10),
            ('DESK', 'DESK001', 'Affordable office desk', 10, 50, 10),
        ])
    result = find_cheaper_alternatives('MON001')
    assert result['ok'], result
    assert [item['item_code'] for item in result['alternatives']] == ['MON002']
    assert result['alternatives'][0]['match_basis'] == 'shared_name_words_only'


def test_name_phrase_search_is_not_unbounded_substring(power_banks):
    result = execute_sql_query('SELECT i.item_code FROM items i WHERE product_matches(i.name, ?) ORDER BY i.item_code', parameters=['power banks'])
    assert result['rows'] == [['CAB001'], ['PB001'], ['PB002'], ['PB003']]
    assert not product_matches('bankruptcy tool', 'bank')
    assert not product_matches('Empowered bank', 'power bank')
    assert not product_matches('Power bank', '')
    assert product_matches('Wireless Mice', 'wireless mouse')
    assert product_text('Wireless headsets') == 'wireless headset'


@pytest.mark.parametrize('sql', [
    "SELECT name FROM customers WHERE name='Alex-Morgan'",
    "SELECT item_code FROM items WHERE item_code='pb001'",
    "SELECT sku FROM items WHERE sku='pb pro'",
    "SELECT charged_price FROM orders WHERE charged_price='10-00'",
    "SELECT order_id FROM orders WHERE order_id='test order'",
    "SELECT currency FROM order_details WHERE currency='usd'",
    "SELECT i.name FROM items i WHERE i.name=? COLLATE BINARY",
    "SELECT i.name FROM items i WHERE i.name LIKE ? ESCAPE '\\'",
    "SELECT i.name FROM items i WHERE i.name || 'extra' = ?",
    "SELECT i.name FROM items i WHERE i.name NOT IN (?)",
    "SELECT i.name FROM items i WHERE i.name IN (name || ?)",
    "SELECT (i.name = 'POWER BANK') FROM items i",
    "SELECT i.name FROM items i WHERE i.name=(SELECT name FROM customers LIMIT 1)",
    "WITH i AS (SELECT name FROM customers) SELECT * FROM i WHERE i.name='power banks'",
    "SELECT name FROM items i, customers c WHERE name='power banks'",
    "SELECT i.name FROM customers i WHERE i.name='power banks'",
    "SELECT 'WHERE i.name=\'power banks\'' FROM items i",
])
def test_rewrite_does_not_change_nonproduct_or_complex_semantics(sql):
    assert normalize_product_filters(sql) == sql


def test_rewrite_retains_comments_strings_and_other_filters(power_banks):
    sql = ('SELECT i.item_code, \'d.category=power banks\' AS note FROM items i '
           'JOIN product_details d ON d.item_code=i.item_code '
           'WHERE /* category search */ d.category=? AND i.item_code=? AND i.sellingprice>=? '
           '-- d.category=\'cables\'\nORDER BY i.item_code')
    result = execute_sql_query(sql, parameters=['power banks', 'PB002', 30])
    assert result['ok'], result
    assert result['rows'] == [['PB002', 'd.category=power banks']]
    assert execute_sql_query(sql, parameters=['power banks', 'pb002', 30])['rows'] == []
    assert execute_sql_query(sql, parameters=['power banks', 'PB002', 31])['rows'] == []


def test_product_like_keeps_sql_null_logic_for_missing_join_details(power_banks):
    result = execute_sql_query('SELECT i.item_code FROM items i LEFT JOIN product_details d ON d.item_code=i.item_code '
                               'WHERE NOT (d.category LIKE ?) ORDER BY i.item_code', parameters=['%power banks%'])
    assert result['rows'] == [['CAB001']]
    assert execute_sql_query('SELECT product_like(NULL, ?), product_like(?,NULL)', parameters=['%', 'bank'])['rows'] == [[None, None]]


def test_quoted_identifiers_and_like_prefix_boundaries(power_banks):
    result = execute_sql_query('SELECT "p"."item_code" FROM "product_details" AS "p" WHERE "p"."category"=? ORDER BY "p"."item_code"', parameters=['Power banks'])
    assert result['rows'] == [['PB001'], ['PB002'], ['PB003']]
    result = execute_sql_query('SELECT i.item_code FROM items i WHERE i.name LIKE ? ORDER BY i.item_code', parameters=['power %'])
    assert result['rows'] == [['CAB001']]


def test_search_functions_do_not_bypass_read_authorization(power_banks):
    customer = scoped_tool(execute_sql_query)
    result = customer('SELECT password_hash FROM customer_credentials WHERE product_matches(customer_id,?)', parameters=['CUST001'], execution_mode='customer')
    assert not result['ok']
    result = customer('UPDATE items SET name=product_text(name)', execution_mode='customer')
    assert not result['ok']


@pytest.mark.parametrize('key', ['battery life', 'BATTERY-LIFE', 'battery_life', 'Ｂａｔｔｅｒｙ　Ｌｉｆｅ'])
def test_feature_keys_ignore_case_unicode_and_separators(power_banks, key):
    with transaction() as conn:
        conn.execute('UPDATE product_details SET features_json=? WHERE item_code IN (?,?)',
                     (json.dumps({'battery_life': 10, 'connection_type': 'USB-C'}), 'PB001', 'PB002'))
    result = compare_products(['PB001', 'PB002'], required_features={key: 10})
    assert result['ok'] and result['lowest_price_matching_items'] == ['PB002']
    assert all(row['matched_features'] == [key] and not row['unknown_features'] for row in result['products'])
    alternative = find_cheaper_alternatives('PB001', required_features={key: 10})
    assert [row['item_code'] for row in alternative['alternatives']] == ['PB002']
    unchanged_values = compare_products(['PB001', 'PB002'], required_features={'connection type': 'USB C'})
    assert not unchanged_values['lowest_price_matching_items']
    assert all(row['different_features'] == ['connection type'] for row in unchanged_values['products'])


def test_typed_feature_overrides_all_equivalent_json_keys_without_changing_units(power_banks):
    with transaction() as conn:
        conn.execute('UPDATE product_details SET features_json=? WHERE item_code=?',
                     (json.dumps({'battery_life': 2, 'Battery-Life': 3, 'color': 'Black'}), 'PB001'))
        conn.execute('INSERT INTO product_attributes VALUES (?,?,NULL,?,?)', ('PB001', 'Battery Life', 10, 'h'))
    result = compare_products(['PB001', 'PB002'], required_features={'BATTERY_LIFE': 10})
    assert result['ok']
    product = result['products'][0]
    assert product['features'] == {'color': 'Black', 'Battery Life': 10.0}
    assert product['feature_units'] == {'Battery Life': 'h'}
    assert product['matched_features'] == ['BATTERY_LIFE'] and not product['ambiguous_features']
    different = compare_products(['PB001', 'PB002'], required_features={'battery life': '10 h'})
    assert different['products'][0]['different_features'] == ['battery life']


def test_conflicting_normalized_json_features_do_not_match_or_win_recommendations(power_banks):
    with transaction() as conn:
        conn.execute('UPDATE product_details SET features_json=? WHERE item_code IN (?,?)',
                     (json.dumps({'battery_life': 10}), 'PB001', 'PB003'))
        conn.execute('UPDATE product_details SET features_json=? WHERE item_code=?',
                     (json.dumps({'battery_life': 10, 'Battery-Life': 20}), 'PB002'))
    result = compare_products(['PB001', 'PB002'], required_features={'battery life': 10})
    assert result['ok'] and result['lowest_price_matching_items'] == ['PB001']
    ambiguous = result['products'][1]
    assert ambiguous['unknown_features'] == ['battery life'] and not ambiguous['matched_features']
    assert set(ambiguous['ambiguous_features']) == {'battery_life', 'Battery-Life'}
    alternatives = find_cheaper_alternatives('PB001', required_features={'battery life': 10})
    assert [row['item_code'] for row in alternatives['alternatives']] == ['PB003']


def test_identical_equivalent_features_are_not_ambiguous(power_banks):
    with transaction() as conn:
        conn.execute('UPDATE product_details SET features_json=? WHERE item_code=?',
                     (json.dumps({'connection_type': 'USB-C', 'Connection-Type': 'usb-c'}), 'PB001'))
    result = compare_products(['PB001', 'PB002'], required_features={'connection type': 'USB-C'})
    assert result['ok'] and result['lowest_price_matching_items'] == ['PB001']
    assert result['products'][0]['matched_features'] == ['connection type']
    assert not result['products'][0]['ambiguous_features']


def test_conflicting_typed_units_are_not_overwritten_or_converted(power_banks):
    with transaction() as conn:
        conn.executemany('INSERT INTO product_attributes VALUES (?,?,NULL,?,?)', [
            ('PB001', 'Battery Life', 10, 'h'), ('PB001', 'battery_life', 10, 'min')])
    result = compare_products(['PB001', 'PB002'], required_features={'battery life': 10})
    assert result['ok'] and not result['lowest_price_matching_items']
    product = result['products'][0]
    assert product['feature_units'] == {'Battery Life': 'h', 'battery_life': 'min'}
    assert product['unknown_features'] == ['battery life']
    assert set(product['ambiguous_features']) == {'Battery Life', 'battery_life'}
