"""docs/scenarios.md — the canonical scenarios (S1-S6, plus S7's two variants
added alongside Phase 8's case-research node), mocked-Claude and
driven through backend.dispatcher.process_supervisor_call (the real dispatch
entry point on this branch; docs/scenarios.md calls it `process_supervisor_call`
too, but an earlier draft of this file used a since-removed `on_ask_supervisor`
name — see docs/architecture.md's note on reading phase-doc signatures as
illustrative, not literal) so this exercises the real dispatcher -> graph ->
state path, not just node functions in isolation.

All 6 scenarios are now implemented for real, unblocked by Phase 4's real
booking node (node_booking) and Phase 5's "multiple_areas" classification
value + is_explicit_human_request heuristic. `send_over_bridge` is patched
in each test so replies are captured without a real /bridge WebSocket.
"""

from unittest.mock import patch

import pytest

from backend import dispatcher
from backend.db.repositories import Repositories
from backend.dispatcher import process_supervisor_call
from backend.supervisor.state import CALL_STATES
from backend.tests.fakes import FakeCallRepository, FakeSlotRepository, FakeTraceRepository

SLOT_A = {"id": 1, "area": "tenancy", "start_time": "2026-09-03T14:00:00", "is_booked": 0}
SLOT_B = {"id": 2, "area": "tenancy", "start_time": "2026-09-03T15:00:00", "is_booked": 0}


@pytest.fixture(autouse=True)
def clear_dispatcher_state():
    CALL_STATES.clear()
    dispatcher.LOCKS.clear()
    dispatcher.SPEAKING.clear()
    dispatcher.DEFERRED.clear()
    dispatcher.CONNECTIONS.clear()
    yield
    CALL_STATES.clear()
    dispatcher.LOCKS.clear()
    dispatcher.SPEAKING.clear()
    dispatcher.DEFERRED.clear()
    dispatcher.CONNECTIONS.clear()


@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


@pytest.fixture
def booking_repos():
    return Repositories(calls=FakeCallRepository(), slots=FakeSlotRepository(), trace=FakeTraceRepository())


async def _turn(repos, call_id, tool_call_id, utterance):
    with patch("backend.dispatcher.send_over_bridge"):
        await process_supervisor_call(repos, call_id, tool_call_id, utterance)


async def _await_statute_search(call_id):
    # Phase 8 (case research): mirrors _await_background_verification below
    # — dispatcher.STATUTE_SEARCHES holds a real asyncio.Task, scheduled but
    # not actually run until explicitly awaited; reconciling immediately
    # after lets a test assert on the merged state without needing a whole
    # extra turn just to observe it.
    task = dispatcher.STATUTE_SEARCHES.get(call_id)
    if task:
        await task
        dispatcher._reconcile_statute_search(CALL_STATES[call_id], call_id)


async def _await_background_verification(call_id, field):
    # Phase 7 (optimistic capture): node_capture_fast advancing spawns a
    # background asyncio.Task (dispatcher.FIELD_VERIFICATIONS) rather than
    # calling extract_field synchronously — must be explicitly awaited
    # (still inside whatever `patch(...)` block mocked the Claude call it
    # depends on), since asyncio.create_task only schedules the task to
    # start, it doesn't run it inline. Awaiting alone resolves the task but
    # does NOT merge its result into CALL_STATES — that normally happens
    # lazily, via dispatcher._reconcile_field_verifications at the START of
    # the NEXT real turn (dispatcher._reconcile_before_capture_turn). Doing
    # that reconcile here too lets tests assert on the merged profile state
    # immediately, without needing a full extra turn just to observe it.
    task = dispatcher.FIELD_VERIFICATIONS.get((call_id, field))
    if task:
        await task
        dispatcher._reconcile_field_verifications(CALL_STATES[call_id], call_id)


async def test_scenario_s1_info_only(repos):
    call_id = "scenario-s1"

    # Turn 1: greeting -> routing -> capture, chained into a single dispatch
    # per docs/fixes/2026-08-22-001.md (node_greeting no longer stalls the
    # caller's first real utterance) — classify_practice_area must be mocked
    # from this first turn now, not the second.
    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "employment", "confidence": 0.9},
    ):
        await _turn(
            repos, call_id, "tool-1",
            "I got let go from my job last week and I'm not sure if that was legal.",
        )
    assert CALL_STATES[call_id]["stage"] == "capture"

    # Turn 2: exercise capture with a non-informative reply
    await _turn(repos, call_id, "tool-2", "Just info for now, thanks.")

    final = CALL_STATES[call_id]
    assert final["practice_area"] == "employment"
    # the one legitimate "info_only" exit path doesn't exist yet (no explicit
    # "no thanks" exit in node_capture) — per docs/scenarios.md's own fallback
    # wording, assert the call simply hasn't reached booking/escalation
    assert final["stage"] not in ("booking", "escalation", "ended")


async def test_scenario_s2_happy_path_booking(booking_repos):
    repos = booking_repos
    call_id = "scenario-s2"
    repos.slots.availability_result = SLOT_A

    await _turn(repos, call_id, "tool-1", "I'd like to book a consultation.")
    assert CALL_STATES[call_id]["stage"] == "routing"

    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "tenancy", "confidence": 0.9},
    ):
        await _turn(repos, call_id, "tool-2", "It's about my flat.")
    assert CALL_STATES[call_id]["stage"] == "capture"

    # Phase 7 (optimistic capture): name/email advance instantly — node_capture_fast
    # asks the next field's question with zero Claude calls on the hot path;
    # the real extraction runs in a background task, explicitly awaited here
    # (still inside the patch) since the test can't otherwise observe when
    # asyncio.create_task's scheduled work actually runs.
    with patch("backend.supervisor.tools.extract_field", return_value={"value": "John Smith", "confidence": 0.9}):
        await _turn(repos, call_id, "tool-3", "John Smith")
        await _await_background_verification(call_id, "name")
    assert CALL_STATES[call_id]["caller_profile"]["name"]["status"] == "confirmed"  # 0.9 >= 0.75 auto-confirms
    assert CALL_STATES[call_id]["last_asked_field"] == "email"

    with patch("backend.supervisor.tools.extract_field", return_value={"value": "john@example.com", "confidence": 0.9}):
        await _turn(repos, call_id, "tool-4", "john at example dot com")
        await _await_background_verification(call_id, "email")
    assert CALL_STATES[call_id]["caller_profile"]["email"]["status"] == "pending_confirm"  # always needs read-back
    assert CALL_STATES[call_id]["last_asked_field"] == "phone"

    # phone is FIELD_PRIORITY's last field — no further fast-ask turn exists
    # to run its background verification concurrently with, so this turn
    # processes it live and transitions straight into the confirm/drain
    # phase, batching a "let me just confirm a couple of things" preamble
    # onto the FIRST pending field in canonical order (email, asked before
    # phone) — see docs/phases/phase-7-optimistic-capture.md.
    with (
        patch("backend.supervisor.tools.extract_field", return_value={"value": "5551234567", "confidence": 0.9}),
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say john@example.com?"),
    ):
        await _turn(repos, call_id, "tool-5", "555-123-4567")
    assert CALL_STATES[call_id]["caller_profile"]["phone"]["status"] == "pending_confirm"
    assert CALL_STATES[call_id]["capture_phase"] == "confirm"

    # Drain item 1: confirming email also immediately produces phone's
    # confirm-back in the SAME turn — node_capture's bottom logic (fixed
    # for Phase 7) sees phone is already "pending_confirm" and asks its
    # confirm-back rather than a fresh "what's your phone number" (which
    # would discard the value already captured for it), so this turn needs
    # both tools mocked.
    with (
        patch("backend.supervisor.tools.confirm_field_answer", return_value={"confirmed": True, "corrected_value": None}),
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say 555-123-4567?"),
    ):
        await _turn(repos, call_id, "tool-6", "Yes, that's right.")
    assert CALL_STATES[call_id]["caller_profile"]["email"]["status"] == "confirmed"
    assert CALL_STATES[call_id]["caller_profile"]["phone"]["status"] == "pending_confirm"

    # Drain item 2: confirm phone
    with patch(
        "backend.supervisor.tools.confirm_field_answer",
        return_value={"confirmed": True, "corrected_value": None},
    ):
        await _turn(repos, call_id, "tool-7", "Yes, that's right.")
    assert CALL_STATES[call_id]["stage"] == "research"

    # Phase 8 (case research): S2/S3 stay focused on the booking flow itself
    # (S7 exercises the research node's own mechanics) — a research-skip
    # phrase moves straight to booking with no background search spawned.
    await _turn(repos, call_id, "tool-7b", "Honestly, let's just book me in.")
    assert CALL_STATES[call_id]["stage"] == "booking"

    with (
        patch(
            "backend.supervisor.tools.extract_datetime",
            return_value={"date": "2026-09-03", "window": "afternoon", "time": "14:00", "confidence": 0.9},
        ),
        patch("backend.supervisor.tools.generate_confirmation_summary", return_value="Thursday at 2pm, sound right?"),
    ):
        await _turn(repos, call_id, "tool-8", "Thursday afternoon.")
    assert CALL_STATES[call_id]["proposed_slot_id"] == SLOT_A["id"]

    with patch(
        "backend.supervisor.tools.confirm_booking_answer",
        return_value={"accepted": True, "needs_clarification": False},
    ):
        await _turn(repos, call_id, "tool-9", "Yes, that works.")

    final = CALL_STATES[call_id]
    assert final["stage"] == "ended"
    assert final["booking_confirmed"] is True
    assert repos.slots.book_calls == [SLOT_A["id"]]
    # FakeCallRepository.upsert stores outcome_override verbatim rather than
    # deriving it from state the way SQLiteCallRepository._derive_outcome
    # does (see that function for the real "booked" derivation), so the
    # call-row assertion from docs/scenarios.md isn't meaningful against the
    # fake here — CallState is the strictly stronger/more precise check.
    assert repos.calls.get(call_id)["outcome"] is None


async def test_scenario_s3_slot_conflict_booking(booking_repos):
    repos = booking_repos
    call_id = "scenario-s3"
    # requested slot is taken (10am/day-1, deterministically pre-booked per
    # docs/scenarios.md); check_availability returns None, one alternative offered
    repos.slots.availability_result = None
    repos.slots.alternatives_result = [SLOT_B]

    await _turn(repos, call_id, "tool-1", "I'd like to book a consultation.")

    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "tenancy", "confidence": 0.9},
    ):
        await _turn(repos, call_id, "tool-2", "It's about my flat.")

    with patch("backend.supervisor.tools.extract_field", return_value={"value": "Jane Doe", "confidence": 0.9}):
        await _turn(repos, call_id, "tool-3", "Jane Doe")
        await _await_background_verification(call_id, "name")
    assert CALL_STATES[call_id]["caller_profile"]["name"]["status"] == "confirmed"

    with patch("backend.supervisor.tools.extract_field", return_value={"value": "jane@example.com", "confidence": 0.9}):
        await _turn(repos, call_id, "tool-4", "jane at example dot com")
        await _await_background_verification(call_id, "email")
    assert CALL_STATES[call_id]["caller_profile"]["email"]["status"] == "pending_confirm"

    # phone is FIELD_PRIORITY's last field — processed live, transitioning
    # straight into the confirm/drain phase with email's confirm-back first
    # (canonical FIELD_PRIORITY order — see docs/phases/phase-7-optimistic-capture.md)
    with (
        patch("backend.supervisor.tools.extract_field", return_value={"value": "5559876543", "confidence": 0.9}),
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say jane@example.com?"),
    ):
        await _turn(repos, call_id, "tool-5", "555-987-6543")
    assert CALL_STATES[call_id]["caller_profile"]["phone"]["status"] == "pending_confirm"
    assert CALL_STATES[call_id]["capture_phase"] == "confirm"

    with (
        patch("backend.supervisor.tools.confirm_field_answer", return_value={"confirmed": True, "corrected_value": None}),
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say 555-987-6543?"),
    ):
        await _turn(repos, call_id, "tool-6", "Yes, that's right.")
    assert CALL_STATES[call_id]["caller_profile"]["email"]["status"] == "confirmed"

    with patch(
        "backend.supervisor.tools.confirm_field_answer",
        return_value={"confirmed": True, "corrected_value": None},
    ):
        await _turn(repos, call_id, "tool-7", "Yes, that's right.")
    assert CALL_STATES[call_id]["stage"] == "research"

    await _turn(repos, call_id, "tool-7b", "Honestly, let's just book me in.")
    assert CALL_STATES[call_id]["stage"] == "booking"

    with (
        patch(
            "backend.supervisor.tools.extract_datetime",
            return_value={"date": "2026-09-03", "window": "morning", "time": "10:00", "confidence": 0.9},
        ),
        patch("backend.supervisor.tools.generate_alternative_offer", return_value="3pm instead, sound right?"),
    ):
        await _turn(repos, call_id, "tool-8", "10am tomorrow please.")
    # offered the alternative(s), not the originally-requested (taken) slot
    assert CALL_STATES[call_id]["offered_slots"] == [SLOT_B]

    with patch(
        "backend.supervisor.tools.select_offered_slot",
        return_value={"selected_index": 0, "declined_all": False, "needs_clarification": False},
    ):
        await _turn(repos, call_id, "tool-9", "Sure, that works.")

    final = CALL_STATES[call_id]
    assert final["stage"] == "ended"
    assert final["booking_confirmed"] is True
    assert repos.slots.book_calls == [SLOT_B["id"]]
    # they accepted the first alternative offered, never declined one
    assert final["declined_slot_ids"] == []


async def test_scenario_s4_low_confidence_capture(repos):
    call_id = "scenario-s4"

    # NB: must avoid heuristics.EXPLICIT_REQUEST_PHRASES (e.g. "talk to
    # someone") here, or the dispatcher's deterministic explicit-request
    # check fires and this becomes an S6-style escalation instead of an
    # ordinary routing turn.
    await _turn(repos, call_id, "tool-1", "I think I need some legal advice.")
    assert CALL_STATES[call_id]["stage"] == "routing"

    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "tenancy", "confidence": 0.9},
    ):
        await _turn(repos, call_id, "tool-2", "It's about my flat.")
    assert CALL_STATES[call_id]["stage"] == "capture"

    # garbled name -> medium confidence -> pending_confirm + confirm-back.
    # Phase 13: node_capture's fresh-extraction path now merges extraction
    # and confirm-back phrasing into one call (extract_and_confirm_field),
    # not a separate extract_field + generate_confirm_back pair.
    with patch(
        "backend.supervisor.tools.extract_and_confirm_field",
        return_value={"value": "Alesh", "confidence": 0.4, "confirm_back_phrasing": "Did you say Alesh?"},
    ):
        await _turn(repos, call_id, "tool-3", "uh, Alesh, maybe")
    assert CALL_STATES[call_id]["caller_profile"]["name"]["status"] == "pending_confirm"
    assert CALL_STATES[call_id]["pending_reply"] == "Did you say Alesh?"

    # a clear correction resolves the pending name via confirm_field_answer's
    # corrected_value path (this branch's real node_capture logic — see the
    # module docstring on why this isn't a second extract_field call)
    with patch(
        "backend.supervisor.tools.confirm_field_answer",
        return_value={"confirmed": False, "corrected_value": "Alex Smith"},
    ):
        await _turn(repos, call_id, "tool-4", "No, it's Alex Smith.")
    assert CALL_STATES[call_id]["caller_profile"]["name"]["status"] == "confirmed"

    # garbled email at medium confidence (0.6, below LOW_CONFIDENCE_CONFIRM_
    # THRESHOLD) is now treated the same as an invalid format — re-asked to
    # spell it out, not silently sent to pending_confirm/a soft Claude
    # confirm-back (this refines, not contradicts, docs/DECISIONS.md's
    # "email/phone are always confirmed back regardless of confidence" —
    # that's still true ONCE confidence clears this floor). "email" isn't
    # FIELD_PRIORITY's last field, so this advances optimistically —
    # extract_field runs in the background, explicitly awaited here.
    with patch("backend.supervisor.tools.extract_field", return_value={"value": "alex@example.com", "confidence": 0.6}):
        await _turn(repos, call_id, "tool-5", "alex at example dot com")
        # Let the background task actually finish (so the NEXT turn's own
        # reconcile-before-invoke step discovers it as done), but don't
        # reconcile it here — verification_failed_field is a genuinely
        # single-turn transient signal (dispatcher._reconcile_field_
        # verifications resets it to None every pass), so consuming it in
        # a separate step before the turn that's meant to act on it would
        # just wipe it again before node_capture_fast ever sees it.
        task = dispatcher.FIELD_VERIFICATIONS.get((call_id, "email"))
        if task:
            await task
    assert CALL_STATES[call_id]["last_asked_field"] == "phone"

    # the caller's next utterance ("555-123-4567", intended as phone) is
    # interrupted by the now-finished background failure — node_capture_fast's
    # own reconcile-before-invoke step discovers it and asks to spell out
    # email instead of processing this utterance as phone's answer.
    await _turn(repos, call_id, "tool-6", "555-123-4567")
    mid = CALL_STATES[call_id]
    assert mid["caller_profile"]["email"]["status"] == "missing"
    assert mid["last_asked_field"] == "email"
    assert mid["capture_phase"] == "fast"
    assert "spell" in mid["pending_reply"].lower()

    # caller spells it out clearly -> high confidence, advances (again) to
    # ask about phone, spawning a fresh background check for email.
    with patch("backend.supervisor.tools.extract_field", return_value={"value": "alex@example.com", "confidence": 0.9}):
        await _turn(repos, call_id, "tool-7", "A, L, E, X, at example dot com")
        await _await_background_verification(call_id, "email")
    assert CALL_STATES[call_id]["caller_profile"]["email"]["status"] == "pending_confirm"
    assert CALL_STATES[call_id]["last_asked_field"] == "phone"

    # phone is FIELD_PRIORITY's last field — not part of docs/scenarios.md's
    # original S4 transcript, but structurally required under Phase 7's
    # design: email's confirm-back only happens once the drain/confirm
    # phase starts, which only begins once every field has been fast-asked
    # about, including phone.
    with (
        patch("backend.supervisor.tools.extract_field", return_value={"value": "5551234567", "confidence": 0.9}),
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say alex@example.com?"),
    ):
        await _turn(repos, call_id, "tool-8", "555-123-4567")
    assert CALL_STATES[call_id]["caller_profile"]["phone"]["status"] == "pending_confirm"
    assert CALL_STATES[call_id]["capture_phase"] == "confirm"

    # Drain: confirm email ("yes that's right")
    with (
        patch("backend.supervisor.tools.confirm_field_answer", return_value={"confirmed": True, "corrected_value": None}),
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say 555-123-4567?"),
    ):
        await _turn(repos, call_id, "tool-9", "Yes, that's right.")

    final_profile = CALL_STATES[call_id]["caller_profile"]
    assert final_profile["name"]["status"] == "confirmed"
    assert final_profile["email"]["status"] == "confirmed"


async def test_scenario_s5_model_judged_escalation_multi_area(repos, tmp_path):
    call_id = "scenario-s5"

    # Turn 1: greeting -> routing chained into one dispatch per
    # docs/fixes/2026-08-22-001.md — classify_practice_area must be mocked
    # from this first turn now. Exactly one classification call, no
    # clarifying retry (unlike the "unclear" path), moves straight to the
    # escalation stage within this same turn.
    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "multiple_areas", "confidence": 0.8},
    ):
        await _turn(repos, call_id, "tool-1", "I have an issue with my employer and my visa.")

    mid = CALL_STATES[call_id]
    assert mid["stage"] == "escalation"
    assert mid["escalation_reason"] == "out_of_scope_multi_area"
    assert mid["retry_counts"].get("classification") is None

    # node_escalation itself (summary + handoff note + final "ended" stage)
    # only runs on the graph's NEXT entry, since each dispatcher turn invokes
    # exactly one node (route_by_stage now sees "escalation" and dispatches
    # straight there — this is the same single-node-per-turn behavior S6
    # below relies on, just arriving at the escalation stage one turn later
    # here because the decision to escalate was itself made inside the
    # routing node's own turn).
    with (
        patch.object(dispatcher.tools, "HANDOFFS_DIR", tmp_path),
        patch("backend.supervisor.tools.generate_call_summary", return_value="Multi-area issue, needs a human."),
    ):
        await _turn(repos, call_id, "tool-2", "(silence)")

    final = CALL_STATES[call_id]
    assert final["stage"] == "ended"
    assert final["escalation_reason"] == "out_of_scope_multi_area"
    assert (tmp_path / f"{call_id}.md").exists()


async def test_scenario_s6_explicit_escalation(repos, tmp_path):
    call_id = "scenario-s6"

    with (
        patch.object(dispatcher.tools, "HANDOFFS_DIR", tmp_path),
        patch("backend.supervisor.tools.generate_call_summary", return_value="Caller explicitly asked for a human."),
    ):
        # escalation happens on the FIRST ask_supervisor call, before
        # routing/capture ever run
        await _turn(repos, call_id, "tool-1", "Can you just put me through to a real person?")

    final = CALL_STATES[call_id]
    assert final["stage"] == "ended"
    assert final["escalation_reason"] == "explicit_request"


async def _drive_to_research(repos, call_id):
    """Shared capture flow for S7's two variants — the same sequence as
    S2's up through 'all fields confirmed', landing in stage='research'
    rather than 'booking' now that Phase 8 sits between them."""
    await _turn(repos, call_id, "tool-1", "I'd like to book a consultation.")
    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "tenancy", "confidence": 0.9},
    ):
        await _turn(repos, call_id, "tool-2", "It's about my flat.")
    with patch("backend.supervisor.tools.extract_field", return_value={"value": "Jamie Lee", "confidence": 0.9}):
        await _turn(repos, call_id, "tool-3", "Jamie Lee")
        await _await_background_verification(call_id, "name")
    with patch("backend.supervisor.tools.extract_field", return_value={"value": "jamie@example.com", "confidence": 0.9}):
        await _turn(repos, call_id, "tool-4", "jamie at example dot com")
        await _await_background_verification(call_id, "email")
    with (
        patch("backend.supervisor.tools.extract_field", return_value={"value": "5551239876", "confidence": 0.9}),
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say jamie@example.com?"),
    ):
        await _turn(repos, call_id, "tool-5", "555-123-9876")
    with (
        patch("backend.supervisor.tools.confirm_field_answer", return_value={"confirmed": True, "corrected_value": None}),
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say 555-123-9876?"),
    ):
        await _turn(repos, call_id, "tool-6", "Yes, that's right.")
    with patch("backend.supervisor.tools.confirm_field_answer", return_value={"confirmed": True, "corrected_value": None}):
        await _turn(repos, call_id, "tool-7", "Yes, that's right.")
    assert CALL_STATES[call_id]["stage"] == "research"
    assert CALL_STATES[call_id]["research_phase"] == "gather"


async def test_scenario_s7_case_research_with_citation(booking_repos):
    repos = booking_repos
    call_id = "scenario-s7-citation"
    repos.slots.availability_result = SLOT_A

    await _drive_to_research(repos, call_id)

    fake_candidate = {
        "id": "tenancy-poe1977-s5", "citation": "Protection from Eviction Act 1977, s.5",
        "text": "...", "score": 5.0, "jurisdiction": "England & Wales", "topic_tags": [],
    }
    await _turn(
        repos, call_id, "tool-7b",
        "My landlord is trying to evict me tomorrow without giving me any notice.",
    )
    assert CALL_STATES[call_id]["research_phase"] == "deliver"
    assert CALL_STATES[call_id]["stage"] == "research"

    with (
        patch("backend.supervisor.tools.search_statute_candidates", return_value=[fake_candidate]),
        patch(
            "backend.supervisor.tools.ground_statute_citation",
            return_value={
                "selected_id": "tenancy-poe1977-s5",
                "spoken_framing": "Landlords generally need to give four weeks' written notice before evicting a tenant.",
            },
        ),
    ):
        await _await_statute_search(call_id)

    await _turn(repos, call_id, "tool-8", "No, nothing in writing, they just showed up and told me to leave.")
    mid = CALL_STATES[call_id]
    assert mid["stage"] == "booking"
    assert "four weeks" in mid["pending_reply"]
    assert "not legal advice" in mid["pending_reply"]

    with (
        patch(
            "backend.supervisor.tools.extract_datetime",
            return_value={"date": "2026-09-03", "window": "afternoon", "time": "14:00", "confidence": 0.9},
        ),
        patch("backend.supervisor.tools.generate_confirmation_summary", return_value="Thursday at 2pm, sound right?"),
    ):
        await _turn(repos, call_id, "tool-9", "Thursday afternoon.")
    with patch(
        "backend.supervisor.tools.confirm_booking_answer",
        return_value={"accepted": True, "needs_clarification": False},
    ):
        await _turn(repos, call_id, "tool-10", "Yes, that works.")

    final = CALL_STATES[call_id]
    assert final["stage"] == "ended"
    assert final["booking_confirmed"] is True
    # research must not change the booking outcome, only add turns before it
    assert repos.slots.book_calls == [SLOT_A["id"]]


async def test_scenario_s7_case_research_no_citation(booking_repos):
    repos = booking_repos
    call_id = "scenario-s7-no-citation"
    repos.slots.availability_result = SLOT_A

    await _drive_to_research(repos, call_id)

    await _turn(repos, call_id, "tool-7b", "Just wanted to ask about your general process, really.")
    assert CALL_STATES[call_id]["research_phase"] == "deliver"

    # BM25 finds nothing relevant at all — the grounding call is never even
    # reached (Decision 2, docs/phases/phase-8-legal-research.md).
    with patch("backend.supervisor.tools.search_statute_candidates", return_value=[]) as mock_search, patch(
        "backend.supervisor.tools.ground_statute_citation"
    ) as mock_ground:
        await _await_statute_search(call_id)
    mock_search.assert_called_once()
    mock_ground.assert_not_called()

    await _turn(repos, call_id, "tool-8", "Nothing specific, just wanted to understand my options.")
    mid = CALL_STATES[call_id]
    assert mid["stage"] == "booking"
    assert "not legal advice" not in mid["pending_reply"]
    assert "day and time" in mid["pending_reply"].lower()

    with (
        patch(
            "backend.supervisor.tools.extract_datetime",
            return_value={"date": "2026-09-03", "window": "afternoon", "time": "14:00", "confidence": 0.9},
        ),
        patch("backend.supervisor.tools.generate_confirmation_summary", return_value="Thursday at 2pm, sound right?"),
    ):
        await _turn(repos, call_id, "tool-9", "Thursday afternoon.")
    with patch(
        "backend.supervisor.tools.confirm_booking_answer",
        return_value={"accepted": True, "needs_clarification": False},
    ):
        await _turn(repos, call_id, "tool-10", "Yes, that works.")

    final = CALL_STATES[call_id]
    assert final["stage"] == "ended"
    assert final["booking_confirmed"] is True
    assert repos.slots.book_calls == [SLOT_A["id"]]
