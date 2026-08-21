"""Phase 6c — the Benevolent Dictator annotation routes (docs/phases/
phase-6c-benevolent-dictator.md, docs/benevolent_dictator.md): the unreviewed
queue and the review lifecycle (GET/POST .../review, delete-then-insert
re-annotation) and the taxonomy-suggestion approve/reject actions."""

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


@pytest.fixture
def seeded_sqlite_repos():
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
def client_with():
    def _make(repos):
        app.dependency_overrides[get_repos] = lambda: repos
        return TestClient(app)

    yield _make
    app.dependency_overrides.pop(get_repos, None)


def test_unreviewed_endpoint_excludes_calls_with_review(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/calls/unreviewed")

    unreviewed_ids = {c["call_id"] for c in response.json()}
    assert "demo-repetition-1" not in unreviewed_ids  # pre-reviewed by seed_annotations
    assert "demo-unconfirmed-action-1" not in unreviewed_ids  # also pre-reviewed
    assert "demo-booked-1" in unreviewed_ids


def test_unreviewed_endpoint_orders_oldest_first(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/calls/unreviewed")

    started_ats = [c["started_at"] for c in response.json()]
    assert started_ats == sorted(started_ats)


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
    categorized = {a["error_class_id"] for a in review["annotations"] if a["error_class_id"]}
    assert categorized == {"repetition", "unconfirmed_action"}
    uncategorized = [a for a in review["annotations"] if a["error_class_id"] is None]
    assert len(uncategorized) == 1
    assert uncategorized[0]["note"] == "something odd about the timezone"


def test_post_review_is_gold_flag_persisted(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    client.post("/api/calls/demo-booked-1/review", json={"error_class_ids": [], "overall_note": "clean", "is_gold": True})
    client.post("/api/calls/demo-booked-2/review", json={"error_class_ids": [], "overall_note": "clean", "is_gold": False})

    gold_review = client.get("/api/calls/demo-booked-1/review").json()
    non_gold_review = client.get("/api/calls/demo-booked-2/review").json()

    assert gold_review["is_gold"] == 1
    assert non_gold_review["is_gold"] == 0


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


def test_get_review_404_when_not_yet_reviewed(client_with, seeded_sqlite_repos):
    repos, _ = seeded_sqlite_repos
    client = client_with(repos)

    response = client.get("/api/calls/demo-booked-1/review")

    assert response.status_code == 404


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
