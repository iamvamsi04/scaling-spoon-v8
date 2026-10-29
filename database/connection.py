import sqlite3
import json
from contextlib import contextmanager
from contextvars import ContextVar
from config import settings

audit_context = ContextVar('billing_audit', default=None)

# Human-readable labels and lifecycle values only. Identifiers and credentials
# deliberately retain their original exact comparison rules.
CASE_INSENSITIVE_COLUMNS = {
    'customers': frozenset({'name'}),
    'items': frozenset({'name'}),
    'product_details': frozenset({'category'}),
    'product_attributes': frozenset({'attribute'}),
    'orders': frozenset({'order_status', 'refund_status'}),
    'order_details': frozenset({'status'}),
    'refund_requests': frozenset({'status'}),
    'order_service': frozenset({'fulfillment_status'}),
    'customer_addresses': frozenset({'label', 'full_name', 'city', 'state'}),
}


def connect(readonly=False):
    if readonly:
        conn = sqlite3.connect(settings.BILLING_DB.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    else:
        conn = sqlite3.connect(settings.BILLING_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


@contextmanager
def transaction():
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        event = audit_context.get()
        if event and conn.total_changes:
            conn.execute('INSERT INTO billing_audit(action,details_json) VALUES (?,?)',
                         (event['action'], json.dumps(event['arguments'], default=str, sort_keys=True)))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize():
    settings.BILLING_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        from database.migrations import (remove_retired_tables, remove_views, add_promotion_expiry,
                                         add_unknown_fulfillment, add_order_status, add_text_collations)
        remove_views(conn)
        remove_retired_tables(conn)
        conn.executescript((settings.ROOT / 'database/schema.sql').read_text())
        add_promotion_expiry(conn)
        conn.executescript((settings.ROOT / 'database/operations.sql').read_text())
        conn.executescript((settings.ROOT / 'database/insights.sql').read_text())
        conn.executescript((settings.ROOT / 'database/service.sql').read_text())
        add_unknown_fulfillment(conn)
        add_order_status(conn)
        conn.executemany('INSERT OR IGNORE INTO billing_config(key,value) VALUES (?,?)',
                         [('tax_rate', '0'), ('shipping_amount', '0'), ('currency', 'USD')])
        conn.commit()
        add_text_collations(conn)
    finally:
        conn.close()
