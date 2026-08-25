import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema.sql"
SCHEMA_TABLES = (
    "escalations",
    "trace_events",
    "human_annotations",
    "call_reviews",
    "taxonomy_suggestions",
    "eval_runs",
    "call_error_flags",
    "calls",
    "slots",
)


def connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, check_same_thread=False)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create whatever schema.sql defines that this database doesn't have
    yet, leaving what it does have alone. Every statement in schema.sql is
    IF NOT EXISTS, so this is safe to run on every startup.

    There's no migration tool here on purpose (local-only SQLite, see
    docs/DECISIONS.md), which used to mean a new table could only reach an
    existing calendar.db via reset_schema — and that drops every logged
    call with it. Additive only: this creates missing tables and indexes,
    it does NOT add a column to a table that already exists. That still
    needs a deliberate migration."""
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def reset_schema(conn: sqlite3.Connection) -> None:
    for table in SCHEMA_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA_PATH.read_text())
