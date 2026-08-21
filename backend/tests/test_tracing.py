import pytest

from backend.db.repositories.sqlite_trace import SQLiteTraceRepository
from backend.db.repositories.testing import create_in_memory_connection
from backend.supervisor.tracing import traced_call


@pytest.fixture
def conn():
    return create_in_memory_connection()


@pytest.fixture
def repo(conn):
    return SQLiteTraceRepository(conn)


def test_record_event_assigns_monotonic_seq_per_call(repo):
    repo.record_event("call-1", "a")
    repo.record_event("call-1", "b")
    repo.record_event("call-1", "c")
    trace = repo.get_trace("call-1")
    assert [e["seq"] for e in trace] == [0, 1, 2]


def test_record_event_seq_independent_across_calls(repo):
    repo.record_event("call-1", "a")
    repo.record_event("call-2", "a")
    assert repo.get_trace("call-1")[0]["seq"] == 0
    assert repo.get_trace("call-2")[0]["seq"] == 0


def test_get_trace_returns_events_in_seq_order(repo, conn):
    conn.execute(
        "INSERT INTO trace_events (call_id, seq, ts, event_type, node, payload_json) "
        "VALUES ('call-1', 2, 't', 'c', NULL, '{}')"
    )
    conn.execute(
        "INSERT INTO trace_events (call_id, seq, ts, event_type, node, payload_json) "
        "VALUES ('call-1', 0, 't', 'a', NULL, '{}')"
    )
    conn.execute(
        "INSERT INTO trace_events (call_id, seq, ts, event_type, node, payload_json) "
        "VALUES ('call-1', 1, 't', 'b', NULL, '{}')"
    )
    conn.commit()
    trace = repo.get_trace("call-1")
    assert [e["event_type"] for e in trace] == ["a", "b", "c"]


def test_traced_call_records_start_and_end_on_success(repo):
    result = traced_call(repo, "call-1", "capture", "some_tool", lambda x: x * 2, 21)
    assert result == 42
    trace = repo.get_trace("call-1")
    event_types = [e["event_type"] for e in trace]
    assert event_types == ["tool_call_start", "tool_call_end"]


def test_traced_call_records_failure_and_reraises(repo):
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        traced_call(repo, "call-1", "capture", "some_tool", boom)

    trace = repo.get_trace("call-1")
    assert trace[-1]["event_type"] == "tool_call_end"
