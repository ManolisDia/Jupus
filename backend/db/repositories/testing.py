import sqlite3

from backend.db.repositories.connection import reset_schema


def create_in_memory_connection() -> sqlite3.Connection:
    # check_same_thread=False: matches backend.db.repositories.connection.connect
    # (production connections are also shared across the async event loop's
    # worker threads) — some tests exercise this connection through FastAPI's
    # TestClient, which runs the app in a different thread via anyio's portal.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    reset_schema(conn)
    return conn
