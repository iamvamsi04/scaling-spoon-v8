from uuid import uuid4
import httpx
from streamlit.testing.v1 import AppTest
from config import settings


def test_streamlit_chat_flow(monkeypatch):
    cid = str(uuid4())
    def respond(self, method, url, **kwargs):
        if url.endswith("/health"):
            data = {"model_configured": True, "model": "test-model"}
        elif url.endswith("/conversations"):
            data = {"conversation_id": cid}
        elif url.endswith("/chat"):
            assert kwargs["json"]["conversation_id"] == cid
            data = {"response": 'There are 4 customers.\n{"reply":"**There are 4 customers.**","shopping_update":{}}', "invoices": []}
        else:
            raise AssertionError(url)
        return httpx.Response(200, json=data, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.Client, "request", respond)
    app = AppTest.from_file(str(settings.ROOT / "ui/streamlit_app.py"), default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "💳 Billing Agent"
    app.chat_input[0].set_value("How many customers?").run()
    assert not app.exception
    assert sum(m.value == "There are 4 customers." for m in app.text) == 1
    assert not any('"reply"' in m.value or 'shopping_update' in m.value for m in app.text)
    app.run()  # Saved message rendering after a rerun must stay clean too.
    assert sum(m.value == "There are 4 customers." for m in app.text) == 1


def test_admin_login_and_logout_clear_session(monkeypatch):
    key = 'test-admin-key-123456789'
    def respond(self, method, url, **kwargs):
        if url.endswith('/admin/access'):
            assert kwargs['headers']['X-Admin-Key'] == key
            data = {'authorized':True}
        elif url.endswith('/health'):
            data = {'model_configured':True}
        elif url.endswith('/conversations'):
            assert kwargs['json']['mode'] == 'admin'
            assert kwargs['headers']['X-Admin-Key'] == key
            data = {'conversation_id':str(uuid4())}
        elif url.endswith('/chat'):
            assert kwargs['headers']['X-Admin-Key'] == key
            data = {'response':'**Store revenue report**','invoices':[], 'charts':[
                {'type':kind,'title':'Revenue '+kind,'x':'month','y':['revenue'],'currency':'USD',
                 'data':[{'month':1,'revenue':10},{'month':2,'revenue':20}]}
                for kind in ('bar','line','horizontal_bar','pie','scatter')]}
        else:
            raise AssertionError(url)
        return httpx.Response(200,json=data,request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.Client,'request',respond)
    app = AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'),default_timeout=30).run()
    app.radio[0].set_value('Admin').run()
    assert not app.chat_input
    next(x for x in app.text_input if x.label == 'Admin key').set_value(key)
    next(x for x in app.button if x.label == 'Unlock admin mode').click().run()
    assert not app.exception
    app.chat_input[0].set_value('Show revenue').run()
    assert any(m.value == 'Store revenue report' for m in app.text)
    assert not app.exception
    assert len(app.get('vega_lite_chart')) == 5
    next(x for x in app.button if x.label == 'Log out of admin mode').click().run()
    assert not app.exception
    assert app.radio[0].value == 'Customer'
    assert app.session_state['admin_key'] == ''
    assert app.session_state['messages'] == []
    assert app.session_state['conversation_id'] is None


def test_failed_chat_shows_plain_error_and_retries_same_request(monkeypatch):
    cid=str(uuid4())
    submitted=[]
    def respond(self,method,url,**kwargs):
        status=200
        if url.endswith('/health'):
            data={'model_configured':True}
        elif url.endswith('/conversations'):
            data={'conversation_id':cid}
        elif url.endswith('/chat'):
            submitted.append(dict(kwargs['json']))
            if len(submitted)==1:
                status=503
                data={'detail':{'message':'Please check the order before retrying.','request_id':'internal'}}
            else:
                data={'response':'Your saved response.','invoices':[]}
        else:
            raise AssertionError(url)
        return httpx.Response(status,json=data,request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.Client,'request',respond)
    app=AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'),default_timeout=30).run()
    app.chat_input[0].set_value('Place the confirmed order.').run()
    assert not app.exception
    assert any('Please check the order before retrying.' in x.value for x in app.error)
    assert not any('request_id' in x.value or '{' in x.value for x in app.error)
    assert app.session_state['pending'] is not None
    next(x for x in app.button if x.label=='Retry same request').click().run()
    assert not app.exception
    assert submitted[0]==submitted[1]
    assert app.session_state['pending'] is None
    assert any(x.value=='Your saved response.' for x in app.text)


def test_unreachable_api_does_not_break_ui(monkeypatch):
    def unavailable(self,method,url,**kwargs):
        raise httpx.ConnectError('Connection unavailable',request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.Client,'request',unavailable)
    app=AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'),default_timeout=30).run()
    assert not app.exception and app.warning
    app.chat_input[0].set_value('Hello').run()
    assert not app.exception and app.error
    assert app.session_state['pending'] is None


def test_health_check_has_short_timeout_and_recovers_after_failure_backoff(monkeypatch):
    import time
    actual_monotonic=time.monotonic
    elapsed=[0.0]
    monkeypatch.setattr(time,'monotonic',lambda:actual_monotonic()+elapsed[0])
    checked=[]
    def respond(self,method,url,**kwargs):
        assert url.endswith('/health')
        assert kwargs['timeout']==2.0
        checked.append(url)
        if len(checked)==1:
            raise httpx.ReadTimeout('Health check unavailable',request=httpx.Request(method,url))
        return httpx.Response(200,json={'model_configured':True},request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.Client,'request',respond)
    app=AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'),default_timeout=30).run()
    assert not app.exception and app.warning
    app.run()
    assert not app.exception and len(checked)==1
    elapsed[0]+=3.1
    app.run()
    assert not app.exception and len(checked)==2
    assert any(x.value=='Azure OpenAI configured' for x in app.success)
    app.run()
    assert len(checked)==2


def test_customer_sign_in_protects_requests_and_sign_out_clears_context(monkeypatch):
    password='Example-customer-password'
    token='a'*43
    sent=[]
    def respond(self,method,url,**kwargs):
        sent.append((url,kwargs))
        if url.endswith('/health'):
            data={'model_configured':True}
        elif url.endswith('/customers/login'):
            assert kwargs['json']=={'customer_id':'CUST001','password':password}
            data={'customer_id':'CUST001','token':token}
        elif url.endswith('/customers/logout'):
            assert kwargs['headers']['X-Customer-Token']==token
            assert kwargs['timeout']==3.0
            data={'signed_out':True}
        elif url.endswith('/conversations'):
            assert kwargs['json']['customer_id']=='CUST001'
            assert kwargs['headers']['X-Customer-Token']==token
            data={'conversation_id':str(uuid4())}
        elif url.endswith('/chat'):
            assert kwargs['headers']['X-Customer-Token']==token
            assert password not in repr(kwargs['json'])
            data={'response':'Your order is available.','invoices':[]}
        else:
            raise AssertionError(url)
        return httpx.Response(200,json=data,request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.Client,'request',respond)
    app=AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'),default_timeout=30).run()
    next(x for x in app.text_input if x.label=='Customer ID').set_value('CUST001')
    next(x for x in app.text_input if x.label=='Password').set_value(password)
    next(x for x in app.button if x.label=='Sign in').click().run()
    assert not app.exception
    assert app.session_state['customer_token']==token
    assert app.session_state['customer_password']==''
    app.chat_input[0].set_value('Show my order').run()
    assert not app.exception and app.session_state['messages']
    next(x for x in app.button if x.label=='Sign out').click().run()
    assert not app.exception
    assert app.session_state['customer_token']=='' and app.session_state['customer_id']==''
    assert app.session_state['conversation_id'] is None and app.session_state['messages']==[]
    assert app.session_state['customer_login_id']==''
    assert any(url.endswith('/customers/logout') for url,_ in sent)


def test_customer_registration_uses_form_and_never_sends_password_to_chat(monkeypatch):
    sent=[]
    def respond(self,method,url,**kwargs):
        sent.append((url,kwargs))
        if url.endswith('/health'):
            data={'model_configured':True}
        elif url.endswith('/customers/register'):
            assert kwargs['json']['password']=='New-account-password'
            assert kwargs['json']['name']=='New Shopper'
            data={'customer_id':'NEW-CUSTOMER','token':'b'*43}
        else:
            raise AssertionError(url)
        return httpx.Response(200,json=data,request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.Client,'request',respond)
    app=AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'),default_timeout=30).run()
    app.selectbox[0].set_value('Create account').run()
    for label,value in [('Customer ID','NEW-CUSTOMER'),('Name','New Shopper'),('Password','New-account-password'),('Repeat password','Different-password')]:
        next(x for x in app.text_input if x.label==label).set_value(value)
    next(x for x in app.button if x.label=='Create account').click().run()
    assert not app.exception and app.error
    assert not any(url.endswith('/customers/register') for url,_ in sent)
    for label in ['Password','Repeat password']:
        next(x for x in app.text_input if x.label==label).set_value('New-account-password')
    next(x for x in app.button if x.label=='Create account').click().run()
    assert not app.exception and app.session_state['customer_id']=='NEW-CUSTOMER'
    assert app.session_state['customer_password']=='' and app.session_state['customer_password_repeat']==''
    assert not app.session_state['messages']


def test_expired_customer_session_clears_private_screen_and_shows_sign_in(monkeypatch):
    def respond(self,method,url,**kwargs):
        status=200
        if url.endswith('/health'):
            data={'model_configured':True}
        elif url.endswith('/customers/login'):
            data={'customer_id':'CUST001','token':'c'*43}
        elif url.endswith('/conversations'):
            data={'conversation_id':str(uuid4())}
        elif url.endswith('/chat'):
            status=401
            data={'detail':'Sign in to the matching customer account.'}
        else:
            raise AssertionError(url)
        return httpx.Response(status,json=data,request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.Client,'request',respond)
    app=AppTest.from_file(str(settings.ROOT/'ui/streamlit_app.py'),default_timeout=30).run()
    next(x for x in app.text_input if x.label=='Customer ID').set_value('CUST001')
    next(x for x in app.text_input if x.label=='Password').set_value('Customer-example-password')
    next(x for x in app.button if x.label=='Sign in').click().run()
    app.chat_input[0].set_value('Show my private orders').run()
    assert not app.exception
    assert app.session_state['customer_token']=='' and app.session_state['customer_id']==''
    assert app.session_state['conversation_id'] is None and app.session_state['pending'] is None
    assert app.session_state['messages']==[] and app.session_state['customer_login_id']==''
    assert any(x.label=='Sign in' for x in app.button)
