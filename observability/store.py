"""Bounded background telemetry writer; request recording never opens SQLite."""
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import sqlite3
from threading import Event, Lock, Thread
import time
from uuid import UUID

from config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS request_events (
    attempt_id TEXT PRIMARY KEY, request_id TEXT, conversation_id TEXT,
    occurred_at TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    latency_ms REAL NOT NULL, queue_ms REAL NOT NULL, agent_ms REAL NOT NULL,
    model_ms REAL NOT NULL, tool_ms REAL NOT NULL,
    model_calls INTEGER NOT NULL, failed_model_calls INTEGER NOT NULL,
    tool_calls INTEGER NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    usage_complete INTEGER NOT NULL, model TEXT NOT NULL, deployment TEXT NOT NULL,
    estimated_cost_usd REAL, cost_complete INTEGER NOT NULL,
    pricing_source TEXT, input_per_million TEXT, output_per_million TEXT,
    details_json TEXT NOT NULL CHECK(json_valid(details_json))
);
CREATE INDEX IF NOT EXISTS ix_observability_time ON request_events(occurred_at);
CREATE TABLE IF NOT EXISTS pricing_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, deployment TEXT NOT NULL,
    effective_at TEXT NOT NULL, input_per_million TEXT NOT NULL,
    output_per_million TEXT NOT NULL, source_label TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_observability_rates
    ON pricing_rates(model,deployment,effective_at);
"""

MODES = frozenset({'customer', 'cashier', 'admin', 'unknown'})
STATUSES = frozenset({'success', 'error', 'cancelled', 'rejected', 'cache_hit'})
OUTCOMES = frozenset({'pass', 'blocked', 'error', 'unknown'})
TOOL_OUTCOMES = frozenset({'success', 'rejected', 'error'})
NUMBER_FIELDS = ('latency_ms', 'queue_ms', 'agent_ms', 'model_ms', 'tool_ms')
COUNT_FIELDS = ('model_calls', 'failed_model_calls', 'tool_calls', 'input_tokens', 'output_tokens')


def _integer_setting(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
        return max(minimum, min(value, maximum))
    except (TypeError, ValueError):
        return default


def enabled():
    return os.getenv('OBSERVABILITY_ENABLED', 'true').strip().lower() not in {'false', '0', 'no', 'off'}


def database_path():
    value = os.getenv('OBSERVABILITY_DB')
    path = Path(value) if value else settings.BILLING_DB.parent / 'observability.db'
    path = (settings.ROOT / path if not path.is_absolute() else path).resolve()
    if path in {settings.BILLING_DB.resolve(), settings.CONTEXT_DB.resolve()}:
        raise ValueError('The observability database must be separate from billing and chat databases.')
    return path


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (AttributeError, TypeError, ValueError, OverflowError):
        return utc_now()


def _uuid(value):
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except (ValueError, AttributeError, TypeError):
        return None


def _code(value, maximum=100):
    if (isinstance(value, str) and len(value) <= maximum
            and all(c.isascii() and (c.isalnum() or c in '_-.') for c in value)):
        return value
    return ''


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) and 0 <= value <= 86_400_000 else 0.0


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else 0


def sanitize(event):
    """Only operational scalars survive. Message bodies and arguments are ignored."""
    if not isinstance(event, dict) or not _uuid(event.get('attempt_id')):
        raise ValueError('Telemetry requires a unique attempt ID.')
    result = {
        'attempt_id': _uuid(event['attempt_id']),
        'request_id': _uuid(event.get('request_id')),
        'conversation_id': _uuid(event.get('conversation_id')),
        'occurred_at': _timestamp(event.get('occurred_at')),
        'mode': event.get('mode') if event.get('mode') in MODES else 'unknown',
        'status': event.get('status') if event.get('status') in STATUSES else 'error',
        'http_status': event.get('http_status') if type(event.get('http_status')) is int and 100 <= event['http_status'] <= 599 else 500,
        'usage_complete': event.get('usage_complete') is True,
        'model': _code(event.get('model'), 150),
        'deployment': _code(event.get('deployment'), 150),
    }
    result.update({key: _number(event.get(key)) for key in NUMBER_FIELDS})
    result.update({key: _count(event.get(key)) for key in COUNT_FIELDS})
    guardrails = []
    for item in (event.get('guardrails') if isinstance(event.get('guardrails'), list) else [])[:100]:
        if isinstance(item, dict) and _code(item.get('check')):
            guardrails.append({'check': _code(item['check']),
                'outcome': item.get('outcome') if item.get('outcome') in OUTCOMES else 'unknown',
                'code': _code(item.get('code')), 'count': _count(item.get('count')) or 1})
    tools = []
    for item in (event.get('tools') if isinstance(event.get('tools'), list) else [])[:100]:
        if isinstance(item, dict) and _code(item.get('name')):
            tools.append({'name': _code(item['name']),
                'outcome': item.get('outcome') if item.get('outcome') in TOOL_OUTCOMES else 'error',
                'latency_ms': _number(item.get('latency_ms')), 'mutating': item.get('mutating') is True,
                'dispatched': item.get('dispatched', True) is not False})
    if result['status'] == 'cache_hit':
        # A cached HTTP reply did not incur the original request's model usage again.
        result.update({key: 0 for key in COUNT_FIELDS})
        result['model_ms'] = result['tool_ms'] = 0.0
        result['usage_complete'] = True
        tools = []
    result['guardrails'], result['tools'] = guardrails, tools
    return result


def connect(readonly=True, path=None):
    path = Path(path).resolve() if path is not None else database_path()
    target = path.as_uri() + '?mode=ro' if readonly else str(path)
    conn = sqlite3.connect(target, uri=readonly, timeout=0.25)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=250')
    return conn


def initialize(path=None):
    path = Path(path).resolve() if path is not None else database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(readonly=False, path=path)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


class _Writer:
    def __init__(self, path):
        self.path = path
        self.queue = Queue(maxsize=_integer_setting('OBSERVABILITY_QUEUE_SIZE', 1000, 1, 10000))
        self.stopping = Event()
        self.ready = Event()
        self.lock = Lock()
        self.written = self.dropped = self.write_errors = 0
        self.state = 'starting'
        self.thread = Thread(target=self._run, name='billing-observability', daemon=True)

    def _run(self):
        try:
            initialize(self.path)
            self.state = 'ready'
            self.ready.set()
        except Exception:
            with self.lock:
                self.state = 'unavailable'
                self.write_errors += 1
                self.stopping.set()
                while True:
                    try:
                        self.queue.get_nowait()
                    except Empty:
                        break
                    self.dropped += 1
                    self.queue.task_done()
                self.ready.set()
            return
        next_prune = 0.0
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                first = self.queue.get(timeout=0.1)
            except Empty:
                continue
            batch = [first]
            for _ in range(49):
                try:
                    batch.append(self.queue.get_nowait())
                except Empty:
                    break
            try:
                from observability.pricing import price_event
                conn = connect(readonly=False, path=self.path)
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    valid = []
                    for event in batch:
                        try:
                            record = sanitize(event)
                            cost = price_event(conn, record)
                            details = json.dumps({'guardrails': record.pop('guardrails'), 'tools': record.pop('tools')}, separators=(',', ':'))
                            record.update(cost, details_json=details)
                            valid.append(record)
                        except (ValueError, TypeError, OverflowError):
                            self.dropped += 1
                    if valid:
                        fields = tuple(valid[0])
                        before_insert = conn.total_changes
                        conn.executemany('INSERT OR IGNORE INTO request_events (' + ','.join(fields) + ') VALUES ('
                            + ','.join(':' + key for key in fields) + ')', valid)
                        inserted = conn.total_changes-before_insert
                    else:
                        inserted = 0
                    if time.monotonic() >= next_prune:
                        cutoff = (datetime.now(timezone.utc) - timedelta(days=_integer_setting('OBSERVABILITY_RETENTION_DAYS', 90, 1, 3650))).isoformat(timespec='microseconds')
                        conn.execute('DELETE FROM request_events WHERE occurred_at<?', (cutoff,))
                        next_prune = time.monotonic() + 3600
                    conn.commit()
                    self.written += inserted
                    self.state = 'ready'
                finally:
                    conn.close()
            except Exception:
                self.write_errors += 1
                self.dropped += len(batch)
                self.state = 'degraded'
            finally:
                for _ in batch:
                    self.queue.task_done()
        self.state = 'stopped'


_writer = None
_manager_lock = Lock()
_start_errors = 0
_owners = 0


def start():
    """Start at API startup, never on a billing request's enqueue path."""
    global _writer, _start_errors, _owners
    if not enabled():
        return False
    try:
        path = database_path()
        with _manager_lock:
            if _writer is not None and _writer.thread.is_alive():
                with _writer.lock:
                    if _writer.path == path and not _writer.stopping.is_set():
                        _owners += 1
                        return True
                    _writer.stopping.set()
            _writer = _Writer(path)
            _owners = 1
            _start_errors = 0
            _writer.thread.start()
        return True
    except Exception:
        _start_errors += 1
        return False


def enqueue(event):
    """Best effort, nonblocking and without SQLite access or model calls."""
    try:
        writer = _writer
        if not enabled() or writer is None:
            return False
        if not writer.lock.acquire(blocking=False):
            writer.dropped += 1
            return False
        try:
            if not writer.thread.is_alive() or writer.stopping.is_set():
                writer.dropped += 1
                return False
            try:
                writer.queue.put_nowait(dict(event))
                return True
            except Full:
                writer.dropped += 1
                return False
        finally:
            writer.lock.release()
    except Exception:
        return False


def flush(timeout=2.0):
    """Testing/admin utility; never used to complete a billing request."""
    writer = _writer
    if writer is None:
        return True
    if writer.state == 'unavailable':
        return False
    deadline = time.monotonic() + max(0.0, timeout)
    while writer.queue.unfinished_tasks and time.monotonic() < deadline:
        if not writer.thread.is_alive():
            return False
        time.sleep(0.005)
    return not writer.queue.unfinished_tasks


def stop(timeout=1.0):
    global _owners
    with _manager_lock:
        writer = _writer
        _owners = max(0, _owners - 1)
        if _owners:
            return True
        if writer is not None:
            with writer.lock:
                writer.stopping.set()
    if writer is not None:
        writer.thread.join(timeout=max(0.0, timeout))
    return writer is None or not writer.thread.is_alive()


def health():
    writer = _writer
    return {'enabled': enabled(), 'state': ('disabled' if not enabled() else writer.state if writer else 'not_started'),
        'queued': writer.queue.qsize() if writer else 0,
        'dropped': writer.dropped if writer else 0,
        'write_errors': (writer.write_errors if writer else 0) + _start_errors,
        'written': writer.written if writer else 0}
