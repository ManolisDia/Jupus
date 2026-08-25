"""Phase 14 — POST /livekit-token, the replacement for /session's role.

/session minted an OpenAI Realtime client secret for the BROWSER to open a
Realtime session with. /livekit-token mints a LiveKit room token instead, and
the browser never holds an OpenAI credential at all — the Realtime session is
opened server-side by the agent worker. These tests pin that boundary.
"""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend.app import app
from backend.config import settings


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def livekit_configured(monkeypatch):
    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_api_key", "APItestkey")
    monkeypatch.setattr(settings, "livekit_api_secret", "s" * 32)
    yield


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def test_returns_url_token_and_room(client):
    response = client.post("/livekit-token", json={"call_id": "call-1"})

    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "wss://test.livekit.cloud"
    assert body["room"] == "call-1"
    assert body["token"]


def test_room_name_is_the_call_id(client):
    # Load-bearing: the room name is how the call_id reaches the agent
    # (ctx.job.room.name). If these drift, every trace event and DB row for the
    # call keys off a different string than the graph does.
    response = client.post("/livekit-token", json={"call_id": "call-xyz"})

    claims = _claims(response.json()["token"])
    assert claims["video"]["room"] == "call-xyz"
    assert claims["video"]["roomJoin"] is True


def test_caller_can_publish_audio_and_data(client):
    # Audio for the call itself; data because call_state snapshots reach the
    # client over LiveKit's data channel now that /bridge is going away.
    claims = _claims(client.post("/livekit-token", json={"call_id": "c"}).json()["token"])

    assert claims["video"]["canPublish"] is True
    assert claims["video"]["canSubscribe"] is True
    assert claims["video"]["canPublishData"] is True


def test_api_secret_never_reaches_the_client(client):
    # The secret signs the token; it must never appear in the response body.
    body = client.post("/livekit-token", json={"call_id": "call-1"}).text

    assert settings.livekit_api_secret not in body


def test_missing_call_id_is_a_validation_error(client):
    assert client.post("/livekit-token", json={}).status_code == 422


def test_unconfigured_livekit_returns_503(client, monkeypatch):
    # A clearer failure than a 500 from the token library, and it tells the
    # operator exactly which env vars are missing.
    monkeypatch.setattr(settings, "livekit_api_key", None)

    response = client.post("/livekit-token", json={"call_id": "call-1"})

    assert response.status_code == 503
    assert "LIVEKIT_URL" in response.json()["error"]


def test_access_gate_applies_when_configured(client, monkeypatch):
    # Same shared-secret gate as /session (Phase 9, Decision 3) — a public
    # deployment must not hand out room tokens to anyone who finds the URL.
    monkeypatch.setattr(settings, "jupus_access_token", "sekret")

    assert client.post("/livekit-token", json={"call_id": "c"}).status_code == 401
    assert (
        client.post("/livekit-token?access_token=sekret", json={"call_id": "c"}).status_code == 200
    )
