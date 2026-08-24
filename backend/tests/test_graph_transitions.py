from unittest.mock import patch

import pytest

from backend.db.repositories import Repositories
from backend.supervisor import tools
from backend.supervisor.graph import GRAPH, route_by_stage
from backend.supervisor.state import new_call_state
from backend.tests.fakes import FakeCallRepository, FakeSlotRepository, FakeTraceRepository


@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=FakeSlotRepository(), trace=FakeTraceRepository())


def _invoke(state, repos):
    return GRAPH.invoke(state, config={"configurable": {"repos": repos}})


def test_greeting_transitions_to_routing(repos):
    state = new_call_state("call-1")
    result = _invoke(state, repos)
    assert result["stage"] == "routing"


def test_booking_transitions_to_ended(repos):
    state = new_call_state("call-1")
    state["stage"] = "booking"
    state["proposed_slot_id"] = 1
    state["requested_date"] = "2026-09-03"
    state["requested_window"] = "morning"
    state["transcript"] = [{"role": "caller", "text": "yes that works", "ts": "t"}]
    with patch("backend.supervisor.tools.confirm_booking_answer", return_value={"accepted": True}):
        result = _invoke(state, repos)
    assert result["stage"] == "ended"
    assert result["booking_confirmed"] is True


def test_escalation_sets_stage_ended(repos, tmp_path):
    state = new_call_state("call-1")
    state["stage"] = "escalation"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch("backend.supervisor.tools.generate_call_summary", return_value="summary"),
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "ended"


@pytest.mark.parametrize(
    "stage,expected_node",
    [
        ("greeting", "greeting"),
        ("routing", "routing"),
        ("booking", "booking"),
        ("escalation", "escalation"),
    ],
)
def test_router_dispatches_to_correct_node_for_each_stage(stage, expected_node):
    state = new_call_state("call-1")
    state["stage"] = stage
    assert route_by_stage(state) == expected_node


@pytest.mark.parametrize(
    "capture_phase,expected_node",
    [
        # Phase 7 (optimistic capture) split "capture" into two sub-phases —
        # route_by_stage now dispatches within stage=="capture" based on
        # capture_phase, not straight to a single "capture" node.
        ("fast", "capture_fast"),
        ("confirm", "capture_confirm"),
    ],
)
def test_router_dispatches_capture_stage_by_sub_phase(capture_phase, expected_node):
    state = new_call_state("call-1")
    state["stage"] = "capture"
    state["capture_phase"] = capture_phase
    assert route_by_stage(state) == expected_node


def test_transcript_accumulates_across_multiple_invocations(repos):
    state = new_call_state("call-1")
    # greeting node is a silent stub — no reply/agent turn of its own, just
    # a stage bump — so it adds 0 agent turns, ends at stage="routing"
    first_result = _invoke(state, repos)
    assert len(first_result["transcript"]) == 0

    # dispatcher.py appends the caller's next utterance before re-invoking.
    # Jump straight to "booking" with a slot already proposed (single
    # confirm_booking_answer Claude call, mocked) rather than continuing to
    # "routing" — routing is Claude-backed as of Phase 3 and is covered by
    # the mocked tests in test_routing_node.py instead.
    second_input = dict(first_result)
    second_input["stage"] = "booking"
    second_input["proposed_slot_id"] = 1
    second_input["requested_date"] = "2026-09-03"
    second_input["requested_window"] = "morning"
    second_input["transcript"] = first_result["transcript"] + [
        {"role": "caller", "text": "in between", "ts": "t"}
    ]
    assert len(second_input["transcript"]) == 1

    # booking node adds 1 more agent turn — total should be both turns,
    # not just the 1 turn this second invocation itself added
    with patch("backend.supervisor.tools.confirm_booking_answer", return_value={"accepted": True}):
        second_result = _invoke(second_input, repos)
    assert len(second_result["transcript"]) == 2
