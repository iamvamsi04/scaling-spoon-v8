import asyncio
import hmac
import logging
import json
from time import perf_counter
from contextlib import asynccontextmanager, closing
from uuid import UUID
from typing import Literal
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from openai import APIConnectionError, APITimeoutError, APIStatusError
from config import settings
from context import context_manager as context
from context.reply_text import plain_reply
from context.tables import sanitize_tables
from context.reports import sanitize_reports
from database.connection import initialize as initialize_billing
from database.connection import connect
from database.invoice import invoice_path
from api import customer_auth

log = logging.getLogger(__name__)


def failure_detail(exc):
    """Classify failures without exposing SDK bodies, URLs or credentials."""
    if isinstance(exc, (TimeoutError, APITimeoutError)):
        return 'request_timeout', 'The request exceeded a timeout while waiting for the agent or Azure OpenAI.'
    if isinstance(exc, APIConnectionError):
        return 'azure_connection_failed', ('Unable to establish a connection to Azure OpenAI. '
            'Check the endpoint hostname, network, VPN, proxy, firewall and certificate configuration.')
    if isinstance(exc, APIStatusError):
        messages = {
            400: 'Azure rejected the model request. Check deployment capabilities and API version.',
            401: 'Azure rejected authentication. Check the configured API key and resource.',
            403: 'Azure denied access. Check resource permissions and network access rules.',
            404: 'Azure could not find the requested resource. Check the endpoint, deployment and API version.',
            429: 'Azure rate or quota limits were reached. Check deployment capacity.',
        }
        return 'azure_http_error', messages.get(exc.status_code, 'Azure returned a service error.')
    return 'agent_failed', 'The billing agent encountered an internal error. Check the server traceback for this request.'


async def authorize(x_api_key: str | None = Header(default=None)):
    if settings.API_KEY and not hmac.compare_digest((x_api_key or "").encode(), settings.API_KEY.encode()):
        raise HTTPException(401, "Invalid API key.")


def require_admin(key):
    configured = settings.ADMIN_API_KEY
    if len(configured) < 16 or configured in (settings.API_KEY, settings.CASHIER_API_KEY):
        raise HTTPException(503, 'Set a distinct BILLING_ADMIN_API_KEY of at least 16 characters and restart the API.')
    if not hmac.compare_digest((key or '').encode(), configured.encode()):
        raise HTTPException(403, 'Admin access denied.')


def require_cashier(key, admin_key=None):
    if admin_key:
        require_admin(admin_key)
        return
    configured = settings.CASHIER_API_KEY
    if len(configured) < 16 or configured in (settings.API_KEY, settings.ADMIN_API_KEY):
        raise HTTPException(503, 'Set a distinct BILLING_CASHIER_API_KEY of at least 16 characters and restart the API.')
    if not hmac.compare_digest((key or '').encode(), configured.encode()):
        raise HTTPException(403, 'Cashier access denied.')


def require_customer(customer_id, token):
    if customer_auth.customer_for_token(token) != customer_id:
        raise HTTPException(401, 'Sign in to the matching customer account.')


def check_record(record, admin_key, cashier_key=None, customer_token=None):
    if not record:
        raise HTTPException(404, 'Conversation not found.')
    if record['mode'] == 'admin':
        require_admin(admin_key)
    elif record['mode'] == 'cashier':
        require_cashier(cashier_key, admin_key)
    elif record['customer_id']:
        require_customer(record['customer_id'], customer_token)


@asynccontextmanager
async def lifespan(app):
    initialize_billing()
    context.initialize()
    app.state.chat_lock = asyncio.Lock()
    if not hasattr(app.state, "engine"):
        from agent.billing_agent import BillingAgent
        app.state.engine = BillingAgent()
    start = getattr(app.state.engine, "start", None)
    close = getattr(app.state.engine, "close", None)
    try:
        if start:
            await start()
        yield
    finally:
        if close:
            await close()


app = FastAPI(title="Billing Agent", version="7.0.0", lifespan=lifespan,
              dependencies=[Depends(authorize)])


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    if request.url.path.startswith(('/customers/addresses', '/customers/orders/')):
        return JSONResponse(status_code=422, content={'detail': 'Check the required delivery address fields and their lengths.'})
    if request.url.path.startswith('/customers/'):
        return JSONResponse(status_code=422, content={'detail': 'Check the customer ID, account details and password length (10 to 128 characters).'})
    # Do not echo user-supplied values in error responses.
    errors = [{key: value for key, value in error.items() if key not in ('input', 'ctx')}
              for error in exc.errors()]
    return JSONResponse(status_code=422, content={'detail': errors})


class ConversationRequest(BaseModel):
    customer_id: str | None = Field(default=None, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    mode: Literal['customer', 'cashier', 'admin'] = 'customer'


class ChatRequest(BaseModel):
    conversation_id: UUID
    request_id: UUID
    message: str = Field(min_length=1, max_length=10000)


class CustomerLogin(BaseModel):
    customer_id: str = Field(min_length=1, max_length=50, pattern=r'^[A-Za-z0-9_-]+$')
    password: str = Field(min_length=10, max_length=128, repr=False, exclude=True)


class CustomerRegistration(CustomerLogin):
    name: str = Field(min_length=1, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=200)


@app.post('/customers/register', status_code=201)
def register_customer(body: CustomerRegistration):
    try:
        return JSONResponse(customer_auth.register(body.customer_id, body.name, body.password, body.company, body.email),
                            status_code=201, headers={'Cache-Control': 'no-store'})
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/customers/login')
def login_customer(body: CustomerLogin):
    try:
        session = customer_auth.login(body.customer_id, body.password)
    except ValueError:
        session = None
    if not session:
        raise HTTPException(401, 'Unable to sign in. Check your credentials or contact store staff.')
    return JSONResponse(session, headers={'Cache-Control': 'no-store'})


@app.post('/customers/logout')
def logout_customer(x_customer_token: str | None = Header(default=None)):
    customer_auth.logout(x_customer_token)
    return {'signed_out': True}


@app.get('/customers/session')
def customer_session(x_customer_token: str | None = Header(default=None)):
    session = customer_auth.session_for_token(x_customer_token)
    if not session:
        raise HTTPException(401, 'Your customer session ended. Please sign in again.', headers={'Cache-Control': 'no-store'})
    return JSONResponse(session, headers={'Cache-Control': 'no-store'})


class DeliveryAddress(BaseModel):
    model_config = ConfigDict(extra='forbid')
    full_name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=1, max_length=30)
    address_line1: str = Field(min_length=1, max_length=200)
    address_line2: str = Field(default='', max_length=200)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=100)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(min_length=2, max_length=2)
    label: str = Field(default='', max_length=50)
    landmark: str = Field(default='', max_length=200)
    is_default: bool = Field(default=False, strict=True)


class DeliveryAddressSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    address_id: str = Field(min_length=1, max_length=50, pattern=r'^[A-Za-z0-9_-]+$')


def address_customer(token):
    customer_id = customer_auth.customer_for_token(token)
    if not customer_id:
        raise HTTPException(401, 'Sign in to manage your delivery addresses.', headers={'Cache-Control': 'no-store'})
    return customer_id


def address_result(operation, *args, status_code=200, **kwargs):
    try:
        result = operation(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(409, str(exc), headers={'Cache-Control': 'no-store'}) from exc
    return JSONResponse(result, status_code=status_code, headers={'Cache-Control': 'no-store'})


@app.get('/customers/addresses')
def customer_addresses(x_customer_token: str | None = Header(default=None)):
    from database.addresses import list_addresses
    customer_id = address_customer(x_customer_token)
    return JSONResponse({'addresses': list_addresses(customer_id)}, headers={'Cache-Control': 'no-store'})


@app.post('/customers/addresses', status_code=201)
def add_delivery_address(body: DeliveryAddress, x_customer_token: str | None = Header(default=None)):
    from database.addresses import save_address
    return address_result(lambda: {'address': save_address(address_customer(x_customer_token), body.model_dump())}, status_code=201)


@app.put('/customers/addresses/{address_id}')
def update_delivery_address(address_id: str, body: DeliveryAddress, x_customer_token: str | None = Header(default=None)):
    from database.addresses import save_address
    return address_result(lambda: {'address': save_address(address_customer(x_customer_token), body.model_dump(), address_id=address_id)})


@app.delete('/customers/addresses/{address_id}')
def remove_delivery_address(address_id: str, x_customer_token: str | None = Header(default=None)):
    from database.addresses import delete_address
    return address_result(delete_address, address_customer(x_customer_token), address_id)


@app.post('/customers/addresses/{address_id}/default')
def default_delivery_address(address_id: str, x_customer_token: str | None = Header(default=None)):
    from database.addresses import set_default_address
    return address_result(lambda: {'address': set_default_address(address_customer(x_customer_token), address_id)})


@app.put('/customers/orders/{order_id}/delivery-address')
def select_delivery_address(order_id: str, body: DeliveryAddressSelection, x_customer_token: str | None = Header(default=None)):
    from database.addresses import select_order_address
    return address_result(select_order_address, order_id, address_customer(x_customer_token), body.address_id)


@app.get('/customers/checkout')
def customer_checkout(x_customer_token: str | None = Header(default=None)):
    from database.addresses import list_addresses
    customer_id = address_customer(x_customer_token)
    with closing(connect(readonly=True)) as conn:
        conn.execute('BEGIN')
        rows = conn.execute('''SELECT o.order_id,o.charged_price,
            COALESCE(d.currency,(SELECT value FROM billing_config WHERE key='currency'),'USD') AS currency,
            delivery.address_id,delivery.address_snapshot
            FROM orders o LEFT JOIN order_details d ON d.order_id=o.order_id
            LEFT JOIN order_delivery delivery ON delivery.order_id=o.order_id
            WHERE o.customer_id=? AND o.order_status='draft'
              AND COALESCE(d.status,'active')<>'cancelled'
            ORDER BY o.order_date DESC,o.order_id''', (customer_id,)).fetchall()
        orders = []
        for row in rows:
            order = {key: row[key] for key in ('order_id', 'charged_price', 'currency', 'address_id')}
            try:
                snapshot = json.loads(row['address_snapshot']) if row['address_snapshot'] else None
            except (ValueError, TypeError):
                snapshot = None
            order['delivery_address'] = snapshot if isinstance(snapshot, dict) else None
            orders.append(order)
    return JSONResponse({'addresses': list_addresses(customer_id), 'orders': orders}, headers={'Cache-Control': 'no-store'})


@app.get("/health")
async def health():
    error = settings.azure_configuration_error()
    return {"status": "degraded" if error else "ok", "api": "ready",
            "provider": "azure_openai", "model_configured": error is None,
            "chart_streaming": True,
            "connection_verified": False, "configuration_error": error,
            "model": settings.AZURE_OPENAI_MODEL,
            "deployment": settings.AZURE_OPENAI_DEPLOYMENT}


@app.post("/conversations")
async def create_conversation(body: ConversationRequest, x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None), x_customer_token: str | None = Header(default=None)):
    customer_id = body.customer_id
    if body.mode == 'admin':
        require_admin(x_admin_key)
    elif body.mode == 'cashier':
        require_cashier(x_cashier_key, x_admin_key)
    elif x_customer_token is not None:
        # The verified session owns a new customer chat even when a client omits
        # its redundant account ID. A supplied invalid token is never anonymous.
        verified = customer_auth.customer_for_token(x_customer_token)
        if not verified or customer_id is not None and customer_id != verified:
            raise HTTPException(401, 'Sign in to the matching customer account.')
        customer_id = verified
    elif customer_id:
        require_customer(customer_id, None)
    return {"conversation_id": context.conversation(customer_id=customer_id, mode=body.mode), 'mode': body.mode}


@app.get('/admin/access')
async def admin_access(x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    return {'authorized': True, 'mode': 'admin', 'read_only': False}


@app.get('/cashier/access')
async def cashier_access(x_cashier_key: str | None = Header(default=None), x_admin_key: str | None = Header(default=None)):
    require_cashier(x_cashier_key, x_admin_key)
    return {'authorized': True, 'mode': 'cashier', 'refund_limit': settings.CASHIER_REFUND_LIMIT,
            'currency': settings.CASHIER_REFUND_CURRENCY}


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID, x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None), x_customer_token: str | None = Header(default=None)):
    record = context.get_conversation(str(conversation_id))
    check_record(record, x_admin_key, x_cashier_key, x_customer_token)
    messages = context.history(str(conversation_id), include_artifacts=True)
    messages = [{**m, "content": plain_reply(m["content"])} if m["role"] == "assistant" else m for m in messages]
    return {**record, "messages": messages,
            **({'analytics_context': context.latest_analytics_context(str(conversation_id))} if record['mode'] == 'admin' else {}),
            **({'shopping_state': context.shopping_state(str(conversation_id))} if record['mode'] == 'customer' else {})}


@app.post("/chat")
async def chat(body: ChatRequest, x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None), x_customer_token: str | None = Header(default=None)):
    return await run_chat(body, x_admin_key, x_cashier_key=x_cashier_key, x_customer_token=x_customer_token)


@app.post('/chat/stream')
async def stream_chat(body: ChatRequest, x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None), x_customer_token: str | None = Header(default=None)):
    # Check access before returning a streaming response or starting any work.
    check_record(context.get_conversation(str(body.conversation_id)), x_admin_key, x_cashier_key, x_customer_token)
    async def events():
        stream_started = perf_counter()
        queue = asyncio.Queue()
        async def run():
            try:
                result = await run_chat(body, x_admin_key, x_cashier_key=x_cashier_key, x_customer_token=x_customer_token,
                    chart_callback=lambda charts: queue.put_nowait({'event':'charts','charts':charts}))
                queue.put_nowait({'event':'result','result':result})
            except HTTPException as exc:
                queue.put_nowait({'event':'error','status':exc.status_code,'detail':exc.detail})
            except Exception:
                log.exception('Streaming chat failed')
                queue.put_nowait({'event':'error','status':503,'detail':'The billing request failed. Check the server logs.'})
        task = asyncio.create_task(run())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield '\n'
                    continue
                logging.getLogger('uvicorn.error.billing_performance').info(
                    'billing_stream event=%s seconds=%.3f', event['event'], perf_counter()-stream_started)
                yield json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n'
                if event['event'] in ('result','error'):
                    break
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return StreamingResponse(events(), media_type='application/x-ndjson',
        headers={'Cache-Control':'no-cache', 'X-Accel-Buffering':'no'})


def cached_chat_response(cached):
    """Apply the same display validation to both completed-request paths."""
    if 'tables' in cached:
        cached['tables'] = sanitize_tables(cached['tables'])
    if 'reports' in cached:
        cached['reports'] = sanitize_reports(cached['reports'])
    return {**cached, 'response': plain_reply(cached['response'])}


async def run_chat(body, x_admin_key, chart_callback=None, x_cashier_key=None, x_customer_token=None):
    cid, rid = str(body.conversation_id), str(body.request_id)
    if not body.message.strip():
        raise HTTPException(422, "Message must not be blank.")
    check_record(context.get_conversation(cid), x_admin_key, x_cashier_key, x_customer_token)
    # A finished response is immutable. Serve an authorized retry immediately
    # rather than queueing it behind an unrelated customer's model request.
    try:
        cached = context.completed_response(cid, rid, body.message)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if cached:
        return cached_chat_response(cached)
    # One worker and one active turn: no mutable agent state is shared across users.
    queued = perf_counter()
    async with app.state.chat_lock:
        logging.getLogger("uvicorn.error.billing_performance").info(
            "billing_api queue_wait_seconds=%.3f", perf_counter() - queued)
        record = context.get_conversation(cid)
        check_record(record, x_admin_key, x_cashier_key, x_customer_token)
        try:
            tracks_changes = getattr(app.state.engine, 'supports_request_progress', False) is True
            cached = context.begin(cid, rid, body.message, may_have_changed=not tracks_changes)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if cached:
            return cached_chat_response(cached)
        initial_shopping_state = None
        try:
            if record['mode'] == 'customer':
                # Keep the original input only on this admitted request. A
                # read-only retry gets its proposal back; new turns do not.
                initial_shopping_state = context.customer_turn_state(cid, rid, record['customer_id'])
            validate = getattr(app.state.engine, "validate_configuration", None)
            if validate:
                validate()
        except ValueError as exc:
            # No tools have run. Retain request-bound input for a same-ID retry,
            # while its approval remains expired for any later customer turn.
            context.fail(cid, rid, before_execution=True)
            raise HTTPException(503, str(exc)) from exc
        try:
            kwargs = {'mode': record['mode']} if record['mode'] in ('admin', 'cashier') else {}
            if tracks_changes:
                kwargs['mutation_callback'] = lambda: context.mark_request_changed(cid, rid)
            if chart_callback is not None and record['mode'] == 'admin':
                kwargs['chart_callback'] = chart_callback
            if record['mode'] == 'admin' and getattr(app.state.engine, 'supports_analytics_context', False):
                kwargs['analytics_context'] = context.latest_analytics_context(cid)
            if record['mode'] in ('admin','cashier') and getattr(app.state.engine, 'supports_action_context', False):
                kwargs['action_context'] = context.latest_action_context(cid,rid)
            if record['mode'] == 'customer':
                kwargs.update(shopping_state=initial_shopping_state,
                    state_callback=lambda state, owner: context.checkpoint_shopping(cid, rid, state, owner))
            result = await asyncio.wait_for(app.state.engine.chat(body.message, context.history(cid), record["customer_id"], **kwargs),
                                            timeout=settings.CHAT_TIMEOUT)
            result["response"] = plain_reply(result["response"])
            if 'tables' in result:
                result['tables'] = sanitize_tables(result['tables'])
            if 'reports' in result:
                result['reports'] = sanitize_reports(result['reports'])
            associated = result.pop('associated_customer_id', None)
            if associated and record['mode'] == 'customer' and associated != record['customer_id']:
                raise ValueError('Customer association requires verified sign-in in a new conversation.')
            response = {"conversation_id": cid, "request_id": rid, **result}
            context.finish(cid, rid, response)
            return response
        except asyncio.CancelledError:
            context.fail(cid, rid)
            raise
        except Exception as exc:
            context.fail(cid, rid)
            log.exception("Billing turn failed: %s", rid)
            code, message = failure_detail(exc)
            raise HTTPException(503, {"code": code, "message": message + ' '
                "If this request included a billing change, check its status before repeating it.",
                "conversation_id": cid, "request_id": rid}) from exc


@app.get("/invoices/{invoice_number}")
async def invoice(invoice_number: str, conversation_id: UUID, customer_id: str | None = None,
                  x_admin_key: str | None = Header(default=None), x_cashier_key: str | None = Header(default=None),
                  x_customer_token: str | None = Header(default=None)):
    record = context.get_conversation(str(conversation_id))
    check_record(record, x_admin_key, x_cashier_key, x_customer_token)
    associated = record["customer_id"]
    if associated and customer_id and associated != customer_id:
        raise HTTPException(403, "Invoice customer does not match this conversation.")
    owner = associated or (customer_id if record['mode'] in ('admin', 'cashier') else None)
    if not owner:
        raise HTTPException(422, "A customer ID is required.")
    try:
        result = await asyncio.wait_for(app.state.engine.invoice(invoice_number, owner), timeout=60)
    except Exception as exc:
        raise HTTPException(503, "Invoice tool unavailable.") from exc
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Invoice not found."))
    path = invoice_path(invoice_number, owner)
    if not path.is_file():
        raise HTTPException(503, "Invoice file was not generated.")
    return FileResponse(path, media_type="application/pdf", filename="invoice.pdf")
