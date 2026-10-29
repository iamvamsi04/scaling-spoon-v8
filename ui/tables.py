"""Render validated data artifacts without exposing tool JSON in chat."""

from html import escape
from urllib.parse import urlsplit

import streamlit as st
from context.tables import sanitize_tables


def capital_start(value):
    """Uppercase the first cased character, retaining every other character."""
    for index, char in enumerate(value):
        if char.lower() != char.upper():
            return value[:index] + char.upper() + value[index + 1:]
    return value


def exact_token(value):
    """Standalone mixed-case units and model/identifier tokens retain their case."""
    return (not any(char.isspace() for char in value)
            and (any(char.isupper() for char in value) and any(char.islower() for char in value)
                 or any(char.isalpha() for char in value) and any(char.isdigit() for char in value)))


def display_heading(value):
    return value if exact_token(value) else capital_start(value)


def display_cell(column, value):
    if not isinstance(value, str):
        return value
    # Column metadata protects exact identifiers and physical units. This is
    # presentation only; no conversation, approval or tool decision is made here.
    words = column.casefold().replace('_', ' ').replace('-', ' ').split()
    key = ''.join(words)
    if (words and words[-1] in {'id', 'ids', 'code', 'codes', 'sku', 'unit', 'units', 'hash', 'email', 'url', 'uri', 'path', 'filename'}
            or key in {'order', 'invoice', 'reference', 'currency', 'filename', 'path'}):
        return value
    if exact_token(value):
        return value  # A standalone model/code or mixed-case unit is an exact token.
    if '@' in value and not any(char.isspace() for char in value):
        return value
    try:
        if urlsplit(value).scheme and '://' in value:
            return value
    except ValueError:
        pass
    return capital_start(value)


def display_rows(columns, rows):
    """Make display copies; source values remain available unchanged for tools."""
    return [{column: display_cell(column, row[column]) for column in columns} for row in rows]


def full_table_html(title, columns, rows):
    """A wrapping, two-axis scroll view without ellipses or clipped cell text."""
    heads = ''.join('<th scope="col">' + escape(display_heading(column)) + '</th>' for column in columns)
    body = ''.join('<tr>' + ''.join('<td>' + escape('' if row[column] is None else str(row[column])) + '</td>'
                                  for column in columns) + '</tr>' for row in rows)
    return '''<style>
.billing-full-table {max-height:480px;max-width:100%;overflow:auto;border:1px solid rgba(128,128,128,.4);border-radius:4px;}
.billing-full-table table {border-collapse:collapse;min-width:100%;width:max-content;}
.billing-full-table th,.billing-full-table td {padding:.5rem .75rem;text-align:left;vertical-align:top;border-bottom:1px solid rgba(128,128,128,.25);}
.billing-full-table th {position:sticky;top:0;background:var(--secondary-background-color,Canvas);white-space:nowrap;z-index:1;}
.billing-full-table td {min-width:8rem;max-width:32rem;white-space:pre-wrap;overflow-wrap:anywhere;}
</style><div class="billing-full-table" role="region" tabindex="0" aria-label="''' + escape(title, quote=True) + ''' full table content"><table><thead><tr>''' + heads + '</tr></thead><tbody>' + body + '</tbody></table></div>'


def render_tables(tables):
    for table in sanitize_tables(tables):
        import pandas as pd
        columns = [column for column in table['columns']
                   if column.casefold().replace('_', '').replace('-', '').replace(' ', '') not in {'sku', 'itemcode'}]
        if not columns:
            continue
        rows = display_rows(columns, table['rows'])
        st.text(capital_start(table['title']))
        column_config = {column: st.column_config.Column(display_heading(column),
            width='large' if any(isinstance(row[column], str) for row in rows) else 'small') for column in columns}
        st.dataframe(pd.DataFrame(rows, columns=columns), hide_index=True,
                     use_container_width=True, height=min(480, max(90, 38 + 35 * len(rows))),
                     column_config=column_config)
        if rows:
            with st.expander('Read full table content'):
                st.caption('Scroll in either direction. Long and multiline cells wrap without hiding their text.')
                st.html(full_table_html(table['title'], columns, rows))
        if table.get('coverage'):
            st.caption(table['coverage'])
        if table['truncated']:
            st.caption(f"Showing {table['row_count']} of {table['total_rows']} rows."
                       if table['total_rows'] is not None else
                       f"Showing {table['row_count']} rows. More matching records exist; the total count is not available.")
        elif not table['row_count']:
            st.caption('No rows were returned for this request.')
