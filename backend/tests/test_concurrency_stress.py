"""Phase 12 — the automated (smaller-N, CI-friendly) half of the concurrency
stress test; eval/concurrency_stress_test.py is the manual, higher-N script
these tests share a mocking strategy with (see that module's docstring for
why GRAPH.invoke is mocked directly, keyed off each call's own call_id,
rather than per-call `with patch(..., return_value=...)` blocks).

Phase 5's own test_dispatcher_async.py already proves same-call_id
serialization and single-call correctness; nothing in this project's suite
before this file ever launched N *distinct* call_ids concurrently and
checked they don't step on each other. That's the gap this file closes.
"""

import asyncio
import functools
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from backend import dispatcher
from backend.db.repositories import Repositories
from backend.dispatcher import process_supervisor_call
from backend.supervisor.state import CALL_STATES, new_call_state
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository
from eval.concurrency_stress_test import _check_leakage, _mock_graph_invoke, _seed_utterance


def _seed_state(call_id: str, stage: str = "routing") -> dict:
    # Seeded past "greeting" deliberately — node_greeting's silent stage-bump
    # makes dispatcher.py chain in a SECOND GRAPH.invoke within the same
    # dispatch (see dispatcher.py's own comment on stage_before == "greeting"),
    # which would otherwise double-count invocations in this file's
    # call-count-sensitive tests for reasons unrelated to concurrency itself.
    state = new_call_state(call_id)
    state["stage"] = stage
    CALL_STATES[call_id] = state
    return state


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


async def _fire_n_distinct_calls(repos: Repositories, n: int) -> list[str]:
    call_ids = [f"stress-{i}" for i in range(n)]
    for call_id in call_ids:
        _seed_state(call_id)
    with patch("backend.dispatcher.send_over_bridge"), patch(
        "backend.dispatcher.GRAPH.invoke", side_effect=_mock_graph_invoke
    ):
        await asyncio.gather(
            *(process_supervisor_call(repos, call_id, f"tool-{i}", _seed_utterance(i)) for i, call_id in enumerate(call_ids))
        )
    return call_ids


async def test_n_concurrent_different_calls_all_complete(repos):
    n = 20
    call_ids = await _fire_n_distinct_calls(repos, n)

    assert len(call_ids) == n
    for call_id in call_ids:
        assert CALL_STATES[call_id]["stage"] == "capture"


async def test_no_cross_call_state_leakage(repos):
    n = 20
    call_ids = await _fire_n_distinct_calls(repos, n)

    for i, call_id in enumerate(call_ids):
        assert not _check_leakage(call_id, i), f"{call_id} picked up another call's seeded data"


async def test_concurrent_wall_clock_is_substantially_less_than_serial(repos):
    n = 10

    concurrent_call_ids = [f"concurrent-{i}" for i in range(n)]
    for call_id in concurrent_call_ids:
        _seed_state(call_id)
    with patch("backend.dispatcher.send_over_bridge"), patch(
        "backend.dispatcher.GRAPH.invoke", side_effect=_mock_graph_invoke
    ):
        start = time.monotonic()
        await asyncio.gather(
            *(
                process_supervisor_call(repos, call_id, f"tool-{i}", _seed_utterance(i))
                for i, call_id in enumerate(concurrent_call_ids)
            )
        )
        concurrent_elapsed = time.monotonic() - start

    CALL_STATES.clear()
    dispatcher.LOCKS.clear()

    serial_call_ids = [f"serial-{i}" for i in range(n)]
    for call_id in serial_call_ids:
        _seed_state(call_id)
    with patch("backend.dispatcher.send_over_bridge"), patch(
        "backend.dispatcher.GRAPH.invoke", side_effect=_mock_graph_invoke
    ):
        start = time.monotonic()
        for i, call_id in enumerate(serial_call_ids):
            await process_supervisor_call(repos, call_id, f"tool-{i}", _seed_utterance(i))
        serial_elapsed = time.monotonic() - start

    # Loose bound (not tight) to stay robust against normal test-machine
    # timing noise — the point is proving genuine parallelism, not pinning
    # an exact speedup ratio.
    assert concurrent_elapsed < 0.6 * serial_elapsed


async def test_same_call_id_still_serializes_under_concurrent_load(repos):
    # Regression guard: mixing repeated call_ids into an otherwise-all-distinct
    # concurrent batch must not weaken Phase 5's existing per-call_id lock.
    order: dict[str, list[int]] = {"repeated-1": [], "repeated-2": []}
    counters = {"repeated-1": 0, "repeated-2": 0}

    def tracking_invoke(state, config=None):
        call_id = state["call_id"]
        if call_id in order:
            counters[call_id] += 1
            order[call_id].append(counters[call_id])
            time.sleep(0.01)
        return _mock_graph_invoke(state, config)

    call_ids = ["repeated-1", "repeated-1", "repeated-2", "repeated-2"] + [f"distinct-{i}" for i in range(6)]
    for call_id in set(call_ids):
        _seed_state(call_id)

    with patch("backend.dispatcher.send_over_bridge"), patch(
        "backend.dispatcher.GRAPH.invoke", side_effect=tracking_invoke
    ):
        await asyncio.gather(
            *(
                process_supervisor_call(repos, call_id, f"tool-{i}", _seed_utterance(i))
                for i, call_id in enumerate(call_ids)
            )
        )

    assert order["repeated-1"] == [1, 2]
    assert order["repeated-2"] == [1, 2]


async def test_thread_pool_saturation_detected_at_high_n(repos):
    # Configures a deliberately small custom executor (via a patched
    # asyncio.to_thread, not loop.set_default_executor — the loop's real
    # default executor is created lazily as None and asyncio refuses to
    # restore that sentinel, so mutating it directly leaves no safe way
    # back) so the default-pool ceiling (Decision 3) is reachable without
    # needing 32+ real threads. Proves the script's own timing measurement
    # correctly surfaces the resulting queuing as increased per-call
    # latency, not silently misreporting a saturated run as "still fine".
    n = 6
    small_pool = ThreadPoolExecutor(max_workers=2)

    async def limited_to_thread(func, /, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(small_pool, functools.partial(func, *args, **kwargs))

    try:
        call_ids = [f"saturate-{i}" for i in range(n)]
        for call_id in call_ids:
            _seed_state(call_id)

        with (
            patch("backend.dispatcher.send_over_bridge"),
            patch("backend.dispatcher.GRAPH.invoke", side_effect=_mock_graph_invoke),
            patch("backend.dispatcher.asyncio.to_thread", side_effect=limited_to_thread),
        ):
            start = time.monotonic()
            durations = await asyncio.gather(
                *(
                    _timed(repos, call_id, f"tool-{i}", _seed_utterance(i))
                    for i, call_id in enumerate(call_ids)
                )
            )
            wall_clock = time.monotonic() - start
    finally:
        small_pool.shutdown(wait=True)

    # With max_workers=2 and 6 calls each holding a worker thread for
    # FAKE_NODE_LATENCY_S, calls must queue in batches of 2 — wall-clock
    # should land near 3x a single call's own latency, not near 1x (what a
    # genuinely unbottlenecked pool would show). This is the detection
    # itself, not just "concurrency generally works".
    from eval.concurrency_stress_test import FAKE_NODE_LATENCY_S

    assert wall_clock > 2 * FAKE_NODE_LATENCY_S
    assert max(durations) > 2 * FAKE_NODE_LATENCY_S * 1000


async def _timed(repos, call_id, tool_call_id, utterance):
    start = time.monotonic()
    await process_supervisor_call(repos, call_id, tool_call_id, utterance)
    return (time.monotonic() - start) * 1000.0
