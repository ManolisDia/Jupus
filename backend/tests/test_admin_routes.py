import pytest
from fastapi.testclient import TestClient

from backend.app import app, get_repos
from backend.db.repositories import Repositories
from backend.db.repositories.sqlite_annotations import SQLiteAnnotationRepository
from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.db.repositories.sqlite_eval import SQLiteEvalRepository
from backend.db.repositories.sqlite_trace import SQLiteTraceRepository
from backend.db.repositories.testing import create_in_memory_connection
from backend.db.seed_demo_calls import seed, seed_annotations
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


@pytest.fixture
def seeded_sqlite_repos():
    """Real SQLite-backed repos over a fresh in-memory DB, seeded with the 8
    demo calls (+ 2 BD annotations) — needed for the trace-ordering test,
    which writes rows out of insertion order via direct SQL (fine in test
    setup; only app code is barred from raw SQL per CLAUDE.md rule 9)."""
    conn = create_in_memory_connection()
    seed(conn)
    seed_annotations(conn)
    repos = Repositories(
        calls=SQLiteCallRepository(conn),
        slots=None,
        trace=SQLiteTraceRepository(conn),
        evals=SQLiteEvalRepository(conn),
        annotations=SQLiteAnnotationRepository(conn),
    )
    return repos, conn


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
            "call_id", "started_at", "practice_area", "outcome", "escalation_reason",
            "booking_slot_id", "error_classes", "reviewed",
        }


def test_api_calls_list_includes_error_badges(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    repos.evals.tag_eval_run("demo-repetition-1", "seed-check")
    repos.evals.add_error_flags(
        "demo-repetition-1", [{"error_class_id": "repetition", "confidence": 0.9, "evidence": "x"}], "seed-check"
    )
    client = client_with(repos)

    response = client.get("/api/calls")

    rows = {row["call_id"]: row for row in response.json()}
    assert rows["demo-repetition-1"]["error_classes"] == ["repetition"]
    assert rows["demo-booked-1"]["error_classes"] == []


def test_api_calls_list_includes_reviewed_flag(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/calls")

    rows = {row["call_id"]: row for row in response.json()}
    assert rows["demo-repetition-1"]["reviewed"] is True  # pre-populated by seed_annotations
    assert rows["demo-booked-1"]["reviewed"] is False


def test_api_call_detail_returns_transcript(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/calls/demo-booked-1")

    assert response.status_code == 200
    body = response.json()
    assert body["call_id"] == "demo-booked-1"
    assert len(body["transcript"]) > 0
    assert body["call_error_flags"] == []
    assert body["human_review"] is None


def test_api_call_detail_includes_all_error_flags_for_that_call(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    repos.evals.tag_eval_run("demo-repetition-1", "run-a")
    repos.evals.add_error_flags(
        "demo-repetition-1",
        [
            {"error_class_id": "repetition", "confidence": 0.9, "evidence": "asked twice"},
            {"error_class_id": "unconfirmed_action", "confidence": 0.5, "evidence": "no read-back"},
        ],
        "run-a",
    )
    client = client_with(repos)

    response = client.get("/api/calls/demo-repetition-1")

    flags = response.json()["call_error_flags"]
    assert len(flags) == 2
    assert {f["error_class_id"] for f in flags} == {"repetition", "unconfirmed_action"}


def test_api_call_detail_includes_human_review_when_present(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/calls/demo-repetition-1")

    review = response.json()["human_review"]
    assert review is not None
    assert review["is_gold"] == 1


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


def test_api_eval_summary_includes_error_rates(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/eval/summary")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "booking_success_rate", "escalation_reason_histogram", "average_turns_per_call",
        "latency", "error_rates",
    }
    assert "repetition" in body["error_rates"]


def test_api_eval_taxonomy_suggestions_returns_seeded_suggestions(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    repos.evals.add_taxonomy_suggestions(
        [{"suggestion_type": "new_class", "call_id": None, "rationale": "recurring pattern"}], "run-a"
    )
    client = client_with(repos)

    response = client.get("/api/eval/taxonomy-suggestions", params={"label": "run-a"})

    assert response.status_code == 200
    suggestions = response.json()
    assert len(suggestions) == 1
    assert suggestions[0]["status"] == "pending"


def test_status_filter_scopes_taxonomy_suggestion_results(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    repos.evals.add_taxonomy_suggestions(
        [{"suggestion_type": "new_class", "call_id": None, "rationale": "x"}], "run-a"
    )
    suggestion_id = repos.evals.list_taxonomy_suggestions("run-a", "pending")[0]["id"]
    repos.evals.update_suggestion_status(suggestion_id, "approved")
    client = client_with(repos)

    pending = client.get("/api/eval/taxonomy-suggestions", params={"status": "pending"}).json()
    approved = client.get("/api/eval/taxonomy-suggestions", params={"status": "approved"}).json()

    assert pending == []
    assert len(approved) == 1


def test_approve_taxonomy_suggestion_updates_status(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    repos.evals.add_taxonomy_suggestions(
        [{"suggestion_type": "new_class", "call_id": None, "rationale": "x"}], "run-a"
    )
    suggestion_id = repos.evals.list_taxonomy_suggestions("run-a", "pending")[0]["id"]
    client = client_with(repos)

    response = client.post(f"/api/eval/taxonomy-suggestions/{suggestion_id}/approve")

    assert response.status_code == 200
    assert repos.evals.list_taxonomy_suggestions("run-a", "approved")[0]["id"] == suggestion_id


def test_reject_taxonomy_suggestion_updates_status(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    repos.evals.add_taxonomy_suggestions(
        [{"suggestion_type": "new_class", "call_id": None, "rationale": "x"}], "run-a"
    )
    suggestion_id = repos.evals.list_taxonomy_suggestions("run-a", "pending")[0]["id"]
    client = client_with(repos)

    response = client.post(f"/api/eval/taxonomy-suggestions/{suggestion_id}/reject")

    assert response.status_code == 200
    assert repos.evals.list_taxonomy_suggestions("run-a", "rejected")[0]["id"] == suggestion_id


def test_api_eval_compare_returns_delta_table(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    repos.evals.tag_eval_run("demo-booked-1", "baseline")
    repos.evals.tag_eval_run("demo-booked-1", "candidate")
    repos.evals.add_error_flags(
        "demo-booked-1", [{"error_class_id": "repetition", "confidence": 0.9, "evidence": "x"}], "candidate"
    )
    client = client_with(repos)

    response = client.get("/api/eval/compare", params={"baseline": "baseline", "candidate": "candidate"})

    assert response.status_code == 200
    body = response.json()
    assert body["baseline"] == "baseline"
    assert body["candidate"] == "candidate"
    row = next(r for r in body["rows"] if r["error_class_id"] == "repetition")
    assert row["delta"] == 1.0


def test_unreviewed_endpoint_excludes_calls_with_review(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/calls/unreviewed")

    unreviewed_ids = {c["call_id"] for c in response.json()}
    assert "demo-repetition-1" not in unreviewed_ids  # pre-reviewed by seed_annotations
    assert "demo-booked-1" in unreviewed_ids


def test_get_review_404_when_not_yet_reviewed(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/calls/demo-booked-1/review")

    assert response.status_code == 404


def test_post_review_creates_call_review_and_annotation_rows(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.post(
        "/api/calls/demo-booked-1/review",
        json={
            "error_class_ids": ["repetition", "unconfirmed_action"],
            "uncategorized_notes": ["something odd about the timezone"],
            "overall_note": "mostly fine",
            "is_gold": True,
        },
    )

    assert response.status_code == 200
    review = response.json()
    assert review["is_gold"] == 1
    assert len(review["annotations"]) == 3


def test_re_reviewing_a_call_replaces_prior_annotations(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)
    client.post("/api/calls/demo-booked-1/review", json={"error_class_ids": ["repetition"], "overall_note": "first"})

    response = client.post(
        "/api/calls/demo-booked-1/review", json={"error_class_ids": ["unconfirmed_action"], "overall_note": "second"}
    )

    review = response.json()
    assert review["overall_note"] == "second"
    assert len(review["annotations"]) == 1
    assert review["annotations"][0]["error_class_id"] == "unconfirmed_action"


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


def test_annotate_page_serves_html():
    repos = Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())
    app.dependency_overrides[get_repos] = lambda: repos
    client = TestClient(app)
    try:
        response = client.get("/admin/annotate")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
    finally:
        app.dependency_overrides.pop(get_repos, None)
