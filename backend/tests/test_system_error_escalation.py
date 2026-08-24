"""Cross-cutting (docs/phases/cross-cutting.md section 1): 3 consecutive
LLMCallFailed turns in any node must escalate with escalation_reason="system_error"
rather than crash or loop forever. The per-node mechanics are already covered by
each node's own test file (test_routing_node.py, test_capture_node.py); this file
is the dedicated cross-cutting checkpoint named in that doc's Definition of Done,
re-asserting the invariant holds across the nodes that have real Claude calls today.

node_booking/node_escalation are still stubs pending Phase 4/5 (no Claude calls of
their own yet), so there is nothing to retrofit LLMCallFailed handling into there —
this file only covers greeting/routing/capture, the nodes that exist for real.
"""

import json
from unittest.mock import patch

import pytest

from backend.db.repositories import Repositories
from backend.supervisor.graph import GRAPH
from backend.supervisor.state import new_call_state
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


def _invoke(state, repos):
    return GRAPH.invoke(state, config={"configurable": {"repos": repos}})


def test_three_consecutive_failures_escalates_with_system_error_in_routing(repos):
    state = new_call_state("call-1")
    state["stage"] = "routing"
    with patch(
        "backend.supervisor.tools.classify_practice_area",
        side_effect=json.JSONDecodeError("truncated", "doc", 0),
    ):
        for _ in range(3):
            state = _invoke(state, repos)

    assert state["stage"] == "escalation"
    assert state["escalation_reason"] == "system_error"


def test_three_consecutive_failures_escalates_with_system_error_in_capture(repos):
    state = new_call_state("call-1")
    state["stage"] = "capture"
    # Phase 7 split "capture" into a fast/confirm sub-phase — this test
    # exercises node_capture's own extract_field retry/escalation logic
    # directly, which now only runs unchanged in the "confirm" phase (the
    # default "fast" phase would route to node_capture_fast instead, which
    # never even calls extract_field when last_asked_field is None).
    state["capture_phase"] = "confirm"
    state["practice_area"] = "employment"
    state["transcript"] = [{"role": "caller", "text": "some utterance", "ts": "t"}]
    with patch(
        "backend.supervisor.tools.extract_field",
        side_effect=json.JSONDecodeError("truncated", "doc", 0),
    ):
        for _ in range(3):
            state = _invoke(state, repos)

    assert state["stage"] == "escalation"
    assert state["escalation_reason"] == "system_error"


def test_one_or_two_failures_do_not_escalate_yet(repos):
    state = new_call_state("call-1")
    state["stage"] = "routing"
    with patch(
        "backend.supervisor.tools.classify_practice_area",
        side_effect=json.JSONDecodeError("truncated", "doc", 0),
    ):
        state = _invoke(state, repos)
        assert state["stage"] == "routing"
        assert state["consecutive_llm_failures"] == 1

        state = _invoke(state, repos)
        assert state["stage"] == "routing"
        assert state["consecutive_llm_failures"] == 2
