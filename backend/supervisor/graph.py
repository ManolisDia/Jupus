import logging

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from backend.supervisor.state import CallState
from backend.utils import now_iso

logger = logging.getLogger(__name__)


def _repos(config: RunnableConfig):
    return config["configurable"]["repos"]


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
    return {
        "stage": "routing",
        "pending_reply": reply,
        "transcript": [{"role": "agent", "text": reply, "ts": now_iso()}],
    }


def node_routing(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    repos.trace.record_event(state["call_id"], "node_entered", node="routing")
    reply = "Got it — let me grab a few details."
    repos.trace.record_event(
        state["call_id"], "node_exited", node="routing",
        stage_from="routing", stage_to="capture", pending_reply=reply,
    )
    return {
        "stage": "capture",
        "practice_area": "employment",
        "pending_reply": reply,
        "transcript": [{"role": "agent", "text": reply, "ts": now_iso()}],
    }


def node_capture(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    repos.trace.record_event(state["call_id"], "node_entered", node="capture")
    reply = "Thanks, when would you like to come in?"
    repos.trace.record_event(
        state["call_id"], "node_exited", node="capture",
        stage_from="capture", stage_to="booking", pending_reply=reply,
    )
    return {
        "stage": "booking",
        "pending_reply": reply,
        "transcript": [{"role": "agent", "text": reply, "ts": now_iso()}],
    }


def node_booking(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    repos.trace.record_event(state["call_id"], "node_entered", node="booking")
    reply = "You're all set. (stub)"
    repos.trace.record_event(
        state["call_id"], "node_exited", node="booking",
        stage_from="booking", stage_to="ended", pending_reply=reply,
    )
    return {
        "stage": "ended",
        "booking_confirmed": True,
        "pending_reply": reply,
        "transcript": [{"role": "agent", "text": reply, "ts": now_iso()}],
    }


def node_escalation(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    repos.trace.record_event(state["call_id"], "node_entered", node="escalation")
    reply = "Let me get you to a person. (stub)"
    repos.trace.record_event(
        state["call_id"], "node_exited", node="escalation",
        stage_from=state["stage"], stage_to="ended", pending_reply=reply,
    )
    return {
        "stage": "ended",
        "pending_reply": reply,
        "transcript": [{"role": "agent", "text": reply, "ts": now_iso()}],
    }


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
