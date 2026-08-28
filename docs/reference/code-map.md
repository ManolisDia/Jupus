# Code Map

Every source file, what it owns, and when you would open it.

Roughly 16,600 lines across `backend/`, `eval/`, `client/`, `admin/` and `scripts/`. The largest file by far is `backend/supervisor/graph.py` (1,218 lines) — that is where the conversation logic lives, and it is worth reading in full before changing any of it.

---

## `backend/` — the server

### Top level

| File | Owns | Open it when |
|---|---|---|
| `app.py` (483) | FastAPI app: the LiveKit token route, all `/api/*` admin routes, two admin WebSockets, the access gate, CORS, the static `/admin` mount, and the `lifespan` that starts and stops the agent worker. | Adding an endpoint; changing auth; changing startup. See [`api.md`](api.md). |
| `dispatcher.py` (423) | The supervisor turn: per-call locking, background-task registries, reconcile steps, the FAQ hook, persistence, disconnect cleanup. | Anything about turn sequencing, background verification, or how a call ends. |
| `config.py` (43) | `Settings` — every env var, via `pydantic-settings`. A module-level `settings` singleton. | Adding configuration. See [`operations.md`](operations.md). |
| `utils.py` (4) | `now_iso()`. That is the whole file. | Never. |

### `backend/supervisor/` — the state machine

| File | Owns | Open it when |
|---|---|---|
| `graph.py` (1218) | All eight nodes, `route_by_stage`, every branch and escalation trigger, the reply strings, the thresholds. | Changing conversation behaviour. Reference: [`supervisor-graph.md`](supervisor-graph.md). |
| `state.py` (133) | `CallState`, `CallerProfile`, `FieldCapture`, `FIELD_PRIORITY`, `new_call_state`, the `CALL_STATES` global. | Adding a state field. Reference: [`call-state.md`](call-state.md). |
| `tools.py` (454) | Every tool implementation and its JSON schema — LLM-backed and deterministic alike. | Adding or changing a tool. Reference: [`tool-catalog.md`](tool-catalog.md). |
| `prompts.py` (202) | One system prompt per Claude-backed tool. | Changing what a tool asks the model for. Validate via [`eval.md`](eval.md). |
| `llm_utils.py` (155) | The **only** place the Anthropic SDK is touched. Retry, model override, prompt caching, usage capture, `LLMCallFailed`. | Changing model ids, retry policy, or token accounting. |
| `tracing.py` (37) | `traced_call` and `summarize`. Small and load-bearing. | Almost never — but read it before adding a tool. |
| `heuristics.py` (155) | Five deterministic checks: `is_explicit_human_request`, `looks_like_tangent`, `looks_like_field_shape`, `looks_like_research_skip`, `looks_like_bare_affirmation`, `looks_like_acknowledgment`. Closed token sets, no LLM. | Tuning the optimistic-capture gates or the filler-interrupt policy. |
| `faq.py` (55) | Four static FAQ entries and a keyword matcher. Called once per turn from the dispatcher. | Adding a canned answer for a caller side-question. |
| `fillers.py` (137) | `FILLER_PHRASES`, timing constants, `VOICE`/`VOICE_SPEED`, and `filler_for_state()`. | Changing what the agent says while thinking. Regenerate the WAVs after. |

### `backend/supervisor/knowledge/` — the statute corpus

| File | Owns |
|---|---|
| `corpus.py` (36) | `StatuteEntry` TypedDict, `load_corpus(area)` with a process-level cache. |
| `search.py` (80) | Okapi BM25 over a corpus, plus a small stopword list. Deliberately not embeddings — 8–12 entries per area does not justify a vector DB. |
| `{employment,tenancy,immigration}_statutes.json` | The corpora themselves. Reference data, not a repository concern. |
| `tests/test_search.py` | Four tests for the ranking. **Not run by pre-commit** — only `backend/tests` and `eval/tests` are. |

### `backend/transport/` — the LiveKit agent

| File | Owns |
|---|---|
| `livekit_agent.py` (646) | `JupusAgent` (the one tool, verbatim-transcript resolution, filler scheduling, latency boundaries, call-state publishing), `build_session`, `entrypoint`, `build_server`, `start_agent_server`/`stop_agent_server`, and `_on_main_loop`. |
| `prompts.py` (116) | `SUPERVISOR_INSTRUCTIONS` (the Realtime system prompt) and `ASK_SUPERVISOR_SCHEMA` (the one tool schema). Moved server-side in Phase 14; used to ship as JavaScript to the caller's machine. |
| `filler_audio/*.wav` | Six pre-rendered clips: two lines each for `confirm_field`, `confirm_booking`, `propose_slot`. Committed on purpose. Regenerate with `scripts/generate_filler_audio.py`. |

### `backend/db/` — persistence

| File | Owns |
|---|---|
| `schema.sql` (86) | Eight tables and the `UNIQUE(call_id, seq)` trace index. |
| `repositories/base.py` (106) | Six ABCs plus `SlotAlreadyBookedError`. The contract everything above depends on. |
| `repositories/__init__.py` | The `Repositories` dataclass and `get_repositories(settings)` — the single DI seam. |
| `repositories/connection.py` | `connect()`, `reset_schema()`, and `SCHEMA_TABLES` (also the allowlist for the admin DB viewer). |
| `repositories/sqlite_*.py` | One implementation per interface: `calls`, `slots`, `trace`, `eval`, `annotations`, `dev`. |
| `repositories/testing.py` | Test-only helpers for building a temp-SQLite `Repositories`. |
| `seed_slots.py` | Resets the schema and seeds 10 business days × 16 half-hour slots × 3 areas, with 10:00 and 14:00 on day one pre-booked so the conflict path is deterministically reachable. |
| `seed_demo_calls.py` (225) | Eight canned calls plus two BD annotations, so the admin panel and eval judge have something to look at without a live mic. |

> `seed_demo_calls.py` sets a `preferred_time` key on `caller_profile`. That field does not exist in `CallerProfile` and nothing reads it — a leftover from an earlier design that also survives in `docs/PLAN.md`'s tool catalog. Harmless; ignore it. The real fields are `FIELD_PRIORITY = ["name", "email", "phone"]`.

### `backend/tests/` — 351 tests

See [`testing.md`](testing.md) for the full map. `fakes.py` holds in-memory repository doubles; `test_scenarios.py` (599 lines) drives all seven canonical scenarios (eight test functions — S7 has two variants) through the real dispatcher → graph → persistence path with Claude mocked.

---

## `eval/` — measurement

| File | Owns | Run it |
|---|---|---|
| `error_classes.py` | The editable four-class taxonomy. Single source of truth for the judge prompt *and* the admin UI. | — |
| `insights_agent.py` (381) | Deterministic stats, latency-stage derivation, cost accounting, the LLM-judge pass, the taxonomy-critique pass. | — (library) |
| `run_eval.py` | Deterministic + classification + critique over a batch, tagged with a label. | `python eval/run_eval.py --label demo` |
| `replay_scenarios.py` (196) | Drives the seven canonical scenarios through the **real, unmocked** pipeline. | `python eval/replay_scenarios.py --label baseline` |
| `compare_runs.py` | Diffs per-error-class rates between two labels. Exit code 1 on regression, so it works as a gate. | `python eval/compare_runs.py --baseline a --candidate b` |
| `calibrate_judge.py` | Judge vs. human annotations: TP/FP/FN, precision, recall, per class. | `python eval/calibrate_judge.py` |
| `concurrency_stress_test.py` (251) | N concurrent independent calls; proves no cross-call state leakage. Also drives the admin stress-test page. | `python eval/concurrency_stress_test.py` |
| `livekit_live_call.py` (267) | Real LiveKit calls with **synthesized caller speech** — the only test that crosses the transport boundary. | `python eval/livekit_live_call.py --all` |
| `filler_latency_report.py` (188) | Perceived wait vs. actual round trip, from real traces. | `python eval/filler_latency_report.py` |
| `pricing.py` | Hardcoded per-million rates for both vendors, keyed by model id. **Verify against published pricing before trusting any dollar figure.** | — |

Full detail in [`eval.md`](eval.md).

---

## `client/` — the caller page

| File | Owns |
|---|---|
| `index.html` (194) | The whole page: inline CSS, the orb canvas, three field tiles, the transcript panel, and the four script tags (LiveKit UMD from unpkg, then `config.js`, `app.js`, `livekit-transport.js`). |
| `app.js` (274) | **Presentation only.** Status text, the orb visualiser, transcript rendering, the captured-details tiles, teardown. Also derives the backend URL from where the page is served. |
| `livekit-transport.js` (123) | Token fetch, room connect, mic publish, the `jupus.call_state` data handler, the `lk.transcription` text-stream handler. |
| `config.example.js` | Optional overrides. Local development needs no `config.js` at all. |

See [`frontend.md`](frontend.md).

---

## `admin/` — five pages

| Page | File | Shows |
|---|---|---|
| Calls + insights | `index.html` / `app.js` | Call list with outcome and error-class badges; drill-in to transcript, trace, per-call latency and cost; the eval summary; pending taxonomy suggestions with approve/reject. |
| Live supervisor | `graph.html` / `graph.js` (626) | A call moving through the graph in real time, over `WS /admin/trace/{call_id}`. |
| Annotate | `annotate.html` / `annotate.js` | The Benevolent Dictator's queue: unreviewed calls, the error-class checklist, free-text notes, gold marking. |
| Stress test | `stress-test.html` / `stress-test.js` | Fire N concurrent calls and watch for cross-call leakage, over `WS /admin/stress-test-stream/{run_id}`. |
| DB viewer | `db-viewer.html` / `db-viewer.js` | Paginated raw table dump for debugging. |

No build step, no framework, no bundler. Plain scripts and `fetch`.

---

## `scripts/`

| File | Owns |
|---|---|
| `check_architecture.py` | Pre-commit: greps the **staged** diff for raw `sqlite3` outside the repository package (rule 9) and direct Anthropic SDK use outside `llm_utils.py` (rule 7). Deliberately incomplete, and says so. |
| `check_no_secrets.py` | Pre-commit: blocks a staged diff containing something shaped like a real API key. |
| `generate_filler_audio.py` | Renders `FILLER_PHRASES` to WAV via OpenAI TTS, in the same voice and speed as the live session. Run only when the phrases, voice or speed change. Costs a few cents. |

---

## Config and infrastructure

| File | Purpose |
|---|---|
| `pyproject.toml` | Deps, the `dev` extra, `asyncio_mode = "auto"` for pytest. |
| `requirements.txt` | Railway's install path (it does not use the editable install). |
| `.pre-commit-config.yaml` | Three local hooks: pytest, architecture, secrets. |
| `Procfile` | Railway start command. |
| `firebase.json` / `.firebaserc` | Firebase Hosting for the client. |
| `.mcp.json` | Two MCP servers for debugging: `chrome-devtools` and a read-only `sqlite` server. **Debugging only** — app code still goes through repositories. |
| `.claude/skills/check-backend-logs/` | A skill that tails `backend.log` and cross-references it against `trace_events` for a `call_id`. |

---

## Dependency direction, in one picture

```
client/ ─┐
admin/  ─┼─► backend/app.py ─► backend/dispatcher.py ─► backend/supervisor/graph.py
         │        │                    │                        │
         │        │                    │                        ├─► tools.py ─► llm_utils.py ─► Anthropic
         │        │                    │                        │       └─► prompts / knowledge / faq
         │        │                    │                        └─► tracing.py
         │        └────────────────────┴────────────────────────┴─► db/repositories/ ─► SQLite
         │
backend/transport/livekit_agent.py ─► dispatcher.run_supervisor_turn
                                   └─► LiveKit + OpenAI Realtime

eval/ ─► backend/  (never the reverse at module load)
```
