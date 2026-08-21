from pathlib import Path

from backend.config import settings
from backend.db.repositories.connection import connect
from backend.db.repositories.sqlite_slots import SQLiteSlotRepository

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
AREAS = ["employment", "tenancy", "immigration"]
BUSINESS_DAYS = 10


def seed(conn) -> None:
    for table in ("trace_events", "eval_flags", "calls", "slots"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA_PATH.read_text())
    SQLiteSlotRepository(conn).seed(AREAS, BUSINESS_DAYS)


if __name__ == "__main__":
    connection = connect(settings.db_path)
    seed(connection)
    print(f"Seeded {AREAS} slots for {BUSINESS_DAYS} business days into {settings.db_path}")
