"""In-memory request observations; optional telemetry never governs billing."""
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from time import perf_counter
from uuid import UUID, uuid4
import asyncio
import os


_current = ContextVar('billing_observation', default=None)


def _enabled():
    return os.getenv('OBSERVABILITY_ENABLED', 'true').strip().lower() not in ('0', 'false', 'no', 'off')


def _identifier(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


class RequestObservation:
    def __init__(self):
        from config import settings
        self.started = perf_counter()
        self.queue_started = None
        self.finished = False
        self.guards = {}
        self.data = {
            'attempt_id':str(uuid4()), 'request_id':None, 'conversation_id':None,
            'occurred_at':datetime.now(timezone.utc).isoformat(), 'mode':'unknown',
            'status':'success', 'http_status':200, 'latency_ms':0.0,
            'queue_ms':0.0, 'agent_ms':0.0, 'model_ms':0.0, 'tool_ms':0.0,
            'model_calls':0, 'failed_model_calls':0, 'tool_calls':0,
            'input_tokens':0, 'output_tokens':0, 'usage_complete':True,
            'model':settings.AZURE_OPENAI_MODEL, 'deployment':settings.AZURE_OPENAI_DEPLOYMENT,
            'guardrails':[], 'tools':[],
        }
        self.guard('business_scope', 'unknown', 'not_independently_evaluated')

    def guard(self, check, outcome, code):
        key = (check, outcome, code)
        if key in self.guards:
            self.guards[key]['count'] += 1
        elif len(self.guards) < 100:
            self.guards[key] = {'check':check, 'outcome':outcome, 'code':code, 'count':1}

    def complete(self):
        if self.finished:
            return
        self.finished = True
        self.data['latency_ms'] = max(0.0, (perf_counter() - self.started) * 1000)
        if self.queue_started is not None:
            self.data['queue_ms'] += max(0.0, (perf_counter() - self.queue_started) * 1000)
        self.data['guardrails'] = list(self.guards.values())
        try:
            from observability import store
            store.enqueue(self.data)
        except Exception:
            pass


def _finish(observation):
    try:
        observation.complete()
    except Exception:
        pass


def _observe(fn):
    @wraps(fn)
    def safe(*args, **kwargs):
        try:
            observation = _current.get()
            if observation is not None and not observation.finished:
                return fn(observation, *args, **kwargs)
        except Exception:
            pass
    return safe


@_observe
def identify(observation, *, request_id=None, conversation_id=None, mode=None):
    if request_id is not None:
        observation.data['request_id'] = _identifier(request_id)
    if conversation_id is not None:
        observation.data['conversation_id'] = _identifier(conversation_id)
    if mode in ('customer', 'cashier', 'admin'):
        observation.data['mode'] = mode


@_observe
def guardrail(observation, check, outcome, code):
    if outcome in ('pass', 'blocked', 'error', 'unknown'):
        observation.guard(check, outcome, code)


@_observe
def queue_started(observation):
    observation.queue_started = perf_counter()


@_observe
def queue_finished(observation):
    if observation.queue_started is not None:
        observation.data['queue_ms'] += max(0.0, (perf_counter() - observation.queue_started) * 1000)
        observation.queue_started = None


@_observe
def cache_hit(observation):
    observation.data['status'] = 'cache_hit'


@_observe
def model_call(observation, seconds, *, input_tokens=None, output_tokens=None, failed=False):
    observation.data['model_calls'] += 1
    observation.data['model_ms'] += max(0.0, seconds * 1000)
    if failed:
        observation.data['failed_model_calls'] += 1
    for key, value in (('input_tokens', input_tokens), ('output_tokens', output_tokens)):
        if type(value) is int and value >= 0:
            observation.data[key] += value
        else:
            observation.data['usage_complete'] = False
    if input_tokens == 0 and output_tokens == 0:
        # Real chat requests have prompt tokens; adapters can default omitted
        # provider usage to zero. Do not present that as a free model call.
        observation.data['usage_complete'] = False


@_observe
def tool_call(observation, name, seconds, *, outcome, mutating):
    elapsed = max(0.0, seconds * 1000)
    observation.data['tool_calls'] += 1
    observation.data['tool_ms'] += elapsed
    if len(observation.data['tools']) < 100:
        observation.data['tools'].append({'name':name, 'latency_ms':elapsed,
                                         'outcome':outcome, 'mutating':bool(mutating), 'dispatched':True})


@_observe
def tool_blocked(observation, name, *, mutating=False):
    # The attempted action is visible without pretending an MCP call or write ran.
    if len(observation.data['tools']) < 100:
        observation.data['tools'].append({'name':name, 'latency_ms':0.0,
            'outcome':'rejected', 'mutating':bool(mutating), 'dispatched':False})


@_observe
def outcome(observation, status, http_status=None):
    if status in ('success', 'error', 'cancelled', 'rejected', 'cache_hit'):
        observation.data['status'] = status
    if type(http_status) is int and 100 <= http_status <= 599:
        observation.data['http_status'] = http_status


async def agent_call(awaitable):
    started = perf_counter()
    try:
        return await awaitable
    finally:
        try:
            observation = _current.get()
            if observation is not None:
                observation.data['agent_ms'] += max(0.0, (perf_counter() - started) * 1000)
        except Exception:
            pass


def observe_chat(fn):
    """Also support direct run_chat callers without an ASGI request context."""
    @wraps(fn)
    async def wrapped(body, *args, **kwargs):
        token = None
        observation = _current.get()
        if observation is None and _enabled():
            try:
                observation = RequestObservation()
                token = _current.set(observation)
            except Exception:
                observation = None
        identify(request_id=body.request_id, conversation_id=body.conversation_id)
        try:
            return await fn(body, *args, **kwargs)
        except BaseException as exc:
            status = getattr(exc, 'status_code', 500)
            if isinstance(exc, asyncio.CancelledError):
                outcome('cancelled', 499)
            else:
                outcome('rejected' if 400 <= status < 500 else 'error', status)
            raise
        finally:
            if token is not None:
                try:
                    _finish(observation)
                finally:
                    _current.reset(token)
    return wrapped


def start():
    try:
        from observability import store
        store.start()
    except Exception:
        pass


def stop():
    try:
        from observability import store
        store.stop(timeout=1.0)
    except Exception:
        pass


class ObservabilityMiddleware:
    """Finalize once after the full ASGI response, including stream errors."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get('type') != 'http' or scope.get('method') != 'POST'
                or scope.get('path') not in ('/chat', '/chat/stream') or not _enabled()):
            return await self.app(scope, receive, send)
        try:
            observation = RequestObservation()
            token = _current.set(observation)
        except Exception:
            return await self.app(scope, receive, send)

        async def observed_send(message):
            try:
                if message.get('type') == 'http.response.start':
                    status = message['status']
                    if observation.data['status'] not in ('error', 'cancelled', 'rejected'):
                        observation.data['http_status'] = status
                        if status >= 400:
                            observation.data['status'] = 'rejected' if status < 500 else 'error'
                    if status in (401, 403):
                        guardrail('api_access', 'blocked', 'http_access_denied')
                    elif status == 422:
                        guardrail('request_validation', 'blocked', 'invalid_request')
            except Exception:
                pass
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except BaseException as exc:
            outcome('cancelled' if isinstance(exc, asyncio.CancelledError) else 'error',
                    499 if isinstance(exc, asyncio.CancelledError) else 500)
            raise
        finally:
            try:
                _finish(observation)
            finally:
                _current.reset(token)
