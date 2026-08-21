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


def _capture_state(**caller_profile_overrides):
    state = new_call_state("call-1")
    state["stage"] = "capture"
    state["practice_area"] = "employment"
    state["transcript"] = [{"role": "caller", "text": "some utterance", "ts": "t"}]
    state["caller_profile"].update(caller_profile_overrides)
    return state


def test_field_priority_order_targets_name_first(repos):
    state = _capture_state()
    with patch(
        "backend.supervisor.tools.extract_field", return_value={"value": "x", "confidence": 0.9}
    ) as mock_extract:
        _invoke(state, repos)
    mock_extract.assert_called_once()
    assert mock_extract.call_args.args[1] == "name"


def test_high_confidence_valid_email_still_confirms_back(repos):
    # email/phone are never auto-trusted, even at high confidence with a
    # perfectly valid format — the caller must explicitly confirm first
    state = _capture_state(
        name={"value": "Alex", "confidence": 0.9, "status": "confirmed", "attempts": 0, "validated": True}
    )
    with (
        patch(
            "backend.supervisor.tools.extract_field", return_value={"value": "a@b.com", "confidence": 0.95}
        ),
        patch(
            "backend.supervisor.tools.generate_confirm_back", return_value="Did you say a@b.com?"
        ) as mock_confirm_back,
    ):
        result = _invoke(state, repos)
    assert result["caller_profile"]["email"]["status"] == "pending_confirm"
    mock_confirm_back.assert_called_once()


def test_confirmed_field_advances_to_next_target(repos):
    state = _capture_state()
    with patch(
        "backend.supervisor.tools.extract_field", return_value={"value": "Alex Smith", "confidence": 0.9}
    ):
        result = _invoke(state, repos)
    assert result["caller_profile"]["name"]["status"] == "confirmed"
    assert result["caller_profile"]["name"]["value"] == "Alex Smith"

    with patch("backend.supervisor.tools.extract_field") as mock_extract:
        mock_extract.return_value = {"value": "a@b.com", "confidence": 0.9}
        _invoke(result, repos)
    assert mock_extract.call_args.args[1] == "email"


def test_medium_confidence_triggers_pending_and_confirm_back_reply(repos):
    state = _capture_state()
    with (
        patch(
            "backend.supervisor.tools.extract_field", return_value={"value": "Alex", "confidence": 0.6}
        ),
        patch(
            "backend.supervisor.tools.generate_confirm_back", return_value="Did you say Alex?"
        ) as mock_confirm_back,
    ):
        result = _invoke(state, repos)
    assert result["caller_profile"]["name"]["status"] == "pending_confirm"
    mock_confirm_back.assert_called_once()
    assert result["pending_reply"]


def test_three_failed_attempts_on_same_field_escalates(repos):
    state = _capture_state()

    for _ in range(3):
        # each cycle: medium-confidence extraction -> pending_confirm,
        # then a denied confirmation with no correction -> back to "missing"
        # (or escalation on the 3rd) — attempts only increments on denial
        with (
            patch("backend.supervisor.tools.extract_field", return_value={"value": "Alex", "confidence": 0.6}),
            patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say Alex?"),
        ):
            state = _invoke(state, repos)
        assert state["caller_profile"]["name"]["status"] == "pending_confirm"

        with patch(
            "backend.supervisor.tools.confirm_field_answer",
            return_value={"confirmed": False, "corrected_value": None},
        ):
            state = _invoke(state, repos)
        if state["stage"] == "escalation":
            break

    assert state["stage"] == "escalation"
    assert state["escalation_reason"] == "capture_failed"


def test_confirmation_yes_accepts_pending_field(repos):
    state = _capture_state(
        name={"value": "Alex", "confidence": 0.6, "status": "pending_confirm", "attempts": 0, "validated": True}
    )
    with patch(
        "backend.supervisor.tools.confirm_field_answer",
        return_value={"confirmed": True, "corrected_value": None},
    ):
        result = _invoke(state, repos)
    assert result["caller_profile"]["name"]["status"] == "confirmed"


def test_confirmation_no_without_correction_reprompts(repos):
    state = _capture_state(
        name={"value": "Alex", "confidence": 0.6, "status": "pending_confirm", "attempts": 0, "validated": True}
    )
    with patch(
        "backend.supervisor.tools.confirm_field_answer",
        return_value={"confirmed": False, "corrected_value": None},
    ):
        result = _invoke(state, repos)
    assert result["caller_profile"]["name"]["status"] == "missing"
    assert result["caller_profile"]["name"]["attempts"] == 1
    assert result["pending_reply"]


def test_confirmation_with_correction_reextracts_and_validates(repos):
    state = _capture_state(
        email={"value": "bad", "confidence": 0.5, "status": "pending_confirm", "attempts": 0, "validated": True}
    )
    with patch(
        "backend.supervisor.tools.confirm_field_answer",
        return_value={"confirmed": False, "corrected_value": "not-an-email"},
    ):
        result = _invoke(state, repos)
    # an invalid correction is caught immediately — explained and re-asked,
    # not silently accepted as "confirmed" just because it's a correction
    assert result["caller_profile"]["email"]["status"] == "missing"
    assert result["caller_profile"]["email"]["attempts"] == 1
    assert "valid" in result["pending_reply"].lower()


def test_persistently_invalid_email_explains_and_escalates_instead_of_looping(repos):
    # regression: an email that can never pass format validation (e.g. no @
    # at all) must be caught immediately at extraction, explained to the
    # caller, and count toward escalation — not loop forever
    state = _capture_state(
        name={"value": "Alex", "confidence": 0.9, "status": "confirmed", "attempts": 0, "validated": True}
    )
    for _ in range(3):
        with patch("backend.supervisor.tools.extract_field", return_value={"value": "manos44", "confidence": 0.9}):
            state = _invoke(state, repos)
        if state["stage"] == "escalation":
            break
        assert state["caller_profile"]["email"]["status"] == "missing"
        assert "valid" in state["pending_reply"].lower()

    assert state["stage"] == "escalation"
    assert state["escalation_reason"] == "capture_failed"


def test_zero_confidence_email_still_explains_and_reprompts(repos):
    # regression: extract_field returning confidence 0 (nothing recognizable
    # as an email said at all) must still get the explanatory reprompt, not
    # silently fall through to a plain "what's your email?" repeat with no
    # indication anything was wrong
    state = _capture_state(
        name={"value": "Alex", "confidence": 0.9, "status": "confirmed", "attempts": 0, "validated": True}
    )
    with patch("backend.supervisor.tools.extract_field", return_value={"value": "", "confidence": 0}):
        result = _invoke(state, repos)
    assert result["caller_profile"]["email"]["status"] == "missing"
    assert result["caller_profile"]["email"]["attempts"] == 1
    assert "valid" in result["pending_reply"].lower()


def test_all_fields_confirmed_transitions_to_booking(repos):
    confirmed = lambda v: {"value": v, "confidence": 0.9, "status": "confirmed", "attempts": 0, "validated": True}
    state = _capture_state(
        name=confirmed("Alex Smith"),
        email=confirmed("a@b.com"),
        phone=confirmed("5551234567"),
        preferred_time=confirmed("Friday 2pm"),
    )
    result = _invoke(state, repos)
    assert result["stage"] == "booking"


def test_llm_failure_returns_fallback_reply_without_crashing(repos):
    state = _capture_state()
    with patch(
        "backend.supervisor.tools.extract_field",
        side_effect=json.JSONDecodeError("truncated", "doc", 0),
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "capture"
    assert result["consecutive_llm_failures"] == 1
    assert result["pending_reply"]
