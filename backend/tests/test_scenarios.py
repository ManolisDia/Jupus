"""docs/scenarios.md — the 6 canonical scenarios (S1-S6), mocked-Claude and
driven through backend.dispatcher.on_ask_supervisor (the real dispatch entry
point on this branch; docs/scenarios.md calls it `process_supervisor_call`,
a name from an earlier draft — see docs/architecture.md's note on reading
phase-doc signatures as illustrative, not literal) so this exercises the real
dispatcher -> graph -> state path, not just node functions in isolation.

STATUS (see phase-6a-observability.md / the task's final report for detail):
only S1 and S4 are buildable on this branch today. S2/S3 need Phase 4's real
booking node (node_booking here is still a stub with no check_availability/
suggest_alternative_slots/book_consultation calls at all). S5 needs
classify_practice_area's schema to support a 5th "multiple_areas" value,
which docs/PLAN.md documents as a *Phase 5* addition, not yet present in
tools.CLASSIFY_SCHEMA. S6 needs a deterministic `is_explicit_human_request`
heuristic (heuristics.py), which is also Phase 5 scope and doesn't exist yet.
Faking any of these against the current stub nodes would produce a green test
proving nothing real — they are left as explicit skips instead, each with its
own reason, per the task's instruction not to fake Phase 4/5-dependent pieces.

Also note: no file outside Phase 4/5's booking/escalation nodes currently
calls `repos.calls.upsert(...)` on every turn (docs/PLAN.md's call-sequence
item 12) — only `mark_call_abandoned` does, for the disconnect path. So
`calls.outcome` assertions from docs/scenarios.md aren't meaningful yet for
S1 on this branch; S1 below asserts on CallState directly instead, which is
the strictly stronger/more precise check available today.
"""

from unittest.mock import patch

import pytest

from backend.db.repositories import Repositories
from backend.dispatcher import on_ask_supervisor
from backend.supervisor.state import CALL_STATES
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


@pytest.fixture(autouse=True)
def clear_call_states():
    CALL_STATES.clear()
    yield
    CALL_STATES.clear()


@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


async def test_scenario_s1_info_only(repos):
    call_id = "scenario-s1"

    # Turn 1: greeting -> routing (greeting node makes no Claude call itself)
    await on_ask_supervisor(
        repos, call_id, "tool-1", "wants info",
        "I got let go from my job last week and I'm not sure if that was legal.",
    )
    assert CALL_STATES[call_id]["stage"] == "routing"

    # Turn 2: routing -> capture, classify_practice_area mocked per docs/scenarios.md
    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "employment", "confidence": 0.9},
    ):
        await on_ask_supervisor(repos, call_id, "tool-2", "continuing", "Just info for now, thanks.")

    final = CALL_STATES[call_id]
    assert final["practice_area"] == "employment"
    # the one legitimate "info_only" exit path doesn't exist yet (no explicit
    # "no thanks" exit in node_capture) — per docs/scenarios.md's own fallback
    # wording, assert the call simply hasn't reached booking/escalation
    assert final["stage"] not in ("booking", "escalation", "ended")


@pytest.mark.skip(reason="blocked on Phase 4 — node_booking is still a stub, no real booking logic to drive S2 through")
def test_scenario_s2_happy_path_booking():
    ...


@pytest.mark.skip(reason="blocked on Phase 4 — node_booking is still a stub, no conflict/alternative-slot logic to drive S3 through")
def test_scenario_s3_slot_conflict_booking():
    ...


async def test_scenario_s4_low_confidence_capture(repos):
    call_id = "scenario-s4"

    await on_ask_supervisor(repos, call_id, "tool-1", "wants booking", "I need to talk to someone.")
    assert CALL_STATES[call_id]["stage"] == "routing"

    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "tenancy", "confidence": 0.9},
    ):
        await on_ask_supervisor(repos, call_id, "tool-2", "continuing", "It's about my flat.")
    assert CALL_STATES[call_id]["stage"] == "capture"

    # garbled name -> medium confidence -> pending_confirm + confirm-back
    with (
        patch("backend.supervisor.tools.extract_field", return_value={"value": "Alesh", "confidence": 0.4}),
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say Alesh?") as mock_confirm_back,
    ):
        await on_ask_supervisor(repos, call_id, "tool-3", "capture", "uh, Alesh, maybe")
    assert CALL_STATES[call_id]["caller_profile"]["name"]["status"] == "pending_confirm"

    # a clear correction resolves the pending name via confirm_field_answer's
    # corrected_value path (this branch's real node_capture logic — see the
    # module docstring on why this isn't a second extract_field call)
    with patch(
        "backend.supervisor.tools.confirm_field_answer",
        return_value={"confirmed": False, "corrected_value": "Alex Smith"},
    ):
        await on_ask_supervisor(repos, call_id, "tool-4", "capture", "No, it's Alex Smith.")
    assert CALL_STATES[call_id]["caller_profile"]["name"]["status"] == "confirmed"

    # garbled email -> always pending_confirm regardless of confidence (email/
    # phone are never auto-trusted, per docs/DECISIONS.md)
    with (
        patch("backend.supervisor.tools.extract_field", return_value={"value": "alex@example.com", "confidence": 0.6}),
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say alex@example.com?"),
    ):
        await on_ask_supervisor(repos, call_id, "tool-5", "capture", "alex at example dot com")
    assert CALL_STATES[call_id]["caller_profile"]["email"]["status"] == "pending_confirm"

    # "yes that's right"
    with patch(
        "backend.supervisor.tools.confirm_field_answer",
        return_value={"confirmed": True, "corrected_value": None},
    ):
        await on_ask_supervisor(repos, call_id, "tool-6", "capture", "Yes, that's right.")

    final_profile = CALL_STATES[call_id]["caller_profile"]
    assert final_profile["name"]["status"] == "confirmed"
    assert final_profile["email"]["status"] == "confirmed"
    mock_confirm_back.assert_called_once()


@pytest.mark.skip(
    reason=(
        "blocked on Phase 5 — classify_practice_area's schema has no "
        "'multiple_areas' value yet (docs/PLAN.md documents this as a Phase 5 "
        "addition), so out_of_scope_multi_area escalation can't fire for real"
    )
)
def test_scenario_s5_model_judged_escalation_multi_area():
    ...


@pytest.mark.skip(
    reason=(
        "blocked on Phase 5 — no deterministic is_explicit_human_request "
        "heuristic exists yet (heuristics.py is Phase 5 scope), so an "
        "explicit-request escalation can't be triggered without an LLM guess"
    )
)
def test_scenario_s6_explicit_escalation():
    ...
