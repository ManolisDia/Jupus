import sqlite3

from backend.db.repositories.connection import reset_schema


def create_in_memory_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    reset_schema(conn)
    return conn
