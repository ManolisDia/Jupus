import sqlite3
from dataclasses import dataclass

from backend.config import Settings
from backend.db.repositories.base import CallRepository, SlotRepository, TraceRepository
from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.db.repositories.sqlite_slots import SQLiteSlotRepository
from backend.db.repositories.sqlite_trace import SQLiteTraceRepository


@dataclass
class Repositories:
    calls: CallRepository
    slots: SlotRepository
    trace: TraceRepository


def get_repositories(settings: Settings) -> Repositories:
    if settings.db_backend == "sqlite":
        conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        return Repositories(
            calls=SQLiteCallRepository(conn),
            slots=SQLiteSlotRepository(conn),
            trace=SQLiteTraceRepository(conn),
        )
    raise NotImplementedError(
        f"db_backend={settings.db_backend!r} — implement Postgres*Repository "
        "classes against the same interfaces above and add a branch here. "
        "Nothing outside this file needs to change."
    )
