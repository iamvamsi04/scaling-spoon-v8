import os
import sqlite3

import pytest

from config import settings
from database.connection import connect, transaction
from mcp_server.tools import database


@pytest.fixture(autouse=True)
def clear_schema_cache():
    database._schema_cache.clear()
    yield
    database._schema_cache.clear()


def track_metadata(monkeypatch):
    statements = []
    original = database.connect

    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(database, 'connect', traced)
    return statements


def metadata_reads(statements):
    return [sql for sql in statements if sql.startswith(('PRAGMA table_info', 'PRAGMA foreign_key_list'))]


def test_repeat_uses_metadata_cache_and_still_checks_schema_version(monkeypatch):
    statements = track_metadata(monkeypatch)
    first = database.schema_hint('customer')
    assert metadata_reads(statements)
    statements.clear()
    assert database.schema_hint('customer') == first
    assert 'PRAGMA schema_version' in statements
    assert not metadata_reads(statements)
    # The cache stores one immutable schema string, never query rows or sessions.
    assert list(database._schema_cache.values()) == [first]
    assert 'customer_credentials' not in first and 'customer_sessions' not in first
    assert not any(sql.lstrip().upper().startswith('SELECT ') for sql in statements)


def test_schema_change_invalidates_cache(monkeypatch):
    statements = track_metadata(monkeypatch)
    old = database.schema_hint('customer')
    with transaction() as conn:
        conn.execute('ALTER TABLE product_details ADD COLUMN warranty_note TEXT')
    statements.clear()
    current = database.schema_hint('customer')
    assert 'warranty_note' not in old and 'warranty_note' in current
    assert metadata_reads(statements)


def test_same_path_database_replacement_with_equal_schema_version_invalidates_cache():
    old = database.schema_hint('admin')
    replacement = settings.BILLING_DB.with_name('replacement.db')
    source = connect(readonly=True)
    target = sqlite3.connect(replacement)
    try:
        version = source.execute('PRAGMA schema_version').fetchone()[0]
        source.backup(target)
        target.execute('ALTER TABLE product_details ADD COLUMN replacement_note TEXT')
        target.execute(f'PRAGMA schema_version={version}')
        target.commit()
    finally:
        target.close()
        source.close()
    os.replace(replacement, settings.BILLING_DB)
    current = database.schema_hint('admin')
    assert 'replacement_note' not in old and 'replacement_note' in current


def test_role_and_allowlist_changes_never_reuse_broader_metadata(monkeypatch):
    admin = database.schema_hint('admin')
    customer = database.schema_hint('customer')
    cashier = database.schema_hint('cashier')
    assert 'baseprice' in admin and 'billing_audit(' in admin
    assert 'baseprice' not in customer and 'billing_audit(' not in customer
    assert 'promotions(' in cashier and 'promotions(' not in customer
    monkeypatch.setattr(database, 'TABLES', database.TABLES - {'product_attributes'})
    restricted = database.schema_hint('admin')
    assert 'product_attributes(' in admin and 'product_attributes(' not in restricted


def test_business_values_are_always_read_fresh_after_metadata_cache_hit():
    database.schema_hint('admin')
    with transaction() as conn:
        conn.execute('UPDATE items SET availablequantity=availablequantity+17 WHERE item_code=?', ('ITEM001',))
    database.schema_hint('admin')
    result = database.execute_sql_query('SELECT availablequantity FROM items WHERE item_code=?', parameters=['ITEM001'])
    assert result['ok']
    conn = connect(readonly=True)
    try:
        expected = conn.execute('SELECT availablequantity FROM items WHERE item_code=?', ('ITEM001',)).fetchone()[0]
    finally:
        conn.close()
    assert result['rows'] == [[expected]]
    assert all('ITEM001' not in hint for hint in database._schema_cache.values())


def test_metadata_cache_is_bounded():
    for number in range(database._SCHEMA_CACHE_LIMIT + 3):
        database.schema_hint('admin')
        with transaction() as conn:
            conn.execute(f'CREATE TABLE cache_check_{number}(id INTEGER)')
    assert len(database._schema_cache) == database._SCHEMA_CACHE_LIMIT
