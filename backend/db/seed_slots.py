from backend.config import settings
from backend.db.repositories.connection import connect, reset_schema
from backend.db.repositories.sqlite_slots import SQLiteSlotRepository

AREAS = ["employment", "tenancy", "immigration"]
BUSINESS_DAYS = 10


def seed(conn) -> None:
    reset_schema(conn)
    SQLiteSlotRepository(conn).seed(AREAS, BUSINESS_DAYS)


if __name__ == "__main__":
    connection = connect(settings.db_path)
    seed(connection)
    print(f"Seeded {AREAS} slots for {BUSINESS_DAYS} business days into {settings.db_path}")
