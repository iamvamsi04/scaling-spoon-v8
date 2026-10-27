"""Validate declarative charts made only from executed query rows."""
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

CHART_TYPES = ('bar','grouped_bar','horizontal_bar','stacked_bar','normalized_bar',
               'line','pie','scatter','bubble','histogram','boxplot','heatmap',
               'range','combo','timeline')
MAX_CHART_ROWS = 1000
SERIES_TYPES = {'bar','grouped_bar','horizontal_bar','stacked_bar','normalized_bar',
                'line'}
CHART_ALIASES = {'box_plot':'boxplot','heat_map':'heatmap', 'combination':'combo',
                 'combination_chart':'combo','gantt':'timeline'}


def canonical_chart_type(kind):
    if not isinstance(kind, str):
        return kind
    kind = kind.strip().lower().replace(' ', '_').replace('-', '_').removesuffix('_chart')
    return CHART_ALIASES.get(kind, kind)
SPEC_FIELDS = {'type','title','x','y','group','size','end','x_type','bins'}
CHART_GUIDE = """
Chart contract for execute_sql_query:
- Types: bar, grouped_bar, horizontal_bar, stacked_bar, normalized_bar, line,
  pie, scatter, bubble, histogram, boxplot, heatmap, range, combo, timeline.
  No fixed number of charts per response. Choose the clearest applicable type;
  do not repeat the same analysis in multiple styles unless requested.
- x names a returned column; y lists numeric columns. Series charts accept 1-4
  measures of one unit; optional group requires one y and unique x/group pairs.
  Stacked/normalized charts require nonnegative values; normalized totals > 0.
- x_type categorical(default), numeric or temporal for series/range/combo;
  ISO dates/times, YYYY-MM and YYYY are accepted. Keep temporal data in order.
- scatter: numeric x, one y, optional group. bubble also requires positive size.
- histogram: raw numeric x, y:[], bins 2-50. boxplot: categorical x, one raw y,
  optional group. Distributions require complete observations, not aggregates.
- heatmap: x and group are distinct category dimensions, one numeric y, unique pairs.
- range: y:[lower,upper], lower<=upper. combo: y:[bar_measure,line_measure],
  separately labelled axes.
- timeline: x start, end column, group task, y:[], actual full ISO date/time intervals.
- pie: one nonnegative y, positive total, <=12 categories. Prefer sorted horizontal
  bars when labels are long or slices are hard to distinguish.
- 1-1000 complete rows, no null coordinates/measures, no truncated results. Include
  normalized currency as returned metadata for money; never use USD as an axis or
  series. Separate different currencies. No arbitrary code, URLs or raw Vega specs.
- Generate all requested applicable charts; explain unavailable data instead of
  fabricating it. Claim a chart only after the tool attached it successfully.
- If a requested type is unsupported, explain and offer a supported alternative;
  never silently substitute another style or a whole dashboard.
"""


def numeric(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError('Chart measures must be finite numbers, without null values.')
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        raise ValueError('Chart integers exceed exact browser precision. Scale the measure or return identifiers as text.')
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Chart measures must be numeric.') from None
    if not math.isfinite(number):
        raise ValueError('Chart measures must be finite.')
    if isinstance(value, str) and abs(number) > 2**53 - 1:
        raise ValueError('Chart integers exceed exact browser precision. Scale the measure or return identifiers as text.')
    if isinstance(value, str) and number == 0:
        try:
            underflow = Decimal(value) != 0
        except InvalidOperation:
            raise ValueError('Chart measures must be numeric.') from None
        if underflow:
            raise ValueError('Chart measure is below browser numeric precision. Scale the measure before charting.')
    return number


def label(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError('Chart category values must be non-null text or numbers.')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Chart category numbers must be finite.')
    try:
        result = str(value)
        result.encode('utf-8')
    except (ValueError, UnicodeError):
        raise ValueError('Chart category labels require valid Unicode text of at most 200 characters.') from None
    if not result.strip() or len(result)>200:
        raise ValueError('Chart category labels require 1-200 characters.')
    return result


def temporal(value, periods=False):
    if periods and isinstance(value,str):
        if re.fullmatch(r'\d{4}-\d{2}',value):
            value += '-01'
        elif re.fullmatch(r'\d{4}',value):
            value += '-01-01'
    if not isinstance(value,str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?', value):
        raise ValueError('Temporal coordinates require ISO date or date-time strings.')
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        raise ValueError('Temporal coordinates require valid ISO dates or date-times.') from None
    offset = re.search(r'[+-](\d{2}):(\d{2})$', value)
    if offset and (int(offset[1]) > 23 or int(offset[2]) > 59):
        raise ValueError('Temporal timezone offsets require valid hours and minutes.')
    if parsed.microsecond % 1000:
        raise ValueError('Temporal coordinates support millisecond precision. Group finer timestamps in SQL before charting.')
    return parsed


def prepare_chart(columns, rows, spec, truncated=False):
    if not isinstance(spec,dict) or set(spec)-SPEC_FIELDS:
        raise ValueError('Chart accepts only type, title, x, y, group, size, end, x_type and bins.')
    spec = dict(spec)
    spec['type'] = canonical_chart_type(spec.get('type'))
    kind = spec['type']
    if not isinstance(kind,str) or kind not in CHART_TYPES:
        raise ValueError('Unsupported chart type. Choose one of: '+', '.join(CHART_TYPES)+'.')
    if isinstance(spec.get('y'), str):
        spec['y'] = [spec['y']]
    if spec.get('group') == spec.get('x') and spec.get('type') in SERIES_TYPES | {'boxplot'}:
        # Grouping a category by itself adds no information and duplicates an axis.
        spec.pop('group', None)
    kind,x,ys=spec.get('type'),spec.get('x'),spec.get('y')
    if (not isinstance(columns, (list, tuple)) or not columns or
            not all(isinstance(c,str) and c.strip() for c in columns) or len(columns)!=len(set(columns))):
        raise ValueError('Give every selected SQL column a unique text alias.')
    try:
        for column in columns:
            column.encode('utf-8')
    except UnicodeError:
        raise ValueError('SQL column aliases require valid Unicode text.') from None
    if not isinstance(x,str) or x not in columns or not isinstance(ys,list):
        raise ValueError('Chart x must name a returned column and y must be a list of columns.')
    count = 0 if kind in ('histogram','timeline') else 2 if kind in ('range','combo') else None
    single = kind in ('pie','scatter','bubble','boxplot','heatmap')
    if (count is not None and len(ys)!=count) or (single and len(ys)!=1) or (count is None and not single and not 1<=len(ys)<=4):
        raise ValueError(f'{kind} has the wrong number of y columns. Check the chart contract.')
    if any(not isinstance(y,str) or y not in columns or y==x for y in ys) or len(ys)!=len(set(ys)):
        raise ValueError('Chart y columns must exist, be distinct, and differ from x.')
    allowed_extra = ({'group'} if kind in SERIES_TYPES or kind in ('scatter','boxplot','heatmap') else
                     {'group','size'} if kind=='bubble' else {'group','end'} if kind=='timeline' else
                     {'bins'} if kind=='histogram' else set())
    grouped_series = kind in SERIES_TYPES and 'group' in spec
    if grouped_series and len(ys) != 1:
        raise ValueError('A series group requires exactly one y measure.')
    if set(spec).intersection({'group','size','end','bins'})-allowed_extra:
        raise ValueError('This chart type does not support one of the supplied optional fields.')
    required = {'group'} if kind=='heatmap' else {'size'} if kind=='bubble' else {'group','end'} if kind=='timeline' else set()
    if not required<=set(spec):
        raise ValueError(f'{kind} requires: '+', '.join(sorted(required))+'.')
    extras={k:spec[k] for k in ('group','size','end') if k in spec}
    if any(not isinstance(v,str) or v not in columns for v in extras.values()) or len(set([x,*ys,*extras.values()]))!=len([x,*ys,*extras.values()]):
        raise ValueError('Chart coordinate columns must exist and be distinct.')
    default_axis='numeric' if kind in ('scatter','bubble','histogram') else 'temporal' if kind=='timeline' else 'categorical'
    axis=spec.get('x_type',default_axis)
    if not isinstance(axis,str) or axis not in ('categorical','numeric','temporal'):
        raise ValueError('x_type must be categorical, numeric or temporal.')
    fixed = kind in ('scatter','bubble','histogram','pie','heatmap','boxplot','timeline')
    if fixed and axis!=default_axis:
        raise ValueError(f'{kind} requires {default_axis} x coordinates.')
    bins=spec.get('bins',10)
    if kind=='histogram' and (type(bins) is not int or not 2<=bins<=50):
        raise ValueError('Histogram bins must be an integer from 2 to 50.')
    if not isinstance(truncated, bool):
        raise ValueError('Chart truncation metadata must be true or false.')
    if truncated:
        raise ValueError('Query results were truncated. Aggregate or narrow the query before charting; distributions require complete observations.')
    if (not isinstance(rows, (list, tuple)) or not rows or len(rows)>MAX_CHART_ROWS or
            any(not isinstance(r, (list, tuple)) or len(r)!=len(columns) for r in rows)):
        raise ValueError('Charts require 1-1000 complete rows. Aggregate or narrow the query.')
    if kind == 'pie' and len(rows)>12:
        raise ValueError('Pie charts support at most 12 categories; group smaller categories or use bars.')
    title=spec.get('title','Query results')
    if not isinstance(title,str) or not title.strip() or len(title)>160:
        raise ValueError('Chart title must be text of at most 160 characters.')
    try:
        title.encode('utf-8')
    except UnicodeError:
        raise ValueError('Chart title requires valid Unicode text.') from None
    currency_columns = [c for c in columns if c.lower() == 'currency']
    if len(currency_columns) > 1:
        raise ValueError('Return one currency metadata column with a unique alias.')
    raw_currencies=[r[columns.index(currency_columns[0])] for r in rows] if currency_columns else []
    if any(not isinstance(c,str) or not re.fullmatch(r'[A-Za-z]{3}', c.strip()) for c in raw_currencies):
        raise ValueError('Chart currency metadata must be a three-letter currency code.')
    currencies={c.strip().upper() for c in raw_currencies}
    if len(currencies)>1:
        raise ValueError('Chart one currency at a time. Filter currency and return it as currency.')
    if x.lower()=='currency' or any(y.lower()=='currency' for y in ys) or extras.get('size','').lower()=='currency':
        raise ValueError('Currency is metadata, not a chart axis or measure. Select a meaningful category, date or numeric measure.')
    if axis == 'categorical' and currencies and all(
            isinstance(row[columns.index(x)], str) and row[columns.index(x)].strip().upper() in currencies
            for row in rows):
        raise ValueError('Currency is metadata, not a chart axis. Select a meaningful category instead of a constant currency label.')
    currency_group = 'group' in extras and currencies and all(
        isinstance(row[columns.index(extras['group'])], str) and
        row[columns.index(extras['group'])].strip().upper() in currencies for row in rows)
    if extras.get('group','').lower()=='currency' or currency_group:
        if kind=='heatmap' or kind=='timeline':
            raise ValueError('Currency is metadata, not a chart dimension. Return another meaningful group column.')
        extras.pop('group')
        spec.pop('group',None)
        grouped_series=False
    elif 'group' in extras and kind in SERIES_TYPES | {'scatter','bubble','boxplot'}:
        if len({label(row[columns.index(extras['group'])]) for row in rows}) == 1:
            # A single currency or constant label is metadata, not another plotted series.
            extras.pop('group')
            spec.pop('group',None)
            grouped_series=False
    records,seen=[],set()
    stacked=kind in ('stacked_bar','normalized_bar')
    normalized=kind == 'normalized_bar'
    nonnegative=stacked or kind == 'pie'
    time_zones=set()
    positions = {column: i for i, column in enumerate(columns)}
    for row in rows:
        raw=row[positions[x]]
        if axis == 'temporal':
            start = temporal(raw, periods=kind != 'timeline')
            time_zones.add(start.tzinfo is not None)
            if start.tzinfo is not None:
                try:
                    start = start.astimezone(timezone.utc)
                except (ValueError, OverflowError):
                    raise ValueError('Temporal coordinates exceed the supported date range after timezone conversion.') from None
            coordinate = start.isoformat()
        else:
            coordinate=numeric(raw) if axis=='numeric' else label(raw)
        values={y:numeric(row[positions[y]]) for y in ys}
        record={x:coordinate,**values}
        if 'group' in extras:
            record[extras['group']]=label(row[columns.index(extras['group'])])
        if 'size' in extras:
            record[extras['size']]=numeric(row[columns.index(extras['size'])])
            if record[extras['size']]<=0:
                raise ValueError('Bubble sizes must be positive numeric values.')
        if 'end' in extras:
            end=temporal(row[columns.index(extras['end'])])
            time_zones.add(end.tzinfo is not None)
            if end.tzinfo is not None:
                try:
                    end = end.astimezone(timezone.utc)
                except (ValueError, OverflowError):
                    raise ValueError('Timeline dates exceed the supported date range after timezone conversion.') from None
            if len(time_zones)>1:
                raise ValueError('Temporal coordinates require consistently timezone-aware or timezone-free dates.')
            if end<start:
                raise ValueError('Timeline end must not precede its start.')
            record[extras['end']]=end.isoformat()
        key=(coordinate,record[extras['group']]) if kind in ('heatmap','timeline') or grouped_series else coordinate
        if kind not in ('scatter','bubble','histogram','boxplot') and key in seen:
            raise ValueError('Group duplicate chart coordinates in SQL before charting. Normalize currency before grouping, including legacy NULL/blank values; use an outer CTE to group the resolved currency, not the raw source column. Do not sum averages or ratios to repair duplicates.')
        seen.add(key)
        if nonnegative and any(v<0 for v in values.values()):
            raise ValueError(f'{kind} requires nonnegative values.')
        if stacked and not math.isfinite(sum(values.values())):
            raise ValueError('Stacked totals overflow. Scale or narrow the measures.')
        if normalized and not grouped_series and sum(values.values())<=0:
            raise ValueError('Percentage charts require a positive total for each x value.')
        if kind=='range' and values[ys[0]]>values[ys[1]]:
            raise ValueError('Range lower bound must not exceed upper bound.')
        records.append(record)
    if len(time_zones)>1:
        raise ValueError('Temporal coordinates require consistently timezone-aware or timezone-free dates.')
    if grouped_series and normalized:
        groups = {}
        for record in records:
            groups[record[x]] = groups.get(record[x], 0) + record[ys[0]]
        if any(not math.isfinite(total) or total <= 0 for total in groups.values()):
            raise ValueError('Percentage charts require a finite positive total for each x value.')
    if kind == 'pie' and sum(r[ys[0]] for r in records)<=0:
        raise ValueError(f'{kind} needs a positive total.')
    totals=[sum(r[y] for r in records) for y in ys]
    if any(not math.isfinite(v) for v in totals):
        raise ValueError('Chart totals overflow. Scale or narrow the measures.')
    # Finite inputs can still produce an infinite browser scale.
    domains = [[r[y] for r in records] for y in ys]
    if axis == 'numeric':
        domains.append([r[x] for r in records])
    if kind == 'range':
        domains.append([r[y] for r in records for y in ys])
    if any(not math.isfinite(max(domain)-min(domain)) for domain in domains):
        raise ValueError('Chart scale overflows. Scale or narrow the measures.')
    return {'type':kind,'title':title,'x':x,'y':ys,'currency':next(iter(currencies),None),
            'x_type':axis,**extras,**({'bins':bins} if kind=='histogram' else {}),'data':records}


def chart_from_result(result):
    if (not isinstance(result, dict) or not isinstance(result.get('chart'), dict) or
            'columns' not in result or 'rows' not in result):
        raise ValueError('Chart requires metadata and complete executed query columns and rows.')
    metadata=result['chart']
    return prepare_chart(result['columns'],result['rows'],
                         {k:v for k,v in metadata.items() if k in SPEC_FIELDS},result.get('truncated',False))


def validate_saved_chart(chart):
    """Apply the same input contract to persisted artifacts before rendering."""
    if (not isinstance(chart, dict) or set(chart) - (SPEC_FIELDS | {'data', 'currency'}) or
            not isinstance(chart.get('data'), list) or not chart['data'] or
            not all(isinstance(row, dict) for row in chart['data'])):
        raise ValueError('Saved chart requires supported metadata and complete data records.')
    columns = list(chart['data'][0])
    if any(set(row) != set(columns) for row in chart['data']):
        raise ValueError('Saved chart rows must contain the same coordinate and measure columns.')
    rows = [[row[column] for column in columns] for row in chart['data']]
    if chart.get('currency') is not None:
        if any(isinstance(column, str) and column.lower() == 'currency' for column in columns):
            raise ValueError('Saved chart currency must be metadata, not a coordinate.')
        columns.append('currency')
        rows = [row + [chart['currency']] for row in rows]
    return prepare_chart(columns, rows, {k:v for k,v in chart.items() if k in SPEC_FIELDS})
