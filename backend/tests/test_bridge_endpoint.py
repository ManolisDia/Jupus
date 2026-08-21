from fastapi.testclient import TestClient

from backend.app import app, get_repos
from backend.db.repositories import Repositories
from backend.supervisor.state import CALL_STATES
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


def _override_repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


def test_bridge_round_trip_and_disconnect_marks_call_abandoned():
    CALL_STATES.clear()
    fake_repos = _override_repos()
    app.dependency_overrides[get_repos] = lambda: fake_repos
    client = TestClient(app)

    try:
        with client.websocket_connect("/bridge?call_id=test-call") as ws:
            ws.send_json(
                {
                    "type": "ask_supervisor",
                    "tool_call_id": "tool-1",
                    "reason": "wants info",
                    "last_caller_utterance": "hi there",
                }
            )
            response = ws.receive_json()
            assert response["type"] == "supervisor_result"
            assert response["tool_call_id"] == "tool-1"
            assert CALL_STATES["test-call"]["stage"] == "routing"
        # `with` block exit closes the socket — server should see
        # WebSocketDisconnect and mark the in-progress call abandoned.
        assert CALL_STATES["test-call"]["stage"] == "ended"
        assert fake_repos.calls.get("test-call")["outcome"] == "abandoned"
    finally:
        app.dependency_overrides.pop(get_repos, None)
        CALL_STATES.clear()
