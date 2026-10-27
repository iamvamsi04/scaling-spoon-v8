"""Bounded tables made from executed tool results, separate from reply text."""

import json
import math
from collections.abc import Mapping


MAX_TABLES = 12
MAX_ROWS = 1000
MAX_COLUMNS = 30
MAX_CELL_LENGTH = 2000
MAX_TABLE_BYTES = 256_000
MAX_TOTAL_BYTES = 1_000_000


def _label(value, maximum, name):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'Invalid table {name}.')
    if any(ord(char) < 32 and char not in '\n\t' for char in value):
        raise ValueError(f'Invalid table {name}.')
    return value.strip()


def _cell(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, str) and len(value) <= MAX_CELL_LENGTH:
        return value
    raise ValueError('Table cells must be finite JSON scalar values.')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def table_artifact(title, columns, rows, *, total_rows=None, coverage=None, truncated=False):
    """Validate a table without interpreting or inventing any financial values.

    Rows may use column-keyed dictionaries or ordered scalar lists. The result
    always uses dictionaries in the declared column order. A capped result states
    its complete row count; a caller-provided total may also describe SQL paging.
    """
    title = _label(title, 160, 'title')
    if not isinstance(columns, (list, tuple)) or not 1 <= len(columns) <= MAX_COLUMNS:
        raise ValueError('Invalid table columns.')
    columns = [_label(column, 100, 'column') for column in columns]
    if len(set(columns)) != len(columns):
        raise ValueError('Table columns must be unique.')
    if not isinstance(rows, (list, tuple)):
        raise ValueError('Table rows must be a list.')
    if type(truncated) is not bool:
        raise ValueError('Invalid table truncation flag.')
    if total_rows is None and not truncated:
        total_rows = len(rows)
    if total_rows is not None and (type(total_rows) is not int or not len(rows) <= total_rows <= 1_000_000_000):
        raise ValueError('Invalid table total row count.')
    if coverage is not None:
        coverage = _label(coverage, 500, 'coverage')
    clean_rows = []
    # Validate each displayed row; rows outside the display cap are never rendered.
    for row in rows[:MAX_ROWS]:
        if isinstance(row, Mapping):
            if set(row) != set(columns):
                raise ValueError('Table row columns do not match.')
            clean_rows.append({column: _cell(row[column]) for column in columns})
        elif isinstance(row, (list, tuple)) and len(row) == len(columns):
            clean_rows.append({column: _cell(value) for column, value in zip(columns, row)})
        else:
            raise ValueError('Invalid table row.')
    result = {'title': title, 'columns': columns, 'rows': clean_rows,
              'row_count': len(clean_rows), 'total_rows': total_rows,
              'truncated': truncated or len(clean_rows) < total_rows}
    if coverage:
        result['coverage'] = coverage
    if len(_encoded(result)) > MAX_TABLE_BYTES:
        raise ValueError('Table is too large to display safely.')
    return result


def sanitize_tables(tables):
    """Discard malformed artifacts while preserving other valid chat artifacts."""
    if not isinstance(tables, (list, tuple)):
        return []
    result, seen, size = [], set(), 0
    for value in tables[:MAX_TABLES]:
        if not isinstance(value, Mapping):
            continue
        try:
            table = table_artifact(value.get('title'), value.get('columns'), value.get('rows'),
                                   total_rows=value.get('total_rows'), coverage=value.get('coverage'),
                                   truncated=value.get('truncated',False))
            encoded = _encoded(table)
        except (TypeError, ValueError, OverflowError):
            continue
        if encoded in seen or size + len(encoded) > MAX_TOTAL_BYTES:
            continue
        seen.add(encoded)
        size += len(encoded)
        result.append(table)
    return result
