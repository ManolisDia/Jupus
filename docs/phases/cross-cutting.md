# Cross-Cutting Concerns

Rules and additions that apply across multiple phases rather than belonging to exactly one. Read `docs/architecture.md` first — `TraceRepository`/`EvalRepository` etc. are where the `trace_events`/error-taxonomy persistence described below actually lives; the `conn`/`get_db_conn()` references in this doc mean "the relevant repository," not a raw connection. Read section 0 before **Phase 2** (traces start at the dispatcher) and the rest before **Phase 3** (where the first Claude-backed tool calls appear) — both change the shape of code written from that point on, and are cheaper to build in from the start than retrofit later.

---

## 0. Traces

A **trace** is the complete, ordered record of everything that happened in one call — every user message, assistant reply, tool call (with its arguments, result, duration, and success/failure), retry, stage transition, and delivery decision (including deferred/dropped-stale replies from the async dispatcher). `state["transcript"]` (Phase 2 onward) stays as the lightweight caller/agent utterance list used for prompting within a live call — it's not replaced. The trace is the separate, richer, durable audit log everything else is built on: debugging, the admin panel's detail view, and — most importantly for what you're actually asking the eval agent to do — the judge's input, since "the tool `check_availability` was called twice with identical arguments" is a far stronger repetition signal than inferring it from surface text alone.

### `trace_events` table (add to the base `schema.sql`, introduced in Phase 2)
```sql
CREATE TABLE trace_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT REFERENCES calls(call_id),
    seq INTEGER NOT NULL,      -- monotonic per call_id, not wall-clock-derived —
                                -- guarantees a stable total order even when two
                                -- events share a timestamp
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    node TEXT,                 -- which graph node was active, if applicable
    payload_json TEXT NOT NULL
);
CREATE INDEX idx_trace_events_call_seq ON trace_events(call_id, seq);
```

### Event types (seed set — extend as needed, but keep `event_type` values stable once used, same rule as the error taxonomy's `id`s)
`user_message`, `node_entered` (`{stage}`), `node_exited` (`{stage_from, stage_to, pending_reply}`), `tool_call_start` (`{tool_name, args}`), `tool_call_end` (`{tool_name, result_summary, duration_ms, success}`), `llm_retry` (`{tool_name, attempt, error}`), `llm_call_failed` (`{tool_name, error}`), `reply_delivered` (`{tool_call_id, reply, was_deferred, wait_ms}`), `reply_deferred` (`{tool_call_id, reason}`), `reply_dropped_stale` (`{tool_call_id, dispatch_stage, current_stage}`), `call_ended` (`{outcome}`), `call_abandoned` (`{}`).

### `backend/db/repositories/sqlite_trace.py` — `SQLiteTraceRepository(TraceRepository)`
Implements the two methods declared on `TraceRepository` in `docs/architecture.md`:
```python
class SQLiteTraceRepository(TraceRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._seq_counters: dict[str, int] = {}   # call_id -> next seq, in-memory

    def record_event(self, call_id: str, event_type: str, node: Optional[str] = None, **payload) -> None:
        seq = self._seq_counters.get(call_id, 0)
        self._seq_counters[call_id] = seq + 1
        self._conn.execute(
            "INSERT INTO trace_events (call_id, seq, ts, event_type, node, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (call_id, seq, now_iso(), event_type, node, json.dumps(payload)),
        )
        self._conn.commit()   # written immediately, not batched — a crash
                                # mid-call still leaves a usable partial trace

    def get_trace(self, call_id: str) -> list[dict]:
        # SELECT * FROM trace_events WHERE call_id=? ORDER BY seq
        ...
```
This is the **only** file that knows the `trace_events` table exists — no raw SQL or `sqlite3` import anywhere else, per `docs/architecture.md`.

### `backend/supervisor/tracing.py` — `traced_call` (standalone helper, not a repository method)
```python
def traced_call(trace_repo: TraceRepository, call_id: str, node: str, tool_name: str, fn: Callable, *args, **kwargs):
    # Wraps ANY tool_catalog function (deterministic or LLM-backed) — records
    # tool_call_start before, tool_call_end (with duration_ms and success)
    # after, re-raising on exception after recording success=False, via
    # trace_repo.record_event(...) (never touches sqlite3 directly — that's
    # SQLiteTraceRepository's job, injected here as trace_repo). Every
    # function in docs/PLAN.md's tool catalog goes through this — including
    # the deterministic ones (validate_email, check_availability, ...), not
    # just the LLM-backed ones, since "complete record of all tool calls" is
    # the actual requirement, not "just the interesting ones."
    start = time.monotonic()
    trace_repo.record_event(call_id, "tool_call_start", node=node, tool_name=tool_name, args=summarize(args, kwargs))
    try:
        result = fn(*args, **kwargs)
        trace_repo.record_event(call_id, "tool_call_end", node=node, tool_name=tool_name,
                                 result_summary=summarize(result), duration_ms=int((time.monotonic()-start)*1000), success=True)
        return result
    except Exception as e:
        trace_repo.record_event(call_id, "tool_call_end", node=node, tool_name=tool_name,
                                 duration_ms=int((time.monotonic()-start)*1000), success=False, error=str(e))
        raise
```
**Standing rule (also `CLAUDE.md` #8)**: every call to a `tools.py` function goes through `traced_call`, never invoked directly — this makes "every tool call is traced" true by construction rather than something each new tool function has to remember to do.

### Tests — `backend/tests/test_tracing.py`
Construct a `SQLiteTraceRepository` against a temp DB (or a fake in-memory `TraceRepository` per `docs/architecture.md`'s testing note) for all of these:
1. `test_record_event_assigns_monotonic_seq_per_call` — record 3 events for one `call_id`, assert `seq` values are `0, 1, 2` in insertion order.
2. `test_record_event_seq_independent_across_calls` — two different `call_id`s each start their own `seq` at `0`.
3. `test_get_trace_returns_events_in_seq_order` — insert out of a contrived order via direct SQL against the temp DB, assert `get_trace` still returns them ordered by `seq`.
4. `test_traced_call_records_start_and_end_on_success` — wrap a simple function, assert both events are recorded (via the injected `trace_repo`) with matching `tool_name` and `success=True`.
5. `test_traced_call_records_failure_and_reraises` — wrap a function that raises, assert a `tool_call_end` with `success=False` is recorded AND the exception still propagates (tracing must never swallow the original error).

---

## 1. Upstream API failure handling

No phase doc above specifies what happens if a Claude (or, less likely given the architecture, OpenAI) API call fails mid-node — times out, rate-limits, or errors. Left unhandled, this would raise out of a node function, out of `GRAPH.invoke`, out of `process_supervisor_call`, and — since that coroutine runs inside a bare `asyncio.create_task` per `dispatcher.py` — **silently die with no reply ever sent to the caller**, who'd just hear dead air. This must not be allowed to happen unhandled.

### `backend/supervisor/llm_utils.py` (new file)
```python
class LLMCallFailed(Exception):
    pass

def call_claude_tool(trace_repo: TraceRepository, call_id: str, node: str, tool_name: str, fn: Callable, *args, **kwargs):
    # Builds on traced_call (section 0) rather than duplicating its
    # tool_call_start/end recording — call_claude_tool adds retry handling
    # AND its own llm_retry/llm_call_failed trace events on top:
    # On anthropic.APIError / RateLimitError / APITimeoutError: record an
    # "llm_retry" event, retry once after a short fixed backoff (e.g. 0.5s).
    # If the retry also fails, record "llm_call_failed" and raise
    # LLMCallFailed(original_exception) — never let the raw SDK exception
    # escape this wrapper. On eventual success (first try or after retry),
    # delegate the actual invocation through traced_call so the standard
    # tool_call_start/end events are still recorded exactly as they are for
    # deterministic tools — one consistent trace shape for every tool,
    # LLM-backed or not.
    ...
```
Every `tools.py` function that calls Claude must go through this wrapper, not call the Anthropic SDK directly.

### Node-level handling
Every node function (`node_greeting`, `node_routing`, `node_capture`, `node_booking`, `node_escalation`) must catch `LLMCallFailed` around its tool call(s) and, instead of propagating it, return a graceful fallback:
```python
except LLMCallFailed:
    failures = state.get("consecutive_llm_failures", 0) + 1
    if failures >= 3:
        return {"stage": "escalation", "escalation_reason": "system_error",
                "consecutive_llm_failures": failures, "pending_reply": "...", "transcript": [...]}
    return {"consecutive_llm_failures": failures,
            "pending_reply": "Sorry, I'm having a little trouble — could you say that again?",
            "transcript": [...]}
    # stage is NOT changed on the first 1-2 failures — the same node
    # naturally retries fresh on the caller's next utterance.
```
`consecutive_llm_failures` is a **new `CallState` field** (`int`, default `0`), reset to `0` on any node call that completes without hitting this path.

### `escalation_reason` enum addition
Add `"system_error"` as a 6th valid value, alongside the 5 defined in Phase 5. This supersedes the enum shown in `docs/PLAN.md`'s tool catalog and Phase 5's table — both should be read as if `"system_error"` is listed alongside the other 5.

### Tests — `backend/tests/test_llm_utils.py`
1. `test_call_succeeds_first_try` — mock the wrapped function to succeed immediately; assert the result passes through unchanged and no retry happens.
2. `test_call_retries_once_then_succeeds` — mock to raise a transient error once, then succeed; assert the wrapper returns the eventual success and the underlying function was called exactly twice.
3. `test_call_raises_llm_call_failed_after_retry_exhausted` — mock to always raise; assert `LLMCallFailed` is raised after exactly 2 attempts (1 original + 1 retry), not more.

### Tests — extend each phase's node test file
Add one test per node confirming `LLMCallFailed` is caught gracefully rather than propagating, e.g. in `backend/tests/test_capture_node.py`: `test_llm_failure_returns_fallback_reply_without_crashing` (mock `extract_field` to raise `LLMCallFailed`, assert the node returns a `pending_reply` and does not raise). Add `backend/tests/test_system_error_escalation.py`: `test_three_consecutive_failures_escalates_with_system_error` (simulate 3 turns each hitting `LLMCallFailed`, assert escalation on the 3rd with the new reason).

---

## 2. WebSocket disconnect cleanup

If a browser tab closes or the network drops mid-call, `backend/app.py`'s `WS /bridge` handler will receive a `WebSocketDisconnect`. Nothing in Phases 1–6 as written cleans up `CONNECTIONS[call_id]`, `SPEAKING[call_id]`, or `DEFERRED[call_id]` — over a long dev session with many test calls, these dicts grow unbounded, and abandoned calls sit in the DB forever with `outcome IS NULL`.

### `backend/dispatcher.py` addition
```python
def mark_call_abandoned(repos: Repositories, call_id: str) -> None:
    state = CALL_STATES.get(call_id)
    if state and state["stage"] != "ended":
        state["stage"] = "ended"
        repos.calls.upsert(state, outcome_override="abandoned")
    repos.trace.record_event(call_id, "call_abandoned")
    CONNECTIONS.pop(call_id, None)
    SPEAKING.pop(call_id, None)
    DEFERRED.pop(call_id, None)
    # LOCKS intentionally left in place — a stray unused asyncio.Lock is
    # harmless and simpler than reasoning about whether it's safe to
    # remove one that might still be referenced by an in-flight task.
```
`backend/app.py`'s `/bridge` handler calls `mark_call_abandoned(repos, call_id)` in the `except WebSocketDisconnect:` block, where `repos` is the app-level `Repositories` bundle (`docs/architecture.md`).

### `CallRepository.upsert` addition
`upsert(self, state: CallState, outcome_override: Optional[str] = None) -> None` (already declared with this signature in `docs/architecture.md`) — when `outcome_override` is provided, `SQLiteCallRepository` uses it directly instead of the derive-from-state logic. `mark_call_abandoned` is the only caller that should ever pass `"abandoned"`.

### Tests — `backend/tests/test_dispatcher_async.py` additions
1. `test_disconnect_marks_call_abandoned` — call `mark_call_abandoned` on a call mid-`capture`; assert the `calls` row now has `outcome == "abandoned"` and `ended_at` set.
2. `test_disconnect_does_not_override_already_ended_call` — call it on a call already `stage == "ended"` with `outcome == "booked"`; assert the outcome is untouched (a disconnect right after a successful booking shouldn't relabel it abandoned).
3. `test_disconnect_clears_registries` — populate `CONNECTIONS`/`SPEAKING`/`DEFERRED` for a `call_id`, call `mark_call_abandoned`, assert all three no longer contain that key.

---

## 3. Automated end-to-end scenario regression suite

See `docs/scenarios.md` for the 6 canonical scenarios (S1–S6). **This is a required part of Phase 6a**, not optional stretch:

### `backend/tests/test_scenarios.py`
One test function per scenario (`test_scenario_s1_info_only` … `test_scenario_s6_explicit_escalation`), each: seeds a fresh `CallState`, mocks every Claude-backed `tools.py` function to return the scripted values from `docs/scenarios.md`, drives the scenario by calling `dispatcher.process_supervisor_call` in sequence for each scripted utterance (not the individual node functions directly — this exercises the real dispatcher/lock/persistence path, not just graph logic in isolation), and asserts the final `CallState` and `calls` row match what `docs/scenarios.md` specifies. No live network calls anywhere in this file.

This is what actually catches a Phase 6a-or-later change quietly breaking Phase 3/4/5 behavior — run it as part of the full suite (`pytest`), not as a separate manual step.

---

## Definition of Done (cross-cutting — verify as part of Phase 6a)
- [x] `pytest backend/tests/test_tracing.py` passes (verified as early as Phase 2, since tracing starts there — re-verify here as part of the full suite).
- [x] Manual: after any live call, `SELECT event_type, node, payload_json FROM trace_events WHERE call_id=? ORDER BY seq` shows a complete, sensible sequence — every tool call the call actually made appears exactly once as a `tool_call_start`/`tool_call_end` pair. (Verified live with real API keys via `eval/replay_scenarios.py`'s real S2 run: `GET /api/calls/replay-s2-fd85c99c/trace` returns 66 events in correct `seq` order, real `node_entered`/`tool_call_start`/`tool_call_end`/`node_exited` pairs with real tool names/args, plausible durations. See phase-6a's DoD for the fuller writeup and one caveat: `wait_ms` is always `None` in this replay since it awaits turns directly rather than through the fire-and-forget `/bridge` path — a genuine deferred `wait_ms` still needs a live browser/mic call, see below.)
- [x] `pytest backend/tests/test_llm_utils.py` passes.
- [x] Every node test file that has a real Claude call today has at least one `LLMCallFailed`-graceful-handling test, and all pass (`test_routing_node.py`, `test_capture_node.py`). `node_booking`/`node_escalation` now have real Claude calls too (`generate_confirmation_summary`, `confirm_booking_answer`, `generate_call_summary`), added and already routed through `call_claude_tool`/`LLMCallFailed` per CLAUDE.md rule 7 during Phase 4/5 — verified by reading `backend/supervisor/graph.py`, no retrofit needed.
- [x] `pytest backend/tests/test_system_error_escalation.py` passes.
- [x] `pytest backend/tests/test_dispatcher_async.py` passes, **no exceptions** — `test_disconnect_clears_registries` is real now that Phase 5's `CONNECTIONS`/`SPEAKING`/`DEFERRED` registries are merged; it asserts `mark_call_abandoned` clears all three for real, not a skip.
- [x] `pytest backend/tests/test_scenarios.py` — **all 6 scenarios pass for real** now that Phase 4's booking node and Phase 5's `multiple_areas`/`is_explicit_human_request` are merged; driven through the real `dispatcher.process_supervisor_call` entry point. See that file's module docstring for a few scenario-wording gotchas found along the way (FIELD_PRIORITY requiring phone too, an S4 utterance colliding with `EXPLICIT_REQUEST_PHRASES`, S5 needing two dispatcher turns since the graph runs one node per invoke).
- [x] Manual: kill the browser tab mid-call (mid-`capture`, say), confirm the backend log shows the call marked abandoned and the `calls` row reflects it — no lingering entries in `CONNECTIONS`/`SPEAKING`/`DEFERRED` (spot-check via a debug endpoint or a log line, don't just assume). (Verified live by the user — see phase-6a's DoD for the full writeup: call_id `16e0423a-9108-413b-b160-3972acc2119e` confirmed `outcome=abandoned` after closing the tab mid-`capture`.)
- [x] Manual: temporarily set an invalid `ANTHROPIC_API_KEY` mid-testing and attempt a live call — confirm the caller hears a graceful "having a little trouble" reply rather than dead air or a crashed connection, and that 3 consecutive failed turns escalate with `system_error`. (Verified live by the user — see phase-6a's DoD for the full trace: 2 graceful fallbacks then a `system_error` escalation on the 3rd consecutive failure, exactly as designed.)
