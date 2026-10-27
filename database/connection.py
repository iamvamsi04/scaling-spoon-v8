import sqlite3
import json
from contextlib import contextmanager
from contextvars import ContextVar
from config import settings

audit_context = ContextVar('billing_audit', default=None)


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
                                         add_unknown_fulfillment, add_order_status)
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
    finally:
        conn.close()
