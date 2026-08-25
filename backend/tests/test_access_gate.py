import pytest
from fastapi.testclient import TestClient

from backend.app import app, get_repos
from backend.config import settings
from backend.db.repositories import Repositories
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


def _override_repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(settings, "jupus_access_token", "s3cret")
    yield "s3cret"


@pytest.fixture(autouse=True)
def livekit_configured(monkeypatch):
    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_api_key", "APItestkey")
    monkeypatch.setattr(settings, "livekit_api_secret", "s" * 32)
    yield


# Phase 14: /session and /bridge are gone, so the call-facing endpoint this
# gate has to cover is /livekit-token — the one that hands out a room token.
# An ungated one would let anyone who finds the URL join a call.
def test_livekit_token_allowed_without_token_when_unset():
    assert TestClient(app).post("/livekit-token", json={"call_id": "call-1"}).status_code == 200


def test_livekit_token_rejects_missing_token_when_configured(gated):
    assert TestClient(app).post("/livekit-token", json={"call_id": "call-1"}).status_code == 401


def test_livekit_token_rejects_wrong_token_when_configured(gated):
    client = TestClient(app)
    response = client.post("/livekit-token?access_token=wrong", json={"call_id": "call-1"})
    assert response.status_code == 401


def test_livekit_token_accepts_correct_token_when_configured(gated):
    client = TestClient(app)
    response = client.post(f"/livekit-token?access_token={gated}", json={"call_id": "call-1"})
    assert response.status_code == 200


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


def test_admin_asset_requests_pass_via_cookie_after_the_first_query_param_hit(gated):
    # The browser's own follow-up requests for /admin's JS/CSS never carry
    # ?access_token= — only the link a person is given does. Without the
    # cookie fallback these 401 and the page never finishes loading.
    repos = _override_repos()
    app.dependency_overrides[get_repos] = lambda: repos
    client = TestClient(app)
    try:
        first = client.get(f"/admin?access_token={gated}")
        assert first.status_code == 200
        assert "jupus_admin_token" in client.cookies  # set via the redirect hop's Set-Cookie

        asset = client.get("/admin/app.js")  # no query param, cookie only
        assert asset.status_code == 200
    finally:
        app.dependency_overrides.pop(get_repos, None)
