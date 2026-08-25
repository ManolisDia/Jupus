"""Phase 7 (optimistic capture) — direct unit coverage for node_capture_fast's
own mechanics, complementing the end-to-end scenario tests in
test_scenarios.py. See docs/phases/phase-7-optimistic-capture.md."""

from unittest.mock import patch

import pytest

from backend import dispatcher
from backend.db.repositories import Repositories
from backend.supervisor import graph
from backend.supervisor.graph import GRAPH
from backend.supervisor.state import CALL_STATES, FIELD_PRIORITY, new_call_state
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


@pytest.fixture(autouse=True)
def clear_field_verifications():
    dispatcher.FIELD_VERIFICATIONS.clear()
    yield
    dispatcher.FIELD_VERIFICATIONS.clear()


def _fast_state(last_asked_field, **profile_overrides):
    state = new_call_state("call-1")
    state["stage"] = "capture"
    state["capture_phase"] = "fast"
    state["practice_area"] = "employment"
    state["last_asked_field"] = last_asked_field
    state["transcript"] = [{"role": "caller", "text": "some utterance", "ts": "t"}]
    # Realistic mid-flow default: every field the fast pass has already
    # moved past (i.e. earlier than last_asked_field in FIELD_PRIORITY) is
    # already confirmed — matches how this state actually looks in
    # production, unless a test explicitly overrides it.
    if last_asked_field is not None:
        for f in FIELD_PRIORITY[: FIELD_PRIORITY.index(last_asked_field)]:
            state["caller_profile"][f] = {
                "value": f"placeholder-{f}", "confidence": 0.9, "status": "confirmed", "attempts": 0, "validated": True,
            }
    state["caller_profile"].update(profile_overrides)
    return state


def _invoke(state, repos):
    return GRAPH.invoke(state, config={"configurable": {"repos": repos}})


def test_fast_pass_asks_next_field_with_zero_llm_calls(repos):
    state = _fast_state("name")
    state["transcript"][-1]["text"] = "Manos"
    with patch("backend.supervisor.tools.extract_field") as mock_extract:
        result = _invoke(state, repos)
    mock_extract.assert_not_called()
    assert result["last_asked_field"] == "email"
    assert result["background_verify_field"] == "name"
    assert "email" in result["pending_reply"].lower()


def test_fast_pass_first_field_has_no_gate_or_urgent_check(repos):
    # last_asked_field is None on the very first fast-pass turn (name was
    # already asked by node_routing's own transition reply, not this node) —
    # must still just ask the next field, not crash on a None field lookup.
    state = _fast_state(None)
    result = _invoke(state, repos)
    assert result["last_asked_field"] == "name"
    assert "background_verify_field" not in result or result["background_verify_field"] is None


@pytest.mark.parametrize("utterance", ["what do you need that for?", "wait, actually", "can you repeat that?"])
def test_fast_pass_gate_falls_back_on_tangent(repos, utterance):
    # A gate fallback routes into node_capture's own fresh-extraction
    # branch (_fallback_to_real_capture -> node_capture), which since
    # Phase 13 calls the merged extract_and_confirm_field, not extract_field.
    state = _fast_state("email")
    state["transcript"][-1]["text"] = utterance
    with patch(
        "backend.supervisor.tools.extract_and_confirm_field",
        return_value={"value": None, "confidence": 0.0, "confirm_back_phrasing": ""},
    ):
        result = _invoke(state, repos)
    # fell back to real node_capture — last_asked_field re-synced to "email"
    # (still missing/unresolved), no optimistic advance to "phone"
    assert result["last_asked_field"] == "email"


def test_fast_pass_gate_falls_back_on_bad_shape(repos):
    # asked about email, but this utterance has no @/at/dot at all
    state = _fast_state("email")
    state["transcript"][-1]["text"] = "yes that's correct"
    with patch(
        "backend.supervisor.tools.extract_and_confirm_field",
        return_value={"value": None, "confidence": 0.0, "confirm_back_phrasing": ""},
    ):
        result = _invoke(state, repos)
    assert result["last_asked_field"] == "email"


def test_fast_pass_gate_falls_back_on_explicit_human_request(repos):
    # dispatcher.process_supervisor_call already checks is_explicit_human_request
    # and escalates BEFORE the graph ever runs in production — this gate
    # entry is defense in depth for if node_capture_fast is ever reached
    # directly with such an utterance regardless: it must not optimistically
    # guess this answers "name", falling back to the real (non-escalating,
    # since node_capture itself has no concept of this heuristic) path
    # instead of advancing straight past it.
    state = _fast_state("name")
    state["transcript"][-1]["text"] = "can I just talk to a human"
    with patch(
        "backend.supervisor.tools.extract_and_confirm_field",
        return_value={"value": None, "confidence": 0.0, "confirm_back_phrasing": ""},
    ):
        result = _invoke(state, repos)
    assert result["last_asked_field"] != "email"


def test_gate_fallback_does_not_confirm_an_unrelated_earlier_pending_field(repos):
    # Regression for a real, live-reproduced bug: email was already
    # pending_confirm (via background verification, its confirm-back not
    # yet spoken — that's batched for the later drain phase), and the
    # caller, right after being asked for their phone number, repeated
    # their email instead (plausible human mistake: they kept talking
    # about email a beat after the fast pass had already moved on).
    # "email at ... dot com" doesn't look like a phone number, so the gate
    # correctly falls back to real node_capture — but the OLD behavior
    # let node_capture pick up email (the ONLY pending_confirm field, in
    # FIELD_PRIORITY order) as if IT were what this utterance answered,
    # silently marking email "confirmed" via confirm_field_answer without
    # ever having spoken its confirm-back question aloud, and discarding
    # the caller's actual (non-)answer to phone entirely. Must instead
    # treat this as a (failed) attempt at "phone" — email stays untouched,
    # to be asked about for real later.
    state = _fast_state(
        "phone",
        email={"value": "manos@gmail.com", "confidence": 0.9, "status": "pending_confirm", "attempts": 0, "validated": True},
    )
    state["transcript"][-1]["text"] = "Manos at gmail dot com."
    with (
        patch(
            "backend.supervisor.tools.extract_and_confirm_field",
            return_value={"value": "", "confidence": 0.0, "confirm_back_phrasing": ""},
        ) as mock_extract,
        patch("backend.supervisor.tools.confirm_field_answer") as mock_confirm,
    ):
        result = _invoke(state, repos)
    mock_confirm.assert_not_called()
    mock_extract.assert_called_once_with("Manos at gmail dot com.", "phone")
    assert result["caller_profile"]["email"]["status"] == "pending_confirm"  # untouched, not silently confirmed
    assert result["caller_profile"]["email"]["value"] == "manos@gmail.com"
    assert "one digit at a time" in result["pending_reply"].lower()  # phone re-ask (SPELL_OUT_REPLIES)


def test_delayed_failure_interrupts_and_reasks_without_touching_current_utterance(repos):
    # Regression test for a real, live-reproduced bug: a background
    # verification failure is NEVER for the currently-asked field, by
    # construction — a field only gets a background check once
    # node_capture_fast has already advanced last_asked_field past it (the
    # check is spawned in the same return that produces the NEXT field's
    # question). So by the time "email" fails in the background, we've
    # already moved on to asking about "phone" — this utterance answers
    # "phone", not "email". The old behavior (falling back to real
    # node_capture, treating this utterance as email's re-extraction
    # attempt) silently misattributed unrelated text to the wrong field.
    state = _fast_state(
        "phone",  # already past "email" — override _fast_state's auto-confirm default for it
        email={"value": None, "confidence": 0.0, "status": "missing", "attempts": 0, "validated": True},
    )
    state["verification_failed_field"] = "email"
    state["transcript"][-1]["text"] = "555-123-4567"  # answering "phone", NOT "email"
    with patch("backend.supervisor.tools.extract_field") as mock_extract:
        result = _invoke(state, repos)
    # extract_field must never be called with "555-123-4567" against
    # "email" — the utterance is never touched at all this turn
    mock_extract.assert_not_called()
    assert result["last_asked_field"] == "email"
    assert "email" in result["pending_reply"].lower()
    assert result["caller_profile"]["email"]["status"] == "missing"  # untouched, not corrupted with phone's answer


def test_next_fast_field_skips_already_resolved_fields_not_just_positional():
    # After a delayed-failure interrupt (see above) resumes the fast pass
    # on an earlier field, the immediately-following FIELD_PRIORITY
    # position may already be resolved (its own background check
    # succeeded while the interrupt was being handled) — must be skipped,
    # not re-asked. E.g. "name" was being re-asked after a delayed
    # failure; "email" already resolved in the meantime; "phone" is next.
    profile = {
        "name": {"value": None, "confidence": 0.0, "status": "missing", "attempts": 0, "validated": True},
        "email": {"value": "manos@gmail.com", "confidence": 0.9, "status": "pending_confirm", "attempts": 0, "validated": True},
        "phone": {"value": None, "confidence": 0.0, "status": "missing", "attempts": 0, "validated": True},
    }
    assert graph._next_fast_field(profile, "name") == "phone"


def test_pending_confirm_field_always_falls_back_regardless_of_gate(repos):
    # "No, it's Alex Smith" doesn't trip looks_like_tangent at all, but name
    # is already pending_confirm (a prior fallback extracted it at medium
    # confidence) — must never be treated as a fresh answer to a NEW field.
    state = _fast_state("name", name={"value": "Alesh", "confidence": 0.4, "status": "pending_confirm", "attempts": 0, "validated": True})
    state["transcript"][-1]["text"] = "No, it's Alex Smith."
    with patch(
        "backend.supervisor.tools.confirm_field_answer",
        return_value={"confirmed": False, "corrected_value": "Alex Smith"},
    ):
        result = _invoke(state, repos)
    assert result["caller_profile"]["name"]["status"] == "confirmed"
    assert result["caller_profile"]["name"]["value"] == "Alex Smith"


def test_transition_processes_last_field_live_and_batches_confirm_back(repos):
    # phone is FIELD_PRIORITY's last field — email already resolved
    # (pending_confirm) via an earlier background merge; this turn extracts
    # phone live and should offer email's confirm-back first (canonical
    # FIELD_PRIORITY order), with the one-time transitional preamble.
    state = _fast_state(
        "phone",
        email={"value": "manos@gmail.com", "confidence": 0.9, "status": "pending_confirm", "attempts": 0, "validated": True},
    )
    state["transcript"][-1]["text"] = "07577670101"
    with (
        patch("backend.supervisor.tools.extract_field", return_value={"value": "07577670101", "confidence": 0.9}),
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say manos@gmail.com?"),
    ):
        result = _invoke(state, repos)
    assert result["capture_phase"] == "confirm"
    assert result["caller_profile"]["phone"]["status"] == "pending_confirm"
    assert result["pending_reply"].startswith("Great, let me just quickly confirm a couple of things:")
    assert "manos@gmail.com" in result["pending_reply"]


async def test_dispatcher_only_spawns_background_verification_on_real_advance():
    # Regression test for a real bug found during Phase 7 implementation:
    # dispatcher used to infer "should I spawn a background check" from a
    # last_asked_field before/after diff, which also fired when
    # _fallback_to_real_capture resolved a pending confirmation and moved to
    # the next already-known field - spawning a REDUNDANT background task
    # that reused this turn's utterance a second time. Now gated by an
    # explicit state["background_verify_field"] signal set only by
    # node_capture_fast's own advance branch.
    repos = Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())
    call_id = "call-regress"
    CALL_STATES.pop(call_id, None)
    await dispatcher.run_supervisor_turn(repos, call_id, "t1", "I need some legal advice.")
    with patch("backend.supervisor.tools.classify_practice_area", return_value={"area": "tenancy", "confidence": 0.9}):
        await dispatcher.run_supervisor_turn(repos, call_id, "t2", "my flat")
    # garbled name -> medium confidence -> pending_confirm (fallback path,
    # since "uh" trips looks_like_tangent) — Phase 13 merged this
    # branch's extract_field + generate_confirm_back into one call.
    with patch(
        "backend.supervisor.tools.extract_and_confirm_field",
        return_value={"value": "Alesh", "confidence": 0.4, "confirm_back_phrasing": "Did you say Alesh?"},
    ):
        await dispatcher.run_supervisor_turn(repos, call_id, "t3", "uh, Alesh, maybe")
    # a correction resolves the pending name via confirm_field_answer -
    # this is where the bug used to spawn a stray "name" background task
    with patch(
        "backend.supervisor.tools.confirm_field_answer",
        return_value={"confirmed": False, "corrected_value": "Alex Smith"},
    ):
        await dispatcher.run_supervisor_turn(repos, call_id, "t4", "No, it's Alex Smith.")

    assert ("call-regress", "name") not in dispatcher.FIELD_VERIFICATIONS
    assert CALL_STATES[call_id]["caller_profile"]["name"]["status"] == "confirmed"
    assert CALL_STATES[call_id]["caller_profile"]["name"]["value"] == "Alex Smith"


def test_confirm_phase_also_interrupts_on_delayed_failure(repos):
    # node_capture (registered as "capture_confirm") has no concept of
    # background verification failures at all — node_capture_confirm's own
    # guard must catch a failure discovered AFTER the confirm/drain phase
    # has already started, same as node_capture_fast's identical check
    # during the fast pass itself. Without this, node_capture would pick
    # the "missing" field as a fresh target_field and misattribute
    # whatever utterance is current — the same bug class, one phase later.
    state = new_call_state("call-1")
    state["stage"] = "capture"
    state["capture_phase"] = "confirm"
    state["practice_area"] = "employment"
    state["transcript"] = [{"role": "caller", "text": "Yes, that's right.", "ts": "t"}]
    state["verification_failed_field"] = "email"
    with patch("backend.supervisor.tools.extract_field") as mock_extract, patch(
        "backend.supervisor.tools.confirm_field_answer"
    ) as mock_confirm:
        result = _invoke(state, repos)
    mock_extract.assert_not_called()
    mock_confirm.assert_not_called()
    assert result["last_asked_field"] == "email"
    assert result["capture_phase"] == "fast"
    assert "email" in result["pending_reply"].lower()
