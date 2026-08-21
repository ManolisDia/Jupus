from unittest.mock import patch

import pytest

from backend.db.repositories import Repositories
from backend.supervisor import tools
from backend.supervisor.graph import GRAPH
from backend.supervisor.state import new_call_state
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


def _invoke(state, repos):
    return GRAPH.invoke(state, config={"configurable": {"repos": repos}})


def test_escalation_node_ends_call(repos, tmp_path):
    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "unable_to_classify"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch("backend.supervisor.tools.generate_call_summary", return_value="Caller needs help."),
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "ended"


def test_writes_handoff_file_with_expected_fields(repos, tmp_path):
    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "unable_to_classify"
    state["practice_area"] = "tenancy"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch("backend.supervisor.tools.generate_call_summary", return_value="Caller needs help with a lease."),
    ):
        _invoke(state, repos)

    note = (tmp_path / "call-1.md").read_text(encoding="utf-8")
    assert "tenancy" in note
    assert "unable_to_classify" in note
    assert "Caller needs help with a lease." in note


def test_handoff_note_omits_unconfirmed_fields(repos, tmp_path):
    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "capture_failed"
    state["caller_profile"]["email"]["value"] = "j@x.com"
    state["caller_profile"]["email"]["status"] = "pending_confirm"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch("backend.supervisor.tools.generate_call_summary", return_value="summary"),
    ):
        _invoke(state, repos)

    note = (tmp_path / "call-1.md").read_text(encoding="utf-8")
    assert "j@x.com" not in note
    assert "Email: not captured" in note


def test_llm_failure_falls_back_to_minimal_handoff_note(repos, tmp_path):
    import json

    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "system_error"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch(
            "backend.supervisor.tools.generate_call_summary",
            side_effect=json.JSONDecodeError("truncated", "doc", 0),
        ),
    ):
        result = _invoke(state, repos)

    assert result["stage"] == "ended"
    note = (tmp_path / "call-1.md").read_text(encoding="utf-8")
    assert "call summary unavailable" in note
