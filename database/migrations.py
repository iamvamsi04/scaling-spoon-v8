"""Remove retired financial and support storage, preserving populated legacy databases first."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import logging
import re
import sqlite3
from uuid import uuid4

from config import settings

RETIRED_TABLES = ('billing_case_events', 'billing_cases', 'billing_disputes', 'payments', 'credit_notes')
TEXT_COLLATION_BACKUP_DIR = settings.ROOT.parent / 'previous_versions/database_backups'

_DDL_TOKEN = re.compile(r'''--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_]\w*|[^\s]''')


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _ddl_name(value):
    if value.startswith(('"', '`', '[')):
        return value[1:-1].replace(value[0] * 2, value[0]).casefold()
    return value.casefold() if re.fullmatch(r'[A-Za-z_]\w*', value) else None


def _collation_definition(definition, table, selected, replacement):
    """Edit proven column declarations, not values or arbitrary query text."""
    tokens = [(m.group(), m.start(), m.end()) for m in _DDL_TOKEN.finditer(definition)
              if not m.group().startswith(('--', '/*'))]
    opening = next((i for i, token in enumerate(tokens) if token[0] == '('), None)
    if (opening is None or opening < 3 or [t[0].upper() for t in tokens[:2]] != ['CREATE', 'TABLE']
            or _ddl_name(tokens[opening - 1][0]) != table.casefold()):
        raise ValueError('Unrecognized table definition; text comparison migration stopped.')
    segments, start, depth, closing = [], opening + 1, 1, None
    for index in range(start, len(tokens)):
        value = tokens[index][0]
        if value == '(':
            depth += 1
        elif value == ')':
            depth -= 1
            if depth == 0:
                segments.append((start, index))
                closing = index
                break
        elif value == ',' and depth == 1:
            segments.append((start, index))
            start = index + 1
    if closing is None:
        raise ValueError('Unbalanced table definition; text comparison migration stopped.')
    edits, changed, check_binary, found = [], False, set(), set()
    constraints = {'NOT', 'NULL', 'PRIMARY', 'UNIQUE', 'CHECK', 'DEFAULT', 'COLLATE',
                   'REFERENCES', 'GENERATED', 'AS', 'CONSTRAINT'}
    for start, end in segments:
        name = _ddl_name(tokens[start][0]) if start < end else None
        if name not in selected:
            continue
        found.add(name)
        top, depth = [], 0
        for index in range(start + 1, end):
            value = tokens[index][0]
            if depth == 0:
                top.append(index)
            depth += (value == '(') - (value == ')')
        collation = next((i for i in top if tokens[i][0].upper() == 'COLLATE'), None)
        if collation is not None:
            current = _ddl_name(tokens[collation + 1][0])
            if current not in ('binary', 'nocase'):
                raise ValueError('A selected text column uses an unsupported collation; no records changed.')
            if current != 'nocase':
                edits.append((tokens[collation + 1][1], tokens[collation + 1][2], 'NOCASE'))
                changed = True
                check_binary.add(name)
        else:
            boundary = next((i for i in top if tokens[i][0].upper() in constraints), end)
            position = tokens[boundary - 1][2]
            edits.append((position, position, ' COLLATE NOCASE'))
            changed = True
            check_binary.add(name)
        if name in ('status', 'order_status', 'fulfillment_status'):
            check_binary.add(name)
    if not selected <= found:
        raise ValueError('Selected columns could not be resolved; no records changed.')
    # Adding a column collation must not weaken its old CHECK expressions.
    for index in range(opening + 1, closing):
        if tokens[index][0].upper() != 'CHECK' or tokens[index + 1][0] != '(':
            continue
        depth = 1
        for item in range(index + 2, closing):
            value = tokens[item][0]
            depth += (value == '(') - (value == ')')
            if depth == 0:
                break
            if (_ddl_name(value) in ('status', 'order_status', 'fulfillment_status')
                    and tokens[item + 1][0].upper() == 'COLLATE'
                    and _ddl_name(tokens[item + 2][0]) != 'binary'):
                raise ValueError('A status CHECK uses a non-binary comparison; no records changed.')
            if _ddl_name(value) in check_binary and tokens[item + 1][0].upper() != 'COLLATE':
                edits.append((tokens[item][2], tokens[item][2], ' COLLATE BINARY'))
                changed = True
    if not changed:
        return None
    edits.append((tokens[opening - 1][1], tokens[opening - 1][2], _quoted(replacement)))
    for start, end, value in sorted(set(edits), reverse=True):
        definition = definition[:start] + value + definition[end:]
    return definition


def _collation_backup(conn, backup_dir=None):
    path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
    if not path.is_file():
        raise ValueError('Cannot migrate populated text columns without a file backup.')
    folder = Path(backup_dir) if backup_dir else TEXT_COLLATION_BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = folder / f'{path.stem}-before-text-collations-{stamp}-{uuid4().hex[:8]}.db'
    # Reserve the new file exclusively; even a name collision cannot overwrite
    # an earlier recovery snapshot.
    with backup.open('xb'):
        pass
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Text comparison backup failed its integrity check.')
    return backup


def add_text_collations(conn, backup_dir=None):
    """Apply selected text collations atomically while preserving records and objects."""
    from database.connection import CASE_INSENSITIVE_COLUMNS

    def plans():
        result = []
        for table, definition in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
            selected = CASE_INSENSITIVE_COLUMNS.get(table.casefold())
            if not selected:
                continue
            columns = conn.execute('PRAGMA table_xinfo(' + _quoted(table) + ')').fetchall()
            present = {row[1].casefold() for row in columns}
            selected = selected & present
            if not selected:
                continue
            temporary = table + '__text_upgrade_' + uuid4().hex[:8]
            updated = _collation_definition(definition, table, selected, temporary)
            if updated:
                result.append((table, temporary, updated, columns, definition))
        return result

    if not plans():
        return None
    if conn.in_transaction:
        raise ValueError('Text comparison migration requires a separate transaction; pending changes were not committed.')
    foreign_keys = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    backup = None
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        conn.execute('BEGIN IMMEDIATE')
        upgrades = plans()
        if not upgrades:
            conn.commit()
            return None
        if any(conn.execute('SELECT 1 FROM ' + _quoted(table) + ' LIMIT 1').fetchone()
               for table, *_ in upgrades):
            backup = _collation_backup(conn, backup_dir)
        triggers = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL").fetchall()
        indexes = {table: conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                                      (table,)).fetchall() for table, *_ in upgrades}
        sequences = (dict(conn.execute('SELECT name,seq FROM sqlite_sequence').fetchall())
                     if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() else {})
        for name, _ in triggers:
            conn.execute('DROP TRIGGER ' + _quoted(name))
        for table, temporary, definition, columns, original in upgrades:
            conn.execute(definition)
            names = [row[1] for row in columns if row[6] == 0]
            # Preserve implicit rowids too, including gaps. A WITHOUT ROWID table
            # has only its declared primary key, which is copied normally.
            tail = original[original.rfind(')') + 1:]
            if not re.search(r'\bWITHOUT\s+ROWID\b', tail, re.IGNORECASE):
                alias = next((name for name in ('rowid', '_rowid_', 'oid')
                              if name not in {row[1].casefold() for row in columns}), None)
                if alias is None:
                    raise ValueError('Cannot preserve the implicit row identifier; no records changed.')
                names = [alias, *names]
            projection = ','.join(_quoted(name) for name in names)
            conn.execute(f'INSERT INTO {_quoted(temporary)} ({projection}) SELECT {projection} FROM {_quoted(table)}')
        for table, *_ in upgrades:
            conn.execute('DROP TABLE ' + _quoted(table))
        for table, temporary, *_ in upgrades:
            conn.execute(f'ALTER TABLE {_quoted(temporary)} RENAME TO {_quoted(table)}')
            if table in sequences:
                saved = conn.execute('UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?', (sequences[table], table))
                if not saved.rowcount:
                    conn.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)', (table, sequences[table]))
            for statement, in indexes[table]:
                conn.execute(statement)
        for _, statement in triggers:
            conn.execute(statement)
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Text comparison migration would leave broken relationships.')
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Text comparison migration failed its integrity check.')
        conn.commit()
        return str(backup) if backup else None
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute(f'PRAGMA foreign_keys={int(foreign_keys)}')


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
            conn.execute("ALTER TABLE orders ADD COLUMN order_status TEXT COLLATE NOCASE NOT NULL DEFAULT 'draft' "
                         "CHECK(order_status COLLATE BINARY IN ('draft','placed','cancelled'))")
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
