# Life of a Call

The end-to-end path, in execution order, with the real function names. If you read one document before touching this codebase, make it this one.

---

## 0. Startup

`uvicorn backend.app:app` imports `backend/app.py`, which:

1. Builds `REPOS = get_repositories(settings)` at module level — one SQLite connection, six repository objects (`backend/db/repositories/__init__.py`).
2. Enters the `lifespan` context manager, which calls `start_agent_server(REPOS)` from `backend/transport/livekit_agent.py`.
3. `start_agent_server` captures the running loop as `MAIN_LOOP`, stashes `_REPOS`, builds an `AgentServer` and launches `server.run()` as a background task. The worker registers **outbound** with LiveKit Cloud and waits for jobs.

If `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` are unset, `start_agent_server` logs a warning and returns without starting. The backend still boots — admin and eval work fine — but no call can connect and `POST /livekit-token` returns a 503 saying exactly that.

> The worker registers with **automatic dispatch**, meaning it is a candidate for *every* room in the LiveKit project. A second backend running against the same credentials silently takes some of your calls. The startup log warns about this. See [`operations.md`](operations.md).

---

## 1. The browser starts a call

`client/index.html` loads the LiveKit UMD SDK from unpkg, then `config.js` (optional), `app.js`, `livekit-transport.js`.

On **Start Call** (`startCall` in `client/app.js`):

1. `callId = crypto.randomUUID()` — the call id is minted **client-side**.
2. `startLiveKitCall(callId)` in `client/livekit-transport.js` POSTs it to `/livekit-token`.
3. `backend/app.py::create_livekit_token` mints a LiveKit room JWT where **the room name is the call id**. That is how the id travels end to end with no side channel — the agent reads it back off the job, and every trace event, `CallState` and DB row keys off the same string.
4. The browser connects to the room and enables the microphone. The caller's browser never holds an OpenAI credential; the Realtime session is opened server-side.

---

## 2. The agent picks up

LiveKit dispatches a job. `entrypoint(ctx)` in `backend/transport/livekit_agent.py`:

1. `call_id = ctx.job.room.name` (read off the *job*, not `ctx.room`, which is not fully populated before `connect()`).
2. `await ctx.connect()`, then `build_session()` creates an `AgentSession` wrapping `openai.realtime.RealtimeModel` with the pinned config: model `gpt-realtime-2.1`, voice `marin` at speed 1.1, `near_field` noise reduction, transcription model `gpt-transcribe`, and `SemanticVad(eagerness="low", create_response=True, interrupt_response=True)`.
3. A `JupusAgent(call_id, repos, ctx.room)` is constructed — one instance per call, so per-call bookkeeping lives on `self` rather than in another global dict.
4. Four session event handlers are registered (see below), plus a shutdown callback.
5. `await session.start(agent=agent, room=ctx.room)`.

Realtime greets the caller **on its own**, from `SUPERVISOR_INSTRUCTIONS` rule 1, without calling any tool. No supervisor turn happens yet.

### The session event handlers

| Event | Handler does | Why |
|---|---|---|
| `user_state_changed` → `listening` | `agent.note_caller_stopped_speaking()` → `speech_stopped` trace event | Opens the `stt_and_dialogue_decision` latency stage. Lost its producer when `/bridge` was retired; re-created here. |
| `agent_state_changed` → `speaking` | `agent.note_agent_started_speaking()` → `first_audio` and `tts_first_audio` trace events | A **real playout** signal, better than the old amplitude heuristic. |
| `user_input_transcribed` (finals only) | `agent.note_transcript(text)` | The verbatim ASR transcript, waiting to be claimed by its turn. |
| `session_usage_updated` | stashes cumulative token totals | Emitted once at shutdown as `realtime_usage`. Cumulative, not per-delta — recording every update would multiply cost by the turn count. |

---

## 3. A turn: `ask_supervisor`

The caller says something substantive. Realtime decides to call the one tool it has. `JupusAgent.ask_supervisor` runs:

**a. Resolve what the caller actually said.** `_verbatim_utterance(raw_arguments)` prefers the real ASR transcript over the model's `last_caller_utterance` argument, waiting up to `TRANSCRIPT_WAIT_SECONDS` (1.5s) for it.

> This is load-bearing, not a nicety. `last_caller_utterance` is a string the Realtime model *generates*, and it invents: a caller said `"manos44"` and `extract_field` received `manos44@example.com`, with an `@` and a domain that were never spoken. That silently repairs exactly the malformed input the confidence pipeline exists to catch. Prompt-tightening was tried and was not reliable (`docs/DECISIONS.md`).

**b. Record `ask_supervisor_received`** with the tool call id, before any work, and start two clocks (`_turn_started_at` for perceived latency, `_reply_ready_at` set later for the Phase 11 boundary).

**c. Filler-interruption guard.** If the caller talked over a filler and `_consume_filler_interruption()` says so, `heuristics.looks_like_acknowledgment(utterance)` decides: a backchannel ("mhm", "okay") is dropped with `raise StopResponse()` and a `filler_interruption_ignored` event; anything substantive falls through and becomes a real turn. Closed token set, no LLM call — routing this through a model would reintroduce exactly the latency the filler exists to hide.

**d. Pick a filler.** `fillers.filler_for_state(CALL_STATES[call_id])` reads the **pre-turn** state and returns `"confirm_field"`, `"confirm_booking"`, `"propose_slot"`, or `None`. When non-`None`, the turn runs inside `ctx.with_filler(...)`, which fires line `[0]` only after 400ms of continuous idle and line `[1]` after a further 4s.

**e. Run the turn.** `_run_turn` awaits `_on_main_loop(run_supervisor_turn(repos, call_id, tool_call_id, utterance))` — marshalling onto the FastAPI loop, because everything past this point touches loop-bound locks.

**f. On return**, record `reply_ready` (round-trip end boundary), publish the `call_state` snapshot to the browser data channel, and **return the reply string**. LiveKit's own turn-taking decides when it is safe to speak it.

---

## 4. `run_supervisor_turn` — the supervisor turn

`backend/dispatcher.py`. Everything below happens under `async with get_lock(call_id)`.

```
1.  state = get_or_create_state(call_id)
2.  if state["stage"] == "ended": return "This call has already been completed."
3.  append the caller utterance to state["transcript"]
4.  if heuristics.is_explicit_human_request(utterance):
        stage = "escalation"; escalation_reason = "explicit_request"
5.  _reconcile_before_capture_turn(state, call_id)   # merge finished field verifications
6.  _reconcile_statute_search(state, call_id)        # merge a finished statute search
7.  stage_before = state["stage"]
8.  updated = await asyncio.to_thread(GRAPH.invoke, state, {"configurable": {"repos": repos}})
9.  if stage_before == "greeting" and updated["stage"] not in ("ended", "escalation"):
        updated = await asyncio.to_thread(GRAPH.invoke, updated, ...)   # chain once
10. if updated["background_verify_field"]:  spawn FIELD_VERIFICATIONS task; reset field to None
11. if updated["background_search_query"]:  spawn STATUTE_SEARCHES task;   reset field to None
12. faq_answer = match_faq(utterance)
    if faq_answer and updated["pending_reply"]: prepend the FAQ answer to the reply
13. CALL_STATES[call_id] = updated
14. repos.calls.upsert(updated)
15. if updated["stage"] == "ended": record call_ended with the derived outcome
16. return updated["pending_reply"], updated["stage"]
```

Four steps deserve attention:

**Step 4 — explicit escalation is checked before the graph runs**, against the raw utterance, from any stage. This is why "put me through to a person" works on the very first turn, before routing has ever run.

**Step 9 — the greeting chain.** `node_greeting` is a content-blind stub that only bumps the stage. The caller's first real utterance is already in this turn's transcript, so without this second invoke it would sit unprocessed until they spoke again — which is exactly the bug in `docs/fixes/2026-08-22-001.md`. Note that a graph invoke runs **exactly one node**; chaining is the dispatcher's job, not the graph's.

**Steps 10–11 — signal fields are popped, not diffed.** Only `node_capture_fast`'s "advance to next field" branch sets `background_verify_field`, and only `node_research_gather` sets `background_search_query`. They are reset to `None` rather than deleted, because LangGraph silently drops keys outside the declared schema.

**Step 12 — the FAQ check is unconditional**, whatever the node decided. A caller can tack a genuine side-question onto an otherwise-successful utterance ("...and are you open weekends?"), and no node-specific tool ever looks at anything but the part it cares about. This is the one place every reply passes through, so it is the one place a tangent can be caught regardless of which node ran.

---

## 5. Inside `GRAPH.invoke`

`route_by_stage(state)` picks exactly one node, every node has an edge straight to `END`, so **one invoke = one node = one reply**. Full per-node reference in [`supervisor-graph.md`](supervisor-graph.md).

Inside a node, every tool call goes through a wrapper:

- `traced_call(trace_repo, call_id, node, tool_name, fn, *args)` — emits `tool_call_start`, runs `fn`, emits `tool_call_end` with duration and success. Used for deterministic tools *and* repository calls.
- `call_claude_tool(...)` — builds on `traced_call`, adds retry, the `llm_usage` event, and the optional per-call `model=` override.

A node returns a **partial dict** of `CallState` keys. LangGraph merges it: `transcript` and `declined_slot_ids` are `Annotated[..., operator.add]` and therefore *append*; every other key *replaces*.

---

## 6. Back out to the caller

`_run_turn` records `reply_ready`, publishes the state snapshot on the `jupus.call_state` data topic, and returns the string. Realtime speaks it. When audio actually starts, `agent_state_changed → speaking` fires `first_audio` (whichever came first, filler or real reply) and `tts_first_audio` (supervisor answered → audible).

The browser renders the reply from the `lk.transcription` text stream, not from the tool result — `client/livekit-transport.js` registers a handler for that topic and appends turns to the transcript panel, showing a "…" bubble as soon as the caller's own final transcript lands.

---

## 7. The call ends

Two ways.

**Graceful.** A node sets `stage="ended"` (booking confirmed, or escalation complete). `run_supervisor_turn` records `call_ended` with the outcome from `derive_outcome_label`: `escalated` if there is an `escalation_reason`, else `booked` if `booking_confirmed`, else `info_only`.

**Disconnect.** The agent's shutdown callback emits the accumulated `realtime_usage` event (or logs a loud warning if there is none — silently recording nothing would look identical to a genuinely free call), then awaits `_on_main_loop(mark_call_abandoned(repos, call_id))`. That takes the same per-call lock and, if the call was not already `ended`, writes `outcome="abandoned"` and records `call_abandoned`.

---

## Worked example: a happy-path booking

Caller utterances in bold; what the system does underneath in between.

> *(Realtime greets, unprompted, no tool call)*

**"I need some help with my tenancy."**
`stage=greeting` → `node_greeting` bumps to `routing` → dispatcher chains → `node_routing` calls `classify_practice_area` → `{"area": "tenancy"}`. Sets `practice_area="tenancy"`, `stage="capture"`, `last_asked_field="name"`. Replies *"Got it — this falls under tenancy law. Let's start with your name..."*

**"Alex Smith"**
`route_by_stage` → `capture_fast` (because `capture_phase == "fast"`). The gates pass. Returns *"Great — and what's your email address?"* with **zero Claude calls**, and signals `background_verify_field="name"`. The dispatcher spawns the real `extract_field` for "name" in the background.

**"alex.smith@example.com"**
`capture_fast` again. Meanwhile the name verification has resolved and been merged in by `_reconcile_field_verifications`. Asks for the phone; signals a background verify for "email".

**"5551234567"**
Phone is the **last** field in `FIELD_PRIORITY`, so there is no next question to run concurrently with it — `_finish_fast_pass` processes it live: `extract_field`, `validate_phone`, then `generate_confirm_back` for the first field still pending. Sets `capture_phase="confirm"`. Replies *"Great, let me just quickly confirm a couple of things: ..."*

**"Yes, that's right."** / **"Yes."**
`route_by_stage` → `capture_confirm` → `node_capture`, draining pending confirmations in `FIELD_PRIORITY` order via `confirm_field_answer`. Email and phone are *always* confirmed back regardless of confidence; a wrong value there means the firm cannot reach the caller.

Once nothing is pending and nothing is missing, `_enter_research` sets `stage="research"`, `research_phase="gather"` and asks *"Before we get you booked in — can you tell me a bit more about what's been happening with your landlord?"* **in the same turn**.

**"My landlord is trying to evict me tomorrow without any notice."**
`node_research_gather`. Not a skip phrase, not a bare affirmation. Sets `research_phase="deliver"`, signals `background_search_query`, and replies with the templated filler question *"Got it — did they give you anything in writing?"* — again **zero Claude calls**, which is what buys the BM25 search plus grounding call a whole turn of wall-clock time.

**"No, nothing in writing."**
`node_research_deliver`. `_reconcile_statute_search` merged the result in just before the graph ran. If a citation was found, the reply is `spoken_framing` + the "general information, not legal advice" disclaimer + *"What day and time works for you?"*. If not, just the last part. Either way, `stage="booking"`.

**"Thursday afternoon"**
`node_booking`, no `offered_slots`, no `proposed_slot_id` → `extract_datetime` → `repos.slots.check_availability`. A hit becomes `_propose_slot` (a `generate_confirmation_summary` Claude call); a miss becomes `_offer_alternatives` (up to three slots, **deterministically formatted**, not an LLM call).

**"Yes, that works."**
`confirm_booking_answer` → accepted → `repos.slots.book(proposed_slot_id)`, an atomic `UPDATE ... WHERE is_booked = 0` that raises `SlotAlreadyBookedError` on a race rather than double-booking. Replies *"You're booked — you'll get a confirmation shortly."*, sets `stage="ended"`, `booking_confirmed=True`.

`repos.calls.upsert` writes `outcome="booked"` and `booking_slot_id`. `call_ended` is recorded. Done.

---

## What this produces

One `calls` row, one `slots` row flipped to `is_booked=1`, and roughly 40–80 `trace_events` rows — every node entry and exit, every tool call with arguments and duration, every retry, every token-usage record, and the latency boundaries. That trace is what the admin panel drills into and what the eval judge reads as evidence. See [`tracing.md`](tracing.md).
