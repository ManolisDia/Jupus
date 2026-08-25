# Written Answers

One section per required question, referencing actual file paths and specific behavior — not
abstract description. Structure per `docs/phases/phase-13-polish-submission.md`.

---

## Q1 — Latency / pipeline design

Every real conversational turn is broken into four measured stages, recorded as `trace_events`
and aggregated by `eval.insights_agent.latency_breakdown_percentiles` /
`_stage_durations_for_call` (`eval/insights_agent.py`), surfaced in the admin panel's "Latency &
cost" panel and per-call at `GET /api/calls/{call_id}/latency`:

1. **`stt_and_dialogue_decision`** — caller stops talking (`speech_stopped`) → the backend
   receives the `ask_supervisor` tool call (`ask_supervisor_received`, recorded synchronously in
   `backend/dispatcher.py::on_bridge_message` before the supervisor task is spawned). This is
   OpenAI Realtime's own VAD/transcription/dialogue-decision pipeline — a black box from this
   project's side; this stage measures the *outside* of that box, not its internals, since owning
   Realtime's STT/turn-detection directly is out of scope.
2. **`supervisor_processing`** — the LangGraph/Claude round-trip itself, from
   `ask_supervisor_received` to `reply_delivered` (or `reply_deferred`, if the caller was still
   talking when the graph finished — see stage 3).
3. **`deferred_wait`** — Phase 5's "caller was still talking" queue delay. Deliberately **not**
   folded into `supervisor_processing`: conflating "how long did Claude take" with "how long did
   we wait for a natural pause to speak" would misattribute a scheduling artifact as model latency
   (`docs/DECISIONS.md`).
4. **`tts_first_audio`** — reply handed to Realtime → caller actually hears audio. Originally
   planned as a `response.audio.delta` data-channel event (per the phase doc); live testing showed
   WebRTC transport never delivers that event — audio flows over the peer connection's media track
   instead, not as data-channel deltas. `client/app.js` now detects first-audio via the same
   remote-stream amplitude analyser the caller-facing visualizer already runs every frame (see
   `docs/DECISIONS.md`'s revision note on this).

**Real observed numbers**, pooled across a small batch of logged calls (`GET /api/eval/summary`,
2026-08-25):

| Stage | avg | p50 | p95 |
|---|---|---|---|
| `stt_and_dialogue_decision` | 814ms | 767ms | 1199ms |
| `supervisor_processing` | 2830ms | 2476ms | 5638ms |
| `deferred_wait` | 0ms | 0ms | 0ms |
| `tts_first_audio` | 790ms | 752ms | 1005ms |
| `total_perceived` | 4885ms | 4638ms | 9196ms |

`supervisor_processing` is the dominant cost by a wide margin — the LangGraph/Claude round-trip,
not Realtime's own STT or the client's TTS-start detection, is where a caller actually waits. This
is exactly why the supervisor detour is kept off the hot path (fire-and-forget dispatch,
`asyncio.to_thread`, deferred-delivery — `backend/dispatcher.py`) rather than blocking the
Realtime session: Realtime itself handles STT+dialogue+TTS in one hop with no added latency; only
the supervisor detour adds a second hop, and that hop is where the real cost lives.

**Estimated cost**: `eval.pricing.estimate_cost_usd` (Claude Sonnet 5 + Realtime `gpt-realtime-2.1`
token usage, rates verified live against `claude.com/pricing` and
`developers.openai.com/api/docs/pricing` on 2026-08-24) — average **$0.024/call** observed across
the same batch (`GET /api/eval/summary`'s `cost.average_usd`), labeled "estimated" everywhere it's
shown per `docs/DECISIONS.md`'s pricing-verification caveat. Cost was raised directly as a concern
in conversation — this is a real, measured answer to it, not a guess.

## Q2 — Turn-taking / interruptions

*TBD — Phase 13 (polish/submission).*

## Q3 — Iteration / scaling / operational health

**Concurrency**: tested for real, not just assumed from the architecture — `eval/
concurrency_stress_test.py` fires N *independent* `call_id`s at
`backend.dispatcher.process_supervisor_call` via `asyncio.gather` (the dispatcher/asyncio/db layer
directly, not through N real browser/WebRTC sessions — see `docs/DECISIONS.md`'s Phase 12 entry
for why), against a real SQLite-backed `Repositories`, mocked-Claude (deterministic, free,
isolates this project's own concurrency behavior from Anthropic/OpenAI API variance). Real output,
`python eval/concurrency_stress_test.py --mode mocked`, 2026-08-25, this machine (20 logical CPUs):

| N | wall_clock_ms | mean_ms | median_ms | p95_ms | leakage? |
|---|---|---|---|---|---|
| 5 | 234.0 | 178.0 | 172.0 | 203.0 | no |
| 10 | 375.0 | 248.7 | 250.5 | 344.0 | no |
| 20 | 672.0 | 403.4 | 406.5 | 657.0 | no |
| 40 | 1235.0 | 675.2 | 671.5 | 1156.0 | no |

**Verdict: holds up cleanly through N=10** (per-call median latency stays within 1.5x of N=5's
baseline). Zero cross-call state leakage at every N — each of the N calls' final `caller_profile`
contained only its own seeded values, checked explicitly
(`backend/tests/test_concurrency_stress.py::test_no_cross_call_state_leakage`), not just inferred
from "nothing crashed". Degradation past N=10 traces to two already-known, deliberate tradeoffs
rather than a surprise: SQLite's single-writer behavior (already named in the README's "Known
limits") and the default `asyncio.to_thread` executor's `min(32, cpu_count+4)` worker cap (24 on
this machine) — both detailed with root-cause evidence in `docs/DECISIONS.md`'s Phase 12 entry,
including the one-line production fix for the latter
(`loop.set_default_executor(ThreadPoolExecutor(max_workers=N))`).

**Iteration loop**: `eval/replay_scenarios.py --label <name>` drives the 6 canonical scenarios
(`docs/scenarios.md`) through the real, unmocked pipeline; `eval/compare_runs.py --baseline
<a> --candidate <b>` diffs per-error-class rates between two labeled runs and exits 1 on
regression — a before/after-a-prompt-tweak loop, not manual re-listening. `eval/insights_agent.py`
(Phase 6b) LLM-classifies every logged call against the editable `eval/error_classes.py` taxonomy;
`eval/calibrate_judge.py` checks that classifier against the Benevolent Dictator's human
annotations (`docs/benevolent_dictator.md`) rather than trusting it blind.

**Operational health**: the admin panel's "Latency & cost" view (Q1) and error-class breakdown
(Phase 6) are the two numbers actually worth watching in production — a `supervisor_processing`
p95 regression points at prompt/model tuning, a `deferred_wait` p95 regression points at dispatcher
timing, and a taxonomy-rate regression points at conversational quality. The stress test above adds
a third: at what concurrent-call volume does either of those two start to move, and why.

## Q4 — Telephony / warm transfer / failure handling

*TBD — Phase 10 (telephony), if built. Design sketch otherwise per
`docs/phases/phase-13-polish-submission.md`.*
