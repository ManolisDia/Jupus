# Tracing and Observability

Every decision the supervisor makes leaves a row in `trace_events`. That table is the substrate for the admin drill-in, the live graph view, the latency breakdown, the cost accounting, and the eval judge's evidence.

The claim "every tool call is traced" is true **by construction**, not by discipline: nodes never call `tools.py` functions directly (rule 8), and `call_claude_tool` is built on top of `traced_call` rather than duplicating it.

---

## The two wrappers

```python
traced_call(trace_repo, call_id, node, tool_name, fn, *args, **kwargs)
```
Emits `tool_call_start`, runs `fn`, emits `tool_call_end` with `duration_ms` and `success`. On exception it emits `tool_call_end` with `success=False` and the error, then **re-raises**. Arguments and results are truncated by `summarize()` to 500 characters.

```python
call_claude_tool(trace_repo, call_id, node, tool_name, fn, *args, model=None, **kwargs)
```
Wraps `traced_call`, and additionally emits `llm_usage` after a successful call, `llm_retry` on the first failure, and `llm_call_failed` before raising `LLMCallFailed`.

**Use `call_claude_tool` for anything that reaches Claude, `traced_call` for everything else** — deterministic tools and repository calls alike.

---

## Event reference

`node` is the node name (`greeting`, `routing`, `capture`, `capture_fast`, `booking`, ...) or one of `transport`, `dispatcher`, `eval_judge`.

### Node lifecycle

| Event | Emitted by | Payload |
|---|---|---|
| `node_entered` | every node, first thing | — |
| `node_exited` | every node, on every return path | `stage_from`, `stage_to`, `pending_reply` |

`node_exited` is what makes stage transitions readable without diffing state. Every return path in every node emits one — if you add a branch and forget it, the trace silently loses that transition.

### Tool calls

| Event | Payload |
|---|---|
| `tool_call_start` | `tool_name`, `args` (truncated repr) |
| `tool_call_end` | `tool_name`, `duration_ms`, `success`, then `result_summary` **or** `error` |

### Claude

| Event | Payload |
|---|---|
| `llm_usage` | `tool_name`, `model`, `input_tokens`, `output_tokens`, `cache_write_tokens`, `cache_read_tokens` |
| `llm_retry` | `tool_name`, `attempt: 1`, `error` |
| `llm_call_failed` | `tool_name`, `error` — the second failure, immediately before `LLMCallFailed` |

`model` is read off the response, not off `MODEL_ID`, so a per-call Haiku override is priced correctly.

### Transport (all `node="transport"`)

| Event | When | Payload |
|---|---|---|
| `speech_stopped` | `user_state_changed → listening` | — |
| `ask_supervisor_received` | top of the tool, before any work | `tool_call_id` |
| `reply_ready` | supervisor returned | `tool_call_id`, `reply`, `dispatch_stage`, `filler_played` |
| `first_audio` | agent state → speaking, **whichever came first, filler or reply** | `tool_call_id`, `ms_since_turn_start` |
| `tts_first_audio` | same moment, narrower meaning | `tool_call_id`, `ms_since_reply_delivered` |
| `filler_spoken` | a filler line started | `filler` (key), `step` (0 or 1) |
| `filler_interruption_ignored` | a backchannel over a filler was dropped | `utterance` |
| `realtime_usage` | once, at session shutdown | `input_audio_tokens`, `output_audio_tokens`, `input_text_tokens`, `output_text_tokens` |

`first_audio` and `tts_first_audio` fire from the same signal but answer different questions, and collapsing them would double-count supervisor time. `first_audio` measures *how long the caller sat in silence* — Phase 14's question. `tts_first_audio` measures *supervisor answered → reply audible* — Phase 11's, and `total_perceived`'s arithmetic depends on it keeping that narrower meaning.

Both are driven by a **real playout signal** (LiveKit's agent state entering `speaking`), not by the moment `say()` was called. An earlier version timed the call site and reported ~400ms for a clip that took 1.3s to make a sound, because 890ms of silence was baked into the front of the WAV.

`realtime_usage` carries **cumulative session totals**, emitted once at shutdown. Recording every `session_usage_updated` would multiply real cost by the number of turns. If no usage was captured, the agent logs a loud warning — silently recording nothing would look identical to a genuinely free call.

### Capture diagnostics

| Event | Meaning |
|---|---|
| `capture_fast_pending_confirm_fallback` | The fast pass fell back because `last_asked_field` was already `pending_confirm` |
| `capture_fast_gate_fallback` | Fell back on a tangent / shape / human-request gate. Includes the `utterance`. |
| `capture_fast_delayed_failure_reask` | A background verification failed; the caller is being interrupted and re-asked |
| `research_gather_bare_affirmation_fallback` | The research answer was pure acknowledgment; re-asking once |

**These four are how you debug optimistic capture.** A call that feels like it is asking the same thing twice will have one of them in its trace explaining exactly why.

### Call lifecycle

| Event | Emitted by | Payload |
|---|---|---|
| `call_ended` | dispatcher, when `stage == "ended"` | `outcome` |
| `call_abandoned` | `mark_call_abandoned` on disconnect | — |
| `unhandled_error` | the dispatcher's outer `except` | `error` |

An `unhandled_error` in a trace means something escaped every intended handler. Treat it as a bug, not as noise.

### Legacy events — read but never written

`reply_delivered` and `reply_deferred` belonged to the retired `/bridge` transport's deferred-delivery bookkeeping. **No production code emits them any more.** `eval/insights_agent.py` still reads both so historical traces stay parseable. If you see them, you are looking at a pre-Phase-14 call.

---

## From trace to latency

`eval/insights_agent.py::_stage_durations_for_call` walks one call's events in order and matches each turn's boundaries by `tool_call_id`.

```
speech_stopped ──► ask_supervisor_received ──► reply_ready ──► tts_first_audio
      └─ stt_and_dialogue_decision ─┘└─ supervisor_processing ─┘└─ tts_first_audio ─┘
                                    (deferred_wait: an honest 0 under LiveKit)

total_perceived = the sum of all four, per turn
```

| Stage | Measures |
|---|---|
| `stt_and_dialogue_decision` | Caller stopped speaking → the tool call arrived. OpenAI's territory. |
| `supervisor_processing` | The tool call arrived → the supervisor answered. **Phase 13's territory** — the only one this project's own code can shorten. |
| `deferred_wait` | Under `/bridge`, time spent queued because the caller was mid-sentence. Under LiveKit, LiveKit's turn-taking owns that decision, so this is a real, honest **zero** rather than a missing measurement. |
| `tts_first_audio` | The supervisor answered → the reply is audible. |
| `total_perceived` | The sum, per turn. |

The function is deliberately forgiving: a turn missing an expected boundary contributes no data for that stage rather than raising or fabricating a number.

`latency_breakdown_percentiles` pools these across many calls and returns `{p50, p95, avg}` per stage, using a nearest-rank percentile — adequate for a local eval tool, not a load-testing-grade implementation.

> This machinery is Phase 11's replacement for an earlier version that had been **silently reading zero since Phase 6a**: it looked for a `user_message` event that was never actually emitted (`docs/fixes/2026-08-24-012.md`). If a latency number looks impossibly clean, check that the events it depends on are really being written.

### Perceived vs. actual

`eval/filler_latency_report.py` reports two durations for every turn, both anchored on `ask_supervisor_received`: **round trip** to `reply_ready`, and **time to audio** to `first_audio`. Phase 14 moved the second and deliberately left the first alone — the Anthropic round trip is fixed by the SDK call inside `llm_utils.py` and is identical regardless of transport. The script exists so that claim is a measurement rather than an assertion.

---

## From trace to cost

`_cost_for_call` sums every `llm_usage` and `realtime_usage` event in one call's trace.

- **Claude** is priced **per event**, by the model that event actually recorded, via `estimate_claude_cost_usd`. A single call can mix Sonnet and Haiku. An unrecognised model id falls back to the **Sonnet** rate — an overestimate worth investigating beats an underestimate that hides real spend.
- **Realtime** is summed into audio/text in/out token totals and priced by `estimate_cost_usd`.
- A call with no usage events returns all-zero totals and `$0.00` without raising.

Rates live in `eval/pricing.py`, hardcoded, with the date each was confirmed in the docstring. **Every place a cost is displayed must be labelled "estimated"**, and you should verify against published pricing before trusting any figure — this file is not kept in sync automatically.

Cost is captured **at the source**, from actual token counts, never estimated from call duration or turn count.

---

## Reading a trace

**In the admin panel.** `/admin` → click a call → the transcript and the full ordered trace, plus that call's own latency and cost breakdown. `/admin/graph.html` shows a live call moving through the graph in real time.

**From the API.** `GET /api/calls/{call_id}/trace` and `GET /api/calls/{call_id}/latency`.

**In the terminal.** The `check-backend-logs` skill tails `backend.log` and cross-references it against `trace_events` for a given `call_id`. Use it instead of asking someone to paste terminal output. This is why the run command in [`CLAUDE.md`](../../CLAUDE.md) redirects uvicorn to `backend.log`.

**In SQLite.** The `.mcp.json` read-only sqlite server, or `sqlite3 backend/db/calendar.db`. Debugging only — application code still goes through repositories.

---

## Adding a trace event

1. Call `repos.trace.record_event(call_id, "your_event", node="...", **payload)`. Payload keys become JSON in `payload_json`; keep them flat and JSON-serialisable.
2. Pick a name in the existing style: `snake_case`, past tense or a noun phrase.
3. If anything downstream should read it, teach `_stage_durations_for_call` or `_cost_for_call` — and remember `_payload(event)` normalises the two row shapes (`payload_json` string from real SQLite, `payload` dict from `FakeTraceRepository`), so read through it rather than indexing directly.
4. If it should be visible in the live graph view, handle it in `admin/graph.js`.
5. Add it to the table above.

**Do not add a second logging path.** If you find yourself wanting one, the answer is almost always another `trace_events` row.
