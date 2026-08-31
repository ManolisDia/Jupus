from backend.db.repositories.connection import ensure_schema, reset_schema
from backend.db.repositories.testing import (
    create_bare_connection,
    create_connection_predating,
    table_names,
)


def test_ensure_schema_adds_a_missing_table_without_dropping_existing_data():
    conn = create_connection_predating("escalations")
    conn.execute("INSERT INTO calls (call_id, outcome) VALUES ('c1', 'escalated')")
    conn.commit()
    assert "escalations" not in table_names(conn)

    ensure_schema(conn)

    assert "escalations" in table_names(conn)
    # The whole reason this isn't just reset_schema: real logged calls survive.
    assert conn.execute("SELECT count(*) FROM calls WHERE call_id = 'c1'").fetchone()[0] == 1


def test_ensure_schema_is_idempotent():
    conn = create_bare_connection()
    ensure_schema(conn)
    tables = table_names(conn)
    conn.execute("INSERT INTO calls (call_id, outcome) VALUES ('c1', 'booked')")
    conn.commit()

    ensure_schema(conn)

    assert table_names(conn) == tables
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 1


def test_reset_schema_still_drops_everything():
    # ensure_schema is additive; reset_schema remains the deliberate wipe
    # that seed_slots.py relies on.
    conn = create_bare_connection()
    ensure_schema(conn)
    conn.execute("INSERT INTO calls (call_id, outcome) VALUES ('c1', 'booked')")
    conn.commit()

    reset_schema(conn)

    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    assert "escalations" in table_names(conn)
