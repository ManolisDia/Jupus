import pytest

from backend.db.repositories.base import SlotAlreadyBookedError
from backend.db.repositories.sqlite_slots import SQLiteSlotRepository
from backend.db.repositories.testing import create_in_memory_connection


def _insert_slot(conn, area: str, start_time: str, is_booked: int = 0) -> int:
    cursor = conn.execute(
        "INSERT INTO slots (area, start_time, is_booked) VALUES (?, ?, ?)",
        (area, start_time, is_booked),
    )
    conn.commit()
    return cursor.lastrowid


@pytest.fixture
def conn():
    return create_in_memory_connection()


@pytest.fixture
def repo(conn):
    return SQLiteSlotRepository(conn)


def test_check_availability_returns_free_slot_in_window(conn, repo):
    _insert_slot(conn, "tenancy", "2026-09-03T09:00:00")
    result = repo.check_availability("2026-09-03", "morning", "tenancy")
    assert result is not None
    assert int(result["start_time"][11:13]) < 12


def test_check_availability_matches_exact_time_when_given(conn, repo):
    _insert_slot(conn, "tenancy", "2026-09-03T09:00:00")
    target_id = _insert_slot(conn, "tenancy", "2026-09-03T09:30:00")
    result = repo.check_availability("2026-09-03", "morning", "tenancy", exact_time="09:30")
    assert result is not None
    assert result["id"] == target_id


def test_check_availability_returns_none_for_taken_exact_time(conn, repo):
    slot_id = _insert_slot(conn, "tenancy", "2026-09-03T10:00:00")
    repo.book(slot_id)
    _insert_slot(conn, "tenancy", "2026-09-03T09:00:00")
    result = repo.check_availability("2026-09-03", "morning", "tenancy", exact_time="10:00")
    assert result is None


def test_check_availability_returns_none_when_fully_booked(conn, repo):
    slot_id = _insert_slot(conn, "tenancy", "2026-09-03T09:00:00")
    repo.book(slot_id)
    assert repo.check_availability("2026-09-03", "morning", "tenancy") is None


def test_suggest_alternatives_excludes_declined_ids(conn, repo):
    top_id = _insert_slot(conn, "tenancy", "2026-09-03T09:00:00")
    other_id = _insert_slot(conn, "tenancy", "2026-09-03T09:30:00")
    results = repo.suggest_alternatives("2026-09-03", "tenancy", exclude_ids=[top_id])
    ids = [r["id"] for r in results]
    assert top_id not in ids
    assert other_id in ids


def test_suggest_alternatives_returns_up_to_three_ordered_by_start_time(conn, repo):
    for hour in range(9, 15):
        _insert_slot(conn, "tenancy", f"2026-09-03T{hour:02d}:00:00")
    results = repo.suggest_alternatives("2026-09-03", "tenancy", exclude_ids=[])
    assert len(results) == 3
    start_times = [r["start_time"] for r in results]
    assert start_times == sorted(start_times)
    assert len(set(start_times)) == len(start_times)


def test_suggest_alternatives_with_no_declines_still_returns_free_slots(conn, repo):
    # regression: "id NOT IN (NULL)" (the old empty-exclude_ids fallback) is
    # never true in SQL, so this silently matched zero rows every time
    _insert_slot(conn, "tenancy", "2026-09-03T09:00:00")
    results = repo.suggest_alternatives("2026-09-03", "tenancy", exclude_ids=[])
    assert len(results) == 1


def test_book_marks_slot_booked(conn, repo):
    slot_id = _insert_slot(conn, "tenancy", "2026-09-03T09:00:00")
    repo.book(slot_id)
    row = conn.execute("SELECT is_booked FROM slots WHERE id = ?", (slot_id,)).fetchone()
    assert row[0] == 1


def test_book_raises_on_already_booked_slot(conn, repo):
    slot_id = _insert_slot(conn, "tenancy", "2026-09-03T09:00:00")
    repo.book(slot_id)
    with pytest.raises(SlotAlreadyBookedError):
        repo.book(slot_id)
    row = conn.execute("SELECT is_booked FROM slots WHERE id = ?", (slot_id,)).fetchone()
    assert row[0] == 1
