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
    stage: Literal["greeting", "routing", "capture", "research", "booking", "escalation", "ended"]
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
    # Set when the caller's exact requested slot wasn't available and up to
    # three nearest alternatives were offered instead (node_booking's
    # _offer_alternatives) — the full slot dicts, not just ids, so the next
    # turn can hand them straight to select_offered_slot without a DB
    # round-trip. None whenever no offer is outstanding (including after the
    # caller picks one, declines all of them, or an exact-match single slot
    # is proposed instead via proposed_slot_id/_propose_slot).
    offered_slots: Optional[list[dict]]
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
    # Phase 7 (optimistic capture) — raw utterance of the most recent FAILED
    # attempt at a field, keyed by field name, so the next attempt can be
    # read together with it rather than in isolation. A caller spelling out
    # a long value routinely splits it across two turns ("manos44" … "at
    # gmail dot com"), and the transport is deliberately forbidden from
    # merging consecutive utterances (backend/transport/prompts.py rule 3a),
    # so without this each attempt extracts an incurable fragment and the
    # re-ask loops forever — confirmed live, see docs/fixes/.
    # Only ever populated for "email"/"phone": those have a deterministic
    # validator to catch a bad stitch, and are the only fields long enough
    # to get split in the first place. Dropped for a field as soon as it is
    # captured, so a later re-ask never stitches onto stale text.
    partial_field_utterances: dict[str, str]
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
    # Phase 8 (case research) — only meaningful while stage == "research".
    # "gather": node_research_gather is either asking the filler follow-up
    # (having just spawned the background search) or, on the very first
    # turn of this stage, has already been asked by node_capture_confirm's
    # own transition and is waiting for the caller's answer. "deliver": the
    # next turn checks the (usually-resolved) background search result and
    # speaks the citation, or nothing, before moving to booking.
    research_phase: Literal["gather", "deliver"]
    # Set once the background statute search resolves — found, not found,
    # or failed all collapse to None here (Decision 4,
    # docs/phases/phase-8-legal-research.md). None while still pending or
    # if research never ran for this call at all (e.g. escalated earlier).
    statute_citation: Optional[dict]
    # Transient — set ONLY by node_research_gather, popped by dispatcher.py
    # right after GRAPH.invoke returns, same pattern as background_verify_field.
    # Never left set across turns.
    background_search_query: Optional[str]


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
        offered_slots=None,
        capture_phase="fast",
        last_asked_field=None,
        verification_failed_field=None,
        background_verify_field=None,
        partial_field_utterances={},
        research_phase="gather",
        statute_citation=None,
        background_search_query=None,
    )


CALL_STATES: dict[str, CallState] = {}


def get_or_create_state(call_id: str) -> CallState:
    if call_id not in CALL_STATES:
        CALL_STATES[call_id] = new_call_state(call_id)
    return CALL_STATES[call_id]
