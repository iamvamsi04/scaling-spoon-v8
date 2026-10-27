import json
import httpx
import pytest
from context.reply_text import plain_reply, error_reply, FALLBACK
from context.shopping_state import ShoppingTracker


def test_formatting_preserves_billing_facts():
    result = plain_reply('## Refund ✅\n**Invoice INV-FAKE-001**\n- Credit: USD -12.50\n- Discount: 10%\n`ITEM_001` belongs to José.')
    assert result == 'Refund\nInvoice INV-FAKE-001\nCredit: USD -12.50\nDiscount: 10 percent\nITEM_001 belongs to José.'
    assert plain_reply(result) == result


@pytest.mark.parametrize('payload', [{'ok':True, 'customer_id':'SECRET'}, [{'amount':20}], '{"shopping_update":'])
def test_internal_payloads_are_not_visible(payload):
    assert plain_reply(payload if isinstance(payload,str) else json.dumps(payload)) == FALLBACK


def test_table_preserves_column_meaning():
    assert plain_reply('Choices\n| Product | Price |\n|---|---|\n| X | USD 10 |\n| Y | USD 20 |\nWhich one?') == 'Choices\nProduct: X; Price: USD 10.\nProduct: Y; Price: USD 20.\nWhich one?'


def test_wrapped_reply_and_confirmation_stay_consistent():
    tracker = ShoppingTracker(None, 'CUST001')
    reply = tracker.finish(json.dumps({'reply':'Update **José** in your contact details? ✅',
        'shopping_update':{'pending_confirmation':{'action':'update_customer', 'order_id':None,
            'summary':'Update **José** in your contact details?'},'next_question':'Update **José** in your contact details?'}}))
    assert reply == 'Update José in your contact details?'
    assert tracker.state['pending_confirmation']['summary'] == reply
    assert tracker.state['next_question'] == reply


def test_account_registration_cannot_be_proposed_in_chat():
    tracker = ShoppingTracker(None, None)
    tracker.finish(json.dumps({'reply':'Create a customer account?', 'shopping_update':{
        'pending_confirmation':{'action':'create_customer','summary':'Create a customer account?'}}}))
    assert tracker.state['pending_confirmation'] is None


def test_http_error_uses_message_only():
    response = httpx.Response(503,json={'detail':{'message':'Please check the order.', 'request_id':'internal'}})
    assert error_reply(response) == 'Please check the order.'
    assert 'loc' not in error_reply(httpx.Response(422,json={'detail':[{'loc':['body']}]}))


def test_comparisons_and_units_are_not_removed_as_markup():
    assert plain_reply("Stock < 5 and price > USD 100.") == "Stock < 5 and price > USD 100."
    assert plain_reply("<strong>ITEM-01</strong>") == "ITEM-01"


@pytest.mark.parametrize('wrapper', [
    'Your quote is USD 20.\n{envelope}',
    'Your quote is USD 20.\n```json\n{envelope}\n```',
    '{envelope}\nYour quote is USD 20.',
    'Answer: {envelope} Extra explanation.',
])
def test_mixed_reply_is_displayed_once(wrapper):
    content = wrapper.format(envelope=json.dumps({'reply':'Your quote is USD 20.',
                                                  'shopping_update':{'needs':['office work']}}))
    assert plain_reply(content) == 'Your quote is USD 20.'
    tracker = ShoppingTracker(None,None)
    assert tracker.finish(content) == 'Your quote is USD 20.'
    assert tracker.state['needs'] == ['office work']


def test_braces_quotes_and_truncated_envelope():
    reply = 'The product is named "Case {Blue}".'
    assert plain_reply(reply+'\n'+json.dumps({'reply':reply})) == reply
    assert plain_reply('Your quote is USD 20.\n```json\n{"reply":"Your quote') == 'Your quote is USD 20.'
    assert plain_reply('Your quote is USD 20.\n{"shopping_update":') == 'Your quote is USD 20.'
    assert plain_reply('The product is named {Blue}.') == 'The product is named {Blue}.'


def test_invalid_reply_field_does_not_crash_cleanup():
    assert plain_reply('{"reply":null,"response":"Hidden fallback"}') == FALLBACK
