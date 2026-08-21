import logging

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from backend.supervisor import tools
from backend.supervisor.llm_utils import LLMCallFailed, call_claude_tool
from backend.supervisor.state import FIELD_PRIORITY, CallState
from backend.supervisor.tracing import traced_call
from backend.utils import now_iso

logger = logging.getLogger(__name__)

FIELD_LABELS = {
    "name": "name",
    "email": "email address",
    "phone": "phone number",
    "preferred_time": "preferred time to come in",
}


def _repos(config: RunnableConfig):
    return config["configurable"]["repos"]


def _agent_turn(reply: str) -> dict:
    return {"pending_reply": reply, "transcript": [{"role": "agent", "text": reply, "ts": now_iso()}]}


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
    return stage


def node_greeting(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    repos.trace.record_event(state["call_id"], "node_entered", node="greeting")
    reply = "Thanks for calling — let me get you sorted."
    repos.trace.record_event(
        state["call_id"], "node_exited", node="greeting",
        stage_from="greeting", stage_to="routing", pending_reply=reply,
    )
    return {"stage": "routing", "consecutive_llm_failures": 0, **_agent_turn(reply)}


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
                reply = "Thanks, I've got everything I need. Let's find you a time to come in."
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
        reply = "Thanks, I've got everything I need. Let's find you a time to come in."
        repos.trace.record_event(
            call_id, "node_exited", node="capture",
            stage_from="capture", stage_to="booking", pending_reply=reply,
        )
        return {"stage": "booking", "caller_profile": new_profile, "consecutive_llm_failures": 0, **_agent_turn(reply)}

    next_target = remaining[0]
    reply = f"Thanks — and what's your {FIELD_LABELS[next_target]}?"
    repos.trace.record_event(
        call_id, "node_exited", node="capture",
        stage_from="capture", stage_to="capture", pending_reply=reply,
    )
    return {"caller_profile": new_profile, "consecutive_llm_failures": 0, **_agent_turn(reply)}


def node_booking(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    repos.trace.record_event(state["call_id"], "node_entered", node="booking")
    reply = "You're all set. (stub)"
    repos.trace.record_event(
        state["call_id"], "node_exited", node="booking",
        stage_from="booking", stage_to="ended", pending_reply=reply,
    )
    return {"stage": "ended", "booking_confirmed": True, "consecutive_llm_failures": 0, **_agent_turn(reply)}


def node_escalation(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    repos.trace.record_event(state["call_id"], "node_entered", node="escalation")
    reply = "Let me get you to a person. (stub)"
    repos.trace.record_event(
        state["call_id"], "node_exited", node="escalation",
        stage_from=state["stage"], stage_to="ended", pending_reply=reply,
    )
    return {"stage": "ended", "consecutive_llm_failures": 0, **_agent_turn(reply)}


def build_graph():
    g = StateGraph(CallState)
    for name, fn in [
        ("greeting", node_greeting),
        ("routing", node_routing),
        ("capture", node_capture),
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
            "capture": "capture",
            "booking": "booking",
            "escalation": "escalation",
        },
    )
    return g.compile()


GRAPH = build_graph()
