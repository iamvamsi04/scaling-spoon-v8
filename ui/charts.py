"""Render saved, query-backed charts with the existing Altair/Streamlit stack."""
import logging
import re
from datetime import datetime

import altair as alt
import streamlit as st

PALETTE = ['#2563eb', '#0d9488', '#f59e0b', '#8b5cf6', '#f43f5e', '#0891b2',
           '#65a30d', '#ea580c', '#6366f1', '#db2777']


def build_chart(chart):
    from mcp_server.charts import validate_saved_chart
    return _build_validated_chart(validate_saved_chart(chart))


def _build_validated_chart(chart):
    kind, x, ys = chart['type'], chart['x'], chart['y']
    axis = chart.get('x_type', 'numeric' if kind in ('scatter','bubble','histogram') else 'temporal' if kind=='timeline' else 'categorical')
    xtype = {'categorical':'O','numeric':'Q','temporal':'T'}[axis]
    currency = chart.get('currency')
    suffix = f' ({currency})' if currency else ''
    def field_title(field):
        title = field.replace('_', ' ').capitalize()
        counts = re.search(r'(?:^|[^a-z0-9])(?:counts?|qty|quantity|quantities|units|products|orders|invoices|customers|items|percent|percentage|ratio|share|rate)(?:[^a-z0-9]|$)|quantity$', field, re.I)
        return title + (suffix if not counts else '')

    measure = field_title(ys[0]) if len(ys)==1 else 'Products' if all(
        field.endswith('products') for field in ys) else 'Value' + (
        suffix if any(field_title(field).endswith(suffix) for field in ys) and suffix else '')
    rows = []
    for i, record in enumerate(chart['data']):
        row = {'x_value':record[x], 'position':i}
        if axis == 'temporal' and datetime.fromisoformat(row['x_value']).tzinfo is None:
            row['x_value'] += 'Z'
        row.update({f'v{j}':record[y] for j,y in enumerate(ys)})
        for source,target in [('group','category'),('size','bubble_size'),('end','end_value')]:
            if source in chart:
                row[target] = record[chart[source]]
                if source == 'end' and datetime.fromisoformat(row[target]).tzinfo is None:
                    row[target] += 'Z'
        rows.append(row)

    def base(data=None):
        return alt.Chart(alt.Data(values=rows if data is None else data))

    time_bar_width = max(2., min(42., 460. / max(1, len({row['x_value'] for row in rows}))))

    def color(field='series:N', title='Measure'):
        return alt.Color(field, title=title, scale=alt.Scale(range=PALETTE),
                         legend=alt.Legend(orient='bottom', labelLimit=180))

    def finish(result, height=340):
        return (result.properties(height=height)
                .configure_view(strokeWidth=0)
                .configure_axis(labelFontSize=12, titleFontSize=12, titlePadding=12,
                                gridColor='#e2e8f0', labelLimit=160)
                .configure_legend(labelFontSize=12, titleFontSize=12))

    order = alt.SortField(field='position', order='ascending')
    axis_options = {}
    time_format = '%Y-%m-%d %H:%M:%S.%L'
    if xtype == 'O':
        axis_options = {'sort': order, 'axis': alt.Axis(labelAngle=-35 if len(rows)>8 else 0,
                                                       labelOverlap='greedy')}
    elif xtype == 'T' and kind != 'timeline':
        dates = [datetime.fromisoformat(row['x_value'].replace('Z', '+00:00')) for row in rows]
        months = {day.year*12+day.month for day in dates}
        if all(day.day == 1 and not (day.hour or day.minute or day.second or day.microsecond) for day in dates):
            span = max(months)-min(months)
            yearly = len({day.month for day in dates}) == 1 and span >= 12
            time_format = '%Y' if yearly else '%b %Y'
            axis_options = {'axis': alt.Axis(format=time_format,
                tickCount={'interval':'year' if yearly else 'month', 'step':max(1, span//120 if yearly else (span+11)//12)},
                labelOverlap='greedy')}
        elif all(not (day.hour or day.minute or day.second or day.microsecond) for day in dates):
            time_format = '%d %b %Y'
            axis_options = {'axis':alt.Axis(format=time_format,labelOverlap='greedy')}
        else:
            axis_options = {'axis':alt.Axis(format=time_format,labelOverlap='greedy')}
    if xtype == 'T':
        axis_options['scale'] = alt.Scale(type='utc')
    xc = alt.X('x_value:'+xtype, title=field_title(x) if xtype=='Q' else x.replace('_',' ').capitalize(),
               **axis_options)
    multiple_series = len(ys)>1 or 'group' in chart
    discrete_bars = kind in ('bar','grouped_bar','horizontal_bar') and multiple_series and xtype != 'O'
    if discrete_bars:
        # Vega drops offsets on continuous axes, which silently overlays series.
        # Keep exact coordinates as distinct categories; format only their labels.
        discrete_axis = alt.Axis(labelAngle=-35 if len(rows)>8 else 0, labelOverlap='greedy',
            **({'labelExpr':f"utcFormat(toDate(datum.value), '{time_format}')"} if xtype=='T' else {}))
        xc = alt.X('x_value:O',title=x.replace('_',' ').capitalize(),sort=order,axis=discrete_axis)
    tip = [alt.Tooltip('x_value:'+xtype, title=x.replace('_',' ').capitalize())]
    tip += [alt.Tooltip(f'v{i}:Q', title=field_title(y), format=',.2f') for i,y in enumerate(ys)]
    if 'group' in chart:
        tip.append(alt.Tooltip('category:N',title=chart['group'].replace('_',' ').capitalize()))

    if kind == 'pie':
        c = base().mark_arc(outerRadius=145,
                            stroke='white',strokeWidth=2).encode(
            theta=alt.Theta('v0:Q'), color=color('x_value:N',x), order='position:O',tooltip=tip)
        return finish(c)
    if kind in ('scatter','bubble'):
        c = base().mark_circle(size=85, opacity=0.75, stroke='white',strokeWidth=0.6).encode(
            x=xc,y=alt.Y('v0:Q',title=measure,scale=alt.Scale(zero=False)),tooltip=tip)
        if 'group' in chart:
            c = c.encode(color=color('category:N',chart['group']))
        else:
            c = c.encode(color=alt.value(PALETTE[0]))
        if kind=='bubble':
            c = c.encode(size=alt.Size('bubble_size:Q',title=chart['size'].replace('_',' ').capitalize(),
                                      scale=alt.Scale(range=[30,1800])),
                         tooltip=tip+[alt.Tooltip('bubble_size:Q',title=chart['size'],format=',.2f')])
        return finish(c)
    if kind=='histogram':
        return finish(base().mark_bar(color=PALETTE[0],binSpacing=2).encode(
            x=alt.X('x_value:Q',bin=alt.Bin(maxbins=chart.get('bins',10)),title=field_title(x)),
            y=alt.Y('count():Q',title='Observations'),
            tooltip=[alt.Tooltip('x_value:Q',bin=True,title=x),alt.Tooltip('count():Q',title='Observations')]))
    if kind=='boxplot':
        c = base().mark_boxplot(extent=1.5,size=28).encode(x=xc,y=alt.Y('v0:Q',title=measure))
        if 'group' in chart:
            c = c.encode(color=color('category:N',chart['group']),xOffset='category:N')
        else:
            c = c.encode(color=alt.value(PALETTE[0]))
        return finish(c)
    if kind=='heatmap':
        return finish(base().mark_rect(stroke='white',strokeWidth=1).encode(
            x=xc,y=alt.Y('category:N',title=chart['group'].replace('_',' ').capitalize()),
            color=alt.Color('v0:Q',title=measure,scale=alt.Scale(scheme='blues')),tooltip=tip),
            max(300,min(720,len({r['category'] for r in rows})*30)))
    if kind=='range':
        c = base().mark_rule(strokeWidth=7,color=PALETTE[0]).encode(
            x=xc,y=alt.Y('v0:Q',title='Range'+suffix,scale=alt.Scale(zero=False)),y2='v1:Q',tooltip=tip)
        endpoints = base().mark_tick(color=PALETTE[0],size=18,thickness=2).encode(x=xc,y='v0:Q')
        return finish(c + endpoints + endpoints.encode(y='v1:Q'))
    if kind=='combo':
        bars = base().mark_bar(color=PALETTE[0],opacity=0.7,
                              **({'size':time_bar_width} if xtype=='T' else {})).encode(
            x=xc,y=alt.Y('v0:Q',title=field_title(ys[0]),
                        axis=alt.Axis(titleColor=PALETTE[0],labelColor=PALETTE[0])),tooltip=tip)
        line = base().mark_line(color=PALETTE[1],point=True,strokeWidth=3).encode(
            x=xc,y=alt.Y('v1:Q',title=field_title(ys[1]),axis=alt.Axis(orient='right',grid=False,
                titleColor=PALETTE[1],labelColor=PALETTE[1])),tooltip=tip)
        return finish(alt.layer(bars,line).resolve_scale(y='independent'))
    if kind=='timeline':
        tip.append(alt.Tooltip('end_value:T',title=chart['end'].replace('_',' ').capitalize()))
        intervals = base().mark_bar(color=PALETTE[0],cornerRadius=4,size=18).encode(
            x=alt.X('x_value:T',title='Date',scale=alt.Scale(type='utc')),x2='end_value:T',
            y=alt.Y('category:N',title=chart['group'].replace('_',' ').capitalize(),sort=order),tooltip=tip)
        starts = base().mark_circle(size=65,color=PALETTE[0]).encode(
            x=alt.X('x_value:T',title='Date',scale=alt.Scale(type='utc')),
            y=alt.Y('category:N',title=chart['group'].replace('_',' ').capitalize(),sort=order),tooltip=tip)
        return finish(intervals+starts,
            max(300,min(900,len({r['category'] for r in rows})*30)))
    series_labels = [y.replace('_',' ').capitalize() for y in ys]
    if len(set(series_labels)) != len(series_labels):
        series_labels = list(ys)
    long = [{'x_value':r['x_value'],'value':r[f'v{j}'],'series':r.get('category',series_labels[j]),'position':r['position']}
            for r in rows for j,y in enumerate(ys)]
    b = base(long)
    if kind in ('bar','grouped_bar','horizontal_bar','stacked_bar','normalized_bar'):
        groups = len(ys) if len(ys)>1 else len({row.get('category') for row in rows}) if 'group' in chart else 1
        c = b.mark_bar(cornerRadiusEnd=3, **({'size':max(2.,time_bar_width/max(1,groups))}
            if xtype=='T' else {}))
    elif kind == 'line':
        c = b.mark_line(point=True,strokeWidth=2.5)
    else:
        raise ValueError('Unsupported chart type.')
    stack = 'normalize' if kind.startswith('normalized') else 'zero' if kind.startswith('stacked') else None
    title = 'Share' if stack=='normalize' else measure
    series_title = chart.get('group','Measure')
    c = c.encode(color=color('series:N',series_title),tooltip=[alt.Tooltip('x_value:'+xtype,title=x),
        alt.Tooltip('series:N',title=series_title),alt.Tooltip('value:Q',title=measure,format=',.2f')])
    if len(ys) == 1 and 'group' not in chart:
        c = c.encode(color=alt.value(PALETTE[0]))
    value_axis = alt.Axis(format='%') if stack=='normalize' else alt.Axis()
    if kind=='horizontal_bar':
        c = c.encode(y=alt.Y('x_value:'+('O' if discrete_bars else xtype),title=x.replace('_',' ').capitalize(),sort=order,
                            **({'axis':discrete_axis} if discrete_bars else
                               {key:value for key,value in axis_options.items() if key!='sort'})),
                     x=alt.X('value:Q',title=title,stack=None,axis=value_axis))
        if len(ys)>1 or 'group' in chart:
            c = c.encode(yOffset='series:N')
        return finish(c,max(320,min(900,len({r['x_value'] for r in rows})*30)))
    c = c.encode(x=xc,y=alt.Y('value:Q',title=title,stack=stack,axis=value_axis))
    if kind in ('bar','grouped_bar') and (len(ys)>1 or 'group' in chart):
        c = c.encode(xOffset='series:N')
    return finish(c)


def render_chart(chart):
    try:
        from mcp_server.charts import validate_saved_chart
        chart = validate_saved_chart(chart)
        result=_build_validated_chart(chart)
        st.subheader(chart['title'])
        st.altair_chart(result,use_container_width=True)
        st.caption('Based on the saved query result' + (f" ({chart['currency']})" if chart.get('currency') else '') + '. Refresh the question for current figures.')
        with st.expander('View chart data'):
            st.dataframe(chart['data'],hide_index=True,use_container_width=True)
    except Exception:
        logging.getLogger(__name__).exception('Unable to render saved chart')
        st.warning('This saved chart cannot be displayed. Ask the agent to generate it again with a supported type and valid data.')
        return False
    return True
