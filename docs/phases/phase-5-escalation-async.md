# Phase 5 — Escalation (User Story 4) + Async Dispatcher Hardening

## Goal

Two things, tightly related: (1) implement the real `escalation` node and handoff-note writing for all five escalation reasons, including the two new trigger paths (explicit request, multi-area out-of-scope); (2) replace the synchronous dispatcher from Phase 2 with the real fire-and-forget / deferred-delivery / staleness-aware version — this is the single most differentiating piece of the submission, so treat its tests as non-negotiable, not just nice-to-have.

## Non-goals
- No admin panel / eval agent yet (Phase 6a+).
- Not attempting full NLU coverage of every possible phrasing of "let me talk to a human" — explicit-request detection is a deterministic keyword check with documented coverage limits (see below), not a Claude call. A missed phrasing simply falls through to normal node processing rather than escalating — acceptable and noted as a limitation in Phase 7.

## Prerequisite
Phase 4 DoD met — booking flow (including conflict + decline-twice escalation) confirmed working live. Also read `docs/architecture.md` if you haven't — `dispatcher.py` below takes the `Repositories` bundle as a parameter throughout, never touches `sqlite3` directly.

---

## Escalation trigger paths — where each `escalation_reason` comes from

| Reason | Set by | When |
|---|---|---|
| `unable_to_classify` | `node_routing` (Phase 3) | 2nd consecutive "unclear" classification |
| `out_of_scope_multi_area` | `node_routing` (**modified this phase**) | classification returns `"multiple_areas"` — immediately, no retry |
| `capture_failed` | `node_capture` (Phase 3) | 3rd failed attempt on the same field |
| `no_acceptable_slot` | `node_booking` (Phase 4) | no alternatives available, or 2 declines |
| `explicit_request` | `dispatcher.py` (**new this phase**) | deterministic keyword match, any stage, checked before graph invocation |
| `system_error` | any node, via the error wrapper; **or** `dispatcher.process_supervisor_call`'s catch-all | 3 consecutive upstream API failures (`docs/phases/cross-cutting.md`, verified alongside Phase 6a) — **or** any unhandled exception anywhere in a supervisor turn (this phase's `process_supervisor_call` try/except), forced straight to escalation without retrying |

### `node_routing` modification (supersedes Phase 3's version — same file, this is a targeted change)
`classify_practice_area`'s schema gains a 5th value: `area: "employment"|"tenancy"|"immigration"|"multiple_areas"|"unclear"`. Update `CLASSIFY_PRACTICE_AREA_PROMPT` to instruct: *"If the caller's issue genuinely spans more than one area (e.g. an employment dispute tangled with an immigration status question), return multiple_areas. If it's simply unclear or you don't have enough information yet, return unclear."* These are different failure modes and must not be conflated: `multiple_areas` escalates immediately (a retry won't resolve genuine multi-area complexity), `unclear` still gets one reprompt (a retry might resolve genuine ambiguity).

```
if result["area"] == "multiple_areas":
    return {"stage": "escalation", "escalation_reason": "out_of_scope_multi_area", ...}
if result["area"] == "unclear":
    # existing Phase 3 retry-then-escalate logic, unchanged
    ...
```

---

## `backend/supervisor/heuristics.py` (new file)

```python
EXPLICIT_REQUEST_PHRASES = [
    "speak to a person", "talk to a human", "real person",
    "representative", "talk to someone", "human agent",
    "speak with someone", "get me a person", "transfer me",
    "speak to someone else", "human being",
]

def is_explicit_human_request(utterance: str) -> bool:
    # Deterministic. Lowercase the utterance, check for any phrase as a
    # substring. Documented limitation: creative phrasing not in this
    # list (e.g. "is there a way to skip the robot") won't be caught —
    # acceptable for this scope, note in docs/answers.md's limitations
    # section rather than trying to build full-coverage NLU here.
    ...
```
This module is also the natural home for the optional turn-detection heuristic mentioned in `docs/DECISIONS.md`, if there's time for it in Phase 7 — don't build that now, just note the file exists for it.

---

## `backend/supervisor/tools.py` additions

```python
def generate_call_summary(state: CallState) -> str:
    # Claude call. Short paragraph: what the caller needs, what's been
    # captured so far, and why a human is needed — grounded in
    # state["transcript"] and state["escalation_reason"].
    ...

def write_handoff_note(call_id: str, state: CallState, summary: str) -> Path:
    # Deterministic file write, NOT a Claude call. Writes to
    # docs/handoffs/{call_id}.md:
    #
    #   # Escalation — {call_id}
    #   Time: {now_iso()}
    #   Practice area: {state["practice_area"] or "not yet determined"}
    #   Reason: {state["escalation_reason"]}
    #
    #   ## Caller details collected
    #   - Name: {value if status=="confirmed" else "not captured"}
    #   - Email: {value if status=="confirmed" else "not captured"}
    #   - Phone: {value if status=="confirmed" else "not captured"}
    #
    #   ## Summary
    #   {summary}
    #
    # Only fields with status=="confirmed" are shown as captured — a
    # "pending_confirm" or "missing" field must render as "not captured",
    # never leak a half-confirmed guess into a document a human will act on.
    ...

def write_minimal_handoff_note(call_id: str, state: CallState, reason: str) -> Path:
    # Deterministic file write, identical output location/format to
    # write_handoff_note above, but takes a plain string `reason` directly
    # instead of a Claude-generated `summary` — used only by the dispatcher's
    # catch-all exception handler, where calling generate_call_summary (a
    # Claude call) would mean trusting another LLM call to succeed right
    # after one just failed unexpectedly. Same "Summary" section, just
    # populated with the raw reason string instead of a generated paragraph.
    ...
```

## `backend/supervisor/graph.py` — `node_escalation` replacement
```
def node_escalation(state: CallState) -> dict:
    summary = generate_call_summary(state)
    write_handoff_note(state["call_id"], state, summary)
    return {"stage": "ended",
            "pending_reply": "<graceful closing line, e.g. 'I've passed this to our team, someone will follow up shortly.'>",
            "transcript": [...]}
```

---

## Async dispatcher (`backend/dispatcher.py`) — full rewrite, supersedes Phase 2

```python
# Every function below takes `repos: Repositories` (docs/architecture.md) —
# never imports sqlite3, never calls a bare get_db_conn(). app.py's WS
# handler holds the single app-level Repositories instance and passes it
# into on_bridge_message for every message on that connection.

LOCKS: dict[str, asyncio.Lock] = {}          # one lock per call_id
SPEAKING: dict[str, bool] = {}               # updated from relayed VAD events
DEFERRED: dict[str, list[tuple[str, str, str, float]]] = {}  # call_id -> [(tool_call_id, reply, dispatch_stage, queued_at)]
CONNECTIONS: dict[str, WebSocket] = {}       # call_id -> active /bridge socket

def get_lock(call_id: str) -> asyncio.Lock:
    return LOCKS.setdefault(call_id, asyncio.Lock())

async def on_bridge_message(repos: Repositories, call_id: str, msg: dict) -> None:
    if msg["type"] == "ask_supervisor":
        asyncio.create_task(process_supervisor_call(repos, call_id, msg["tool_call_id"], msg["last_caller_utterance"]))
        # returns immediately — the WS receive loop is never blocked here
    elif msg["type"] == "speech_started":
        SPEAKING[call_id] = True
    elif msg["type"] == "speech_stopped":
        SPEAKING[call_id] = False
        drain_deferred(repos, call_id)

async def process_supervisor_call(repos: Repositories, call_id: str, tool_call_id: str, utterance: str) -> None:
    # This whole function runs inside a bare asyncio.create_task with
    # nothing awaiting it (on_bridge_message fires-and-forgets it). If
    # ANYTHING raises here that isn't already caught internally — a
    # deterministic tool hitting an unexpected error, a bug in node logic,
    # a repository failure, anything at all — asyncio will silently log
    # "Task exception was never retrieved" and drop it. The caller gets
    # dead air, no fallback, no escalation, nothing. This is a strictly
    # worse failure mode than the LLMCallFailed path (cross-cutting.md
    # section 1), which only covers Claude API failures nodes explicitly
    # catch. The try/except below is the backstop for everything else.
    try:
        async with get_lock(call_id):
            # Holding the lock across the whole invoke serializes concurrent
            # supervisor turns FOR THE SAME call_id, preventing lost-update
            # races on CALL_STATES — see docs/DECISIONS.md. It does NOT block
            # the audio/WS layer, since create_task already returned before
            # this coroutine started waiting on the lock.
            state = get_or_create_state(call_id)
            if state["stage"] == "ended":
                deliver_or_defer(repos, call_id, tool_call_id, "This call has already been completed.", "ended")
                return
            state["transcript"] = state["transcript"] + [{"role": "caller", "text": utterance, "ts": now_iso()}]
            if is_explicit_human_request(utterance):
                state["stage"] = "escalation"
                state["escalation_reason"] = "explicit_request"
            dispatch_stage = state["stage"]
            # repos reaches node functions via LangGraph's config/configurable
            # mechanism, not a second positional arg to invoke() — see the note
            # below this code block.
            updated = GRAPH.invoke(state, config={"configurable": {"repos": repos}})
            CALL_STATES[call_id] = updated
    except Exception as e:
        # Catch-all safety net — deliberately broad. Do NOT re-run the
        # graph (it just failed once already, from an unknown cause —
        # retrying risks the same failure again, or worse, a partial/
        # inconsistent state). Force straight to escalation instead.
        logger.exception("unhandled error processing call_id=%s", call_id)
        repos.trace.record_event(call_id, "unhandled_error", error=str(e))
        state = CALL_STATES.get(call_id) or get_or_create_state(call_id)
        state["stage"] = "ended"
        state["escalation_reason"] = "system_error"
        CALL_STATES[call_id] = state
        repos.calls.upsert(state)
        # Write a minimal handoff note directly here (deterministic, no
        # Claude call) rather than routing through node_escalation's
        # generate_call_summary — we just caught an unexpected exception,
        # this isn't the moment to trust another LLM call to succeed.
        write_minimal_handoff_note(call_id, state, reason=f"Unhandled error: {e}")
        deliver_or_defer(repos, call_id, tool_call_id,
                          "Sorry, something went wrong on my end — let me get you to someone who can help.",
                          "escalation")
        return
        repos.calls.upsert(updated)
    deliver_or_defer(repos, call_id, tool_call_id, updated["pending_reply"], dispatch_stage)

def deliver_or_defer(repos: Repositories, call_id: str, tool_call_id: str, reply: str, dispatch_stage: str) -> None:
    if SPEAKING.get(call_id, False):
        DEFERRED.setdefault(call_id, []).append((tool_call_id, reply, dispatch_stage, time.monotonic()))
        repos.trace.record_event(call_id, "reply_deferred", tool_call_id=tool_call_id, reason="caller_speaking")
    else:
        send_over_bridge(call_id, tool_call_id, reply)
        repos.trace.record_event(call_id, "reply_delivered", tool_call_id=tool_call_id, reply=reply, was_deferred=False, wait_ms=0)

def drain_deferred(repos: Repositories, call_id: str) -> None:
    items = DEFERRED.pop(call_id, [])
    current_stage = CALL_STATES.get(call_id, {}).get("stage")
    for tool_call_id, reply, dispatch_stage, queued_at in items:
        if dispatch_stage != current_stage:
            logger.debug("dropping stale deferred reply call_id=%s tool_call_id=%s", call_id, tool_call_id)
            repos.trace.record_event(call_id, "reply_dropped_stale", tool_call_id=tool_call_id,
                                      dispatch_stage=dispatch_stage, current_stage=current_stage)
            continue
        send_over_bridge(call_id, tool_call_id, reply)
        repos.trace.record_event(call_id, "reply_delivered", tool_call_id=tool_call_id, reply=reply,
                                  was_deferred=True, wait_ms=int((time.monotonic()-queued_at)*1000))

def send_over_bridge(call_id: str, tool_call_id: str, reply: str) -> None:
    # look up CONNECTIONS[call_id], send {"type": "supervisor_result", ...}
    ...
```

### `client/app.js` additions
- On the OpenAI data channel, also relay `input_audio_buffer.speech_started` / `speech_stopped` events (confirm exact event names against current Realtime docs) to the backend over `/bridge` as `{"type": "speech_started"}` / `{"type": "speech_stopped"}`.

### How `repos` reaches node functions (retroactively applies to every node from Phase 2 onward)
LangGraph node functions are only ever called by the graph with `state` (and optionally a `config`) — not arbitrary extra positional arguments. Every node function shown across Phases 2–5 (`node_greeting`, `node_routing`, `node_capture`, `node_booking`, `node_escalation`) should actually be written as `def node_x(state: CallState, config: RunnableConfig) -> dict`, retrieving `repos = config["configurable"]["repos"]` at the top. The `GRAPH.invoke(state, config={"configurable": {"repos": repos}})` call above is what supplies it. Read every earlier node snippet that doesn't show this `config` parameter as abbreviated for readability, not as the literal final signature.

---

## Stretch (optional, only if core DoD below is already done) — dynamic per-stage turn-detection eagerness

Phase 1 turns on `semantic_vad` with a single fixed `eagerness` for the whole call — that's the required baseline. This stretch adjusts eagerness as the LangGraph stage changes, since different stages have different natural pause patterns (patient while a caller spells out an email, snappier during a quick yes/no confirm). See `docs/DECISIONS.md`.

### `backend/supervisor/turn_detection.py` (new file, stretch only)
```python
def stage_to_eagerness(stage: str) -> str:
    # Pure function, no I/O — easy to unit test regardless of whether
    # the rest of this stretch item gets built.
    return {
        "greeting": "auto",
        "routing": "auto",
        "capture": "low",       # patient — spelling out email/phone
        "booking": "auto",
        "escalation": "auto",
    }.get(stage, "auto")
```
Whenever `dispatcher.deliver_or_defer` sends a reply that changed `stage` (compare the pre- and post-invoke stage), also send `{"type": "set_eagerness", "eagerness": stage_to_eagerness(new_stage)}` over `/bridge`; `client/app.js` relays this as a `session.update` with the new `turn_detection.eagerness` value over the OpenAI data channel.

### Tests (stretch — only required if this is actually built)
- `backend/tests/test_turn_detection.py`: `test_stage_to_eagerness_capture_is_low`, `test_stage_to_eagerness_unknown_stage_defaults_auto`, `test_stage_to_eagerness_covers_all_five_defined_stages` (parametrized, asserts every value in `CallState`'s `stage` `Literal` has an explicit mapping rather than silently falling through to the default).

### If skipped
Note it explicitly in `docs/answers.md`'s Q2 answer and the README's limitations section as a deliberate scope cut, not an oversight — the fixed baseline from Phase 1 already satisfies the core requirement.

---

## Tests

### `backend/tests/test_heuristics.py`
1. `test_common_phrases_detected` — parametrized over `["can I speak to a person", "I want a real person", "get me a representative", "let me talk to a human"]`, all → `True`.
2. `test_unrelated_utterances_not_flagged` — parametrized over `["I need help with my lease", "my email is john at gmail", "yes that's correct"]`, all → `False`.
3. `test_case_insensitive` — `"SPEAK TO A PERSON"` → `True`.

### `backend/tests/test_escalation_node.py` (mock `generate_call_summary`, use `tmp_path` for handoff notes)
1. `test_escalation_node_ends_call` — assert `stage == "ended"`.
2. `test_writes_handoff_file_with_expected_fields` — assert `docs/handoffs/{call_id}.md` exists and contains the practice area, reason, and summary text.
3. `test_handoff_note_omits_unconfirmed_fields` — pre-seed `caller_profile.email.status = "pending_confirm"`; assert the written note shows "not captured" for email, not the tentative value.

### `backend/tests/test_routing_node.py` additions (extends Phase 3's file)
4. `test_multiple_areas_escalates_immediately_no_retry` — mock `classify_practice_area` → `{"area": "multiple_areas", ...}`; assert `stage == "escalation"`, `escalation_reason == "out_of_scope_multi_area"` on the **first** call (`retry_counts["classification"]` untouched — this path bypasses the retry counter entirely).

### `backend/tests/test_dispatcher_async.py` (pytest-asyncio — this is the most important file in this phase)
1. `test_on_bridge_message_returns_without_awaiting_graph` — mock `GRAPH.invoke` to `await asyncio.sleep(0.3)` before returning; assert `on_bridge_message` itself completes in well under 0.3s (e.g. `< 0.05s`), proving the WS handler never blocks on the graph call.
2. `test_result_delivered_immediately_when_not_speaking` — `SPEAKING[call_id] = False`; after the background task resolves, assert `send_over_bridge` was called (mock/spy it) rather than the result sitting in `DEFERRED`.
3. `test_result_deferred_when_speaking` — `SPEAKING[call_id] = True`; assert `send_over_bridge` was **not** called, and the result appears in `DEFERRED[call_id]`.
4. `test_deferred_result_delivered_on_speech_stopped` — queue a deferred item matching the current `stage`, then call `drain_deferred`; assert `send_over_bridge` is now called with that item.
5. `test_stale_deferred_result_dropped_on_speech_stopped` — queue a deferred item tagged with `dispatch_stage="capture"`, then mutate `CALL_STATES[call_id]["stage"]` to `"booking"` (simulating the conversation having moved on via a different completed turn), then call `drain_deferred`; assert `send_over_bridge` is **not** called for that item.
6. `test_concurrent_calls_for_same_call_id_serialize` — mock `GRAPH.invoke` to append its own call index to a shared list and sleep briefly before returning; fire two `process_supervisor_call` tasks for the same `call_id` back-to-back without awaiting the first; assert they run to completion in dispatch order (the shared list ends up `[1, 2]`, not interleaved/out of order) and the second task's input `state["transcript"]` includes the first task's appended turn (proves the lock prevents the lost-update race, not just call ordering).
7. `test_ended_call_short_circuits_without_invoking_graph` — pre-set `CALL_STATES[call_id]["stage"] = "ended"`; call `process_supervisor_call`; assert `GRAPH.invoke` was never called and the fallback message was delivered/deferred appropriately.
8. `test_immediate_delivery_records_reply_delivered_with_zero_wait` — `SPEAKING=False`; assert a `reply_delivered` trace event is recorded with `was_deferred=False`, `wait_ms=0`.
9. `test_deferred_then_delivered_records_nonzero_wait_ms` — queue a reply, wait a small controlled amount (or mock `time.monotonic`), then drain; assert the recorded `reply_delivered` event has `was_deferred=True` and `wait_ms` roughly matching the controlled delay — this is what makes the Phase 6a latency metric actually accurate, closing the gap the original design flagged as a caveat (measuring only node-compute time, not deferred-queue wait).
10. `test_dropped_stale_records_reply_dropped_stale_event` — assert the dropped case records a `reply_dropped_stale` trace event with both `dispatch_stage` and `current_stage`.
11. `test_unexpected_exception_in_graph_invoke_delivers_fallback_not_dead_air` — mock `GRAPH.invoke` to raise a plain `RuntimeError` (something NOT `LLMCallFailed` — an unrelated bug/failure); assert `process_supervisor_call` does not propagate the exception (the task completes cleanly), `deliver_or_defer` is called with the "something went wrong" fallback text, `state["stage"] == "ended"` and `escalation_reason == "system_error"` afterward, and `repos.calls.upsert` was called with that state.
12. `test_unexpected_exception_writes_handoff_note` — same setup as #11; assert `write_minimal_handoff_note` was called (mock/spy it) rather than `write_handoff_note`/`generate_call_summary` — confirms the catch-all doesn't try to make another Claude call after one just failed unexpectedly.
13. `test_unexpected_exception_records_trace_event` — same setup; assert an `unhandled_error` trace event was recorded with the exception message.
14. `test_exception_during_lock_hold_still_releases_lock` — mock `GRAPH.invoke` to raise inside the `async with get_lock(call_id)` block; assert a subsequent `process_supervisor_call` for the same `call_id` doesn't deadlock (the lock was released despite the exception — `async with` guarantees this, but worth a regression test given how much correctness in this file depends on the lock actually being released).

---

## Definition of Done

- [x] `pytest backend/tests/test_heuristics.py backend/tests/test_escalation_node.py backend/tests/test_routing_node.py backend/tests/test_dispatcher_async.py` — all pass, now rebased onto the merged Phase 4 (`node_booking` is the real implementation, not a stub). Full suite: `pytest backend/tests` — 131 passed (grew from 120 across this phase's live-testing fixes: `test_faq.py`, dispatcher greeting-chain tests, etc.).
- [x] Manual live call: say "can I just talk to a real person" as your very first utterance — confirm immediate escalation (no routing/capture questions asked first), and `docs/handoffs/<call_id>.md` is written. Confirmed — `call_id=c24fee5b-035e-4343-8297-8f357393c70c`: single `escalation` node entered directly, `escalation_reason=explicit_request`, handoff note written.
- [x] Manual live call: describe a genuinely multi-area issue (e.g. an employment dispute tangled with a visa status question) — confirm immediate escalation with `out_of_scope_multi_area`, not a retry loop. Confirmed live during this phase's testing (`classify_practice_area` → `area: "multiple_areas"` on the first call, `retry_counts` untouched, immediate escalation) — the specific `call_id` cited at the time (`3f8c7364-...`) didn't survive a later `calendar.db` reset and no handoff file for it was ever git-committed, so an independent DoD review (see this phase's final commits) couldn't re-verify it from artifacts. Re-confirmed as still-working behavior by the project owner rather than re-run through this session; automated coverage of the same logic (`test_multiple_areas_escalates_immediately_no_retry` in `test_routing_node.py`) remains green regardless.
- [x] **Manual live call — the core async requirement:** trigger a supervisor call (e.g. ask to book something), and immediately speak a follow-up question before the first reply comes back. Confirm: (a) the follow-up is heard and responded to without waiting for the first call to resolve, and (b) the first call's result is spoken only once there's a natural gap, not talked over the follow-up. Confirmed — `call_id=8b360bad-d2f3-4bb8-8f87-de5c138c8f4c`: `reply_deferred` (`reason: caller_speaking`) immediately followed by `reply_delivered` (`was_deferred: true, wait_ms: 15`) for the booking-confirmation reply, while the caller's overlapping "what's your address?" got answered first.
- [x] Manual: check the backend log during the above test — confirm you can see the first task's result being deferred (`SPEAKING=True`) and then delivered on the next `speech_stopped` event, not silently dropped or double-spoken. Confirmed via `trace_events` for the same call above (equivalent structured source to the raw log for this check).
- [x] Manual: temporarily make a deterministic tool raise on purpose (e.g. a one-line edit to `SlotRepository.book` to always raise `RuntimeError`), attempt a live booking, confirm you hear the graceful fallback and a clean escalation rather than dead air or a hung call — then revert the temporary edit. Confirmed — `call_id=63773aad-94ca-4226-a55b-e385a9ad8291`: `book_consultation` raised, `unhandled_error` trace event recorded, fallback reply delivered ("Sorry, something went wrong on my end..."), final state `outcome=escalated, escalation_reason=system_error, booking_slot_id=None`, handoff note written via `write_minimal_handoff_note`. Temporary edit reverted immediately after (confirmed clean `git diff`).
- [x] `docs/fixes/` or `docs/known-issues/` has an entry if anything about the async timing behaved unexpectedly during manual testing — don't let a flaky-but-passing manual check go unrecorded. See `docs/fixes/2026-08-21-004.md` (deferred-reply staleness), `docs/fixes/2026-08-22-001.md` (`node_greeting` silently discarding the caller's first real utterance — fixed via dispatcher-level chaining into the real node), and `docs/known-issues/2026-08-22-001.md` (a genuinely overlapping caller utterance can still get silently cancelled/dropped by `semantic_vad`'s `interrupt_response` before reaching `ask_supervisor` — accepted as a documented residual limitation after a client-side retry attempt proved worse, crashing the call outright; see `docs/DECISIONS.md`'s `interrupt_response` entry for the full back-and-forth).
- [x] `no_acceptable_slot` (set by the real `node_booking`) confirmed to flow into `node_escalation`'s handoff-note writing exactly like the other four reasons — `node_escalation` is reason-agnostic by design, so no extra wiring was needed; added `test_no_acceptable_slot_from_booking_flows_into_escalation_note` in `test_escalation_node.py` to prove the two nodes' hand-off (booking sets `stage`/`escalation_reason` on its exit turn, escalation writes the note on the *next* turn — same turn-based pattern every other escalation reason already uses).

**Note on how this phase was built**: started in `.claude/worktrees/phase-5-escalation-async` (branched from `master`, which had no Phase 4 commits yet — Phase 4 was still in-progress/uncommitted in the main checkout at the time) at the user's explicit request, to get ahead on Phase 5's independent pieces while Phase 4 finished elsewhere. Once Phase 4 merged into `master`, this branch was rebased onto it (two small conflicts in `dispatcher.py`/`tools.py`, resolved by keeping both sides — Phase 4's `repos.calls.upsert`/booking tooling and Phase 5's async rewrite/handoff-note writer; `graph.py` and `prompts.py` merged cleanly). Full suite verified green post-rebase (120 tests). Everything that doesn't require an actual human on a live mic/browser call is done: `heuristics.py`, the `multiple_areas` routing branch, the real `node_escalation` + handoff-note writing (including the `no_acceptable_slot` hand-off from booking), and the full async dispatcher rewrite (locks, `SPEAKING`/`DEFERRED`/`CONNECTIONS`, deferred delivery, staleness handling, the unhandled-exception catch-all, and cross-cutting.md section 2's disconnect cleanup). What's left is exclusively the manual live-call DoD items above — those need a person with a working mic to actually run them.
