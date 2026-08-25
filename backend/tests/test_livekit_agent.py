"""Phase 14 — the LiveKit agent's contract with everything below it.

These are unit tests over the agent's own logic (tool surface, filler
scheduling, interrupt policy, failure handling). They deliberately do NOT spin
up a LiveKit room or a Realtime session — the transport itself is verified by
the live scenario runs the phase DoD requires, which no mock can stand in for.
What's testable here is the part that has burned this codebase before: the
decision logic around a tool call racing the caller's next utterance.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

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

    def with_filler(self, source, *, delay, interval=None, max_steps=None):
        # Mirror the real scheduler's contract closely enough to assert on it:
        # record the schedule, and drive `source` through its steps so the
        # agent's own filler callback is genuinely exercised.
        self.filler_calls.append({"delay": delay, "interval": interval, "max_steps": max_steps})
        outer = self

        class _CM:
            async def __aenter__(self):
                step = 0
                while max_steps is None or step < max_steps:
                    if source(step) is None:
                        break
                    step += 1
                return None

            async def __aexit__(self, *exc):
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

    async def fake_turn(*_a, **_kw):
        # By now the filler has already been driven by the fake context.
        assert ctx.session.said, "filler should precede the supervisor's reply"
        return "You're booked.", "ended"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        reply = await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    spoken = [text for text, _ in ctx.session.said]
    assert spoken == list(FILLER_PHRASES["confirm_booking"])
    assert reply == "You're booked."


async def test_filler_is_scheduled_with_idle_delay_and_repeat(repos):
    from backend.supervisor.fillers import FILLER_IDLE_DELAY_SECONDS, FILLER_REPEAT_SECONDS

    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    async def fake_turn(*_a, **_kw):
        return "ok", "booking"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    # The delay is what stops a fast turn being narrated at all; the interval
    # is what stops a slow one being a promise followed by dead air.
    assert ctx.filler_calls == [
        {
            "delay": FILLER_IDLE_DELAY_SECONDS,
            "interval": FILLER_REPEAT_SECONDS,
            "max_steps": len(FILLER_PHRASES["confirm_booking"]),
        }
    ]


async def test_filler_is_kept_out_of_the_realtime_chat_context(repos):
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    async def fake_turn(*_a, **_kw):
        return "ok", "booking"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    assert all(add_to_ctx is False for _text, add_to_ctx in ctx.session.said)


async def test_filler_spoken_is_traced(repos):
    _seed("call-1", "booking", proposed_slot_id=7)
    agent = JupusAgent("call-1", repos)
    ctx = _FakeRunContext()

    async def fake_turn(*_a, **_kw):
        return "ok", "booking"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
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

    async def fake_turn(*_a, **_kw):
        return "You're booked.", "ended"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
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


# --- Decision 3: interrupt policy -------------------------------------------


async def test_acknowledgment_during_filler_does_not_reach_the_graph(repos):
    # Phase doc test 2. Without this guard every "mhm" over a filler becomes a
    # real utterance and reroutes the turn.
    from livekit.agents import StopResponse

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

    async def fake_turn(*_a, **_kw):
        return "Sorry, I'm having a little trouble — could you say that again?", "booking"

    with patch.object(livekit_agent, "run_supervisor_turn", fake_turn):
        reply = await agent.ask_supervisor(ctx, {"reason": "r", "last_caller_utterance": "yes"})

    assert ctx.session.said, "filler played before the failure"
    assert "trouble" in reply
