import sqlite3
from typing import Optional

from backend.db.repositories.base import EscalationRepository
from backend.supervisor.state import CallState, confirmed_value
from backend.utils import now_iso


class SQLiteEscalationRepository(EscalationRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def record(
        self,
        state: CallState,
        *,
        reason_for_call: Optional[str],
        escalation_explanation: Optional[str],
    ) -> None:
        caller_profile = state["caller_profile"]
        # Upsert rather than a plain INSERT: a call can only reach the
        # escalation node once (it ends the call), but dispatcher.py's
        # unhandled-exception catch-all writes a record too, and a failure
        # late enough in the same turn could plausibly follow one. Second
        # write wins — the later record is the more complete story of why
        # the call ended up with a human — instead of a PK violation
        # crashing the fallback path that exists precisely to not crash.
        self._conn.execute(
            """
            INSERT INTO escalations (
                call_id, escalated_at, escalation_reason, reason_for_call,
                escalation_explanation, practice_area, caller_name,
                caller_email, caller_phone
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(call_id) DO UPDATE SET
                escalated_at = excluded.escalated_at,
                escalation_reason = excluded.escalation_reason,
                reason_for_call = excluded.reason_for_call,
                escalation_explanation = excluded.escalation_explanation,
                practice_area = excluded.practice_area,
                caller_name = excluded.caller_name,
                caller_email = excluded.caller_email,
                caller_phone = excluded.caller_phone
            """,
            (
                state["call_id"],
                now_iso(),
                state.get("escalation_reason"),
                reason_for_call,
                escalation_explanation,
                state.get("practice_area"),
                confirmed_value(caller_profile, "name"),
                confirmed_value(caller_profile, "email"),
                confirmed_value(caller_profile, "phone"),
            ),
        )
        self._conn.commit()

    def get(self, call_id: str) -> Optional[dict]:
        cursor = self._conn.execute("SELECT * FROM escalations WHERE call_id = ?", (call_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [description[0] for description in cursor.description]
        return dict(zip(columns, row))

    def list(self) -> list[dict]:
        cursor = self._conn.execute("SELECT * FROM escalations ORDER BY escalated_at DESC")
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
