# Jupus — Build Plan

## Context

Voice AI Engineer take-home: a working voice agent prototype for a law firm inbound line. Must route callers across at least two (we're doing three: employment, tenancy, immigration) areas of law, capture booking details with confidence handling on a noisy line, book a consultation against a local calendar (handling conflicts), and escalate to a human when out of scope. Must run locally on a normal laptop at no cost to the evaluator, and must not be over-built. Submission = code + README + Loom video + written answers to 4 architecture questions (see `docs/answers.md`).

Read `CLAUDE.md` first for the hard architecture rules, then `docs/architecture.md` for the layered/repository-pattern shape everything else is built on. Read `docs/DECISIONS.md` for why the other rules exist. `docs/diagrams.md` has visual versions of the architecture, call sequence, state machine, and eval data flow — useful for presenting this to someone rather than building it. This file is the phase-gated execution plan — work one phase at a time, in order, and don't consider a phase done until its Definition of Done is actually verified.

**Locked decisions:** OpenAI Realtime API (WebRTC, browser client) for conversation; Claude via LangGraph for the supervisor; SQLite for calendar + call logs; no Docker, no telephony, no Railway hosting unless every phase below is done ahead of schedule.

---

## Architecture — full call sequence

```
1. Browser loads client/index.html
2. Browser generates a call_id (crypto.randomUUID()) and
   POSTs /session {call_id} → backend calls OpenAI's realtime session-create
   REST endpoint with the server-held API key, gets an ephemeral
   client_secret (short TTL), returns it to the browser.
3. Browser opens a WebRTC PeerConnection directly to OpenAI Realtime,
   authenticating with the ephemeral secret. Attaches the local mic
   track, opens a data channel ("oai-events") for JSON control/tool events.
4. Browser ALSO opens a plain WebSocket to the backend: WS /bridge?call_id=...
   (our own channel, unrelated to OpenAI — used only to hand tool-call
   work to the Python supervisor).
5. On connect, the browser sends session.update over the OpenAI data
   channel: short system instructions (greet naturally, call
   ask_supervisor whenever the caller needs anything beyond small talk),
   a voice, and the single tool schema for ask_supervisor. The opening
   greeting itself is scripted directly in these instructions — it does
   NOT require a supervisor round-trip.
6. Caller talks. Realtime does STT+VAD+dialogue itself. For turns that
   need business logic, the model emits a
   response.function_call_arguments.done event for "ask_supervisor"
   with args {reason, last_caller_utterance}.
7. Browser JS sees this on the data channel → forwards
   {call_id, tool_call_id, reason, utterance} over the /bridge
   WebSocket to the backend.
8. Backend dispatcher.py receives it, spawns an asyncio.Task running
   the LangGraph supervisor for that call_id — returns immediately,
   does NOT block the WebSocket or the browser.
9. Caller may keep talking during this window — Realtime keeps
   listening/responding to those turns independently; the browser
   keeps relaying any further ask_supervisor calls the same way
   (multiple in-flight supervisor tasks per call are fine — each
   carries its own tool_call_id).
10. When the task resolves, the dispatcher checks a shared per-call
    "caller_speaking" flag (updated from relayed
    input_audio_buffer.speech_started / speech_stopped events) — if
    the caller is mid-speech, the result is queued; otherwise it's
    pushed immediately down the /bridge socket.
11. Browser receives the result, sends conversation.item.create
    (type: function_call_output, matching tool_call_id) then
    response.create over the OpenAI data channel — Realtime speaks
    the result as part of its next turn.
12. Every ask_supervisor round trip and its result is written to the
    calls table (transcript_json) in SQLite for the eval agent and
    admin panel. Separately, every tool call, retry, stage transition,
    and delivery decision along the way is recorded as a durable, ordered
    trace_events row (docs/phases/cross-cutting.md section 0) — the
    transcript is the lightweight conversational history, the trace is
    the complete record everything else (debugging, the admin panel's
    detail view, the eval judge) is built on.
```

---

## LangGraph supervisor — state, nodes, edges

### State (`backend/supervisor/state.py`)

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
    transcript: list[dict]          # rolling {role, text, ts}
    retry_counts: dict[str, int]    # per-field reprompt counter
    escalation_reason: Optional[str]
    booking_confirmed: bool
    pending_reply: Optional[str]    # text handed back to Realtime
```

State persists per `call_id` in an in-memory dict on the backend — fine for a single-process local prototype, no external store needed.

### Nodes (`backend/supervisor/graph.py`)

1. **greeting** — first `ask_supervisor` of a call once the caller has stated something. No tools; one Claude call to interpret intent (info vs booking), sets `stage="routing"`.

2. **routing** — binds `classify_practice_area` (Claude, forced tool_choice, schema restricted to the 3 areas + "unclear"). On "unclear" after 2 attempts → `escalation`, `reason="unable_to_classify"`. Otherwise sets `practice_area`, moves to `capture`.

3. **capture** — binds `update_caller_profile` (Claude extraction, one field at a time driven by which fields are still `None`). Immediately after email/phone extraction, code (not LLM) runs `validate_email`/`validate_phone`. Threshold logic (plain code):
   - `confidence >= 0.75` → accept, move to next missing field
   - `0.4 <= confidence < 0.75` → generate a confirm-back prompt via a short Claude call, increment `retry_counts[field]`
   - `confidence < 0.4` OR `retry_counts[field] >= 3` → `escalation`, `reason="capture_failed"`
   - all required fields present + confirmed → `booking`

4. **booking** — binds `check_availability`, `suggest_alternative_slots`, `book_consultation` (all deterministic code against SQLite). Flow is code-driven:
   - extract preferred day/time (Claude) → `check_availability`
   - free → confirm full details back (Claude-generated confirmation sentence) → caller "yes" → `book_consultation` → `stage="ended"`
   - taken → `suggest_alternative_slots` → repeat confirm loop with new slot
   - caller declines twice → `escalation`, `reason="no_acceptable_slot"`

5. **escalation** — binds `escalate_to_human` only. Writes the handoff note, sets `stage="ended"`, returns a graceful closing line.

### Edges
Plain `if/else` on `CallState` — never an LLM decision about which node runs next (see `CLAUDE.md` rule 2 / `docs/DECISIONS.md`).

---

## Tool catalog

| Tool | Node | Impl | Signature |
|---|---|---|---|
| `ask_supervisor` | Realtime → backend (only tool Realtime sees) | dispatch | `{reason: string, last_caller_utterance: string}` |
| `classify_practice_area` | routing | Claude, forced choice | `{area: "employment"\|"tenancy"\|"immigration"\|"unclear", confidence: float}` |
| `update_caller_profile` | capture | Claude extraction | `{field: "name"\|"email"\|"phone"\|"preferred_time", value: string, confidence: float}` |
| `validate_email` | capture (code) | regex/format | `(email: str) -> bool` |
| `validate_phone` | capture (code) | format check | `(phone: str) -> bool` |
| `check_availability` | booking (code) | SQLite query | `(date: str, window: str, area: str) -> slot \| None` |
| `suggest_alternative_slots` | booking (code) | SQLite query | `(date: str, area: str) -> list[slot]` |
| `book_consultation` | booking (code) | SQLite insert | `(slot_id, caller_profile) -> booking_id` |
| `escalate_to_human` | escalation | Claude summary + code write | `{reason: "unable_to_classify"\|"capture_failed"\|"no_acceptable_slot"\|"out_of_scope_multi_area"\|"explicit_request"\|"system_error", call_summary: string}` — `system_error` added in `docs/phases/cross-cutting.md` |
| `lookup_practice_area_info` (stretch only) | routing/capture info path | local JSON KB, Claude grounds | `(area) -> text` |

---

## Database schema (`backend/db/schema.sql`)

```sql
CREATE TABLE slots (
    id INTEGER PRIMARY KEY,
    area TEXT NOT NULL,               -- employment | tenancy | immigration
    start_time TEXT NOT NULL,         -- ISO datetime
    is_booked INTEGER DEFAULT 0
);

CREATE TABLE calls (
    call_id TEXT PRIMARY KEY,
    started_at TEXT,
    ended_at TEXT,
    practice_area TEXT,
    outcome TEXT,                     -- booked | escalated | info_only | abandoned
    escalation_reason TEXT,
    caller_name TEXT,
    caller_email TEXT,
    caller_phone TEXT,
    booking_slot_id INTEGER,
    transcript_json TEXT              -- full turn-by-turn log incl. tool calls + latencies
);

```

`seed_slots.py` populates ~2 weeks of half-hour slots, 9am–5pm weekdays, for all 3 areas, with a handful pre-marked `is_booked=1` so conflict handling has real data to hit.

**Eval schema (`call_error_flags`, `eval_runs` from Phase 6b; `taxonomy_suggestions`, `call_reviews`, `human_annotations` from Phase 6c) is defined and owned by `docs/phases/phase-6b-error-taxonomy.md`, `docs/phases/phase-6c-benevolent-dictator.md`, and `docs/error_taxonomy.md`, not here** — the eval agent classifies calls against an editable error taxonomy rather than a single flagged/not-flagged bit, see those docs for the full design.

---

## Async dispatcher (`backend/dispatcher.py`)

- `PENDING: dict[str, asyncio.Task]` keyed by `tool_call_id`.
- `SPEAKING: dict[call_id, bool]` updated from relayed `speech_started`/`speech_stopped` events.
- `on_ask_supervisor(call_id, tool_call_id, payload)` → `asyncio.create_task(run_supervisor_turn(...))`, stores it, returns immediately (handler never awaits it).
- `run_supervisor_turn` invokes the graph, gets `pending_reply` + updated state, calls `try_deliver`.
- `try_deliver`: if `SPEAKING[call_id]` → push onto `DEFERRED[call_id]` queue; else send over `/bridge` now.
- On every relayed `speech_stopped` → drain `DEFERRED[call_id]` oldest-first, re-checking staleness (does the queued reply's `practice_area`/`stage` still match current state — if the caller changed topic while it was pending, drop it rather than speak a stale answer).
- Plain asyncio, single process — no external broker needed.

---

## Eval / insights agent — see `docs/error_taxonomy.md`, `docs/benevolent_dictator.md`, and `docs/phases/phase-6a-observability.md` / `phase-6b-error-taxonomy.md` / `phase-6c-benevolent-dictator.md`

Summary (full design lives in those docs, not duplicated here): the judge classifies each call against an editable error taxonomy (seed classes: `repetition`, `tool_or_system_failure_surfaced`, `premature_escalation`, `unconfirmed_action`) rather than a single flagged/not-flagged bit, plus a separate batch-level pass that critiques the taxonomy itself (new/misclassified/refine suggestions, `pending` until a human approves them). That human is a single designated **Benevolent Dictator** — one domain expert whose annotations (via a dedicated `/admin/annotate` page, available any time, not gated to eval runs) are both the strongest input to taxonomy-critique suggestions and the ground truth `eval/calibrate_judge.py` measures the LLM judge against. `eval/run_eval.py --label <name>` runs the deterministic + classification + critique passes and tags the batch; `eval/replay_scenarios.py --label <name>` drives the 6 canonical scenarios (`docs/scenarios.md`) through the real, unmocked pipeline to generate a fresh comparable batch after a prompt-engineering change; `eval/compare_runs.py --baseline <a> --candidate <b>` diffs per-class error rates between two labeled runs to catch regressions. This whole area was split across three sub-phases (6a observability, 6b taxonomy/judge, 6c BD/regression) since it had grown into by far the largest single phase — see `docs/PLAN.md`'s phase index below for the dependency order.

---

## Admin panel

A few extra routes on the same FastAPI backend — not a separate app, no auth (local-only).

- `GET /admin` — HTML shell, vanilla JS
- `GET /api/calls` — list: call_id, started_at, practice_area, outcome, escalation_reason, booking_slot_id
- `GET /api/calls/{call_id}` — drill-in: turn-by-turn transcript with tool-call annotations + latency, plus `eval_flags` row if present
- `GET /api/eval/summary` — deterministic-pass aggregates

---

## Repo layout

```
Jupus/
  README.md
  CLAUDE.md
  .env.example
  .gitignore
  .pre-commit-config.yaml           # see docs/workflow.md's Enforcement section
  pyproject.toml
  scripts/
    check_architecture.py           # CLAUDE.md rules 7 & 9, mechanically checkable subset
    check_no_secrets.py
  backend/
    app.py                            # POST /session, WS /bridge, GET /admin, /api/*
    dispatcher.py
    supervisor/
      state.py
      graph.py
      tools.py
      prompts.py
      tracing.py                      # trace_events recording — see cross-cutting.md
      llm_utils.py                    # call_claude_tool wrapper — see cross-cutting.md
    db/
      schema.sql
      seed_slots.py
      seed_demo_calls.py
      repositories/                   # see docs/architecture.md — the ONLY
        __init__.py                   # place allowed to know SQL/table names
        base.py                       # exist; ABCs + one SQLite impl each
        sqlite_calls.py
        sqlite_slots.py
        sqlite_trace.py
        sqlite_eval.py
        sqlite_annotations.py
      calendar.db                     # gitignored
    tests/
      test_graph_transitions.py
      test_validators.py
      test_dispatcher.py
  client/
    index.html
    app.js
  admin/
    index.html
    app.js
  eval/
    error_classes.py                  # editable error taxonomy registry
    insights_agent.py
    run_eval.py
    replay_scenarios.py               # live-Claude regression batch generator
    compare_runs.py                   # diffs error rates between two labeled runs
    calibrate_judge.py                # LLM judge vs Benevolent Dictator agreement
    tests/
  admin/
    index.html / app.js               # calls list, drill-in, eval summary, taxonomy suggestions
    annotate.html / annotate.js       # Benevolent Dictator annotation page
  docs/
    PLAN.md                           # this file
    DECISIONS.md
    error_taxonomy.md                 # error class design + evolution process
    benevolent_dictator.md            # the human annotation/calibration role & process
    scenarios.md                      # the 6 canonical test scenarios (S1-S6)
    answers.md                        # 4 required written answers
    handoffs/                         # one .md per escalated call
    phases/
      cross-cutting.md
      phase-1-raw-voice-loop.md ... phase-8-polish-submission.md
    fixes/
      INDEX.md
    known-issues/
      INDEX.md
```

---

## Phases — see `docs/phases/`

The architecture above is the shared reference every phase builds against. Each phase now has its own fully self-contained spec doc — exact function signatures, exact enumerated test cases, and a strict checkbox Definition of Done. **Read the phase doc, not just this summary, before starting a phase.** Where a phase doc refines something shown above (e.g. Phase 3 replaces the flat `CallerProfile` with a richer per-field model, Phase 5 adds a 5th value to `classify_practice_area`'s schema), the phase doc is authoritative — this file stays as the high-level map.

Work in order. Do not start a phase until the previous one's DoD is verified — a passing test suite alone does not count for phases with a live-call manual check listed.

| Phase | Doc | Summary |
|---|---|---|
| 0 | ✅ done | Repo scaffold — `CLAUDE.md`, this file, `DECISIONS.md`, fixes/known-issues indices, config files |
| 1 | [`phases/phase-1-raw-voice-loop.md`](phases/phase-1-raw-voice-loop.md) | Prove the WebRTC audio round-trip works, zero tools |
| 2 | [`phases/phase-2-supervisor-skeleton.md`](phases/phase-2-supervisor-skeleton.md) | Wire `ask_supervisor` → `/bridge` → LangGraph with stub nodes; seed the calendar |
| 3 | [`phases/phase-3-routing-capture.md`](phases/phase-3-routing-capture.md) | Real routing + single-field capture with confidence thresholds (stories 1 & 3) |
| 4 | [`phases/phase-4-booking.md`](phases/phase-4-booking.md) | Real booking against SQLite, conflict handling, call persistence (story 2) |
| 5 | [`phases/phase-5-escalation-async.md`](phases/phase-5-escalation-async.md) | Real escalation + handoff notes; the full async fire-and-forget dispatcher (story 4) |
| 6a | [`phases/phase-6a-observability.md`](phases/phase-6a-observability.md) | Deterministic metrics, base admin panel (list/drill-in/trace viewer), closes out `cross-cutting.md` (error handling, disconnect cleanup, mocked scenario suite). Depends only on Phases 1–5. |
| 6b | [`phases/phase-6b-error-taxonomy.md`](phases/phase-6b-error-taxonomy.md) | Error taxonomy registry, LLM-judge classification pass, `run_eval.py`. Depends on 6a only. |
| 6c | [`phases/phase-6c-benevolent-dictator.md`](phases/phase-6c-benevolent-dictator.md) | BD annotation page, taxonomy critique + approval, judge calibration, live-Claude regression harness. Depends on 6a and 6b. |
| 7 | [`phases/phase-7-optimistic-capture.md`](phases/phase-7-optimistic-capture.md) | Decouple field-capture latency from Claude round-trips: a fast deterministic sequencer asks the next field instantly, real verification runs in the background, corrections drain in a batched confirm phase |
| 8 | [`phases/phase-8-polish-submission.md`](phases/phase-8-polish-submission.md) | README, written answers, full regression pass, video |

Phase 6 was originally one phase but split into 6a/6b/6c — by far the largest single phase otherwise, and several of its pieces don't actually depend on each other (you can see call traces in the admin panel long before the taxonomy/judge machinery exists). The dependency order is strictly forward: 6b depends on 6a, 6c depends on 6a+6b, and neither 6a nor 6b ever depends on something built later.

`docs/phases/cross-cutting.md` also applies retroactively from Phase 3 onward (every Claude-backed tool function must use its `call_claude_tool` wrapper from the moment it's written) even though its own Definition of Done is verified at Phase 6a — read it before Phase 3, not after. `docs/scenarios.md` defines the 6 canonical test scenarios used by both manual DoD checks, Phase 6a's automated mocked-regression suite, and Phase 6c's live-pipeline regression harness.
