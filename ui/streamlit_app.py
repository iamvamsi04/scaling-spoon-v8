import os
import json
from time import monotonic
import sys
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import streamlit as st
from config import settings
from context.reply_text import plain_reply, error_reply
from ui.tables import render_tables
from ui.reports import render_reports

st.set_page_config(
    page_title="Billing Agent",
    page_icon="💳",
    layout="centered",
    initial_sidebar_state="collapsed",
)
API = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
HEADERS = {"X-API-Key": settings.API_KEY}


@st.cache_resource
def api_client():
    return httpx.Client(timeout=settings.CHAT_TIMEOUT + 30)


def request_headers():
    headers = dict(HEADERS)
    if st.session_state.get('mode') == 'Admin' and st.session_state.get('admin_key'):
        headers['X-Admin-Key'] = st.session_state.admin_key
    if st.session_state.get('mode') == 'Cashier' and st.session_state.get('cashier_key'):
        headers['X-Cashier-Key'] = st.session_state.cashier_key
    if st.session_state.get('mode') == 'Customer' and st.session_state.get('customer_token'):
        headers['X-Customer-Token'] = st.session_state.customer_token
    return headers


def request(method, route, **kwargs):
    if st.session_state.get('mode') == 'Customer' and st.session_state.get('customer_token'):
        validate_customer_session()
    result = api_client().request(method, API + route, headers=request_headers(), **kwargs)
    result.raise_for_status()
    return result


def health_status(api, api_key):
    cached=st.session_state.get('_health_cache')
    if cached and cached[:2]==(api,api_key) and monotonic()-cached[2]<15:
        return cached[3]
    failed=st.session_state.get('_health_failure')
    if failed and failed[:2]==(api,api_key) and monotonic()-failed[2]<3:
        raise httpx.ConnectError('The API health check is temporarily unavailable.')
    try:
        result = api_client().get(api + '/health', headers={'X-API-Key':api_key}, timeout=2.0)
        result.raise_for_status()
    except httpx.HTTPError:
        st.session_state['_health_failure']=(api,api_key,monotonic())
        raise
    data=result.json()
    st.session_state.pop('_health_failure',None)
    st.session_state['_health_cache']=(api,api_key,monotonic(),data)
    return data


def streamed_chat(body, placeholder):
    with api_client().stream('POST', API+'/chat/stream', headers=request_headers(), json=body) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.strip():
                continue
            try:
                event=json.loads(line)
            except ValueError as exc:
                raise httpx.ReadError('Invalid chat stream.') from exc
            if event.get('event')=='charts':
                from ui.charts import render_chart
                with placeholder.container():
                    for chart in event.get('charts',[]):
                        render_chart(chart)
            elif event.get('event')=='result':
                return event['result']
            elif event.get('event')=='error':
                failed=httpx.Response(event.get('status',503),json={'detail':event.get('detail')},
                    request=httpx.Request('POST',API+'/chat/stream'))
                failed.raise_for_status()
    raise httpx.ReadError('Chat stream ended before the saved response arrived.')


for key, value in {"messages": [], "conversation_id": None, "pending": None, 'admin_key': '', 'cashier_key': '',
                   'customer_token': '', 'customer_id': '', 'account_error': '', 'mode': 'Customer'}.items():
    if key not in st.session_state:
        st.session_state[key] = value


def clear_customer_session():
    st.session_state.messages = []
    st.session_state.conversation_id = None
    st.session_state.pending = None
    for key in ('customer_token', 'customer_id', 'customer_password', 'customer_password_repeat',
                'customer_login_id', 'customer_name', 'customer_company', 'customer_email'):
        st.session_state[key] = ''
    for key in ('_customer_session_checked', '_customer_session_deadline',
                '_customer_session_retry_after', '_customer_session_warning'):
        st.session_state.pop(key, None)


def reset_mode():
    token = st.session_state.customer_token
    clear_customer_session()
    st.session_state.account_error = ''
    st.session_state.admin_key = ''
    st.session_state.admin_password = ''
    st.session_state.cashier_key = ''
    st.session_state.cashier_password = ''
    if token:
        try:
            api_client().post(API + '/customers/logout', timeout=3.0,
                headers={**HEADERS, 'X-Customer-Token': token}).raise_for_status()
        except httpx.HTTPError:
            st.session_state.account_error = 'Signed out on this screen. The server could not be reached to revoke the session.'


def handle_expired_session(exc):
    if (st.session_state.mode == 'Customer' and st.session_state.customer_token
            and isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401):
        try:
            if exc.response.json().get('detail') == 'Invalid API key.':
                return  # A server configuration error does not revoke the customer's identity.
        except (ValueError, AttributeError):
            pass
        clear_customer_session()
        st.session_state.account_error = 'Your session ended. Please sign in again.'
        st.rerun()


def remember_customer_session(account):
    now = monotonic()
    st.session_state['_customer_session_checked'] = (API, st.session_state.customer_token, now)
    remaining = account.get('expires_in')
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and remaining > 0:
        st.session_state['_customer_session_deadline'] = now + remaining
    else:
        st.session_state.pop('_customer_session_deadline', None)
    st.session_state.pop('_customer_session_retry_after', None)
    st.session_state.pop('_customer_session_warning', None)


def validate_customer_session():
    """Keep the screen's identity in sync with the API without adding a call to every rerun."""
    now = monotonic()
    deadline = st.session_state.get('_customer_session_deadline')
    if deadline is not None and now >= deadline:
        clear_customer_session()
        st.session_state.account_error = 'Your session ended. Please sign in again.'
        st.rerun()
    checked = st.session_state.get('_customer_session_checked')
    if checked and checked[:2] == (API, st.session_state.customer_token) and now - checked[2] < 15:
        return
    if now < st.session_state.get('_customer_session_retry_after', 0):
        return
    try:
        result = api_client().get(API + '/customers/session', headers=request_headers(), timeout=2.0)
        result.raise_for_status()
        session = result.json()
        if session.get('customer_id') != st.session_state.customer_id:
            clear_customer_session()
            st.session_state.account_error = 'Your session ended. Please sign in again.'
            st.rerun()
        remember_customer_session(session)
    except httpx.HTTPError as exc:
        handle_expired_session(exc)
        st.session_state['_customer_session_retry_after'] = now + 3
        st.session_state['_customer_session_warning'] = 'Unable to check your session. Check the API connection.'
    except (ValueError, AttributeError):
        st.session_state['_customer_session_retry_after'] = now + 3
        st.session_state['_customer_session_warning'] = 'Unable to check your session. Check the API connection.'


def logout():
    reset_mode()
    st.session_state.mode = 'Customer'


def customer_account(action):
    st.session_state.account_error = ''
    password = st.session_state.get('customer_password', '')
    try:
        if action == 'register' and password != st.session_state.get('customer_password_repeat', ''):
            st.session_state.account_error = 'The two passwords must match.'
            return
        body = {'customer_id': st.session_state.get('customer_login_id', '').strip(), 'password': password}
        if action == 'register':
            body.update(name=st.session_state.get('customer_name', '').strip(),
                        company=st.session_state.get('customer_company', '').strip() or None,
                        email=st.session_state.get('customer_email', '').strip() or None)
        response = api_client().post(API + '/customers/' + action, headers=HEADERS, json=body)
        response.raise_for_status()
        account = response.json()
        st.session_state.customer_token = account['token']
        st.session_state.customer_id = account['customer_id']
        remember_customer_session(account)
        st.session_state.conversation_id = None
        st.session_state.messages = []
        st.session_state.pending = None
    except httpx.HTTPStatusError as exc:
        st.session_state.account_error = error_reply(exc.response)
    except httpx.HTTPError:
        st.session_state.account_error = 'Unable to reach the store. Please try again.'
    finally:
        st.session_state.customer_password = ''
        st.session_state.customer_password_repeat = ''

with st.sidebar:
    st.subheader("Store assistant")
    st.radio('Mode', ['Customer', 'Cashier', 'Admin'], key='mode', on_change=reset_mode)
    if st.session_state.mode == 'Admin':
        st.caption('Analytics, records and refund approvals')
        if not st.session_state.admin_key:
            password = st.text_input('Admin key', type='password', key='admin_password')
            if st.button('Unlock admin mode'):
                try:
                    response = api_client().request('GET', API + '/admin/access',
                        headers={**HEADERS, 'X-Admin-Key': password})
                    response.raise_for_status()
                    st.session_state.admin_key = password
                    st.rerun()
                except httpx.HTTPError:
                    st.error('Admin access denied or not configured. Check BILLING_ADMIN_API_KEY on the API server.')
            st.info('Enter the separate admin key to view store-wide analytics and records.')
            st.stop()
        st.button('Log out of admin mode', on_click=logout)
    elif st.session_state.mode == 'Cashier':
        st.caption('Records and limited refund approvals')
        if not st.session_state.cashier_key:
            password = st.text_input('Cashier key', type='password', key='cashier_password')
            if st.button('Unlock cashier mode'):
                try:
                    response = api_client().request('GET', API + '/cashier/access',
                        headers={**HEADERS, 'X-Cashier-Key': password})
                    response.raise_for_status()
                    st.session_state.cashier_key = password
                    st.session_state.cashier_limit = response.json()
                    st.rerun()
                except httpx.HTTPError:
                    st.error('Cashier access denied or not configured. Check BILLING_CASHIER_API_KEY on the API server.')
            st.info('Enter the separate cashier key to review and approve eligible refund requests.')
            st.stop()
        limit = st.session_state.get('cashier_limit', {})
        st.caption(f"Refund approval limit: {limit.get('refund_limit', '')} {limit.get('currency', '')}")
        st.button('Log out of cashier mode', on_click=logout)
    else:
        st.caption('Shopping and billing help')
        if st.session_state.customer_token:
            validate_customer_session()
            st.caption('Signed in as ' + st.session_state.customer_id)
            st.button('Sign out', on_click=reset_mode)
            if st.session_state.get('_customer_session_warning'):
                st.warning(st.session_state['_customer_session_warning'])
        else:
            st.caption('Browse without signing in. Sign in for your orders, invoices and refunds.')
            with st.expander('Customer account'):
                account_action = st.selectbox('Account', ['Sign in', 'Create account'])
                with st.form('customer_account_form'):
                    st.text_input('Customer ID', key='customer_login_id')
                    if account_action == 'Create account':
                        st.text_input('Name', key='customer_name')
                        st.text_input('Company (optional)', key='customer_company')
                        st.text_input('Email (optional)', key='customer_email')
                    st.text_input('Password', type='password', key='customer_password', help='Use 10 to 128 characters. Do not put your password in chat.')
                    if account_action == 'Create account':
                        st.text_input('Repeat password', type='password', key='customer_password_repeat')
                    st.form_submit_button(account_action, on_click=customer_account,
                                          args=('register' if account_action == 'Create account' else 'login',))
        if st.session_state.account_error:
            st.error(st.session_state.account_error)
    customer = (st.session_state.customer_id if st.session_state.mode == 'Customer' else
                st.text_input('Customer ID (optional)', placeholder='CUST001', disabled=bool(st.session_state.conversation_id)))
    if st.button("New conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.conversation_id = None
        st.session_state.pending = None
        st.rerun()
    resume = st.text_input("Resume conversation ID")
    if st.button("Resume", disabled=not resume):
        try:
            saved = request("GET", f"/conversations/{resume}").json()
            if saved.get('mode', 'customer') != st.session_state.mode.lower():
                st.error('Switch to the matching mode before resuming this conversation.')
                st.stop()
            st.session_state.conversation_id = saved["conversation_id"]
            st.session_state.messages = saved["messages"]
            st.session_state.pending = None
            st.rerun()
        except httpx.HTTPError as exc:
            handle_expired_session(exc)
            st.error(f"Could not resume: {exc}")
    if st.session_state.conversation_id:
        st.caption("Conversation ID")
        st.code(st.session_state.conversation_id, language=None)
    try:
        health = health_status(API, settings.API_KEY)
        st.session_state.chart_streaming = bool(health.get('chart_streaming'))
        if health["model_configured"]:
            st.success("Azure OpenAI configured")
            st.caption("Connection is checked when you send a message.")
        else:
            st.warning(health.get("configuration_error") or "Configure Azure OpenAI in .env to enable chat.")
    except httpx.HTTPError:
        st.session_state.chart_streaming = False
        st.warning("Start the API with python app.py")
    st.divider()
    st.caption("Refunds update the ledger only. This console does not send payments.")

st.title("💳 Billing Agent")
st.caption(
    "Ask questions about invoices, orders, refunds, "
    "billing, spending, and costs."
)


def show_invoices(entry, index):
    for n, invoice in enumerate(entry.get("invoices", [])):
        if st.button(f"Prepare PDF · {invoice['invoice_number']}", key=f"pdf-{index}-{n}"):
            try:
                pdf = request("GET", f"/invoices/{invoice['invoice_number']}", params={
                    "conversation_id": st.session_state.conversation_id, "customer_id": invoice["customer_id"]})
                st.download_button("Download invoice", pdf.content, "invoice.pdf", "application/pdf", key=f"dl-{index}-{n}")
            except httpx.HTTPError as exc:
                handle_expired_session(exc)
                st.error(f"Invoice unavailable: {exc}")


for i, entry in enumerate(st.session_state.messages):
    with st.chat_message(entry["role"]):
        if entry["role"] == "assistant":
            st.text(plain_reply(entry["content"]))
        else:
            st.markdown(entry["content"])
        if st.session_state.mode == 'Admin' and entry.get('charts'):
            from ui.charts import render_chart
            for chart in entry.get('charts', []):
                render_chart(chart)
        if entry['role'] == 'assistant':
            render_tables(entry.get('tables', []))
            render_reports(entry.get('reports', []), i)
        show_invoices(entry, i)

prompt = st.chat_input("Ask a billing question...", disabled=bool(st.session_state.pending))
if prompt:
    try:
        if not st.session_state.conversation_id:
            st.session_state.conversation_id = request("POST", "/conversations", json={
                "customer_id": customer.strip() or None, 'mode': st.session_state.mode.lower()}).json()["conversation_id"]
        st.session_state.pending = {"message": prompt, "request_id": str(uuid4()), "conversation_id": st.session_state.conversation_id}
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.rerun()
    except httpx.HTTPError as exc:
        handle_expired_session(exc)
        st.error(f"Could not start the conversation: {exc}")

if st.session_state.pending:
    try:
        with st.chat_message("assistant"):
            with st.spinner("Billing Agent is thinking..."):
                if st.session_state.mode == 'Admin' and st.session_state.get('chart_streaming'):
                    reply = streamed_chat(st.session_state.pending, st.empty())
                else:
                    reply = request("POST", "/chat", json=st.session_state.pending).json()
        st.session_state.messages.append({"role": "assistant", "content": reply["response"],
            "invoices": reply.get("invoices", []), 'charts':reply.get('charts',[]),
            'tables':reply.get('tables',[]), 'reports':reply.get('reports',[])})
        st.session_state.pending = None
        st.rerun()
    except httpx.HTTPError as exc:
        handle_expired_session(exc)
        detail = error_reply(exc.response) if isinstance(exc, httpx.HTTPStatusError) else "Unable to reach the Billing Agent. Check that the API is running."
        st.error(f"Request failed: {detail}")
        st.caption("Check current order state before repeating a write. A timed-out request may have completed.")
        if st.button("Retry same request"):
            st.rerun()
        if st.button("Clear pending request"):
            st.session_state.pending = None
            st.rerun()
