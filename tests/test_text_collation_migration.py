"""Selected label comparisons change; stored facts and database rules do not."""
from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3

import pytest

from config import settings
from database import migrations
from database.connection import CASE_INSENSITIVE_COLUMNS


def legacy(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    schema = '\n'.join((settings.ROOT / 'database' / name).read_text()
                       for name in ('schema.sql', 'operations.sql', 'insights.sql', 'service.sql'))
    conn.executescript(schema.replace(' COLLATE NOCASE', '').replace(' COLLATE BINARY', ''))
    migrations.add_order_status(conn)
    conn.executescript("""
        INSERT INTO customers VALUES ('Owner-A','Alice Example','Company','Alice@example.com');
        INSERT INTO customers VALUES ('Owner-B','Other Customer',NULL,NULL);
        INSERT INTO customers VALUES ('Deleted','Removed Customer',NULL,NULL);
        DELETE FROM customers WHERE customer_id='Deleted';
        INSERT INTO customers VALUES ('Owner-C','Third Customer',NULL,NULL);
        INSERT INTO items VALUES ('SKU-A','ITEM-A','Bright Projector',10,20,5);
        INSERT INTO product_details VALUES ('ITEM-A','Projectors','Display','{}');
        INSERT INTO product_attributes VALUES ('ITEM-A','Brightness',NULL,100,'mW');
        INSERT INTO orders(order_id,customer_id,order_invoice,subtotal,charged_price)
            VALUES ('Order-A','Owner-A','INV-A',20,20);
        INSERT INTO order_details(order_id,currency) VALUES ('Order-A','USD');
        INSERT INTO order_service(order_id,fulfillment_status) VALUES ('Order-A','placed');
        INSERT INTO refund_requests(order_id,amount,currency,reason) VALUES ('Order-A',5,'USD','Return');
        INSERT INTO customer_credentials(customer_id,password_hash) VALUES ('Owner-A','Preserved-Hash-MixedCase');
        INSERT INTO customer_sessions VALUES ('Preserved-Token-Hash','Owner-A',2000000000);
        INSERT INTO store_credit_ledger(customer_id,order_id,currency,amount,kind)
            VALUES ('Owner-A','Order-A','USD',5,'refund_credit');
        ALTER TABLE customers ADD COLUMN extra TEXT DEFAULT 'name COLLATE NOCASE';
        CREATE INDEX custom_name_index ON customers(name);
        CREATE TRIGGER custom_customer_update AFTER UPDATE OF name ON customers
          BEGIN UPDATE customer_billing_details SET billing_address=NEW.name WHERE customer_id=NEW.customer_id; END;
    """)
    conn.commit()
    return conn


def records(conn):
    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: [tuple(row) for row in conn.execute('SELECT rowid,* FROM "' + name + '" ORDER BY rowid')]
            for name in names}


def objects(conn):
    return [tuple(row) for row in conn.execute("SELECT type,name,sql FROM sqlite_master "
        "WHERE type IN ('index','trigger') ORDER BY type,name")]


def test_migration_preserves_records_rowids_objects_and_foreign_keys(tmp_path):
    path = tmp_path / 'legacy.db'
    with closing(legacy(path)) as conn:
        before, before_objects = records(conn), objects(conn)
        backup = migrations.add_text_collations(conn, tmp_path / 'backups')
        assert backup and Path(backup).is_file()
        with closing(sqlite3.connect(backup)) as saved:
            assert records(saved) == before
        assert records(conn) == before
        assert objects(conn) == before_objects
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute("SELECT order_id FROM refund_requests WHERE status='PENDING'").fetchone()[0] == 'Order-A'
        assert conn.execute("SELECT name FROM customers WHERE name='ALICE EXAMPLE'").fetchone()[0] == 'Alice Example'
        assert conn.execute("SELECT COUNT(*) FROM customers WHERE customer_id='owner-a'").fetchone()[0] == 0
        version = conn.execute('PRAGMA schema_version').fetchone()[0]
        assert migrations.add_text_collations(conn, tmp_path / 'backups') is None
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == version
        assert len(list((tmp_path / 'backups').glob('*.db'))) == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE refund_requests SET status='PENDING' WHERE order_id='Order-A'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM store_credit_ledger")
        conn.rollback()
        conn.execute("INSERT INTO customer_billing_details(customer_id) VALUES ('Owner-A')")
        conn.execute("UPDATE customers SET name='New Label' WHERE customer_id='Owner-A'")
        assert conn.execute("SELECT billing_address FROM customer_billing_details WHERE customer_id='Owner-A'").fetchone()[0] == 'New Label'


def test_fresh_schema_already_has_comparison_rules_without_migration_backup(tmp_path):
    with closing(sqlite3.connect(settings.BILLING_DB)) as conn:
        version = conn.execute('PRAGMA schema_version').fetchone()[0]
        assert migrations.add_text_collations(conn, tmp_path / 'backups') is None
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == version
        assert not (tmp_path / 'backups').exists()
        for table, selected in CASE_INSENSITIVE_COLUMNS.items():
            sql = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
            assert migrations._collation_definition(sql, table, selected, table + '_unused') is None


@pytest.mark.parametrize('failure', ['backup', 'drop', 'unique'])
def test_failure_rolls_back_rows_schema_triggers_and_foreign_key_setting(tmp_path, monkeypatch, failure):
    with closing(legacy(tmp_path / 'failure.db')) as conn:
        if failure == 'unique':
            conn.execute('CREATE UNIQUE INDEX custom_unique_name ON customers(name)')
            conn.execute("INSERT INTO customers(customer_id,name) VALUES ('Owner-D','alice example')")
            conn.commit()
        before, before_objects = records(conn), objects(conn)
        definitions = [tuple(row) for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")]
        if failure == 'backup':
            def fail(*args):
                raise OSError('Backup unavailable')
            monkeypatch.setattr(migrations, '_collation_backup', fail)
        if failure == 'drop':
            conn.set_authorizer(lambda action, table, *args:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE and table == 'items' else sqlite3.SQLITE_OK)
        with pytest.raises((OSError, sqlite3.DatabaseError)):
            migrations.add_text_collations(conn, tmp_path / 'backups')
        conn.set_authorizer(None)
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert records(conn) == before
        assert objects(conn) == before_objects
        assert [tuple(row) for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")] == definitions


def test_migration_never_commits_an_existing_caller_transaction(tmp_path):
    with closing(legacy(tmp_path / 'pending.db')) as conn:
        conn.execute("INSERT INTO customers(customer_id,name) VALUES ('Unsaved','Pending change')")
        with pytest.raises(ValueError, match='separate transaction'):
            migrations.add_text_collations(conn, tmp_path / 'backups')
        assert conn.in_transaction
        conn.rollback()
        assert not conn.execute("SELECT 1 FROM customers WHERE customer_id='Unsaved'").fetchone()


def test_quoted_columns_without_rowid_and_generated_columns_are_preserved(tmp_path):
    path = tmp_path / 'custom.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript('''CREATE TABLE "Customers" (
            "customer_id" TEXT PRIMARY KEY, "Name" TEXT NOT NULL,
            extra TEXT GENERATED ALWAYS AS (lower("Name")) STORED,
            CHECK("Name" <> 'Blocked')) WITHOUT ROWID;
            INSERT INTO "Customers"(customer_id,"Name") VALUES ('Exact-ID','Alice');''')
        migrations.add_text_collations(conn, tmp_path / 'backups')
        assert conn.execute("SELECT customer_id,Name,extra FROM Customers WHERE Name='ALICE'").fetchall() == [('Exact-ID', 'Alice', 'alice')]
        # The implicit binary CHECK is preserved even though reads ignore case.
        conn.execute("INSERT INTO Customers(customer_id,Name) VALUES ('Other-ID','blocked')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO Customers(customer_id,Name) VALUES ('Third-ID','Blocked')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('SELECT rowid FROM Customers').fetchall()


def test_unsupported_custom_collation_fails_before_any_schema_change(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'unsupported.db')) as conn:
        conn.create_collation('CUSTOM_CASE', lambda left, right: (left > right) - (left < right))
        conn.execute('CREATE TABLE customers(customer_id TEXT PRIMARY KEY,name TEXT COLLATE CUSTOM_CASE)')
        before = conn.execute('PRAGMA schema_version').fetchone()[0]
        with pytest.raises(ValueError, match='unsupported collation'):
            migrations.add_text_collations(conn, tmp_path / 'backups')
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == before
        assert not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('empty', [False, True])
def test_custom_autoincrement_sequence_keeps_deleted_high_water_mark(tmp_path, empty):
    with closing(sqlite3.connect(tmp_path / 'sequence.db')) as conn:
        conn.executescript('''CREATE TABLE customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT UNIQUE, name TEXT);
            INSERT INTO customers(customer_id,name) VALUES ('First','Alice');
            INSERT INTO customers(id,customer_id,name) VALUES (80,'Deleted','Removed');
            DELETE FROM customers WHERE id=80;''')
        if empty:
            conn.execute('DELETE FROM customers')
            conn.commit()
        migrations.add_text_collations(conn, tmp_path / 'backups')
        conn.execute("INSERT INTO customers(customer_id,name) VALUES ('Next','Bob')")
        assert conn.execute("SELECT id FROM customers WHERE customer_id='Next'").fetchone()[0] == 81


def test_explicit_nonbinary_status_check_is_rejected_without_changes(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'status-check.db')) as conn:
        conn.execute("CREATE TABLE refund_requests(order_id TEXT PRIMARY KEY,status TEXT "
                     "CHECK(status COLLATE NOCASE IN ('pending','approved')))")
        before = conn.execute('PRAGMA schema_version').fetchone()[0]
        with pytest.raises(ValueError, match='non-binary comparison'):
            migrations.add_text_collations(conn, tmp_path / 'backups')
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == before


def test_initialize_upgrades_legacy_comparisons_without_changing_business_rows(tmp_path, monkeypatch):
    from database.connection import initialize
    path = tmp_path / 'initialize.db'
    folder = tmp_path / 'initialize_backups'
    with closing(legacy(path)) as conn:
        conn.executemany('INSERT INTO billing_config(key,value) VALUES (?,?)',
                         [('tax_rate', '0'), ('shipping_amount', '0'), ('currency', 'USD')])
        conn.commit()
        before = records(conn)
    monkeypatch.setattr(settings, 'BILLING_DB', path)
    monkeypatch.setattr(migrations, 'TEXT_COLLATION_BACKUP_DIR', folder)
    initialize()
    with closing(sqlite3.connect(path)) as conn:
        assert records(conn) == before
        assert conn.execute("SELECT order_id FROM refund_requests WHERE status='PENDING'").fetchall() == [('Order-A',)]
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert len(list(folder.glob('*.db'))) == 1


def test_generator_early_return_upgrades_legacy_comparisons_without_reseeding(tmp_path, monkeypatch):
    from database import service_seed
    import generate_seed
    path = tmp_path / 'generator.db'
    folder = tmp_path / 'generator_backups'
    with closing(legacy(path)) as conn:
        enrichment_marker = service_seed.seed_layout(conn)[3]
        conn.execute('INSERT INTO billing_config(key,value) VALUES (?,?)', (enrichment_marker, 'Existing records'))
        conn.commit()
        before = records(conn)
    monkeypatch.setattr(migrations, 'TEXT_COLLATION_BACKUP_DIR', folder)
    monkeypatch.setattr(service_seed, 'seed_service_scenarios', lambda *args: None)
    result = generate_seed.generate_records(path, 42, date.today())
    assert result['status'] == 'already_seeded'
    with closing(sqlite3.connect(path)) as conn:
        assert records(conn) == before
        assert conn.execute("SELECT order_id FROM refund_requests WHERE status='PENDING'").fetchall() == [('Order-A',)]
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert len(list(folder.glob('*.db'))) == 1
