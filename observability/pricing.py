"""Approximate model-token costs, with versioned administrator-supplied rates."""
from contextlib import closing
from decimal import Decimal, InvalidOperation

from config import settings
from observability import store


REFERENCE_MODEL = 'gpt-5.4-mini'
REFERENCE_URL = 'https://developers.openai.com/api/docs/models/gpt-5.4-mini'
REFERENCE_LABEL = 'OpenAI published reference; verify your Azure rates'
REFERENCE_MODELS = frozenset({'gpt-5.4-mini', 'gpt-5.4-mini-2026-03-17'})


def _rate(value):
    if isinstance(value, bool):
        raise ValueError('Rates must be finite, nonnegative numbers per million tokens.')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Rates must be finite, nonnegative numbers per million tokens.') from None
    if not number.is_finite() or not 0 <= number <= Decimal('1000000'):
        raise ValueError('Rates must be finite, nonnegative numbers per million tokens.')
    if number.as_tuple().exponent < -8:
        raise ValueError('Rates support at most eight decimal places.')
    return format(number, 'f')


def _identity(value, name):
    if not isinstance(value, str) or not value or store._code(value, 150) != value:
        raise ValueError(f'Provide a valid {name} identifier.')
    return value


def _reference(model, deployment):
    known = model in REFERENCE_MODELS
    return {'model':model, 'deployment':deployment,
        'input_per_million':0.75 if known else None,
        'output_per_million':4.50 if known else None,
        'source_label':REFERENCE_LABEL if known else 'Rates not configured for this model',
        'source_url':REFERENCE_URL if known else None,
        'effective_at':None, 'source_checked_at':'2026-10-31' if known else None,
        'currency':'USD', 'configured':False,
        'basis':'All reported input tokens use the normal input rate; cached-input savings are not available in this client usage breakdown. Output includes reasoning tokens already.',
        'is_reference':known}


def rate_for(conn, model, deployment, at=None):
    """Select the rate effective when the request began, including writer backlog."""
    at = at or store.utc_now()
    row = conn.execute('''SELECT * FROM pricing_rates WHERE model=? AND deployment=?
        AND effective_at<=? ORDER BY effective_at DESC,id DESC LIMIT 1''',
        (model, deployment, at)).fetchone()
    if not row:
        return _reference(model, deployment)
    return {'model':model,'deployment':deployment,
        'input_per_million':float(row['input_per_million']),
        'output_per_million':float(row['output_per_million']),
        'source_label':row['source_label'],'source_url':None,
        'effective_at':row['effective_at'],'source_checked_at':None,
        'currency':'USD','configured':True,'is_reference':False,
        'basis':'Configured normal input and output rates per million tokens; no cache-token discount is assumed.'}


def get_pricing(model=None, deployment=None):
    # An unconfigured dashboard offers the mini reference, but events with an
    # unknown actual model are never silently assigned those prices.
    model = model if model is not None else (settings.AZURE_OPENAI_MODEL or REFERENCE_MODEL)
    deployment = deployment if deployment is not None else settings.AZURE_OPENAI_DEPLOYMENT
    try:
        with closing(store.connect()) as conn:
            return rate_for(conn, model, deployment)
    except Exception:
        return _reference(model, deployment)


def save_pricing(input_per_million, output_per_million, model, deployment, source_label='Azure configured rates'):
    model = _identity(model, 'model')
    if not isinstance(deployment, str) or store._code(deployment, 150) != deployment:
        raise ValueError('Provide a valid deployment identifier, or leave it blank when unconfigured.')
    input_rate, output_rate = _rate(input_per_million), _rate(output_per_million)
    if (not isinstance(source_label, str) or not source_label.strip() or len(source_label)>120
            or any(ord(char)<32 for char in source_label)):
        raise ValueError('Use a short rate-source label without line breaks.')
    path = store.database_path()
    store.initialize(path)
    with closing(store.connect(readonly=False, path=path)) as conn:
        conn.execute('INSERT INTO pricing_rates(model,deployment,effective_at,input_per_million,output_per_million,source_label) '
            'VALUES (?,?,?,?,?,?)',(model,deployment,store.utc_now(),input_rate,output_rate,source_label.strip()))
        conn.commit()
    return get_pricing(model, deployment)


def price_event(conn, event):
    """Cost only usage actually reported; failed unreported calls stay unknown."""
    if event['model_calls'] == 0 and event['failed_model_calls'] == 0:
        return {'estimated_cost_usd':0.0, 'cost_complete':1,
            'pricing_source':'No model call', 'input_per_million':None, 'output_per_million':None}
    rate = rate_for(conn, event['model'], event['deployment'], event['occurred_at'])
    known = rate['input_per_million'] is not None and rate['output_per_million'] is not None
    complete = bool(known and event['usage_complete'] and not event['failed_model_calls'])
    if known and (event['usage_complete'] or event['input_tokens'] or event['output_tokens']):
        amount = (Decimal(event['input_tokens'])*Decimal(str(rate['input_per_million']))
                + Decimal(event['output_tokens'])*Decimal(str(rate['output_per_million'])))/Decimal(1000000)
        cost = float(amount.quantize(Decimal('0.000000000001')))
    else:
        cost = None
    return {'estimated_cost_usd':cost, 'cost_complete':int(complete),
        'pricing_source':rate['source_label'],
        'input_per_million':str(rate['input_per_million']) if known else None,
        'output_per_million':str(rate['output_per_million']) if known else None}
