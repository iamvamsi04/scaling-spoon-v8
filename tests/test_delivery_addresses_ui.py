"""Exercise the address widgets and requests without a model or external service."""
from copy import deepcopy
from urllib.parse import urlsplit

import httpx
from streamlit.testing.v1 import AppTest

from config import settings


def widget(app, kind, label):
    return next(entry for entry in getattr(app, kind) if entry.label == label)


def signed_in_app(monkeypatch):
    saved = []
    orders = [{'order_id': 'ADDRESS-ORDER', 'charged_price': 100, 'currency': 'USD',
               'address_id': None, 'delivery_address': None}]
    calls = []

    def respond(self, method, url, **kwargs):
        path = urlsplit(str(url)).path
        calls.append((method, path, deepcopy(kwargs.get('json'))))
        if path == '/health':
            result = {'model_configured': True}
        elif path == '/customers/session':
            result = {'customer_id': 'CUST001', 'expires_in': 3600}
        elif path == '/customers/checkout':
            assert kwargs['headers']['X-Customer-Token'] == 'address-session-token'
            result = {'addresses': deepcopy(saved), 'orders': deepcopy(orders)}
        elif path == '/customers/addresses' and method == 'POST':
            address = {**kwargs['json'], 'address_id': 'ADDR-ONE', 'customer_id': 'CUST001',
                       'active': True, 'is_default': True}
            saved.append(address)
            result = {'address': deepcopy(address)}
        elif path == '/customers/addresses/ADDR-ONE' and method == 'PUT':
            saved[0].update(kwargs['json'])
            result = {'address': deepcopy(saved[0])}
        elif path == '/customers/addresses/ADDR-ONE' and method == 'DELETE':
            saved.clear()
            orders[0].update(address_id=None, delivery_address=None)
            result = {'address_id': 'ADDR-ONE', 'deleted': True}
        elif path == '/customers/orders/ADDRESS-ORDER/delivery-address':
            assert kwargs['json'] == {'address_id': 'ADDR-ONE'}
            fields = ('full_name', 'phone', 'address_line1', 'address_line2', 'city',
                      'state', 'postal_code', 'country', 'landmark')
            orders[0].update(address_id='ADDR-ONE', delivery_address={key: saved[0][key] for key in fields})
            result = {'order_id': 'ADDRESS-ORDER', **deepcopy(orders[0])}
        elif path == '/customers/logout':
            result = {'signed_out': True}
        else:
            raise AssertionError((method, path))
        return httpx.Response(200, json=result, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, 'request', respond)
    app = AppTest.from_file(str(settings.ROOT / 'ui/streamlit_app.py'), default_timeout=30)
    app.session_state['customer_token'] = 'address-session-token'
    app.session_state['customer_id'] = 'CUST001'
    app.run()
    return app, saved, orders, calls


def test_delivery_address_book_is_lazy_and_required_fields_are_marked(monkeypatch):
    app, _, _, calls = signed_in_app(monkeypatch)
    assert not app.exception
    assert not any(path == '/customers/checkout' for _, path, _ in calls)
    widget(app, 'button', 'Manage and select delivery address').click().run()
    assert not app.exception
    assert {entry.label for entry in app.text_input if entry.label.endswith('*')} == {
        'Full name *', 'Phone number *', 'Address line 1 *', 'City *',
        'State / province / region *', 'Postal code *', 'Country code *'}
    assert all(not widget(app, 'text_input', name).label.endswith('*')
               for name in ('Address line 2', 'Address label', 'Landmark'))
    assert widget(app, 'button', 'Save address')


def test_delivery_address_create_select_edit_delete_and_sign_out(monkeypatch):
    app, saved, orders, calls = signed_in_app(monkeypatch)
    widget(app, 'button', 'Manage and select delivery address').click().run()
    values = {'Full name *': 'Alex Customer', 'Phone number *': '+1 202 555 0142',
              'Address line 1 *': '12 River Road', 'City *': 'Austin',
              'State / province / region *': 'Texas', 'Postal code *': '78701',
              'Country code *': 'US', 'Address label': 'Home'}
    for label, value in values.items():
        widget(app, 'text_input', label).set_value(value)
    widget(app, 'button', 'Save address').click().run()
    assert not app.exception and len(saved) == 1
    assert saved[0]['full_name'] == 'Alex Customer'
    assert orders[0]['address_id'] is None  # Saving/default never chooses for a draft.
    choice = widget(app, 'selectbox', 'Deliver this order to')
    assert choice.value is None
    choice.set_value('ADDR-ONE').run()
    widget(app, 'button', 'Use this delivery address').click().run()
    assert not app.exception
    assert orders[0]['delivery_address']['address_line1'] == '12 River Road'
    assert any(entry.value == 'Selected for this order' for entry in app.caption)
    widget(app, 'selectbox', 'Address book').set_value('ADDR-ONE').run()
    widget(app, 'text_input', 'Address line 1 *').set_value('14 River Road')
    widget(app, 'button', 'Save address').click().run()
    assert not app.exception and saved[0]['address_line1'] == '14 River Road'
    widget(app, 'checkbox', 'Confirm deleting this saved address').check().run()
    widget(app, 'button', 'Delete address').click().run()
    assert not app.exception and saved == []
    assert widget(app, 'selectbox', 'Address book').value == ''
    widget(app, 'button', 'Sign out').click().run()
    assert not app.exception
    assert app.session_state['customer_token'] == ''
    assert not any(key.startswith(('_address_', '_delivery_'))
                   for key in app.session_state._state.filtered_state)
    assert not any('place_order' in path for _, path, _ in calls)
