import json
import sqlite3
from typing import Optional

from backend.db.repositories.base import CallRepository
from backend.supervisor.state import CallState
from backend.utils import now_iso


def _derive_outcome(state: CallState) -> Optional[str]:
    if state["stage"] != "ended":
        return None
    if state.get("escalation_reason"):
        return "escalated"
    if state.get("booking_confirmed"):
        return "booked"
    return "info_only"


class SQLiteCallRepository(CallRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def upsert(self, state: CallState, outcome_override: Optional[str] = None) -> None:
        outcome = outcome_override if outcome_override is not None else _derive_outcome(state)
        ended_at = now_iso() if state["stage"] == "ended" else None
        caller_profile = state["caller_profile"]

        self._conn.execute(
            """
            INSERT INTO calls (
                call_id, started_at, ended_at, practice_area, outcome,
                escalation_reason, caller_name, caller_email, caller_phone,
                booking_slot_id, transcript_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(call_id) DO UPDATE SET
                ended_at = excluded.ended_at,
                practice_area = excluded.practice_area,
                outcome = excluded.outcome,
                escalation_reason = excluded.escalation_reason,
                caller_name = excluded.caller_name,
                caller_email = excluded.caller_email,
                caller_phone = excluded.caller_phone,
                booking_slot_id = excluded.booking_slot_id,
                transcript_json = excluded.transcript_json
            """,
            (
                state["call_id"],
                now_iso(),
                ended_at,
                state["practice_area"],
                outcome,
                state["escalation_reason"],
                caller_profile["name"],
                caller_profile["email"],
                caller_profile["phone"],
                None,
                json.dumps(state["transcript"]),
            ),
        )
        self._conn.commit()

    def get(self, call_id: str) -> Optional[dict]:
        cursor = self._conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [description[0] for description in cursor.description]
        return dict(zip(columns, row))

    def list(self, *, with_outcome_only: bool = False, reviewed: Optional[bool] = None) -> list[dict]:
        if reviewed is not None:
            raise NotImplementedError(
                "filtering by reviewed status requires human_annotations, introduced in Phase 6c"
            )
        query = "SELECT * FROM calls"
        if with_outcome_only:
            query += " WHERE outcome IS NOT NULL"
        cursor = self._conn.execute(query)
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
