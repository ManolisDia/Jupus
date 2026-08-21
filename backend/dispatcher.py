import logging

from backend.db.repositories import Repositories
from backend.supervisor.graph import GRAPH
from backend.supervisor.state import CALL_STATES, CallState, get_or_create_state
from backend.utils import now_iso

logger = logging.getLogger(__name__)


def derive_outcome_label(state: CallState) -> str:
    if state.get("escalation_reason"):
        return "escalated"
    if state.get("booking_confirmed"):
        return "booked"
    return "info_only"


def mark_call_abandoned(repos: Repositories, call_id: str) -> None:
    state = CALL_STATES.get(call_id)
    if state and state["stage"] != "ended":
        state["stage"] = "ended"
        repos.calls.upsert(state, outcome_override="abandoned")
    repos.trace.record_event(call_id, "call_abandoned")


async def on_ask_supervisor(repos: Repositories, call_id: str, tool_call_id: str, reason: str, utterance: str) -> str:
    state = get_or_create_state(call_id)
    if state["stage"] == "ended":
        logger.warning("ask_supervisor called for ended call_id=%s", call_id)
        return "This call has already been completed."

    state["transcript"].append({"role": "caller", "text": utterance, "ts": now_iso()})
    repos.trace.record_event(call_id, "user_message", text=utterance)

    updated = GRAPH.invoke(state, config={"configurable": {"repos": repos}})
    CALL_STATES[call_id] = updated

    if updated["stage"] == "ended":
        repos.trace.record_event(call_id, "call_ended", outcome=derive_outcome_label(updated))

    return updated["pending_reply"]
