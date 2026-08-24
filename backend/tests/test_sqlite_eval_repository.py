from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.db.repositories.sqlite_eval import SQLiteEvalRepository
from backend.db.repositories.testing import create_in_memory_connection
from backend.supervisor.state import new_call_state


def _seed_one_call(conn, call_id="c1"):
    state = new_call_state(call_id)
    state["stage"] = "ended"
    state["booking_confirmed"] = True
    SQLiteCallRepository(conn).upsert(state)


def test_tag_eval_run_and_call_ids_already_evaluated():
    conn = create_in_memory_connection()
    _seed_one_call(conn, "c1")
    repo = SQLiteEvalRepository(conn)

    repo.tag_eval_run("c1", "label-a")

    assert repo.call_ids_already_evaluated() == {"c1"}


def test_add_error_flags_and_get_error_flags():
    conn = create_in_memory_connection()
    _seed_one_call(conn, "c1")
    repo = SQLiteEvalRepository(conn)

    repo.add_error_flags(
        "c1",
        [{"error_class_id": "repetition", "confidence": 0.8, "evidence": "asked twice"}],
        "label-a",
    )

    flags = repo.get_error_flags("c1")
    assert len(flags) == 1
    assert flags[0]["error_class_id"] == "repetition"
    assert flags[0]["evidence"] == "asked twice"


def test_compute_error_rates_includes_zero_rate_classes():
    conn = create_in_memory_connection()
    _seed_one_call(conn, "c1")
    _seed_one_call(conn, "c2")
    repo = SQLiteEvalRepository(conn)
    repo.tag_eval_run("c1", "label-a")
    repo.tag_eval_run("c2", "label-a")
    repo.add_error_flags("c1", [{"error_class_id": "repetition", "confidence": 0.9, "evidence": "x"}], "label-a")

    rates = repo.compute_error_rates("label-a")

    assert rates["repetition"] == 0.5
    assert rates["premature_escalation"] == 0.0


def test_compute_error_rates_zero_denominator_returns_zeros():
    conn = create_in_memory_connection()
    repo = SQLiteEvalRepository(conn)

    rates = repo.compute_error_rates("nonexistent-label")

    assert all(rate == 0.0 for rate in rates.values())


def test_taxonomy_suggestion_lifecycle():
    conn = create_in_memory_connection()
    _seed_one_call(conn, "c1")
    repo = SQLiteEvalRepository(conn)

    repo.add_taxonomy_suggestions(
        [{"suggestion_type": "new_class", "call_id": "c1", "rationale": "recurring pattern"}],
        "label-a",
    )

    pending = repo.list_taxonomy_suggestions("label-a", "pending")
    assert len(pending) == 1
    suggestion_id = pending[0]["id"]

    repo.update_suggestion_status(suggestion_id, "approved")

    approved = repo.list_taxonomy_suggestions("label-a", "approved")
    assert len(approved) == 1
    assert repo.list_taxonomy_suggestions("label-a", "pending") == []
