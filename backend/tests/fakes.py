from typing import Optional

from backend.db.repositories.base import CallRepository, SlotRepository, TraceRepository
from backend.supervisor.state import CallState
from backend.utils import now_iso


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


class FakeSlotRepository(SlotRepository):
    def __init__(self):
        self.availability_result: Optional[dict] = None
        self.alternatives_result: list[dict] = []
        self.book_calls: list[int] = []
        self.book_side_effect: Optional[Exception] = None

    def check_availability(
        self,
        date: str,
        window: str,
        area: str,
        exact_time: Optional[str] = None,
        exclude_ids: Optional[list[int]] = None,
    ) -> Optional[dict]:
        return self.availability_result

    def suggest_alternatives(self, date: str, area: str, exclude_ids: list[int]) -> list[dict]:
        return self.alternatives_result

    def book(self, slot_id: int) -> int:
        self.book_calls.append(slot_id)
        if self.book_side_effect is not None:
            effect, self.book_side_effect = self.book_side_effect, None
            raise effect
        return slot_id

    def seed(self, areas: list[str], business_days: int) -> None:
        raise NotImplementedError("FakeSlotRepository does not support seeding")


class FakeTraceRepository(TraceRepository):
    def __init__(self):
        self.events: list[dict] = []
        self._seq_counters: dict[str, int] = {}

    def record_event(self, call_id: str, event_type: str, node: Optional[str] = None, **payload) -> None:
        seq = self._seq_counters.get(call_id, 0)
        self._seq_counters[call_id] = seq + 1
        self.events.append(
            {
                "call_id": call_id,
                "seq": seq,
                "ts": now_iso(),
                "event_type": event_type,
                "node": node,
                "payload": payload,
            }
        )

    def get_trace(self, call_id: str) -> list[dict]:
        return sorted((e for e in self.events if e["call_id"] == call_id), key=lambda e: e["seq"])
