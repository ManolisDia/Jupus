from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def _mock_response(status_code: int, json_body: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_body,
        request=httpx.Request("POST", "https://api.openai.com/v1/realtime/client_secrets"),
    )


def test_session_success_returns_client_secret():
    mocked = _mock_response(
        200,
        {
            "value": "ek_test123",
            "expires_at": 1234567890,
            "session": {"id": "sess_abc"},
        },
    )
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mocked)):
        response = client.post("/session", json={"call_id": "call-1"})

    assert response.status_code == 200
    body = response.json()
    assert body["client_secret"] == "ek_test123"
    assert body["session_id"] == "sess_abc"
    assert body["expires_at"] == "1234567890"


def test_session_openai_error_returns_502():
    mocked = _mock_response(401, {"error": {"message": "invalid api key"}})
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mocked)):
        response = client.post("/session", json={"call_id": "call-1"})

    assert response.status_code == 502
    assert "sk-" not in response.text
    assert "settings.openai_api_key" not in response.text


def test_session_missing_call_id_returns_422():
    response = client.post("/session", json={})
    assert response.status_code == 422
