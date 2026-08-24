# Phase 12 — Concurrent-Call Stress Test

## Goal

Prove, with a real scripted run and real numbers, that this project's async dispatcher does what it's designed to do under load: many *different* calls in flight at once, none of them blocking each other, no cross-call state leakage. Right now the claim "this architecture handles production volume" rests on Phase 5's design and its own tests — which prove a single call stays correct while the caller keeps talking mid-turn, and that concurrent turns *for the same call_id* serialize correctly. Neither proves what the JD explicitly asks about: "many concurrent calls." This phase adds the one test that actually does.

## Prerequisite

Phase 5 (async dispatcher) DoD met. Genuinely synergizes with Phase 11 (latency instrumentation) if that's already built — this phase's report reuses `eval.insights_agent.latency_breakdown_percentiles` directly rather than inventing a second latency-measurement mechanism — but Phase 11 is not a hard blocker; if it isn't built yet, this phase measures simple total-wall-clock-time instead and skips the per-stage breakdown in its report.

## Why this exists

Phase 5's own test suite (`test_dispatcher_async.py`) is thorough about the things it set out to prove — deferred delivery, staleness checks, the unhandled-exception catch-all, same-`call_id` serialization — but every one of those tests exercises **one call_id at a time**. Nothing in this project's test suite today launches N *independent* calls concurrently and checks that they don't step on each other or degrade each other's latency. That's a real, specific gap between "the architecture is designed for concurrency" (true, and already true before this phase) and "the architecture has been shown to actually behave that way" (not yet true). This phase closes exactly that gap, with a script whose whole job is to produce a number: how many concurrent calls can this run comfortably, and does per-call latency hold up as that number grows.

## Non-goals

- **Not a real production-scale load test.** No thousands of calls, no distributed load generation, no multi-process/multi-machine setup. This proves single-process concurrent-call correctness and non-blocking behavior at a scale a laptop can genuinely produce (tens of calls, not thousands) — right-sized to what this take-home can credibly demonstrate, not a claim about true production capacity.
- **Not testing horizontal scaling.** `CALL_STATES`/`LOCKS`/`SPEAKING`/`DEFERRED`/`CONNECTIONS` are all in-memory, single-process dicts (already a documented tradeoff in the README's "Known limits" section) — this phase proves concurrency *within* that single-process design works correctly, it does not attempt to prove or improve multi-process scaling, which is out of scope by the README's own existing framing.
- **Not driving concurrency through the full transport stack.** N real browser tabs each holding a real WebRTC session and a real OpenAI Realtime connection would be expensive, slow to script, and would mostly be testing OpenAI's/the network's concurrency handling, not this project's. This phase drives concurrency at the `dispatcher`/`graph`/`db` layer directly — the layer whose concurrency properties are actually this project's own engineering, and the same boundary `eval/replay_scenarios.py` and `backend/tests/test_scenarios.py` already treat as "the real, unmocked pipeline" for their own purposes.
- **Not fixing whatever the stress test finds, as part of this phase.** If the run surfaces a genuine bottleneck (Decision 3 flags one likely candidate below), this phase's job is to *measure and report it honestly*, not to silently patch around it and only show the good numbers. A finding written up plainly is worth more here than a hidden fix — the JD asks about concurrency *management*, and knowing where your own ceiling is is part of that.

## Decisions made, not left open for the implementer

**1. Mocked-Claude mode is the default and the primary evidence; a small, capped live-API mode is optional, for one real recorded number.** Free, deterministic, repeatable, safe to run in CI (`pytest`), and — most importantly — isolates what this phase is actually testing (the dispatcher/asyncio/SQLite layer's concurrency behavior) from Anthropic/OpenAI's own API latency variance, which would otherwise dominate and obscure the measurement. Mocked mode reuses the exact same mocking approach `backend/tests/test_scenarios.py` already establishes for S1–S8. A live mode exists only to produce one real, small (N≈5, cost-capped) number worth citing in the video/README — never the primary evidence.

**2. Concurrency is simulated by firing N independent `call_id`s at `dispatcher.on_bridge_message`/`process_supervisor_call` via `asyncio.gather`, not by spinning up N sequential fixtures and hoping the event loop interleaves them.** `asyncio.gather` launches all N supervisor calls in the same tick, which is what actually exercises the concurrent-`call_id` path (distinct `asyncio.Lock()` per `call_id`, distinct `asyncio.to_thread` dispatches for each `GRAPH.invoke`) rather than accidentally testing a serial loop that merely *looks* concurrent from the outside.

**3. The stress test explicitly measures whether `asyncio.to_thread`'s default executor pool becomes the real bottleneck at higher N — a genuine, testable hypothesis, not an assumption.** Every `GRAPH.invoke` call runs via `asyncio.to_thread` (Phase 5's design, since LangGraph's node functions make blocking Claude calls). Python's default `ThreadPoolExecutor` caps concurrent worker threads at `min(32, os.cpu_count() + 4)` unless a different executor is explicitly configured. If N exceeds that cap, calls beyond it queue at the thread-pool level even though each has its own `asyncio.Lock` and would otherwise run fully in parallel — a real, discoverable constraint worth reporting rather than a hidden explanation for "latency degraded at N=40." The script deliberately runs at more than one N (e.g. 5, 10, 20, 40) specifically to surface whether/where this ceiling shows up, and reports it plainly if found rather than only running below it.

**4. SQLite's single-writer behavior is an accepted, already-documented constraint, not something this phase treats as a bug to work around.** The README's "Known limits" already names SQLite (vs. a hosted DB) as a deliberate tradeoff. This phase doesn't attempt to prove SQLite handles heavy concurrent writes gracefully — it reports what actually happens (e.g. a write briefly serializing/retrying under `sqlite3`'s locking) as one more honest data point, consistent with the same "don't hide the ceiling" spirit as Decision 3.

**5. The script reuses Phase 11's latency-breakdown infrastructure when available, rather than building a second, parallel measurement path.** If `eval.insights_agent.latency_breakdown_percentiles` exists (Phase 11 built), the stress-test script calls it directly, scoped to just the `call_id`s it generated, to report per-stage p50/p95 *under concurrent load* — directly comparable to Phase 11's own single-call-at-a-time baseline numbers. If Phase 11 isn't built yet, the script falls back to a simpler total-wall-clock-per-call measurement using its own `time.monotonic()` calls, and the report says plainly which mode it ran in.

**6. Correctness (no cross-call leakage) is checked explicitly, not just inferred from "nothing crashed."** Each simulated call is seeded with a distinct, easily-attributable fake caller profile (e.g. `name = f"stress-caller-{i}"`, a distinct scripted email per index) so the script can assert, after all N complete, that every call's final state contains *only* its own seeded values — catching the specific class of bug this project has already hit once for real (the Phase 7 cross-call/cross-field mock-attribution bug documented in `docs/fixes/`), just now checked deliberately under real concurrency instead of by accident.

---

## `eval/concurrency_stress_test.py` (new script, same category as `replay_scenarios.py`/`compare_runs.py`/`calibrate_judge.py`)

```python
DEFAULT_N_LEVELS = (5, 10, 20, 40)   # deliberately spans below/above the default executor cap (Decision 3)

def run_stress_level(n: int, mode: str, repos: Repositories) -> dict:
    """Generates n distinct call_ids, each seeded with a distinct scripted
    caller profile, fires n concurrent single-turn ask_supervisor calls at
    dispatcher.process_supervisor_call via asyncio.gather, and returns:
    {
      "n": n,
      "wall_clock_ms": <total time for all n to complete>,
      "per_call_ms": [<n individual completion times>],
      "cross_call_leakage_found": <bool>,
      "latency_breakdown": <Phase 11's per-stage p50/p95 if available, else None>,
    }
    In "mocked" mode, tools.extract_field/classify_practice_area/etc. are
    monkeypatched the same way backend/tests/test_scenarios.py already does
    — deterministic, free, no real API calls. In "live" mode, no mocking;
    n is expected to be small (script warns/refuses above a hardcoded safety
    cap, e.g. n > 10, to bound real API spend for an accidental large run).
    """
    ...

def main():
    # argparse: --n-levels (defaults to DEFAULT_N_LEVELS), --mode mocked|live
    # (default mocked), --label (for saving a comparable report, same
    # convention as replay_scenarios.py's --label).
    # Runs run_stress_level for each N, prints a per-N summary table
    # (n, wall_clock_ms, mean/median per-call ms, leakage found y/n, and
    # the latency breakdown if Phase 11 is present), and a final one-line
    # verdict: the largest N at which per-call latency stayed within some
    # multiple (e.g. 1.5x) of the smallest N's per-call latency — i.e. "the
    # system holds up cleanly through N=X" stated as one plain number.
    ...
```

---

## Tests

### `backend/tests/test_concurrency_stress.py` (new file — automated, smaller N than the manual script's higher levels, runs in `pytest`/CI)

1. `test_n_concurrent_different_calls_all_complete` — `N=20` distinct `call_id`s fired via `asyncio.gather`; assert all 20 reach the expected post-turn stage with no exceptions raised.
2. `test_no_cross_call_state_leakage` — same 20-call run, each seeded with a distinct scripted name/email; assert every call's final `caller_profile` contains only its own seeded values (Decision 6) — this is the single most important test in this file.
3. `test_concurrent_wall_clock_is_substantially_less_than_serial` — run N=10 concurrently, measure wall-clock; separately run the same N=10 sequentially (`await`ed one at a time, not gathered); assert the concurrent run's wall-clock is meaningfully lower than the serial run's (a loose bound, e.g. `< 0.6 *` serial time, chosen to be robust against normal test-machine timing noise rather than a tight assertion that could flake) — this is the actual proof of genuine parallelism, not just "didn't error."
4. `test_same_call_id_still_serializes_under_concurrent_load` — a regression guard: mixing a few repeated `call_id`s into an otherwise-all-distinct batch of concurrent calls; assert the repeated ones still serialize correctly per Phase 5's existing per-`call_id` lock (this phase adds cross-call concurrency, it must not accidentally weaken same-call-id correctness in the process).
5. `test_thread_pool_saturation_detected_at_high_n` — `N` deliberately set above the default executor's worker cap (or the test explicitly configures a small custom executor to make the ceiling reachable without needing 32+ real threads); assert the script's own measurement correctly shows increased queuing/latency at that N rather than silently misreporting it as "still fine" (validates Decision 3's detection logic itself, not just that concurrency generally works).

---

## Worked example

1. `python eval/concurrency_stress_test.py --mode mocked` runs the default N levels (5, 10, 20, 40).
2. At N=5 and N=10: wall-clock stays close to a single call's own latency, confirming real parallelism — 10 calls don't take 10x one call's time.
3. At N=20 or N=40 (depending on the machine's `os.cpu_count()`): per-call latency starts climbing measurably, and the report explicitly attributes this to thread-pool queuing (Decision 3) rather than leaving it as an unexplained number — e.g. "per-call p95 latency degraded 2.3x at N=40 vs. N=5; consistent with the default asyncio.to_thread executor's worker cap (`min(32, cpu_count+4)` = 16 on this machine) being exceeded."
4. `cross_call_leakage_found: false` at every N — the thing that actually mattered most going in, confirmed rather than assumed.
5. `python eval/concurrency_stress_test.py --mode live --n-levels 5` (capped, real API calls) produces one real, small, citable number for the video: "5 concurrent real calls, all completed correctly, wall-clock Xms vs. a single call's own ~Yms."

---

## Definition of Done

- [ ] `pytest backend/tests/test_concurrency_stress.py` — all 5 tests pass, including the leakage check (#2) and the serial-vs-concurrent wall-clock proof (#3).
- [ ] `python eval/concurrency_stress_test.py --mode mocked` run and its output saved/quoted somewhere referenced by `docs/answers.md`'s Q3 answer — a real table of N vs. wall-clock vs. leakage-found, not a description of what such a table would probably show.
- [ ] If thread-pool saturation is actually observed at the higher N levels (Decision 3): documented plainly in `docs/DECISIONS.md` as a known, measured ceiling — including what raising it would look like (`loop.set_default_executor(ThreadPoolExecutor(max_workers=N))`, a one-line production config change) — rather than silently only reporting the N levels that looked good.
- [ ] `docs/DECISIONS.md` entry: why concurrency is tested at the dispatcher layer directly rather than through the full transport stack (Decision 2's reasoning), and the SQLite single-writer caveat (Decision 4) restated in this context alongside the README's existing mention of it.
- [ ] Optional: one small, capped live-API run (`--mode live --n-levels 5` or similar), its real numbers quoted directly in the video alongside the mocked report, clearly labeled as the one live data point among otherwise-mocked evidence.
- [ ] `docs/answers.md`'s Q3 answer (iteration/scaling/health) updated to cite this phase's real numbers directly — "tested up to N concurrent calls, held up through X, degraded due to Y at higher N" — instead of describing the async architecture in the abstract.

---

## Note on scope relative to Phase 11

This phase and Phase 11 (latency + cost instrumentation) are deliberately kept as separate docs even though this one leans on Phase 11's aggregation code (Decision 5) — Phase 11 is about *what gets measured on every real call, always*; this phase is about *one deliberate, scripted stress run*, not something that happens on every call. Building Phase 11 first is worth doing if both are in flight, purely because it means this phase's report gets the richer per-stage breakdown instead of a single wall-clock number — but neither phase blocks the other structurally.
