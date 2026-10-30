import json
import math
import re
import sqlite3
from datetime import date
from contextlib import contextmanager
from uuid import uuid4
from config import settings
from context.tables import sanitize_tables
from context.reports import sanitize_reports


@contextmanager
def connection():
    conn = sqlite3.connect(settings.CONTEXT_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize():
    settings.CONTEXT_DB.parent.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY, customer_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
            request_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done', response_json TEXT,
            may_have_changed INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(conversation_id,request_id,role));
        CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages(conversation_id,id);
        CREATE TABLE IF NOT EXISTS shopping_states (
            conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
            state_json TEXT NOT NULL CHECK(json_valid(state_json)),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        columns = {r['name'] for r in conn.execute('PRAGMA table_info(conversations)')}
        if 'mode' not in columns:
            # Older transcripts may contain store-wide data; require admin to resume them.
            conn.execute("ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'admin'")
        columns = {r['name'] for r in conn.execute('PRAGMA table_info(messages)')}
        if 'may_have_changed' not in columns:
            # Existing failures have no dispatch evidence and cannot be replayed.
            conn.execute("ALTER TABLE messages ADD COLUMN may_have_changed INTEGER NOT NULL DEFAULT 1")


def conversation(conversation_id=None, customer_id=None, mode='customer'):
    with connection() as conn:
        if conversation_id:
            row = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if not row:
                raise ValueError("Conversation not found.")
            if customer_id != row["customer_id"]:
                raise ValueError("Customer association cannot change. Start a new conversation.")
            return conversation_id
        conversation_id = str(uuid4())
        conn.execute("INSERT INTO conversations(conversation_id,customer_id,mode) VALUES (?,?,?)", (conversation_id, customer_id, mode))
        return conversation_id


def get_conversation(conversation_id):
    with connection() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        return dict(row) if row else None


def _analytics_filter(key, value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Invalid analytics scope.')
    if key in ('date_from', 'date_to'):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError('Invalid analytics date.')
    elif not re.fullmatch(r'[A-Za-z0-9_-]{1,50}', value):
        raise ValueError('Invalid analytics account or order.')
    return value


def sanitize_analytics_context(value):
    """Keep read recipes, never saved business values or a claimed permission.

    These recipes are only conversation context. A follow-up must execute a new
    authorized read before presenting figures or charts.
    """
    filters = ('customer_id', 'order_id', 'date_from', 'date_to')
    if not isinstance(value, dict):
        return None
    try:
        if len(json.dumps(value).encode('utf-8')) > 250_000:
            return None
        kind = value.get('kind')
        if kind == 'store_overview':
            if set(value) - {'kind', 'filters', 'chart_mode'}:
                return None
            raw = value.get('filters')
            if not isinstance(raw, dict) or set(raw) != set(filters):
                return None
            result = {'kind': kind, 'filters': {key: _analytics_filter(key, raw[key]) for key in filters}}
            if value.get('chart_mode') not in (None, 'overview', 'dashboard', 'all', 'none'):
                return None
            if value.get('chart_mode') is not None:
                result['chart_mode'] = value['chart_mode']
            selected = result['filters']
        elif kind in ('invoice_trend', 'billing_analysis'):
            if set(value) != {'kind', 'arguments'} or not isinstance(value.get('arguments'), dict):
                return None
            arguments = value['arguments']
            allowed = set(filters) | {'report', 'top_n', 'trend_period', 'trend_metric', 'chart_type', 'chart_mode'}
            if set(arguments) - allowed:
                return None
            clean = {}
            for key, item in arguments.items():
                if key in filters:
                    clean[key] = _analytics_filter(key, item)
                elif key == 'top_n':
                    if type(item) is not int or not 1 <= item <= 100:
                        return None
                    clean[key] = item
                elif item is None or isinstance(item, str) and 0 < len(item) <= 100:
                    clean[key] = item
                else:
                    return None
            if clean.get('chart_mode') not in (None, 'overview', 'dashboard', 'all', 'none'):
                return None
            result = {'kind': kind, 'arguments': clean}
            selected = clean
        elif kind == 'sql':
            if set(value) != {'kind', 'sources'} or not isinstance(value.get('sources'), list) or not value['sources']:
                return None
            sources = []
            for source in value['sources']:
                if not isinstance(source, dict) or set(source) != {'query', 'parameters', 'columns'}:
                    return None
                query, parameters, columns = (source[key] for key in ('query', 'parameters', 'columns'))
                if not isinstance(query, str) or not query.strip() or len(query) > 20_000:
                    return None
                if not isinstance(parameters, list) or len(parameters) > 1000:
                    return None
                if any(item is not None and (not isinstance(item, (str, int, float))
                       or isinstance(item, float) and not math.isfinite(item)) for item in parameters):
                    return None
                if not isinstance(columns, list) or not 1 <= len(columns) <= 100 or any(
                        not isinstance(column, str) or not column or len(column) > 100 for column in columns):
                    return None
                if len(set(columns)) != len(columns):
                    return None
                sources.append({'query': query, 'parameters': parameters, 'columns': columns})
            return {'kind': kind, 'sources': sources}
        else:
            return None
        if (selected.get('date_from') or '') > (selected.get('date_to') or '9999-12-31'):
            return None
        return result
    except (ValueError, TypeError, OverflowError):
        return None


def latest_analytics_context(conversation_id):
    """Read the latest completed admin reply's recipe, within this chat only."""
    with connection() as conn:
        row = conn.execute('''SELECT m.response_json FROM messages m
            JOIN conversations c ON c.conversation_id=m.conversation_id
            WHERE c.conversation_id=? AND c.mode='admin' AND m.role='user'
            AND m.status='done' ORDER BY m.id DESC LIMIT 1''', (conversation_id,)).fetchone()
    if not row or not row['response_json']:
        return None
    try:
        response = json.loads(row['response_json'])
    except (ValueError, TypeError):
        return None
    return sanitize_analytics_context(response.get('analytics_context')) if isinstance(response, dict) else None


def latest_action_context(conversation_id, exclude_request_id=None):
    """Read only the latest trusted staff approval, never an older proposal."""
    with connection() as conn:
        row = conn.execute('''SELECT CASE WHEN json_valid(m.response_json)
            THEN json_extract(m.response_json,'$.pending_staff_action') END AS proposal,m.status
            FROM messages m JOIN conversations c ON c.conversation_id=m.conversation_id
            WHERE c.conversation_id=? AND c.mode IN ('admin','cashier')
            AND m.role='user' AND (? IS NULL OR m.request_id<>?) ORDER BY m.id DESC LIMIT 1''',
            (conversation_id,exclude_request_id,exclude_request_id)).fetchone()
    if not row or row['status'] != 'done' or not isinstance(row['proposal'], str) or len(row['proposal']) > 12000:
        return None
    try:
        value = json.loads(row['proposal'])
    except (ValueError, TypeError):
        return None
    fields = {'kind','action','order_id','order_fingerprint','arguments'}
    if (not isinstance(value,dict) or set(value) != fields
            or value['kind'] not in ('staff_refund','staff_action')
            or value['action'] not in ('approve','escalate','ship','deliver')
            or not isinstance(value['arguments'],dict)):
        return None
    return value


def shopping_state(conversation_id):
    from context.shopping_state import empty_state
    with connection() as conn:
        row = conn.execute('SELECT state_json FROM shopping_states WHERE conversation_id=?',
                           (conversation_id,)).fetchone()
        defaults = empty_state()
        saved = json.loads(row[0]) if row else {}
        return {**defaults, **{key: value for key, value in saved.items() if key in defaults}}


def write_shopping_state(conn, conversation_id, state):
    conn.execute('''INSERT INTO shopping_states(conversation_id,state_json) VALUES (?,?)
        ON CONFLICT(conversation_id) DO UPDATE SET state_json=excluded.state_json,
        updated_at=CURRENT_TIMESTAMP''', (conversation_id, json.dumps(state)))


def checkpoint_shopping(conversation_id, request_id, state, customer_id):
    """Persist observed tool facts even if a later model call fails or times out."""
    with connection() as conn:
        record = conn.execute('''SELECT c.customer_id FROM conversations c JOIN messages m
            ON m.conversation_id=c.conversation_id WHERE c.conversation_id=? AND c.mode='customer'
            AND m.request_id=? AND m.role='user' AND m.status='pending' ''',
            (conversation_id, request_id)).fetchone()
        if record is None or record['customer_id'] != customer_id:
            raise ValueError('Shopping checkpoint does not match the active conversation.')
        write_shopping_state(conn, conversation_id, state)


def history(conversation_id, include_artifacts=False):
    with connection() as conn:
        if include_artifacts:
            rows = conn.execute("SELECT m.role,m.content,u.response_json FROM messages m LEFT JOIN messages u "
                                "ON m.role='assistant' AND u.role='user' AND u.conversation_id=m.conversation_id "
                                "AND u.request_id=m.request_id WHERE m.conversation_id=? "
                                "AND m.status='done' ORDER BY m.id DESC LIMIT 40", (conversation_id,)).fetchall()
        else:
            # Model history needs transcript text only. Saved responses can hold
            # complete reports and charts; do not read those blobs on each turn.
            rows = conn.execute("SELECT role,content FROM messages WHERE conversation_id=? "
                                "AND status='done' ORDER BY id DESC LIMIT 40", (conversation_id,)).fetchall()
        result = []
        for row in reversed(rows):
            message = {'role':row['role'],'content':row['content']}
            if include_artifacts and row['role']=='assistant' and row['response_json']:
                saved = json.loads(row['response_json'])
                message.update(charts=saved.get('charts',[]),invoices=saved.get('invoices',[]))
                message['tables'] = sanitize_tables(saved.get('tables', []))
                message['reports'] = sanitize_reports(saved.get('reports', []))
            result.append(message)
        return result


def completed_response(conversation_id, request_id, message):
    """Read an immutable completed reply without starting or retrying a turn.

    Callers must verify conversation access first. Pending/failed requests remain
    subject to begin() under the execution lock; they never return a cached reply.
    """
    with connection() as conn:
        row = conn.execute("SELECT content,status,response_json FROM messages WHERE "
                           "conversation_id=? AND request_id=? AND role='user'",
                           (conversation_id, request_id)).fetchone()
    if row is None:
        return None
    if row['content'] != message:
        raise ValueError('Request ID was already used with different content.')
    if row['status'] == 'done' and row['response_json']:
        return json.loads(row['response_json'])
    return None


def begin(conversation_id, request_id, message, may_have_changed=True):
    with connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT * FROM messages WHERE conversation_id=? AND request_id=? AND role='user'",
                           (conversation_id, request_id)).fetchone()
        if row:
            if row["content"] != message:
                raise ValueError("Request ID was already used with different content.")
            if row['status'] == 'done' and row["response_json"]:
                return json.loads(row["response_json"])
            newer = conn.execute("SELECT 1 FROM messages WHERE conversation_id=? AND role='user' AND id>? LIMIT 1",
                                 (conversation_id, row['id'])).fetchone()
            if row['status'] == 'failed' and row['may_have_changed'] == 0 and not newer:
                conn.execute("UPDATE messages SET status='pending',may_have_changed=? WHERE id=?",
                             (int(bool(may_have_changed)), row['id']))
                return None
            raise ValueError("This turn is pending or failed. Check order state before sending a new mutation.")
        conn.execute("INSERT INTO messages(conversation_id,request_id,role,content,status,may_have_changed) "
                     "VALUES (?,?,'user',?,'pending',?)",
                     (conversation_id, request_id, message, int(bool(may_have_changed))))
        return None


def mark_request_changed(conversation_id, request_id):
    """Persist uncertainty before dispatching a change, never after its result."""
    with connection() as conn:
        changed = conn.execute("UPDATE messages SET may_have_changed=1,response_json=NULL WHERE conversation_id=? "
                               "AND request_id=? AND role='user' AND status='pending'",
                               (conversation_id, request_id))
        if changed.rowcount != 1:
            raise ValueError('The billing change does not match an active request.')


def customer_turn_state(conversation_id, request_id, customer_id):
    """Expire global approval while retaining input for this request's safe retry.

    begin() must first admit the request. It alone permits reopening a failed
    read with the same message and no newer turn; pending or uncertain writes
    cannot reach this input again. Never copy it into a different request.
    """
    from context.shopping_state import clear_confirmation, empty_state
    with connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        record = conn.execute('''SELECT m.response_json FROM messages m
            JOIN conversations c ON c.conversation_id=m.conversation_id
            WHERE c.conversation_id=? AND c.mode='customer' AND c.customer_id IS ?
              AND m.request_id=? AND m.role='user' AND m.status='pending' ''',
            (conversation_id, customer_id, request_id)).fetchone()
        if record is None:
            raise ValueError('Shopping input does not match the active customer request.')
        saved = json.loads(record['response_json']) if record['response_json'] else None
        if isinstance(saved, dict) and set(saved) == {'shopping_input'} and isinstance(saved['shopping_input'], dict):
            state = saved['shopping_input']
        else:
            row = conn.execute('SELECT state_json FROM shopping_states WHERE conversation_id=?',
                               (conversation_id,)).fetchone()
            defaults = empty_state()
            current = json.loads(row[0]) if row else {}
            state = {**defaults, **{key:value for key,value in current.items() if key in defaults}}
            conn.execute("UPDATE messages SET response_json=? WHERE conversation_id=? "
                         "AND request_id=? AND role='user' AND status='pending'",
                         (json.dumps({'shopping_input':state}), conversation_id, request_id))
        write_shopping_state(conn, conversation_id, clear_confirmation(state))
        return state


def finish(conversation_id, request_id, response):
    with connection() as conn:
        if 'shopping_state' in response:
            record = conn.execute('SELECT mode FROM conversations WHERE conversation_id=?',
                                  (conversation_id,)).fetchone()
            if not record or record['mode'] != 'customer':
                raise ValueError('Only customer conversations have shopping state.')
            write_shopping_state(conn, conversation_id, response['shopping_state'])
        conn.execute("UPDATE messages SET status='done',response_json=? WHERE conversation_id=? AND request_id=? AND role='user'",
                     (json.dumps(response), conversation_id, request_id))
        conn.execute("INSERT INTO messages(conversation_id,request_id,role,content) VALUES (?,?,'assistant',?)",
                     (conversation_id, request_id, response["response"]))


def fail(conversation_id, request_id, before_execution=False):
    with connection() as conn:
        conn.execute("UPDATE messages SET status='failed',may_have_changed=CASE WHEN ? THEN 0 ELSE may_have_changed END "
                     "WHERE conversation_id=? AND request_id=? AND role='user' AND status='pending'",
                     (int(bool(before_execution)), conversation_id, request_id))


def discard_pending(conversation_id, request_id):
    """Only used for configuration rejection before any model/tool execution."""
    with connection() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id=? AND request_id=? "
                     "AND role='user' AND status='pending'", (conversation_id, request_id))
