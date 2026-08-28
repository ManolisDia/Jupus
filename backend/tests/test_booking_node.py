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
SLOT_C = {"id": 3, "area": "tenancy", "start_time": "2026-09-03T10:00:00", "is_booked": 0}


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


def test_taken_slot_offers_up_to_three_alternatives(repos):
    repos.slots.availability_result = None
    repos.slots.alternatives_result = [SLOT_A, SLOT_B, SLOT_C]
    state = _booking_state()
    with patch(
        "backend.supervisor.tools.extract_datetime",
        return_value={"date": "2026-09-03", "window": "morning", "time": "16:00", "confidence": 0.9},
    ):
        result = _invoke(state, repos)
    assert result["proposed_slot_id"] is None
    assert result["offered_slots"] == [SLOT_A, SLOT_B, SLOT_C]
    assert "9" in result["pending_reply"] and "9:30" in result["pending_reply"]


def test_exact_time_taken_passes_unavailable_note_to_alternative_offer(repos):
    repos.slots.availability_result = None  # the caller's exact requested time isn't free
    repos.slots.alternatives_result = [SLOT_B]
    state = _booking_state()
    with (
        patch(
            "backend.supervisor.tools.extract_datetime",
            return_value={"date": "2026-09-03", "window": "morning", "time": "10:00", "confidence": 0.9},
        ),
        patch(
            "backend.supervisor.tools.generate_alternative_offer",
            return_value="10am is taken, how about this?",
        ) as mock_offer,
    ):
        result = _invoke(state, repos)
    assert result["offered_slots"] == [SLOT_B]
    args, kwargs = mock_offer.call_args
    assert args[-1] == "10:00" or kwargs.get("unavailable_requested_time") == "10:00"


def test_selecting_an_offered_alternative_books_and_ends_call(repos):
    state = _booking_state(
        offered_slots=[SLOT_A, SLOT_B, SLOT_C], requested_date="2026-09-03", requested_window="morning"
    )
    state["transcript"].append({"role": "caller", "text": "9:30 works", "ts": "t2"})
    with patch(
        "backend.supervisor.tools.select_offered_slot",
        return_value={"selected_index": 1, "declined_all": False, "needs_clarification": False},
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "ended"
    assert result["booking_confirmed"] is True
    assert result["offered_slots"] is None
    assert repos.slots.book_calls == [SLOT_B["id"]]
    # sqlite_calls.py's upsert derives the persisted booking_slot_id from
    # this field whenever booking_confirmed is True — must be set even
    # though this path never goes through the propose/confirm cycle
    assert result["proposed_slot_id"] == SLOT_B["id"]


def test_declining_all_offered_alternatives_asks_for_other_time_and_does_not_escalate(repos):
    state = _booking_state(
        offered_slots=[SLOT_A, SLOT_B, SLOT_C], requested_date="2026-09-03", requested_window="morning"
    )
    state["transcript"].append({"role": "caller", "text": "none of those work", "ts": "t2"})
    with patch(
        "backend.supervisor.tools.select_offered_slot",
        return_value={"selected_index": None, "declined_all": True, "needs_clarification": False},
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert result["offered_slots"] is None
    assert set(result["declined_slot_ids"]) == {SLOT_A["id"], SLOT_B["id"], SLOT_C["id"]}
    assert "what" in result["pending_reply"].lower()


def test_declined_alternatives_excluded_from_next_search_and_loop_continues_indefinitely(repos):
    # regression: this path must never cap out and escalate purely from
    # repeated declines — it should keep looping until the caller agrees,
    # unlike the single-exact-match-proposal decline path below
    state = _booking_state(requested_date="2026-09-03", requested_window="morning")
    state["declined_slot_ids"] = [SLOT_A["id"], SLOT_B["id"], SLOT_C["id"]]
    state["transcript"].append({"role": "caller", "text": "how about next week", "ts": "t3"})
    repos.slots.availability_result = None
    repos.slots.alternatives_result = []  # genuinely nothing left this round
    with patch(
        "backend.supervisor.tools.extract_datetime",
        return_value={"date": "2026-09-10", "window": "any", "time": None, "confidence": 0.9},
    ):
        result = _invoke(state, repos)
    # only escalates because suggest_alternatives itself came back empty,
    # not because of how many rounds of declining already happened
    assert result["stage"] == "escalation"
    assert result["escalation_reason"] == "no_acceptable_slot"


def test_out_of_range_selected_index_treated_as_decline(repos):
    # defensive guard: an LLM-returned index outside the offered list must
    # never be trusted, even though the prompt forbids it
    state = _booking_state(
        offered_slots=[SLOT_A, SLOT_B], requested_date="2026-09-03", requested_window="morning"
    )
    state["transcript"].append({"role": "caller", "text": "the third one", "ts": "t2"})
    with patch(
        "backend.supervisor.tools.select_offered_slot",
        return_value={"selected_index": 5, "declined_all": False, "needs_clarification": False},
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert result["offered_slots"] is None
    assert repos.slots.book_calls == []


def test_selecting_offered_alternative_needs_clarification_repeats_offer(repos):
    state = _booking_state(
        offered_slots=[SLOT_A, SLOT_B], requested_date="2026-09-03", requested_window="morning"
    )
    state["transcript"].append({"role": "agent", "text": "Do any of those work for you?", "ts": "t1.5"})
    state["transcript"].append({"role": "caller", "text": "what?", "ts": "t2"})
    with patch(
        "backend.supervisor.tools.select_offered_slot",
        return_value={"selected_index": None, "declined_all": False, "needs_clarification": True},
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "booking"
    # Re-asks with the times themselves rather than replaying the previous
    # reply verbatim — that one opened with "that time at 4PM is already
    # booked", which is stale on the repeat and reads as `repetition`.
    assert "9AM" in result["pending_reply"] and "9:30AM" in result["pending_reply"]
    assert "already booked" not in result["pending_reply"]
    # still an offer, so the caller can answer it next turn
    assert result["offered_slots"] == [SLOT_A, SLOT_B]
    assert repos.slots.book_calls == []


def test_race_condition_selecting_offered_slot_booked_between_offer_and_pick(repos):
    repos.slots.book_side_effect = SlotAlreadyBookedError("taken")
    repos.slots.alternatives_result = [SLOT_C]
    state = _booking_state(
        offered_slots=[SLOT_A, SLOT_B], requested_date="2026-09-03", requested_window="morning"
    )
    state["transcript"].append({"role": "caller", "text": "9am works", "ts": "t2"})
    with (
        patch(
            "backend.supervisor.tools.select_offered_slot",
            return_value={"selected_index": 0, "declined_all": False, "needs_clarification": False},
        ),
        patch("backend.supervisor.tools.generate_alternative_offer", return_value="How about this instead?"),
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert result["offered_slots"] == [SLOT_C]


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
        patch("backend.supervisor.tools.generate_alternative_offer", return_value="How about this one?"),
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert result["offered_slots"] == [SLOT_B]


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


def test_counter_offer_during_alternatives_is_looked_up_not_re_read_back(repos):
    # Regression: the caller answered an offer of 9/9:30/10 with "can you do
    # Friday at 3pm?". select_offered_slot had no outcome for a counter-offer,
    # so it returned needs_clarification and booking read the identical three
    # slots back — twice. A new time must be looked up like any other.
    repos.slots.availability_result = SLOT_C
    state = _booking_state(
        offered_slots=[SLOT_A, SLOT_B], requested_date="2026-09-03", requested_window="morning"
    )
    state["transcript"].append({"role": "caller", "text": "Can you do Friday at 3pm?", "ts": "t2"})
    with (
        patch(
            "backend.supervisor.tools.select_offered_slot",
            return_value={
                "selected_index": None, "declined_all": False,
                "needs_clarification": False, "proposed_new_time": True,
            },
        ),
        patch(
            "backend.supervisor.tools.extract_datetime",
            return_value={"date": "2026-09-04", "window": "afternoon", "time": "15:00", "confidence": 0.9},
        ),
        patch("backend.supervisor.tools.generate_confirmation_summary", return_value="Friday 3PM then?"),
    ):
        result = _invoke(state, repos)
    assert result["proposed_slot_id"] == SLOT_C["id"]
    assert result["pending_reply"] == "Friday 3PM then?"
    # the previous round's offer must not survive, or the next turn would go
    # straight back into select_offered_slot instead of confirm_booking_answer
    assert result["offered_slots"] is None
    # asking for 3pm is not refusing 9/9:30 — the caller has to be able to
    # circle back to them, which excluding them here would prevent
    assert result["declined_slot_ids"] == []


def test_counter_offer_to_a_taken_time_offers_fresh_alternatives(repos):
    repos.slots.availability_result = None
    repos.slots.alternatives_result = [SLOT_C]
    state = _booking_state(
        offered_slots=[SLOT_A, SLOT_B], requested_date="2026-09-03", requested_window="morning"
    )
    state["transcript"].append({"role": "caller", "text": "anything Friday afternoon?", "ts": "t2"})
    with (
        patch(
            "backend.supervisor.tools.select_offered_slot",
            return_value={
                "selected_index": None, "declined_all": False,
                "needs_clarification": False, "proposed_new_time": True,
            },
        ),
        patch(
            "backend.supervisor.tools.extract_datetime",
            return_value={"date": "2026-09-04", "window": "afternoon", "time": "15:00", "confidence": 0.9},
        ),
    ):
        result = _invoke(state, repos)
    # the new round's list replaces the old one rather than being cleared
    assert result["offered_slots"] == [SLOT_C]
    assert result["declined_slot_ids"] == []
    assert "already booked" in result["pending_reply"]


def test_counter_offer_with_unintelligible_time_reprompts_without_a_bogus_slot(repos):
    # _handle_time_request's low-confidence guard has to still apply when it is
    # reached from the offer branch, not just from a cold "what day works?".
    state = _booking_state(
        offered_slots=[SLOT_A, SLOT_B], requested_date="2026-09-03", requested_window="morning"
    )
    state["transcript"].append({"role": "caller", "text": "uhh sometime ideally", "ts": "t2"})
    with (
        patch(
            "backend.supervisor.tools.select_offered_slot",
            return_value={
                "selected_index": None, "declined_all": False,
                "needs_clarification": False, "proposed_new_time": True,
            },
        ),
        patch(
            "backend.supervisor.tools.extract_datetime",
            return_value={"date": None, "window": None, "confidence": 0.0},
        ),
    ):
        result = _invoke(state, repos)
    assert result["proposed_slot_id"] is None
    assert repos.slots.book_calls == []
    assert "didn't catch" in result["pending_reply"]
    assert result["offered_slots"] is None


def test_counter_offer_llm_failure_keeps_the_offer_on_the_table(repos):
    # The fallback asks the caller to repeat themselves, so the offer has to
    # survive: a repeated "let's go with ten" needs select_offered_slot, and
    # would be near-meaningless input to extract_datetime. A failed turn must
    # not change which question the next turn is answering.
    from backend.supervisor.llm_utils import LLMCallFailed

    state = _booking_state(
        offered_slots=[SLOT_A, SLOT_B], requested_date="2026-09-03", requested_window="morning"
    )
    state["transcript"].append({"role": "caller", "text": "how about 3pm", "ts": "t2"})
    with (
        patch(
            "backend.supervisor.tools.select_offered_slot",
            return_value={
                "selected_index": None, "declined_all": False,
                "needs_clarification": False, "proposed_new_time": True,
            },
        ),
        patch("backend.supervisor.tools.extract_datetime", side_effect=LLMCallFailed("boom")),
    ):
        result = _invoke(state, repos)
    assert result["offered_slots"] == [SLOT_A, SLOT_B]
    assert result["consecutive_llm_failures"] == 1
    assert repos.slots.book_calls == []


# --- the "I didn't catch a date" re-ask must terminate -----------------


def test_unparseable_time_escalates_on_the_third_try_instead_of_looping(repos):
    # Live call (docs/fixes/2026-08-28-007.md): a caller who wanted
    # something other than a booking got "Sorry, I didn't catch a date or
    # time there" on a loop — this branch was the only "I didn't understand
    # you" path in the graph with no ceiling — until they hung up.
    state = _booking_state()
    state["transcript"][-1]["text"] = "I just want to speak to a real human."
    state["retry_counts"] = {"booking_datetime": 2}
    with patch(
        "backend.supervisor.tools.extract_datetime",
        return_value={"date": "", "window": "any", "time": None, "confidence": 0.0},
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "escalation"
    assert result["escalation_reason"] == "booking_failed"
    assert result["retry_counts"]["booking_datetime"] == 3


def test_unparseable_time_counts_up_before_escalating(repos):
    state = _booking_state()
    state["transcript"][-1]["text"] = "hmm"
    with patch(
        "backend.supervisor.tools.extract_datetime",
        return_value={"date": "", "window": "any", "time": None, "confidence": 0.0},
    ):
        result = _invoke(state, repos)
    assert result.get("stage", "booking") == "booking"
    assert "didn\'t catch a date" in result["pending_reply"]
    assert result["retry_counts"]["booking_datetime"] == 1


def test_a_understood_time_clears_earlier_confusion(repos):
    # Two mumbles then a real answer must not leave the caller one bad
    # utterance away from an escalation later in the same booking.
    repos.slots.availability_result = SLOT_A
    state = _booking_state()
    state["retry_counts"] = {"booking_datetime": 2}
    state["transcript"][-1]["text"] = "Thursday afternoon"
    with patch("backend.supervisor.tools.generate_confirmation_summary", return_value="Sounds right?"), patch(
        "backend.supervisor.tools.extract_datetime",
        return_value={"date": "2026-09-03", "window": "afternoon", "time": None, "confidence": 0.9},
    ):
        result = _invoke(state, repos)
    assert result["retry_counts"]["booking_datetime"] == 0
