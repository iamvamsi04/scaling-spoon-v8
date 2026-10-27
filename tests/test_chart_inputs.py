"""Input boundaries and observation integrity for every supported chart family."""
import copy
import json
import random
from datetime import datetime, timezone

import pytest

from mcp_server import charts as chart_api
from mcp_server.charts import CHART_TYPES, chart_from_result, prepare_chart
from test_chart_types import sample
from ui.charts import build_chart


BAD_NUMBERS = [None, True, False, '', 'not a number', [], {}, float('nan'),
               float('inf'), -float('inf')]


@pytest.fixture(autouse=True)
def isolated_db():
    # These pure validators/renderers never open a database; avoid seeding one
    # hundreds of times for parameterized malformed JSON values.
    yield


@pytest.mark.parametrize('kind', CHART_TYPES)
@pytest.mark.parametrize('invalid', BAD_NUMBERS)
def test_all_chart_families_reject_invalid_observations(kind, invalid):
    columns, rows, spec = copy.deepcopy(sample(kind))
    field = spec['y'][0] if spec['y'] else spec['x']
    rows[0][columns.index(field)] = invalid
    with pytest.raises(ValueError):
        prepare_chart(columns, rows, spec)


@pytest.mark.parametrize('kind', CHART_TYPES)
@pytest.mark.parametrize('invalid', [None, True, False, [], {}, float('nan'), float('inf')])
def test_all_chart_families_reject_invalid_coordinates(kind, invalid):
    columns, rows, spec = copy.deepcopy(sample(kind))
    rows[0][columns.index(spec['x'])] = invalid
    with pytest.raises(ValueError):
        prepare_chart(columns, rows, spec)


@pytest.mark.parametrize('columns', [None, 1, True, False, {}, 'xv', ['x', 1],
                                     ['x', 'x'], ['', 'v'], ['   ', 'v']])
def test_malformed_column_containers_are_validation_errors(columns):
    with pytest.raises(ValueError):
        prepare_chart(columns, [['A', 1]], {'type': 'bar', 'x': 'x', 'y': ['v']})


@pytest.mark.parametrize('rows', [None, 1, True, False, {}, 'A1', [], [None], [1],
                                  [True], ['A1'], [{'x': 'A', 'v': 1}], [['A']],
                                  [['A', 1, 2]], [['A', 1], None]])
def test_malformed_row_containers_are_validation_errors(rows):
    with pytest.raises(ValueError):
        prepare_chart(['x', 'v'], rows, {'type': 'bar', 'x': 'x', 'y': ['v']})


@pytest.mark.parametrize('invalid', [None, True, False, 1, 1.5, '', 'bar', [],
                                     ['bar'], {'type': []}, {'type': {}},
                                     {'type': 'bar', 'x': [], 'y': ['v']},
                                     {'type': 'bar', 'x': 'x', 'y': {}},
                                     {'type': 'bar', 'x': 'x', 'y': [{}]},
                                     {'type': 'bar', 'x': 'x', 'y': ['v'], 'group': []},
                                     {'type': 'bar', 'x': 'x', 'y': ['v'], 'x_type': []},
                                     {'type': 'bar', 'x': 'x', 'y': ['v'], 'title': True},
                                     {'type': 'bar', 'x': 'x', 'y': ['v'], 'title': 12},
                                     {'type': 'bar', 'x': 'x', 'y': ['v'], 'url': 'https://invalid.example'}])
def test_malformed_specifications_are_validation_errors(invalid):
    with pytest.raises(ValueError):
        prepare_chart(['x', 'v'], [['A', 1]], invalid)


@pytest.mark.parametrize('result', [None, True, [], {}, {'chart': []},
    {'columns': ['x', 'v'], 'rows': [['A', 1]], 'chart': []},
    {'columns': ['x', 'v'], 'rows': [['A', 1]], 'chart': 'bar'},
    {'columns': None, 'rows': [['A', 1]], 'chart': {'type': 'bar', 'x': 'x', 'y': ['v']}},
    {'columns': ['x', 'v'], 'rows': [None], 'chart': {'type': 'bar', 'x': 'x', 'y': ['v']}}])
def test_malformed_tool_results_cannot_escape_chart_validation(result):
    with pytest.raises(ValueError):
        chart_from_result(result)


def _rename_fields(columns, rows, spec):
    replacements = {field: f'{field}. [field] é' for field in columns}
    renamed = {key: replacements[value] if key in ('x', 'group', 'size', 'end') else
               [replacements[field] for field in value] if key == 'y' else value
               for key, value in spec.items()}
    renamed['title'] = 'Sales, returns and products — 東京'
    return [replacements[field] for field in columns], rows, renamed


@pytest.mark.parametrize('kind', CHART_TYPES)
@pytest.mark.parametrize('seed', range(4))
def test_valid_randomized_observations_with_unicode_and_unusual_aliases(kind, seed):
    rng = random.Random(seed)
    columns, rows, spec = copy.deepcopy(sample(kind))
    factor = 10 ** rng.randint(-3, 3)
    for row in rows:
        for index, value in enumerate(row):
            if type(value) in (int, float):
                row[index] = value * factor
            elif value in ('A', 'B'):
                row[index] = {'A': '電子機器 é — 🛒', 'B': 'Office / home [new]'}[value]
    columns, rows, spec = _rename_fields(columns, rows, spec)
    chart = prepare_chart(columns, rows, spec)
    before = copy.deepcopy(chart)
    grammar = build_chart(chart).to_dict(validate=True)
    assert '$schema' in grammar
    assert chart == before
    json.dumps(grammar, allow_nan=False)
    restored = chart_from_result({'columns': columns, 'rows': rows,
        'chart': {key: value for key, value in chart.items() if key != 'data'}})
    assert restored == chart


@pytest.mark.parametrize('kind', CHART_TYPES)
def test_valid_saved_chart_revalidation_preserves_observations(kind):
    chart = prepare_chart(*sample(kind))
    before = copy.deepcopy(chart)
    validated = chart_api.validate_saved_chart(chart)
    assert validated == before
    assert chart == before
    build_chart(validated).to_dict(validate=True)


@pytest.mark.parametrize('value', ['1e-400', '-1e-400'])
def test_nonzero_numeric_strings_cannot_silently_underflow_to_zero(value):
    with pytest.raises(ValueError, match='precision'):
        prepare_chart(['category', 'value'], [['A', value]],
                      {'type':'bar','x':'category','y':['value']})


@pytest.mark.parametrize('kind', ['bar', 'grouped_bar', 'horizontal_bar'])
@pytest.mark.parametrize('coordinate,axis', [('2026-01', 'temporal'), (1, 'numeric')])
def test_continuous_coordinates_keep_grouped_bars_separate(kind, coordinate, axis):
    chart = prepare_chart(['period', 'sales', 'returns'], [[coordinate, 10, 2]],
        {'type':kind,'x':'period','x_type':axis,'y':['sales','returns']})
    grammar = build_chart(chart).to_dict(validate=True)
    channel = 'y' if kind == 'horizontal_bar' else 'x'
    assert grammar['encoding'][channel]['type'] == 'ordinal'
    assert channel+'Offset' in grammar['encoding']
    if axis == 'temporal':
        expression = grammar['encoding'][channel]['axis']['labelExpr']
        assert 'utcFormat' in expression and 'datum.value' in expression


def test_single_month_uses_calendar_label_and_utc_coordinate():
    chart = prepare_chart(['month', 'sales'], [['2026-01', 10]],
        {'type':'bar','x':'month','x_type':'temporal','y':['sales']})
    grammar = build_chart(chart).to_dict(validate=True)
    assert grammar['encoding']['x']['axis']['format'] == '%b %Y'
    assert grammar['data']['values'][0]['x_value'].endswith('Z')
    assert grammar['encoding']['x']['scale']['type'] == 'utc'


@pytest.mark.parametrize('field', ['invoice count', 'orders', 'customers', 'return rate', 'items', 'sales percentage'])
def test_count_and_ratio_aliases_with_spaces_never_claim_currency_units(field):
    chart = prepare_chart(['category',field,'currency'], [['A',2,'USD']],
        {'type':'bar','x':'category','y':[field]})
    assert 'USD' not in build_chart(chart).to_dict()['encoding']['y']['title']


@pytest.mark.parametrize('location', ['column', 'category', 'title'])
def test_invalid_unicode_is_rejected_before_browser_or_dataframe_serialization(location):
    columns, rows, spec = ['x','value'], [['A',1]], {'type':'bar','x':'x','y':['value']}
    if location == 'column':
        columns[0] = spec['x'] = '\ud800'
    elif location == 'category':
        rows[0][0] = '\ud800'
    else:
        spec['title'] = '\ud800'
    with pytest.raises(ValueError, match='Unicode'):
        prepare_chart(columns,rows,spec)


def _corrupt_saved_chart(case):
    chart = prepare_chart(['x', 'v'], [['A', 1]],
                          {'type': 'bar', 'x': 'x', 'y': ['v']})
    if case == 'missing_x':
        del chart['data'][0]['x']
    elif case == 'bad_measure':
        chart['data'][0]['v'] = float('nan')
    elif case == 'extra_spec':
        chart['url'] = 'https://invalid.example'
    else:
        field, value = case
        chart[field] = value
    return chart


@pytest.mark.parametrize('case', ['missing_x', 'bad_measure', 'extra_spec',
    ('data', None), ('data', []), ('data', [None]), ('data', [['A', 1]]),
    ('x', 'missing'), ('y', []), ('y', ['missing']), ('y', [{}]),
    ('currency', {}), ('x_type', 'invalid'), ('type', 'unknown')])
def test_saved_chart_boundary_rejects_corrupt_or_untrusted_payloads(case):
    chart = _corrupt_saved_chart(case)
    with pytest.raises(ValueError):
        chart_api.validate_saved_chart(chart)
    with pytest.raises(ValueError):
        build_chart(chart)


@pytest.mark.parametrize('kind', ['bar', 'line', 'range', 'combo'])
def test_same_instant_with_different_offsets_is_not_two_temporal_points(kind):
    columns, rows, spec = copy.deepcopy(sample(kind))
    spec['x_type'] = 'temporal'
    rows[0][columns.index(spec['x'])] = '2026-01-01T01:00:00+01:00'
    rows[1][columns.index(spec['x'])] = '2026-01-01T00:00:00Z'
    with pytest.raises(ValueError, match='duplicate|coordinates|instant'):
        prepare_chart(columns, rows, spec)


@pytest.mark.parametrize('kind', ['bar', 'line', 'range', 'combo'])
def test_temporal_axes_reject_mixed_timezone_awareness(kind):
    columns, rows, spec = copy.deepcopy(sample(kind))
    spec['x_type'] = 'temporal'
    rows[0][columns.index(spec['x'])] = '2026-01-01T00:00:00'
    rows[1][columns.index(spec['x'])] = '2026-01-02T00:00:00Z'
    with pytest.raises(ValueError, match='timezone|aware'):
        prepare_chart(columns, rows, spec)


def test_aware_temporal_points_are_canonical_utc_and_keep_the_instant():
    chart = prepare_chart(['time', 'value'],
        [['2026-01-01T01:00:00+01:00', 3], ['2026-01-02T02:30:00+01:00', 4]],
        {'type': 'line', 'x': 'time', 'y': ['value'], 'x_type': 'temporal'})
    actual = [datetime.fromisoformat(row['time'].replace('Z', '+00:00')) for row in chart['data']]
    assert actual == [datetime(2026, 1, 1, tzinfo=timezone.utc),
                      datetime(2026, 1, 2, 1, 30, tzinfo=timezone.utc)]
    assert all(day.utcoffset().total_seconds() == 0 for day in actual)
    build_chart(chart).to_dict(validate=True)


@pytest.mark.parametrize('date', ['2026-02-29', '2024-13-01', '2026-01-01T24:00:00',
    '2026-01-01T00:00:00.000001', '2026-01-01T00:00:00.123456Z',
    '2026-01-01T00:00:00+01:60'])
def test_invalid_or_submillisecond_dates_fail_instead_of_collapsing_in_the_browser(date):
    with pytest.raises(ValueError):
        prepare_chart(['time', 'value'], [[date, 3]],
            {'type': 'line', 'x': 'time', 'y': ['value'], 'x_type': 'temporal'})


@pytest.mark.parametrize('date', ['2024-02-29', '2026-01', '2026',
    '2026-01-01T00:00:00.123Z', '2026-01-01T00:00:00+05:30'])
def test_valid_dates_months_years_and_millisecond_timestamps_render(date):
    chart = prepare_chart(['time', 'value'], [[date, 3]],
        {'type': 'line', 'x': 'time', 'y': ['value'], 'x_type': 'temporal'})
    build_chart(chart).to_dict(validate=True)


@pytest.mark.parametrize('kind', CHART_TYPES)
def test_currency_alias_case_cannot_bypass_mixed_currency_guard(kind):
    columns, rows, spec = sample(kind)
    with pytest.raises(ValueError, match='currency'):
        prepare_chart(columns + ['Currency'],
            [row + ['USD' if index == 0 else 'EUR'] for index, row in enumerate(rows)], spec)


@pytest.mark.parametrize('currency', [None, '', 'US', 'USDD', 'U$D', '\u00dcSD', '123'])
def test_currency_metadata_requires_a_three_letter_ascii_code(currency):
    with pytest.raises(ValueError, match='currency'):
        prepare_chart(['x', 'v', 'currency'], [['A', 1, currency]],
            {'type': 'bar', 'x': 'x', 'y': ['v']})


def test_currency_alias_is_case_insensitive_and_normalized():
    chart = prepare_chart(['x', 'v', 'Currency'], [['A', 1, ' usd ']],
        {'type': 'bar', 'x': 'x', 'y': ['v']})
    assert chart['currency'] == 'USD'
    build_chart(chart).to_dict(validate=True)


@pytest.mark.parametrize('value', [2**53 + 1, -(2**53 + 1), str(2**53 + 1),
                                    str(2**53 + 1) + '.0', '9.007199254740993e15'])
@pytest.mark.parametrize('field', ['x', 'v'])
def test_integers_that_lose_precision_cannot_silently_change_the_observation(value, field):
    row = [1, 2]
    row[['x', 'v'].index(field)] = value
    with pytest.raises(ValueError):
        prepare_chart(['x', 'v'], [row], {'type': 'scatter', 'x': 'x', 'y': ['v']})


@pytest.mark.parametrize('kind', ['grouped_bar', 'stacked_bar', 'line', 'normalized_bar'])
def test_humanized_measure_names_do_not_merge_distinct_series(kind):
    chart = prepare_chart(['category', 'gross_total', 'gross total'],
        [['A', 10, 20], ['B', 30, 40]],
        {'type': kind, 'x': 'category', 'y': ['gross_total', 'gross total']})
    grammar = build_chart(chart).to_dict(validate=True)
    data = grammar['data'].get('values')
    if data is None:
        data = grammar['datasets'][grammar['data']['name']]
    assert len({row['series'] for row in data}) == 2
    assert sorted(row['value'] for row in data) == [10, 20, 30, 40]


@pytest.mark.parametrize('kind', CHART_TYPES)
def test_each_chart_accepts_its_complete_row_limit_and_rejects_excess_or_truncation(kind):
    columns, template, spec = copy.deepcopy(sample(kind))
    limit = 12 if kind == 'pie' else 1000
    rows = []
    for index in range(limit + 1):
        row = copy.deepcopy(template[0])
        if kind == 'timeline':
            row[columns.index(spec['group'])] = f'Order {index}'
        else:
            row[columns.index(spec['x'])] = index + 1 if kind in (
                'scatter', 'bubble', 'histogram') else f'Item {index}'
        rows.append(row)
    chart = prepare_chart(columns, rows[:limit], spec)
    assert len(chart['data']) == limit
    build_chart(chart).to_dict(validate=True)
    with pytest.raises(ValueError):
        prepare_chart(columns, rows, spec)
    with pytest.raises(ValueError, match='truncated'):
        prepare_chart(columns, rows[:limit], spec, truncated=True)


@pytest.mark.parametrize('kind', ['stacked_bar', 'normalized_bar'])
@pytest.mark.parametrize('values', [(10, 5), (0, 0)])
def test_single_coordinate_stacked_bars_keep_components_or_reject_zero_share(kind, values):
    columns = ['category', 'sales', 'refunds']
    rows = [['January', *values]]
    spec = {'type': kind, 'x': 'category', 'y': ['sales', 'refunds']}
    if kind == 'normalized_bar' and not any(values):
        with pytest.raises(ValueError, match='positive'):
            prepare_chart(columns, rows, spec)
        return
    chart = prepare_chart(columns, rows, spec)
    grammar = build_chart(chart).to_dict(validate=True)
    assert grammar['mark']['type'] == 'bar'
    assert grammar['encoding']['y']['stack'] == ('normalize' if kind == 'normalized_bar' else 'zero')
    assert chart['data'][0]['sales'] == values[0]
    assert chart['data'][0]['refunds'] == values[1]
