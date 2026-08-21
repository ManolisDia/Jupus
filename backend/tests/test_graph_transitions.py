import pytest

from backend.db.repositories import Repositories
from backend.supervisor.graph import GRAPH, route_by_stage
from backend.supervisor.state import new_call_state
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


def _invoke(state, repos):
    return GRAPH.invoke(state, config={"configurable": {"repos": repos}})


def test_greeting_transitions_to_routing(repos):
    state = new_call_state("call-1")
    result = _invoke(state, repos)
    assert result["stage"] == "routing"


def test_routing_transitions_to_capture(repos):
    state = new_call_state("call-1")
    state["stage"] = "routing"
    result = _invoke(state, repos)
    assert result["stage"] == "capture"
    assert result["practice_area"] == "employment"


def test_capture_transitions_to_booking(repos):
    state = new_call_state("call-1")
    state["stage"] = "capture"
    result = _invoke(state, repos)
    assert result["stage"] == "booking"


def test_booking_transitions_to_ended(repos):
    state = new_call_state("call-1")
    state["stage"] = "booking"
    result = _invoke(state, repos)
    assert result["stage"] == "ended"
    assert result["booking_confirmed"] is True


def test_escalation_sets_stage_ended(repos):
    state = new_call_state("call-1")
    state["stage"] = "escalation"
    result = _invoke(state, repos)
    assert result["stage"] == "ended"


@pytest.mark.parametrize(
    "stage,expected_node",
    [
        ("greeting", "greeting"),
        ("routing", "routing"),
        ("capture", "capture"),
        ("booking", "booking"),
        ("escalation", "escalation"),
    ],
)
def test_router_dispatches_to_correct_node_for_each_stage(stage, expected_node):
    state = new_call_state("call-1")
    state["stage"] = stage
    assert route_by_stage(state) == expected_node


def test_transcript_accumulates_across_multiple_invocations(repos):
    state = new_call_state("call-1")
    # greeting node adds 1 agent turn, ends at stage="routing"
    first_result = _invoke(state, repos)
    assert len(first_result["transcript"]) == 1

    # dispatcher.py appends the caller's next utterance before re-invoking
    second_input = dict(first_result)
    second_input["transcript"] = first_result["transcript"] + [
        {"role": "caller", "text": "in between", "ts": "t"}
    ]
    assert len(second_input["transcript"]) == 2

    # routing node adds 1 more agent turn — total should be all 3 turns,
    # not just the 1 turn this second invocation itself added
    second_result = _invoke(second_input, repos)
    assert len(second_result["transcript"]) == 3
