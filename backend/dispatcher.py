import asyncio
import logging
import time

from fastapi import WebSocket

from backend.db.repositories import Repositories
from backend.supervisor import tools
from backend.supervisor.faq import match_faq
from backend.supervisor.graph import GRAPH, apply_extraction
from backend.supervisor.heuristics import is_explicit_human_request
from backend.supervisor.llm_utils import LLMCallFailed, call_claude_tool
from backend.supervisor.state import CALL_STATES, FIELD_PRIORITY, CallState, get_or_create_state
from backend.supervisor.tracing import traced_call
from backend.utils import now_iso

logger = logging.getLogger(__name__)

LOCKS: dict[str, asyncio.Lock] = {}
SPEAKING: dict[str, bool] = {}
DEFERRED: dict[str, list[tuple[str, str, str, float]]] = {}
CONNECTIONS: dict[str, WebSocket] = {}
# Phase 7 (optimistic capture) — (call_id, field_name) -> the background
# task doing that field's REAL extraction/validation while node_capture_fast
# has already moved on. Deliberately a plain result-returning task, never
# touching CALL_STATES/the per-call lock itself — see
# _verify_field_in_background's docstring for why (avoiding a deadlock
# against a turn that might be awaiting this same task while holding the
# lock).
FIELD_VERIFICATIONS: dict[tuple[str, str], asyncio.Task] = {}
# Phase 8 (case research) — call_id -> the background statute-search task
# spawned off node_research_gather's background_search_query signal. Only
# one in flight at a time per call (research runs once per call). Same
# "never touches CALL_STATES/the per-call lock" shape as
# _verify_field_in_background, for the same deadlock-avoidance reason.
STATUTE_SEARCHES: dict[str, asyncio.Task] = {}


def get_lock(call_id: str) -> asyncio.Lock:
    return LOCKS.setdefault(call_id, asyncio.Lock())


def derive_outcome_label(state: CallState) -> str:
    if state.get("escalation_reason"):
        return "escalated"
    if state.get("booking_confirmed"):
        return "booked"
    return "info_only"


async def on_bridge_message(repos: Repositories, call_id: str, msg: dict) -> None:
    msg_type = msg.get("type")
    if msg_type == "ask_supervisor":
        asyncio.create_task(
            process_supervisor_call(repos, call_id, msg["tool_call_id"], msg["last_caller_utterance"])
        )
    elif msg_type == "speech_started":
        SPEAKING[call_id] = True
    elif msg_type == "speech_stopped":
        SPEAKING[call_id] = False
        drain_deferred(repos, call_id)
    else:
        logger.warning("unknown /bridge message type=%r call_id=%s", msg_type, call_id)


async def _verify_field_in_background(repos: Repositories, call_id: str, field: str, utterance: str) -> dict:
    """Phase 7 (optimistic capture) — one field's real extraction/validation,
    run fully in the background while node_capture_fast has already moved
    on to asking about the next one. Mirrors node_capture's own "extract a
    new field" branch (backend/supervisor/graph.py) but is kept as its own
    copy rather than sharing code with it, to avoid touching that
    well-tested, synchronous-path function's control flow for this.

    Deliberately never touches CALL_STATES or acquires get_lock(call_id) —
    process_supervisor_call may be AWAITING this very task while it already
    holds that lock (see the blocking-wait case in process_supervisor_call
    below); if this function also needed the lock to write its result,
    that would deadlock. Instead it's a pure computation that returns its
    result, and only the lock-holding turn processing (via
    _reconcile_field_verifications) ever writes it into shared state.

    On failure, deliberately does NOT replicate node_capture's attempts/
    escalation bookkeeping — a background failure was never spoken to the
    caller, so it isn't a real "attempt" in the sense retry_counts and the
    3-strikes escalation care about. The real attempt only happens once the
    foreground actually re-processes this field for real, via
    node_capture_fast's urgent-reask fallback (_fallback_to_real_capture),
    which reuses node_capture's existing, unchanged attempts/escalation
    logic.
    """
    try:
        extracted = await asyncio.to_thread(
            call_claude_tool, repos.trace, call_id, "capture_fast_background", "extract_field",
            tools.extract_field, utterance, field,
        )
    except LLMCallFailed:
        return {"field": field, "success": False}

    if field in ("email", "phone"):
        candidate = extracted["value"] or None
        validator = tools.validate_email if field == "email" else tools.validate_phone
        valid = candidate is not None and await asyncio.to_thread(
            traced_call, repos.trace, call_id, "capture_fast_background",
            "validate_email" if field == "email" else "validate_phone", validator, candidate,
        )
        if not valid:
            return {"field": field, "success": False}
        return {
            "field": field, "success": True,
            "value": candidate, "confidence": extracted["confidence"], "status": "pending_confirm",
        }

    value, status = apply_extraction(field, extracted["value"], extracted["confidence"])
    if status == "missing":
        return {"field": field, "success": False}
    return {"field": field, "success": True, "value": value, "confidence": extracted["confidence"], "status": status}


def _reconcile_field_verifications(state: CallState, call_id: str) -> None:
    """Non-blocking: merge any already-finished background verification
    results into state['caller_profile'], and set/clear
    state['verification_failed_field'] for whichever field just failed (if
    any). Always safe to call while holding get_lock(call_id) — never
    awaits anything, just inspects already-completed Task objects and pops
    them out of FIELD_VERIFICATIONS.

    A failure is flagged regardless of whether the failed field is the
    CURRENT last_asked_field — by construction it never can be. A field
    only gets a background check once node_capture_fast has already
    advanced last_asked_field past it (the check is spawned in the same
    return that produces the NEXT field's question), so by the time that
    check can possibly resolve, last_asked_field always already points at
    a later field. Gating this on field == last_asked_field (an earlier
    version of this function did) meant the signal could effectively never
    fire for real — confirmed live: a caller's later utterance got silently
    misattributed to re-extract an unrelated, already-passed field once
    node_capture's generic "first missing field" logic eventually reached
    it. See docs/fixes/ for the write-up and node_capture_fast's handling
    of this field for the corrected interrupt-and-reask behavior.
    """
    state["verification_failed_field"] = None
    done_keys = [key for key in FIELD_VERIFICATIONS if key[0] == call_id and FIELD_VERIFICATIONS[key].done()]
    failed_fields = []
    for key in done_keys:
        task = FIELD_VERIFICATIONS.pop(key)
        field = key[1]
        try:
            result = task.result()
        except Exception:
            logger.exception("background field verification crashed call_id=%s field=%s", call_id, field)
            result = {"field": field, "success": False}
        if result["success"]:
            profile = state["caller_profile"]
            state["caller_profile"] = {
                **profile,
                field: {**profile[field], "value": result["value"], "status": result["status"]},
            }
        else:
            failed_fields.append(field)
    if failed_fields:
        # Deterministic if more than one resolved to failure in the same
        # reconcile pass (rare) — always surface the earliest in
        # FIELD_PRIORITY order first, matching the canonical drain order
        # used everywhere else in this design.
        state["verification_failed_field"] = min(failed_fields, key=FIELD_PRIORITY.index)


async def _search_statutes_in_background(repos: Repositories, call_id: str, area: str, utterance: str) -> dict | None:
    """Phase 8 (case research) — the background half of the research node's
    latency-hiding pattern: BM25-search the area's corpus, and only if the
    top candidate clears BM25_RELEVANCE_FLOOR, spend one Claude call
    grounding a citation against that closed candidate set. Returns None
    (never raises) for "nothing relevant," "search failed," and "grounding
    call failed" alike — a failed or empty search degrades silently, it
    never escalates the call (Decision 4,
    docs/phases/phase-8-legal-research.md). Deliberately never touches
    CALL_STATES or acquires get_lock(call_id), same reasoning as
    _verify_field_in_background above.
    """
    candidates = await asyncio.to_thread(
        traced_call, repos.trace, call_id, "research", "search_statute_candidates",
        tools.search_statute_candidates, area, utterance,
    )
    if not candidates or candidates[0]["score"] < tools.BM25_RELEVANCE_FLOOR:
        return None
    try:
        grounded = await asyncio.to_thread(
            call_claude_tool, repos.trace, call_id, "research", "ground_statute_citation",
            tools.ground_statute_citation, utterance, candidates,
        )
    except LLMCallFailed:
        return None
    candidate_ids = {c["id"] for c in candidates}
    selected_id = grounded.get("selected_id")
    if not selected_id or selected_id not in candidate_ids:
        # Defensive guard (Decision 3): never trust a returned id that
        # isn't actually one of the candidates offered, even though the
        # prompt already forbids it.
        return None
    entry = next(c for c in candidates if c["id"] == selected_id)
    return {"citation": entry["citation"], "text": entry["text"], "spoken_framing": grounded["spoken_framing"]}


def _reconcile_statute_search(state: CallState, call_id: str) -> None:
    """Non-blocking, mirrors _reconcile_field_verifications: merges an
    already-finished background search result into state['statute_citation']
    if one is ready. Never awaits an in-flight task — if it isn't done yet
    by the time node_research_deliver runs, state['statute_citation'] simply
    stays whatever it already was (None by default), which that node
    already treats as "nothing to say" (Decision 4)."""
    task = STATUTE_SEARCHES.get(call_id)
    if task is not None and task.done():
        STATUTE_SEARCHES.pop(call_id, None)
        try:
            state["statute_citation"] = task.result()
        except Exception:
            logger.exception("background statute search crashed call_id=%s", call_id)
            state["statute_citation"] = None


def _reconcile_before_capture_turn(state: CallState, call_id: str) -> None:
    """Called immediately before invoking GRAPH.invoke for every turn
    (cheap no-op — FIELD_VERIFICATIONS is empty — outside stage ==
    "capture"). Always non-blocking: no field in FIELD_PRIORITY ever needs
    a real wait here. Every field except the last one gets a genuine
    background head start (spawned when node_capture_fast advances past
    it, resolved well before drain time in practice); the last field never
    gets a background task spawned for it at all — graph.py's
    _finish_fast_pass processes it live instead, since there's no further
    "ask the next field" turn to run concurrently with it. See
    docs/phases/phase-7-optimistic-capture.md."""
    _reconcile_field_verifications(state, call_id)


async def process_supervisor_call(repos: Repositories, call_id: str, tool_call_id: str, utterance: str) -> None:
    try:
        async with get_lock(call_id):
            state = get_or_create_state(call_id)
            if state["stage"] == "ended":
                deliver_or_defer(repos, call_id, tool_call_id, "This call has already been completed.", "ended")
                return
            state["transcript"] = state["transcript"] + [{"role": "caller", "text": utterance, "ts": now_iso()}]
            if is_explicit_human_request(utterance):
                state["stage"] = "escalation"
                state["escalation_reason"] = "explicit_request"
            # Phase 7 (optimistic capture): fold in any finished background
            # field verification BEFORE the graph runs, so node_capture_fast/
            # node_capture see real, up-to-date profile data rather than
            # stale defaults. Always non-blocking — see
            # _reconcile_before_capture_turn's docstring for why no field
            # ever needs a real wait here.
            _reconcile_before_capture_turn(state, call_id)
            # Phase 8 (case research): fold in a finished background statute
            # search BEFORE the graph runs, so node_research_deliver sees a
            # real, up-to-date result rather than a stale None. Always
            # non-blocking, same shape as the capture-verification
            # reconciliation above — see _reconcile_statute_search's
            # docstring for why no call ever needs a real wait here.
            _reconcile_statute_search(state, call_id)
            stage_before = state["stage"]
            # GRAPH.invoke is synchronous and its node functions make real
            # blocking Claude/Anthropic SDK calls with no internal await —
            # run it off the event loop via asyncio.to_thread so the /bridge
            # WebSocket can keep receiving speech_started/speech_stopped VAD
            # events (and other calls' messages) while a tool call is in
            # flight. Without this, the event loop is fully frozen for the
            # duration of every real Claude call, and deliver_or_defer's
            # SPEAKING check always sees stale state — the deferred-reply
            # path (see deliver_or_defer/drain_deferred below) never
            # actually triggers against real timing, only in tests that set
            # SPEAKING directly.
            updated = await asyncio.to_thread(
                GRAPH.invoke, state, config={"configurable": {"repos": repos}}
            )
            if stage_before == "greeting" and updated["stage"] not in ("ended", "escalation"):
                # node_greeting is a silent, content-blind stub (it only bumps
                # the stage) — the caller's first real utterance is already in
                # this turn's transcript and would otherwise sit unprocessed
                # until the caller spoke again. Chain straight into the next
                # node now, within the same dispatch, rather than treating the
                # greeting stage-bump as a turn worth replying to on its own.
                updated = await asyncio.to_thread(
                    GRAPH.invoke, updated, config={"configurable": {"repos": repos}}
                )
            # Phase 7 (optimistic capture): node_capture_fast's own "advance
            # to the next field" branch is the ONLY place a background
            # verification should be spawned — signaled explicitly via
            # state["background_verify_field"], not inferred from a
            # last_asked_field before/after diff. A diff-based check is a
            # real trap here: _fallback_to_real_capture's paths ALSO change
            # last_asked_field (e.g. resolving a pending confirmation and
            # moving to the next already-known field), but that utterance
            # was already fully, synchronously processed — spawning a
            # redundant background task for it too would use THIS turn's
            # utterance a second time, and its eventual result could land on
            # a LATER turn and silently overwrite an already-correct value
            # with something extracted from unrelated text (confirmed live:
            # this happened — a stray "name" verification picked up email's
            # mock on a later turn purely because both patch the same global
            # tools.extract_field attribute; see docs/fixes/).
            #
            # Reset to None afterward rather than popped/deleted — this is a
            # real declared CallState field (LangGraph silently drops keys
            # outside its schema, and merge semantics for an entirely
            # missing key vs. one explicitly reset to None aren't worth
            # relying on).
            if verify_field := updated.get("background_verify_field"):
                FIELD_VERIFICATIONS[(call_id, verify_field)] = asyncio.create_task(
                    _verify_field_in_background(repos, call_id, verify_field, utterance)
                )
                updated["background_verify_field"] = None
            # Phase 8 (case research): node_research_gather's own "spawn the
            # search, ask the filler question" branch is the only place a
            # background statute search should be spawned — signaled
            # explicitly via state["background_search_query"], same reason
            # background_verify_field is signal-based rather than diffed
            # (see the comment above).
            if query := updated.get("background_search_query"):
                STATUTE_SEARCHES[call_id] = asyncio.create_task(
                    _search_statutes_in_background(repos, call_id, updated["practice_area"], query)
                )
                updated["background_search_query"] = None
            # Tag the deferred reply with the stage it resulted in, not the
            # stage it started from — a node that naturally advances the
            # stage (e.g. greeting -> routing) must not have its own reply
            # dropped as stale later just because the stage moved; staleness
            # should only fire when a LATER, separately-dispatched turn has
            # since moved the conversation past this reply's own result.
            dispatch_stage = updated["stage"]
            # Checked unconditionally against the caller's raw utterance for
            # this turn, regardless of whether the node's own logic succeeded
            # or failed — a caller can tack a genuine side-question ("...and
            # are you open weekends?") onto an otherwise-successful utterance,
            # and nothing node-specific (classify_practice_area, extract_field,
            # etc.) ever looks at anything but the part it cares about. This
            # is the one place every turn's reply passes through, so it's the
            # one place that can catch a tangent no matter which node ran.
            faq_answer = match_faq(utterance)
            if faq_answer and updated.get("pending_reply"):
                updated["pending_reply"] = f"{faq_answer} {updated['pending_reply']}"
            CALL_STATES[call_id] = updated
            repos.calls.upsert(updated)
            if updated["stage"] == "ended":
                repos.trace.record_event(call_id, "call_ended", outcome=derive_outcome_label(updated))
            broadcast_call_state(call_id)
    except Exception as e:
        logger.exception("unhandled error processing call_id=%s", call_id)
        repos.trace.record_event(call_id, "unhandled_error", error=str(e))
        state = CALL_STATES.get(call_id) or get_or_create_state(call_id)
        state["stage"] = "ended"
        state["escalation_reason"] = "system_error"
        CALL_STATES[call_id] = state
        repos.calls.upsert(state)
        traced_call(
            repos.trace, call_id, "dispatcher", "write_minimal_handoff_note",
            tools.write_minimal_handoff_note, call_id, state, f"Unhandled error: {e}",
        )
        broadcast_call_state(call_id)
        deliver_or_defer(
            repos, call_id, tool_call_id,
            "Sorry, something went wrong on my end — let me get you to someone who can help.",
            "escalation",
        )
        return
    deliver_or_defer(repos, call_id, tool_call_id, updated["pending_reply"], dispatch_stage)


def deliver_or_defer(repos: Repositories, call_id: str, tool_call_id: str, reply: str, dispatch_stage: str) -> None:
    if SPEAKING.get(call_id, False):
        DEFERRED.setdefault(call_id, []).append((tool_call_id, reply, dispatch_stage, time.monotonic()))
        repos.trace.record_event(call_id, "reply_deferred", tool_call_id=tool_call_id, reason="caller_speaking")
    else:
        send_over_bridge(call_id, tool_call_id, reply)
        repos.trace.record_event(
            call_id, "reply_delivered", tool_call_id=tool_call_id, reply=reply, was_deferred=False, wait_ms=0
        )


def drain_deferred(repos: Repositories, call_id: str) -> None:
    items = DEFERRED.pop(call_id, [])
    current_stage = CALL_STATES.get(call_id, {}).get("stage")
    for tool_call_id, reply, dispatch_stage, queued_at in items:
        if dispatch_stage != current_stage:
            logger.debug("dropping stale deferred reply call_id=%s tool_call_id=%s", call_id, tool_call_id)
            repos.trace.record_event(
                call_id, "reply_dropped_stale", tool_call_id=tool_call_id,
                dispatch_stage=dispatch_stage, current_stage=current_stage,
            )
            continue
        send_over_bridge(call_id, tool_call_id, reply)
        repos.trace.record_event(
            call_id, "reply_delivered", tool_call_id=tool_call_id, reply=reply,
            was_deferred=True, wait_ms=int((time.monotonic() - queued_at) * 1000),
        )


async def _send_json_safely(ws: WebSocket, payload: dict, call_id: str) -> None:
    try:
        await ws.send_json(payload)
    except Exception:
        logger.warning("failed to send supervisor_result to call_id=%s (connection likely closed)", call_id)


def send_over_bridge(call_id: str, tool_call_id: str, reply: str) -> None:
    ws = CONNECTIONS.get(call_id)
    if ws is None:
        logger.warning("no active /bridge connection for call_id=%s — dropping reply", call_id)
        return
    asyncio.create_task(
        _send_json_safely(ws, {"type": "supervisor_result", "tool_call_id": tool_call_id, "reply": reply}, call_id)
    )


def call_state_snapshot(state: CallState) -> dict:
    # A small, read-only projection of CallState — rendered by the
    # caller-facing client's "captured details" panel AND by the admin
    # Live Supervisor graph page's node sub-state badges (both Phase 7
    # stretches). Neither consumer writes it back; it's display-only. The
    # LangGraph node granularity itself doesn't change because of this —
    # field-by-field capture and slot proposal/decline are, by design
    # (CLAUDE.md rule #2), plain deterministic branches *inside* the single
    # "capture"/"booking" nodes, not additional graph nodes. This snapshot
    # just makes that already-existing internal state visible, it doesn't
    # add new state.
    profile = state["caller_profile"]
    return {
        "type": "call_state",
        "stage": state["stage"],
        "practice_area": state.get("practice_area"),
        "escalation_reason": state.get("escalation_reason"),
        "booking_confirmed": state.get("booking_confirmed", False),
        "caller_profile": {
            field: {
                "value": profile[field]["value"],
                "confidence": profile[field]["confidence"],
                "status": profile[field]["status"],
            }
            for field in ("name", "email", "phone")
        },
        "booking": {
            "proposed_slot_id": state.get("proposed_slot_id"),
            "declined_count": len(state.get("declined_slot_ids") or []),
            "requested_date": state.get("requested_date"),
            "requested_window": state.get("requested_window"),
        },
    }


def broadcast_call_state(call_id: str) -> None:
    ws = CONNECTIONS.get(call_id)
    if ws is None:
        return
    state = CALL_STATES.get(call_id)
    if state is None:
        return
    asyncio.create_task(_send_json_safely(ws, call_state_snapshot(state), call_id))


async def mark_call_abandoned(repos: Repositories, call_id: str) -> None:
    # Must hold the same per-call lock process_supervisor_call holds while
    # mutating CALL_STATES/writing the outcome — otherwise a disconnect
    # landing mid-turn (GRAPH.invoke can run for seconds) races the
    # in-flight turn: whichever of the two writes CALL_STATES[call_id] and
    # calls repos.calls.upsert() second silently wins, which can revert an
    # abandoned call back to "in progress" or overwrite a real outcome with
    # "abandoned" depending on timing. See docs/code-review-2026-08-24.md
    # finding #1.
    async with get_lock(call_id):
        state = CALL_STATES.get(call_id)
        if state and state["stage"] != "ended":
            state["stage"] = "ended"
            repos.calls.upsert(state, outcome_override="abandoned")
        repos.trace.record_event(call_id, "call_abandoned")
        CONNECTIONS.pop(call_id, None)
        SPEAKING.pop(call_id, None)
        DEFERRED.pop(call_id, None)
