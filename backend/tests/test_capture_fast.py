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
    # ...and the re-ask was about phone, so that is what the caller answers
    # next — not email, which is only pending_confirm because a background
    # check put it there and whose confirm-back has still never been spoken.
    assert result["last_asked_field"] == "phone"


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


# --- Retrying a field that already failed once -------------------------
# All four reproduce one live call (docs/fixes/2026-08-28-004.md): the
# caller gave an unusable email, was correctly asked to spell it out, and
# from there the agent alternated "Great — and what's your phone number?"
# with "could you spell out your email" until they hung up.


def _retry_state(previous_attempt=None, attempts=1):
    # email has already failed once and been re-asked, so it is still
    # last_asked_field — the caller's next utterance is a RETRY of it.
    state = _fast_state(
        "email",
        email={"value": None, "confidence": 0.0, "status": "missing", "attempts": attempts, "validated": True},
    )
    if previous_attempt:
        state["partial_field_utterances"] = {"email": previous_attempt}
    return state


def test_retry_is_settled_this_turn_instead_of_optimistically_advancing(repos):
    # The whiplash at the heart of the live bug. "At gmail dot com." clears
    # looks_like_field_shape, so the fast path used to advance to phone and
    # leave email to a background check — which failed, and interrupted the
    # NEXT turn to ask for the email all over again, discarding whatever
    # the caller had said in between. Two turns per retry, no progress.
    # A retry must be resolved in the turn it arrives in.
    state = _retry_state()
    state["transcript"][-1]["text"] = "At gmail dot com."
    with patch(
        "backend.supervisor.tools.extract_field", return_value={"value": "@gmail.com", "confidence": 0.6}
    ) as mock_extract:
        result = _invoke(state, repos)
    mock_extract.assert_called_once()
    assert "phone" not in result["pending_reply"].lower()
    assert result["pending_reply"] == graph.SPELL_OUT_REPLIES["email"]
    assert result["last_asked_field"] == "email"
    assert result["capture_phase"] == "fast"
    # and no background check was spawned for a turn already fully processed
    assert result.get("background_verify_field") is None


def test_retry_counts_toward_escalation_rather_than_looping(repos):
    # Same turn as above on the caller's third try: three failed attempts at
    # one field hands them to a human. Live, this never fired at all.
    state = _retry_state(attempts=2)
    state["transcript"][-1]["text"] = "At gmail dot com."
    with patch("backend.supervisor.tools.extract_field", return_value={"value": "@gmail.com", "confidence": 0.6}):
        result = _invoke(state, repos)
    assert result["stage"] == "escalation"
    assert result["escalation_reason"] == "capture_failed"
    assert result["caller_profile"]["email"]["attempts"] == 3


def test_retry_reads_the_previous_attempt_so_a_split_value_can_be_rejoined(repos):
    # The caller said "Manos 44." and then, asked to spell it out, "at gmail
    # dot com" — two halves of one address, split across a pause. The
    # transport is forbidden from merging them (transport/prompts.py rule
    # 3a), so the extraction is the only place they can be reunited; without
    # the earlier half it can only ever produce "@gmail.com", which no
    # amount of re-asking fixes.
    state = _retry_state(previous_attempt="Manos 44.")
    state["transcript"][-1]["text"] = "At G M A I L dot C O M."
    with patch(
        "backend.supervisor.tools.extract_field",
        return_value={"value": "manos44@gmail.com", "confidence": 0.9},
    ) as mock_extract:
        result = _invoke(state, repos)
    mock_extract.assert_called_once_with("At G M A I L dot C O M.", "email", "Manos 44.")
    assert result["caller_profile"]["email"]["value"] == "manos44@gmail.com"
    assert result["caller_profile"]["email"]["status"] == "pending_confirm"
    # the fast pass resumes on the next unasked field rather than dropping
    # into the confirm/drain phase with phone never asked about
    assert result["last_asked_field"] == "phone"
    assert result["capture_phase"] == "fast"
    assert "phone" in result["pending_reply"].lower()
    # spent partial, dropped — a later re-ask must not stitch onto it
    assert "email" not in result["partial_field_utterances"]


def test_first_attempt_prompt_and_call_are_unchanged(repos):
    # The stitching context must never leak into an ordinary first answer:
    # extract_field is called with exactly two arguments, as always.
    state = _fast_state("phone")  # last FIELD_PRIORITY entry -> processed live
    state["transcript"][-1]["text"] = "07577670101"
    with (
        patch(
            "backend.supervisor.tools.extract_field", return_value={"value": "07577670101", "confidence": 0.9}
        ) as mock_extract,
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Did you say 07577670101?"),
    ):
        _invoke(state, repos)
    mock_extract.assert_called_once_with("07577670101", "phone")


def test_repeated_background_failures_escalate_instead_of_reasking_forever(repos):
    # The delayed-failure interrupt is a real attempt — it ran against an
    # answer the caller really gave out loud. It used not to be counted, so
    # a field could fail in the background indefinitely without ever
    # reaching 3 strikes.
    state = _fast_state(
        "phone",
        email={"value": None, "confidence": 0.0, "status": "missing", "attempts": 2, "validated": True},
    )
    state["verification_failed_field"] = "email"
    state["transcript"][-1]["text"] = "555-123-4567"
    with patch("backend.supervisor.tools.extract_field") as mock_extract:
        result = _invoke(state, repos)
    mock_extract.assert_not_called()
    assert result["stage"] == "escalation"
    assert result["escalation_reason"] == "capture_failed"
    assert result["caller_profile"]["email"]["attempts"] == 3


async def test_live_reproduction_split_email_recovers_instead_of_hanging():
    # End-to-end through the real dispatcher (reconciliation, background
    # verification and all), replaying the two live utterances that derailed
    # the call. Before the fix these produced "could you spell out your
    # email" and then "Great — and what's your phone number?", followed by
    # the spell-out again on the next turn, and again after that — never
    # advancing, never escalating, until the caller hung up.
    #
    # Deliberately seeds the capture stage rather than driving through
    # greeting/routing, which would reach the real Anthropic API on turn 1
    # (docs/known-issues/2026-08-25-002.md).
    repos = Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())
    call_id = "call-split-email"
    state = new_call_state(call_id)
    state["stage"] = "capture"
    state["capture_phase"] = "fast"
    state["practice_area"] = "tenancy"
    state["last_asked_field"] = "email"  # email has just been asked for
    state["caller_profile"]["name"] = {
        "value": "Manos", "confidence": 0.9, "status": "confirmed", "attempts": 0, "validated": True,
    }
    CALL_STATES[call_id] = state
    seen_args = []

    def _extract(utterance, field, previous_attempt=None):
        # Stands in for the real model: a domain on its own is unusable, but
        # a domain offered right after a failed attempt that was the local
        # part is the rest of that same address.
        seen_args.append((utterance, field, previous_attempt))
        if previous_attempt == "Manos 44.":
            return {"value": "manos44@gmail.com", "confidence": 0.92}
        return {"value": "@gmail.com", "confidence": 0.6}

    # "Manos 44." has no @/at/dot, so it fails looks_like_field_shape and
    # goes down the synchronous fallback — attempt 1, correctly re-asked.
    # This is the part of the live call that already worked.
    with patch(
        "backend.supervisor.tools.extract_and_confirm_field",
        return_value={"value": "Manos 44", "confidence": 0.15, "confirm_back_phrasing": "..."},
    ):
        reply1, _ = await dispatcher.run_supervisor_turn(repos, call_id, "t1", "Manos 44.")
    assert reply1 == graph.SPELL_OUT_REPLIES["email"]
    assert CALL_STATES[call_id]["partial_field_utterances"] == {"email": "Manos 44."}

    # The turn that used to derail the call: answered here and now, with the
    # first half of the address in hand.
    with patch("backend.supervisor.tools.extract_field", side_effect=_extract):
        reply2, _ = await dispatcher.run_supervisor_turn(repos, call_id, "t2", "At gmail dot com.")

    assert ("At gmail dot com.", "email", "Manos 44.") in seen_args
    profile = CALL_STATES[call_id]["caller_profile"]
    assert profile["email"]["value"] == "manos44@gmail.com"
    assert profile["email"]["status"] == "pending_confirm"
    assert CALL_STATES[call_id]["stage"] == "capture"
    # spent partial dropped, so a later re-ask starts clean
    assert CALL_STATES[call_id]["partial_field_utterances"] == {}
    # and the call moved forward instead of asking for the email a third time
    assert "phone" in reply2.lower()
    assert CALL_STATES[call_id]["last_asked_field"] == "phone"
    # nothing left in flight to interrupt the next turn with a stale failure
    assert not [k for k in dispatcher.FIELD_VERIFICATIONS if k[0] == call_id]


# --- last_asked_field must be the field the reply actually asked about ---
# All three reproduce one live call (docs/fixes/2026-08-28-005.md), where a
# status-scanning guess picked the wrong field twice: once making the caller
# give their email twice, once making them confirm their phone number twice.


def test_reask_leaves_the_reasked_field_outstanding_not_an_unspoken_pending_one(repos):
    # Live trace #26-#40. name is pending_confirm from a background check
    # at 0.6 — its confirm-back has never been spoken. The caller's "Manos
    # 44." fails looks_like_field_shape for email, so node_capture re-asks
    # for the email. The outstanding question is therefore about EMAIL; the
    # old derivation returned "name" (first pending_confirm in
    # FIELD_PRIORITY order), so the caller's next utterance — them spelling
    # out their email address — was fed to confirm_field_answer as a yes/no
    # about their NAME, which swallowed it and asked for the email again.
    state = _fast_state(
        "email",
        name={"value": "Manos", "confidence": 0.6, "status": "pending_confirm", "attempts": 0, "validated": True},
    )
    state["transcript"][-1]["text"] = "Manos 44."
    with patch(
        "backend.supervisor.tools.extract_and_confirm_field",
        return_value={"value": "Manos44", "confidence": 0.2, "confirm_back_phrasing": "..."},
    ):
        result = _invoke(state, repos)
    assert result["pending_reply"] == graph.SPELL_OUT_REPLIES["email"]
    assert result["last_asked_field"] == "email"
    assert result["caller_profile"]["name"]["status"] == "pending_confirm"  # untouched


def test_confirm_back_leaves_its_own_field_outstanding_not_an_earlier_pending_one(repos):
    # Live trace #65-#79. email is pending_confirm with its confirm-back
    # unspoken (batched for the drain phase); the caller reads out their
    # phone number in words, which fails looks_like_field_shape, and
    # node_capture extracts it and speaks PHONE's confirm-back. The old
    # derivation returned "email" — so the caller's "Yes" to the phone
    # question was applied to email instead, and the phone confirm-back had
    # to be asked all over again.
    state = _fast_state(
        "phone",
        email={"value": "manos@gmail.com", "confidence": 0.9, "status": "pending_confirm", "attempts": 0, "validated": True},
    )
    state["transcript"][-1]["text"] = "O seven five seven seven six seven oh one oh one."
    with patch(
        "backend.supervisor.tools.extract_and_confirm_field",
        return_value={
            "value": "07577670101", "confidence": 0.85,
            "confirm_back_phrasing": "I heard your phone number as 0-7-5-7-7-6-7-0-1-0-1 — is that correct?",
        },
    ):
        result = _invoke(state, repos)
    assert "phone number" in result["pending_reply"].lower()
    assert result["last_asked_field"] == "phone"


def test_yes_to_a_confirm_back_resolves_that_field_and_does_not_repeat_it(repos):
    # The turn after the one above, end to end: "Yes." must confirm PHONE
    # (the question actually asked) and move on to email's confirm-back —
    # not confirm email and then ask about the phone number a second time.
    state = _fast_state(
        "phone",
        email={"value": "manos@gmail.com", "confidence": 0.9, "status": "pending_confirm", "attempts": 0, "validated": True},
    )
    state["transcript"][-1]["text"] = "O seven five seven seven six seven oh one oh one."
    with patch(
        "backend.supervisor.tools.extract_and_confirm_field",
        return_value={
            "value": "07577670101", "confidence": 0.85,
            "confirm_back_phrasing": "I heard your phone number as 0-7-5-7-7-6-7-0-1-0-1 — is that correct?",
        },
    ):
        first = _invoke(state, repos)

    second_state = dict(first)
    second_state["transcript"] = first["transcript"] + [{"role": "caller", "text": "Yes.", "ts": "t"}]
    with (
        patch(
            "backend.supervisor.tools.confirm_field_answer",
            return_value={"confirmed": True, "corrected_value": None, "needs_clarification": False},
        ) as mock_confirm,
        patch("backend.supervisor.tools.generate_confirm_back", return_value="Just to confirm — manos@gmail.com?"),
    ):
        second = _invoke(second_state, repos)

    mock_confirm.assert_called_once_with("Yes.", "phone", "07577670101")
    assert second["caller_profile"]["phone"]["status"] == "confirmed"
    # the remaining question is email's confirm-back, and the phone number
    # is never read back a second time
    assert "phone" not in second["pending_reply"].lower()
    assert second["last_asked_field"] == "email"


# --- a non-name utterance must not be optimistically taken as the name ---


def test_late_answer_to_another_question_is_not_optimistically_taken_as_a_name(repos):
    # Live trace #24-#37 (docs/fixes/2026-08-28-008.md). The caller answered
    # the ROUTING question a turn late, after the fast pass had already moved
    # on to asking their name. Nothing said that wasn't a name, so the fast
    # path advanced to "Great - and what's your email address?", spawned a
    # background name check that (correctly) found no name, and a turn later
    # interrupted with "what's your name again?" - discarding the email the
    # caller had meanwhile given. Must fall back and deal with it now.
    state = _fast_state("name")
    state["transcript"][-1]["text"] = (
        "Yeah, it's about my home. He's basically trying to kick me out with little notice."
    )
    with patch(
        "backend.supervisor.tools.extract_and_confirm_field",
        return_value={"value": "", "confidence": 0.0, "confirm_back_phrasing": ""},
    ) as mock_extract:
        result = _invoke(state, repos)
    mock_extract.assert_called_once()  # handled now, synchronously
    assert "email" not in result["pending_reply"].lower()  # no optimistic advance
    assert result.get("background_verify_field") is None
    assert result["last_asked_field"] == "name"
    assert result["caller_profile"]["name"]["attempts"] == 1


def test_a_name_that_never_extracts_escalates_instead_of_asking_forever(repos):
    # node_capture's fresh-extraction branch counted an attempt for email and
    # phone but not for name, which fell through to a plain "Thanks - and
    # what's your name?" with attempts untouched. Verified pre-fix: three
    # turns, attempts still 0, no escalation possible. Rarely reached before
    # (the fast path advanced past such utterances); now the ordinary path.
    state = _fast_state("name", name={
        "value": None, "confidence": 0.0, "status": "missing", "attempts": 2, "validated": True,
    })
    state["transcript"][-1]["text"] = "he keeps saying I have to be out of the flat by the weekend"
    with patch(
        "backend.supervisor.tools.extract_and_confirm_field",
        return_value={"value": "", "confidence": 0.0, "confirm_back_phrasing": ""},
    ):
        result = _invoke(state, repos)
    assert result["stage"] == "escalation"
    assert result["escalation_reason"] == "capture_failed"
    assert result["caller_profile"]["name"]["attempts"] == 3
