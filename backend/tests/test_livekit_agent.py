"""Phase 14 — the LiveKit agent's contract with everything below it.

These are unit tests over the agent's own logic (tool surface, filler
scheduling, interrupt policy, failure handling). They deliberately do NOT spin
up a LiveKit room or a Realtime session — the transport itself is verified by
the live scenario runs the phase DoD requires, which no mock can stand in for.
What's testable here is the part that has burned this codebase before: the
decision logic around a tool call racing the caller's next utterance.
"""

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from livekit.agents import StopResponse

from backend import dispatcher
from backend.db.repositories import Repositories
from backend.supervisor.fillers import FILLER_PHRASES
from backend.supervisor.state import CALL_STATES, new_call_state
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository
from backend.transport import livekit_agent
from backend.transport.livekit_agent import JupusAgent


@pytest.fixture(autouse=True)
def clear_state():
    CALL_STATES.clear()
    dispatcher.LOCKS.clear()
    yield
    CALL_STATES.clear()
    dispatcher.LOCKS.clear()


@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


@pytest.fixture(autouse=True)
def fast_timings():
    # Same logic, milliseconds instead of seconds. Without this every filler
    # test pays the real 0.4s dwell just to prove the callback fired, and every
    # test that doesn't seed a transcript pays the full 1.5s ASR wait. The
    # production values are asserted separately (see
    # test_filler_is_scheduled_with_idle_delay_and_repeat and
    # test_production_filler_timings_are_what_the_measurement_assumes); tests
    # that are specifically ABOUT a timeout patch it themselves.
    with (
        patch.object(livekit_agent, "FILLER_IDLE_DELAY_SECONDS", 0.01),
        patch.object(livekit_agent, "FILLER_REPEAT_SECONDS", 0.02),
        patch.object(livekit_agent, "TRANSCRIPT_WAIT_SECONDS", 0.02),
    ):
        yield


def _slow_turn(reply: str, stage: str, seconds: float = 0.12):
    """A supervisor turn slow enough for the filler scheduler to fire."""

    async def _turn(*_args, **_kwargs):
        await asyncio.sleep(seconds)
        return reply, stage

    return _turn


def _instant_turn(reply: str, stage: str):
    """A supervisor turn that beats the idle dwell — no filler should fire."""

    async def _turn(*_args, **_kwargs):
        return reply, stage

    return _turn


class _FakeHandle:
    def __init__(self, interrupted: bool = False):
        self.interrupted = interrupted


class _FakeSession:
    """Records say() calls instead of synthesizing anything."""

    def __init__(self):
        self.said: list[tuple[str, bool]] = []

    def say(self, text, *, audio=None, allow_interruptions=True, add_to_chat_ctx=True):
        self.said.append((text, add_to_chat_ctx))
        return _FakeHandle()


class _FakeRunContext:
    def __init__(self, session=None):
        self.session = session or _FakeSession()
        self.function_call = SimpleNamespace(call_id="tool-1")
        self.filler_calls: list[dict] = []
        self._filler_task = None

    def with_filler(self, source, *, delay, interval=None, max_steps=None):
        """A timing-faithful stand-in for LiveKit's _FillerScheduler.

        It has to model the TIMING, not just call the callback: the whole
        point of the real scheduler's idle dwell is that a turn which resolves
        quickly is never narrated at all, and a fake that fires eagerly would
        assert the opposite of the shipped behaviour. So the filler runs as a
        background task that waits `delay` first, repeats every `interval`, and
        is cancelled when the body finishes — exactly what the real context
        manager's __aexit__ does via scheduler.aclose().
        """
        self.filler_calls.append({"delay": delay, "interval": interval, "max_steps": max_steps})
        outer = self

        async def _schedule():
            step = 0
            await asyncio.sleep(delay)
            while max_steps is None or step < max_steps:
                if source(step) is None:
                    break
                step += 1
                if interval is None:
                    break
                await asyncio.sleep(interval)

        class _CM:
            async def __aenter__(self):
                outer._filler_task = asyncio.create_task(_schedule())
                return None

            async def __aexit__(self, *exc):
                task = outer._filler_task
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                return False

        return _CM()


def _seed(call_id: str, stage: str, **overrides):
    state = new_call_state(call_id)
    state["stage"] = stage
    state.update(overrides)
    CALL_STATES[call_id] = state
    return state


# --- CLAUDE.md rule #1 ------------------------------------------------------


def test_realtime_sees_exactly_one_tool(repos):
    # The single most load-bearing architectural rule in the project: all
    # business logic sits behind one dispatch call. A second tool appearing
    # here is a doctrine violation, not a feature.
    agent = JupusAgent("call-1", repos)

    assert [tool.info.name for tool in agent.tools] == ["ask_supervisor"]


def test_instructions_are_the_server_side_prompt(repos):
    from backend.transport.prompts import SUPERVISOR_INSTRUCTIONS

    assert JupusAgent("call-1", repos).instructions == SUPERVISOR_INSTRUCTIONS


# --- verbatim transcript beats the model's invented argument ----------------
# docs/DECISIONS.md: `last_caller_utterance` is authored by the Realtime model,
# not passed through from ASR, and it invents ("manos44" -> "manos44@example.com").
# That entry also records that tightening the prompt was tried and was NOT
# reliable. The pre-Phase-14 client took real ASR in preference; these pin that
# the LiveKit transport does the same.


async def test_real_transcript_wins_over_the_models_invented_argument(repos):
    _seed("call-1", "capture", capture_phase="fast")
    agent = JupusAgent("call-1", repos)
    agent.note_transcript("manos44")
    seen = []

    async def fake_turn(_r, _c, _t, utterance):
        seen.append(utterance)
        return "ok", "capture"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        await agent.ask_supervisor(
            _FakeRunContext(),
            {"reason": "r", "last_caller_utterance": "manos44@example.com"},
        )

    # The invented "@example.com" must never reach the graph — silently
    # repairing a malformed email defeats the whole validation pipeline.
    assert seen == ["manos44"]


async def test_falls_back_to_the_model_argument_when_no_transcript_arrives(repos):
    # Better a model-authored utterance than a dropped turn. The wait is
    # shortened here so the test doesn't sit through the real timeout.
    _seed("call-1", "routing")
    agent = JupusAgent("call-1", repos)
    seen = []

    async def fake_turn(_r, _c, _t, utterance):
        seen.append(utterance)
        return "ok", "routing"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        await agent.ask_supervisor(
            _FakeRunContext(), {"reason": "r", "last_caller_utterance": "my flat"}
        )

    assert seen == ["my flat"]


async def test_turn_waits_for_a_transcript_that_lands_late(repos):
    # The ASR/tool-call race: the model can call the tool before transcription
    # finishes. docs/fixes/2026-08-25-001.md is the live bug from getting this
    # wrong — a call stuck re-asking one turn behind.
    _seed("call-1", "capture", capture_phase="fast")
    agent = JupusAgent("call-1", repos)
    seen = []

    async def fake_turn(_r, _c, _t, utterance):
        seen.append(utterance)
        return "ok", "capture"

    async def land_late():
        await asyncio.sleep(0.03)
        agent.note_transcript("alex at example dot com")

    with (
        patch.object(livekit_agent, "TRANSCRIPT_WAIT_SECONDS", 1.0),
        patch.object(livekit_agent, "run_supervisor_turn", fake_turn),
    ):
        asyncio.create_task(land_late())
        await agent.ask_supervisor(
            _FakeRunContext(),
            {"reason": "r", "last_caller_utterance": "alex@example.com"},
        )

    assert seen == ["alex at example dot com"]


async def test_a_transcript_is_claimed_once_and_not_reused(repos):
    # Reusing a stale transcript is the OTHER half of the same live bug: the
    # next turn must not inherit the previous turn's text.
    _seed("call-1", "capture", capture_phase="fast")
    agent = JupusAgent("call-1", repos)
    seen = []

    async def fake_turn(_r, _c, _t, utterance):
        seen.append(utterance)
        return "ok", "capture"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        agent.note_transcript("Alex Smith")
        await agent.ask_supervisor(_FakeRunContext(), {"reason": "r", "last_caller_utterance": "x"})
        await agent.ask_supervisor(_FakeRunContext(), {"reason": "r", "last_caller_utterance": "y"})

    assert seen == ["Alex Smith", "y"]


def test_session_pins_transcription_model_voice_and_turn_detection():
    # Left unset, the plugin defaults to gpt-4o-mini-transcribe — a silently
    # different ASR model than the pre-Phase-14 config used, and transcription
    # fidelity is exactly what _verbatim_utterance depends on.
    #
    # RealtimeModel is patched rather than built: constructing one opens a live
    # WebSocket to OpenAI, and nothing in backend/tests may make a real API
    # call. Capturing the kwargs asserts the same thing without the network.
    captured = {}

    def fake_model(**kwargs):
        captured.update(kwargs)
        return object()

    with (
        patch.object(livekit_agent.openai.realtime, "RealtimeModel", fake_model),
        patch.object(livekit_agent, "AgentSession", lambda **kw: kw),
    ):
        livekit_agent.build_session()

    assert captured["input_audio_transcription"].model == "gpt-transcribe"
    assert captured["model"] == "gpt-realtime-2.1"
    assert captured["voice"] == "marin"
    # docs/DECISIONS.md records live-testing reasons for both of these.
    assert captured["turn_detection"].eagerness == "low"
    assert captured["turn_detection"].interrupt_response is True
    assert captured["input_audio_noise_reduction"] == "near_field"


# --- Phase 11 latency boundaries, re-produced under the new transport -------


async def test_first_audio_is_timed_from_the_start_of_the_turn(repos):
    # Both boundaries lost their producer when /bridge went (the browser
    # reported them). Without them stt_and_dialogue_decision, tts_first_audio
    # and total_perceived all read as no-data for every LiveKit call, which is
    # Phase 11's latency view going dark without saying so.
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    with patch.object(livekit_agent, "run_supervisor_turn", _slow_turn("ok", "booking")):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})
    agent.note_agent_started_speaking()

    events = {e["event_type"]: e for e in repos.trace.get_trace("call-1")}
    # first_audio answers Phase 14's question (total silence the caller sat
    # through); tts_first_audio keeps Phase 11's narrower one (supervisor
    # answered -> audible), so total_perceived's arithmetic stays valid.
    assert events["first_audio"]["payload"]["ms_since_turn_start"] >= 0
    assert events["first_audio"]["payload"]["tool_call_id"] == "tool-1"
    assert events["tts_first_audio"]["payload"]["ms_since_reply_delivered"] >= 0


async def test_first_audio_is_recorded_once_per_turn(repos):
    # The agent enters "speaking" for the filler AND again for the reply. Only
    # the first matters — that is when the caller's silence actually ends.
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)

    with patch.object(livekit_agent, "run_supervisor_turn", _slow_turn("ok", "booking")):
        await agent.ask_supervisor(
            _FakeRunContext(), {"reason": "r", "last_caller_utterance": "yes"}
        )
    agent.note_agent_started_speaking()
    agent.note_agent_started_speaking()

    kinds = [e["event_type"] for e in repos.trace.get_trace("call-1")]
    assert kinds.count("first_audio") == 1
    assert kinds.count("tts_first_audio") == 1


def test_caller_stopped_speaking_is_recorded(repos):
    agent = JupusAgent("call-1", repos)

    agent.note_caller_stopped_speaking()

    assert [e["event_type"] for e in repos.trace.get_trace("call-1")] == ["speech_stopped"]


# --- worker startup ---------------------------------------------------------


def test_worker_does_not_start_without_credentials(repos, monkeypatch, caplog):
    # Unguarded, the worker raises ValueError("ws_url is required") inside a
    # bare create_task where nothing awaits it: the backend looks healthy,
    # /livekit-token returns a tidy 503, and the agent is dead with no message
    # anywhere. config.py promises the backend still boots for admin/eval work
    # without these, so the failure has to be loud instead of silent.
    from backend.config import settings as real_settings

    monkeypatch.setattr(real_settings, "livekit_api_key", None)
    built = []
    with patch.object(livekit_agent, "build_server", lambda: built.append(1)):
        livekit_agent.start_agent_server(repos)

    assert built == [], "worker must not be built without credentials"
    assert any("not configured" in r.message for r in caplog.records)


# --- the reply path ---------------------------------------------------------


async def test_reply_is_returned_verbatim_from_the_supervisor(repos):
    _seed("call-1", "routing")
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    async def fake_turn(*_a, **_kw):
        return "Which of those areas is it?", "routing"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        reply = await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "hi"})

    assert reply == "Which of those areas is it?"


async def test_no_filler_on_a_turn_that_has_a_natural_next_question(repos):
    # Routing already ends by asking something; a filler here would talk over
    # Phase 7/8's existing latency hiding.
    _seed("call-1", "routing")
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    async def fake_turn(*_a, **_kw):
        return "ok", "routing"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "hi"})

    assert ctx.filler_calls == []
    assert ctx.session.said == []


async def test_filler_plays_before_real_answer_on_a_confirm_turn(repos):
    # Phase doc test 1: the real answer takes longer than the filler, so the
    # filler is what's spoken first and the real answer follows.
    state = _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    with patch.object(livekit_agent, "run_supervisor_turn", _slow_turn("You're booked.", "ended")):
        reply = await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    spoken = [text for text, _ in ctx.session.said]
    assert spoken == list(FILLER_PHRASES["confirm_booking"])
    assert reply == "You're booked."


async def test_filler_is_scheduled_with_idle_delay_and_repeat(repos):
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    with patch.object(livekit_agent, "run_supervisor_turn", _slow_turn("ok", "booking")):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    # The delay is what stops a fast turn being narrated at all; the interval
    # is what stops a slow one being a promise followed by dead air. Compared
    # against the module's live values because the timings fixture shrinks them
    # — the PRODUCTION values are pinned separately below.
    assert ctx.filler_calls == [
        {
            "delay": livekit_agent.FILLER_IDLE_DELAY_SECONDS,
            "interval": livekit_agent.FILLER_REPEAT_SECONDS,
            "max_steps": len(FILLER_PHRASES["confirm_booking"]),
        }
    ]


def test_production_filler_timings_are_what_the_measurement_assumes():
    # docs/DECISIONS.md reports ~400ms time-to-first-audio from live traces,
    # and that number IS this constant. If it changes, that table is stale.
    from backend.supervisor import fillers

    assert fillers.FILLER_IDLE_DELAY_SECONDS == 0.4
    assert fillers.FILLER_REPEAT_SECONDS == 4.0


async def test_filler_is_kept_out_of_the_realtime_chat_context(repos):
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    with patch.object(livekit_agent, "run_supervisor_turn", _slow_turn("ok", "booking")):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    assert all(add_to_ctx is False for _text, add_to_ctx in ctx.session.said)


async def test_filler_spoken_is_traced(repos):
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    with patch.object(livekit_agent, "run_supervisor_turn", _slow_turn("ok", "booking")):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    events = [e for e in repos.trace.get_trace("call-1") if e["event_type"] == "filler_spoken"]
    # The DoD's perceived-latency claim has to rest on trace data, not
    # assertion — this is the event it rests on.
    assert len(events) == len(FILLER_PHRASES["confirm_booking"])


async def test_turn_is_bracketed_by_latency_boundary_events(repos):
    # These two events are what the phase's "perceived latency changed,
    # round-trip latency did not" claim is computed from. Without both, the
    # DoD's side-by-side numbers would be an assertion rather than a
    # measurement.
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    with patch.object(livekit_agent, "run_supervisor_turn", _slow_turn("You're booked.", "ended")):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    kinds = [e["event_type"] for e in repos.trace.get_trace("call-1")]
    assert kinds.index("ask_supervisor_received") < kinds.index("filler_spoken")
    assert kinds.index("filler_spoken") < kinds.index("reply_ready")


async def test_reply_ready_records_whether_a_filler_played(repos):
    # Lets the eval layer separate filler turns from non-filler turns without
    # re-deriving the selection logic.
    _seed("call-1", "routing")
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    async def fake_turn(*_a, **_kw):
        return "ok", "routing"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "hi"})

    ready = [e for e in repos.trace.get_trace("call-1") if e["event_type"] == "reply_ready"]
    assert len(ready) == 1
    assert ready[0]["payload"]["filler_played"] is False


async def test_answer_ready_before_the_filler_fires_produces_no_filler(repos):
    """Phase doc test 4: the real answer resolves faster than expected.

    The doc frames this as "assert it queues correctly rather than colliding
    with the still-playing filler", carried over from the /bridge transport's
    one-active-response-at-a-time constraint. Under LiveKit the collision it
    worried about cannot happen, and the reason is worth pinning: the filler is
    scheduled on a continuous-idle dwell inside a context manager that is
    cancelled the moment the supervisor returns. A turn that beats the dwell
    therefore never produces a filler at all — there is nothing to collide
    with, and the caller hears exactly one thing.

    That is a stronger guarantee than queueing, and it is also the half of the
    Phase 2 filler finding that was always correct: a fast turn must not be
    narrated.
    """
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    with patch.object(livekit_agent, "run_supervisor_turn", _instant_turn("You're booked.", "ended")):
        reply = await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    assert reply == "You're booked."
    assert ctx.session.said == [], "a turn faster than the idle dwell must stay silent"
    assert [e["event_type"] for e in repos.trace.get_trace("call-1")].count("filler_spoken") == 0
    # The scheduler was still armed — this is the dwell winning the race, not
    # filler selection having declined the turn (which is a different path,
    # covered by test_no_filler_on_a_turn_that_has_a_natural_next_question).
    assert len(ctx.filler_calls) == 1


async def test_slow_turn_reaches_the_second_filler_line(repos):
    # The other side of the same race: a turn that outlives FILLER_REPEAT_SECONDS
    # gets line [1], which is what keeps a long wait from being a promise
    # followed by dead air (the Phase 2 complaint recorded in DECISIONS.md).
    _seed("call-1", "capture", capture_phase="confirm")
    CALL_STATES["call-1"]["caller_profile"]["email"]["status"] = "pending_confirm"
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    with patch.object(livekit_agent, "run_supervisor_turn", _slow_turn("Thanks.", "capture", seconds=0.2)):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    assert [text for text, _ in ctx.session.said] == list(FILLER_PHRASES["confirm_field"])


# --- Decision 3: interrupt policy -------------------------------------------


async def test_acknowledgment_during_filler_does_not_reach_the_graph(repos):
    # Phase doc test 2. Without this guard every "mhm" over a filler becomes a
    # real utterance and reroutes the turn.
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    agent._filler_handles = [_FakeHandle(interrupted=True)]
    ctx = _FakeRunContext()

    async def fake_turn(*_a, **_kw):
        raise AssertionError("the graph must not see a backchannel")

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        with pytest.raises(StopResponse):
            await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "mhm"})

    ignored = [
        e for e in repos.trace.get_trace("call-1") if e["event_type"] == "filler_interruption_ignored"
    ]
    assert len(ignored) == 1


async def test_substantive_interruption_during_filler_reaches_the_graph(repos):
    # Phase doc test 3, the case singled out for live verification because this
    # codebase has already shipped an ask_supervisor/ASR race bug.
    _seed("call-1", "capture", capture_phase="confirm")
    agent = JupusAgent("call-1", repos)
    agent._filler_handles = [_FakeHandle(interrupted=True)]
    ctx = _FakeRunContext()
    seen = []

    async def fake_turn(_repos, _call_id, _tool_call_id, utterance):
        seen.append(utterance)
        return "Got it — Alesh with an H.", "capture"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        reply = await agent.ask_supervisor(
            ctx, {"reason": "r", "last_caller_utterance": "actually it's Alesh with an H"}
        )

    assert seen == ["actually it's Alesh with an H"]
    assert reply == "Got it — Alesh with an H."


async def test_negation_during_filler_is_never_treated_as_acknowledgment(repos):
    # "no" over a filler on the booking-confirm turn is a decline, not a
    # backchannel — swallowing it is the worst version of this bug.
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    agent._filler_handles = [_FakeHandle(interrupted=True)]
    ctx = _FakeRunContext()
    seen = []

    async def fake_turn(_repos, _call_id, _tool_call_id, utterance):
        seen.append(utterance)
        return "No problem — what other day works?", "booking"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "no"})

    assert seen == ["no"]


async def test_backchannel_still_suppressed_after_a_substantive_interruption(repos):
    """Regression: overlapping turns must not wipe each other's interrupt state.

    LiveKit keeps a tool running after its speech is interrupted, and the
    interrupting utterance starts its own turn — so two ask_supervisor calls
    are genuinely in flight at once, sharing this agent instance. An earlier
    version reset the filler-handle list at the top of every turn, so a
    substantive interruption erased the evidence that a filler had been cut off
    while the first turn was still running, and the NEXT backchannel sailed
    past the guard into the graph as a real utterance.

    Reproduced against the pre-fix code before this test was written.
    """
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    agent._filler_handles = [_FakeHandle(interrupted=True)]
    reached = []

    async def fake_turn(_repos, _call_id, _tool_call_id, utterance):
        reached.append(utterance)
        return "ok", "booking"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        # A real correction: consumes the interruption and reaches the graph.
        await agent.ask_supervisor(
            _FakeRunContext(), {"reason": "r", "last_caller_utterance": "actually make it Friday"}
        )
        # A backchannel arriving right behind it, while a filler for the still
        # in-flight turn is cut off again.
        agent._filler_handles.append(_FakeHandle(interrupted=True))
        with pytest.raises(StopResponse):
            await agent.ask_supervisor(
                _FakeRunContext(), {"reason": "r", "last_caller_utterance": "mhm"}
            )

    assert reached == ["actually make it Friday"], "the backchannel must not reach the graph"


async def test_interruption_is_consumed_so_the_guard_disarms(repos):
    # The other half of the contract: once a turn has taken responsibility for
    # an interruption, a LATER backchannel with no fresh interruption behind it
    # is ordinary input. Leaving the guard armed forever would silently swallow
    # every "yes" for the rest of the call.
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    agent._filler_handles = [_FakeHandle(interrupted=True)]
    reached = []

    async def fake_turn(_repos, _call_id, _tool_call_id, utterance):
        reached.append(utterance)
        return "ok", "booking"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        with pytest.raises(StopResponse):
            await agent.ask_supervisor(
                _FakeRunContext(), {"reason": "r", "last_caller_utterance": "mhm"}
            )
        await agent.ask_supervisor(
            _FakeRunContext(), {"reason": "r", "last_caller_utterance": "yes"}
        )

    assert reached == ["yes"]


async def test_acknowledgment_without_an_interrupted_filler_is_normal_input(repos):
    # The guard is scoped to filler interruptions specifically. A plain "yes"
    # answering a confirm-back question is the single most common real
    # utterance in the whole call and must always reach the graph.
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()
    seen = []

    async def fake_turn(_repos, _call_id, _tool_call_id, utterance):
        seen.append(utterance)
        return "You're booked.", "ended"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    assert seen == ["yes"]


# --- Decision 4: failure after the filler has played ------------------------


async def test_supervisor_failure_after_filler_still_returns_a_graceful_reply(repos):
    # Phase doc test 5. run_supervisor_turn absorbs LLMCallFailed into
    # _llm_failure_fallback's reply, so the filler having played changes
    # nothing — the caller is never left hanging.
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    slow = _slow_turn("Sorry, I'm having a little trouble — could you say that again?", "booking")
    with patch.object(livekit_agent, "run_supervisor_turn", slow):
        reply = await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    assert ctx.session.said, "filler played before the failure"
    assert "trouble" in reply
