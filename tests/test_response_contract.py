"""Structured result validation; request wording never selects reports or tools."""
from copy import deepcopy
import pytest

from agent import response_contract as module
from agent.response_contract import (
    INVENTORY_QUERY, ResponseContract, evidence_for, evidence_satisfied,
    inventory_rows, report_reply, result_contract,
)


@pytest.fixture(autouse=True)
def isolated_db():
    # No database setup: all inputs are executed-result envelopes.
    yield


def inventory(rows=None, query=INVENTORY_QUERY):
    return ({'query': query, 'parameters': []}, {
        'ok': True, 'columns': ['category','products','in_stock_products'],
        'rows': [['Keyboards', 3, 2], ['Mice', 1, 1]] if rows is None else rows,
        'truncated': False,
    })


def overview(mode='overview', **scope):
    filters = dict.fromkeys(module.FILTER_KEYS)
    filters.update(scope)
    arguments = {'report': 'store_overview', 'chart_mode': mode, **scope}
    data = {'ok': True, 'report': 'store_overview', 'chart_mode': mode,
            'filters': filters, 'coverage': {'complete': True},
            'summary': {'matched_orders': 4, 'invoice_count': 2,
                        'draft_count': 1, 'cancelled_count': 1}}
    return arguments, data


class UninspectableQuestion:
    def __str__(self):
        raise AssertionError('Application must not inspect question words for routing')


def test_legacy_request_helpers_never_classify_or_inspect_question_words():
    question = UninspectableQuestion()
    assert module.request_contract(question, 'admin') == ResponseContract()
    assert module.chart_intent(question) is None
    assert module.table_intent(question) is False
    assert module.general_explanation(question) is True
    assert ResponseContract('store_overview').prompt() == ''


def test_complete_inventory_result_is_recognized_without_any_user_prompt():
    arguments, data = inventory()
    contract = result_contract('execute_sql_query', arguments, data)
    assert contract.kind == 'inventory_overview' and contract.report
    assert contract.domains == frozenset({'products'})
    assert evidence_for(contract, 'execute_sql_query', arguments, data)
    assert inventory_rows(arguments, data) == [['Keyboards',3,2], ['Mice',1,1]]
    assert report_reply(contract, ('execute_sql_query', arguments, data)).startswith(
        'The store has 4 products across 2 categories. 3 products are currently in stock.')


def test_complete_empty_inventory_is_a_verified_empty_report():
    arguments, data = inventory([])
    contract = result_contract('execute_sql_query', arguments, data)
    assert contract.report and inventory_rows(arguments, data) == []
    assert 'no products' in report_reply(contract, ('execute_sql_query', arguments, data))


def test_inventory_summary_preserves_singular_grammar_and_sorted_rows():
    arguments, data = inventory([['chairs',1,1]])
    contract = result_contract('execute_sql_query', arguments, data)
    assert report_reply(contract, ('execute_sql_query', arguments, data)).startswith(
        'The store has 1 product across 1 category. 1 product is currently in stock.')
    arguments, data = inventory([['Mice',1,1],['Keyboards',3,2]])
    assert inventory_rows(arguments,data)[0][0] == 'Keyboards'


@pytest.mark.parametrize('query', [
    INVENTORY_QUERY + ' LIMIT 5',
    INVENTORY_QUERY.replace('LEFT JOIN','INNER JOIN'),
    INVENTORY_QUERY.replace(' GROUP BY', ' WHERE i.availablequantity > 0 GROUP BY'),
    'SELECT category, 400 AS products, 400 AS in_stock_products FROM product_details',
    'SELECT * FROM items',
])
def test_scoped_or_unproven_inventory_results_never_become_complete_overviews(query):
    arguments, data = inventory(query=query)
    assert inventory_rows(arguments,data) is None
    assert not result_contract('execute_sql_query',arguments,data).report


@pytest.mark.parametrize('field,value', [
    ('ok',False), ('truncated',True), ('columns',['category','products']),
    ('rows',[['Keyboards',True,1]]), ('rows',[['Keyboards',2,3]]),
    ('rows',[['Keyboards',-1,0]]), ('rows',[['',1,1]]),
    ('rows',[['Keyboards',1,1],['Keyboards',2,2]]),
    ('rows',[['Keyboards',2,1,'extra']]), ('rows',None),
])
def test_invalid_inventory_shape_is_not_locally_summarized(field,value):
    arguments, data = inventory()
    data[field] = value
    assert inventory_rows(arguments,data) is None
    assert not result_contract('execute_sql_query',arguments,data).report


@pytest.mark.parametrize('mode',['overview','dashboard','all','none'])
def test_store_report_preserves_executed_presentation_without_inventing_a_mode(mode):
    arguments, data = overview(mode)
    contract = result_contract('billing_analysis',arguments,data)
    assert contract.kind == 'store_overview' and contract.chart_mode == mode
    assert contract.expected_filters == dict.fromkeys(module.FILTER_KEYS)
    assert evidence_satisfied(contract,[('billing_analysis',arguments,data)])
    assert contract.prompt() == ''


def test_store_report_preserves_exact_executed_scope_and_does_not_mutate_inputs():
    arguments, data = overview('all',customer_id='CUST001',order_id='ORD-1',
                               date_from='2026-01-01',date_to='2026-03-31')
    before = deepcopy((arguments,data))
    contract = result_contract('billing_analysis',arguments,data)
    assert contract.scoped and contract.expected_filters == data['filters']
    assert (arguments,data) == before
    assert contract.filter_items == (('customer_id','CUST001'),('order_id','ORD-1'),
                                    ('date_from','2026-01-01'),('date_to','2026-03-31'))


@pytest.mark.parametrize('name,field,value', [
    ('different_tool','report','store_overview'),
    ('billing_analysis','report','pending_refunds'),
    ('billing_analysis','ok',False),
    ('billing_analysis','coverage',{'complete':False}),
    ('billing_analysis','coverage',{'complete':1}),
    ('billing_analysis','coverage',None),
    ('billing_analysis','chart_mode','all'),
    ('billing_analysis','chart_mode','unknown'),
    ('billing_analysis','filters',{}),
    ('billing_analysis','filters',dict.fromkeys((*module.FILTER_KEYS,'category'))),
    ('billing_analysis','summary',{}),
])
def test_unproven_or_changed_report_envelopes_are_not_locally_summarized(name,field,value):
    arguments, data = overview()
    data[field] = value
    assert not result_contract(name,arguments,data).report


@pytest.mark.parametrize('scope', [
    {'customer_id':'CUST001'}, {'order_id':'ORD-1'},
    {'date_from':'2026-01-01'}, {'date_to':'2026-03-31'},
])
def test_report_result_cannot_silently_add_a_scope_not_in_the_executed_arguments(scope):
    arguments, data = overview()
    data['filters'].update(scope)
    assert not result_contract('billing_analysis',arguments,data).report


@pytest.mark.parametrize('scope', [
    {'customer_id':''}, {'customer_id':'bad customer'}, {'customer_id':2},
    {'order_id':'ORD 1'}, {'date_from':'2026-02-30'}, {'date_from':'2026-1-1'},
    {'date_from':'2026-03-31','date_to':'2026-01-01'},
])
def test_invalid_report_scope_is_not_a_trusted_local_summary(scope):
    arguments, data = overview(**scope)
    assert not result_contract('billing_analysis',arguments,data).report


@pytest.mark.parametrize('key,value', [('matched_orders',True),('invoice_count',-1),
                                      ('draft_count','1'),('cancelled_count',None)])
def test_invalid_summary_counts_never_reach_the_local_report_formatter(key,value):
    arguments, data = overview()
    data['summary'][key] = value
    assert not result_contract('billing_analysis',arguments,data).report


@pytest.mark.parametrize('arguments,data', [(None,{}), ({},None), ([],{}), ({},[]), ({},{'ok':False})])
def test_result_contract_rejects_invalid_envelopes(arguments,data):
    assert result_contract('billing_analysis',arguments,data) == ResponseContract()


def test_unrelated_successful_reads_remain_normal_results_without_forcing_a_report():
    for name, arguments, data in [
        ('execute_sql_query',{'query':'SELECT * FROM items'},{'ok':True,'columns':['item_code'],'rows':[]}),
        ('billing_analysis',{'report':'pending_refunds'},{'ok':True,'report':'pending_refunds','requests':[]}),
        ('billing_anomalies',{}, {'ok':True,'findings':[]}),
    ]:
        assert result_contract(name,arguments,data) == ResponseContract()


def test_evidence_still_uses_executed_business_sources_not_literals_or_schema_reads():
    contract = ResponseContract('data',domains=frozenset({'refund_requests'}))
    assert not evidence_for(contract,'execute_sql_query',
        {'query':"SELECT 'FROM refund_requests' AS note FROM items -- JOIN refund_requests"},{'ok':True})
    assert not evidence_for(contract,'execute_sql_query',{'query':'PRAGMA table_info(refund_requests)'},{'ok':True})
    real = ('execute_sql_query',{'query':'SELECT order_id FROM refund_requests'},{'ok':True})
    assert evidence_satisfied(contract,[real])
    assert not evidence_satisfied(contract,[('execute_sql_query',{'query':'SELECT order_id FROM orders'},{'ok':True})])


def test_combined_evidence_can_cover_several_structured_domains_without_a_dashboard():
    contract = ResponseContract('data',domains=frozenset({'finance','customers'}))
    totals = ('billing_analysis',{}, {'ok':True,'totals':{'charged_price':'100.00'}})
    customer_rows = ('execute_sql_query',{'query':'SELECT customer_id,name FROM customers'}, {'ok':True})
    assert not evidence_satisfied(contract,[totals])
    assert evidence_satisfied(contract,[totals,customer_rows])


def test_report_reply_claims_only_recorded_chart_attachments_and_explains_omissions():
    arguments, data = overview('all')
    data['chart_delivery'] = {'attached_this_call':2,'errors':[]}
    data['coverage']['chart_omissions'] = [{'type':'timeline','reason':'No shipment dates'}]
    contract = result_contract('billing_analysis',arguments,data)
    reply = report_reply(contract,('billing_analysis',arguments,data))
    assert '2 charts are attached.' in reply and 'No shipment dates' in reply
    data['chart_delivery']['attached_this_call'] = 0
    reply = report_reply(contract,('billing_analysis',arguments,data))
    assert 'No charts could be generated' in reply and '2 charts are attached' not in reply
