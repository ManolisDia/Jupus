import json

import pytest

from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.db.repositories.testing import create_in_memory_connection
from backend.supervisor.state import new_call_state


@pytest.fixture
def conn():
    return create_in_memory_connection()


@pytest.fixture
def repo(conn):
    return SQLiteCallRepository(conn)


def test_upsert_creates_row_on_first_turn(repo):
    state = new_call_state("call-1")
    repo.upsert(state)
    row = repo.get("call-1")
    assert row is not None
    assert row["outcome"] is None


def test_upsert_preserves_started_at_across_calls(repo):
    state = new_call_state("call-1")
    repo.upsert(state)
    first_started_at = repo.get("call-1")["started_at"]

    repo.upsert(state)
    second_started_at = repo.get("call-1")["started_at"]

    assert first_started_at == second_started_at


def test_upsert_sets_ended_at_and_outcome_booked(repo):
    state = new_call_state("call-1")
    state["stage"] = "ended"
    state["booking_confirmed"] = True
    repo.upsert(state)
    row = repo.get("call-1")
    assert row["ended_at"] is not None
    assert row["outcome"] == "booked"


def test_upsert_sets_outcome_escalated(repo):
    state = new_call_state("call-1")
    state["stage"] = "ended"
    state["escalation_reason"] = "no_acceptable_slot"
    repo.upsert(state)
    row = repo.get("call-1")
    assert row["outcome"] == "escalated"


def test_upsert_transcript_json_round_trips(repo):
    state = new_call_state("call-1")
    state["transcript"] = [
        {"role": "caller", "text": "hi", "ts": "t1"},
        {"role": "agent", "text": "hello", "ts": "t2"},
    ]
    repo.upsert(state)
    row = repo.get("call-1")
    assert json.loads(row["transcript_json"]) == state["transcript"]


def test_upsert_only_persists_confirmed_caller_fields(repo):
    state = new_call_state("call-1")
    state["caller_profile"]["name"]["value"] = "John Smith"
    state["caller_profile"]["name"]["status"] = "confirmed"
    state["caller_profile"]["email"]["value"] = "j@example.com"
    state["caller_profile"]["email"]["status"] = "pending_confirm"
    repo.upsert(state)
    row = repo.get("call-1")
    assert row["caller_name"] == "John Smith"
    assert row["caller_email"] is None
