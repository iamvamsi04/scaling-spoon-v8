"""SQLite parser/authorizer is the security boundary, not a keyword regex."""
import math
import re
import sqlite3
import time
from collections import OrderedDict
from threading import RLock
from config import settings
from database.connection import connect
from database.product_search import normalize_product_filters, register_product_search

TABLES = {'billing_config', 'customers', 'items', 'promotions', 'orders',
          'order_requests', 'order_fulfilled', 'invoice_lines', 'price_history',
          'order_details', 'billing_audit', 'refund_requests', 'refund_details', 'order_service',
          'customer_billing_details','invoice_revisions','store_credit_ledger',
          'product_details', 'product_attributes', 'product_relations', 'json_each'}
FUNCTIONS = set('abs avg coalesce count date datetime glob group_concat hex ifnull '
                'instr julianday length like lower ltrim max min nullif printf replace '
                'round rtrim strftime substr substring sum time total trim typeof '
                'unicode upper row_number rank dense_rank lag lead first_value last_value '
                'json_extract json_type json_valid json_array_length current_date current_time current_timestamp '
                'product_text product_matches product_like'.split())

# Store schema text only. Each lookup still checks the current database file and
# SQLite schema version; prices, customers and other business rows are never cached.
_SCHEMA_CACHE_LIMIT = 12
_schema_cache = OrderedDict()
_schema_cache_lock = RLock()


def _database_identity(path):
    stat = path.stat()
    return (str(path), stat.st_dev, stat.st_ino, stat.st_ctime_ns)


def quote_reserved_aliases(query):
    """Quote bare explicit aliases only; never change literals, comments or values.

    This is syntax repair, not an authorization boundary. SQLite still parses the
    result under the same read-only authorizer and execution deadline.
    """
    tokens = re.findall(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|\w+|[^\w]", query)
    reserved = {'GROUP','ORDER','LIMIT','OFFSET','SELECT','FROM','WHERE','HAVING',
                'VALUES','INDEX','TABLE','JOIN','DISTINCT','UNION','CASE','WHEN','END'}
    previous = None
    repaired = []
    for token in tokens:
        if token.isspace() or token.startswith(('--','/*')):
            repaired.append(token)
            continue
        upper = token.upper()
        repaired.append('"'+token+'"' if previous == 'AS' and upper in reserved else token)
        previous = upper
    return ''.join(repaired)


def schema_hint(mode='customer'):
    """Return current, role-scoped schema metadata without reading business rows."""
    path = settings.BILLING_DB.resolve()
    identity = _database_identity(path)
    conn = connect(readonly=True)
    try:
        # Read the version and columns from one snapshot if another process runs
        # a migration. Replacements at the same path get a different file key.
        conn.execute('BEGIN')
        version = conn.execute('PRAGMA schema_version').fetchone()[0]
        key = (identity, version, mode, tuple(sorted(TABLES)))
        unchanged = identity == _database_identity(path)
        if unchanged:
            with _schema_cache_lock:
                cached = _schema_cache.get(key)
                if cached is not None:
                    _schema_cache.move_to_end(key)
                    return cached
        tables = sorted(TABLES - {'json_each'} - ({'billing_audit'} if mode != 'admin' else set()))
        if mode == 'customer':
            tables = [table for table in tables if table != 'promotions']
        columns = {}
        for table in tables:
            names = [r['name'] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            if mode != 'admin' and table == 'items':
                names = [n for n in names if n in {'sku','item_code','name','sellingprice','availablequantity'}]
            if names:
                columns[table] = names
        lines = ['CURRENT SQL TABLE MAP (metadata only). Qualify columns with table aliases. '
                 'Join the table that owns each requested column; do not guess column locations.']
        lines.extend(f'{table}({", ".join(names)})' for table,names in columns.items())
        joins = []
        for table in columns:
            for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
                if fk['table'] in columns and fk['from'] in columns[table] and fk['to'] in columns[fk['table']]:
                    joins.append(f'{table}.{fk["from"]} = {fk["table"]}.{fk["to"]}')
        lines.append('Declared join keys: ' + '; '.join(joins))
        lines.append('Aggregate each one-to-many side before combining totals. Missing columns are unavailable; do not invent them.')
        lines.append('Product search: product_matches(i.name, ?) or product_matches(d.category, ?) matches words/phrases regardless of case, spaces, underscores, hyphens and common plurals. Use only on product text; IDs, prices and other fields keep exact semantics.')
        hint = '\n'.join(lines)
        if unchanged and identity == _database_identity(path):
            with _schema_cache_lock:
                _schema_cache[key] = hint
                _schema_cache.move_to_end(key)
                while len(_schema_cache) > _SCHEMA_CACHE_LIMIT:
                    _schema_cache.popitem(last=False)
        return hint
    finally:
        conn.close()


def authorize(action, arg1, arg2, db, trigger):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and arg1 in TABLES and db in ('main', None):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or '').lower() in FUNCTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 == 'table_info' and arg2 in TABLES:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def execute_sql_query(query: str | None = None, chart: dict | None = None, parameters: list | None = None) -> dict:
    """Answer flexible questions with one read-only SELECT/WITH or PRAGMA table_info(table).

    Product discovery uses qualified SQL against items and optional product_details.
    Search names/categories with product_matches(i.name, ?) or product_matches(d.category, ?).
    It normalizes case, spaces, underscores, hyphens and common plurals; it matches whole words/phrases.
    Simple name/category equality, IN and LIKE filters are normalized locally, without changing stored labels.
    Keep item codes, SKUs, customer IDs and financial fields exact. Ambiguous products need clarification.
    Count all matching products before displaying a page. Use stable ordering and LIMIT/OFFSET.
    Include out-of-stock products unless explicitly filtered. Preserve scope on next-page requests.
    Use effective price_history for current prices; overlapping prices are unknown, never guessed.

    Join/filter/group permitted base tables instead of needing a tool per question.
    Inspect columns with PRAGMA. No database views exist. External payment records are unavailable;
    store_credit_ledger records local credit grants and redemptions only.
    Invoice totals minus recorded refunds are not amounts due or proof of settlement.
    Use ? placeholders with parameters for user-provided values; never interpolate names into SQL.
    For requested admin visuals optionally pass chart={"type":"bar","title":"Revenue","x":"month","y":["revenue"]}.
    Types: bar,grouped_bar,horizontal_bar,stacked_bar,normalized_bar,line,pie,
    scatter,bubble,histogram,boxplot,heatmap,range,combo,timeline.
    x/y and optional group, bubble size or timeline end must be returned column aliases.
    Histogram: numeric x,y:[],bins 2-50. Heatmap: x,group,y:[measure]. Range: y:[low,high].
    Other types accept the column counts in the chart contract. Optional x_type: categorical,numeric,temporal.
    Standard bars/lines also accept group with one y, returning one row per x/group pair.
    Distributions use complete observations; summaries aggregate duplicate coordinates in SQL.
    Include a currency column for money and query ONE currency per chart; currency is metadata,
    never an axis or a constant USD series. Order trends chronologically. No chart-count cap.
    Chart queries allow up to 1000 rows; ordinary reads use SQL_MAX_ROWS. No charts from truncated results. A chart_error leaves query rows usable; correct SQL/spec if needed.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > 20000:
        return {'ok': False, 'error': 'Provide one SQL query of at most 20000 characters.'}
    if parameters is not None and (not isinstance(parameters,list) or len(parameters)>100 or
            any(value is not None and not isinstance(value,(str,int,float)) for value in parameters)):
        return {'ok':False,'error':'Parameters must be a list of up to 100 text, numeric or null values.'}
    if any((isinstance(value, int) and not -(2**63) <= value < 2**63) or
           (isinstance(value, float) and not math.isfinite(value)) for value in parameters or []):
        return {'ok':False,'error':'Numeric parameters must be finite and integers must fit the SQLite 64-bit range.'}
    from mcp_server.access import scope, customer_query_connection
    access = scope.get()
    if chart is not None and (not access or access['mode'] != 'admin'):
        return {'ok': False, 'error': 'Analytics charts require Admin mode.'}
    conn = (customer_query_connection(access['customer_id'], staff=access['mode'] == 'cashier')
            if access and access['mode'] in ('customer', 'cashier') else connect(readonly=True))
    try:
        register_product_search(conn)
        conn.execute('PRAGMA query_only=ON')
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1000000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
        deadline = time.monotonic() + 2
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.set_authorizer(authorize)
        original_query = query
        query = normalize_product_filters(query)
        repaired_aliases = False
        try:
            cursor = conn.execute(query, parameters or [])
        except sqlite3.OperationalError as exc:
            repaired = quote_reserved_aliases(query) if chart is not None and 'syntax error' in str(exc).lower() else query
            if repaired == query:
                raise
            cursor = conn.execute(repaired, parameters or [])
            repaired_aliases = True
        from mcp_server.charts import MAX_CHART_ROWS
        row_limit = MAX_CHART_ROWS if chart is not None else settings.SQL_MAX_ROWS
        rows = cursor.fetchmany(row_limit + 1)
        if any(isinstance(value, float) and not math.isfinite(value) for row in rows for value in row):
            return {'ok':False,'error':'The query produced a non-finite number. Narrow the calculation or correct its inputs.'}
        result = {'ok': True, 'columns': [c[0] for c in cursor.description or []],
                'rows': [list(row) for row in rows[:row_limit]],
                'row_count': min(len(rows), row_limit),
                'truncated': len(rows) > row_limit,
                'coverage_note': 'row_count counts returned SQL rows only. SQL LIMIT and filters may exclude matches even when truncated is false. Count the full matching set separately before claiming completeness, absence or a store-wide total.'}
        if repaired_aliases:
            result['query_note'] = 'Quoted reserved SQL aliases locally. Use names such as series_label instead of group/order in future queries.'
        if query != original_query:
            result['product_search_note'] = 'Product name/category filters ignore case, separators and common plural endings. IDs and financial filters retain their original semantics.'
        if chart is not None:
            from mcp_server.charts import prepare_chart
            try:
                payload = prepare_chart(result['columns'],result['rows'],chart,result['truncated'])
                result['chart'] = {key:value for key,value in payload.items() if key != 'data'}
            except ValueError as exc:
                result['chart_error'] = str(exc)
        return result
    except sqlite3.Error as exc:
        result = {'ok': False, 'error': f'Query rejected or failed: {exc}'}
        if any(term in str(exc).lower() for term in ('no such column', 'ambiguous column', 'no such table')):
            result['schema_hint'] = schema_hint(access['mode'] if access else 'admin')
        return result
    finally:
        conn.close()
