"""Customer shopping comparisons; deterministic prices, no invented specifications."""
import json
import re
import unicodedata
from decimal import Decimal
from database.product_search import product_text, register_product_search
from mcp_server.tools.billing_tools import BillingError, tool_result, money, price_for
from mcp_server.tools.operations_tools import reading


def options(quantity, budget, required_features):
    if type(quantity) is not int or not 1 <= quantity <= 100000:
        raise BillingError('Quantity must be an integer from 1 to 100000.')
    budget = money(budget) if budget is not None else None
    required_features = required_features or {}
    if not isinstance(required_features, dict) or len(required_features) > 20 or any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, (str, int, float, bool))
        for k, v in required_features.items()
    ):
        raise BillingError('Required features must be a dictionary of at most 20 feature/value pairs.')
    return budget, required_features


def resolve(conn, reference):
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 200:
        raise BillingError('Provide an item code, SKU or product name.')
    reference = reference.strip()
    register_product_search(conn)
    rows = conn.execute('SELECT * FROM items WHERE item_code=? OR sku=?', (reference, reference)).fetchall()
    if not rows:
        rows = conn.execute('SELECT * FROM items WHERE product_text(name)=product_text(?)', (reference,)).fetchall()
    if not rows:
        rows = conn.execute('SELECT * FROM items WHERE product_matches(name,?) LIMIT 6', (reference,)).fetchall()
    if len(rows) != 1:
        choices = ', '.join(f"{r['item_code']} ({r['name']})" for r in rows[:5])
        raise BillingError('Product not found.' if not rows else 'Product is ambiguous. Ask the customer to choose: ' + choices)
    return rows[0]


def feature_key(value):
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value).casefold()))


def same_feature_value(left, right):
    if type(left) in (int, float) and type(right) in (int, float):
        return Decimal(str(left)) == Decimal(str(right))
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    return type(left) is type(right) and left == right


def product(conn, row, today, quantity, budget, requirements):
    detail = conn.execute('SELECT * FROM product_details WHERE item_code=?', (row['item_code'],)).fetchone()
    features = json.loads(detail['features_json']) if detail else {}
    units = {}
    attributes = conn.execute('SELECT * FROM product_attributes WHERE item_code=?', (row['item_code'],)).fetchall()
    typed_keys = {feature_key(attribute['attribute']) for attribute in attributes}
    # Typed records override legacy JSON, but never overwrite a conflicting typed
    # record simply because its spelling uses a different separator.
    features = {key: value for key, value in features.items() if feature_key(key) not in typed_keys}
    for attribute in attributes:
        key = attribute['attribute']
        features[key] = attribute['text_value'] if attribute['text_value'] is not None else attribute['number_value']
        if attribute['unit']:
            units[key] = attribute['unit']
    keys = {}
    for key in features:
        keys.setdefault(feature_key(key), []).append(key)
    ambiguous = {name for name, matches in keys.items() if any(
        not same_feature_value(features[matches[0]], features[key]) or units.get(matches[0]) != units.get(key)
        for key in matches[1:])}
    unit = price_for(conn, row, today)
    total = money(unit * quantity)
    matched, missing, different = [], [], []
    for key, value in requirements.items():
        normalized = feature_key(key)
        recorded_keys = keys.get(normalized, [])
        recorded_key = recorded_keys[0] if recorded_keys and normalized not in ambiguous else None
        if recorded_key is None:
            missing.append(key)
        elif (str(features[recorded_key]).casefold() == str(value).casefold() or
              (type(features[recorded_key]) in (int,float) and type(value) in (int,float)
               and Decimal(str(features[recorded_key])) == Decimal(str(value)))):
            matched.append(key)
        else:
            different.append(key)
    return {'item_code': row['item_code'], 'sku': row['sku'], 'name': row['name'],
            'category': detail['category'] if detail else None,
            'description': detail['description'] if detail else '', 'features': features, 'feature_units': units,
            'unit_price': str(unit), 'quantity': quantity, 'merchandise_total': str(total),
            'available_quantity': row['availablequantity'], 'enough_stock': row['availablequantity'] >= quantity,
            'within_budget': budget is None or total <= budget,
            'matched_features': matched, 'unknown_features': missing, 'different_features': different,
            'ambiguous_features': [key for name, matches in keys.items() if name in ambiguous for key in matches]}


def context(conn):
    config = dict(conn.execute('SELECT key,value FROM billing_config'))
    return conn.execute('SELECT CURRENT_DATE').fetchone()[0], config.get('currency', 'USD')


def eligible(row):
    return row['enough_stock'] and row['within_budget'] and not row['unknown_features'] and not row['different_features']


NOTES = ['Prices use today\'s dated price history, falling back to sellingprice, as in order quoting.',
         'Budget covers merchandise for the requested quantity, before promotions, tax and shipping. Request an order draft for the final total.',
         'Conflicting equivalent feature keys remain unknown; verify their recorded values and units before recommending.',
         'Missing features are unknown. Price and stock do not establish quality, compatibility or suitability. No inventory is reserved.']


@tool_result
def compare_products(products: list[str], quantity: int = 1, budget: str | None = None,
                     required_features: dict | None = None) -> dict:
    """Compare 2-5 products by code/SKU/name, price, stock and recorded features.

    budget is the TOTAL merchandise budget for quantity. required_features maps recorded feature
    names to desired values (e.g. {"connection":"wireless"}); key case/separators are ignored,
    values and units are preserved, and missing or conflicting features stay unknown.
    Suggests the lowest-priced available match, never an unsupported overall quality winner.
    Ask what 'better' means for the customer. Product names can be ambiguous; ask instead of guessing.
    """
    budget, requirements = options(quantity, budget, required_features)
    if not isinstance(products, list) or not 2 <= len(products) <= 5:
        raise BillingError('Compare between 2 and 5 products.')
    with reading() as conn:
        today, currency = context(conn)
        rows = [product(conn, resolve(conn, ref), today, quantity, budget, requirements) for ref in products]
        if len({r['item_code'] for r in rows}) != len(rows):
            raise BillingError('Choose distinct products to compare.')
        matches = [r for r in rows if eligible(r)]
        lowest = min((Decimal(r['unit_price']) for r in matches), default=None)
        winners = [r['item_code'] for r in matches if Decimal(r['unit_price']) == lowest]
        categories = {product_text(r['category']) for r in rows}
        return {'currency': currency, 'as_of': today, 'products': rows,
                'lowest_price_matching_items': winners,
                'recommendation_basis': 'Lowest current price among in-stock products meeting budget and every recorded requirement; ties retained.',
                'same_recorded_category': len(categories) == 1 and None not in categories,
                'notes': NOTES + ['Different or missing categories require checking that the products serve the same purpose.']}


def words(name):
    return {word for word in product_text(name).split() if word.isalpha()} - {
        'a', 'an', 'the', 'for', 'with', 'and', 'inch', 'cm', 'gb', 'tb', 'hd',
        'black', 'white', 'red', 'blue', 'premium', 'basic', 'pro', 'model', 'variant', 'wireless', 'usb'}


@tool_result
def find_cheaper_alternatives(product_reference: str, quantity: int = 1, budget: str | None = None,
                              required_features: dict | None = None, limit: int = 5) -> dict:
    """Find cheaper in-stock alternatives within the same recorded category.

    Without a source category, uses shared meaningful name words and labels matches tentative.
    Never treats a cheaper unrelated item as equivalent. Required features must be recorded and
    match; key case/separators are ignored, while values and units are preserved. Budget is total
    merchandise cost for quantity, before tax/shipping/promotions.
    """
    budget, requirements = options(quantity, budget, required_features)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise BillingError('Limit must be from 1 to 10.')
    with reading() as conn:
        today, currency = context(conn)
        source = product(conn, resolve(conn, product_reference), today, quantity, budget, requirements)
        if source['category']:
            candidates = conn.execute('SELECT i.* FROM items i JOIN product_details d ON d.item_code=i.item_code '
                'WHERE product_text(d.category)=product_text(?) AND i.item_code<>? AND i.availablequantity>=?',
                (source['category'], source['item_code'], quantity)).fetchall()
        else:
            candidates = conn.execute('SELECT * FROM items WHERE item_code<>? AND availablequantity>=?',
                                      (source['item_code'], quantity)).fetchall()
        choices, unavailable_prices = [], []
        for row in candidates:
            if not source['category'] and not words(source['name']).intersection(words(row['name'])):
                continue
            try:
                candidate = product(conn, row, today, quantity, budget, requirements)
            except BillingError:
                unavailable_prices.append(row['item_code'])
                continue
            if not eligible(candidate) or Decimal(candidate['unit_price']) >= Decimal(source['unit_price']):
                continue
            candidate['savings_for_quantity'] = str(money(Decimal(source['merchandise_total']) - Decimal(candidate['merchandise_total'])))
            candidate['match_basis'] = 'recorded_category' if source['category'] else 'shared_name_words_only'
            choices.append(candidate)
        choices.sort(key=lambda r: (Decimal(r['unit_price']), r['item_code']))
        return {'currency': currency, 'as_of': today, 'source': source, 'alternatives': choices[:limit],
                'matched_count': len(choices), 'truncated': len(choices) > limit,
                'items_with_unusable_prices': unavailable_prices,
                'notes': NOTES + ['Name-based matches are possible alternatives only; verify purpose and compatibility before recommending.',
                                 'No matches means no qualifying cheaper option was found; do not invent alternatives.']}
