from backend.db.repositories.sqlite_annotations import SQLiteAnnotationRepository
from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.db.repositories.testing import create_in_memory_connection
from backend.supervisor.state import new_call_state


def _seed_call(conn, call_id):
    state = new_call_state(call_id)
    state["stage"] = "ended"
    state["booking_confirmed"] = True
    SQLiteCallRepository(conn).upsert(state)


def test_save_review_and_get_review_roundtrip():
    conn = create_in_memory_connection()
    _seed_call(conn, "c1")
    repo = SQLiteAnnotationRepository(conn)

    repo.save_review(
        "c1",
        annotator="benevolent_dictator",
        error_class_ids=["repetition", "unconfirmed_action"],
        uncategorized_notes=["agent sounded confused about the timezone"],
        overall_note="mostly fine, one repeated question",
        is_gold=True,
    )

    review = repo.get_review("c1")
    assert review["annotator"] == "benevolent_dictator"
    assert review["is_gold"] == 1
    assert len(review["annotations"]) == 3
    flagged_classes = {a["error_class_id"] for a in review["annotations"] if a["error_class_id"]}
    assert flagged_classes == {"repetition", "unconfirmed_action"}
    uncategorized = [a for a in review["annotations"] if a["error_class_id"] is None]
    assert len(uncategorized) == 1
    assert uncategorized[0]["note"] == "agent sounded confused about the timezone"


def test_get_review_returns_none_when_not_reviewed():
    conn = create_in_memory_connection()
    _seed_call(conn, "c1")
    repo = SQLiteAnnotationRepository(conn)

    assert repo.get_review("c1") is None


def test_re_reviewing_replaces_prior_annotations():
    conn = create_in_memory_connection()
    _seed_call(conn, "c1")
    repo = SQLiteAnnotationRepository(conn)

    repo.save_review("c1", "benevolent_dictator", ["repetition"], [], "first pass", False)
    repo.save_review("c1", "benevolent_dictator", ["unconfirmed_action"], [], "second pass", True)

    review = repo.get_review("c1")
    assert review["overall_note"] == "second pass"
    assert review["is_gold"] == 1
    assert len(review["annotations"]) == 1
    assert review["annotations"][0]["error_class_id"] == "unconfirmed_action"


def test_list_unreviewed_excludes_reviewed_calls_and_orders_oldest_first():
    conn = create_in_memory_connection()
    _seed_call(conn, "c1")
    _seed_call(conn, "c2")
    repo = SQLiteAnnotationRepository(conn)

    repo.save_review("c1", "benevolent_dictator", [], [], "reviewed", False)

    unreviewed = repo.list_unreviewed()
    assert [c["call_id"] for c in unreviewed] == ["c2"]
