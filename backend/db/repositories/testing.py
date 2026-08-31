import sqlite3

from backend.db.repositories.connection import SCHEMA_PATH, reset_schema


def create_bare_connection() -> sqlite3.Connection:
    """An in-memory connection with no schema applied — for tests exercising
    schema creation itself rather than running against it."""
    # check_same_thread=False: matches backend.db.repositories.connection.connect
    # (production connections are also shared across the async event loop's
    # worker threads) — some tests exercise this connection through FastAPI's
    # TestClient, which runs the app in a different thread via anyio's portal.
    return sqlite3.connect(":memory:", check_same_thread=False)


def create_in_memory_connection() -> sqlite3.Connection:
    conn = create_bare_connection()
    reset_schema(conn)
    return conn


def create_connection_predating(table: str) -> sqlite3.Connection:
    """An in-memory database built from schema.sql as it read BEFORE `table`
    was added to the end of it — what an already-in-use calendar.db looks
    like the first time someone pulls a change that adds a table. Lets
    ensure_schema's additive behaviour be tested without checking a stale
    fixture .db into the repo."""
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    older_schema, separator, _ = SCHEMA_PATH.read_text().partition(marker)
    if not separator:
        raise ValueError(f"{table!r} is not defined in schema.sql")
    conn = create_bare_connection()
    conn.executescript(older_schema)
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
