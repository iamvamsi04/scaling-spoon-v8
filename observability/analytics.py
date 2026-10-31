"""Read operational metrics on demand, outside the billing request path."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3

from observability import pricing, store


def _bounded(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}.')
    return value


def _cutoff(days):
    _bounded(days, 'Days', 1, 3660)
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days-1)
    return datetime.combine(start, datetime.min.time(), timezone.utc).isoformat(timespec='microseconds')


def _where(days, mode=None, status=None):
    clauses, values = ['occurred_at>=?', 'occurred_at<=?'], [_cutoff(days), store.utc_now()]
    if mode is not None:
        if mode not in store.MODES:
            raise ValueError('Unknown request role.')
        clauses.append('mode=?')
        values.append(mode)
    if status is not None:
        if status not in store.STATUSES:
            raise ValueError('Unknown request status.')
        clauses.append('status=?')
        values.append(status)
    return ' AND '.join(clauses), values


def _request(row):
    value = dict(row)
    try:
        details = json.loads(value.pop('details_json'))
    except (TypeError, ValueError):
        details = {}
    value.update(guardrails=details.get('guardrails', []), tools=details.get('tools', []))
    value['usage_complete'] = bool(value['usage_complete'])
    value['cost_complete'] = bool(value['cost_complete'])
    return value


def requests(days=7, limit=100, offset=0, mode=None, status=None):
    _bounded(limit, 'Limit', 1, 500)
    _bounded(offset, 'Offset', 0, 10**9)
    where, values = _where(days, mode, status)
    try:
        with closing(store.connect()) as conn:
            conn.execute('BEGIN')
            total = conn.execute('SELECT COUNT(*) FROM request_events WHERE '+where, values).fetchone()[0]
            rows = conn.execute('SELECT * FROM request_events WHERE '+where+' ORDER BY occurred_at DESC,attempt_id DESC LIMIT ? OFFSET ?',
                                [*values,limit,offset]).fetchall()
        return {'available':True, 'rows':[_request(row) for row in rows],
                'total':total,'limit':limit,'offset':offset, 'health':store.health()}
    except (OSError, sqlite3.Error):
        return {'available':False, 'rows':[], 'total':0, 'limit':limit, 'offset':offset,
                'health':store.health(), 'error':'Operational metrics are not available. Billing requests remain independent.'}


def _percentile(conn, where, values, count, proportion):
    if not count:
        return None
    rank = (count-1)*proportion
    index = math.floor(rank)
    rows = conn.execute('SELECT latency_ms FROM request_events WHERE '+where+' ORDER BY latency_ms LIMIT 2 OFFSET ?',
                        [*values,index]).fetchall()
    low = float(rows[0][0])
    return low if len(rows)==1 else low+(float(rows[1][0])-low)*(rank-index)


def summary(days=7):
    where, values = _where(days)
    health = store.health()
    result = {'available':False, 'days':days, 'timezone':'UTC', 'overview':{}, 'daily':[],
        'by_role':[], 'guardrails':[], 'slowest':[], 'forecast':{},
        'health':health, 'pricing':pricing.get_pricing(), 'notes':[
            'Costs are estimates of reported model-token usage, not Azure invoices or store payment amounts.',
            'Normal input pricing is used because cached-input token details are not available in this client.',
            'Output-token totals already include reasoning tokens; reasoning is not charged a second time.',
            'Failed calls with unreported usage may incur additional cost. They remain explicitly incomplete.',
            'Guardrails show observed structured checks. Semantic business-scope decisions are unassessed unless explicitly reported; no extra classification call is made.',
            'Traffic projections use observed UTC calendar days, including quiet days and the partial current day. Month=30 days; year=365 days.',
        ]}
    try:
        with closing(store.connect()) as conn:
            conn.execute('BEGIN')
            aggregate = conn.execute('''SELECT COUNT(*) AS requests,
                SUM(status='success') AS successes,SUM(status='error') AS errors,
                SUM(status='rejected') AS rejected,SUM(status='cancelled') AS cancelled,
                SUM(status='cache_hit') AS cache_hits,
                SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
                SUM(model_calls) AS model_calls,SUM(failed_model_calls) AS failed_model_calls,
                SUM(tool_calls) AS tool_calls,SUM(estimated_cost_usd) AS known_cost_usd,
                AVG(CASE WHEN cost_complete=1 THEN estimated_cost_usd END) AS average_cost_usd,
                SUM(CASE WHEN cost_complete=1 AND model_calls>0 THEN 1 ELSE 0 END) AS complete_priced_provider_requests,
                SUM(cost_complete=0) AS unknown_cost_requests,
                SUM(usage_complete=0) AS incomplete_usage_requests,
                AVG(latency_ms) AS latency_average_ms,AVG(queue_ms) AS queue_average_ms,
                AVG(agent_ms) AS agent_average_ms,AVG(model_ms) AS model_average_ms,
                AVG(tool_ms) AS tool_average_ms,MIN(occurred_at) AS first_request_at,
                MAX(occurred_at) AS last_request_at FROM request_events WHERE '''+where, values).fetchone()
            overview = dict(aggregate)
            count = overview['requests']
            for key in ('successes','errors','rejected','cancelled','cache_hits','input_tokens','output_tokens',
                        'model_calls','failed_model_calls','tool_calls','complete_priced_provider_requests',
                        'unknown_cost_requests','incomplete_usage_requests'):
                overview[key] = overview[key] or 0
            if not count:
                overview['known_cost_usd'] = 0
            overview['latency_p50_ms'] = _percentile(conn,where,values,count,.5)
            overview['latency_p95_ms'] = _percentile(conn,where,values,count,.95)
            overview['cost_coverage_percent'] = 100*(count-overview['unknown_cost_requests'])/count if count else None
            daily = conn.execute('''SELECT substr(occurred_at,1,10) AS date,COUNT(*) AS requests,
                SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
                SUM(estimated_cost_usd) AS cost_usd,
                SUM(cost_complete=0) AS unknown_cost_requests,AVG(latency_ms) AS latency_average_ms
                FROM request_events WHERE '''+where+' GROUP BY date ORDER BY date',values).fetchall()
            roles = conn.execute('''SELECT mode,COUNT(*) AS requests,SUM(estimated_cost_usd) AS cost_usd,
                SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
                SUM(cost_complete=0) AS unknown_cost_requests,AVG(latency_ms) AS latency_average_ms
                FROM request_events WHERE '''+where+' GROUP BY mode ORDER BY mode',values).fetchall()
            guards = conn.execute('''SELECT json_extract(g.value,'$.check') AS "check",
                json_extract(g.value,'$.outcome') AS outcome,
                SUM(COALESCE(json_extract(g.value,'$.count'),1)) AS count
                FROM request_events e,json_each(e.details_json,'$.guardrails') g
                WHERE e.occurred_at>=? AND e.occurred_at<=?
                GROUP BY "check",outcome ORDER BY "check",outcome''',values).fetchall()
            overview['guardrail_checks'] = sum(row['count'] for row in guards)
            overview['guardrail_blocks'] = sum(row['count'] for row in guards if row['outcome']=='blocked')
            overview['guardrail_unassessed'] = sum(row['count'] for row in guards if row['outcome']=='unknown')
            slowest = conn.execute('SELECT * FROM request_events WHERE '+where+
                ' ORDER BY latency_ms DESC,occurred_at DESC LIMIT 10',values).fetchall()
        today = datetime.now(timezone.utc).date()
        first = datetime.fromisoformat(overview['first_request_at']).date() if count else None
        observed_days = max(1,(today-first).days+1) if first else 0
        average = overview['average_cost_usd']
        cache_only_sample = bool(overview['unknown_cost_requests']
                                 and not overview['complete_priced_provider_requests'])
        if cache_only_sample:
            average = None
        traffic = count/observed_days if observed_days else 0
        per_day = average*traffic if average is not None else None
        result.update(available=True, overview=overview, daily=[dict(row) for row in daily],
            by_role=[dict(row) for row in roles],guardrails=[dict(row) for row in guards],
            slowest=[_request(row) for row in slowest],
            forecast={'per_request_usd':average,'requests_per_day':traffic,'per_day_usd':per_day,
                'per_month_usd':per_day*30 if per_day is not None else None,
                'per_year_usd':per_day*365 if per_day is not None else None,
                'observed_calendar_days':observed_days,
                'coverage_note':('Model-call usage or pricing is incomplete. Zero-cost cached replies cannot estimate unpriced model traffic.' if cache_only_sample else
                    ('Uses mean cost of attempts with complete pricing and usage, including zero-cost cached responses, and assumes the observed request mix continues. '
                    + ('Some attempts have unknown usage or pricing, so actual spend may differ further.' if overview['unknown_cost_requests'] else
                       'Reference rates may differ from your Azure deployment agreement.')) if count else
                    'No request observations yet; projections will appear after tracked traffic.')})
    except (OSError, sqlite3.Error):
        result['notes'].insert(0,'Operational metrics are not available. Billing requests remain independent.')
    return result
