"""Phase 7 (optimistic capture) — direct unit coverage for node_capture_fast's
own mechanics, complementing the end-to-end scenario tests in
test_scenarios.py. See docs/phases/phase-7-optimistic-capture.md."""

from unittest.mock import patch

import pytest

from backend import dispatcher
from backend.db.repositories import Repositories
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
    state = _fast_state("email")
    state["transcript"][-1]["text"] = utterance
    with patch("backend.supervisor.tools.extract_field", return_value={"value": None, "confidence": 0.0}):
        result = _invoke(state, repos)
    # fell back to real node_capture — last_asked_field re-synced to "email"
    # (still missing/unresolved), no optimistic advance to "phone"
    assert result["last_asked_field"] == "email"


def test_fast_pass_gate_falls_back_on_bad_shape(repos):
    # asked about email, but this utterance has no @/at/dot at all
    state = _fast_state("email")
    state["transcript"][-1]["text"] = "yes that's correct"
    with patch("backend.supervisor.tools.extract_field", return_value={"value": None, "confidence": 0.0}):
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
    with patch("backend.supervisor.tools.extract_field", return_value={"value": None, "confidence": 0.0}):
        result = _invoke(state, repos)
    assert result["last_asked_field"] != "email"


def test_urgent_reask_falls_back_instead_of_advancing(repos):
    state = _fast_state("email", email={"value": None, "confidence": 0.0, "status": "missing", "attempts": 1, "validated": True})
    state["verification_failed_field"] = "email"
    state["transcript"][-1]["text"] = "manos at gmail dot com"
    with patch("backend.supervisor.tools.extract_field", return_value={"value": "manos@gmail.com", "confidence": 0.9}):
        result = _invoke(state, repos)
    # fell back to real node_capture (which re-extracts email for real) —
    # never advanced straight to "phone" despite the utterance passing the
    # shape gate
    assert result["last_asked_field"] != "phone"


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
    with patch("backend.dispatcher.send_over_bridge"):
        await dispatcher.process_supervisor_call(repos, call_id, "t1", "I need some legal advice.")
        with patch("backend.supervisor.tools.classify_practice_area", return_value={"area": "tenancy", "confidence": 0.9}):
            await dispatcher.process_supervisor_call(repos, call_id, "t2", "my flat")
        # garbled name -> medium confidence -> pending_confirm (fallback path,
        # since "uh" trips looks_like_tangent)
        with (
            patch("backend.supervisor.tools.extract_field", return_value={"value": "Alesh", "confidence": 0.4}),
            patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say Alesh?"),
        ):
            await dispatcher.process_supervisor_call(repos, call_id, "t3", "uh, Alesh, maybe")
        # a correction resolves the pending name via confirm_field_answer -
        # this is where the bug used to spawn a stray "name" background task
        with patch(
            "backend.supervisor.tools.confirm_field_answer",
            return_value={"confirmed": False, "corrected_value": "Alex Smith"},
        ):
            await dispatcher.process_supervisor_call(repos, call_id, "t4", "No, it's Alex Smith.")

    assert ("call-regress", "name") not in dispatcher.FIELD_VERIFICATIONS
    assert CALL_STATES[call_id]["caller_profile"]["name"]["status"] == "confirmed"
    assert CALL_STATES[call_id]["caller_profile"]["name"]["value"] == "Alex Smith"
