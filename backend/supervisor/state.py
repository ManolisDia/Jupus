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
    )


CALL_STATES: dict[str, CallState] = {}


def get_or_create_state(call_id: str) -> CallState:
    if call_id not in CALL_STATES:
        CALL_STATES[call_id] = new_call_state(call_id)
    return CALL_STATES[call_id]
