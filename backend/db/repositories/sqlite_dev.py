import sqlite3

from backend.db.repositories.base import DevRepository
from backend.db.repositories.connection import SCHEMA_TABLES


class SQLiteDevRepository(DevRepository):
    """Read-only raw-table access for the admin DB viewer. Table names are
    always checked against SCHEMA_TABLES before being interpolated into SQL —
    never accept a table name from the request without that check."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def list_tables(self) -> list[str]:
        return list(SCHEMA_TABLES)

    def get_table(self, table: str, *, limit: int = 100, offset: int = 0) -> dict:
        if table not in SCHEMA_TABLES:
            raise ValueError(f"unknown table: {table!r}")

        total = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        cursor = self._conn.execute(f"SELECT * FROM {table} LIMIT ? OFFSET ?", (limit, offset))
        columns = [description[0] for description in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return {"table": table, "columns": columns, "rows": rows, "total": total, "limit": limit, "offset": offset}
