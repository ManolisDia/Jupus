# Phase 2 — Supervisor Skeleton + Data

## Goal

Wire the full round trip — Realtime tool call → browser → `/bridge` WebSocket → LangGraph supervisor → reply → Realtime speaks it — using **stub node logic only** (no Claude calls yet, no real business logic). This proves the plumbing works in isolation from the harder logic in Phases 3–5. Also stands up the SQLite schema and seeds the calendar.

## Non-goals
- No Claude/Anthropic calls anywhere in this phase — every node returns canned/stub output. Phase 3 replaces the stubs with real extraction/classification.
- No confidence thresholds, no validation, no booking logic, no escalation triggers — the graph just walks greeting → routing → capture → booking in a fixed line for now.
- Dispatcher is **synchronous** in this phase (`await`s the graph call directly in the WebSocket handler). The fire-and-forget/deferred-delivery async version is Phase 5 — don't build it early, it needs the `SPEAKING` flag plumbing that isn't wired until then.

## Prerequisite
Phase 1 DoD fully met (reliable voice round-trip with zero tools). **Also read `docs/architecture.md` first** — this phase introduces the first persistence code, and it should be built directly as `backend/db/repositories/` (a `TraceRepository` for `trace_events`, a `CallRepository` for `calls`) rather than as bare `sqlite3` calls threaded through function parameters, even though the snippets below (written before that doc existed) show `conn: sqlite3.Connection` — read every such signature as the matching repository per `docs/architecture.md`.

---

## Files to create

### `backend/db/schema.sql`
Full schema up front (all three tables), even though `calls`/`eval_flags` aren't populated until later phases — one migration file, not scattered across phases.

```sql
CREATE TABLE slots (
    id INTEGER PRIMARY KEY,
    area TEXT NOT NULL,
    start_time TEXT NOT NULL,
    is_booked INTEGER DEFAULT 0
);

CREATE TABLE calls (
    call_id TEXT PRIMARY KEY,
    started_at TEXT,
    ended_at TEXT,
    practice_area TEXT,
    outcome TEXT,
    escalation_reason TEXT,
    caller_name TEXT,
    caller_email TEXT,
    caller_phone TEXT,
    booking_slot_id INTEGER,
    transcript_json TEXT
);

CREATE TABLE eval_flags (
    call_id TEXT PRIMARY KEY REFERENCES calls(call_id),
    flagged INTEGER,
    flag_reason TEXT,
    evaluated_at TEXT
);

-- See docs/phases/cross-cutting.md section 0 for full detail — this table
-- is the durable, ordered, complete record of everything that happens in
-- a call (tool calls, retries, stage transitions, delivery decisions),
-- distinct from and richer than the caller/agent-only `transcript` field
-- on CallState. Introduced here because dispatcher.py (this phase) is
-- where the first events need recording.
CREATE TABLE trace_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT REFERENCES calls(call_id),
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    node TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX idx_trace_events_call_seq ON trace_events(call_id, seq);
```
(`eval_flags` above is superseded by Phase 6b's richer schema — kept here only because Phase 2 needs *a* schema file to exist; Phase 6b replaces it with `call_error_flags`/`eval_runs`, see that phase's doc.)

### `backend/db/seed_slots.py`
- Connects to `settings.db_path`, executes `schema.sql` (idempotent: `DROP TABLE IF EXISTS` each table before `CREATE`, so re-running always produces a clean, predictable state — this is a dev-seed script, not a migration tool).
- Populates `slots`: weekdays only, 9:00am–5:00pm, 30-minute increments (16 slots/day), 10 business days out, for each of `["employment", "tenancy", "immigration"]`. Total rows = `10 days * 16 slots * 3 areas = 480`.
- After inserting, marks a deterministic subset as pre-booked so conflict-handling (Phase 4) has real data: mark every slot at exactly `10:00am` and `2:00pm` on the **first** business day for each area as `is_booked=1` (6 rows total). Deterministic, not random — required so tests can assert exact expected state.

### `backend/supervisor/state.py`
```python
class CallerProfile(TypedDict):
    name: Optional[str]
    name_confidence: float
    email: Optional[str]
    email_confidence: float
    email_validated: bool
    phone: Optional[str]
    phone_confidence: float
    phone_validated: bool
    preferred_slot: Optional[str]

class CallState(TypedDict):
    call_id: str
    stage: Literal["greeting", "routing", "capture", "booking", "escalation", "ended"]
    practice_area: Optional[Literal["employment", "tenancy", "immigration"]]
    caller_profile: CallerProfile
    transcript: Annotated[list[dict], operator.add]   # IMPORTANT: see note below
    retry_counts: dict[str, int]
    escalation_reason: Optional[str]
    booking_confirmed: bool
    pending_reply: Optional[str]
```

**Important LangGraph detail:** `transcript` must use `Annotated[list[dict], operator.add]` (or LangGraph's equivalent reducer syntax — confirm exact import path against the installed LangGraph version). Without a reducer, returning `{"transcript": [...]}` from a node *replaces* the whole list instead of appending to it, silently losing prior turns. This is exactly the kind of thing to verify with a dedicated test (see below), not assume.

Also in this file:
```python
def new_call_state(call_id: str) -> CallState: ...   # all fields at sensible empty defaults, stage="greeting"

CALL_STATES: dict[str, CallState] = {}   # in-memory, single-process — see docs/DECISIONS.md

def get_or_create_state(call_id: str) -> CallState: ...
```

### `backend/supervisor/graph.py`
```python
def route_by_stage(state: CallState) -> str:
    # dispatches to the node matching state["stage"]. "ended" should never
    # reach here — dispatcher.py guards against invoking the graph for an
    # ended call (see below) — but if it ever does, route to escalation
    # rather than raising, and log a known-issue-worthy warning.
    ...

def node_greeting(state: CallState) -> dict:
    # Phase 2 stub: no Claude call. Always transitions to "routing".
    # Every node (stub or real, this phase onward) calls record_event for
    # "node_entered" at the top and "node_exited" just before returning —
    # omitted from the remaining stub snippets below for brevity, but
    # required in the actual implementation of all five nodes.
    return {
        "stage": "routing",
        "pending_reply": "Thanks for calling — let me get you sorted.",
        "transcript": [{"role": "agent", "text": "...", "ts": now_iso()}],
    }

def node_routing(state: CallState) -> dict:
    # Phase 2 stub: hardcodes practice_area="employment" regardless of
    # input. Phase 3 replaces this with classify_practice_area.
    return {
        "stage": "capture",
        "practice_area": "employment",
        "pending_reply": "Got it — let me grab a few details.",
        "transcript": [...],
    }

def node_capture(state: CallState) -> dict:
    # Phase 2 stub: no extraction. Immediately transitions to booking.
    return {
        "stage": "booking",
        "pending_reply": "Thanks, when would you like to come in?",
        "transcript": [...],
    }

def node_booking(state: CallState) -> dict:
    # Phase 2 stub: no real availability check. Immediately "books".
    return {
        "stage": "ended",
        "booking_confirmed": True,
        "pending_reply": "You're all set. (stub)",
        "transcript": [...],
    }

def node_escalation(state: CallState) -> dict:
    # Reachable directly in tests even though nothing routes here
    # automatically yet in Phase 2.
    return {
        "stage": "ended",
        "pending_reply": "Let me get you to a person. (stub)",
        "transcript": [...],
    }

def build_graph():
    g = StateGraph(CallState)
    for name, fn in [("greeting", node_greeting), ("routing", node_routing),
                      ("capture", node_capture), ("booking", node_booking),
                      ("escalation", node_escalation)]:
        g.add_node(name, fn)
        g.add_edge(name, END)
    g.set_conditional_entry_point(route_by_stage, {
        "greeting": "greeting", "routing": "routing", "capture": "capture",
        "booking": "booking", "escalation": "escalation",
    })
    return g.compile()

GRAPH = build_graph()
```
Each `ask_supervisor` call runs exactly **one** node then stops (edges go straight to `END`) — this matches the real-world semantics where one Realtime tool call should produce one reply, not race through multiple stages silently in a single turn.

### `backend/supervisor/tools.py`, `backend/supervisor/prompts.py`
Create as empty modules with a module docstring noting they're populated in Phase 3+. Do not add placeholder functions that aren't called yet — nothing to import prematurely.

### `backend/supervisor/tracing.py` (new — see `docs/phases/cross-cutting.md` section 0 for full spec)
Implement `record_event`, `get_trace`, and `traced_call` now, even though there are no real tools to wrap yet — the stub nodes in this phase should still call `record_event` directly for `node_entered`/`node_exited` (there's no tool call to wrap with `traced_call` until Phase 3, but the stage-transition events are meaningful even for stub nodes and cheap to add now rather than retrofit).

### `backend/dispatcher.py` (Phase 2 version — synchronous)
```python
async def on_ask_supervisor(repos: Repositories, call_id: str, tool_call_id: str, reason: str, utterance: str) -> str:
    state = get_or_create_state(call_id)
    if state["stage"] == "ended":
        # Guard: Realtime should not call ask_supervisor after the graph
        # reached "ended", but don't crash if it does — log and return
        # a safe fallback.
        logger.warning("ask_supervisor called for ended call_id=%s", call_id)
        return "This call has already been completed."
    state["transcript"].append({"role": "caller", "text": utterance, "ts": now_iso()})
    repos.trace.record_event(call_id, "user_message", text=utterance)
    updated = GRAPH.invoke(state)
    CALL_STATES[call_id] = updated
    if updated["stage"] == "ended":
        repos.trace.record_event(call_id, "call_ended", outcome=derive_outcome_label(updated))
    return updated["pending_reply"]
```
`repos` here is the app-level `Repositories` bundle from `docs/architecture.md` (`repos.trace` is the `TraceRepository`) — `on_ask_supervisor` takes it as a parameter, it's never a global.
This is intentionally synchronous/blocking in Phase 2 — the async fire-and-forget behavior (background task, deferred delivery while caller is speaking, staleness checks) is built in Phase 5, once there's real node latency worth hiding. Building it now against stub nodes that return instantly would be untestable in any meaningful way.

### `backend/app.py` additions
```
WS /bridge?call_id=...
  loop:
    try:
      raw = await websocket.receive_text()
      msg = json.loads(raw)  # or FastAPI's receive_json()
      validate msg against the expected shape (a Pydantic model is fine
      here) — {"type": "ask_supervisor", "tool_call_id": str, "reason":
      str, "last_caller_utterance": str}
    except (json.JSONDecodeError, ValidationError):
      # malformed message from our own client shouldn't happen in
      # practice (it's code we control), but a parse/validation failure
      # here must not crash the receive loop and silently kill the
      # connection for the rest of the call — log it and continue
      # waiting for the next message instead.
      logger.warning("malformed /bridge message call_id=%s: %r", call_id, raw)
      continue
    except WebSocketDisconnect:
      mark_call_abandoned(repos, call_id)  # see docs/phases/cross-cutting.md section 2
      break
    reply = await dispatcher.on_ask_supervisor(call_id, msg.tool_call_id, msg.reason, msg.last_caller_utterance)
    await websocket.send_json({"type": "supervisor_result", "tool_call_id": msg.tool_call_id, "reply": reply})
```
(This is the Phase 2 synchronous version — Phase 5 replaces the body of the loop with `on_bridge_message`'s fire-and-forget dispatch, but the try/except structure around `receive_text`/`json.loads`/validation stays the same.)

### `client/app.js` additions
- On call start, also open `ws = new WebSocket("ws://localhost:{port}/bridge?call_id=" + call_id)`.
- `sendSessionUpdate()`'s `instructions` field is upgraded from Phase 1's bare "greet naturally" placeholder to the real text below — this is the single highest-leverage piece of prompt content in the whole system, since it's what actually determines whether Realtime defers to the supervisor or starts freelancing legal-sounding answers that never touch `ask_supervisor` — and therefore never get traced, logged, or evaluated by anything built in Phase 6. Get this wrong and the entire observability/eval stack silently sees nothing.

```
You are the phone-answering voice for a law firm. You have exactly one
capability beyond talking: a tool called ask_supervisor.

Rules, always:
1. Greet the caller warmly and ask what they need. You may handle
   greetings, small talk, and simple acknowledgments ("okay", "got it",
   "sounds good") yourself, without calling any tool.
2. The moment the caller describes a legal issue, asks about scheduling,
   gives you any personal detail (name, email, phone, a date or time), or
   asks anything you are not completely certain how to answer — call
   ask_supervisor. Do this every time, even if you think you already
   know the answer. Never state legal information, confirm a booking, or
   promise anything on your own — you do not have real information about
   the firm's calendar, policies, or legal positions; only
   ask_supervisor does.
3. If the caller asks to speak to a person, still call ask_supervisor —
   do not handle that yourself, and do not argue or try to talk them out
   of it.
4. Never narrate that you're checking, looking something up, or
   thinking — no "one moment," "let me check that for you," "just a
   second," or anything similar. Do not promise a follow-up you can't
   immediately deliver. If there's a brief pause before your next reply,
   that's natural and fine — a person doesn't announce every small pause
   either. When ask_supervisor returns, treat its reply as your next
   conversational turn and flow straight into it, the way a person
   continuing a conversation would — not as the payoff to an earlier
   promise.
5. When ask_supervisor returns a reply, speak it naturally in your own
   voice — you may lightly rephrase for tone, but never alter facts,
   names, dates, or numbers it gives you.
6. Never invent details about the firm, its lawyers, its fees, or the
   law itself. If you don't have an answer from ask_supervisor yet, say
   you'll check rather than guessing.
```

**Rule 4 was originally a filler-phrase allowance ("let me check that," "one moment") and was deliberately reversed** — live testing confirmed the model *could* speak an acknowledgment and call the tool in the same turn, but the actual experience was worse, not better: a spoken promise ("one moment") followed by dead air until the reply eventually arrives reads as more broken than a brief natural pause with no announcement at all. Rely on Phase 1's `semantic_vad` and Phase 5's non-blocking dispatch to keep any gap feeling human-paced; don't paper over it with a verbal placeholder. See `docs/DECISIONS.md`.

- `sendSessionUpdate()` now includes exactly one tool:
  ```json
  {
    "type": "function",
    "name": "ask_supervisor",
    "description": "Call this whenever the caller needs anything beyond simple greetings or small talk — routing, booking, detail capture, or escalation.",
    "parameters": {
      "type": "object",
      "properties": {
        "reason": {"type": "string"},
        "last_caller_utterance": {"type": "string"}
      },
      "required": ["reason", "last_caller_utterance"]
    }
  }
  ```
- `dataChannel.onmessage`: when the parsed event indicates a completed `ask_supervisor` function call (confirm the exact OpenAI Realtime event name/shape against current docs — this has been `response.function_call_arguments.done` historically, verify at implementation time), extract `tool_call_id` and the parsed arguments, send `{"type": "ask_supervisor", "tool_call_id": ..., "reason": ..., "last_caller_utterance": ...}` over `ws`.
- `ws.onmessage`: on `{"type": "supervisor_result", ...}`, send two events over the OpenAI data channel: a `conversation.item.create` with `type: "function_call_output"` matching `tool_call_id` and `output: reply`, followed by `response.create` — this is what makes Realtime actually speak the reply.
- `ws.onerror` / `ws.onclose`: call Phase 1's `teardown("error: lost connection to backend")` — the `/bridge` socket is just as much a hard dependency of the call as the WebRTC leg is, and losing it silently (backend restarted, crashed, or the connection dropped) must surface the same way any other connection failure does, not be a special case that leaves the UI thinking it's still connected while `ask_supervisor` calls quietly go nowhere.

---

## Tests

Also implement and pass `backend/tests/test_tracing.py` per `docs/phases/cross-cutting.md` section 0 — `record_event`/`get_trace`/`traced_call` are introduced in this phase, even though nothing calls `traced_call` for real yet.

### `backend/tests/test_graph_transitions.py`
1. `test_greeting_transitions_to_routing` — invoke `GRAPH` with a fresh `new_call_state`, assert returned `stage == "routing"`.
2. `test_routing_transitions_to_capture` — invoke with `stage="routing"`, assert `stage == "capture"` and `practice_area == "employment"` (the stub value).
3. `test_capture_transitions_to_booking` — invoke with `stage="capture"`, assert `stage == "booking"`.
4. `test_booking_transitions_to_ended` — invoke with `stage="booking"`, assert `stage == "ended"` and `booking_confirmed is True`.
5. `test_escalation_sets_stage_ended` — invoke with `stage="escalation"` directly, assert `stage == "ended"`.
6. `test_router_dispatches_to_correct_node_for_each_stage` — parametrized over all 5 non-`ended` stages, asserting `route_by_stage` returns the matching node name.
7. `test_transcript_accumulates_across_multiple_invocations` — invoke the graph twice in sequence, feeding the first call's output state as the second call's input (with a manually appended caller turn in between, as `dispatcher.py` does); assert `len(transcript)` after both calls equals the sum of turns added by both invocations, **not** just the last invocation's turns. This is the specific regression test for the `Annotated[..., operator.add]` reducer behavior called out above.

### `backend/tests/test_dispatcher.py` (Phase 2 subset — async hardening tests come in Phase 5)
1. `test_creates_new_state_for_unseen_call_id` — call `on_ask_supervisor` with a fresh `call_id`, assert `CALL_STATES` now contains it starting from `stage="greeting"` pre-invocation semantics (i.e., the first call runs the greeting node).
2. `test_reuses_existing_state_for_known_call_id` — call twice with the same `call_id`, assert the second call resumes from the stage the first call left off at (i.e., `routing`, not `greeting` again).
3. `test_guards_against_ended_call` — manually set a state's `stage` to `"ended"` in `CALL_STATES`, call `on_ask_supervisor` again for that `call_id`, assert it returns the fallback string and does **not** raise or re-invoke the graph.

### `backend/tests/test_seed_slots.py`
1. `test_seed_creates_expected_slot_count` — run the seed function against a temp DB, assert `SELECT COUNT(*) FROM slots` equals `480` (10 days × 16 slots/day × 3 areas).
2. `test_seed_marks_expected_slots_pre_booked` — assert exactly 6 rows have `is_booked=1`, matching the deterministic 10am/2pm-on-day-1 rule.
3. `test_seed_is_idempotent` — run the seed function twice in a row against the same DB file, assert the second run doesn't raise and the final row counts still match #1 and #2 exactly (proves the `DROP TABLE IF EXISTS` reset actually resets, rather than accumulating duplicate rows).

---

## Definition of Done

- [x] `python backend/db/seed_slots.py` runs clean, `sqlite3 backend/db/calendar.db "SELECT COUNT(*) FROM slots"` returns `480`.
- [x] `pytest backend/tests/test_graph_transitions.py backend/tests/test_dispatcher.py backend/tests/test_seed_slots.py` — all pass.
- [x] `session.update` sent by the client contains exactly one tool (`ask_supervisor`) with the schema above — confirm by inspecting the browser devtools network/data-channel log, not just by reading the source.
- [x] Manual: ask a substantive legal question live (e.g. "can my landlord evict me for this") — confirm the agent does **not** answer it directly. Since the stub nodes in this phase return canned text, "correct" behavior here is any visibly generic/stub-sounding reply coming back through `ask_supervisor`, not a confident freelanced legal answer. If it answers directly without triggering the tool at all, the instructions text needs tightening before Phase 3 — don't proceed with a supervisor that Realtime is routing around.
- [ ] Manual: after removing rule 4's filler-phrase allowance, confirm live that the agent no longer says "one moment"/"let me check" style acknowledgments, and that its next reply lands as a natural conversational continuation rather than dead air with no announcement at all — a silent pause is fine, a silent pause that *was* preceded by an unfulfilled verbal promise is what this change is fixing.
- [x] Manual live test: start a call, say anything (content doesn't matter, nodes are stubs) — confirm you hear, in order across successive utterances: the greeting stub reply, then the routing stub reply, then the capture stub reply, then "You're all set. (stub)" from booking — proving the full chain (Realtime → data channel → `/bridge` WS → dispatcher → graph → WS → data channel → spoken reply) works end to end.
- [x] Manual: backend log shows `CALL_STATES[call_id]["stage"]` progressing greeting → routing → capture → booking → ended across that same test call.
- [x] Manual: `SELECT event_type, node FROM trace_events WHERE call_id=? ORDER BY seq` for that same test call shows a `node_entered`/`node_exited` pair for each of the four stub stages plus a final `call_ended`.
- [x] Zero unhandled exceptions in the backend terminal or browser console across the full stub run-through.

Do not start Phase 3 until every stub transition above has actually been heard out loud in a live call — a passing pytest suite alone does not confirm the WebRTC/data-channel/WebSocket wiring is correct.
