from backend.db.repositories.testing import create_in_memory_connection
from backend.db.seed_slots import seed


def _count(conn, query, params=()):
    return conn.execute(query, params).fetchone()[0]


def test_seed_creates_expected_slot_count():
    conn = create_in_memory_connection()
    seed(conn)
    assert _count(conn, "SELECT COUNT(*) FROM slots") == 480


def test_seed_marks_expected_slots_pre_booked():
    conn = create_in_memory_connection()
    seed(conn)
    assert _count(conn, "SELECT COUNT(*) FROM slots WHERE is_booked = 1") == 6


def test_seed_is_idempotent():
    conn = create_in_memory_connection()
    seed(conn)
    seed(conn)
    assert _count(conn, "SELECT COUNT(*) FROM slots") == 480
    assert _count(conn, "SELECT COUNT(*) FROM slots WHERE is_booked = 1") == 6
