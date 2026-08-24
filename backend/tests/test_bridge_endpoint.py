from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import dispatcher
from backend.app import app, get_repos
from backend.db.repositories import Repositories
from backend.supervisor.state import CALL_STATES
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


def _override_repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


def _clear_dispatcher_state():
    CALL_STATES.clear()
    dispatcher.LOCKS.clear()
    dispatcher.SPEAKING.clear()
    dispatcher.DEFERRED.clear()
    dispatcher.CONNECTIONS.clear()


def _receive_supervisor_result(ws):
    # Every successful turn now also broadcasts a "call_state" message (Phase
    # 7 caller-facing live-state stretch) alongside the reply — these tests
    # care about the reply, not message ordering between the two, so skip
    # past any other message type.
    while True:
        msg = ws.receive_json()
        if msg["type"] == "supervisor_result":
            return msg


def test_bridge_round_trip_and_disconnect_marks_call_abandoned():
    _clear_dispatcher_state()
    fake_repos = _override_repos()
    app.dependency_overrides[get_repos] = lambda: fake_repos
    client = TestClient(app)

    # A fresh call now chains greeting straight into the (real, Claude-backed)
    # routing node within the same dispatch — mock classification so this
    # test stays deterministic and network-free like the rest of the suite.
    classify_patch = patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "unclear", "confidence": 0.2},
    )
    classify_patch.start()

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
            # process_supervisor_call runs fire-and-forget on the server's
            # event loop — the result arrives asynchronously once it resolves.
            response = _receive_supervisor_result(ws)
            assert response["tool_call_id"] == "tool-1"
            assert CALL_STATES["test-call"]["stage"] == "routing"
        # `with` block exit closes the socket — server should see
        # WebSocketDisconnect and mark the in-progress call abandoned.
        assert CALL_STATES["test-call"]["stage"] == "ended"
        assert fake_repos.calls.get("test-call")["outcome"] == "abandoned"
    finally:
        classify_patch.stop()
        app.dependency_overrides.pop(get_repos, None)
        _clear_dispatcher_state()


def test_bridge_relays_speech_started_and_stopped():
    _clear_dispatcher_state()
    fake_repos = _override_repos()
    app.dependency_overrides[get_repos] = lambda: fake_repos
    client = TestClient(app)

    # This test is about the SPEAKING/DEFERRED plumbing, not classification —
    # but a fresh call now chains greeting straight into the (real,
    # Claude-backed) routing node within the same dispatch, so mock
    # classify_practice_area to keep this test's timing assertion about
    # dispatcher behavior, not live API latency.
    classify_patch = patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "unclear", "confidence": 0.2},
    )
    classify_patch.start()

    try:
        with client.websocket_connect("/bridge?call_id=test-call-2") as ws:
            ws.send_json({"type": "speech_started"})
            ws.send_json(
                {
                    "type": "ask_supervisor",
                    "tool_call_id": "tool-1",
                    "reason": "wants info",
                    "last_caller_utterance": "hi there",
                }
            )
            # Caller is (per our own message) still speaking, so the reply
            # must be deferred rather than sent immediately.
            import time

            time.sleep(0.1)
            assert "test-call-2" in dispatcher.DEFERRED

            ws.send_json({"type": "speech_stopped"})
            _receive_supervisor_result(ws)
    finally:
        classify_patch.stop()
        app.dependency_overrides.pop(get_repos, None)
        _clear_dispatcher_state()
