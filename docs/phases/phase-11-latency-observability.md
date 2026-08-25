# Phase 11 — Real Per-Turn Latency + Cost Instrumentation

## Goal

Turn Q1's answer ("latency/pipeline design") from architecture prose into a number on screen. Break down every real conversational turn into its actual measured stages — caller-stops-talking → Realtime's own STT/turn-decision, the supervisor round-trip, any deferred-delivery queue wait, and time-to-first-audio on the way back — record each as trace data, and surface **p50/p95 per stage** (not a single average) in the admin panel. By the end of this phase, "the supervisor detour is deliberately kept off the hot path" is a claim you can point at a live chart to prove, not something the video just asserts.

Also compute and surface a real **estimated dollar cost per call** (Realtime audio tokens + Claude supervisor tokens) — added to this phase rather than as a separate one because it shares the exact same mechanism: both are per-turn numbers reported alongside the events this phase already instruments, aggregated by the exact same `eval/insights_agent.py` pass, and shown in the exact same admin panel section. Cost was raised as a direct concern in conversation with Jupus, so this isn't a speculative nice-to-have — it's a real, named question worth having a live number to answer with instead of a guess.

## Prerequisite

None beyond Phase 5 (async dispatcher) and Phase 6a (trace events / admin panel) already being in place — both are. Independent of Phase 8/9/10; can be built any time. Worth doing *before* Phase 10 (telephony) if both are in flight, since Phase 10's `TelephonySupervisorChannel` should emit the same trace events this phase defines from day one rather than needing a follow-up patch (noted at the end of this doc).

## Why this exists

Two real, separate problems, discovered while scoping this phase, not assumed going in:

1. **The JD (and Q1) explicitly separates "latency optimization" from "evaluation/observability" as two distinct asks.** Right now this project has strong observability for *correctness* (the error taxonomy, the LLM judge, the Benevolent Dictator flow) and zero observability for *latency* — nothing in the admin panel shows a timing number at all.
2. **The latency plumbing that already exists is silently broken.** `eval/insights_agent.py`'s `processing_latency_percentiles` computes a round-trip p50/p95 by looking for a `"user_message"` trace event — but grepping the codebase shows `record_event(..., "user_message", ...)` is never actually called anywhere in `dispatcher.py`, `graph.py`, or `app.py`. It only appears in that function's own docstring and in a test file that manually fabricates the event. On every real call this project has ever logged, `processing_latency_percentiles` has been silently returning `{"p50": 0.0, "p95": 0.0}` — and `GET /api/eval/summary` has been serving that dead zero this whole time, with nothing in `admin/app.js` even rendering it. This phase isn't adding observability from nothing; it's replacing a plumbing bug with something real, and that discovery is worth being upfront about (in `docs/fixes/`) rather than quietly papering over.
3. **Cost was raised directly, in conversation with Jupus, as a real concern about running Realtime at production volume.** A guess in the video ("it's probably cheap enough") is a weak answer to a question they already explicitly asked. A real per-call number, computed from real token usage rather than assumed, is a direct, credible answer — and it's the same kind of "show, don't narrate" move this whole batch of stretches is built around.

## Non-goals

- **Not a general APM/tracing framework.** No OpenTelemetry, no external metrics service. Same local-first, zero-new-infrastructure spirit as the rest of this project's eval stack — everything here is more `trace_events` rows plus one aggregation pass, exactly like Phase 6a's original latency stat attempted to be.
- **Not decomposing Realtime's own internal STT/dialogue-decision time any further.** OpenAI's Realtime pipeline (VAD → transcription → the model deciding to call `ask_supervisor`) is a black box from this project's side — this phase measures the *outside* of that box (caller-stopped-talking to `ask_supervisor`-received) as one stage, not its internals. Q1's written answer should say this plainly: this is the boundary of what's observable without owning Realtime's internals directly.
- **Not adding retries, timeouts, or any behavior change based on a slow measurement.** This phase is instrumentation only — it changes what's recorded and shown, never what the system *does*. (A follow-on phase using these numbers to actually change behavior — e.g. an alert, a fallback path on sustained high p95 — is future work, not this phase.)
- **Not touching the LangGraph nodes, tool catalog, or any business logic.** Every change in this phase is either a new/extended trace event, a small addition to `dispatcher.py`'s existing message handling, a client-side timestamp report, or an aggregation function in `eval/`. Zero changes to `graph.py`'s nodes/edges or `tools.py`.
- **Not retroactively computing stage breakdowns for calls logged before this phase shipped.** Old calls keep whatever (broken) latency data they have; new calls get the real breakdown. No backfill/migration script.
- **Not a billing-accurate invoice.** The dollar figure is an *estimate* from token counts × a hardcoded pricing table, not a reconciliation against either provider's actual invoice — model/audio pricing changes over time and this project's pricing constants can go stale. This is stated plainly wherever the number is shown (Decision 10), not asserted as exact.
- **Not tracking cost for anything except the two paid APIs already in the stack.** No infrastructure/hosting cost modeling (that's the Phase 10 Railway-hosting conversation's territory, not this one's).

## Decisions made, not left open for the implementer

**1. Four stages, not one number — chosen to match where a caller actually perceives delay, not where the code happens to have a convenient boundary.** `stt_and_dialogue_decision` (caller stops talking → `ask_supervisor` received by the backend), `supervisor_processing` (the graph/Claude round-trip itself), `deferred_wait` (Phase 5's "caller was still talking" queue delay — kept **separate** from `supervisor_processing` deliberately: conflating "how long did Claude take" with "how long did we wait for a natural gap to speak" would misattribute a scheduling artifact as model latency, and they have completely different fixes if either turns out to be the bottleneck), and `tts_first_audio` (reply handed to Realtime → caller hears the first bit of the spoken response). A `total_perceived` figure (sum of whichever stages actually applied to that turn) is also computed, since that's the number a caller experiences.

**2. `speech_stopped` and `ask_supervisor`-received both become real trace events — closing the exact gap that made the old metric dead.** `dispatcher.on_bridge_message` already receives both message types; it just never called `repos.trace.record_event` for them. Fixed directly here rather than reintroducing a differently-named version of the same bug.

**3. Time-to-first-audio requires one small, deliberately minimal client-side addition — a duration report, not a redesign.** The backend has zero visibility into Realtime's TTS playback; only `client/app.js` (and, from Phase 10 onward, the telephony side's own audio path) knows when audio actually started playing. Rather than instrument audio playback deeply, the client does the one measurement only it can do — timestamp when it sends `response.create` after delivering a supervisor result, timestamp the first corresponding `response.audio.delta`, compute the delta — and reports just that single number back over `/bridge` as a new small message type. No raw audio, no waveform data, nothing beyond one integer.

**4. Aggregation lives in `eval/insights_agent.py`, replacing (not duplicating) `processing_latency_percentiles`.** The old function's shape (pool across `call_ids`, nearest-rank percentile, degrade to zeros on no data) is sound and reused — this phase changes what boundaries it looks for and adds the missing stages, it doesn't redesign the statistical approach. `_percentile` is reused unchanged.

**5. Per-call stage durations get their own small endpoint, not just the aggregate.** The aggregate p50/p95 in `/api/eval/summary` answers "how is the system doing overall" (the Q3-flavored view); a new `GET /api/calls/{call_id}/latency` answers "what happened on *this* call" (useful for pointing at one specific real call in the video for Q1, the same way the admin panel's trace viewer already lets you point at one specific call for Q3). Both share one underlying per-call-events parser (`_stage_durations_for_call`) so the two never drift apart from independently-written boundary-matching logic.

**6. This phase is instrumentation-only and must add zero latency to the hot path.** Every timestamp captured is either already implicitly available (an event is already being written; this just adds a `ts`-bearing sibling) or a single `time.monotonic()` call at a point that's already executing. No new synchronous work is inserted into `process_supervisor_call`'s critical path, and the client-side TTS-timing report is fire-and-forget (sent, not awaited by anything that blocks the caller's audio).

**7. Cost has two independent sources, captured where the usage data actually originates — never estimated from duration.** Claude token usage is captured server-side, right where the Anthropic API response already arrives (`call_claude_json`/`call_claude_text` in `llm_utils.py`); Realtime token usage is only ever visible to whichever side holds the OpenAI session (`client/app.js` for WebRTC, and — once Phase 10 exists — the telephony control channel), since `response.done` events carrying `usage` are a property of that session, not something the backend can otherwise see. Neither is inferred from `duration_ms` or turn count — a slow turn isn't necessarily an expensive one, and estimating cost from timing would be exactly the kind of made-up number this phase exists to avoid producing.

**8. Claude usage capture uses a thread-local, not a threaded-through parameter, to avoid touching every LLM-backed function in `tools.py`.** `call_claude_json`/`call_claude_text` are called from deep inside individual `tools.py` functions (`extract_field`, `classify_practice_area`, etc.) that don't currently receive `call_id`/`trace_repo` — threading those through every such function's signature just to attach usage is a much bigger diff than this phase needs. Instead, `call_claude_json`/`call_claude_text` stash the just-received `response.usage` in a `threading.local()` right after the API call returns; `call_claude_tool` (the one choke point every Claude-backed tool call already passes through, rule #7) reads and clears it immediately after `fn(...)` returns, in the same thread, and records it as its own `llm_usage` trace event. This is safe specifically because `call_claude_tool`'s call to `fn` and any `call_claude_json`/`call_claude_text` call nested inside it always run synchronously in the same OS thread (each `asyncio.to_thread` invocation owns one worker thread for its own duration) — there is no cross-thread read of the stashed value, and it's cleared immediately after being read so a thread being reused later by the pool never sees a stale value.

**9. Realtime usage capture is a client-reported delta per response, same shape as Decision 3's `tts_first_audio` report — not a new mechanism.** On every `response.done` event, `client/app.js` already receives a `usage` object (audio/text input/output token counts for that one response). It relays this over `/bridge` as a new small message type, `{"type": "realtime_usage", "tool_call_id": ..., "input_audio_tokens": ..., "output_audio_tokens": ..., "input_text_tokens": ..., "output_text_tokens": ...}` — recorded as a `realtime_usage` trace event, same pattern as `tts_first_audio`, fire-and-forget.

**10. Pricing constants live in one place, are clearly marked as needing verification against current published rates, and every displayed dollar figure is labeled "estimated."** `eval/pricing.py` holds `$/1M tokens` rates for both providers as named constants with a comment instructing whoever's reading the code to confirm them against OpenAI's/Anthropic's current pricing pages before treating the dollar output as accurate at the time it's read — the same "don't trust a training-data snapshot of a fast-moving API surface, verify live" caution already applied elsewhere in this project (e.g. Realtime event names in `docs/DECISIONS.md`). Every place a dollar amount is rendered (admin panel, `docs/answers.md`) is labeled "estimated cost" in the UI/text itself, not just in a footnote.

---

## New trace event types

| Event | Recorded where | Fields |
|---|---|---|
| `speech_stopped` | `dispatcher.on_bridge_message`, `speech_stopped` branch (already exists — just add the `record_event` call it's currently missing) | — |
| `ask_supervisor_received` | `dispatcher.on_bridge_message`, `ask_supervisor` branch, **before** `asyncio.create_task(...)` | `tool_call_id` |
| `tts_first_audio` | `dispatcher.on_bridge_message`, new `tts_first_audio` branch (client-reported, see below) | `tool_call_id`, `ms_since_reply_delivered` |
| `llm_usage` | `call_claude_tool` (`llm_utils.py`), right after `traced_call` returns (Decision 8) | `node`, `tool_name`, `model`, `input_tokens`, `output_tokens` |
| `realtime_usage` | `dispatcher.on_bridge_message`, new `realtime_usage` branch, client-reported per `response.done` (Decision 9) | `tool_call_id`, `input_audio_tokens`, `output_audio_tokens`, `input_text_tokens`, `output_text_tokens` |

`reply_delivered`/`reply_deferred` (Phase 5) and `tool_call_start`/`tool_call_end` (Phase 2/`traced_call`) already exist and are reused as-is — no changes to those.

---

## Changes to existing files

### `backend/dispatcher.py`

```python
async def on_bridge_message(repos: Repositories, call_id: str, msg: dict) -> None:
    msg_type = msg.get("type")
    if msg_type == "ask_supervisor":
        repos.trace.record_event(call_id, "ask_supervisor_received", tool_call_id=msg["tool_call_id"])
        asyncio.create_task(
            process_supervisor_call(repos, call_id, msg["tool_call_id"], msg["last_caller_utterance"])
        )
    elif msg_type == "speech_started":
        SPEAKING[call_id] = True
    elif msg_type == "speech_stopped":
        SPEAKING[call_id] = False
        repos.trace.record_event(call_id, "speech_stopped")   # <- new
        drain_deferred(repos, call_id)
    elif msg_type == "tts_first_audio":   # <- new
        repos.trace.record_event(
            call_id, "tts_first_audio",
            tool_call_id=msg["tool_call_id"], ms_since_reply_delivered=msg["ms_since_reply_delivered"],
        )
    elif msg_type == "realtime_usage":   # <- new
        repos.trace.record_event(
            call_id, "realtime_usage", tool_call_id=msg.get("tool_call_id"),
            input_audio_tokens=msg["input_audio_tokens"], output_audio_tokens=msg["output_audio_tokens"],
            input_text_tokens=msg["input_text_tokens"], output_text_tokens=msg["output_text_tokens"],
        )
    else:
        logger.warning("unknown /bridge message type=%r call_id=%s", msg_type, call_id)
```

Everything else in `dispatcher.py` — `process_supervisor_call`, `deliver_or_defer`, `drain_deferred` — is unchanged. Their existing trace events (`reply_delivered`/`reply_deferred`, `tool_call_start`/`tool_call_end`) already carry everything the `supervisor_processing`/`deferred_wait` stages need; this phase's aggregation reads them, it doesn't change how they're written.

### `client/app.js`
- On `speech_stopped` relay (already sent to `/bridge`, Phase 5) — no change, just confirms this is the boundary `speech_stopped`'s trace event now marks.
- New: when sending `response.create` after a `supervisor_result` arrives, record `const replySentAt = performance.now()`. On the **first** `response.audio.delta` event for that response, compute `Math.round(performance.now() - replySentAt)` and send `{"type": "tts_first_audio", "tool_call_id": ..., "ms_since_reply_delivered": <value>}` over `/bridge` — fire-and-forget, no response expected, never blocks audio playback itself.
- New: on every `response.done` event, read its `response.usage` object and relay `{"type": "realtime_usage", "tool_call_id": <the triggering ask_supervisor's id, if this response followed one, else omitted>, "input_audio_tokens": ..., "output_audio_tokens": ..., "input_text_tokens": ..., "output_text_tokens": ...}` to `/bridge` — this fires for **every** response, not just ones following a supervisor round-trip (the opening greeting and any small-talk turn also consume Realtime tokens and cost real money; excluding them would understate the number). Confirm the exact `response.usage` field names/shape against current Realtime API docs at implementation time (same caution as everywhere else in this project that touches a fast-moving part of the Realtime event surface).

### `backend/supervisor/llm_utils.py` — Claude usage capture (Decision 8)

```python
import threading

_last_usage = threading.local()

def call_claude_json(system: str, user_content: str, json_schema: dict, max_tokens: int = 512) -> dict:
    response = _client.messages.create(...)   # unchanged call
    _last_usage.value = {
        "model": MODEL_ID,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)

# call_claude_text gets the identical two-line addition.

def call_claude_tool(
    trace_repo: TraceRepository, call_id: str, node: str, tool_name: str, fn: Callable, *args, **kwargs
):
    try:
        result = traced_call(trace_repo, call_id, node, tool_name, fn, *args, **kwargs)
        usage = getattr(_last_usage, "value", None)
        if usage is not None:
            trace_repo.record_event(call_id, "llm_usage", node=node, tool_name=tool_name, **usage)
            _last_usage.value = None   # cleared immediately after reading — Decision 8
        return result
    except RETRYABLE_ERRORS as e:
        ...   # unchanged retry logic
```

A `fn` that doesn't itself call `call_claude_json`/`call_claude_text` (i.e. a deterministic tool routed through `call_claude_tool` by mistake — shouldn't happen given rule #7, but defensively) simply leaves `_last_usage.value` as `None`/stale-cleared, so no `llm_usage` event is recorded for it — correct behavior, not a bug to guard against further.

### `eval/pricing.py` (new file)

```python
"""Hardcoded $/1,000,000-token pricing for the two paid APIs this project
uses. VERIFY AGAINST CURRENT PUBLISHED PRICING before treating any dollar
figure this project displays as accurate — these rates change over time
and this file is not automatically kept in sync with either provider.
Every place a cost is shown must be labeled "estimated" (Decision 10)."""

CLAUDE_SONNET_INPUT_PER_MILLION = <verify at implementation time>
CLAUDE_SONNET_OUTPUT_PER_MILLION = <verify at implementation time>
REALTIME_AUDIO_INPUT_PER_MILLION = <verify at implementation time>
REALTIME_AUDIO_OUTPUT_PER_MILLION = <verify at implementation time>
REALTIME_TEXT_INPUT_PER_MILLION = <verify at implementation time>
REALTIME_TEXT_OUTPUT_PER_MILLION = <verify at implementation time>


def estimate_cost_usd(
    claude_input_tokens: int, claude_output_tokens: int,
    realtime_audio_in: int, realtime_audio_out: int,
    realtime_text_in: int, realtime_text_out: int,
) -> float:
    # Pure arithmetic, no I/O — trivially unit-testable in isolation from
    # any trace-parsing logic.
    ...
```

### `eval/insights_agent.py` — `processing_latency_percentiles` replaced by `latency_breakdown_percentiles`

```python
LATENCY_STAGES = ("stt_and_dialogue_decision", "supervisor_processing", "deferred_wait", "tts_first_audio", "total_perceived")


def _stage_durations_for_call(events: list[dict]) -> dict[str, Optional[float]]:
    """Walks one call's trace_events in order, matching each ask_supervisor
    turn's boundary events into stage durations (ms). A turn missing an
    expected boundary (e.g. a telephony call before Phase 10 wired up the
    same events, or a turn where the caller never paused so speech_stopped
    never fired before ask_supervisor) simply contributes no data for that
    stage on that turn — never raises, never fabricates a number. Returns
    per-stage lists of durations found across every turn in this one call's
    trace (a call can have many ask_supervisor turns)."""
    ...


def latency_breakdown_percentiles(trace_repo: TraceRepository, call_ids: list[str]) -> dict[str, dict[str, float]]:
    """Pools _stage_durations_for_call's output across every given call_id,
    then computes {stage: {"p50": ..., "p95": ...}} for each of
    LATENCY_STAGES, degrading any stage with zero data points to
    {"p50": 0.0, "p95": 0.0} — same never-raises contract the function it
    replaces had. Reuses _percentile unchanged."""
    ...
```

`run_deterministic_pass`'s `"latency"` key now holds this breakdown dict (was the single `{"p50", "p95"}` pair from the now-removed `processing_latency_percentiles`).

```python
def _cost_for_call(events: list[dict]) -> dict:
    """Sums every llm_usage and realtime_usage event in one call's trace
    into token totals, then converts via pricing.estimate_cost_usd. A call
    with zero usage events (e.g. abandoned before any real turn) returns
    all-zero totals and $0.00 — never raises."""
    ...


def average_cost_per_call(calls: list[dict], trace_repo: TraceRepository) -> dict:
    """{"average_usd": ..., "p50_usd": ..., "p95_usd": ...} pooled across
    every given call via _cost_for_call + _percentile — same
    never-raises-on-empty-input contract as every other stat in this
    module."""
    ...
```

`run_deterministic_pass` gains one more key: `"cost"` holding `average_cost_per_call`'s result.

### `backend/app.py` — one new route, extended for cost
```python
@app.get("/api/calls/{call_id}/latency")
async def api_call_latency(call_id: str, repos: Repositories = Depends(get_repos)):
    from eval.insights_agent import _stage_durations_for_call, _cost_for_call
    events = repos.trace.get_trace(call_id)
    if not events:
        raise HTTPException(status_code=404, detail="call not found")
    return {"stages": _stage_durations_for_call(events), "cost": _cost_for_call(events)}
```
(Kept as one route rather than a separate `/cost` endpoint — both are derived from the same already-fetched trace, and a video pointing at one call's detail view benefits from timing and cost sitting together, not two separate fetches/panels for one call.)

### `admin/index.html` / `admin/app.js`
- A new small "Latency & Cost" panel on the eval-summary view (same place the existing `booking_success_rate`/`escalation_reason_histogram` numbers already render) — one row per `LATENCY_STAGES` entry (p50/p95), plus a separate small row for average/p50/p95 **estimated cost per call**, explicitly labeled "estimated" in the UI text itself (Decision 10). A plain table, consistent with the admin panel's current functional-first styling.
- The existing per-call detail view gains one small addition: fetch `GET /api/calls/{call_id}/latency` alongside the transcript/trace it already fetches, and render both the stage/duration rows and that one call's estimated cost — this is the thing worth pointing the camera at for Q1 (and the cost question raised in conversation) in the video: a real call's real breakdown and real dollar estimate, not an aggregate or a guess.

---

## Worked example

1. Caller finishes a sentence. Realtime's VAD/turn-detection decides the turn is over; `client/app.js` relays `speech_stopped` to `/bridge` → `speech_stopped` trace event recorded.
2. Some further processing inside Realtime itself (not observable) lands on "this needs `ask_supervisor`"; the tool call is emitted, relayed to `/bridge` → `ask_supervisor_received` trace event recorded (`stt_and_dialogue_decision` stage = the gap between these first two events).
3. `dispatcher.process_supervisor_call` runs the graph; `tool_call_start`/`tool_call_end` events bracket each individual Claude call inside it. The turn resolves; either `reply_delivered` fires immediately (`SPEAKING` was false — `supervisor_processing` stage = gap between `ask_supervisor_received` and `reply_delivered`, `deferred_wait` = 0 for this turn) or `reply_deferred` then a later `reply_delivered` fires once the caller stops talking again (`supervisor_processing` = gap to `reply_deferred`, `deferred_wait` = `reply_delivered`'s own existing `wait_ms` field, already computed by Phase 5's code, just now folded into this phase's aggregate rather than sitting unused).
4. `client/app.js` sends the result into Realtime, times the response, and reports `tts_first_audio` once the caller actually starts hearing the reply.
5. `GET /api/calls/{call_id}/latency` for this call now shows four real timing numbers *and* an estimated dollar cost instead of a claim; `GET /api/eval/summary`'s `latency` and `cost` fields show the same breakdown and an average/p50/p95 cost pooled across every logged call. Separately, every `call_claude_json`/`call_claude_text` invocation triggered along the way recorded its own `llm_usage` event, and every Realtime `response.done` (including the opening greeting, which never touches the supervisor at all) recorded its own `realtime_usage` event — both folded into this same call's cost total.

---

## Tests

### `eval/tests/test_insights_agent.py` (extends the existing file — replaces the now-deleted tests for `processing_latency_percentiles`)
1. `test_stage_durations_computed_for_complete_turn` — a fabricated trace with all five boundary events for one turn in the expected order; assert each of the four per-turn stages comes back with the correct millisecond gap.
2. `test_immediate_delivery_has_zero_deferred_wait` — a turn with `reply_delivered` directly (no `reply_deferred`); assert `deferred_wait` contributes `0` for that turn, not `None`/missing.
3. `test_deferred_delivery_uses_existing_wait_ms_field` — a turn with `reply_deferred` then `reply_delivered(was_deferred=True, wait_ms=N)`; assert `deferred_wait` picks up exactly `N`, not a value independently recomputed from timestamps (single source of truth — Phase 5's own `wait_ms` field).
4. `test_missing_boundary_event_yields_no_data_for_that_stage_not_a_crash` — a trace missing `speech_stopped` entirely for a turn (e.g. the caller was already mid-utterance when the call started); assert `stt_and_dialogue_decision` simply has no data point from that turn, and nothing raises.
5. `test_multiple_turns_in_one_call_all_contribute` — a trace with two full `ask_supervisor` turns; assert both contribute independent data points, not just the first/last.
6. `test_latency_breakdown_percentiles_empty_calls_returns_zeroed_stages` — no `call_ids`; every stage returns `{"p50": 0.0, "p95": 0.0}`, matching the never-raises contract the old function had.
7. `test_latency_breakdown_percentiles_pools_across_multiple_calls` — two calls each contributing one turn; assert the percentile computation sees both calls' data points pooled, not computed per-call and averaged.

### `backend/tests/test_dispatcher_latency_events.py` (new file)
1. `test_ask_supervisor_records_received_event_before_spawning_task` — assert `ask_supervisor_received` is recorded synchronously in `on_bridge_message`, not from inside the spawned task (matters for the timestamp to reflect actual receipt time, not whenever the task happens to start running).
2. `test_speech_stopped_now_records_trace_event` — regression test for the exact bug this phase fixes; assert `speech_stopped` produces a `trace_events` row (it didn't before this phase).
3. `test_tts_first_audio_message_recorded_with_reported_fields` — a mocked `{"type": "tts_first_audio", ...}` message results in a matching trace event with both fields intact.

### `backend/tests/test_admin_routes.py` (extends the existing file)
1. `test_call_latency_endpoint_returns_stage_breakdown` — seed a call's trace with a complete turn; assert `GET /api/calls/{call_id}/latency` returns the expected stage keys/values.
2. `test_call_latency_endpoint_404s_for_unknown_call`.
3. `test_call_latency_endpoint_includes_cost` — seed a call's trace with `llm_usage`/`realtime_usage` events; assert the response's `cost` key reflects them.

### `backend/tests/test_llm_utils_usage.py` (new file)
1. `test_call_claude_tool_records_llm_usage_event` — mock `_client.messages.create` to return a response with a known `usage.input_tokens`/`usage.output_tokens`; assert `call_claude_tool` records an `llm_usage` trace event with matching values.
2. `test_last_usage_cleared_after_read` — call `call_claude_tool` twice in sequence within the same thread, the second wrapping a plain deterministic `fn` that makes no Claude call; assert the second call does **not** record a stale `llm_usage` event carried over from the first (regression test for Decision 8's "cleared immediately after read" requirement).
3. `test_deterministic_fn_records_no_llm_usage_event` — `call_claude_tool` wrapping a `fn` that never calls `call_claude_json`/`call_claude_text`; assert no `llm_usage` event is recorded.

### `eval/tests/test_pricing.py` (new file)
1. `test_estimate_cost_usd_zero_tokens_is_zero_dollars`.
2. `test_estimate_cost_usd_matches_hand_computed_value` — a fixed token count run through `estimate_cost_usd`, asserted against a value computed by hand from the same constants (catches an arithmetic bug in the function, independent of whether the constants themselves are currently accurate — Decision 10's "verify against current pricing" caveat is about the constants' *values*, not this test's job).

### `eval/tests/test_insights_agent.py` — cost additions
1. `test_cost_for_call_sums_multiple_llm_usage_events` — a trace with several `llm_usage` events (e.g. one per graph node in a multi-turn call); assert the total reflects all of them, not just the first/last.
2. `test_cost_for_call_includes_realtime_usage_even_without_supervisor_call` — a trace with only `realtime_usage` events (e.g. the opening greeting, no `ask_supervisor` ever fired); assert cost is still computed, not silently zero.
3. `test_average_cost_per_call_empty_input_returns_zero` — matches the never-raises contract of every other stat function in this module.

---

## Definition of Done

- [x] `pytest` — full suite, including every new/modified test above, zero regressions.
- [x] `docs/fixes/` gets an entry documenting the dead `user_message`/`processing_latency_percentiles` bug found while scoping this phase — what was broken, since when (Phase 6a), and that it silently returned zeros rather than erroring, which is exactly why it went unnoticed.
- [x] Manual, live: complete one ordinary (non-escalating) call, then check `GET /api/calls/{call_id}/latency` — confirm all four stages show real, non-zero, plausible numbers (not `0.0`/`null` across the board, which would mean an event wiring gap slipped through).
- [x] Manual, live: force one deferred-delivery turn (same technique as Phase 5's own DoD check — speak a follow-up immediately after triggering a supervisor call) and confirm `deferred_wait` for that specific turn is non-zero and roughly matches what was observed, while `supervisor_processing` for the same turn stays close to what an immediate-delivery turn shows (proves the two stages are genuinely decomposed, not one bleeding into the other).
- [x] `admin`'s eval-summary Latency & Cost panel and the per-call latency+cost rows both visually confirmed against at least one real call.
- [x] `eval/pricing.py`'s constants confirmed against current published OpenAI Realtime and Anthropic pricing pages immediately before recording any real numbers for the video/README — not left at placeholder/stale values (Decision 10).
- [x] Manual, live: after a small batch of real calls (the same batch used for the latency DoD checks above is fine), confirm `GET /api/eval/summary`'s `cost` field shows a plausible, non-zero average cost per call, and that it's the same order of magnitude for a `llm_usage`-only vs. a `realtime_usage`-only sanity check (i.e. neither side of the two-source sum is silently missing).
- [x] `docs/answers.md`'s Q1 answer updated to reference this doc's actual stage breakdown and cite a real observed p50/p95 pair (from a real, if small, batch of logged calls) rather than describing the pipeline in the abstract; also add the estimated cost-per-call figure here or in the README, directly addressing the cost concern raised in conversation, labeled "estimated" and dated against whatever pricing was current when measured.
- [x] `docs/DECISIONS.md` gets a short entry: the four-stage latency split (Decision 1) and why `deferred_wait` is kept separate from `supervisor_processing`; a second entry for the cost mechanism (Decision 7/8/9) and the explicit "estimated, verify pricing constants" caveat (Decision 10).

---

## Forward note for Phase 10 (telephony), if built after this phase

`TelephonySupervisorChannel` (Phase 10) should emit `ask_supervisor_received`/`speech_stopped` at the same points `dispatcher.on_bridge_message` does today, so telephony-channel calls contribute to the same aggregate stats rather than silently having no `stt_and_dialogue_decision`/`deferred_wait` data. `tts_first_audio` has no client to report it from on the telephony channel at all (Decision 1 of Phase 10 — audio never touches this backend), so that stage will always show "no data" for telephony calls; this is an accepted, documented gap, not a bug — telephony's real time-to-first-audio would need to be measured differently (e.g. from Twilio's own call-quality insights) if it's ever needed, out of scope for either phase.
