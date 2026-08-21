from typing import Optional

from backend.db.repositories.base import CallRepository, TraceRepository
from backend.supervisor.state import CallState


class FakeCallRepository(CallRepository):
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def upsert(self, state: CallState, outcome_override: Optional[str] = None) -> None:
        self.rows[state["call_id"]] = {
            "call_id": state["call_id"],
            "stage": state["stage"],
            "outcome": outcome_override,
            "escalation_reason": state["escalation_reason"],
            "ended_at": "stub-ts" if state["stage"] == "ended" else None,
        }

    def get(self, call_id: str) -> Optional[dict]:
        return self.rows.get(call_id)

    def list(self, *, with_outcome_only: bool = False, reviewed: Optional[bool] = None) -> list[dict]:
        rows = list(self.rows.values())
        if with_outcome_only:
            rows = [r for r in rows if r["outcome"] is not None]
        return rows


class FakeTraceRepository(TraceRepository):
    def __init__(self):
        self.events: list[dict] = []
        self._seq_counters: dict[str, int] = {}

    def record_event(self, call_id: str, event_type: str, node: Optional[str] = None, **payload) -> None:
        seq = self._seq_counters.get(call_id, 0)
        self._seq_counters[call_id] = seq + 1
        self.events.append(
            {"call_id": call_id, "seq": seq, "event_type": event_type, "node": node, "payload": payload}
        )

    def get_trace(self, call_id: str) -> list[dict]:
        return sorted((e for e in self.events if e["call_id"] == call_id), key=lambda e: e["seq"])
