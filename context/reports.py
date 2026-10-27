"""Validated complete report datasets for downloads, outside model history."""

import csv
import io
import re
from collections.abc import Mapping

from context.tables import MAX_COLUMNS, _cell, _encoded, _label

MAX_DATASETS = 16
MAX_REPORT_ROWS = 10_000
MAX_REPORT_BYTES = 8_000_000


def _metadata(value, depth=0, budget=None):
    if budget is None:
        budget = [0]
    budget[0] += 1
    if depth > 4 or budget[0] > 2048:
        raise ValueError('Report metadata is too deeply nested.')
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise ValueError('Too much report metadata.')
        return {_label(key, 100, 'metadata field'): _metadata(item, depth + 1, budget)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > 1024:
            raise ValueError('Too much report metadata.')
        return [_metadata(item, depth + 1, budget) for item in value]
    return _cell(value)


def report_artifact(title, datasets, *, coverage=None, notes=None):
    """Keep complete, bounded scalar datasets returned by a trusted report tool."""
    if not isinstance(datasets, (list, tuple)) or not 1 <= len(datasets) <= MAX_DATASETS:
        raise ValueError('Invalid report datasets.')
    result = {'title': _label(title, 160, 'report title'), 'datasets': []}
    if coverage is not None:
        result['coverage'] = _metadata(coverage)
    if notes is not None:
        if not isinstance(notes, (list, tuple)) or len(notes) > 30:
            raise ValueError('Invalid report notes.')
        result['notes'] = [_label(note, 1000, 'report note') for note in notes]
    names, size = set(), len(_encoded(result))
    for value in datasets:
        if not isinstance(value, Mapping):
            raise ValueError('Invalid report dataset.')
        name = value.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name) or name in names:
            raise ValueError('Report datasets need unique names.')
        names.add(name)
        columns = value.get('columns')
        if not isinstance(columns, (list, tuple)) or not 1 <= len(columns) <= MAX_COLUMNS:
            raise ValueError('Invalid report columns.')
        columns = [_label(column, 100, 'report column') for column in columns]
        if len(set(columns)) != len(columns):
            raise ValueError('Report columns must be unique.')
        source = value.get('rows')
        if not isinstance(source, (list, tuple)) or len(source) > MAX_REPORT_ROWS:
            raise ValueError('Report dataset exceeds the row limit.')
        total = value.get('total_rows', len(source))
        if type(total) is not int or not len(source) <= total <= 1_000_000_000:
            raise ValueError('Invalid report total row count.')
        truncated = value.get('truncated', False)
        if type(truncated) is not bool:
            raise ValueError('Invalid report truncation flag.')
        dataset = {'name': name, 'title': _label(value.get('title'), 160, 'report dataset title'),
                   'columns': columns, 'rows': [], 'row_count': len(source),
                   'total_rows': total, 'truncated': truncated or total > len(source)}
        for field in ('scope', 'grain'):
            if value.get(field) is not None:
                dataset[field] = _label(value[field], 1000, 'report ' + field)
        for row in source:
            if isinstance(row, Mapping) and set(row) == set(columns):
                clean = {column: _cell(row[column]) for column in columns}
            elif isinstance(row, (list, tuple)) and len(row) == len(columns):
                clean = {column: _cell(cell) for column, cell in zip(columns, row)}
            else:
                raise ValueError('Report row columns do not match.')
            size += len(_encoded(clean))
            if size > MAX_REPORT_BYTES:
                raise ValueError('Report exceeds the download size limit.')
            dataset['rows'].append(clean)
        size += len(_encoded({key: val for key, val in dataset.items() if key != 'rows'}))
        result['datasets'].append(dataset)
    if any(dataset['truncated'] for dataset in result['datasets']) and isinstance(result.get('coverage'), dict):
        result['coverage'].update(complete=False, truncated=True)
    if len(_encoded(result)) > MAX_REPORT_BYTES:
        raise ValueError('Report exceeds the download size limit.')
    return result


def sanitize_reports(reports):
    if not isinstance(reports, (list, tuple)) or not reports or not isinstance(reports[0], Mapping):
        return []
    report = reports[0]
    try:
        return [report_artifact(report.get('title'), report.get('datasets'),
                                coverage=report.get('coverage'), notes=report.get('notes'))]
    except (TypeError, ValueError, OverflowError):
        return []


def csv_bytes(dataset):
    """Excel-friendly UTF-8 CSV; text values cannot execute spreadsheet formulas."""
    def safe(value):
        # Exact decimal strings are how billing tools carry money. Keep negative
        # adjustments numeric in spreadsheets without permitting expressions.
        decimal = isinstance(value, str) and re.fullmatch(r'[+-]?\d+(?:\.\d+)?', value)
        if isinstance(value, str) and not decimal and value.lstrip(' \t\r\n').startswith(('=', '+', '-', '@')):
            return "'" + value
        return value
    target = io.StringIO(newline='')
    writer = csv.writer(target)
    writer.writerow([safe(column) for column in dataset['columns']])
    writer.writerows([[safe(row[column]) for column in dataset['columns']] for row in dataset['rows']])
    return target.getvalue().encode('utf-8-sig')
