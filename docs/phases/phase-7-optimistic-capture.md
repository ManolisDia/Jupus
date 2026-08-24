# Phase 7 — Optimistic Capture

## Goal

Decouple the *felt* latency of field capture from the real latency of a Claude round-trip. Today, every `ask_supervisor` call blocks the Realtime model mid-turn until the backend's real extraction/validation/confirm-back logic finishes (see "Why this exists" below) — 4-6 seconds of silence per field is common. This phase makes the common case (the caller answers the expected field, plainly) feel instant, while keeping all actual sequencing decisions deterministic and backend-owned, per rule 2. Booking/routing/escalation are explicitly out of scope — see Non-goals.

## Prerequisite
Phases 1-6 (a/b/c) DoD met. Builds directly on top of `backend/supervisor/graph.py`'s `node_capture`, `backend/supervisor/state.py`'s `FIELD_PRIORITY`/`FieldCapture`, and `backend/dispatcher.py`'s async dispatch — read all three in full before starting, plus `docs/DECISIONS.md`'s entries on the email/phone confirm-back threshold and the `ask_supervisor` single-tool decision (this phase does *not* revisit either of those, it works within them).

## Why this exists — the actual mechanism, not a guess

Traced live during design discussion (see conversation history / commit log around 2026-08-24 for the full trail): when Realtime calls the `ask_supervisor` function, it is *structurally paused* per OpenAI's Realtime tool-calling protocol — it cannot speak again until `client/app.js` sends a `function_call_output` for that specific `call_id`, which today only happens once the backend's real `supervisor_result` comes back over `/bridge`. The Phase 5 "async fire-and-forget dispatcher" makes the *server* non-blocking (doesn't freeze on other calls/VAD events), but does nothing to shorten what the *caller* waits through, because the model is deliberately blocked on that one specific tool result. This phase addresses that directly, for field capture only.

## Non-goals
- Routing (`classify_practice_area`) and booking (availability/conflict) are **not** in scope — both are genuinely branchy; there's no deterministic "next question" to guess optimistically without the real result, unlike field-order which is fixed (`FIELD_PRIORITY`).
- Not removing `ask_supervisor` as Realtime's one tool (rule 1 stays intact) — this phase changes *what* gets returned and *when*, not the transport.
- Not touching the confirm-back drain phase's own turn-taking — that phase stays fully synchronous (see Decision 2 below), deliberately paced like a real "let me just double-check a couple of things" moment.

## Decisions made, not left open for the implementer

**1. Gate strictness — err conservative.** A false-positive (falls back to today's slower-but-correct behavior) costs nothing new; a false-negative (guesses wrong, asks a jarringly disconnected next question) is a real, visible UX failure. When `looks_like_tangent`/the shape-check is ambiguous, do **not** go optimistic — run the real, synchronous path for that one turn, exactly as today.

**2. The `capture_confirm` sub-phase stays fully synchronous.** No fast-path inside it. It's meant to read as a deliberate, distinct "let me just confirm a couple of things" moment, not another opportunity to shave latency — a real receptionist pauses here too.

**3. `retry_counts`/3-strikes escalation ownership is unchanged.** Only the *real* verification path (`_verify_field_in_background`, and the `capture_confirm` re-ask branch) increments `retry_counts` or triggers `capture_failed` escalation — the fast path never does, since it never actually verified anything. This preserves today's escalation semantics exactly; nothing about when a call escalates changes, only when the *caller hears about it*.

---

## State additions

```python
# backend/supervisor/state.py
class CallState(TypedDict):
    ...
    capture_phase: Literal["fast", "confirm"]   # only meaningful while stage == "capture"
    last_asked_field: Optional[str]             # field whose answer is currently outstanding in the fast pass
```

```python
# backend/dispatcher.py — mirrors the existing LOCKS/SPEAKING/DEFERRED module-level dict pattern.
# NOT part of CallState: asyncio.Task objects aren't plain/serializable data.
FIELD_VERIFICATIONS: dict[tuple[str, str], asyncio.Task] = {}   # (call_id, field_name) -> background verification task
```

No new queue data structure. `caller_profile[field]["status"]` (`missing`/`pending_confirm`/`confirmed`, unchanged) *is* the queue — the confirm phase just walks `FIELD_PRIORITY` and asks about whichever fields aren't yet `"confirmed"`.

## New heuristic

```python
# backend/supervisor/heuristics.py
def looks_like_tangent(utterance: str) -> bool:
    # Cheap, deterministic, same spirit as the existing is_explicit_human_request:
    # starts with a WH-word/hedge ("what", "why", "wait", "actually", "sorry"),
    # or otherwise doesn't read as a direct answer to whatever was just asked.
    # Tune conservatively per Decision 1 above.
```

For email/phone specifically, no new heuristic is needed — `tools.validate_email`/`validate_phone` already exist, are pure code, sub-millisecond, and are the correct per-field gate.

## Graph changes

### `node_capture_fast` (new) — replaces `node_capture` while `capture_phase == "fast"`

```python
def node_capture_fast(state: CallState, config: RunnableConfig) -> dict:
    profile = state["caller_profile"]
    utterance = state["transcript"][-1]["text"]
    asked_field = state["last_asked_field"]

    # Urgent check: did the field we asked about LAST time already resolve
    # to a genuine failure? If so, re-ask now rather than advancing further.
    # (dispatcher checks FIELD_VERIFICATIONS[(call_id, asked_field)].done()
    # before invoking this node — passed in via config, or checked here if
    # the node has access to it; exact wiring is an implementation detail,
    # the behavior above is not.)
    if asked_field and <that field's real status resolved to "missing">:
        return node_capture(state, config)  # real re-ask, today's logic, unchanged

    # Gate: is it safe to guess this utterance answers `asked_field`?
    if asked_field and (
        heuristics.is_explicit_human_request(utterance)
        or heuristics.looks_like_tangent(utterance)
        or (asked_field in ("email", "phone") and not <cheap shape check passes>)
    ):
        return node_capture(state, config)  # doubt -> real, synchronous path, today's logic

    next_field = <first FIELD_PRIORITY entry after asked_field with status == "missing">
    if next_field:
        return {
            "last_asked_field": next_field,
            "_background_verify_field": asked_field,  # dispatcher spawns the real check
            **_agent_turn(f"Great — and what's your {FIELD_LABELS[next_field]}?"),
        }

    # Fast pass exhausted (every field has been fast-asked once) -> hand off
    return {"capture_phase": "confirm", "_background_verify_field": asked_field, **_agent_turn(None)}
```

`node_capture_fast` is the thing deciding what to ask next — it stays inside the deterministic graph (rule 2 intact), same as `FIELD_PRIORITY` governs `node_capture` today. It never calls Claude.

### Background verification (dispatcher-owned)

`_verify_field_in_background(repos, call_id, field, utterance)` is today's `node_capture` logic for one field — `extract_field` → validate → `apply_extraction` (or `confirm_field_answer` if applicable) — minus producing a spoken reply. It updates `caller_profile[field]["status"/"value"]` under the same per-`call_id` lock (`dispatcher.get_lock`) once the real Claude call resolves. Spawned by `dispatcher.py` right after `GRAPH.invoke` returns, keyed off the `_background_verify_field` signal `node_capture_fast` returns:

```python
# dispatcher.py, right after GRAPH.invoke / asyncio.to_thread(GRAPH.invoke, ...)
if field := updated.pop("_background_verify_field", None):
    FIELD_VERIFICATIONS[(call_id, field)] = asyncio.create_task(
        _verify_field_in_background(repos, call_id, field, utterance)
    )
```

### `node_capture_confirm` (new) — handles `capture_phase == "confirm"`

Walks `FIELD_PRIORITY` in that fixed order (never completion-order — background tasks for different fields can finish out of order due to variable API latency; draining by completion-order would ask about confirming a later field before an earlier one, which reads as backwards). For the first field not yet `"confirmed"`:
- If its background task hasn't finished, `await` it now — the one point a real wait can still happen here, bounded, and by this point in the conversation Graph B has usually had the whole time the caller spent speaking every subsequent field to catch up, so this is typically small or zero.
- If `pending_confirm` → ask the read-back-and-confirm question, exactly like today's `node_capture` `pending_field` branch. On the *first* item of this phase only, prefix with a fixed transitional line ("Great, let me just quickly confirm a couple of things:") so the batched confirm-backs read as a deliberate phase, not a disconnected non-sequitur several turns after the fact.
- If `missing` (genuine failure not caught earlier) → real re-ask, today's `_deny_and_reprompt` logic, unchanged including its 3-strikes escalation.
- If already `confirmed` (only possible for name/preferred_time auto-confirming at ≥0.75) → skip silently, move to the next field.

Once every field is `"confirmed"`, transition to `"booking"` exactly as today's `node_capture` does when `remaining` is empty.

### `route_by_stage`

Extend to dispatch `capture` + `capture_phase == "fast"` to `node_capture_fast`, `capture` + `capture_phase == "confirm"` to `node_capture_confirm`. `new_call_state` initializes `capture_phase="fast"`, `last_asked_field=None`.

---

## Worked example (matches the design conversation's dialogue exactly)

1. "Manos" → `node_capture_fast`: gate passes, background-verifies `name`, asks "what's your email?" — **zero visible wait**.
2. "manos@gmail.com" → gate passes (shape check: contains `@`), background-verifies `email`, asks "what's your phone number?" — **zero visible wait**. (`name`'s background check has typically already resolved `confirmed` by now, since ≥0.75 confidence auto-confirms with no read-back.)
3. "07577670101" → gate passes (shape check: digit-heavy), background-verifies `phone`, no more fields left → `capture_phase` becomes `"confirm"`.
4. Drain: `name` already `confirmed` → skip. `email` is `pending_confirm` (mandatory, regardless of confidence, per `docs/DECISIONS.md`) → *"Great, let me just quickly confirm a couple of things: was that manos@gmail.com?"* → caller confirms → `phone`'s real check has typically finished by now too → *"And your phone number, 07577670101 — is that right?"*

---

## Tests

Same pattern as the existing `test_capture_node.py` — mock the Claude-backed `tools.py` functions, drive turns through `dispatcher.process_supervisor_call`, assert `CallState` transitions and spoken replies. New cases specific to this phase:

1. `test_fast_pass_asks_next_field_with_zero_llm_calls` — assert `node_capture_fast` produces the next question without any `call_claude_tool`/`call_claude_json` call being made.
2. `test_fast_pass_gate_falls_back_on_tangent` — an utterance matching `looks_like_tangent` (or `is_explicit_human_request`) routes to `node_capture` (real, synchronous path), not an optimistic guess.
3. `test_fast_pass_gate_falls_back_on_bad_shape` — an email-field turn whose utterance doesn't pass the cheap shape check falls back to the real path.
4. `test_urgent_reask_before_advancing` — a background verification that's already resolved to `"missing"` by the time the next turn arrives triggers a real re-ask, not a further optimistic advance.
5. `test_confirm_phase_drains_in_field_priority_order_not_completion_order` — seed background tasks that resolve out of order (e.g. `phone`'s finishes before `email`'s); assert the drain still asks about `email` first.
6. `test_confirm_phase_skips_already_confirmed_fields_silently` — a `name` that auto-confirmed during the fast pass produces no confirm-back turn.
7. `test_confirm_phase_awaits_unfinished_background_task` — drain reaching a field whose background task hasn't resolved yet correctly waits for it rather than reading stale/default state.
8. `test_confirm_phase_first_item_gets_transition_preamble_only_once` — the fixed "let me just quickly confirm..." line appears exactly once, on the first drain item, not on every one.
9. `test_retry_counts_only_incremented_by_real_verification` — the fast path advancing through several fields never touches `retry_counts`; only `_verify_field_in_background`/`node_capture_confirm`'s re-ask branch does.
10. Full end-to-end: `test_scenario_s2_happy_path_booking` (existing, in `backend/tests/test_scenarios.py`) still passes with this phase's changes — the observable outcome (booked, confirmed profile) must be identical to today, only the turn-by-turn shape of getting there changes. Update rather than duplicate if the turn count assertions need adjusting.

---

## Definition of Done

- [x] `node_capture_fast`, `node_capture_confirm`, `looks_like_tangent` implemented per the design above. (`node_capture_confirm` was initially just `node_capture` registered directly under that name, per Decision 2 — a real independent-review finding required adding a genuine thin wrapper function after all, to guard against a background-verification failure surfacing after the confirm phase had already started; see `docs/fixes/2026-08-24-005.md`.)
- [x] `pytest` (full suite) passes with zero failures — 262 tests, including `test_scenarios.py` (all 6 canonical scenarios) and `test_capture_fast.py`'s direct unit coverage. Test names/count don't map 1:1 onto this doc's original illustrative list of 10 — some were combined, some (the urgent-reask/delayed-failure tests) were rewritten entirely once the independent review found the original mechanism didn't work as designed. `_next_fast_field`'s resume-skips-resolved-fields behavior and both the fast-pass and confirm-phase delayed-failure interrupts are covered directly.
- [x] Manual, live: run through the field-capture flow with a real browser/mic session, confirm the fast pass genuinely has no audible gap between fields, and the confirm phase reads naturally with its transitional line. (User-confirmed: "works perfectly.")
- [x] Manual, live: deliberately mumble/garble an email so real extraction fails — confirm the urgent-reask path catches it promptly rather than deferring all the way to the end of the fast pass. (Confirmed working after the fix in `docs/fixes/2026-08-24-005.md` — the original mechanism this item describes could never actually fire; a real call in `backend/db/calendar.db` surfaced this during the independent pre-merge review, and the corrected interrupt-and-reask behavior is now covered by direct regression tests.)
- [x] Manual, live: say something off-topic ("wait, how long will this take?") in place of an expected field answer — confirm the gate correctly falls back to the real, synchronous path rather than guessing wrong. (User-confirmed live; also independently corroborated against real trace data — `capture_fast_gate_fallback` fired for shape-check failures in the same live session.)
- [ ] `eval/replay_scenarios.py`'s S2/S3 (the two booking scenarios that exercise full field capture) re-run live against the real pipeline, produce the same real outcomes as before this phase (booked, correct slot), confirming the optimistic path doesn't change what actually gets captured/booked, only how it feels. **Not yet independently re-run since the delayed-failure fix landed** — worth a fresh live pass before or shortly after merge to confirm the fix didn't change the happy-path outcome.

**Known, accepted residual limitation** (not blocking merge, see `docs/fixes/2026-08-24-005.md`'s tail): a background verification that is still genuinely in-flight (neither succeeded nor failed) by the time the confirm/drain phase begins is never explicitly awaited — judged pathological enough in practice (would require a Claude call slower than the rest of the fast pass plus a drain turn combined) not to warrant blocking-wait infrastructure. The *failure* case (the one with real evidence of occurring) is fully fixed.
