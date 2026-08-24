from fastapi.testclient import TestClient

from backend.app import app, get_repos
from backend.db.repositories import Repositories
from backend.supervisor.state import CALL_STATES, new_call_state
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


def _override_repos(trace_repo):
    return Repositories(calls=FakeCallRepository(), slots=None, trace=trace_repo)


def _clear_call_states():
    CALL_STATES.clear()


def test_trace_stream_sends_existing_backlog_on_connect():
    trace_repo = FakeTraceRepository()
    trace_repo.record_event("call-1", "node_entered", node="routing")
    trace_repo.record_event("call-1", "node_exited", node="routing", stage_from="routing", stage_to="capture")

    app.dependency_overrides[get_repos] = lambda: _override_repos(trace_repo)
    client = TestClient(app)
    try:
        with client.websocket_connect("/admin/trace/call-1") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "trace_events"
            assert len(msg["events"]) == 2
            assert msg["events"][0]["event_type"] == "node_entered"
            assert msg["events"][1]["event_type"] == "node_exited"
    finally:
        app.dependency_overrides.pop(get_repos, None)


def test_trace_stream_sends_only_new_events_since_last_send():
    trace_repo = FakeTraceRepository()
    trace_repo.record_event("call-1", "node_entered", node="routing")

    app.dependency_overrides[get_repos] = lambda: _override_repos(trace_repo)
    client = TestClient(app)
    try:
        with client.websocket_connect("/admin/trace/call-1") as ws:
            first = ws.receive_json()
            assert len(first["events"]) == 1

            trace_repo.record_event("call-1", "node_exited", node="routing", stage_from="routing", stage_to="capture")

            second = ws.receive_json()
            assert len(second["events"]) == 1
            assert second["events"][0]["event_type"] == "node_exited"
    finally:
        app.dependency_overrides.pop(get_repos, None)


def test_trace_stream_for_unknown_call_id_sends_nothing_then_stays_open():
    trace_repo = FakeTraceRepository()

    app.dependency_overrides[get_repos] = lambda: _override_repos(trace_repo)
    client = TestClient(app)
    try:
        with client.websocket_connect("/admin/trace/never-happened") as ws:
            trace_repo.record_event("never-happened", "node_entered", node="greeting")
            msg = ws.receive_json()
            assert msg["events"][0]["node"] == "greeting"
    finally:
        app.dependency_overrides.pop(get_repos, None)


def test_trace_stream_sends_call_state_snapshot_when_present():
    # The graph page's node sub-state badges (field-by-field capture
    # progress, slot proposal/decline) come from this — CALL_STATES is the
    # same in-memory, live-process state a real call already holds, read
    # directly (not through a repository — there's no DB row for it).
    _clear_call_states()
    trace_repo = FakeTraceRepository()
    state = new_call_state("call-1")
    state["stage"] = "capture"
    state["caller_profile"]["name"]["status"] = "confirmed"
    state["caller_profile"]["name"]["value"] = "Jordan Lee"
    CALL_STATES["call-1"] = state

    app.dependency_overrides[get_repos] = lambda: _override_repos(trace_repo)
    client = TestClient(app)
    try:
        with client.websocket_connect("/admin/trace/call-1") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "call_state"
            assert msg["stage"] == "capture"
            assert msg["caller_profile"]["name"]["status"] == "confirmed"
            assert msg["caller_profile"]["name"]["value"] == "Jordan Lee"
            assert "booking" in msg
    finally:
        app.dependency_overrides.pop(get_repos, None)
        _clear_call_states()


def test_trace_stream_resends_call_state_only_when_it_changes():
    _clear_call_states()
    trace_repo = FakeTraceRepository()
    state = new_call_state("call-1")
    CALL_STATES["call-1"] = state

    app.dependency_overrides[get_repos] = lambda: _override_repos(trace_repo)
    client = TestClient(app)
    try:
        with client.websocket_connect("/admin/trace/call-1") as ws:
            first = ws.receive_json()
            assert first["type"] == "call_state"
            assert first["booking"]["proposed_slot_id"] is None

            CALL_STATES["call-1"]["proposed_slot_id"] = 42
            second = ws.receive_json()
            assert second["type"] == "call_state"
            assert second["booking"]["proposed_slot_id"] == 42
    finally:
        app.dependency_overrides.pop(get_repos, None)
        _clear_call_states()
