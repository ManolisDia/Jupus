from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.db.repositories.testing import create_in_memory_connection
from backend.db.seed_demo_calls import seed


def test_seed_creates_three_calls_with_expected_outcomes():
    conn = create_in_memory_connection()
    seed(conn)

    repo = SQLiteCallRepository(conn)
    rows = repo.list()

    assert len(rows) == 3
    outcomes = sorted(r["outcome"] for r in rows)
    assert outcomes == ["booked", "booked", "escalated"]

    escalated = [r for r in rows if r["outcome"] == "escalated"][0]
    assert escalated["escalation_reason"] == "unable_to_classify"


def test_seed_is_idempotent():
    conn = create_in_memory_connection()
    seed(conn)
    seed(conn)

    repo = SQLiteCallRepository(conn)
    assert len(repo.list()) == 3
