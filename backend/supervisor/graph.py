import logging
from datetime import date as date_cls

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from backend.db.repositories.base import SlotAlreadyBookedError
from backend.supervisor import heuristics, tools
from backend.supervisor.llm_utils import LLMCallFailed, call_claude_tool
from backend.supervisor.state import FIELD_PRIORITY, CallState
from backend.supervisor.tracing import traced_call
from backend.utils import now_iso

logger = logging.getLogger(__name__)

FIELD_LABELS = {
    "name": "name",
    "email": "email address",
    "phone": "phone number",
}


def _repos(config: RunnableConfig):
    return config["configurable"]["repos"]


def _agent_turn(reply: str) -> dict:
    return {"pending_reply": reply, "transcript": [{"role": "agent", "text": reply, "ts": now_iso()}]}


def _last_agent_reply(transcript: list[dict], fallback: str) -> str:
    # transcript[-1] is always the caller's current utterance (appended by
    # the dispatcher before invoking the graph) — skip it and find the
    # question we're actually repeating
    for turn in reversed(transcript[:-1]):
        if turn["role"] == "agent":
            return turn["text"]
    return fallback


def _llm_failure_fallback(repos, state: CallState, node: str) -> dict:
    call_id = state["call_id"]
    failures = state.get("consecutive_llm_failures", 0) + 1
    if failures >= 3:
        reply = "I'm having some technical trouble on my end — let me get you to a person who can help."
        repos.trace.record_event(
            call_id, "node_exited", node=node, stage_from=state["stage"], stage_to="escalation", pending_reply=reply
        )
        return {
            "stage": "escalation",
            "escalation_reason": "system_error",
            "consecutive_llm_failures": failures,
            **_agent_turn(reply),
        }
    reply = "Sorry, I'm having a little trouble — could you say that again?"
    repos.trace.record_event(
        call_id, "node_exited", node=node, stage_from=state["stage"], stage_to=state["stage"], pending_reply=reply
    )
    return {"consecutive_llm_failures": failures, **_agent_turn(reply)}


def apply_extraction(field_name: str, value, confidence: float):
    # email/phone are always confirmed back to the caller before being
    # accepted, regardless of extraction confidence or format validity —
    # a wrong value here means we can't reach the caller back, so it's
    # never auto-trusted. Only a genuinely empty extraction (confidence 0,
    # field not present in the utterance at all) is discarded outright.
    if field_name in ("email", "phone"):
        if confidence <= 0 or value is None:
            return None, "missing"
        return value, "pending_confirm"
    if confidence >= 0.75:
        return value, "confirmed"
    elif confidence >= 0.4:
        return value, "pending_confirm"
    else:
        return None, "missing"


def route_by_stage(state: CallState) -> str:
    stage = state["stage"]
    if stage == "ended":
        logger.warning(
            "route_by_stage invoked for an already-ended call_id=%s — "
            "dispatcher should guard against this; routing to escalation as a fallback",
            state["call_id"],
        )
        return "escalation"
    if stage == "capture":
        # Phase 7 (optimistic capture): same "capture" stage, two entry
        # points depending on which sub-phase we're in — capture_phase is
        # never touched outside stage == "capture" so this is a clean split,
        # not a new top-level stage.
        return "capture_confirm" if state["capture_phase"] == "confirm" else "capture_fast"
    return stage


def node_greeting(state: CallState, config: RunnableConfig) -> dict:
    # No spoken reply of its own: the Realtime client already delivers the
    # actual verbal greeting itself (per its own instructions), without ever
    # calling ask_supervisor for it. This node's only job is the one-time
    # stage bump so the caller's first real utterance — already sitting in
    # this same invocation's transcript — gets classified by node_routing
    # right away instead of being silently discarded for a turn. See
    # dispatcher.process_supervisor_call, which chains straight into the
    # next node when a call starts from "greeting" rather than treating
    # this stage bump as a turn worth replying to on its own.
    repos = _repos(config)
    repos.trace.record_event(state["call_id"], "node_entered", node="greeting")
    repos.trace.record_event(
        state["call_id"], "node_exited", node="greeting",
        stage_from="greeting", stage_to="routing", pending_reply=None,
    )
    return {"stage": "routing", "consecutive_llm_failures": 0}


def node_routing(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    call_id = state["call_id"]
    repos.trace.record_event(call_id, "node_entered", node="routing")

    try:
        result = call_claude_tool(
            repos.trace, call_id, "routing", "classify_practice_area",
            tools.classify_practice_area, state["transcript"],
        )
    except LLMCallFailed:
        return _llm_failure_fallback(repos, state, "routing")

    if result["area"] == "multiple_areas":
        reply = (
            "It sounds like this touches more than one area — let me get you to someone "
            "who can help directly."
        )
        repos.trace.record_event(
            call_id, "node_exited", node="routing",
            stage_from="routing", stage_to="escalation", pending_reply=reply,
        )
        return {
            "stage": "escalation",
            "escalation_reason": "out_of_scope_multi_area",
            "consecutive_llm_failures": 0,
            **_agent_turn(reply),
        }

    if result["area"] == "unclear":
        attempts = state["retry_counts"].get("classification", 0) + 1
        if attempts >= 2:
            reply = (
                "I'm having trouble telling which area this falls under, so let me "
                "get you to someone who can help directly."
            )
            repos.trace.record_event(
                call_id, "node_exited", node="routing",
                stage_from="routing", stage_to="escalation", pending_reply=reply,
            )
            return {
                "stage": "escalation",
                "escalation_reason": "unable_to_classify",
                "retry_counts": {**state["retry_counts"], "classification": attempts},
                "consecutive_llm_failures": 0,
                **_agent_turn(reply),
            }
        reply = "Could you tell me a bit more — is this to do with your job, your home, or immigration?"
        repos.trace.record_event(
            call_id, "node_exited", node="routing",
            stage_from="routing", stage_to="routing", pending_reply=reply,
        )
        return {
            "retry_counts": {**state["retry_counts"], "classification": attempts},
            "consecutive_llm_failures": 0,
            **_agent_turn(reply),
        }

    area = result["area"]
    reply = f"Got it — this falls under {area} law. Let's start with your name — could you tell me that?"
    repos.trace.record_event(
        call_id, "node_exited", node="routing",
        stage_from="routing", stage_to="capture", pending_reply=reply,
    )
    return {
        "stage": "capture",
        "practice_area": area,
        # This reply already asks about "name" (Phase 7 fast-pass sequencing
        # starts here, not in node_capture_fast's own first turn) — record
        # it so node_capture_fast knows the caller's next utterance answers
        # "name", not that it still needs asking.
        "last_asked_field": "name",
        "consecutive_llm_failures": 0,
        **_agent_turn(reply),
    }


def node_capture(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    call_id = state["call_id"]
    repos.trace.record_event(call_id, "node_entered", node="capture")
    profile = state["caller_profile"]
    utterance = state["transcript"][-1]["text"] if state["transcript"] else ""

    def _is_valid_format(field_name: str, value) -> bool:
        # deterministic validators still go through traced_call — every
        # tool_catalog call is traced, not just the LLM-backed ones (CLAUDE.md
        # rule 8 / cross-cutting.md section 0)
        if field_name == "email":
            return traced_call(repos.trace, call_id, "capture", "validate_email", tools.validate_email, value)
        if field_name == "phone":
            return traced_call(repos.trace, call_id, "capture", "validate_phone", tools.validate_phone, value)
        return True

    def _deny_and_reprompt(field_name: str, reply: str):
        # Shared by an explicit "no", a "yes" to a value that can never pass
        # format validation, and a fresh extraction that's already invalid —
        # all are failed attempts and must count toward escalation, or an
        # unconfirmable value (e.g. an email with no @ at all) loops forever.
        attempts = profile[field_name]["attempts"] + 1
        if attempts >= 3:
            escalate_reply = "I'm having trouble getting that detail — let me get you to someone who can help directly."
            repos.trace.record_event(
                call_id, "node_exited", node="capture",
                stage_from="capture", stage_to="escalation", pending_reply=escalate_reply,
            )
            return {
                "stage": "escalation",
                "escalation_reason": "capture_failed",
                "caller_profile": {**profile, field_name: {**profile[field_name], "attempts": attempts}},
                "consecutive_llm_failures": 0,
                **_agent_turn(escalate_reply),
            }
        repos.trace.record_event(
            call_id, "node_exited", node="capture",
            stage_from="capture", stage_to="capture", pending_reply=reply,
        )
        return {
            "caller_profile": {**profile, field_name: {**profile[field_name], "status": "missing", "attempts": attempts}},
            "consecutive_llm_failures": 0,
            **_agent_turn(reply),
        }

    def _invalid_format_reply(field_name: str) -> str:
        return f"That doesn't look like a valid {FIELD_LABELS[field_name]} — could you say it again?"

    try:
        pending_field = next((f for f in FIELD_PRIORITY if profile[f]["status"] == "pending_confirm"), None)

        if pending_field:
            answer = call_claude_tool(
                repos.trace, call_id, "capture", "confirm_field_answer",
                tools.confirm_field_answer, utterance, pending_field, profile[pending_field]["value"],
            )
            if answer.get("needs_clarification"):
                repeat_reply = _last_agent_reply(
                    state["transcript"], f"Sorry, could you confirm your {FIELD_LABELS[pending_field]}?"
                )
                repos.trace.record_event(
                    call_id, "node_exited", node="capture",
                    stage_from="capture", stage_to="capture", pending_reply=repeat_reply,
                )
                return {"consecutive_llm_failures": 0, **_agent_turn(repeat_reply)}
            if answer["confirmed"]:
                candidate = profile[pending_field]["value"]
                if _is_valid_format(pending_field, candidate):
                    target_field, new_value, new_status = pending_field, candidate, "confirmed"
                else:
                    # defense in depth: an invalid value should never have reached
                    # pending_confirm, but if it did, a "yes" can't be allowed to
                    # confirm it — that value will never validate
                    return _deny_and_reprompt(pending_field, _invalid_format_reply(pending_field))
            elif answer["corrected_value"]:
                if not _is_valid_format(pending_field, answer["corrected_value"]):
                    return _deny_and_reprompt(pending_field, _invalid_format_reply(pending_field))
                target_field = pending_field
                new_value, new_status = apply_extraction(pending_field, answer["corrected_value"], 0.9)
            else:
                return _deny_and_reprompt(pending_field, f"Sorry, could you tell me your {FIELD_LABELS[pending_field]} again?")
        else:
            target_field = next((f for f in FIELD_PRIORITY if profile[f]["status"] == "missing"), None)
            if target_field is None:
                reply = "Thanks, I've got everything I need. What day and time works for you?"
                repos.trace.record_event(
                    call_id, "node_exited", node="capture",
                    stage_from="capture", stage_to="booking", pending_reply=reply,
                )
                return {"stage": "booking", "consecutive_llm_failures": 0, **_agent_turn(reply)}
            extracted = call_claude_tool(
                repos.trace, call_id, "capture", "extract_field", tools.extract_field, utterance, target_field
            )
            if target_field in ("email", "phone"):
                # handled fully explicitly, not through apply_extraction's
                # confidence thresholds: any outcome that isn't a genuinely
                # valid value — including confidence 0 (nothing recognizable
                # said at all) — must explain why and re-ask, not silently
                # repeat the same question with no indication anything was wrong
                candidate = extracted["value"] or None
                if candidate is None or not _is_valid_format(target_field, candidate):
                    return _deny_and_reprompt(target_field, _invalid_format_reply(target_field))
                new_value, new_status = candidate, "pending_confirm"
            else:
                new_value, new_status = apply_extraction(target_field, extracted["value"], extracted["confidence"])
    except LLMCallFailed:
        return _llm_failure_fallback(repos, state, "capture")

    new_profile = {**profile, target_field: {**profile[target_field], "value": new_value, "status": new_status}}

    if new_status == "pending_confirm":
        try:
            reply = call_claude_tool(
                repos.trace, call_id, "capture", "generate_confirm_back",
                tools.generate_confirm_back, target_field, new_value,
            )
        except LLMCallFailed:
            return _llm_failure_fallback(repos, state, "capture")
        repos.trace.record_event(
            call_id, "node_exited", node="capture",
            stage_from="capture", stage_to="capture", pending_reply=reply,
        )
        return {"caller_profile": new_profile, "consecutive_llm_failures": 0, **_agent_turn(reply)}

    remaining = [f for f in FIELD_PRIORITY if new_profile[f]["status"] != "confirmed"]
    if not remaining:
        reply = "Thanks, I've got everything I need. What day and time works for you?"
        repos.trace.record_event(
            call_id, "node_exited", node="capture",
            stage_from="capture", stage_to="booking", pending_reply=reply,
        )
        return {"stage": "booking", "caller_profile": new_profile, "consecutive_llm_failures": 0, **_agent_turn(reply)}

    next_target = remaining[0]
    if new_profile[next_target]["status"] == "pending_confirm":
        # Phase 7 (optimistic capture): the confirm/drain phase can have
        # multiple fields already sitting in "pending_confirm" at once —
        # batched by the fast pass (backend.dispatcher.FIELD_VERIFICATIONS
        # resolving in the background while later fields were being asked
        # about). Today's ORIGINAL flow could never reach this branch: it
        # always fully resolved one field through confirmation before ever
        # touching the next, so "the next non-confirmed field is already
        # pending_confirm" was structurally impossible before Phase 7. Ask
        # its confirm-back, not a fresh "what's your X" — that would
        # silently discard the value already captured for it.
        try:
            reply = call_claude_tool(
                repos.trace, call_id, "capture", "generate_confirm_back",
                tools.generate_confirm_back, next_target, new_profile[next_target]["value"],
            )
        except LLMCallFailed:
            return _llm_failure_fallback(repos, state, "capture")
        repos.trace.record_event(
            call_id, "node_exited", node="capture",
            stage_from="capture", stage_to="capture", pending_reply=reply,
        )
        return {"caller_profile": new_profile, "consecutive_llm_failures": 0, **_agent_turn(reply)}

    reply = f"Thanks — and what's your {FIELD_LABELS[next_target]}?"
    repos.trace.record_event(
        call_id, "node_exited", node="capture",
        stage_from="capture", stage_to="capture", pending_reply=reply,
    )
    return {"caller_profile": new_profile, "consecutive_llm_failures": 0, **_agent_turn(reply)}


def _next_fast_field(asked_field):
    if asked_field is None:
        return FIELD_PRIORITY[0]
    idx = FIELD_PRIORITY.index(asked_field)
    return FIELD_PRIORITY[idx + 1] if idx + 1 < len(FIELD_PRIORITY) else None


def _sync_last_asked_field(result: dict, current_asked_field):
    # node_capture (today's original function, reused as the fallback path
    # here) has no concept of last_asked_field — its return dict never sets
    # it, so without this it would go stale after a fallback that actually
    # advances the conversation (e.g. a field auto-confirms and node_capture
    # moves on to ask about the next one internally), leaving the NEXT
    # node_capture_fast turn thinking the caller's answer is about the
    # wrong field. Re-derive it from whatever node_capture actually decided,
    # the same way node_capture's own bottom logic would.
    profile = result.get("caller_profile")
    if profile is None:
        return current_asked_field  # e.g. escalated, or unchanged — irrelevant either way
    pending = next((f for f in FIELD_PRIORITY if profile[f]["status"] == "pending_confirm"), None)
    if pending:
        return pending
    missing = next((f for f in FIELD_PRIORITY if profile[f]["status"] == "missing"), None)
    return missing if missing is not None else current_asked_field


def _fallback_to_real_capture(state: CallState, config: RunnableConfig) -> dict:
    result = node_capture(state, config)
    result["last_asked_field"] = _sync_last_asked_field(result, state["last_asked_field"])
    return result


def node_capture_fast(state: CallState, config: RunnableConfig) -> dict:
    """Phase 7 (optimistic capture) — the fast, zero-Claude-call sequencer.
    Asks through FIELD_PRIORITY optimistically while the real extraction/
    validation runs in the background (backend.dispatcher.FIELD_VERIFICATIONS,
    spawned by the dispatcher after this node returns — this node never
    touches asyncio itself). All actual sequencing stays deterministic here,
    per CLAUDE.md rule 2 — this never asks an LLM what to say next.

    Falls back to plain node_capture (today's real, synchronous logic,
    unchanged) whenever there's genuine doubt it's safe to guess: an
    explicit human request, an utterance that doesn't read as a direct
    answer, an utterance that doesn't even superficially match the expected
    field's shape, or a background verification that already came back a
    genuine failure (state["verification_failed_field"], set by the
    dispatcher's reconciliation step immediately before this node runs).
    See docs/phases/phase-7-optimistic-capture.md for the full design.
    """
    repos = _repos(config)
    call_id = state["call_id"]
    profile = state["caller_profile"]
    utterance = state["transcript"][-1]["text"] if state["transcript"] else ""
    asked_field = state["last_asked_field"]

    repos.trace.record_event(call_id, "node_entered", node="capture_fast")

    if asked_field and profile[asked_field]["status"] == "pending_confirm":
        # asked_field is already mid-confirmation (a prior fallback — gate
        # or urgent-reask — extracted it at medium confidence and it's now
        # awaiting a yes/no/correction). This utterance answers THAT
        # confirm-back question, not a fresh field question, regardless of
        # what the tangent/shape gate below would say about it (a plain
        # "no, it's Alex Smith" doesn't look like a tangent at all, but
        # advancing on it here would silently skip the confirmation
        # entirely, leaving the field pending_confirm forever). Always
        # defer to node_capture's real pending_field handling.
        repos.trace.record_event(
            call_id, "capture_fast_pending_confirm_fallback", node="capture_fast", field=asked_field
        )
        return _fallback_to_real_capture(state, config)

    if asked_field and state.get("verification_failed_field") == asked_field:
        repos.trace.record_event(
            call_id, "capture_fast_urgent_reask", node="capture_fast", field=asked_field
        )
        return _fallback_to_real_capture(state, config)

    if asked_field and (
        heuristics.is_explicit_human_request(utterance)
        or heuristics.looks_like_tangent(utterance)
        or not heuristics.looks_like_field_shape(asked_field, utterance)
    ):
        repos.trace.record_event(
            call_id, "capture_fast_gate_fallback", node="capture_fast", field=asked_field, utterance=utterance
        )
        return _fallback_to_real_capture(state, config)

    next_field = _next_fast_field(asked_field)
    if next_field:
        reply = f"Great — and what's your {FIELD_LABELS[next_field]}?"
        repos.trace.record_event(
            call_id, "node_exited", node="capture_fast",
            stage_from="capture", stage_to="capture", pending_reply=reply,
        )
        return {
            "last_asked_field": next_field,
            # Explicit signal, not inferred from a before/after
            # last_asked_field diff — this is the ONLY branch where a
            # background verification should actually be spawned.
            # _fallback_to_real_capture's paths also change last_asked_field
            # (e.g. resolving a pending confirmation and moving to the next
            # already-known field), but that utterance was already fully,
            # synchronously processed by real node_capture logic — spawning
            # a background task for it too would be redundant AND wrong: a
            # stray extra call to a mocked/live extract_field using this
            # turn's utterance, whose result could land and get merged on a
            # LATER turn, silently overwriting an already-correct value.
            # dispatcher.py checks for this key specifically, not a diff.
            "background_verify_field": asked_field,
            "consecutive_llm_failures": 0,
            **_agent_turn(reply),
        }

    # Fast pass exhausted — every field has been asked about once. Hand off
    # to the confirm/drain phase via _finish_fast_pass, which processes
    # THIS turn's utterance (the answer to asked_field, the last fast-asked
    # field) for real before deciding the first drain item.
    result = _finish_fast_pass(state, config, asked_field)
    result["capture_phase"] = "confirm"
    return result


def _finish_fast_pass(state: CallState, config: RunnableConfig, asked_field: str) -> dict:
    """The transition turn. Unlike every earlier field, asked_field (the
    LAST FIELD_PRIORITY entry) never got a background head start — there's
    no further "ask the next field" turn to run concurrently with it, since
    it IS the last one. So this turn processes it live, right here — real
    latency, same as node_capture would have for any field. This is the
    ONE turn Phase 7's design accepts that on: every earlier field already
    resolved in the background while later questions were being asked, so
    this still collapses what used to be N per-field waits down to at
    most 1.

    Deliberately does NOT call node_capture directly: by this point earlier
    fields (e.g. email) have very likely already been merged into
    caller_profile as "pending_confirm" by the dispatcher's background-
    verification reconciliation, and node_capture's OWN first check is "is
    there a pending_field needing confirmation" — it would misinterpret
    THIS utterance (the answer to asked_field) as a confirm/deny answer to
    that earlier, already-pending field instead. This function knows
    unambiguously which field the current utterance answers and processes
    it directly, only consulting FIELD_PRIORITY afterward to decide which
    field's confirm-back to speak first.

    Duplicates a meaningful chunk of node_capture's "extract a new field"
    logic rather than sharing code with it, to avoid touching that
    well-tested function's control flow — a known, accepted trade-off, same
    category as docs/code-review-2026-08-24.md's other flagged duplication
    (simplification, not correctness).
    """
    repos = _repos(config)
    call_id = state["call_id"]
    profile = state["caller_profile"]
    utterance = state["transcript"][-1]["text"] if state["transcript"] else ""

    try:
        extracted = call_claude_tool(
            repos.trace, call_id, "capture_fast", "extract_field", tools.extract_field, utterance, asked_field
        )
        if asked_field in ("email", "phone"):
            candidate = extracted["value"] or None
            valid = candidate is not None and traced_call(
                repos.trace, call_id, "capture_fast",
                "validate_email" if asked_field == "email" else "validate_phone",
                tools.validate_email if asked_field == "email" else tools.validate_phone, candidate,
            )
            new_value, new_status = (candidate, "pending_confirm") if valid else (None, "missing")
        else:
            new_value, new_status = apply_extraction(asked_field, extracted["value"], extracted["confidence"])
    except LLMCallFailed:
        return _llm_failure_fallback(repos, state, "capture_fast")

    if new_status == "missing":
        attempts = profile[asked_field]["attempts"] + 1
        if attempts >= 3:
            reply = "I'm having trouble getting that detail — let me get you to someone who can help directly."
            repos.trace.record_event(
                call_id, "node_exited", node="capture_fast",
                stage_from="capture", stage_to="escalation", pending_reply=reply,
            )
            return {
                "stage": "escalation",
                "escalation_reason": "capture_failed",
                "caller_profile": {**profile, asked_field: {**profile[asked_field], "attempts": attempts}},
                "consecutive_llm_failures": 0,
                **_agent_turn(reply),
            }
        reply = f"That doesn't look like a valid {FIELD_LABELS[asked_field]} — could you say it again?"
        repos.trace.record_event(
            call_id, "node_exited", node="capture_fast",
            stage_from="capture", stage_to="capture", pending_reply=reply,
        )
        return {
            "caller_profile": {**profile, asked_field: {**profile[asked_field], "status": "missing", "attempts": attempts}},
            "consecutive_llm_failures": 0,
            **_agent_turn(reply),
        }

    new_profile = {**profile, asked_field: {**profile[asked_field], "value": new_value, "status": new_status}}

    pending_field = next((f for f in FIELD_PRIORITY if new_profile[f]["status"] == "pending_confirm"), None)
    if pending_field is None:
        reply = "Thanks, I've got everything I need. What day and time works for you?"
        repos.trace.record_event(
            call_id, "node_exited", node="capture_fast",
            stage_from="capture", stage_to="booking", pending_reply=reply,
        )
        return {"stage": "booking", "caller_profile": new_profile, "consecutive_llm_failures": 0, **_agent_turn(reply)}

    try:
        confirm_back = call_claude_tool(
            repos.trace, call_id, "capture_fast", "generate_confirm_back",
            tools.generate_confirm_back, pending_field, new_profile[pending_field]["value"],
        )
    except LLMCallFailed:
        return _llm_failure_fallback(repos, state, "capture_fast")
    spoken = f"Great, let me just quickly confirm a couple of things: {confirm_back}"
    repos.trace.record_event(
        call_id, "node_exited", node="capture_fast",
        stage_from="capture", stage_to="capture", pending_reply=spoken,
    )
    return {"caller_profile": new_profile, "consecutive_llm_failures": 0, **_agent_turn(spoken)}


def node_booking(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    call_id = state["call_id"]
    repos.trace.record_event(call_id, "node_entered", node="booking")
    utterance = state["transcript"][-1]["text"] if state["transcript"] else ""

    def _escalate_no_slot(requested_date, requested_window, declined_delta=None):
        reply = (
            "I'm not finding anything that works near that time — let me get you to "
            "someone who can find a slot directly."
        )
        repos.trace.record_event(
            call_id, "node_exited", node="booking",
            stage_from="booking", stage_to="escalation", pending_reply=reply,
        )
        result = {
            "stage": "escalation",
            "escalation_reason": "no_acceptable_slot",
            "requested_date": requested_date,
            "requested_window": requested_window,
            "consecutive_llm_failures": 0,
            **_agent_turn(reply),
        }
        if declined_delta is not None:
            result["declined_slot_ids"] = declined_delta
        return result

    def _propose_slot(slot, requested_date, requested_window, declined_delta=None, unavailable_time=None):
        try:
            summary = call_claude_tool(
                repos.trace, call_id, "booking", "generate_confirmation_summary",
                tools.generate_confirmation_summary, state["caller_profile"], slot, state["practice_area"],
                unavailable_time,
            )
        except LLMCallFailed:
            return _llm_failure_fallback(repos, state, "booking")
        repos.trace.record_event(
            call_id, "node_exited", node="booking",
            stage_from="booking", stage_to="booking", pending_reply=summary,
        )
        result = {
            "proposed_slot_id": slot["id"],
            "requested_date": requested_date,
            "requested_window": requested_window,
            "consecutive_llm_failures": 0,
            **_agent_turn(summary),
        }
        if declined_delta is not None:
            result["declined_slot_ids"] = declined_delta
        return result

    if state["proposed_slot_id"] is None:
        try:
            parsed = call_claude_tool(
                repos.trace, call_id, "booking", "extract_datetime",
                tools.extract_datetime, utterance, date_cls.today(),
            )
        except LLMCallFailed:
            return _llm_failure_fallback(repos, state, "booking")

        if not parsed.get("date") or parsed.get("confidence", 0) < 0.4:
            # nothing recognizable as a date/time was said at all — never
            # fabricate a proposal from an empty/garbage date (an empty
            # string passes every "date(start_time) >= ?" filter, which
            # silently produced a bogus proposal the caller never asked for)
            reply = "Sorry, I didn't catch a date or time there — what day and time would work for you?"
            repos.trace.record_event(
                call_id, "node_exited", node="booking",
                stage_from="booking", stage_to="booking", pending_reply=reply,
            )
            return {"consecutive_llm_failures": 0, **_agent_turn(reply)}

        requested_date, requested_window, requested_time = parsed["date"], parsed["window"], parsed.get("time")
        slot = traced_call(
            repos.trace, call_id, "booking", "check_availability",
            repos.slots.check_availability, requested_date, requested_window, state["practice_area"],
            requested_time, state["declined_slot_ids"],
        )
        if slot:
            return _propose_slot(slot, requested_date, requested_window)

        alternatives = traced_call(
            repos.trace, call_id, "booking", "suggest_alternative_slots",
            repos.slots.suggest_alternatives, requested_date, state["practice_area"], state["declined_slot_ids"],
        )
        if not alternatives:
            return _escalate_no_slot(requested_date, requested_window)
        return _propose_slot(
            alternatives[0], requested_date, requested_window, unavailable_time=requested_time
        )

    try:
        answer = call_claude_tool(
            repos.trace, call_id, "booking", "confirm_booking_answer",
            tools.confirm_booking_answer, utterance,
        )
    except LLMCallFailed:
        return _llm_failure_fallback(repos, state, "booking")

    if answer.get("needs_clarification"):
        repeat_reply = _last_agent_reply(state["transcript"], "Sorry, does that proposed time work for you?")
        repos.trace.record_event(
            call_id, "node_exited", node="booking",
            stage_from="booking", stage_to="booking", pending_reply=repeat_reply,
        )
        return {"consecutive_llm_failures": 0, **_agent_turn(repeat_reply)}

    requested_date, requested_window = state["requested_date"], state["requested_window"]

    if answer["accepted"]:
        try:
            traced_call(
                repos.trace, call_id, "booking", "book_consultation",
                repos.slots.book, state["proposed_slot_id"],
            )
        except SlotAlreadyBookedError:
            # rare race — someone else took it between propose and confirm;
            # re-run availability rather than crash the call or double-book
            slot = traced_call(
                repos.trace, call_id, "booking", "check_availability",
                repos.slots.check_availability, requested_date, requested_window, state["practice_area"],
            )
            if slot:
                return _propose_slot(slot, requested_date, requested_window)
            alternatives = traced_call(
                repos.trace, call_id, "booking", "suggest_alternative_slots",
                repos.slots.suggest_alternatives, requested_date, state["practice_area"],
                state["declined_slot_ids"] + [state["proposed_slot_id"]],
            )
            if not alternatives:
                return _escalate_no_slot(
                    requested_date, requested_window, declined_delta=[state["proposed_slot_id"]]
                )
            return _propose_slot(
                alternatives[0], requested_date, requested_window,
                declined_delta=[state["proposed_slot_id"]],
            )

        reply = "You're booked — you'll get a confirmation shortly."
        repos.trace.record_event(
            call_id, "node_exited", node="booking",
            stage_from="booking", stage_to="ended", pending_reply=reply,
        )
        return {"stage": "ended", "booking_confirmed": True, "consecutive_llm_failures": 0, **_agent_turn(reply)}

    declined = state["declined_slot_ids"] + [state["proposed_slot_id"]]
    if len(declined) >= 2:
        return _escalate_no_slot(requested_date, requested_window, declined_delta=[state["proposed_slot_id"]])

    # ask what would work instead, rather than unilaterally guessing the
    # next chronological slot — the caller may have a specific day/time in
    # mind that isn't just "whatever's next". Reset proposed_slot_id so the
    # next turn re-enters the propose branch above via a fresh extract_datetime.
    reply = "No problem — what other day or time would work for you?"
    repos.trace.record_event(
        call_id, "node_exited", node="booking",
        stage_from="booking", stage_to="booking", pending_reply=reply,
    )
    return {
        "proposed_slot_id": None,
        "declined_slot_ids": [state["proposed_slot_id"]],
        "consecutive_llm_failures": 0,
        **_agent_turn(reply),
    }


def node_escalation(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    call_id = state["call_id"]
    repos.trace.record_event(call_id, "node_entered", node="escalation")

    try:
        summary = call_claude_tool(
            repos.trace, call_id, "escalation", "generate_call_summary",
            tools.generate_call_summary, state,
        )
        traced_call(repos.trace, call_id, "escalation", "write_handoff_note", tools.write_handoff_note, call_id, state, summary)
    except LLMCallFailed:
        # Don't trust another Claude call to succeed right after one just
        # failed unexpectedly — fall back to a deterministic note.
        traced_call(
            repos.trace, call_id, "escalation", "write_minimal_handoff_note",
            tools.write_minimal_handoff_note, call_id, state,
            f"escalation_reason={state.get('escalation_reason')} (call summary unavailable)",
        )

    reply = "I've passed this to our team, someone will follow up shortly."
    repos.trace.record_event(
        call_id, "node_exited", node="escalation",
        stage_from=state["stage"], stage_to="ended", pending_reply=reply,
    )
    return {"stage": "ended", "consecutive_llm_failures": 0, **_agent_turn(reply)}


def build_graph():
    g = StateGraph(CallState)
    for name, fn in [
        ("greeting", node_greeting),
        ("routing", node_routing),
        ("capture_fast", node_capture_fast),
        # "capture_confirm" reuses node_capture unchanged — the drain phase
        # is fully synchronous and behaves exactly like today's capture
        # node (Decision 2, docs/phases/phase-7-optimistic-capture.md): it
        # already correctly handles a pending confirm-back answer and a
        # genuinely-failed field's re-ask, no new logic needed there.
        ("capture_confirm", node_capture),
        ("booking", node_booking),
        ("escalation", node_escalation),
    ]:
        g.add_node(name, fn)
        g.add_edge(name, END)
    g.set_conditional_entry_point(
        route_by_stage,
        {
            "greeting": "greeting",
            "routing": "routing",
            "capture_fast": "capture_fast",
            "capture_confirm": "capture_confirm",
            "booking": "booking",
            "escalation": "escalation",
        },
    )
    return g.compile()


GRAPH = build_graph()
