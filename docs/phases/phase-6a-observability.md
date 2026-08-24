# Phase 6a — Observability Foundation

## Goal

Make every call inspectable and prove the pipeline is robust, before any of the LLM-judge/taxonomy machinery exists. Two things: deterministic stats + a basic admin panel (calls list, drill-in, full trace viewer) on top of what Phases 2–5 already persist, and closing out `docs/phases/cross-cutting.md`'s three items, none of which need anything from 6b/6c.

**Dependency note**: this sub-phase depends only on Phases 1–5, `docs/architecture.md`, and `docs/phases/cross-cutting.md` — nothing here reads `call_error_flags`, `eval_runs`, `taxonomy_suggestions`, `call_reviews`, or `human_annotations` (all introduced in 6b/6c). 6b and 6c both depend on 6a being done; 6a never depends forward on them.

## Non-goals
- No error-taxonomy classification, no LLM judge, no `eval/error_classes.py` yet — that's 6b.
- No Benevolent Dictator annotation, no calibration, no regression harness — that's 6c.
- No live/real-time dashboard, no auth on admin routes — same as the rest of the project.

## Prerequisite
Phase 5 DoD met — escalation and async dispatcher confirmed working live.

## This phase closes out `docs/phases/cross-cutting.md`
All three items there — verified here since this is the first point after Phase 5 where a checkpoint makes sense, not because they depend on anything built in this sub-phase:
1. `backend/supervisor/llm_utils.py` (`call_claude_tool`, `LLMCallFailed`) retrofitted into every `tools.py` Claude call written since Phase 3, plus each node's graceful-fallback handling and the `system_error` escalation path.
2. WebSocket disconnect cleanup (`dispatcher.mark_call_abandoned`, the `/bridge` handler's `except WebSocketDisconnect` block, `CallRepository.upsert`'s `outcome_override` param).
3. `backend/tests/test_scenarios.py` — the 6 mocked-Claude scenario regression tests from `docs/scenarios.md` (fast, deterministic, part of every `pytest` run).

If any of these were already built incrementally while doing Phases 3–5 (recommended, per `CLAUDE.md` rule 7), this sub-phase is just where they're verified complete.

---

## Deterministic metrics — exact definitions

```python
def booking_success_rate(calls: list[dict]) -> float:
    # among calls where outcome in ("booked", "escalated") — calls that
    # reached a real conclusion — booked / (booked + escalated).
    # Returns 0.0 if the denominator is 0, never raises ZeroDivisionError.
    ...

def escalation_reason_histogram(calls: list[dict]) -> dict[str, int]:
    # {reason: count} for outcome == "escalated", grouped by escalation_reason
    ...

def average_turns_per_call(calls: list[dict]) -> float:
    # mean of len(json.loads(c["transcript_json"])) across the given calls
    ...

def processing_latency_percentiles(trace_repo: TraceRepository, call_ids: list[str]) -> dict[str, float]:
    # Computed from trace_events (docs/phases/cross-cutting.md section 0),
    # not transcript timestamps — trace_events' "reply_delivered" events
    # carry an accurate wait_ms (0 for immediate delivery, the real queued
    # duration for a deferred one, per Phase 5's dispatcher). For each
    # call_id: for every "reply_delivered" event, the round-trip latency is
    # (that event's ts) - (the preceding "user_message" event's ts for the
    # same ask_supervisor turn). Pool all round-trip latencies across the
    # given call_ids, return {"p50": ..., "p95": ...}.
    ...
```

## `eval/insights_agent.py` (new file — only the deterministic pass in this sub-phase)
```python
def run_deterministic_pass(repos: Repositories, calls: list[dict]) -> dict:
    # {"booking_success_rate": ..., "escalation_reason_histogram": ...,
    #  "average_turns_per_call": ..., "latency": {"p50": ..., "p95": ...}}
    # combining the four functions above, using repos.trace for latency.
    # 6b extends this module with classification; 6c extends it further
    # with taxonomy critique — this file grows across the three sub-phases,
    # same incremental-extension pattern used for dispatcher.py across
    # Phases 2 and 5.
    ...
```

## `backend/db/seed_demo_calls.py` (new file — minimal base version)
3 canned rows inserted directly into `calls` (hand-authored `transcript_json`, bypassing the live pipeline): 2 `outcome="booked"`, 1 `outcome="escalated"` with a legitimate reason. Just enough to exercise the admin panel and deterministic stats. 6b extends this file with error-class-exhibiting rows; 6c extends it further with pre-populated annotations.

---

## Admin panel (base)

- `GET /admin` — HTML shell, vanilla JS
- `GET /api/calls` — list: call_id, started_at, practice_area, outcome, escalation_reason, booking_slot_id (no error badges or "reviewed" flag yet — those are additive in 6b/6c)
- `GET /api/calls/{call_id}` — drill-in: turn-by-turn transcript only in this sub-phase (call_error_flags and human_annotations are added to this response in 6b/6c respectively). 404 if unknown.
- `GET /api/calls/{call_id}/trace` — the full ordered `trace_events` list (`repos.trace.get_trace`) — every tool call, retry, stage transition, and delivery decision. This is the "show me exactly what happened" view for the video, and it's fully available now since tracing has existed since Phase 2.
- `GET /api/eval/summary` — `run_deterministic_pass` output only in this sub-phase (no `error_rates` key yet — added in 6b).

### `admin/index.html` + `admin/app.js` (base)
Calls list + drill-in (transcript) + a "view full trace" toggle rendering `/api/calls/{call_id}/trace` as an ordered timeline (event type, node, key payload fields). No error badges, no taxonomy panel, no annotate link yet.

---

## Tests

### `eval/tests/test_insights_agent.py` (started here, extended in 6b/6c)
1. `test_booking_success_rate_exact_value` — construct 3 `"booked"` + 1 `"escalated"` calls, assert rate `== 0.75`.
2. `test_booking_success_rate_zero_denominator` — empty list, assert `0.0`, not a `ZeroDivisionError`.
3. `test_escalation_histogram_counts_correctly`.
4. `test_average_turns_per_call`.
5. `test_latency_percentiles_from_trace_events` — seed `trace_events` rows with known `user_message`/`reply_delivered` timestamp gaps (including at least one with a non-zero `wait_ms` simulating a deferred delivery), assert `p50`/`p95` match expected values.

### `backend/tests/test_seed_demo_calls.py` (started here)
1. `test_seed_creates_three_calls_with_expected_outcomes`.

### `backend/tests/test_admin_routes.py` (started here, extended in 6b/6c)
1. `test_api_calls_list_returns_seeded_calls`.
2. `test_api_call_detail_returns_transcript` — assert transcript present, no `call_error_flags`/`human_review` keys expected yet.
3. `test_api_call_detail_404_for_unknown_id`.
4. `test_api_call_trace_returns_ordered_events` — seed `trace_events` out of insertion order via direct SQL, assert the endpoint returns them ordered by `seq`.
5. `test_api_eval_summary_returns_deterministic_keys_only`.
6. `test_admin_page_serves_html`.

### Cross-cutting closeout tests (per `docs/phases/cross-cutting.md`)
`backend/tests/test_llm_utils.py`, the `LLMCallFailed`-graceful-handling test added to each node test file, `backend/tests/test_system_error_escalation.py`, the 3 disconnect tests in `backend/tests/test_dispatcher_async.py`, and `backend/tests/test_scenarios.py` (all 6 canonical scenarios, mocked).

---

## Definition of Done

- [x] `python backend/db/seed_demo_calls.py` runs clean, produces 3 calls with expected outcomes. (verified live against a real `calendar.db`.)
- [x] `pytest eval/tests/test_insights_agent.py backend/tests/test_seed_demo_calls.py backend/tests/test_admin_routes.py` — all pass.
- [x] `pytest backend/tests/test_llm_utils.py backend/tests/test_system_error_escalation.py backend/tests/test_dispatcher_async.py backend/tests/test_scenarios.py` — all pass (cross-cutting closeout). **Update after rebasing onto Phase 4/5 (phase-5-escalation-async @ 449d651):** `test_disconnect_clears_registries` is no longer skipped — Phase 5's real `CONNECTIONS`/`SPEAKING`/`DEFERRED` registries exist now and the test asserts `mark_call_abandoned` clears all three for real. `test_scenarios.py` now implements all 6 scenarios for real against the real booking node (S2/S3) and the real `multiple_areas` value / `is_explicit_human_request` heuristic (S5/S6), driven through `dispatcher.process_supervisor_call` (the real entry point; the earlier `on_ask_supervisor` name was from a pre-Phase-5 draft). See that file's module docstring and its git history for the couple of scenario-wording gotchas found along the way (FIELD_PRIORITY requiring phone too, an S4 utterance colliding with `EXPLICIT_REQUEST_PHRASES`, S5 needing two dispatcher turns since the graph runs one node per invoke).
- [x] Manual: `/admin` shows the seeded calls list; clicking a call shows its transcript. (verified live via `uvicorn` + `curl`/redirect-follow against `/admin`, `/api/calls`, `/api/calls/{id}`.)
- [x] Manual (partial — see caveat): view the full trace for one real live call from Phase 5's scenarios in the admin panel — confirm every tool call it actually made appears, in order, with plausible durations, and that a deferred reply (if that scenario produced one) shows a non-zero `wait_ms`. (Verified live with real API keys: `GET /api/calls/replay-s2-fd85c99c/trace` — one of `eval/replay_scenarios.py`'s real S2 run — returns 66 correctly-ordered events by `seq`, with real `node_entered`/`tool_call_start`/`tool_call_end`/`node_exited` events, real tool names/args in the payload, and plausible durations (~1.5-3s for real Claude calls, ~10ms for deterministic ones). **Caveat: every `wait_ms` is `None`/0 across all 6 replayed scenarios** — `replay_scenarios.py` awaits each turn directly rather than going through `on_bridge_message`'s fire-and-forget wrapping (per its own module docstring), so it never exercises the deferred-delivery path at all. Confirming a real non-zero `wait_ms` genuinely still requires a live browser/mic call with real concurrent audio, which remains blocked here.)
- [x] Manual: `GET /api/eval/summary` returns sensible deterministic numbers matching the seeded data by hand-calculation. (0.667 booking success rate = 2/3 booked+escalated; histogram `{"unable_to_classify": 1}`; avg turns `(14+14+4)/3 = 10.667`; latency 0/0 since seeded calls carry no trace_events — all match hand-calculation.)
- [ ] Manual: kill the browser tab mid-call, confirm the backend marks it abandoned and the registries (`CONNECTIONS`/`SPEAKING`/`DEFERRED`) are cleared. **STILL BLOCKED on a live browser/mic session (none possible in this environment) — but the registries now exist for real (Phase 5) and the full non-manual behavior (`mark_call_abandoned` sets `outcome="abandoned"`, doesn't override an already-ended call, and clears all three of `CONNECTIONS`/`SPEAKING`/`DEFERRED`) is unit-tested for real in `test_dispatcher_async.py::test_disconnect_clears_registries` (no longer a stub/skip).**
- [ ] Manual: temporarily break `ANTHROPIC_API_KEY`, attempt a live call, confirm a graceful fallback reply rather than dead air, and `system_error` escalation after 3 consecutive failures. **STILL BLOCKED — requires a live browser/mic call against real OpenAI/Anthropic keys; this environment's `.env` only has placeholder values (see note above), so this can't be attempted here either live or via a real API run. The underlying behavior (graceful fallback on 1-2 failures, `system_error` escalation on the 3rd) is unit-tested end-to-end in `test_system_error_escalation.py` and per-node test files with mocked failures — now including the real booking/escalation node Claude calls (`generate_confirmation_summary`, `confirm_booking_answer`, `generate_call_summary`), which already routed through `call_claude_tool`/`LLMCallFailed` per CLAUDE.md rule 7 as built during Phase 4/5, verified by reading `backend/supervisor/graph.py`'s `node_booking`/`node_escalation` rather than duplicating that wiring.**
