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


def test_confident_classification_sets_area_and_advances(repos):
    state = new_call_state("call-1")
    state["stage"] = "routing"
    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "tenancy", "confidence": 0.9},
    ):
        result = _invoke(state, repos)
    assert result["practice_area"] == "tenancy"
    assert result["stage"] == "capture"


def test_unclear_classification_reprompts_once(repos):
    state = new_call_state("call-1")
    state["stage"] = "routing"
    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "unclear", "confidence": 0.3},
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "routing"
    assert result["retry_counts"]["classification"] == 1


def test_unclear_classification_twice_escalates(repos):
    state = new_call_state("call-1")
    state["stage"] = "routing"
    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "unclear", "confidence": 0.3},
    ):
        first_result = _invoke(state, repos)
        second_result = _invoke(first_result, repos)
    assert second_result["stage"] == "escalation"
    assert second_result["escalation_reason"] == "unable_to_classify"
