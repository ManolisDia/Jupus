from dataclasses import dataclass
from typing import Optional

from backend.config import Settings
from backend.db.repositories.base import (
    AnnotationRepository,
    CallRepository,
    DevRepository,
    EscalationRepository,
    EvalRepository,
    SlotRepository,
    TraceRepository,
)
from backend.db.repositories.connection import connect
from backend.db.repositories.sqlite_annotations import SQLiteAnnotationRepository
from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.db.repositories.sqlite_dev import SQLiteDevRepository
from backend.db.repositories.sqlite_escalations import SQLiteEscalationRepository
from backend.db.repositories.sqlite_eval import SQLiteEvalRepository
from backend.db.repositories.sqlite_slots import SQLiteSlotRepository
from backend.db.repositories.sqlite_trace import SQLiteTraceRepository


@dataclass
class Repositories:
    calls: CallRepository
    slots: SlotRepository
    trace: TraceRepository
    # Optional purely so the many unit-test fixtures that construct a
    # Repositories for one narrow node don't all have to name every repo —
    # same as evals/annotations/dev below, and same as `slots=None`. Code
    # that needs it uses it unconditionally rather than skipping the write
    # when it's absent: a silently-unrecorded escalation is the exact
    # failure this table exists to prevent.
    escalations: Optional[EscalationRepository] = None
    evals: Optional[EvalRepository] = None
    annotations: Optional[AnnotationRepository] = None
    dev: Optional[DevRepository] = None


def get_repositories(settings: Settings) -> Repositories:
    if settings.db_backend == "sqlite":
        conn = connect(settings.db_path)
        return Repositories(
            calls=SQLiteCallRepository(conn),
            slots=SQLiteSlotRepository(conn),
            trace=SQLiteTraceRepository(conn),
            escalations=SQLiteEscalationRepository(conn),
            evals=SQLiteEvalRepository(conn),
            annotations=SQLiteAnnotationRepository(conn),
            dev=SQLiteDevRepository(conn),
        )
    raise NotImplementedError(
        f"db_backend={settings.db_backend!r} — implement Postgres*Repository "
        "classes against the same interfaces above and add a branch here. "
        "Nothing outside this file needs to change."
    )
