"""Trusted API/workbench scope carried over the private local stdio transport."""
from contextvars import ContextVar
from contextlib import closing
from functools import wraps
import inspect
import sqlite3
from database.connection import connect

scope = ContextVar('tool_scope', default=None)
ADMIN_TOOLS = {'execute_sql_query', 'billing_analysis', 'billing_anomalies', 'download_invoice', 'process_refund', 'manage_draft'}
CASHIER_TOOLS = {'execute_sql_query', 'download_invoice', 'process_refund'}
CUSTOMER_TOOLS = {'execute_sql_query', 'request_order', 'place_order', 'apply_promotion',
                  'download_invoice', 'manage_draft', 'manage_customer', 'process_refund',
                  'compare_products', 'find_cheaper_alternatives'}
SCOPE_FIELDS = ('execution_mode', 'execution_customer_id', 'execution_coupon_codes')


def scoped_tool(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        mode = kwargs.pop('execution_mode', 'customer')
        customer = kwargs.pop('execution_customer_id', None)
        coupon_codes = kwargs.pop('execution_coupon_codes', None)
        allowed = ADMIN_TOOLS if mode == 'admin' else CASHIER_TOOLS if mode == 'cashier' else CUSTOMER_TOOLS if mode == 'customer' else set()
        if fn.__name__ not in allowed:
            return {'ok': False, 'error': 'This tool is not available in the current mode.'}
        arguments = inspect.signature(fn).bind(*args, **kwargs).arguments
        if mode in ('admin', 'cashier') and fn.__name__ == 'download_invoice' and arguments.get('action', 'generate') != 'generate':
            return {'ok': False, 'error': 'Staff invoice access is read-only. The customer must confirm corrections in their own conversation.'}
        if mode == 'customer' and fn.__name__ == 'manage_customer' and arguments.get('action', 'create') == 'create':
            return {'ok': False, 'error': 'Create your account using the registration form, then sign in. Account credentials must never be sent in chat.'}
        if mode == 'customer' and 'customer_id' in arguments:
            if not customer or arguments['customer_id'] != customer:
                return {'ok': False, 'error': 'Sign in to the matching customer account before accessing its records.'}
        if mode == 'customer' and fn.__name__ == 'apply_promotion':
            code = arguments.get('promo_code')
            supplied = {value.strip().casefold() for value in coupon_codes or []
                        if isinstance(value, str)} if isinstance(coupon_codes, list) else set()
            if not isinstance(code, str) or code.strip().casefold() not in supplied:
                return {'ok': False, 'error': 'Ask the customer to provide a coupon code. '
                        'Only a code explicitly supplied by that customer can be checked or applied.'}
        token = scope.set({'mode': mode, 'customer_id': customer})
        try:
            return fn(*args, **kwargs)
        finally:
            scope.reset(token)
    run.__signature__ = inspect.signature(fn).replace(parameters=[
        *inspect.signature(fn).parameters.values(),
        inspect.Parameter('execution_mode', inspect.Parameter.KEYWORD_ONLY, default='customer', annotation=str),
        inspect.Parameter('execution_customer_id', inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None),
        inspect.Parameter('execution_coupon_codes', inspect.Parameter.KEYWORD_ONLY, default=None, annotation=list[str] | None)])
    run.__annotations__ = {**fn.__annotations__, 'execution_mode': str, 'execution_customer_id': str | None,
                           'execution_coupon_codes': list[str] | None}
    return run


def customer_query_connection(customer_id, staff=False):
    """Materialize only public products and associated customer rows in an isolated DB.

    User SQL never touches the source connection, preventing joins, CTEs and main.table
    qualification from bypassing a row filter. Original column names are preserved.
    """
    source, target = connect(readonly=True), sqlite3.connect(':memory:')
    target.row_factory = sqlite3.Row
    queries = {
        'billing_config': "SELECT key,value FROM billing_config WHERE key IN ('currency','tax_rate','shipping_amount')",
        'items': 'SELECT sku,item_code,name,sellingprice,availablequantity FROM items',
        'product_details': 'SELECT * FROM product_details',
        'product_attributes': 'SELECT * FROM product_attributes',
        'product_relations': 'SELECT * FROM product_relations',
        'price_history': 'SELECT * FROM price_history',
        'customers': 'SELECT * FROM customers WHERE customer_id=?',
        'orders': 'SELECT * FROM orders WHERE customer_id=?',
        'customer_billing_details':'SELECT * FROM customer_billing_details WHERE customer_id=?',
        'store_credit_ledger':'SELECT * FROM store_credit_ledger WHERE customer_id=?',
    }
    # Coupons are private codes supplied by a customer, not public products.
    # Keep this table out of customer SQL entirely; row filters or LIMITs would
    # still let a model enumerate codes in follow-up queries.
    if staff:
        queries['promotions'] = 'SELECT * FROM promotions'
    for table in ('order_requests','order_fulfilled','invoice_lines','order_details','refund_requests','refund_details','order_service','invoice_revisions'):
        queries[table] = f'SELECT t.* FROM {table} t JOIN orders o ON o.order_id=t.order_id WHERE o.customer_id=?'
    try:
        source.execute('BEGIN')
        for table, sql in queries.items():
            if staff:
                sql = sql.replace(' WHERE customer_id=?', '').replace(' WHERE o.customer_id=?', '')
            cursor = source.execute(sql, (customer_id,) if '?' in sql else ())
            columns = [c[0] for c in cursor.description]
            target.execute('CREATE TABLE '+table+' ('+', '.join('"'+c+'"' for c in columns)+')')
            target.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in columns)+')', cursor.fetchall())
        target.commit()
        return target
    except BaseException:
        target.close()
        raise
    finally:
        source.close()
