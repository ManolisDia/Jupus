import pytest
from fastapi.testclient import TestClient

from backend.app import app, get_repos
from backend.db.repositories import Repositories
from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.db.repositories.sqlite_trace import SQLiteTraceRepository
from backend.db.repositories.testing import create_in_memory_connection
from backend.db.seed_demo_calls import seed
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


@pytest.fixture
def seeded_sqlite_repos():
    """Real SQLite-backed repos over a fresh in-memory DB, seeded with the 3
    demo calls — needed for the trace-ordering test, which writes rows out of
    insertion order via direct SQL (fine in test setup; only app code is
    barred from raw SQL per CLAUDE.md rule 9)."""
    conn = create_in_memory_connection()
    seed(conn)
    return Repositories(calls=SQLiteCallRepository(conn), slots=None, trace=SQLiteTraceRepository(conn)), conn


@pytest.fixture
def client_with(monkeypatch):
    def _make(repos):
        app.dependency_overrides[get_repos] = lambda: repos
        return TestClient(app)

    yield _make
    app.dependency_overrides.pop(get_repos, None)


def test_api_calls_list_returns_seeded_calls(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/calls")

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 8
    assert {"demo-booked-1", "demo-booked-2", "demo-escalated-1"} <= {row["call_id"] for row in rows}
    for row in rows:
        assert set(row.keys()) == {
            "call_id", "started_at", "practice_area", "outcome", "escalation_reason", "booking_slot_id",
        }


def test_api_call_detail_returns_transcript(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/calls/demo-booked-1")

    assert response.status_code == 200
    body = response.json()
    assert body["call_id"] == "demo-booked-1"
    assert len(body["transcript"]) > 0
    assert "call_error_flags" not in body
    assert "human_review" not in body


def test_api_call_detail_404_for_unknown_id(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/calls/does-not-exist")

    assert response.status_code == 404


def test_api_call_trace_returns_ordered_events(client_with, seeded_sqlite_repos):
    repos, conn = seeded_sqlite_repos
    # seed out of order directly via SQL (bypassing the seq counter)
    conn.execute(
        "INSERT INTO trace_events (call_id, seq, ts, event_type, node, payload_json) VALUES (?,?,?,?,?,?)",
        ("demo-booked-1", 2, "t2", "tool_call_end", "booking", "{}"),
    )
    conn.execute(
        "INSERT INTO trace_events (call_id, seq, ts, event_type, node, payload_json) VALUES (?,?,?,?,?,?)",
        ("demo-booked-1", 0, "t0", "node_entered", "booking", "{}"),
    )
    conn.execute(
        "INSERT INTO trace_events (call_id, seq, ts, event_type, node, payload_json) VALUES (?,?,?,?,?,?)",
        ("demo-booked-1", 1, "t1", "tool_call_start", "booking", "{}"),
    )
    conn.commit()
    client = client_with(repos)

    response = client.get("/api/calls/demo-booked-1/trace")

    assert response.status_code == 200
    events = response.json()
    assert [e["seq"] for e in events] == [0, 1, 2]


def test_api_eval_summary_returns_deterministic_keys_only(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/eval/summary")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "booking_success_rate", "escalation_reason_histogram", "average_turns_per_call", "latency",
    }
    assert "error_rates" not in body


def test_admin_page_serves_html():
    repos = Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())
    app.dependency_overrides[get_repos] = lambda: repos
    client = TestClient(app)
    try:
        response = client.get("/admin")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "<title>" in response.text
    finally:
        app.dependency_overrides.pop(get_repos, None)
