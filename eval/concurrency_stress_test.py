"""python eval/concurrency_stress_test.py [--n-levels 5 10 20 40] [--mode mocked|live]

Phase 12 — proves the async dispatcher handles many *independent* concurrent
calls without blocking or leaking state between them, not just a single call
staying correct while it's the only one in flight (Phase 5's own tests, see
backend/tests/test_dispatcher_async.py, only ever exercise one call_id, or
concurrent turns for the SAME call_id).

Fires n distinct call_ids at backend.dispatcher.process_supervisor_call via
asyncio.gather (Decision 2 in docs/phases/phase-12-concurrency-stress-test.md)
— the dispatcher/asyncio/db layer, the same boundary eval/replay_scenarios.py
and backend/tests/test_scenarios.py already treat as "the real pipeline" for
their own purposes, and the one layer whose concurrency properties are this
project's own engineering (not OpenAI's/the network's).

Mocked mode patches backend.dispatcher.GRAPH.invoke with a SINGLE side_effect
callable keyed off each turn's own input state (its call_id), not per-call
`with patch(..., return_value=...)` blocks — see Decision 6 and the comment
above _mock_graph_invoke below for why the latter is actually unsafe under
true asyncio.gather concurrency (unittest.mock.patch mutates one shared
global attribute; N nested context managers entering/exiting out of strict
LIFO order — guaranteed once GRAPH.invoke calls suspend on real
asyncio.to_thread work — corrupt each other's restore state). This mirrors
backend/tests/test_dispatcher_async.py's own convention of mocking
GRAPH.invoke directly for its concurrency-focused tests (as opposed to
backend/tests/test_scenarios.py's tools.py-level mocking, used there for
business-logic fidelity, not concurrency).
"""

import argparse
import asyncio
import os
import statistics
import time
from typing import Callable, Optional
from unittest.mock import patch

from backend import dispatcher
from backend.config import settings
from backend.db.repositories import Repositories, get_repositories
from backend.db.repositories.connection import connect, reset_schema
from backend.dispatcher import process_supervisor_call
from backend.supervisor.state import CALL_STATES, get_or_create_state, new_call_state

# A dedicated SQLite file, not backend/db/calendar.db — this script's
# stress-* call_ids are throwaway synthetic data and must never show up
# next to real demo calls in the admin panel. Still real SQLite (not an
# in-memory fake), since exercising SQLite's actual single-writer behavior
# under concurrent writes is part of what this script measures (Decision 4).
STRESS_DB_PATH = "backend/db/calendar_stress_test.db"

DEFAULT_N_LEVELS = (5, 10, 20, 40)  # deliberately spans below/above the default asyncio.to_thread executor cap (Decision 3)
LIVE_SAFETY_CAP = 10  # bounds real API spend for an accidental large --mode live run
DEGRADATION_MULTIPLE = 1.5  # verdict threshold: per-call median must stay within this multiple of the smallest N's

FAKE_NODE_LATENCY_S = 0.05  # simulated per-turn node work, so wall-clock parallelism is actually measurable


def _seed_utterance(i: int) -> str:
    return (
        f"I need help with my tenancy. My name is Stress Caller {i}, "
        f"my email is stress{i}@example.com, my phone is 555000{i:04d}."
    )


def _seeded_values(i: int) -> dict:
    return {
        "name": f"Stress Caller {i}",
        "email": f"stress{i}@example.com",
        "phone": f"555000{i:04d}",
    }


def _mock_graph_invoke(state, config=None):
    # Deterministic purely from `state["call_id"]` — no closure over a
    # shared mutable index, no per-call `with patch(...)` block. Every
    # concurrent task reads only its own call_id's data no matter what
    # order asyncio.to_thread happens to run/finish these on (Decision 6).
    i = int(state["call_id"].rsplit("-", 1)[-1])
    time.sleep(FAKE_NODE_LATENCY_S)
    seeded = _seeded_values(i)
    profile = new_call_state(state["call_id"])["caller_profile"]
    for field, value in seeded.items():
        profile[field] = {"value": value, "confidence": 0.95, "status": "confirmed", "attempts": 1, "validated": True}
    return {
        **state,
        "stage": "capture",
        "practice_area": "tenancy",
        "caller_profile": profile,
        "pending_reply": f"Got it, {seeded['name']}.",
    }


def _check_leakage(call_id: str, i: int) -> bool:
    """True if any captured field doesn't match this call's own seeded
    value — i.e. it picked up a DIFFERENT call's data. Only checks fields
    that actually got captured (not None) so this works in --mode live too,
    where a single turn may not always populate every field."""
    profile = CALL_STATES[call_id]["caller_profile"]
    expected = _seeded_values(i)
    for field, expected_value in expected.items():
        actual = profile[field]["value"]
        if actual is not None and actual != expected_value:
            return True
    return False


async def _timed_call(repos: Repositories, call_id: str, tool_call_id: str, utterance: str) -> float:
    start = time.monotonic()
    await process_supervisor_call(repos, call_id, tool_call_id, utterance)
    return (time.monotonic() - start) * 1000.0


async def run_stress_level(n: int, mode: str, repos: Repositories) -> dict:
    """Fires n concurrent single-turn ask_supervisor calls, each a distinct
    call_id with a distinct seeded caller identity, and reports wall-clock
    time, per-call latency, and whether any call's final state picked up
    another call's data."""
    if mode == "live" and n > LIVE_SAFETY_CAP:
        raise ValueError(f"--mode live refuses n={n} > safety cap {LIVE_SAFETY_CAP} (bounds real API spend)")

    call_ids = [f"stress-{i}" for i in range(n)]
    for call_id in call_ids:
        get_or_create_state(call_id)

    async def _run_all() -> list[float]:
        tasks = [_timed_call(repos, call_id, f"tool-{i}", _seed_utterance(i)) for i, call_id in enumerate(call_ids)]
        return await asyncio.gather(*tasks)

    start = time.monotonic()
    if mode == "mocked":
        with patch("backend.dispatcher.send_over_bridge"), patch("backend.dispatcher.GRAPH.invoke", side_effect=_mock_graph_invoke):
            per_call_ms = await _run_all()
    else:
        with patch("backend.dispatcher.send_over_bridge"):
            per_call_ms = await _run_all()
    wall_clock_ms = (time.monotonic() - start) * 1000.0

    leakage_found = any(_check_leakage(call_id, i) for i, call_id in enumerate(call_ids))

    latency_breakdown = None
    try:
        from eval.insights_agent import latency_breakdown_percentiles

        latency_breakdown = latency_breakdown_percentiles(repos.trace, call_ids)
    except ImportError:
        pass

    return {
        "n": n,
        "wall_clock_ms": wall_clock_ms,
        "per_call_ms": per_call_ms,
        "mean_per_call_ms": statistics.mean(per_call_ms),
        "median_per_call_ms": statistics.median(per_call_ms),
        "p95_per_call_ms": sorted(per_call_ms)[max(0, int(0.95 * (len(per_call_ms) - 1)))],
        "cross_call_leakage_found": leakage_found,
        "latency_breakdown": latency_breakdown,
    }


def compute_verdict(results: list[dict]) -> dict:
    """The largest N at which per-call median latency stayed within
    DEGRADATION_MULTIPLE of the smallest N's own median — shared by the CLI
    report and the admin-panel stress-test page so both state the same
    verdict the same way."""
    baseline = results[0]["median_per_call_ms"]
    holds_through = results[0]["n"]
    for r in results:
        if r["median_per_call_ms"] <= DEGRADATION_MULTIPLE * baseline:
            holds_through = r["n"]
        else:
            break
    return {
        "holds_through_n": holds_through,
        "baseline_n": results[0]["n"],
        "baseline_median_ms": baseline,
        "degradation_multiple": DEGRADATION_MULTIPLE,
        "any_leakage": any(r["cross_call_leakage_found"] for r in results),
    }


def _print_report(results: list[dict]) -> None:
    print(f"{'N':>4}{'wall_clock_ms':>16}{'mean_ms':>12}{'median_ms':>12}{'p95_ms':>12}  leakage?")
    for r in results:
        print(
            f"{r['n']:>4}{r['wall_clock_ms']:>16.1f}{r['mean_per_call_ms']:>12.1f}"
            f"{r['median_per_call_ms']:>12.1f}{r['p95_per_call_ms']:>12.1f}  "
            f"{'YES' if r['cross_call_leakage_found'] else 'no'}"
        )

    verdict = compute_verdict(results)
    print(
        f"\nVerdict: the system holds up cleanly through N={verdict['holds_through_n']} "
        f"(per-call median latency stayed within {verdict['degradation_multiple']}x of "
        f"N={verdict['baseline_n']}'s {verdict['baseline_median_ms']:.1f}ms baseline)."
    )
    if verdict["any_leakage"]:
        print("WARNING: cross-call state leakage was detected at one or more N levels — see per-N table above.")


def build_stress_repos(db_path: str = STRESS_DB_PATH) -> Repositories:
    """An isolated SQLite db for stress runs — never backend/db/calendar.db,
    so synthetic stress-* call_ids never show up next to real demo calls (in
    the CLI table or the admin panel alike). Schema is (re)initialized only
    the first time this path is seen, not deleted-and-recreated on every
    call: the admin page's backend calls this once per run in a long-lived
    process, and deleting a file a previous run's sqlite3.Connection is
    still holding open fails outright on Windows (file-locking semantics
    differ from POSIX, where an unlinked-but-open file is fine) — reusing
    the existing schema sidesteps that instead of needing to track down and
    close every prior connection first."""
    if not os.path.exists(db_path):
        reset_schema(connect(db_path))
    stress_settings = settings.model_copy(update={"db_path": db_path})
    return get_repositories(stress_settings)


async def run_all_levels(
    n_levels: tuple[int, ...],
    mode: str,
    repos: Repositories,
    on_level_done: Optional[Callable[[dict], None]] = None,
) -> list[dict]:
    results = []
    for n in n_levels:
        CALL_STATES.clear()
        dispatcher.LOCKS.clear()
        dispatcher.SPEAKING.clear()
        dispatcher.DEFERRED.clear()
        dispatcher.CONNECTIONS.clear()
        result = await run_stress_level(n, mode, repos)
        results.append(result)
        print(f"N={n}: wall_clock={result['wall_clock_ms']:.1f}ms, leakage={result['cross_call_leakage_found']}")
        if on_level_done is not None:
            on_level_done(result)
    return results


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-levels", type=int, nargs="+", default=list(DEFAULT_N_LEVELS))
    parser.add_argument("--mode", choices=("mocked", "live"), default="mocked")
    parser.add_argument("--label", default=None, help="optional; not currently used to tag persisted rows")
    args = parser.parse_args(argv)

    repos = build_stress_repos()
    results = asyncio.run(run_all_levels(tuple(args.n_levels), args.mode, repos))
    print()
    _print_report(results)


if __name__ == "__main__":
    main()
