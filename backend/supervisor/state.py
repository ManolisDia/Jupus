import operator
from typing import Annotated, Literal, Optional, TypedDict


class CallerProfile(TypedDict):
    name: Optional[str]
    name_confidence: float
    email: Optional[str]
    email_confidence: float
    email_validated: bool
    phone: Optional[str]
    phone_confidence: float
    phone_validated: bool
    preferred_slot: Optional[str]


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


def new_call_state(call_id: str) -> CallState:
    return CallState(
        call_id=call_id,
        stage="greeting",
        practice_area=None,
        caller_profile=CallerProfile(
            name=None,
            name_confidence=0.0,
            email=None,
            email_confidence=0.0,
            email_validated=False,
            phone=None,
            phone_confidence=0.0,
            phone_validated=False,
            preferred_slot=None,
        ),
        transcript=[],
        retry_counts={},
        escalation_reason=None,
        booking_confirmed=False,
        pending_reply=None,
    )


CALL_STATES: dict[str, CallState] = {}


def get_or_create_state(call_id: str) -> CallState:
    if call_id not in CALL_STATES:
        CALL_STATES[call_id] = new_call_state(call_id)
    return CALL_STATES[call_id]
