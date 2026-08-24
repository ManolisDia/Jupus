# Full codebase review — 2026-08-24

Six-agent review across `backend/`, `eval/`, `admin/`, and `client/`, checked against the architecture doctrine and hard rules in `CLAUDE.md`. Findings ranked most-severe first.

## 1. `mark_call_abandoned` races the supervisor lock (concurrency, CONFIRMED)
**File:** `backend/dispatcher.py:176`

`mark_call_abandoned` mutates `CALL_STATES` and writes DB outcome without holding `get_lock(call_id)`, racing against the lock-holding `process_supervisor_call`.

**Failure scenario:** A caller disconnects while `GRAPH.invoke` is mid-flight (lock held for seconds). `mark_call_abandoned` races in unguarded, sets `outcome='abandoned'` in `CALL_STATES` and the DB. When `GRAPH.invoke` returns, `process_supervisor_call` unconditionally overwrites `CALL_STATES[call_id]` and calls `repos.calls.upsert(updated)` with no `outcome_override`, silently reverting the call to "in progress" and erasing the abandoned marker — a real call gets lost from the abandoned-call view. A narrower reverse race can also spuriously log a `call_abandoned` trace event milliseconds after a real booking and wipe `DEFERRED`/`CONNECTIONS` state out from under the final `deliver_or_defer`, dropping the last reply to the caller.

## 2. Trace event `seq` counter races across threads (concurrency, CONFIRMED)
**File:** `backend/db/repositories/sqlite_trace.py:12`

`_seq_counters` is a plain in-process dict updated with a non-atomic read-modify-write, but `record_event` is called from both the `asyncio.to_thread` worker thread (via `GRAPH.invoke`) and the main event-loop thread (via `drain_deferred`), neither path holding `get_lock(call_id)` at that point.

**Failure scenario:** While `GRAPH.invoke` for a call is mid-flight emitting trace events on a worker thread, a `speech_stopped` event on the main thread triggers `drain_deferred`, which also calls `record_event` for the same `call_id` without the lock. Both threads can read the same `seq` before either writes back, producing two `trace_events` rows with a duplicate `(call_id, seq)` — there's no UNIQUE constraint on that pair, only a non-unique index. `get_trace`'s `ORDER BY seq` then returns an ambiguous/wrong event order, breaking the trace viewer, the `check-backend-logs` skill, and the eval agent's timeline reasoning.

## 3. Trace `seq` counter doesn't survive process restart (correctness, CONFIRMED)
**File:** `backend/db/repositories/sqlite_trace.py:12`

`_seq_counters` resets to empty on process restart, but the dev workflow runs `uvicorn --reload`, which restarts on every file save.

**Failure scenario:** A call is in flight when the dev server auto-reloads on a file save. The new process's `_seq_counters` starts at 0 for that `call_id` even though `trace_events` already has rows with higher `seq` values, so post-restart events get `seq=0,1,2…` again — colliding with or sorting before pre-restart events under `ORDER BY seq`. No code seeds the counter from `SELECT MAX(seq) FROM trace_events WHERE call_id=?` on first use.

## 4. `processing_latency_percentiles` is dead against real data (correctness, CONFIRMED)
**File:** `eval/insights_agent.py:81`

Depends on a `"user_message"` trace event that no code path in the pipeline ever emits, so p50/p95 latency is always 0.0 against real data.

**Failure scenario:** Run `eval/run_eval.py` or hit `GET /api/eval/summary` against real logged calls: since `last_user_message_ts` never gets set, the `reply_delivered` branch that computes latency never fires, and the summary silently reports `{"p50": 0.0, "p95": 0.0}` for every run. The existing unit test passes only because it injects synthetic `"user_message"` events directly, masking that the real code path is unreachable in production.

## 5. Failed classifications inflate the eval error-rate denominator (correctness, CONFIRMED)
**File:** `eval/run_eval.py:41`

A call that fails LLM classification is still tagged into `eval_runs` before classification runs, so it counts in `compute_error_rates`' denominator with zero flags — indistinguishable from a genuinely clean call.

**Failure scenario:** A prompt or judge-side change starts causing `classify_call_errors` to fail/timeout on some calls. Those calls stay in `eval_runs` with no error flags, artificially lowering every error-class rate for that labeled run in `eval/compare_runs.py` — a real regression shows up as an *improvement*. The same calls also silently drop out of future `--calls new` runs unless someone explicitly reruns with `--calls all`, so they may never get classified at all. `calibrate_judge.py` inherits the same ambiguity, understating judge precision/recall against Benevolent Dictator annotations without any signal that classification never ran.

## 6. Classification failure isolation only catches `LLMCallFailed` (correctness)
**File:** `backend/supervisor/llm_utils.py:22`

`run_classification_pass` only catches `LLMCallFailed` (itself limited to `anthropic.APIError`/`json.JSONDecodeError`/`StopIteration`), so any other exception from `classify_call_errors` — e.g. a `KeyError`/`TypeError` from a malformed-but-JSON-valid judge response — still crashes the entire eval batch.

**Failure scenario:** The LLM judge returns syntactically valid JSON that doesn't match the expected schema (e.g. a missing key). `classify_call_errors` raises `KeyError`/`TypeError`, which propagates uncaught past the `except LLMCallFailed` in `run_classification_pass` and aborts the whole `run_eval.py` batch — the exact failure mode the recent "isolate per-call classification failures" fix was meant to eliminate, just for exception types outside its narrower catch surface.

## 7. N+1 queries on every admin panel load (efficiency)
**File:** `backend/app.py:126`

`api_calls_list` issues one `repos.annotations.get_review` and one `repos.evals.get_error_flags` round-trip per call instead of a single batched query, making every admin panel load O(2N+1) SQLite round-trips.

**Failure scenario:** As logged call volume grows, `GET /api/calls` (loaded on every admin panel refresh) does linearly more DB round-trips per request instead of a constant number of batched queries — noticeable latency degradation on the admin panel with no code change elsewhere.

## 8. `reviewed` filter on `CallRepository.list` is dead but documented (correctness)
**File:** `backend/db/repositories/base.py:19`

A documented ABC parameter whose only implementation unconditionally raises `NotImplementedError`, even though the `human_annotations`/`call_reviews` tables it would need already exist and are wired up.

**Failure scenario:** A future caller (e.g. `eval/run_eval.py` wanting to skip already-annotated calls efficiently) calls `repos.calls.list(reviewed=True)` exactly as the interface documents and crashes at runtime instead of getting filtered results.

## 9. `book()` conflates "already booked" with "doesn't exist" (correctness)
**File:** `backend/db/repositories/sqlite_slots.py:96`

Raises the same `SlotAlreadyBookedError` for a nonexistent `slot_id` as for a genuinely already-booked slot, since both produce `rowcount == 0` from the same `UPDATE`.

**Failure scenario:** A caller passes a stale or garbage `slot_id` (e.g. from a suggestion list built against a stale DB snapshot) and gets "slot is not available," which reads as a normal booking conflict rather than surfacing as a data-integrity/programmer error worth investigating differently.

## 10. Fire-and-forget tasks hold no strong reference (concurrency)
**File:** `backend/dispatcher.py:39` (and `:171`)

`asyncio.create_task(...)` results are never stored in a variable or set, so the event loop holds only a weak reference to each `Task`.

**Failure scenario:** Per Python's documented asyncio gotcha, a `Task` with no strong reference elsewhere can be garbage-collected mid-execution, silently killing an in-flight `process_supervisor_call` or `_send_json_safely` with no error surfaced anywhere.

## 11. Trace-then-reply pattern duplicated ~14 times (simplification)
**File:** `backend/supervisor/graph.py:129` (and ~13 more sites)

`repos.trace.record_event('node_exited', ...)` immediately followed by `return {..., **_agent_turn(reply)}` is copy-pasted across roughly 14 branches instead of factored into one helper.

**Failure scenario:** A future change to how turn transitions are traced (e.g. adding a field to every `node_exited` event) requires editing a dozen-plus call sites instead of one; some branches already pass `stage_from=state['stage']` while others hardcode a literal stage name, so the sites are already drifting out of sync with each other.

## 12. Outcome-derivation logic duplicated (simplification)
**File:** `backend/dispatcher.py:28`

`derive_outcome_label` duplicates the identical escalation/booking/info_only mapping already implemented as `_derive_outcome` in `backend/db/repositories/sqlite_calls.py`.

**Failure scenario:** A future terminal outcome state gets added to one implementation but not the other, causing `dispatcher.py`'s `call_ended` trace-log decision and `sqlite_calls.upsert`'s persisted outcome column to silently disagree for the same call.

## 13. `escapeHtml` duplicated between admin pages (simplification)
**File:** `admin/app.js:174`

Duplicated verbatim between `admin/app.js` and `admin/annotate.js` instead of living in a shared module.

**Failure scenario:** A future escaping fix (e.g. handling `null`/undefined differently) gets applied to one file and forgotten in the other, leaving one of the two admin pages with an XSS-relevant inconsistency.

## 14. Dead `FieldCapture.validated` field (simplification)
**File:** `backend/supervisor/state.py:10`

Declared in the `TypedDict` and always initialized to `True`, but never read or set anywhere in production code — actual format validity is tracked via `_is_valid_format`/`status` in `graph.py` instead.

**Failure scenario:** A developer debugging the capture flow reasonably assumes `validated` reflects per-field validation state and spends time looking for where it's set to `False` — it never is, since the real signal lives elsewhere.

---

*Generated from a 6-agent review (repository-pattern/tracing rule compliance, architecture-doctrine/altitude conventions, reuse/simplification/efficiency sweep, `eval/` correctness, DB/repository SQL correctness, and a dispatcher concurrency deep dive). No fixes applied yet.*
