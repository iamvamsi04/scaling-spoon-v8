"""Remove retired financial and support storage, preserving populated legacy databases first."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import logging
import sqlite3
from uuid import uuid4

from config import settings

RETIRED_TABLES = ('billing_case_events', 'billing_cases', 'billing_disputes', 'payments', 'credit_notes')


def remove_retired_tables(conn, backup_dir=None):
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    retired = [name for name in RETIRED_TABLES if name in existing]
    if not retired:
        return None
    conn.execute('BEGIN IMMEDIATE')
    backup = None
    try:
        if any(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in retired):
            path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
            if not path.is_file():
                raise ValueError('Cannot remove populated legacy tables without a file backup.')
            folder = Path(backup_dir) if backup_dir else settings.ROOT.parent / 'previous_versions/database_backups'
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = folder / f'{path.stem}-before-table-cleanup-{stamp}-{uuid4().hex[:8]}.db'
            # A separate read connection can snapshot the committed database while
            # this connection holds the reserved write lock, before any drops.
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
                    if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Legacy backup failed its integrity check.')
            logging.getLogger(__name__).warning('Retired records preserved in %s', backup)
        for table in retired:
            conn.execute(f'DROP TABLE {table}')
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Table cleanup would leave broken references.')
        conn.commit()
        return str(backup) if backup else None
    except BaseException:
        conn.rollback()
        raise


def remove_views(conn):
    """Drop stored query definitions atomically; underlying records are preserved."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")]
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            conn.execute('DROP VIEW ' + quoted)
        conn.commit()
        return names
    except BaseException:
        conn.rollback()
        raise


def add_promotion_expiry(conn):
    """Legacy codes stay stored but expire until an operator assigns a real date."""
    if 'expires_on' not in {row[1] for row in conn.execute('PRAGMA table_info(promotions)')}:
        conn.execute("ALTER TABLE promotions ADD COLUMN expires_on DATE NOT NULL DEFAULT '1970-01-01' "
                     "CHECK(length(expires_on)=10 AND expires_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
                     "AND date(expires_on,'+0 days') IS NOT NULL AND date(expires_on,'+0 days')=expires_on)")


def add_unknown_fulfillment(conn):
    """Preserve service records while allowing unknown legacy shipment status.

    Rebuild only the affected CHECK constraint; the savepoint makes a failure
    atomic and also works inside an existing seed transaction.
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='order_service'").fetchone()
    if not row or "'unknown'" in row[0]:
        return
    definition = row[0]
    updated = definition.replace("DEFAULT 'placed'", "DEFAULT 'unknown'").replace(
        "IN ('placed','shipped','delivered','cancelled')",
        "IN ('unknown','placed','shipped','delivered','cancelled')")
    if updated == definition or "'unknown'" not in updated:
        raise ValueError('Unrecognized order_service schema; migration stopped without changing records.')
    import re
    updated = re.sub(r'(?i)CREATE TABLE\s+(?:IF NOT EXISTS\s+)?["`\[]?order_service["`\]]?',
                     'CREATE TABLE order_service_upgrade', updated, count=1)
    conn.execute('SAVEPOINT service_upgrade')
    try:
        objects = conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name='order_service' "
                               "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
        conn.execute(updated)
        columns = ','.join('"'+r[1].replace('"','""')+'"' for r in conn.execute('PRAGMA table_info(order_service)'))
        conn.execute(f'INSERT INTO order_service_upgrade ({columns}) SELECT {columns} FROM order_service')
        conn.execute('DROP TABLE order_service')
        conn.execute('ALTER TABLE order_service_upgrade RENAME TO order_service')
        for obj in objects:
            conn.execute(obj[0])
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Service upgrade would leave broken references.')
        conn.execute('RELEASE service_upgrade')
    except BaseException:
        conn.execute('ROLLBACK TO service_upgrade')
        conn.execute('RELEASE service_upgrade')
        raise


def add_order_status(conn):
    """Keep one explicit order lifecycle, preserving invoices and cancellations.

    Invoice triggers also support existing imports that supply an invoice without
    a status. An empty invoice is never evidence of a placed order.
    """
    conn.execute('SAVEPOINT order_status_upgrade')
    try:
        if 'order_status' not in {row[1] for row in conn.execute('PRAGMA table_info(orders)')}:
            conn.execute("ALTER TABLE orders ADD COLUMN order_status TEXT NOT NULL DEFAULT 'draft' "
                         "CHECK(order_status IN ('draft','placed','cancelled'))")
        nonempty = "length(trim(COALESCE(order_invoice,''),char(9)||char(10)||char(13)||' '))>0"
        conn.execute("UPDATE orders SET order_invoice=NULL WHERE order_invoice IS NOT NULL AND NOT (" + nonempty + ")")
        state = ("CASE WHEN order_status='cancelled' "
                 "OR EXISTS(SELECT 1 FROM order_details d WHERE d.order_id=orders.order_id AND d.status='cancelled') "
                 "OR EXISTS(SELECT 1 FROM order_service s WHERE s.order_id=orders.order_id AND s.fulfillment_status='cancelled') "
                 "THEN 'cancelled' WHEN " + nonempty + " THEN 'placed' ELSE 'draft' END")
        conn.execute("UPDATE orders SET order_status=" + state + " WHERE order_status!=(" + state + ")")
        conn.execute('CREATE INDEX IF NOT EXISTS ix_orders_status_customer ON orders(order_status,customer_id,order_date)')
        invoice_new = "length(trim(COALESCE(NEW.order_invoice,''),char(9)||char(10)||char(13)||' '))>0"
        invoice_old = "length(trim(COALESCE(OLD.order_invoice,''),char(9)||char(10)||char(13)||' '))>0"
        triggers = [
            "CREATE TRIGGER IF NOT EXISTS order_status_insert_guard BEFORE INSERT ON orders "
            "WHEN NEW.order_status='placed' AND NOT (" + invoice_new + ") "
            "BEGIN SELECT RAISE(ABORT,'Placed order requires an issued invoice'); END",
            "CREATE TRIGGER IF NOT EXISTS order_status_update_guard BEFORE UPDATE OF order_status ON orders "
            "WHEN (OLD.order_status='cancelled' AND NEW.order_status!='cancelled') "
            "OR (OLD.order_status='placed' AND NEW.order_status='draft') "
            "OR (NEW.order_status='placed' AND NOT (" + invoice_new + ")) "
            "BEGIN SELECT RAISE(ABORT,'Invalid order lifecycle transition'); END",
            "CREATE TRIGGER IF NOT EXISTS order_invoice_identity_guard BEFORE UPDATE OF order_invoice ON orders "
            "WHEN (OLD.order_status='cancelled' OR (OLD.order_status='placed' AND (" + invoice_old + "))) "
            "AND NEW.order_invoice IS NOT OLD.order_invoice "
            "BEGIN SELECT RAISE(ABORT,'Issued invoice identity cannot change'); END",
            "CREATE TRIGGER IF NOT EXISTS order_invoice_insert_state AFTER INSERT ON orders "
            "WHEN NEW.order_status='draft' AND (" + invoice_new + ") "
            "BEGIN UPDATE orders SET order_status='placed' WHERE order_id=NEW.order_id; END",
            "CREATE TRIGGER IF NOT EXISTS order_invoice_update_state AFTER UPDATE OF order_invoice ON orders "
            "WHEN NEW.order_status='draft' AND (" + invoice_new + ") "
            "BEGIN UPDATE orders SET order_status='placed' WHERE order_id=NEW.order_id; END",
        ]
        for table, column in (('order_details', 'status'), ('order_service', 'fulfillment_status')):
            for event in ('INSERT', 'UPDATE OF ' + column):
                suffix = 'insert' if event == 'INSERT' else 'update'
                triggers.append(f'CREATE TRIGGER IF NOT EXISTS {table}_cancel_{suffix} AFTER {event} ON {table} '
                                f"WHEN NEW.{column}='cancelled' BEGIN UPDATE orders SET order_status='cancelled' "
                                'WHERE order_id=NEW.order_id; END')
        for statement in triggers:
            conn.execute(statement)
        conn.execute('RELEASE order_status_upgrade')
    except BaseException:
        conn.execute('ROLLBACK TO order_status_upgrade')
        conn.execute('RELEASE order_status_upgrade')
        raise
