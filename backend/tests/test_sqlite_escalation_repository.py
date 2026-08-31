from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.db.repositories.sqlite_escalations import SQLiteEscalationRepository
from backend.db.repositories.testing import create_in_memory_connection
from backend.supervisor.state import new_call_state


def _escalated_state(call_id, *, reason="explicit_request", area="tenancy", **confirmed):
    state = new_call_state(call_id)
    state["stage"] = "ended"
    state["escalation_reason"] = reason
    state["practice_area"] = area
    for field, value in confirmed.items():
        state["caller_profile"][field]["value"] = value
        state["caller_profile"][field]["status"] = "confirmed"
    return state


def _seed_call(conn, state):
    SQLiteCallRepository(conn).upsert(state)


def test_record_and_get_roundtrip():
    conn = create_in_memory_connection()
    state = _escalated_state("c1", name="Dana", email="dana@example.com", phone="07700900123")
    _seed_call(conn, state)
    repo = SQLiteEscalationRepository(conn)

    repo.record(
        state,
        reason_for_call="Landlord is withholding the deposit after a move-out.",
        escalation_explanation="Caller asked to speak to a person before intake finished.",
    )

    row = repo.get("c1")
    assert row["call_id"] == "c1"
    assert row["escalation_reason"] == "explicit_request"
    assert row["reason_for_call"] == "Landlord is withholding the deposit after a move-out."
    assert row["escalation_explanation"] == "Caller asked to speak to a person before intake finished."
    assert row["practice_area"] == "tenancy"
    assert row["caller_name"] == "Dana"
    assert row["caller_email"] == "dana@example.com"
    assert row["caller_phone"] == "07700900123"
    assert row["escalated_at"]


def test_record_stores_only_confirmed_caller_fields():
    conn = create_in_memory_connection()
    state = _escalated_state("c1", name="Dana")
    # Heard, read back, never confirmed — not a fact about the caller.
    state["caller_profile"]["phone"]["value"] = "07700900999"
    state["caller_profile"]["phone"]["status"] = "pending_confirm"
    _seed_call(conn, state)
    repo = SQLiteEscalationRepository(conn)

    repo.record(state, reason_for_call="x", escalation_explanation="y")

    row = repo.get("c1")
    assert row["caller_name"] == "Dana"
    assert row["caller_phone"] is None


def test_record_accepts_missing_summary_fields():
    # The fallback path (escalating because a Claude call failed) has no
    # generated prose to store — that must persist, not raise.
    conn = create_in_memory_connection()
    state = _escalated_state("c1", reason="system_error", area=None)
    _seed_call(conn, state)
    repo = SQLiteEscalationRepository(conn)

    repo.record(state, reason_for_call=None, escalation_explanation="Unhandled error: boom")

    row = repo.get("c1")
    assert row["reason_for_call"] is None
    assert row["escalation_explanation"] == "Unhandled error: boom"
    assert row["practice_area"] is None


def test_record_twice_for_same_call_updates_rather_than_raising():
    # A late failure could follow the escalation node's own write in the
    # same call. The fallback path must not die on a PK collision.
    conn = create_in_memory_connection()
    state = _escalated_state("c1")
    _seed_call(conn, state)
    repo = SQLiteEscalationRepository(conn)

    repo.record(state, reason_for_call="first", escalation_explanation="first")
    state["escalation_reason"] = "system_error"
    repo.record(state, reason_for_call=None, escalation_explanation="Unhandled error: boom")

    assert len(repo.list()) == 1
    row = repo.get("c1")
    assert row["escalation_reason"] == "system_error"
    assert row["escalation_explanation"] == "Unhandled error: boom"


def test_get_returns_none_for_call_that_never_escalated():
    conn = create_in_memory_connection()
    _seed_call(conn, new_call_state("c1"))
    assert SQLiteEscalationRepository(conn).get("c1") is None


def test_list_returns_every_escalation():
    conn = create_in_memory_connection()
    repo = SQLiteEscalationRepository(conn)
    for call_id in ("c1", "c2"):
        state = _escalated_state(call_id)
        _seed_call(conn, state)
        repo.record(state, reason_for_call="x", escalation_explanation="y")

    assert {row["call_id"] for row in repo.list()} == {"c1", "c2"}
