"""Full report downloads, kept separate from concise chat and display tables."""

import streamlit as st
from context.reports import csv_bytes, sanitize_reports


def render_reports(reports, key):
    for report in sanitize_reports(reports):
        with st.expander('Download report data'):
            st.text(report['title'])
            coverage = report.get('coverage')
            if isinstance(coverage, str):
                st.caption(coverage)
            elif isinstance(coverage, dict):
                first, last = coverage.get('matched_order_date_from'), coverage.get('matched_order_date_to')
                if first and last:
                    st.caption(f'Matching order dates: {first} to {last}.')
                if coverage.get('complete') is True and coverage.get('truncated') is False:
                    st.caption('All matching report records are included in the downloads.')
            for note in report.get('notes', []):
                st.caption(note)
            for dataset in report['datasets']:
                st.text(dataset['title'])
                if dataset.get('scope'):
                    st.caption(dataset['scope'])
                if dataset.get('grain'):
                    st.caption(dataset['grain'])
                label = f"{dataset['row_count']} rows"
                if dataset['truncated']:
                    label += f" of {dataset['total_rows']}; incomplete dataset"
                st.caption(label)
                st.download_button('Download ' + dataset['title'], csv_bytes(dataset),
                    file_name=dataset['name'] + '.csv', mime='text/csv',
                    key=f"report-{key}-{dataset['name']}")
