import json
import sqlite3
import threading
from typing import Optional

from backend.db.repositories.base import TraceRepository
from backend.utils import now_iso


class SQLiteTraceRepository(TraceRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        # record_event is called from both the asyncio.to_thread worker
        # thread (via GRAPH.invoke's node functions) and the main
        # event-loop thread (e.g. drain_deferred) for the same call_id with
        # no other synchronization between them — an in-memory
        # _seq_counters dict with a non-atomic read-modify-write let two
        # threads read the same seq before either wrote back, producing
        # duplicate (call_id, seq) rows with no UNIQUE constraint to catch
        # it, corrupting ORDER BY seq. It also never survived a process
        # restart (uvicorn --reload resets it to empty mid-call). Fixed by
        # deriving seq directly from the DB's own MAX under a lock that
        # covers the read-then-insert as one atomic unit — no in-memory
        # cache to go stale or desync, see docs/code-review-2026-08-24.md
        # finding #2/#3.
        self._seq_lock = threading.Lock()

    def record_event(self, call_id: str, event_type: str, node: Optional[str] = None, **payload) -> None:
        with self._seq_lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM trace_events WHERE call_id = ?", (call_id,)
            ).fetchone()
            seq = row[0]
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
