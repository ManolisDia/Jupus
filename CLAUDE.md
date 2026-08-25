# CLAUDE.md — Jupus (Voice AI Take-Home)

## What is this?

Jupus is a take-home build for a Voice AI Engineer role: an inbound voice agent for a law firm. It answers calls, routes callers across employment/tenancy/immigration, captures booking details with confidence handling on noisy audio, books a consultation against a local calendar (handling conflicts), and escalates to a human when out of scope. It must run locally, cost the evaluator nothing, and must not be over-built.

This project is unrelated to Foundermatcha — do not reference or reuse Foundermatcha-specific code, only the architectural *patterns* (LangGraph, repository-style tool boundaries) that informed this design.

---

## Resuming work — start here, every session

This repo is worked on across many separate sessions with no memory of each other. **Git state, not conversation history, is the source of truth for what's done.** Before doing anything else:

1. `git log --oneline --all` and `git branch -a`. Three cases:
   - **No commits at all** (a brand new checkout of the scaffold): commit everything currently on disk to `master` first (`docs(scaffold): initial plan, architecture, and knowledge base` or similar — this is planning/config content, not a phase deliverable, so it's the one commit that doesn't belong to a phase branch). Then proceed to the next case.
   - **`master` has commits, no phase branch checked out**: `master`'s history tells you which phases are merged (`docs/PLAN.md`'s phase index gives the full ordered list — 1, 2, 3, 4, 5, 6a, 6b, 6c, 7, 8, 9, 10, 11, 12, 13, 14, 15; 9–14 are stretches beyond the original four required user stories, see `docs/PLAN.md`'s own note on why they're ordered the way they are). Create a branch for the next unmerged phase (`git checkout -b phase-N-<name>`, matching the doc's filename) and start it.
   - **A phase branch is already checked out**: you're mid-phase. Don't assume where it left off — read that phase's doc, then actually check what's implemented and which DoD items are genuinely satisfied (run the tests, check the files exist) before continuing. Claimed-done in a prior session's summary is not the same as verified-done.
2. Read that phase's doc in full (`docs/phases/phase-N-*.md`) before writing any code, per the reading order below.
3. Briefly state your understanding of the phase's goal and DoD back before starting — cheap insurance against a misreading, given how dense these specs are.
4. Before your first live-call test, confirm `.env` has real `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` values (not just present in `.env.example`) — if missing, stop and ask rather than guessing or working around it. Both are paid APIs; if you don't know whether spend caps/alerts are set on either account, ask before running a long test sequence.

## `docs/reference/` — how the code actually works today

`docs/reference/` is a developer handbook written *from the code on `master`*, not from a plan: the life of a call, every node and branch, every `CallState` field, the tool catalog, the schema, the API surface, the trace events, the eval pipeline, and per-task recipes. Start at `docs/reference/README.md`.

Read it when you need to understand or change existing behaviour. The planning docs below are still the authority on *why* things are the way they are and on what a given phase was supposed to deliver — but where a planning doc describes the code and disagrees with `docs/reference/`, the reference is the one that was checked against the code. Several planning docs describe designs that were later refined or reversed (the tool catalog in `docs/PLAN.md` and the file map at the bottom of this file are both stale; `docs/reference/code-map.md` is current).

## Before doing anything else: read `docs/architecture.md`, then `docs/PLAN.md`, then the current phase doc in `docs/phases/`

`docs/architecture.md` defines the four-layer shape (transport → orchestration → domain/tools → data access) and the repository pattern every piece of persistence code must follow — read it before Phase 2, since that's where the first `sqlite3` calls appear in the phase docs (written before this doc existed, so their `conn: sqlite3.Connection` signatures are illustrative of the operation, not the literal final signature — see `docs/architecture.md`'s last section for exactly how to read them).

`docs/PLAN.md` is the shared architecture reference and the phase index. Each phase has its own fully self-contained spec in `docs/phases/phase-N-*.md` (Phase 6 is split into 6a/6b/6c — see `docs/PLAN.md`'s phase index for the strictly forward-only dependency order between them) — exact function signatures, exact enumerated test cases, and a strict checkbox **Definition of Done**. Work one phase at a time, in order, reading that phase's doc in full before writing any code for it. Do not consider a phase complete until every item in its DoD is actually verified (tests pass, and any listed manual/live check succeeds) — don't move on based on "this should work." Where a phase doc refines something described in `docs/PLAN.md` (e.g. Phase 3's richer per-field state model, Phase 5's extra classification value), the phase doc is authoritative.

Also read `docs/phases/cross-cutting.md` before starting **Phase 2** — its section 0 (traces) is introduced there, since dispatcher.py and the graph both start in that phase. The rest of that doc (the upstream-API-failure wrapper, WebSocket-disconnect cleanup, and the automated 6-scenario regression suite) applies from Phase 3 onward. `docs/scenarios.md` defines those 6 scenarios as the single canonical source used by both manual and automated tests. `docs/error_taxonomy.md` defines the editable error-class registry the Phase 6b eval agent classifies calls against — read it before touching anything in `eval/`. `docs/benevolent_dictator.md` defines the single-human-annotator role: one designated person annotates calls via `/admin/annotate` whenever, their input is the strongest signal into taxonomy-critique suggestions, and they're the sole approver of any change to `eval/error_classes.py` — never let the LLM judge or its own self-critique auto-apply a taxonomy change.

`docs/DECISIONS.md` records *why* several non-obvious calls were made. Read it before changing any of the architecture below — if you think one of these decisions is wrong, flag it rather than quietly reversing it.

`docs/workflow.md` governs *how* work happens, not what to build: one branch per phase off `master`, commit far more often than "once the phase is done" (after every green test file, every DoD item checked off, every meaningful unit — never bundle unrelated changes), and an independent subagent review of the DoD checklist before merging a phase branch into `master`. Read it before starting Phase 1.

---

## Architecture doctrine — hard rules, not suggestions

1. **Realtime must never see more than one tool: `ask_supervisor`.** All business logic (routing, extraction, booking, escalation) lives behind that single dispatch call into the LangGraph supervisor. Do not add additional tools to the Realtime session config, even for "simple" things.
2. **Graph edges are deterministic conditionals on `CallState`.** Never let a node ask an LLM which node should run next. Confidence thresholds, retry counts, and field-completeness checks are plain Python `if/else`.
3. **`validate_email` / `validate_phone` are plain code** (regex/format checks). Never route these through an LLM call.
4. **Supervisor turns are async.** Never `await` a supervisor call inside a handler that also needs to keep receiving caller audio/VAD events. See `backend/dispatcher.py` and Phase 5 in the plan for the fire-and-forget + deferred-delivery + staleness-check pattern.
5. **Each LangGraph node binds only its own scoped tool subset** (see the tool catalog in `docs/PLAN.md`). Don't widen a node's available tools "just in case" — this is what keeps tool selection low-risk.
6. **No telephony, no Docker, no Railway hosting** unless every phase through Phase 8 is done ahead of schedule — see `docs/DECISIONS.md` for why.
7. **Every Claude API call inside a `tools.py` function goes through `call_claude_tool` in `backend/supervisor/llm_utils.py`, never the Anthropic SDK directly.** See `docs/phases/cross-cutting.md`. An unhandled upstream API failure must never propagate out of a node — it must become a graceful fallback reply, and 3 consecutive failures escalate with `escalation_reason="system_error"`.
8. **Every call to a `tools.py` function — deterministic or LLM-backed — goes through `traced_call` (`backend/supervisor/tracing.py`), never invoked directly.** This is what makes "every tool call is traced" true by construction. `call_claude_tool` builds on top of `traced_call`, it doesn't duplicate it. See `docs/phases/cross-cutting.md` section 0.
9. **No file outside `backend/db/repositories/` imports `sqlite3` or writes raw SQL.** `dispatcher.py`, `tools.py`, `tracing.py`, `eval/insights_agent.py`, and the admin routes all depend on the repository interfaces (`CallRepository`, `SlotRepository`, `TraceRepository`, `EscalationRepository`, `EvalRepository`, `AnnotationRepository`), never a connection object directly. See `docs/architecture.md`.

---

## Before debugging: check the knowledge base

Before deep-diving into any bug, grep `docs/fixes/` and `docs/known-issues/` for the symptom — a past session may already have solved it or ruled out dead ends.

- Fixes index: `docs/fixes/INDEX.md`, entries at `docs/fixes/YYYY-MM-DD-NNN.md`
- Known issues index: `docs/known-issues/INDEX.md`, entries at `docs/known-issues/YYYY-MM-DD-NNN.md`

After solving a non-trivial bug, add an entry to `docs/fixes/` and update its index. If you investigate something at length without solving it, add an entry to `docs/known-issues/` instead — symptom, what was ruled out, failed workarounds, current best hypothesis. If a known issue later gets solved, move the file into `docs/fixes/`, refresh its title, and remove it from `docs/known-issues/INDEX.md`.

---

## Run / test commands

```bash
# install deps
pip install -e ".[dev]"

# one-time: install the pre-commit hooks (see docs/workflow.md's
# Enforcement section) — every commit from here on is gated on
# pytest passing, the architecture-doctrine checks, and a secrets scan
pre-commit install

# run the backend (serves /session, /bridge, /admin, /api/*)
# redirected to backend.log (gitignored) so Claude can read logs itself — see the
# check-backend-logs skill below
uvicorn backend.app:app --reload > backend.log 2>&1

# seed the local calendar
python backend/db/seed_slots.py

# seed canned demo calls (for testing the eval agent without a live mic session)
python backend/db/seed_demo_calls.py

# run unit tests
pytest backend/tests

# run the eval / insights agent over logged calls (deterministic stats +
# error-taxonomy classification + taxonomy critique), tagged with a label
python eval/run_eval.py --label <name>

# drive the 6 canonical scenarios through the REAL (unmocked) pipeline —
# use this before/after a prompt-engineering change to build comparable batches
python eval/replay_scenarios.py --label baseline
python eval/replay_scenarios.py --label after-prompt-tweak

# diff per-error-class rates between two labeled runs
python eval/compare_runs.py --baseline baseline --candidate after-prompt-tweak

# compare the LLM judge's classifications against the Benevolent
# Dictator's human annotations (see docs/benevolent_dictator.md)
python eval/calibrate_judge.py
```

Caller client: open `client/index.html` in a browser (backend must be running).
Admin panel: open `http://localhost:8000/admin` (backend must be running).

---

## Debugging tools

- `.mcp.json` configures two MCP servers: `chrome-devtools` (inspect the caller client's console/network activity in `client/index.html` directly, instead of asking the user to paste devtools output) and `sqlite` (read-only inspection of `backend/db/calendar.db` for debugging only — app code must still go through the repository classes per rule #9 above, never query through this server).
- The `check-backend-logs` skill (`.claude/skills/check-backend-logs/`) tails `backend.log` and cross-references it against the `trace_events` table for a given `call_id` — use it instead of asking the user to paste terminal output when debugging a live call.

---

## File map

- `backend/app.py` — FastAPI: `POST /session` (ephemeral Realtime token), `WS /bridge` (tool-call bridge), `GET /admin` + `/api/calls[/{id}]` + `/api/eval/summary`
- `backend/dispatcher.py` — async supervisor dispatch, deferred delivery, staleness checks
- `backend/supervisor/` — `state.py` (CallState/CallerProfile), `graph.py` (LangGraph nodes/edges), `tools.py` (tool implementations), `prompts.py` (per-node system prompts)
- `backend/db/` — `schema.sql`, `seed_slots.py`, `seed_demo_calls.py`, `calendar.db` (gitignored)
- `backend/tests/` — pytest suite, no live API calls required
- `client/` — caller-facing browser page (WebRTC + mic)
- `admin/` — calls list with error-class badges/transcript drill-in/eval summary/taxonomy-suggestion approval, plus `annotate.html` — the Benevolent Dictator's dedicated annotation page
- `eval/` — `error_classes.py` (the editable taxonomy registry), `insights_agent.py`, `run_eval.py`, `replay_scenarios.py`, `compare_runs.py`, `calibrate_judge.py`
- `docs/` — `PLAN.md`, `architecture.md`, `DECISIONS.md`, `workflow.md`, `diagrams.md`, `error_taxonomy.md`, `benevolent_dictator.md`, `scenarios.md`, `answers.md` (the 4 required written answers), `handoffs/` (one file per escalated call — a readable view of the `escalations` table, not the system of record), `phases/` (per-phase specs + `cross-cutting.md`), `fixes/`, `known-issues/`
