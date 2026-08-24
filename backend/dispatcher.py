import asyncio
import logging
import time

from fastapi import WebSocket

from backend.db.repositories import Repositories
from backend.supervisor import tools
from backend.supervisor.faq import match_faq
from backend.supervisor.graph import GRAPH
from backend.supervisor.heuristics import is_explicit_human_request
from backend.supervisor.state import CALL_STATES, CallState, get_or_create_state
from backend.supervisor.tracing import traced_call
from backend.utils import now_iso

logger = logging.getLogger(__name__)

LOCKS: dict[str, asyncio.Lock] = {}
SPEAKING: dict[str, bool] = {}
DEFERRED: dict[str, list[tuple[str, str, str, float]]] = {}
CONNECTIONS: dict[str, WebSocket] = {}


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


def mark_call_abandoned(repos: Repositories, call_id: str) -> None:
    state = CALL_STATES.get(call_id)
    if state and state["stage"] != "ended":
        state["stage"] = "ended"
        repos.calls.upsert(state, outcome_override="abandoned")
    repos.trace.record_event(call_id, "call_abandoned")
    CONNECTIONS.pop(call_id, None)
    SPEAKING.pop(call_id, None)
    DEFERRED.pop(call_id, None)
