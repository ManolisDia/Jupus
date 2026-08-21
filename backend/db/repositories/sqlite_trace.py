import json
import sqlite3
from typing import Optional

from backend.db.repositories.base import TraceRepository
from backend.utils import now_iso


class SQLiteTraceRepository(TraceRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._seq_counters: dict[str, int] = {}

    def record_event(self, call_id: str, event_type: str, node: Optional[str] = None, **payload) -> None:
        seq = self._seq_counters.get(call_id, 0)
        self._seq_counters[call_id] = seq + 1
        self._conn.execute(
            "INSERT INTO trace_events (call_id, seq, ts, event_type, node, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (call_id, seq, now_iso(), event_type, node, json.dumps(payload)),
        )
        self._conn.commit()

    def get_trace(self, call_id: str) -> list[dict]:
        cursor = self._conn.execute(
            "SELECT id, call_id, seq, ts, event_type, node, payload_json "
            "FROM trace_events WHERE call_id = ? ORDER BY seq",
            (call_id,),
        )
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
