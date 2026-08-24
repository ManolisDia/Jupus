from typing import Optional

from backend.db.repositories.base import (
    AnnotationRepository,
    CallRepository,
    EvalRepository,
    SlotRepository,
    TraceRepository,
)
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


class FakeEvalRepository(EvalRepository):
    def __init__(self):
        self.error_flags: dict[str, list[dict]] = {}
        self.eval_runs: dict[str, set[str]] = {}  # label -> {call_id}
        self.suggestions: list[dict] = []
        self._next_suggestion_id = 1

    def add_error_flags(self, call_id: str, flags: list[dict], eval_run_label: str) -> None:
        rows = self.error_flags.setdefault(call_id, [])
        for flag in flags:
            rows.append({**flag, "eval_run_label": eval_run_label})

    def get_error_flags(self, call_id: str) -> list[dict]:
        return self.error_flags.get(call_id, [])

    def tag_eval_run(self, call_id: str, eval_run_label: str, scenario_id: Optional[str] = None) -> None:
        self.eval_runs.setdefault(eval_run_label, set()).add(call_id)

    def call_ids_already_evaluated(self) -> set[str]:
        evaluated: set[str] = set()
        for call_ids in self.eval_runs.values():
            evaluated |= call_ids
        return evaluated

    def compute_error_rates(self, eval_run_label: str) -> dict[str, float]:
        from eval.error_classes import get_active_error_classes

        call_ids = self.eval_runs.get(eval_run_label, set())
        total = len(call_ids)
        rates = {}
        for cls in get_active_error_classes():
            if total == 0:
                rates[cls["id"]] = 0.0
                continue
            flagged = sum(
                1
                for call_id in call_ids
                if any(f["error_class_id"] == cls["id"] and f["eval_run_label"] == eval_run_label
                       for f in self.error_flags.get(call_id, []))
            )
            rates[cls["id"]] = flagged / total
        return rates

    def compute_error_rates_all(self) -> dict[str, float]:
        from eval.error_classes import get_active_error_classes

        all_call_ids: set[str] = set()
        for call_ids in self.eval_runs.values():
            all_call_ids |= call_ids
        total = len(all_call_ids)
        rates = {}
        for cls in get_active_error_classes():
            if total == 0:
                rates[cls["id"]] = 0.0
                continue
            flagged = sum(
                1
                for call_id in all_call_ids
                if any(f["error_class_id"] == cls["id"] for f in self.error_flags.get(call_id, []))
            )
            rates[cls["id"]] = flagged / total
        return rates

    def add_taxonomy_suggestions(self, suggestions: list[dict], eval_run_label: str) -> None:
        for suggestion in suggestions:
            self.suggestions.append(
                {**suggestion, "id": self._next_suggestion_id, "eval_run_label": eval_run_label, "status": "pending"}
            )
            self._next_suggestion_id += 1

    def update_suggestion_status(self, suggestion_id: int, status: str) -> None:
        for suggestion in self.suggestions:
            if suggestion["id"] == suggestion_id:
                suggestion["status"] = status

    def list_taxonomy_suggestions(self, eval_run_label: Optional[str], status: Optional[str]) -> list[dict]:
        rows = self.suggestions
        if eval_run_label is not None:
            rows = [s for s in rows if s["eval_run_label"] == eval_run_label]
        if status is not None:
            rows = [s for s in rows if s["status"] == status]
        return rows


class FakeAnnotationRepository(AnnotationRepository):
    def __init__(self):
        self.reviews: dict[str, dict] = {}

    def save_review(
        self,
        call_id: str,
        annotator: str,
        error_class_ids: list[str],
        uncategorized_notes: list[str],
        overall_note: str,
        is_gold: bool,
    ) -> None:
        annotations = [{"error_class_id": cid, "note": None} for cid in error_class_ids]
        annotations += [{"error_class_id": None, "note": note} for note in uncategorized_notes]
        self.reviews[call_id] = {
            "call_id": call_id,
            "annotator": annotator,
            "is_gold": int(is_gold),
            "overall_note": overall_note,
            "annotations": annotations,
        }

    def get_review(self, call_id: str) -> Optional[dict]:
        return self.reviews.get(call_id)

    def list_unreviewed(self) -> list[dict]:
        return []
