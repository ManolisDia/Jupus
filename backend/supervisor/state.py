import operator
from typing import Annotated, Literal, Optional, TypedDict


class FieldCapture(TypedDict):
    value: Optional[str]
    confidence: float
    status: Literal["missing", "pending_confirm", "confirmed"]
    attempts: int
    validated: bool


class CallerProfile(TypedDict):
    name: FieldCapture
    email: FieldCapture
    phone: FieldCapture


FIELD_PRIORITY: list[str] = ["name", "email", "phone"]


class CallState(TypedDict):
    call_id: str
    stage: Literal["greeting", "routing", "capture", "booking", "escalation", "ended"]
    practice_area: Optional[Literal["employment", "tenancy", "immigration"]]
    caller_profile: CallerProfile
    transcript: Annotated[list[dict], operator.add]
    retry_counts: dict[str, int]
    escalation_reason: Optional[str]
    booking_confirmed: bool
    pending_reply: Optional[str]
    consecutive_llm_failures: int
    proposed_slot_id: Optional[int]
    declined_slot_ids: Annotated[list[int], operator.add]
    requested_date: Optional[str]
    requested_window: Optional[str]
    # Phase 7 (optimistic capture) — only meaningful while stage == "capture".
    # "fast": node_capture_fast is asking through FIELD_PRIORITY optimistically,
    # zero Claude calls on the hot path. "confirm": the batched drain phase,
    # fully synchronous, reads exactly like today's node_capture.
    capture_phase: Literal["fast", "confirm"]
    # Which field's question is currently outstanding in the fast pass — the
    # field the caller's NEXT utterance is presumed to answer. None before
    # the first field has been asked about.
    last_asked_field: Optional[str]
    # Transient — set by dispatcher.py's reconciliation step immediately
    # before invoking GRAPH.invoke for a capture-stage turn, cleared every
    # turn. Signals that last_asked_field's real background verification
    # (see dispatcher.FIELD_VERIFICATIONS) came back a genuine failure, so
    # node_capture_fast should fall back to the real synchronous path this
    # turn rather than advancing further. Never set by any graph node.
    verification_failed_field: Optional[str]
    # Transient — set ONLY by node_capture_fast's own "advance to the next
    # field" branch, popped and consumed by dispatcher.py right after
    # GRAPH.invoke returns (never left set across turns). This must be a
    # real, declared CallState field, not an ad-hoc extra key on the
    # returned dict — LangGraph's merge silently drops any key not part of
    # this schema. Deliberately NOT inferred from a last_asked_field
    # before/after diff — see graph.py's node_capture_fast for why that's a
    # real bug trap (a fallback resolving a pending confirmation and moving
    # to the next already-known field also changes last_asked_field, but
    # that utterance was already fully processed and must not be
    # background-re-verified).
    background_verify_field: Optional[str]


def _new_field_capture() -> FieldCapture:
    return FieldCapture(value=None, confidence=0.0, status="missing", attempts=0, validated=True)


def new_call_state(call_id: str) -> CallState:
    return CallState(
        call_id=call_id,
        stage="greeting",
        practice_area=None,
        caller_profile=CallerProfile(
            name=_new_field_capture(),
            email=_new_field_capture(),
            phone=_new_field_capture(),
        ),
        transcript=[],
        retry_counts={},
        escalation_reason=None,
        booking_confirmed=False,
        pending_reply=None,
        consecutive_llm_failures=0,
        proposed_slot_id=None,
        declined_slot_ids=[],
        requested_date=None,
        requested_window=None,
        capture_phase="fast",
        last_asked_field=None,
        verification_failed_field=None,
        background_verify_field=None,
    )


CALL_STATES: dict[str, CallState] = {}


def get_or_create_state(call_id: str) -> CallState:
    if call_id not in CALL_STATES:
        CALL_STATES[call_id] = new_call_state(call_id)
    return CALL_STATES[call_id]
