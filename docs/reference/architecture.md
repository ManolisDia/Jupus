# Architecture

What the pieces are, how they fit, and which invariants you must not break.

---

## The one-sentence version

A LiveKit-hosted OpenAI Realtime session does all the *talking*; a Claude-backed LangGraph state machine does all the *deciding*; the two are joined by exactly one tool call, `ask_supervisor`.

---

## The two-vendor split

This is the central bet of the project, and most of the design follows from it.

| | OpenAI Realtime (`gpt-realtime-2.1`) | Claude (`claude-sonnet-5`, some tools on Haiku 4.5) |
|---|---|---|
| Owns | Speech-to-text, turn detection, voice, barge-in, phrasing | Routing, extraction, validation, booking, escalation, eval |
| Sees | Exactly one tool: `ask_supervisor` | Its own node's scoped inputs, one tool call at a time |
| Decides | *When* the caller has finished a sentence | *What* to do about what they said |
| Configured in | `backend/transport/prompts.py`, `backend/transport/livekit_agent.py` | `backend/supervisor/` |

Realtime is explicitly instructed never to make a business decision on its own (`SUPERVISOR_INSTRUCTIONS` in `backend/transport/prompts.py`). It may greet, acknowledge, and lightly rephrase for tone — nothing else. Every substantive turn goes through `ask_supervisor`.

**Why this matters operationally:** if Realtime ever answers a legal question itself instead of calling the tool, nothing downstream knows the turn happened. No trace event, no state transition, no eval signal. The observability story in [`tracing.md`](tracing.md) is only true because that prompt holds.

---

## The four layers

```
Transport         backend/app.py · backend/transport/ · client/ · admin/
                  thin; no business logic
                        │
Orchestration     backend/dispatcher.py · backend/supervisor/graph.py
                  async dispatch + the state machine; coordinates, never touches SQL
                        │
Domain / tools    backend/supervisor/tools.py (+ prompts, heuristics, faq, knowledge)
                  business logic and Claude calls; persists only through repositories
                        │
Data access       backend/db/repositories/
                  the ONLY layer that knows SQL or table names exist
```

Dependencies point downward only. `eval/` sits beside the stack and depends on `backend/`; **`backend/` must never import from `eval/` at module load time.** Two places need an `eval` symbol anyway, and both use a deliberate function-local import to keep the direction honest: `backend/db/repositories/sqlite_eval.py` (for `get_active_error_classes`) and several route handlers in `backend/app.py`.

---

## The hard rules, and what they mean when you are editing

These are stated as doctrine in [`CLAUDE.md`](../../CLAUDE.md). Here is what each one actually forbids in practice, and where it is enforced.

| # | Rule | In practice | Enforced by |
|---|---|---|---|
| 1 | Realtime sees exactly one tool | `ASK_SUPERVISOR_SCHEMA` is the only entry in the session's tool list. Do not add a second `@function_tool` to `JupusAgent`. | Review only |
| 2 | Graph edges are deterministic conditionals | No node may ask an LLM which node runs next. Thresholds, retry counts and completeness checks are plain `if`/`else`. | Review only |
| 3 | `validate_email` / `validate_phone` are plain code | Regex and digit counting. Never an LLM call. | Review only |
| 4 | Supervisor turns are async | `run_supervisor_turn` is awaited from the agent's tool; `GRAPH.invoke` runs off-loop via `asyncio.to_thread`. | Review only |
| 5 | Each node binds only its own tool subset | A node calls the tools its stage needs and no others. Do not widen "just in case". | Review only |
| 6 | No telephony, Docker, or extra hosting | Scope guard. | Review only |
| 7 | Claude only via `call_claude_tool` | Never `anthropic.Anthropic(...)` or `.messages.create(...)` outside `backend/supervisor/llm_utils.py`. | **Pre-commit** (`scripts/check_architecture.py`) |
| 8 | Every `tools.py` call goes through `traced_call` | Including the deterministic ones. `call_claude_tool` wraps `traced_call`; it does not replace it. | Review only |
| 9 | No SQL outside `backend/db/repositories/` | Never `import sqlite3` elsewhere. Pass a `Repositories` (or one repo) as a parameter. | **Pre-commit** (`scripts/check_architecture.py`) |

Two of nine are machine-checkable; the rest depend on review. `scripts/check_architecture.py` says as much in its own docstring. Do not mistake a green pre-commit run for a clean architecture check.

---

## Concurrency and threading — the part that is easy to get wrong

The system runs in **one process with three kinds of execution context**, and asyncio primitives do not move between them. This is the least obvious thing in the codebase.

```
┌─ FastAPI event loop (uvicorn)  ← captured as MAIN_LOOP
│    · HTTP routes, admin WebSockets
│    · every asyncio.Lock in dispatcher.LOCKS is bound here
│    · every background task (FIELD_VERIFICATIONS, STATUTE_SEARCHES) lives here
│
├─ LiveKit job context (a THREAD, with its OWN event loop)
│    · one per call; runs JupusAgent, the Realtime session, filler scheduling
│    · shares module globals (CALL_STATES, ...) but NOT loop-bound objects
│    · reaches the supervisor only via _on_main_loop()
│
└─ asyncio.to_thread worker threads
     · GRAPH.invoke runs here (its Claude calls are blocking SDK calls)
     · background verification / statute search run their blocking parts here
```

### The rules that follow

**1. `JobExecutorType.THREAD` is passed explicitly, never left to default.** LiveKit defaults to a subprocess on Linux/macOS and a thread on Windows. A subprocess would mutate a `CALL_STATES` in the wrong process — the backend would look fine and the admin panel would show nothing. See `build_server()` in `backend/transport/livekit_agent.py`.

**2. A thread job still gets its own event loop.** Module globals are genuinely shared; `asyncio.Lock` is not. A lock binds to the first loop that awaits it and raises if awaited from another. Therefore **every supervisor call is marshalled back onto the FastAPI loop** by `_on_main_loop()`. Anything you add that touches `dispatcher.get_lock()` from agent code must go through that helper.

**3. `GRAPH.invoke` is synchronous and blocking.** Its nodes make real Anthropic SDK calls with no internal `await`. It is run via `asyncio.to_thread` so the event loop stays free for turn detection and filler scheduling. Never call it directly from async code.

**4. One lock per call, held across the whole turn.** `dispatcher.get_lock(call_id)` serialises turns for a single call. Two overlapping utterances for the same call queue behind each other; different calls never contend. `mark_call_abandoned` takes the *same* lock — a disconnect landing mid-turn used to race the in-flight turn and silently overwrite the outcome (`docs/fixes/2026-08-24-003.md`).

**5. Background tasks never take the lock and never touch `CALL_STATES`.** `_verify_field_in_background` and `_search_statutes_in_background` are pure computations that *return* a result. Only the lock-holding turn merges results in, via `_reconcile_field_verifications` / `_reconcile_statute_search`. If a background task needed the lock, it would deadlock against a turn awaiting it while holding that lock. Both functions' docstrings say so; keep it true.

**6. Thread-locals in `llm_utils.py` are safe for one specific reason.** `_last_usage` and `_model_override` are `threading.local()`. They work because `call_claude_tool`'s invocation of `fn`, and any nested `call_claude_json`/`call_claude_text`, always run synchronously in the *same* OS thread (each `asyncio.to_thread` call owns one worker thread for its duration). Both are cleared immediately after use, so a recycled pool thread never sees a stale value. Do not make either of these paths async.

**7. Trace sequence numbers are guarded by a `threading.Lock`.** `SQLiteTraceRepository.record_event` is called from both worker threads and the loop thread. `seq` is derived from `MAX(seq)` *inside* the lock, and a `UNIQUE(call_id, seq)` index in the schema turns any bypass into a loud failure rather than silent trace corruption.

---

## Where state lives

There are two stores and they are not the same thing.

**`backend/supervisor/state.py::CALL_STATES`** — an in-memory `dict[call_id, CallState]`: the live conversation state. It is what the graph reads and writes, what the admin live-graph view reads, and what the caller's "captured details" panel is rendered from. It does not survive a process restart, and it is the reason the LiveKit worker runs in-process. Full field reference: [`call-state.md`](call-state.md).

**SQLite (`backend/db/calendar.db`)** — the durable record: the calendar, one row per call, the full trace, and everything eval writes. Written through repositories only. Full reference: [`data-layer.md`](data-layer.md).

`repos.calls.upsert(state)` runs at the end of every turn, so the DB row tracks the live state within one turn's latency. **The DB is a projection of `CallState`, never the reverse** — nothing ever loads a `CallState` back out of SQLite.

**Consequence:** in-memory state means no horizontal scaling, and two backends pointed at one LiveKit project will each hold half the calls with neither seeing the other's. The backend logs a loud startup warning about exactly this.

---

## The latency-hiding pattern

Four mechanisms attack perceived silence, and they attack different things. Three of them share one shape worth recognising, because you will meet it three times:

> A node sets a **signal field** on `CallState` → the dispatcher pops that field immediately after `GRAPH.invoke` returns and spawns a background task → a later turn's **reconcile** step merges the finished result in before the graph runs.

- **Optimistic capture (Phase 7).** `node_capture_fast` asks for the *next* field with zero Claude calls, signalling `background_verify_field`. `FIELD_VERIFICATIONS` does the real extraction concurrently. Corrections drain in a batched confirm phase at the end.
- **Case research (Phase 8).** `node_research_gather` asks a templated follow-up question, signalling `background_search_query`. `STATUTE_SEARCHES` runs BM25 plus a grounding call while the caller answers.
- **Fillers (Phase 14).** No signal field — this one is transport-level. `fillers.filler_for_state()` reads the *pre-turn* state to predict which of three call sites the turn will reach, and `ctx.with_filler` plays pre-rendered audio only once the session has been continuously idle for 400ms.

The signal fields are **explicit, not inferred**. An earlier version derived "should I spawn a verification?" from a before/after diff of `last_asked_field`, which fired on paths that had already processed the utterance synchronously — the stray task's result then landed on a later turn and overwrote a correct value. Both `dispatcher.py` and `graph.py` carry long comments about this; it is a real bug that reached live calls.

The fourth mechanism, Phase 13, is not a masking trick at all: it made the round trip genuinely shorter (merged calls, per-tool model choice, a root-caused retry tail). Only that one moved the actual number.

---

## Failure handling

The contract is: **an upstream API failure must never reach the caller as silence or a stack trace.**

1. `call_claude_tool` retries once after 500ms on `anthropic.APIError`, `json.JSONDecodeError`, or `StopIteration` — a truncated or malformed response is the same failure as an API error from the caller's point of view. A second failure raises `LLMCallFailed`.
2. Every node catches `LLMCallFailed` and calls `_llm_failure_fallback`, which speaks *"Sorry, I'm having a little trouble — could you say that again?"* and increments `consecutive_llm_failures`.
3. **Three consecutive failures escalate** with `escalation_reason="system_error"`. Any node that succeeds resets the counter to 0.
4. `run_supervisor_turn` wraps everything in a final `try/except`: an unhandled error records an `unhandled_error` trace event, ends the call, writes a minimal handoff note, and returns a graceful reply. **It never raises** — on the old fire-and-forget path a raised exception would have killed the task silently and left the caller hearing dead air.

---

## Related reading

- [`life-of-a-call.md`](life-of-a-call.md) — the same architecture, walked through in execution order
- [`../DECISIONS.md`](../DECISIONS.md) — *why* each non-obvious call was made, including several tried and reversed
- [`../architecture.md`](../architecture.md) — the original pre-implementation design note this reference supersedes
