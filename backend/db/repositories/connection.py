import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema.sql"
SCHEMA_TABLES = ("trace_events", "eval_flags", "calls", "slots")


def connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, check_same_thread=False)


def reset_schema(conn: sqlite3.Connection) -> None:
    for table in SCHEMA_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA_PATH.read_text())
