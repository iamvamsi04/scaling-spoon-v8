from uuid import uuid4
from fastapi.testclient import TestClient
from api.main import app
from config import settings
import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, APIStatusError


class Engine:
    def __init__(self):
        self.calls = 0
        self.histories = []
    async def chat(self, message, history, customer_id, **kwargs):
        self.calls += 1
        self.histories.append(history)
        return {"response": f"Hello {customer_id}", "invoices": []}


def test_api_manages_engine_lifecycle():
    events = []
    class ManagedEngine(Engine):
        async def start(self):
            events.append("start")
        async def close(self):
            events.append("close")
    app.state.engine = ManagedEngine()
    with TestClient(app):
        assert events == ["start"]
    assert events == ["start", "close"]


def test_chat_history_and_idempotency():
    engine = Engine()
    app.state.engine = engine
    with TestClient(app) as client:
        from api.customer_auth import set_password
        password = 'Customer test password 2026'
        set_password('CUST001', password)
        login = client.post('/customers/login', json={'customer_id': 'CUST001', 'password': password})
        assert login.status_code == 200
        client.headers['X-Customer-Token'] = login.json()['token']
        cid = client.post("/conversations", json={"customer_id": "CUST001"}).json()["conversation_id"]
        body = {"conversation_id": cid, "request_id": str(uuid4()), "message": "Hi"}
        first = client.post("/chat", json=body)
        assert first.status_code == 200
        assert client.post("/chat", json=body).json() == first.json()
        assert engine.calls == 1
        assert client.post("/chat", json={**body, "message": "Different"}).status_code == 409
        client.post("/chat", json={**body, "request_id": str(uuid4()), "message": "Follow up"})
        assert len(engine.histories[1]) == 2
        assert len(client.get(f"/conversations/{cid}").json()["messages"]) == 4
        assert client.post("/chat", json={**body, "conversation_id": str(uuid4())}).status_code == 404


def test_api_key(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "test-secret")
    app.state.engine = Engine()
    with TestClient(app) as client:
        assert client.post("/conversations", json={}).status_code == 401
        assert client.post("/conversations", json={}, headers={"X-API-Key": "test-secret"}).status_code == 200


def test_failed_turn_cannot_replay():
    class Failing:
        async def chat(self, *args, **kwargs):
            raise RuntimeError("unavailable")
    app.state.engine = Failing()
    with TestClient(app) as client:
        cid = client.post("/conversations", json={}).json()["conversation_id"]
        body = {"conversation_id": cid, "request_id": str(uuid4()), "message": "Place order"}
        assert client.post("/chat", json=body).status_code == 503
        assert client.post("/chat", json=body).status_code == 409


@pytest.mark.parametrize('kind,code', [
    ('connection', 'azure_connection_failed'), ('timeout', 'request_timeout'),
    ('deadline', 'request_timeout'), ('auth', 'azure_http_error'),
    ('internal', 'agent_failed')])
def test_failure_classification_and_no_secret_leak(kind, code):
    request = httpx.Request('POST', 'https://example.invalid')
    errors = {
        'connection': APIConnectionError(message='private-secret', request=request),
        'timeout': APITimeoutError(request=request),
        'deadline': TimeoutError('private-secret'),
        'auth': APIStatusError('private-secret', response=httpx.Response(401, request=request), body=None),
        'internal': RuntimeError('private-secret'),
    }
    class Failing:
        async def chat(self, *args, **kwargs):
            raise errors[kind]
    app.state.engine = Failing()
    with TestClient(app) as client:
        cid = client.post('/conversations', json={}).json()['conversation_id']
        body = {'conversation_id': cid, 'request_id': str(uuid4()), 'message': 'show me some projectors'}
        response = client.post('/chat', json=body)
        assert response.status_code == 503
        assert response.json()['detail']['code'] == code
        assert 'private-secret' not in response.text
        assert client.post('/chat', json=body).status_code == 409
