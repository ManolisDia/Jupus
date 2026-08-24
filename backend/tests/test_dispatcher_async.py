import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from backend import dispatcher
from backend.db.repositories import Repositories
from backend.dispatcher import (
    drain_deferred,
    mark_call_abandoned,
    on_bridge_message,
    process_supervisor_call,
)
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


async def test_on_bridge_message_returns_without_awaiting_graph(repos):
    _seed_state("call-1")

    def slow_invoke(state, config=None):
        time.sleep(0.3)
        return {**state, "pending_reply": "done"}

    with patch("backend.dispatcher.GRAPH.invoke", side_effect=slow_invoke):
        start = time.monotonic()
        await on_bridge_message(
            repos, "call-1",
            {"type": "ask_supervisor", "tool_call_id": "tool-1", "last_caller_utterance": "hi"},
        )
        elapsed = time.monotonic() - start
    assert elapsed < 0.05


async def test_call_state_broadcast_after_successful_turn(repos):
    _seed_state("call-1")
    dispatcher.SPEAKING["call-1"] = False
    dispatcher.CONNECTIONS["call-1"] = object()

    with (
        patch(
            "backend.dispatcher.GRAPH.invoke",
            return_value={**CALL_STATES["call-1"], "stage": "capture", "practice_area": "employment", "pending_reply": "ok"},
        ),
        patch("backend.dispatcher.send_over_bridge"),
        patch("backend.dispatcher._send_json_safely") as send_spy,
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    send_spy.assert_called_once()
    payload = send_spy.call_args.args[1]
    assert payload["type"] == "call_state"
    assert payload["stage"] == "capture"
    assert payload["practice_area"] == "employment"
    assert set(payload["caller_profile"]) == {"name", "email", "phone"}


async def test_call_state_broadcast_skipped_when_no_connection(repos):
    _seed_state("call-1")
    dispatcher.SPEAKING["call-1"] = False
    # No dispatcher.CONNECTIONS entry for call-1 — must not raise.

    with (
        patch("backend.dispatcher.GRAPH.invoke", return_value={**CALL_STATES["call-1"], "pending_reply": "ok"}),
        patch("backend.dispatcher.send_over_bridge"),
        patch("backend.dispatcher._send_json_safely") as send_spy,
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    send_spy.assert_not_called()


async def test_call_state_broadcast_after_unhandled_exception(repos):
    _seed_state("call-1")
    dispatcher.CONNECTIONS["call-1"] = object()

    with (
        patch("backend.dispatcher.GRAPH.invoke", side_effect=RuntimeError("boom")),
        patch("backend.dispatcher.send_over_bridge"),
        patch("backend.dispatcher.tools.write_minimal_handoff_note"),
        patch("backend.dispatcher._send_json_safely") as send_spy,
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    send_spy.assert_called_once()
    payload = send_spy.call_args.args[1]
    assert payload["type"] == "call_state"
    assert payload["escalation_reason"] == "system_error"


async def test_result_delivered_immediately_when_not_speaking(repos):
    _seed_state("call-1")
    dispatcher.SPEAKING["call-1"] = False

    with (
        patch("backend.dispatcher.GRAPH.invoke", return_value={**CALL_STATES["call-1"], "pending_reply": "ok"}),
        patch("backend.dispatcher.send_over_bridge") as spy,
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    spy.assert_called_once_with("call-1", "tool-1", "ok")
    assert "call-1" not in dispatcher.DEFERRED


async def test_result_deferred_when_speaking(repos):
    _seed_state("call-1")
    dispatcher.SPEAKING["call-1"] = True

    with (
        patch("backend.dispatcher.GRAPH.invoke", return_value={**CALL_STATES["call-1"], "pending_reply": "ok"}),
        patch("backend.dispatcher.send_over_bridge") as spy,
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    spy.assert_not_called()
    assert len(dispatcher.DEFERRED["call-1"]) == 1
    assert dispatcher.DEFERRED["call-1"][0][0] == "tool-1"


async def test_greeting_start_chains_into_next_node_same_dispatch(repos):
    # Regression test: node_greeting is a silent, content-blind stage bump.
    # A fresh call must not stop there and speak nothing useful for the
    # caller's first real utterance — the dispatcher should immediately
    # chain into whatever node "routing" (or wherever greeting leads)
    # actually decides, within the same dispatch, so the caller gets a
    # real reply to what they actually said on turn one.
    _seed_state("call-1", stage="greeting")
    dispatcher.SPEAKING["call-1"] = False
    call_count = {"n": 0}

    def fake_invoke(state, config=None):
        call_count["n"] += 1
        if state["stage"] == "greeting":
            return {**state, "stage": "routing", "pending_reply": None}
        return {**state, "stage": "capture", "pending_reply": "Got it — this falls under employment law."}

    with (
        patch("backend.dispatcher.GRAPH.invoke", side_effect=fake_invoke),
        patch("backend.dispatcher.send_over_bridge") as spy,
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "I need help, my boss fired me")

    assert call_count["n"] == 2
    spy.assert_called_once_with("call-1", "tool-1", "Got it — this falls under employment law.")
    assert CALL_STATES["call-1"]["stage"] == "capture"


async def test_greeting_chain_not_triggered_when_already_past_greeting(repos):
    _seed_state("call-1", stage="routing")
    dispatcher.SPEAKING["call-1"] = False
    call_count = {"n": 0}

    def fake_invoke(state, config=None):
        call_count["n"] += 1
        return {**state, "stage": "capture", "pending_reply": "ok"}

    with patch("backend.dispatcher.GRAPH.invoke", side_effect=fake_invoke):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    assert call_count["n"] == 1


async def test_greeting_chain_not_triggered_when_explicit_request_escalates(repos):
    # is_explicit_human_request forces stage to "escalation" before the
    # graph ever sees "greeting" — the chain must not fire an extra invoke
    # on top of that real escalation.
    _seed_state("call-1", stage="greeting")
    dispatcher.SPEAKING["call-1"] = False
    call_count = {"n": 0}

    def fake_invoke(state, config=None):
        call_count["n"] += 1
        return {**state, "stage": "ended", "pending_reply": "I've passed this to our team."}

    with patch("backend.dispatcher.GRAPH.invoke", side_effect=fake_invoke):
        await process_supervisor_call(repos, "call-1", "tool-1", "let me talk to a person")

    assert call_count["n"] == 1


async def test_faq_tangent_answered_even_when_turn_otherwise_succeeds(repos):
    # Regression test: a caller utterance can carry BOTH real signal (that a
    # node correctly acts on) AND an unrelated side-question in the same
    # breath — e.g. "...my boss is trying to fire me... are you open
    # weekends?" classifies fine as employment, but nothing node-specific
    # ever looks at the weekends part. The FAQ check must run against the
    # raw utterance regardless of whether the node's own logic succeeded.
    _seed_state("call-1", stage="routing")
    dispatcher.SPEAKING["call-1"] = False

    with (
        patch(
            "backend.dispatcher.GRAPH.invoke",
            return_value={**CALL_STATES["call-1"], "stage": "capture", "pending_reply": "Got it — employment law."},
        ),
        patch("backend.dispatcher.send_over_bridge") as spy,
    ):
        await process_supervisor_call(
            repos, "call-1", "tool-1",
            "My boss is trying to fire me. Also, are you open on weekends?",
        )

    spy.assert_called_once()
    delivered_reply = spy.call_args.args[2]
    assert "Monday to Friday" in delivered_reply
    assert "Got it — employment law." in delivered_reply


async def test_faq_not_triggered_for_unrelated_utterance(repos):
    _seed_state("call-1", stage="routing")
    dispatcher.SPEAKING["call-1"] = False

    with (
        patch(
            "backend.dispatcher.GRAPH.invoke",
            return_value={**CALL_STATES["call-1"], "stage": "capture", "pending_reply": "Got it — employment law."},
        ),
        patch("backend.dispatcher.send_over_bridge") as spy,
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "My boss is trying to fire me.")

    spy.assert_called_once_with("call-1", "tool-1", "Got it — employment law.")


async def test_deferred_result_delivered_on_speech_stopped(repos):
    _seed_state("call-1", stage="routing")
    dispatcher.DEFERRED["call-1"] = [("tool-1", "queued reply", "routing", time.monotonic())]

    with patch("backend.dispatcher.send_over_bridge") as spy:
        drain_deferred(repos, "call-1")

    spy.assert_called_once_with("call-1", "tool-1", "queued reply")
    assert "call-1" not in dispatcher.DEFERRED


async def test_stale_deferred_result_dropped_on_speech_stopped(repos):
    _seed_state("call-1", stage="booking")
    dispatcher.DEFERRED["call-1"] = [("tool-1", "queued reply", "capture", time.monotonic())]

    with patch("backend.dispatcher.send_over_bridge") as spy:
        drain_deferred(repos, "call-1")

    spy.assert_not_called()


async def test_concurrent_calls_for_same_call_id_serialize(repos):
    _seed_state("call-1")
    order: list[int] = []
    call_index = {"n": 0}

    def fake_invoke(state, config=None):
        call_index["n"] += 1
        idx = call_index["n"]
        order.append(idx)
        time.sleep(0.01)
        return {**state, "pending_reply": f"reply-{idx}"}

    with patch("backend.dispatcher.GRAPH.invoke", side_effect=fake_invoke):
        t1 = asyncio.create_task(process_supervisor_call(repos, "call-1", "tool-1", "first"))
        t2 = asyncio.create_task(process_supervisor_call(repos, "call-1", "tool-2", "second"))
        await asyncio.gather(t1, t2)

    assert order == [1, 2]
    # second invocation's input state must include the first turn's appended transcript entry
    texts = [turn["text"] for turn in CALL_STATES["call-1"]["transcript"]]
    assert "first" in texts and "second" in texts


async def test_ended_call_short_circuits_without_invoking_graph(repos):
    _seed_state("call-1", stage="ended")

    with (
        patch("backend.dispatcher.GRAPH.invoke") as invoke_mock,
        patch("backend.dispatcher.send_over_bridge") as spy,
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    invoke_mock.assert_not_called()
    spy.assert_called_once_with("call-1", "tool-1", "This call has already been completed.")


async def test_immediate_delivery_records_reply_delivered_with_zero_wait(repos):
    _seed_state("call-1")
    dispatcher.SPEAKING["call-1"] = False

    with (
        patch("backend.dispatcher.GRAPH.invoke", return_value={**CALL_STATES["call-1"], "pending_reply": "ok"}),
        patch("backend.dispatcher.send_over_bridge"),
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    events = [e for e in repos.trace.get_trace("call-1") if e["event_type"] == "reply_delivered"]
    assert len(events) == 1
    assert events[0]["payload"]["was_deferred"] is False
    assert events[0]["payload"]["wait_ms"] == 0


async def test_deferred_then_delivered_records_nonzero_wait_ms(repos):
    _seed_state("call-1", stage="routing")
    dispatcher.DEFERRED["call-1"] = [("tool-1", "queued reply", "routing", time.monotonic() - 0.05)]

    with patch("backend.dispatcher.send_over_bridge"):
        drain_deferred(repos, "call-1")

    events = [e for e in repos.trace.get_trace("call-1") if e["event_type"] == "reply_delivered"]
    assert len(events) == 1
    assert events[0]["payload"]["was_deferred"] is True
    assert events[0]["payload"]["wait_ms"] > 0


async def test_dropped_stale_records_reply_dropped_stale_event(repos):
    _seed_state("call-1", stage="booking")
    dispatcher.DEFERRED["call-1"] = [("tool-1", "queued reply", "capture", time.monotonic())]

    with patch("backend.dispatcher.send_over_bridge"):
        drain_deferred(repos, "call-1")

    events = [e for e in repos.trace.get_trace("call-1") if e["event_type"] == "reply_dropped_stale"]
    assert len(events) == 1
    assert events[0]["payload"]["dispatch_stage"] == "capture"
    assert events[0]["payload"]["current_stage"] == "booking"


async def test_unexpected_exception_in_graph_invoke_delivers_fallback_not_dead_air(repos, tmp_path):
    _seed_state("call-1")

    with (
        patch("backend.dispatcher.GRAPH.invoke", side_effect=RuntimeError("boom")),
        patch("backend.dispatcher.send_over_bridge") as spy,
        patch.object(dispatcher.tools, "HANDOFFS_DIR", tmp_path),
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    spy.assert_called_once_with(
        "call-1", "tool-1", "Sorry, something went wrong on my end — let me get you to someone who can help."
    )
    assert CALL_STATES["call-1"]["stage"] == "ended"
    assert CALL_STATES["call-1"]["escalation_reason"] == "system_error"
    assert repos.calls.get("call-1") is not None


async def test_unexpected_exception_writes_handoff_note(repos):
    _seed_state("call-1")

    with (
        patch("backend.dispatcher.GRAPH.invoke", side_effect=RuntimeError("boom")),
        patch("backend.dispatcher.send_over_bridge"),
        patch("backend.dispatcher.tools.write_minimal_handoff_note") as note_spy,
        patch("backend.dispatcher.tools.write_handoff_note") as full_note_spy,
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    note_spy.assert_called_once()
    full_note_spy.assert_not_called()


async def test_unexpected_exception_records_trace_event(repos):
    _seed_state("call-1")

    with (
        patch("backend.dispatcher.GRAPH.invoke", side_effect=RuntimeError("boom")),
        patch("backend.dispatcher.send_over_bridge"),
        patch("backend.dispatcher.tools.write_minimal_handoff_note"),
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    events = [e for e in repos.trace.get_trace("call-1") if e["event_type"] == "unhandled_error"]
    assert len(events) == 1
    assert "boom" in events[0]["payload"]["error"]


async def test_exception_during_lock_hold_still_releases_lock(repos):
    _seed_state("call-1")

    with (
        patch("backend.dispatcher.GRAPH.invoke", side_effect=RuntimeError("boom")),
        patch("backend.dispatcher.send_over_bridge"),
        patch("backend.dispatcher.tools.write_minimal_handoff_note"),
    ):
        await process_supervisor_call(repos, "call-1", "tool-1", "hi")

    # A subsequent call for the same call_id must not deadlock.
    _seed_state("call-1", stage="ended")
    with patch("backend.dispatcher.send_over_bridge") as spy:
        await asyncio.wait_for(process_supervisor_call(repos, "call-1", "tool-2", "hi again"), timeout=1.0)
    spy.assert_called_once()


# --- cross-cutting.md section 2: WebSocket disconnect cleanup ---

async def test_disconnect_marks_call_abandoned(repos):
    _seed_state("call-1", stage="capture")

    await mark_call_abandoned(repos, "call-1")

    assert CALL_STATES["call-1"]["stage"] == "ended"
    row = repos.calls.get("call-1")
    assert row["outcome"] == "abandoned"
    assert row["ended_at"] is not None


async def test_disconnect_does_not_override_already_ended_call(repos):
    state = _seed_state("call-1", stage="ended")
    repos.calls.upsert(state, outcome_override="booked")

    await mark_call_abandoned(repos, "call-1")

    assert repos.calls.get("call-1")["outcome"] == "booked"


async def test_disconnect_clears_registries(repos):
    _seed_state("call-1", stage="capture")
    dispatcher.CONNECTIONS["call-1"] = object()
    dispatcher.SPEAKING["call-1"] = True
    dispatcher.DEFERRED["call-1"] = [("t", "r", "capture", time.monotonic())]

    await mark_call_abandoned(repos, "call-1")

    assert "call-1" not in dispatcher.CONNECTIONS
    assert "call-1" not in dispatcher.SPEAKING
    assert "call-1" not in dispatcher.DEFERRED


async def test_disconnect_waits_for_in_flight_turn_before_marking_abandoned(repos):
    # Regression test for docs/code-review-2026-08-24.md finding #1: a
    # disconnect landing mid-turn must not race the in-flight
    # process_supervisor_call — it must wait for that turn to finish (via
    # the same per-call lock) so whichever outcome is actually correct wins
    # deterministically, not whichever write happens to land last.
    #
    # GRAPH.invoke runs on a worker thread via asyncio.to_thread (see
    # process_supervisor_call), so the mock below is a plain synchronous
    # function coordinating with the test via threading.Event, not
    # asyncio.Event — an asyncio primitive created on the event-loop thread
    # isn't safely awaitable from a different OS thread.
    _seed_state("call-1", stage="capture")
    started = threading.Event()
    finish_turn = threading.Event()

    def slow_invoke(state, config=None):
        started.set()
        finish_turn.wait(timeout=5)
        return {**state, "stage": "ended", "booking_confirmed": True, "pending_reply": "done"}

    with (
        patch("backend.dispatcher.GRAPH.invoke", side_effect=slow_invoke),
        patch("backend.dispatcher.send_over_bridge"),
    ):
        turn_task = asyncio.create_task(process_supervisor_call(repos, "call-1", "tool-1", "hi"))
        await asyncio.get_event_loop().run_in_executor(None, started.wait, 5)

        abandon_task = asyncio.create_task(mark_call_abandoned(repos, "call-1"))
        await asyncio.sleep(0.05)
        assert not abandon_task.done()  # blocked behind the lock, not racing in

        finish_turn.set()
        await turn_task
        await abandon_task

    # The in-flight turn finished with stage="ended" (a real booking) before
    # mark_call_abandoned got the lock, so it correctly did NOT overwrite it.
    assert repos.calls.get("call-1")["outcome"] != "abandoned"
