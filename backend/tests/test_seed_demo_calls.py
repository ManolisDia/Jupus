import json

from backend.db.repositories.sqlite_annotations import SQLiteAnnotationRepository
from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.db.repositories.testing import create_in_memory_connection
from backend.db.seed_demo_calls import seed, seed_annotations
from eval.error_classes import get_active_error_classes


def test_seed_creates_eight_calls_total():
    conn = create_in_memory_connection()
    seed(conn)

    repo = SQLiteCallRepository(conn)
    rows = repo.list()

    assert len(rows) == 8
    outcomes = sorted(r["outcome"] for r in rows)
    assert outcomes == [
        "booked", "booked", "booked", "booked", "booked", "booked", "escalated", "escalated",
    ]


def test_seed_includes_the_original_base_three_outcomes():
    # Phase 6a's original 3 rows (2 booked, 1 legitimate escalation reason)
    # are still present among the 8, unchanged.
    conn = create_in_memory_connection()
    seed(conn)

    repo = SQLiteCallRepository(conn)
    escalated_legit = repo.get("demo-escalated-1")
    assert escalated_legit["outcome"] == "escalated"
    assert escalated_legit["escalation_reason"] == "unable_to_classify"
    assert repo.get("demo-booked-1")["outcome"] == "booked"
    assert repo.get("demo-booked-2")["outcome"] == "booked"


def test_seed_is_idempotent():
    conn = create_in_memory_connection()
    seed(conn)
    seed(conn)

    repo = SQLiteCallRepository(conn)
    assert len(repo.list()) == 8


def test_seed_includes_one_example_per_error_class():
    # Structural check only (per docs/phases/phase-6b-error-taxonomy.md):
    # proves each fixture is shaped so a judge *could* plausibly classify it
    # against that class — doesn't prove a real judge will, only that the
    # fixture isn't malformed.
    conn = create_in_memory_connection()
    seed(conn)
    repo = SQLiteCallRepository(conn)

    repetition_transcript = json.loads(repo.get("demo-repetition-1")["transcript_json"])
    email_questions = [t for t in repetition_transcript if t["role"] == "agent" and "email address" in t["text"]]
    assert len(email_questions) >= 2

    tool_failure_transcript = json.loads(repo.get("demo-tool-failure-1")["transcript_json"])
    assert any("having a little trouble" in t["text"] for t in tool_failure_transcript if t["role"] == "agent")

    premature = repo.get("demo-premature-escalation-1")
    assert premature["outcome"] == "escalated"
    premature_transcript = json.loads(premature["transcript_json"])
    assert len(premature_transcript) == 2  # escalated on the very first exchange, no failed attempts first

    unconfirmed = repo.get("demo-unconfirmed-action-1")
    assert unconfirmed["outcome"] == "booked"
    unconfirmed_transcript = json.loads(unconfirmed["transcript_json"])
    # no read-back/confirmation turn between the field capture and the booking line
    booking_idx = next(i for i, t in enumerate(unconfirmed_transcript) if "booked" in t["text"].lower())
    assert booking_idx == len(unconfirmed_transcript) - 1
    assert not any("did you say" in t["text"].lower() for t in unconfirmed_transcript)

    active_ids = {c["id"] for c in get_active_error_classes()}
    assert active_ids == {
        "repetition", "tool_or_system_failure_surfaced", "premature_escalation", "unconfirmed_action",
    }


def test_seed_includes_deliberately_bad_invalid_email_call():
    conn = create_in_memory_connection()
    seed(conn)
    repo = SQLiteCallRepository(conn)

    row = repo.get("demo-invalid-email-1")
    assert row["outcome"] == "booked"
    assert "@" not in row["caller_email"]


def test_seed_annotations_populates_two_reviews():
    conn = create_in_memory_connection()
    seed(conn)
    seed_annotations(conn)

    annotations = SQLiteAnnotationRepository(conn)
    repetition_review = annotations.get_review("demo-repetition-1")
    assert repetition_review is not None
    assert repetition_review["is_gold"] == 1
    assert any(a["error_class_id"] == "repetition" for a in repetition_review["annotations"])

    unconfirmed_review = annotations.get_review("demo-unconfirmed-action-1")
    assert unconfirmed_review is not None
    assert any(a["error_class_id"] == "unconfirmed_action" for a in unconfirmed_review["annotations"])
