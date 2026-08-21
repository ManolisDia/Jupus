import json
from unittest.mock import patch

import pytest

from backend.db.repositories import Repositories
from backend.db.repositories.base import SlotAlreadyBookedError
from backend.supervisor.graph import GRAPH
from backend.supervisor.state import new_call_state
from backend.tests.fakes import FakeCallRepository, FakeSlotRepository, FakeTraceRepository

SLOT_A = {"id": 1, "area": "tenancy", "start_time": "2026-09-03T09:00:00", "is_booked": 0}
SLOT_B = {"id": 2, "area": "tenancy", "start_time": "2026-09-03T09:30:00", "is_booked": 0}


@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=FakeSlotRepository(), trace=FakeTraceRepository())


def _booking_state(**overrides):
    state = new_call_state("call-1")
    state["stage"] = "booking"
    state["practice_area"] = "tenancy"
    state["transcript"] = [{"role": "caller", "text": "Thursday morning please", "ts": "t1"}]
    state["caller_profile"]["name"]["value"] = "John Smith"
    state["caller_profile"]["name"]["status"] = "confirmed"
    state["caller_profile"]["email"]["value"] = "j@example.com"
    state["caller_profile"]["email"]["status"] = "confirmed"
    state.update(overrides)
    return state


def _invoke(state, repos):
    return GRAPH.invoke(state, config={"configurable": {"repos": repos}})


def test_free_slot_proposes_and_awaits_confirmation(repos):
    repos.slots.availability_result = SLOT_A
    state = _booking_state()
    with (
        patch("backend.supervisor.tools.extract_datetime", return_value={"date": "2026-09-03", "window": "morning", "confidence": 0.9}),
        patch("backend.supervisor.tools.generate_confirmation_summary", return_value="Sounds right?"),
    ):
        result = _invoke(state, repos)
    assert result["proposed_slot_id"] == SLOT_A["id"]
    assert result["stage"] == "booking"


def test_no_date_given_reprompts_without_proposing_a_slot(repos):
    # regression: extract_datetime confidence 0 with an empty date string
    # must never fall through to check_availability/suggest_alternatives —
    # an empty date passed every "date(start_time) >= ?" filter, which
    # silently proposed a slot the caller never asked for
    state = _booking_state()
    with patch(
        "backend.supervisor.tools.extract_datetime",
        return_value={"date": "", "window": "any", "time": None, "confidence": 0},
    ):
        result = _invoke(state, repos)
    assert result["proposed_slot_id"] is None
    assert result["stage"] == "booking"
    assert repos.slots.book_calls == []


def test_taken_slot_offers_first_alternative(repos):
    repos.slots.availability_result = None
    repos.slots.alternatives_result = [SLOT_A, SLOT_B]
    state = _booking_state()
    with (
        patch("backend.supervisor.tools.extract_datetime", return_value={"date": "2026-09-03", "window": "morning", "confidence": 0.9}),
        patch("backend.supervisor.tools.generate_confirmation_summary", return_value="Sounds right?"),
    ):
        result = _invoke(state, repos)
    assert result["proposed_slot_id"] == SLOT_A["id"]


def test_exact_time_taken_passes_unavailable_note_to_confirmation_summary(repos):
    repos.slots.availability_result = None  # the caller's exact requested time isn't free
    repos.slots.alternatives_result = [SLOT_B]
    state = _booking_state()
    with (
        patch(
            "backend.supervisor.tools.extract_datetime",
            return_value={"date": "2026-09-03", "window": "morning", "time": "10:00", "confidence": 0.9},
        ),
        patch("backend.supervisor.tools.generate_confirmation_summary", return_value="10am is taken, how about this?") as mock_summary,
    ):
        result = _invoke(state, repos)
    assert result["proposed_slot_id"] == SLOT_B["id"]
    args, kwargs = mock_summary.call_args
    assert args[-1] == "10:00" or kwargs.get("unavailable_requested_time") == "10:00"


def test_confirm_needs_clarification_repeats_proposal_without_declining(repos):
    # regression: "what time?" or similar must repeat the proposed-slot
    # summary verbatim, not get treated as a decline (which burns the
    # decline budget toward escalation)
    state = _booking_state(proposed_slot_id=SLOT_A["id"], requested_date="2026-09-03", requested_window="morning")
    state["transcript"].append({"role": "agent", "text": "How about 9:30 AM — does that work?", "ts": "t1.5"})
    state["transcript"].append({"role": "caller", "text": "What time?", "ts": "t2"})
    with patch(
        "backend.supervisor.tools.confirm_booking_answer",
        return_value={"accepted": False, "needs_clarification": True},
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert result["proposed_slot_id"] == SLOT_A["id"]
    assert result.get("declined_slot_ids", []) == []
    assert result["pending_reply"] == "How about 9:30 AM — does that work?"


def test_accepting_proposed_slot_books_and_ends_call(repos):
    state = _booking_state(proposed_slot_id=SLOT_A["id"], requested_date="2026-09-03", requested_window="morning")
    state["transcript"].append({"role": "caller", "text": "yes that works", "ts": "t2"})
    with patch("backend.supervisor.tools.confirm_booking_answer", return_value={"accepted": True}):
        result = _invoke(state, repos)
    assert result["stage"] == "ended"
    assert result["booking_confirmed"] is True
    assert repos.slots.book_calls == [SLOT_A["id"]]


def test_declining_first_slot_asks_what_else_works_instead_of_auto_suggesting(repos):
    # the caller may have a specific day/time in mind, not just "whatever's
    # next chronologically" — a decline asks an open question rather than
    # unilaterally proposing another slot for them to confirm/deny
    state = _booking_state(proposed_slot_id=SLOT_A["id"], requested_date="2026-09-03", requested_window="morning")
    state["transcript"].append({"role": "caller", "text": "no not that one", "ts": "t2"})
    with patch("backend.supervisor.tools.confirm_booking_answer", return_value={"accepted": False}):
        result = _invoke(state, repos)
    assert result["proposed_slot_id"] is None
    assert SLOT_A["id"] in result["declined_slot_ids"]
    assert "what" in result["pending_reply"].lower()


def test_after_decline_next_utterance_reproposes_excluding_declined_slot(repos):
    # once proposed_slot_id resets to None on decline, the next turn's fresh
    # extract_datetime/check_availability call must still exclude the
    # already-declined slot so it's never re-offered
    state = _booking_state(proposed_slot_id=None, requested_date="2026-09-03", requested_window="morning")
    state["declined_slot_ids"] = [SLOT_A["id"]]
    state["transcript"].append({"role": "caller", "text": "how about 10am", "ts": "t3"})
    repos.slots.availability_result = SLOT_B
    with (
        patch(
            "backend.supervisor.tools.extract_datetime",
            return_value={"date": "2026-09-03", "window": "morning", "time": "10:00", "confidence": 0.9},
        ),
        patch("backend.supervisor.tools.generate_confirmation_summary", return_value="How about this one?"),
    ):
        result = _invoke(state, repos)
    assert result["proposed_slot_id"] == SLOT_B["id"]


def test_declining_twice_escalates(repos):
    state = _booking_state(
        proposed_slot_id=SLOT_A["id"],
        requested_date="2026-09-03",
        requested_window="morning",
        declined_slot_ids=[SLOT_B["id"]],
    )
    state["transcript"].append({"role": "caller", "text": "no", "ts": "t2"})
    with patch("backend.supervisor.tools.confirm_booking_answer", return_value={"accepted": False}):
        result = _invoke(state, repos)
    assert result["stage"] == "escalation"
    assert result["escalation_reason"] == "no_acceptable_slot"


def test_no_alternatives_available_escalates_immediately(repos):
    repos.slots.availability_result = None
    repos.slots.alternatives_result = []
    state = _booking_state()
    with patch("backend.supervisor.tools.extract_datetime", return_value={"date": "2026-09-03", "window": "morning", "confidence": 0.9}):
        result = _invoke(state, repos)
    assert result["stage"] == "escalation"
    assert result["escalation_reason"] == "no_acceptable_slot"


def test_race_condition_slot_booked_between_check_and_confirm(repos):
    repos.slots.book_side_effect = SlotAlreadyBookedError("taken")
    repos.slots.availability_result = None
    repos.slots.alternatives_result = [SLOT_B]
    state = _booking_state(proposed_slot_id=SLOT_A["id"], requested_date="2026-09-03", requested_window="morning")
    state["transcript"].append({"role": "caller", "text": "yes", "ts": "t2"})
    with (
        patch("backend.supervisor.tools.confirm_booking_answer", return_value={"accepted": True}),
        patch("backend.supervisor.tools.generate_confirmation_summary", return_value="How about this one?"),
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert result["proposed_slot_id"] == SLOT_B["id"]


def test_llm_failure_returns_fallback_reply_without_crashing(repos):
    state = _booking_state()
    with patch(
        "backend.supervisor.tools.extract_datetime",
        side_effect=json.JSONDecodeError("truncated", "doc", 0),
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert result["consecutive_llm_failures"] == 1
    assert result["pending_reply"]
