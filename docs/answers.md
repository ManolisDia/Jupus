# Written Answers

One section per required question, referencing actual file paths and specific behavior — not
abstract description. Structure per `docs/phases/phase-15-polish-submission.md`.

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

**Phase 13 (latency reduction) update, 2026-08-25**: given `supervisor_processing` was confirmed the
dominant cost above, this phase attacked it directly — see `docs/phases/phase-13-latency-reduction.md`
and `docs/DECISIONS.md` for the full writeup of each change. Summary of what actually moved the
number (all measured via `eval/replay_scenarios.py` + `eval/compare_runs.py` against the real,
unmocked pipeline, not assumed):
- **Prompt caching** — shipped, measured, confirmed to have **zero effect**: this project's system
  prompts (~100-650 tokens per call) sit under Anthropic's 1024-token minimum cacheable length.
  Kept in code (free, activates automatically if prompts ever grow) but contributed nothing here.
- **Merging `extract_field`+`generate_confirm_back`** into one call (`tools.extract_and_confirm_field`)
  — the same real turn shape went from two round trips (1522ms + 2552ms = 4074ms) to one (3221ms),
  ~21% faster, with zero error-class regression.
- **Root-causing `confirm_field_answer`'s 10s+ retry tail** — a live call showed its `corrected_value`
  occasionally over-elaborating past `max_tokens=512`, truncating the JSON and forcing a retry.
  Fixed by constraining the prompt, not raising the token ceiling (`docs/fixes/2026-08-25-002.md`).
- **Per-tool model choice** — `select_offered_slot` (closed-set index selection, not free-text
  extraction) moved to Haiku after showing identical behavior to Sonnet on an ambiguous case and
  1092-1782ms vs. Sonnet's 3500ms on clear ones. `ground_statute_citation` (same closed-set shape,
  Phase 8's legal-citation grounding call) moved to Haiku next after identifying it as the single
  longest individual call remaining — identical statute selected by both models, 4090ms (Sonnet) vs.
  1367ms (Haiku). `confirm_field_answer`/`confirm_booking_answer`/`classify_practice_area` remain
  untested candidates for the same treatment.

**Net effect across the 6 canonical scenarios + 2 research variants** (total Claude tool-call time
across all 8 scenario calls, `phase13-baseline` vs. `phase13-final-v2` labels, real API, including
both Haiku swaps above): **132,991ms → 116,124ms total (16,624ms → 14,516ms average per call,
~12.7%)**, with zero error-class regression (`eval/compare_runs.py`). An earlier measurement
(`phase13-final`, before the `ground_statute_citation` swap) showed ~9% — kept in
`docs/phases/phase-13-latency-reduction.md`'s DoD as the historical record of what that phase's
original four items achieved on their own; this figure is the current combined total. Reported as
total/average tool-call duration rather than through the `stt_and_dialogue_decision`/`supervisor_processing`
stage breakdown above, because that breakdown depends on bridge-level events (`speech_stopped`,
`ask_supervisor_received`) that `eval/replay_scenarios.py` doesn't emit — it drives
`process_supervisor_call` directly, bypassing
the WebSocket bridge entirely (see that script's own docstring). The two metrics measure the same
underlying thing (the graph/Claude round-trip), just via different instrumentation; a live-call
re-measurement through the real bridge would be needed to update the stage-breakdown table itself.

### Phase 14: attacking the *perceived* wait, once the real one stopped shrinking

Phases 11-13 took the round trip about as far as prompt- and model-level work could: instrument it,
merge two calls into one, root-cause a retry tail, move two tools to Haiku. What that leaves is a
floor. `confirm_field_answer` still costs ~2.5s and a booking-proposal turn ~4.9s, because there is
a real model call in the middle and no amount of transport work removes it.

So Phase 14 attacks a different quantity: not how long the caller waits, but how long they wait *in
silence*. On most turns this project already hides the wait behind a real question — Phase 7 asks
for the next field while the previous one verifies in the background, Phase 8 asks a filler question
while a statute search runs. Three turns have no such cover, because on them the caller has just
answered and the reply *is* the next thing they are waiting for. Those three get a short, canned,
pre-rendered line ("Okay, one sec.") scheduled on a continuous-idle dwell.

Measured live over the real transport (`eval/filler_latency_report.py`, 18 turns):

| turns | n | round trip | time to first audio |
|---|---:|---:|---:|
| with a filler | 6 | 2484ms | **422ms** |
| without one | 12 | 766ms | **1796ms** (p95: 6342ms) |

**The left column is unchanged and is not claimed as an improvement** — it is the same Claude work
Phase 13 measured. What moves is when the caller stops hearing silence. The two rows are the
comparison rather than a before/after of identical turns: filler turns are deliberately the slow
ones (Decision 2 scopes filler to the sites where the caller has just answered and has nothing else
to do), and the "without" row is what those sites looked like before.

"Time to first audio" is a real playout signal, not the moment `say()` was called. An earlier
version of this measurement conflated the two and reported ~400ms for a filler clip that took 1.3s
to actually make a sound.

Two design points are what make this different from the filler this project tried and *removed*
back in Phase 2 (see `docs/DECISIONS.md`, "No filler acknowledgment ... reversed after live
testing"). That earlier attempt had the model narrate "one moment" at the top of every turn, and
the complaint was precise: a spoken promise followed by dead air reads worse than an unannounced
pause. So: (1) the filler fires only after the session has been continuously idle, meaning a fast
turn is never narrated at all and the filler can never talk over the caller; and (2) a turn still
running four seconds later gets a second line, so a long wait is re-acknowledged rather than
promised once and abandoned. The Phase 2 finding is treated as still correct — the mechanism is
what changed.

## Q2 — Turn-taking / interruptions

Turn-taking is deliberately **not** owned by this project. OpenAI Realtime's `semantic_vad` decides
when the caller has finished speaking, using the model's own judgement of utterance completion
rather than a silence timer — which is what handles "umm..." mid-sentence without a bespoke
end-of-turn model. `eagerness: "low"` plus `near_field` noise reduction were both tuned against
live failures where background noise was read as speech (`docs/DECISIONS.md`). Third-party
turn-detection packages were considered and rejected: they assume you own the raw audio stream,
which means disabling Realtime's own turn handling and rebuilding the chained-pipeline turn-taking
this project deliberately avoided.

Since Phase 14, the *transport* around that is LiveKit Agents (still hosting the same Realtime
model). That deleted two pieces of hand-rolled turn-taking machinery outright — a one-deep
`response.create` collision queue, and the dispatcher's own `SPEAKING`/`DEFERRED` bookkeeping that
decided whether a ready reply could be spoken yet. Both were real live bugs with their own entries
in `docs/fixes/`, and both are the kind of thing a transport library should own.

A third piece — the `transcriptionPending`/`awaitingToolCall` machinery — was **not** something
LiveKit takes over, and it is worth being precise about, because the migration initially dropped it
by mistake. It exists because `last_caller_utterance` is authored by the Realtime model rather than
passed through from speech recognition, and the model invents (`docs/DECISIONS.md`: a caller said
"manos44" and the graph received `manos44@example.com`). The fix was never a better prompt — that
was tried and recorded as unreliable — it was to prefer the real ASR transcript over the model's
argument, and to wait for it when the tool call wins the race. That behaviour is rebuilt on
LiveKit's `user_input_transcribed` and pinned by tests. The lesson generalises: a transport swap
can quietly delete a *feature* that looks like plumbing.

**Interruptions.** `interrupt_response: true` is kept, so a caller can barge in on the agent
mid-sentence. The interesting case Phase 14 added is barge-in *during a filler*, where the caller
talks over "Okay, one sec." while the real answer is still being computed. That needs a policy,
because the two possibilities want opposite handling:

- A **backchannel** ("mhm", "okay", "got it") means "I heard you, keep going." Feeding it to the
  graph as a real utterance would reroute the turn for no reason.
- A **substantive** interruption ("actually it's Alesh with an H") is a correction the caller
  needs heard, and dropping it makes them repeat themselves.

The distinction is a closed-token-set check (`looks_like_acknowledgment`, `heuristics.py`) —
deliberately not an LLM call, since that would reintroduce exactly the round trip the filler exists
to hide. Backchannels are dropped; substantive interruptions fall through and reach the graph as
the next turn's input, serialized behind the in-flight turn by the per-call lock. The check
diverges from its sibling `looks_like_bare_affirmation` on one point that matters: a bare "no" is
contentless for "did the caller answer the research question?", but over a filler it is a decline
that must not be swallowed — same word, opposite correct answer, because the cost of being wrong
differs.

**What isn't solved.** If a caller substantively corrects themselves mid-turn, the first turn's now
stale reply is still spoken before the correction is processed. The `/bridge` transport had the
same gap (its staleness check only dropped replies whose *stage* had changed, and both turns here
share a stage), so Phase 14 neither introduced nor fixed it. Fixing it properly means letting a
turn be cancelled once its input is superseded, which reaches into the graph rather than the
transport, and the phase scoped itself to transport deliberately.

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

**One real, live data point** (`--mode live --n-levels 5`, real Claude calls, capped spend,
2026-08-25): 5 concurrent real calls, wall-clock 2937ms vs. each individual call's own ~2203ms
median — i.e. 5 calls in parallel cost barely more than one call alone, not 5x, confirming the
mocked evidence's shape against a real API round-trip. All 5 independently classified the correct
practice area (`tenancy`) with no cross-call mixing in the stored transcripts. Narrower leakage
coverage than the mocked sweep, though — a single live turn only reaches practice-area
classification and the first capture question, not full name/email/phone extraction, so there was
less state to check for contamination than the mocked N=40 run's fully-populated profiles.

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
`docs/phases/phase-15-polish-submission.md`.*
