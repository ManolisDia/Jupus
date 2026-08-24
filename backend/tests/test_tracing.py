import threading

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


def test_record_event_is_thread_safe_no_duplicate_or_gapped_seq(repo):
    # Regression test for docs/code-review-2026-08-24.md finding #2:
    # record_event is called from both the asyncio.to_thread worker thread
    # (GRAPH.invoke's node functions) and the main event-loop thread
    # (drain_deferred) for the same call_id with no other synchronization —
    # a non-atomic read-modify-write on an in-memory counter let two threads
    # read the same seq and both write it, producing duplicate rows with no
    # unique constraint to catch it. 20 threads hammering the same call_id
    # concurrently must still produce exactly 0..19 with no duplicates/gaps.
    threads = [threading.Thread(target=repo.record_event, args=("call-1", "e")) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = sorted(e["seq"] for e in repo.get_trace("call-1"))
    assert seqs == list(range(20))


def test_record_event_seq_survives_repository_recreation(conn):
    # Regression test for docs/code-review-2026-08-24.md finding #3: the old
    # in-memory _seq_counters dict reset to empty on process restart (uvicorn
    # --reload restarts on every file save), so a call in flight across a
    # restart got seq=0,1,2... again, colliding with/sorting before its own
    # pre-restart events. A fresh SQLiteTraceRepository instance against the
    # SAME underlying connection/DB (simulating a restart, new process =
    # fresh Python object, same on-disk data) must continue from the DB's
    # real MAX(seq), not restart at 0.
    first_instance = SQLiteTraceRepository(conn)
    first_instance.record_event("call-1", "a")
    first_instance.record_event("call-1", "b")

    second_instance = SQLiteTraceRepository(conn)  # fresh object, same conn/data
    second_instance.record_event("call-1", "c")

    seqs = [e["seq"] for e in second_instance.get_trace("call-1")]
    assert seqs == [0, 1, 2]


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
