from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import dispatcher
from backend.app import app, get_repos
from backend.config import settings
from backend.db.repositories import Repositories
from backend.supervisor.state import CALL_STATES
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


def _mock_response(status_code: int, json_body: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_body,
        request=httpx.Request("POST", "https://api.openai.com/v1/realtime/client_secrets"),
    )


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(settings, "jupus_access_token", "s3cret")
    yield "s3cret"


def _override_repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


def _clear_dispatcher_state():
    CALL_STATES.clear()
    dispatcher.LOCKS.clear()
    dispatcher.SPEAKING.clear()
    dispatcher.DEFERRED.clear()
    dispatcher.CONNECTIONS.clear()


def _session_client():
    mocked = _mock_response(
        200,
        {"value": "ek_test123", "expires_at": 1234567890, "session": {"id": "sess_abc"}},
    )
    return mocked


def test_session_allowed_without_token_when_unset():
    client = TestClient(app)
    mocked = _session_client()
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mocked)):
        response = client.post("/session", json={"call_id": "call-1"})
    assert response.status_code == 200


def test_session_rejects_missing_token_when_configured(gated):
    client = TestClient(app)
    response = client.post("/session", json={"call_id": "call-1"})
    assert response.status_code == 401


def test_session_rejects_wrong_token_when_configured(gated):
    client = TestClient(app)
    response = client.post("/session?access_token=wrong", json={"call_id": "call-1"})
    assert response.status_code == 401


def test_session_accepts_correct_token_when_configured(gated):
    client = TestClient(app)
    mocked = _session_client()
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mocked)):
        response = client.post(f"/session?access_token={gated}", json={"call_id": "call-1"})
    assert response.status_code == 200


def test_bridge_ws_closes_on_missing_or_wrong_token_when_configured(gated):
    _clear_dispatcher_state()
    app.dependency_overrides[get_repos] = _override_repos
    client = TestClient(app)
    try:
        with pytest.raises(Exception):
            with client.websocket_connect("/bridge?call_id=gated-call") as ws:
                ws.receive_text()
        with pytest.raises(Exception):
            with client.websocket_connect("/bridge?call_id=gated-call&access_token=wrong") as ws:
                ws.receive_text()
    finally:
        app.dependency_overrides.pop(get_repos, None)
        _clear_dispatcher_state()


def test_bridge_ws_accepts_correct_token_when_configured(gated):
    _clear_dispatcher_state()
    app.dependency_overrides[get_repos] = _override_repos
    client = TestClient(app)
    try:
        with client.websocket_connect(f"/bridge?call_id=gated-call&access_token={gated}") as ws:
            ws.send_json({"type": "speech_started"})
            assert "gated-call" in dispatcher.CONNECTIONS
    finally:
        app.dependency_overrides.pop(get_repos, None)
        _clear_dispatcher_state()


def test_admin_routes_gated_the_same_way(gated):
    repos = _override_repos()
    app.dependency_overrides[get_repos] = lambda: repos
    client = TestClient(app)
    try:
        assert client.get("/admin").status_code == 401
        assert client.get("/admin?access_token=wrong").status_code == 401
        assert client.get(f"/admin?access_token={gated}").status_code == 200
    finally:
        app.dependency_overrides.pop(get_repos, None)
