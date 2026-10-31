"""Admin request telemetry. Opening this page never calls the billing agent."""
import math
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pandas as pd
import streamlit as st

from config import settings


st.set_page_config(page_title='Observability', page_icon='📊', layout='wide')
API = os.getenv('API_URL', 'http://127.0.0.1:8000').rstrip('/')
PREFIX = '/admin/observability'


def api_request(method, route, key, **kwargs):
    with httpx.Client(timeout=15) as client:
        response = client.request(method, API + route,
            headers={'X-API-Key': settings.API_KEY, 'X-Admin-Key': key}, **kwargs)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError('Invalid observability response.')
    return data


def sign_in():
    key = st.session_state.get('_obs_login_key', '')
    st.session_state['_obs_login_key'] = ''
    try:
        access = api_request('GET', '/admin/access', key)
        if access.get('authorized') is not True:
            raise ValueError('Admin access was not verified.')
    except (httpx.HTTPError, ValueError):
        st.session_state['_obs_login_error'] = 'Admin access denied or the API is unavailable.'
        return
    # Share the existing credential so main-page logout clears dashboard access
    # too. Do not alter the active billing conversation or customer identity.
    st.session_state['admin_key'] = key
    st.session_state['_obs_authorized'] = True
    st.session_state.pop('_obs_login_error', None)


def sign_out():
    st.session_state['admin_key'] = ''
    st.session_state['_obs_authorized'] = False
    st.session_state.pop('_obs_login_error', None)


def access_error(exc):
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        sign_out()
        st.error('Admin access expired or was denied. Sign in again to view observability.')
    else:
        st.error('Observability is unavailable. Check the API connection; billing can continue independently.')


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def usd(value):
    value = number(value)
    if value is None:
        return 'Not available'
    return f'${value:,.6f}' if abs(value) < 1 else f'${value:,.2f}'


def seconds(value):
    value = number(value)
    return 'Not available' if value is None else f'{value / 1000:,.3f} s'


def readable(key):
    return str(key).replace('_', ' ').capitalize()


def rows_table(rows, columns=None):
    if not rows:
        st.info('No records for this selection.')
        return
    frame = pd.DataFrame(rows)
    if columns:
        frame = frame[[column for column in columns if column in frame.columns]]
    frame = frame.rename(columns=readable)
    st.dataframe(frame, hide_index=True, use_container_width=True)


def load(route, key, **params):
    try:
        return api_request('GET', PREFIX + route, key, params=params)
    except (httpx.HTTPError, ValueError) as exc:
        access_error(exc)
        return None


st.title('Observability')
st.caption('Chat-request performance, model usage, guardrail outcomes and committed billing activity. Admin access only.')
st.caption('Request metrics cover POST /chat and /chat/stream only. Health checks, dashboard reads and sign-in calls are not included.')

admin_key = st.session_state.get('admin_key', '')
authorized_screen = st.session_state.get('mode') == 'Admin' or st.session_state.get('_obs_authorized') is True
if not admin_key or not authorized_screen:
    with st.form('observability_login'):
        st.text_input('Admin key', type='password', key='_obs_login_key')
        st.form_submit_button('Open observability', on_click=sign_in)
    if st.session_state.get('_obs_login_error'):
        st.error(st.session_state['_obs_login_error'])
    st.info('Customer and cashier credentials do not grant access to this page.')
    st.stop()

with st.sidebar:
    st.subheader('Observability')
    days = st.selectbox('Period', [1, 7, 30, 90], index=1, format_func=lambda value: f'Last {value} day' + ('s' if value != 1 else ''))
    st.button('Refresh dashboard')
    st.button('Sign out of admin access', on_click=sign_out)
    st.link_button('Back to billing chat', '/')
    st.caption('All reporting dates use UTC. Refreshing this page does not call the model.')

summary = load('/summary', admin_key, days=days)
if summary is None:
    st.stop()
data_available = summary.get('available', True)
if not data_available:
    st.warning('Telemetry records are currently unavailable. Request totals and costs have not been inferred.')

health = summary.get('health') or {}
if health.get('enabled') is False:
    st.warning('Observability collection is disabled. Existing billing requests are unaffected.')
elif health.get('state') not in (None, 'ready', 'running', 'healthy'):
    st.warning('Telemetry storage is not ready. Recent requests may not appear yet.')
if health.get('dropped') or health.get('write_errors'):
    st.warning(f"Telemetry delivery is incomplete: {health.get('dropped', 0)} dropped records and {health.get('write_errors', 0)} storage errors.")
if health.get('queued'):
    st.caption(f"{health['queued']} request records are waiting to be saved. Refresh to see them after the background writer finishes.")

overview, requests_tab, guardrails_tab, audit_tab, pricing_tab = st.tabs(
    ['Overview', 'Requests', 'Guardrails', 'Audit logs', 'Cost settings'])

with overview:
    totals = summary.get('overview') or {}
    missing = 0 if data_available else 'Not available'
    first = st.columns(4)
    first[0].metric('Requests', totals.get('requests', missing))
    first[1].metric('Estimated model cost', usd(totals.get('known_cost_usd')))
    first[2].metric('Input tokens', totals.get('input_tokens', missing))
    first[3].metric('Output tokens', totals.get('output_tokens', missing))
    second = st.columns(4)
    second[0].metric('Median latency', seconds(totals.get('latency_p50_ms')))
    second[1].metric('95th percentile latency', seconds(totals.get('latency_p95_ms')))
    second[2].metric('Errors / rejected', f"{totals.get('errors', missing)} / {totals.get('rejected', missing)}")
    second[3].metric('Cached replies', totals.get('cache_hits', missing))
    st.caption('Model cost covers measured token usage only. It excludes hosting, storage, taxes and other services. Cached replies do not repeat the original model cost.')
    unknown = totals.get('unknown_cost_requests', 0)
    incomplete = totals.get('incomplete_usage_requests', 0)
    if unknown or incomplete:
        st.warning(f'{unknown} requests have incomplete cost estimates and {incomplete} have incomplete provider usage. Known cost is not the complete bill.')

    daily = pd.DataFrame(summary.get('daily') or [])
    if not daily.empty and 'date' in daily.columns:
        by_date = daily.set_index('date')
        left, right = st.columns(2)
        with left:
            st.subheader('Daily request volume')
            if 'requests' in by_date:
                st.bar_chart(by_date[['requests']].rename(columns={'requests': 'Requests'}))
            st.subheader('Daily token usage')
            token_columns = [column for column in ('input_tokens', 'output_tokens') if column in by_date]
            if token_columns:
                st.line_chart(by_date[token_columns].rename(columns=readable))
        with right:
            st.subheader('Daily estimated model cost (USD)')
            if 'cost_usd' in by_date and by_date['cost_usd'].notna().any():
                st.line_chart(by_date[['cost_usd']].rename(columns={'cost_usd': 'Estimated cost (USD)'}))
            else:
                st.info('Cost becomes available when pricing and token usage are known.')
            st.subheader('Average daily latency (seconds)')
            if 'latency_average_ms' in by_date:
                latency = by_date[['latency_average_ms']].div(1000).rename(columns={'latency_average_ms': 'Latency (seconds)'})
                st.line_chart(latency)
    else:
        st.info('No captured requests in this period. Send a billing question, then refresh this page.'
                if data_available else 'Request telemetry cannot currently be read. Billing remains independent.')

    st.subheader('Average request timing')
    stages = [{'Stage': label, 'Seconds': value / 1000} for key, label in
              (('latency_average_ms', 'Entire request'), ('queue_average_ms', 'Queue'),
               ('model_average_ms', 'Model calls'), ('tool_average_ms', 'Tools'))
              if (value := number(totals.get(key))) is not None]
    if stages:
        st.bar_chart(pd.DataFrame(stages).set_index('Stage'))
        st.caption('Model and tool timings are parts of the request total; the bars are not additive.')
    st.subheader('Usage by role')
    rows_table(summary.get('by_role') or [])

    st.subheader('Cost outlook')
    forecast = summary.get('forecast') or {}
    per_request = number(forecast.get('per_request_usd'))
    observed_rate = number(forecast.get('requests_per_day'))
    expected_rate = st.number_input('Expected requests per day', min_value=0.0,
        value=float(observed_rate or 0), step=1.0, help='Starts with observed daily traffic. Change it to model a different workload.')
    estimates = st.columns(4)
    estimates[0].metric('Average cost per request', usd(per_request))
    for column, label, multiplier in zip(estimates[1:], ('Per day', 'Per 30-day month', 'Per 365-day year'), (1, 30, 365)):
        column.metric(label, usd(per_request * expected_rate * multiplier) if per_request is not None else 'Not available')
    if forecast.get('coverage_note'):
        st.caption(forecast['coverage_note'])
    st.caption('Projection assumes the same request mix and average token usage as the selected observation period. It is an estimate, not a spending commitment.')
    for note in summary.get('notes') or []:
        st.caption(str(note))

with requests_tab:
    st.subheader('Request history')
    filters = st.columns(3)
    role = filters[0].selectbox('Role', ['All', 'customer', 'cashier', 'admin', 'unknown'])
    status = filters[1].selectbox('Status', ['All', 'success', 'error', 'cancelled', 'rejected', 'cache_hit'])
    page = filters[2].number_input('Request page', min_value=1, value=1, step=1)
    params = {'days': days, 'limit': 100, 'offset': (page - 1) * 100}
    if role != 'All':
        params['mode'] = role
    if status != 'All':
        params['status'] = status
    request_data = load('/requests', admin_key, **params)
    if request_data is not None and request_data.get('available') is False:
        st.warning('Request history is currently unavailable.')
        request_data = None
    if request_data is not None:
        request_rows = request_data.get('rows') or []
        st.caption(f"{request_data.get('total', 0)} matching requests. Up to 100 rows per page.")
        columns = ['occurred_at', 'request_id', 'attempt_id', 'mode', 'status', 'latency_ms', 'queue_ms',
                   'model_calls', 'tool_calls', 'input_tokens', 'output_tokens', 'estimated_cost_usd',
                   'usage_complete', 'cost_complete', 'model', 'deployment']
        rows_table(request_rows, columns)
        if request_rows:
            chosen = st.selectbox('Inspect request', range(len(request_rows)),
                format_func=lambda index: f"{request_rows[index].get('occurred_at', '')} — {request_rows[index].get('request_id') or request_rows[index].get('attempt_id', '')}")
            record = request_rows[chosen]
            st.caption('Each retry is a separate attempt. Cached replies show no new model token usage.')
            details = [{'Field': readable(key), 'Value': str(record[key]) if record.get(key) is not None else 'Not available'} for key in
                       ('attempt_id', 'conversation_id', 'http_status', 'agent_ms', 'model_ms', 'tool_ms',
                        'failed_model_calls', 'pricing_source', 'input_per_million', 'output_per_million')]
            rows_table(details)
            st.caption('Snapshot input and output rates are USD per one million tokens, as recorded for this request.')
            left, right = st.columns(2)
            with left:
                st.markdown('**Executed guardrail checks**')
                rows_table(record.get('guardrails') or [])
            with right:
                st.markdown('**Tool calls**')
                rows_table(record.get('tools') or [])
                st.caption('Tool-call totals count actual MCP requests. A tool row with Dispatched=false was blocked before MCP.')

with guardrails_tab:
    st.subheader('Observed guardrail outcomes')
    st.caption('These are checks actually executed by the application. An allowed request is not a guarantee that its answer is correct. Semantic topic adherence is not independently graded here.')
    guards = summary.get('guardrails') or []
    rows_table(guards)
    if guards:
        frame = pd.DataFrame(guards)
        if {'outcome', 'count'} <= set(frame.columns):
            st.bar_chart(frame.groupby('outcome')['count'].sum().rename_axis('Outcome'))

with audit_tab:
    st.subheader('Committed billing changes')
    st.caption('Read-only records from the existing billing audit log. Request telemetry is stored separately. No customer messages, passwords or address text are shown.')
    audit_page = st.number_input('Audit page', min_value=1, value=1, step=1)
    audit = load('/audit', admin_key, limit=100, offset=(audit_page - 1) * 100)
    if audit is not None and audit.get('available') is False:
        st.warning('Committed billing audit records are currently unavailable. No record total has been inferred.')
    elif audit is not None:
        st.caption(f"{audit.get('total', 0)} committed audit records. Up to 100 rows per page.")
        rows_table(audit.get('rows') or [])

with pricing_tab:
    st.subheader('Model cost settings')
    pricing = load('/pricing', admin_key)
    if pricing is not None:
        st.caption('Rates are USD per one million tokens. Changing these rates affects cost reporting only; it does not change the billing agent model or deployment.')
        st.caption('Rate changes apply to new requests. Historical requests retain their recorded rates and cost estimates.')
        if pricing.get('source_label'):
            st.info('Current pricing source: ' + str(pricing['source_label']))
        if pricing.get('source_url'):
            # The API returns a fixed official reference, never user-provided HTML.
            st.caption('Reference: ' + str(pricing['source_url']))
        if pricing.get('source_checked_at'):
            st.caption('Reference checked: ' + str(pricing['source_checked_at']))
        if not pricing.get('configured') and pricing.get('input_per_million') is None:
            st.warning('No rate is known for the configured model. Enter your Azure rates before interpreting costs.')
        st.caption('Input estimates use the ordinary input rate because the client does not provide a cached-token breakdown. Reasoning tokens are already part of output usage. Azure region, deployment type and contract can change actual charges.')
        with st.form('observability_pricing'):
            model = st.text_input('Model for these rates', value=pricing.get('model') or settings.AZURE_OPENAI_MODEL, max_chars=200)
            deployment = st.text_input('Deployment for these rates', value=pricing.get('deployment') or settings.AZURE_OPENAI_DEPLOYMENT, max_chars=200)
            input_rate = st.number_input('Input price per million tokens (USD)', min_value=0.0,
                value=float(number(pricing.get('input_per_million')) or 0), step=0.01, format='%.6f')
            output_rate = st.number_input('Output price per million tokens (USD)', min_value=0.0,
                value=float(number(pricing.get('output_per_million')) or 0), step=0.01, format='%.6f')
            source = st.text_input('Rate description', value='Azure configured rates', max_chars=120)
            save = st.form_submit_button('Save pricing rates')
        if save:
            try:
                api_request('PUT', PREFIX + '/pricing', admin_key, json={
                    'model': model, 'deployment': deployment, 'input_per_million': input_rate,
                    'output_per_million': output_rate, 'source_label': source})
                st.success('Pricing rates saved. New records use the applicable rate; historical estimates retain their recorded rate.')
            except (httpx.HTTPError, ValueError) as exc:
                access_error(exc)
