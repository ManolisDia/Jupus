from unittest.mock import patch

import pytest

from backend.db.repositories import Repositories
from backend.supervisor import tools
from backend.supervisor.graph import GRAPH
from backend.supervisor.state import new_call_state
from backend.tests.fakes import (
    FakeCallRepository,
    FakeEscalationRepository,
    FakeTraceRepository,
)


@pytest.fixture
def repos():
    return Repositories(
        calls=FakeCallRepository(),
        slots=None,
        trace=FakeTraceRepository(),
        escalations=FakeEscalationRepository(),
    )


def _summary(reason_for_call="Caller needs help.", escalation_explanation="A human is needed."):
    return {"reason_for_call": reason_for_call, "escalation_explanation": escalation_explanation}


def _invoke(state, repos):
    return GRAPH.invoke(state, config={"configurable": {"repos": repos}})


def test_escalation_node_ends_call(repos, tmp_path):
    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "unable_to_classify"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch("backend.supervisor.tools.summarize_escalation", return_value=_summary()),
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
        patch(
            "backend.supervisor.tools.summarize_escalation",
            return_value=_summary("Caller needs help with a lease.", "Could not classify the matter."),
        ),
    ):
        _invoke(state, repos)

    note = (tmp_path / "call-1.md").read_text(encoding="utf-8")
    assert "tenancy" in note
    assert "unable_to_classify" in note
    assert "Caller needs help with a lease." in note
    assert "Could not classify the matter." in note


def test_records_escalation_row_with_all_three_parts(repos, tmp_path):
    # The whole point of the table: someone picking this off the queue can
    # see why they rang, who they are, and why we handed over — without
    # opening a markdown file or replaying the transcript.
    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "out_of_scope_multi_area"
    state["practice_area"] = "employment"
    for field, value in (("name", "Dana"), ("email", "dana@example.com"), ("phone", "07700900123")):
        state["caller_profile"][field]["value"] = value
        state["caller_profile"][field]["status"] = "confirmed"

    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch(
            "backend.supervisor.tools.summarize_escalation",
            return_value=_summary(
                "Dismissed after raising a grievance, and their visa depends on the job.",
                "The matter spans employment and immigration, which intake can't triage.",
            ),
        ),
    ):
        _invoke(state, repos)

    row = repos.escalations.get("call-1")
    assert row is not None
    assert row["escalation_reason"] == "out_of_scope_multi_area"
    assert row["reason_for_call"].startswith("Dismissed after raising a grievance")
    assert "employment and immigration" in row["escalation_explanation"]
    assert row["practice_area"] == "employment"
    assert (row["caller_name"], row["caller_email"], row["caller_phone"]) == (
        "Dana",
        "dana@example.com",
        "07700900123",
    )
    assert row["escalated_at"]


def test_escalation_row_omits_unconfirmed_fields(repos, tmp_path):
    # A pending_confirm value is a guess at noisy audio. Handing a human a
    # phone number the caller never actually confirmed is worse than handing
    # them nothing, because they'd ring it.
    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "capture_failed"
    state["caller_profile"]["phone"]["value"] = "07700900999"
    state["caller_profile"]["phone"]["status"] = "pending_confirm"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch("backend.supervisor.tools.summarize_escalation", return_value=_summary()),
    ):
        _invoke(state, repos)

    assert repos.escalations.get("call-1")["caller_phone"] is None


def test_handoff_note_omits_unconfirmed_fields(repos, tmp_path):
    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "capture_failed"
    state["caller_profile"]["email"]["value"] = "j@x.com"
    state["caller_profile"]["email"]["status"] = "pending_confirm"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch("backend.supervisor.tools.summarize_escalation", return_value=_summary()),
    ):
        _invoke(state, repos)

    note = (tmp_path / "call-1.md").read_text(encoding="utf-8")
    assert "j@x.com" not in note
    assert "Email: not captured" in note


def test_no_acceptable_slot_from_booking_flows_into_escalation_note(repos, tmp_path):
    # Integration point with Phase 4: node_booking sets escalation_reason on
    # its own exit turn but doesn't write the handoff note itself — the
    # NEXT turn is what actually routes into node_escalation, same as every
    # other escalation_reason. Prove the two nodes hand off correctly rather
    # than just asserting each one in isolation.
    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "no_acceptable_slot"
    state["practice_area"] = "tenancy"
    state["requested_date"] = "2026-09-03"
    state["requested_window"] = "morning"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch(
            "backend.supervisor.tools.summarize_escalation",
            return_value=_summary("Deposit dispute.", "No slots worked for the caller."),
        ),
    ):
        result = _invoke(state, repos)

    assert result["stage"] == "ended"
    note = (tmp_path / "call-1.md").read_text(encoding="utf-8")
    assert "no_acceptable_slot" in note
    assert "No slots worked for the caller." in note
    assert repos.escalations.get("call-1")["escalation_reason"] == "no_acceptable_slot"


def test_llm_failure_falls_back_to_minimal_handoff_note(repos, tmp_path):
    import json

    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "system_error"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch(
            "backend.supervisor.tools.summarize_escalation",
            side_effect=json.JSONDecodeError("truncated", "doc", 0),
        ),
    ):
        result = _invoke(state, repos)

    assert result["stage"] == "ended"
    note = (tmp_path / "call-1.md").read_text(encoding="utf-8")
    assert "call summary unavailable" in note


def test_llm_failure_still_records_escalation_row(repos, tmp_path):
    # The summary is the part that's unavailable, not the escalation. A
    # call that ends up with a human must leave a row either way, or the
    # failures nobody wants to lose are exactly the ones that vanish.
    import json

    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "system_error"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch(
            "backend.supervisor.tools.summarize_escalation",
            side_effect=json.JSONDecodeError("truncated", "doc", 0),
        ),
    ):
        _invoke(state, repos)

    row = repos.escalations.get("call-1")
    assert row is not None
    assert row["escalation_reason"] == "system_error"
    assert row["reason_for_call"] is None
    assert "call summary unavailable" in row["escalation_explanation"]


def test_record_escalation_is_traced(repos, tmp_path):
    # Doctrine #8: every tool call goes through traced_call. The DB write is
    # a tool call like any other, so it has to show up in the trace.
    state = new_call_state("call-1")
    state["stage"] = "escalation"
    state["escalation_reason"] = "explicit_request"
    with (
        patch.object(tools, "HANDOFFS_DIR", tmp_path),
        patch("backend.supervisor.tools.summarize_escalation", return_value=_summary()),
    ):
        _invoke(state, repos)

    traced = {
        e["payload"].get("tool_name")
        for e in repos.trace.get_trace("call-1")
        if e["event_type"] == "tool_call_end"
    }
    assert {"record_escalation", "write_handoff_note"} <= traced
