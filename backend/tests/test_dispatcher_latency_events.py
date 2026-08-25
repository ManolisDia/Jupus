import time
from unittest.mock import patch

import pytest

from backend import dispatcher
from backend.db.repositories import Repositories
from backend.dispatcher import on_bridge_message
from backend.supervisor.state import CALL_STATES, new_call_state
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


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


def _seed_state(call_id: str, stage: str = "routing") -> dict:
    state = new_call_state(call_id)
    state["stage"] = stage
    CALL_STATES[call_id] = state
    return state


async def test_ask_supervisor_records_received_event_before_spawning_task(repos):
    _seed_state("call-1")

    def slow_invoke(state, config=None):
        time.sleep(0.3)
        return {**state, "pending_reply": "done"}

    with patch("backend.dispatcher.GRAPH.invoke", side_effect=slow_invoke):
        await on_bridge_message(
            repos, "call-1",
            {"type": "ask_supervisor", "tool_call_id": "tool-1", "last_caller_utterance": "hi"},
        )
        # The synchronous part of on_bridge_message (recording the event)
        # completed before this point even returned control — the spawned
        # task itself is still running the slow_invoke above.
        events = repos.trace.get_trace("call-1")
        assert any(e["event_type"] == "ask_supervisor_received" for e in events)
        received = next(e for e in events if e["event_type"] == "ask_supervisor_received")
        assert received["payload"]["tool_call_id"] == "tool-1"


async def test_speech_stopped_now_records_trace_event(repos):
    # Regression test for the exact bug this phase fixes — speech_stopped's
    # record_event call was missing since Phase 6a, silently zeroing every
    # latency stat computed since. See docs/fixes/.
    _seed_state("call-1")

    await on_bridge_message(repos, "call-1", {"type": "speech_stopped"})

    events = repos.trace.get_trace("call-1")
    assert any(e["event_type"] == "speech_stopped" for e in events)


async def test_tts_first_audio_message_recorded_with_reported_fields(repos):
    await on_bridge_message(
        repos, "call-1",
        {"type": "tts_first_audio", "tool_call_id": "tool-1", "ms_since_reply_delivered": 250},
    )

    events = repos.trace.get_trace("call-1")
    event = next(e for e in events if e["event_type"] == "tts_first_audio")
    assert event["payload"]["tool_call_id"] == "tool-1"
    assert event["payload"]["ms_since_reply_delivered"] == 250


async def test_realtime_usage_message_recorded_with_reported_fields(repos):
    await on_bridge_message(
        repos, "call-1",
        {
            "type": "realtime_usage", "tool_call_id": "tool-1",
            "input_audio_tokens": 100, "output_audio_tokens": 50,
            "input_text_tokens": 10, "output_text_tokens": 5,
        },
    )

    events = repos.trace.get_trace("call-1")
    event = next(e for e in events if e["event_type"] == "realtime_usage")
    assert event["payload"]["input_audio_tokens"] == 100
    assert event["payload"]["output_text_tokens"] == 5
