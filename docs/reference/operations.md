# Operations

Running it, configuring it, deploying it, and fixing it when it misbehaves.

---

## Requirements

- **Python 3.11+**
- **An OpenAI API key** — Realtime speech. Paid.
- **An Anthropic API key** — supervisor reasoning. Paid.
- **A LiveKit Cloud project** — the free "Build" tier is enough, no card needed. **Without it no call can connect at all.**

Both API keys cost real money on every live call. If you do not know whether spend caps or alerts are set on either account, check before running a long test sequence — `eval/replay_scenarios.py` alone drives seven full scenarios against live APIs.

---

## First run

```bash
pip install -e ".[dev]"
pre-commit install

cp .env.example .env
# fill in OPENAI_API_KEY, ANTHROPIC_API_KEY,
# and LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET

python backend/db/seed_slots.py            # creates + seeds the database
uvicorn backend.app:app --reload > backend.log 2>&1
```

Then open `client/index.html` in a browser and talk. Watch it at `http://localhost:8000/admin`.

The log redirection is deliberate: it is what lets the `check-backend-logs` skill read the logs itself instead of asking you to paste terminal output. `backend.log` is gitignored.

### Without a microphone

```bash
python backend/db/seed_demo_calls.py       # 8 canned calls + 2 BD annotations
python eval/run_eval.py --label demo       # run the judge over them
```

`/admin` then has badges, transcripts and traces to look at. Note that hand-seeded calls have **no trace events**, so the judge will never flag them — see the known limits in [`eval.md`](eval.md).

---

## Configuration

Everything is read by `backend/config.py` through `pydantic-settings`, from `.env` or the real environment.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `OPENAI_API_KEY` | **yes** | — | Realtime. The backend will not start without it. |
| `ANTHROPIC_API_KEY` | **yes** | — | Supervisor. Same. |
| `LIVEKIT_URL` | for calls | `None` | `wss://...`. Missing → agent worker does not start, `/livekit-token` returns 503. |
| `LIVEKIT_API_KEY` | for calls | `None` | |
| `LIVEKIT_API_SECRET` | for calls | `None` | |
| `JUPUS_PORT` | no | `8000` | **Currently has no effect.** `settings.port` is defined in `config.py` but read nowhere. Pass `uvicorn --port` instead. (Railway uses its own `$PORT`, via the `Procfile`.) |
| `JUPUS_DB_PATH` | no | `backend/db/calendar.db` | |
| `db_backend` | no | `sqlite` | Only `sqlite` is implemented. |
| `DATABASE_URL` | no | `None` | Unused until a Postgres backend exists. |
| `annotator_name` | no | `benevolent_dictator` | A fixed label written onto every annotation, not an auth identity. |
| `JUPUS_ACCESS_TOKEN` | no | `None` | Unset → both gates are complete no-ops. Set → required by `/livekit-token` and `/admin`. |
| `PUBLIC_CLIENT_ORIGIN` | no | `None` | Unset → CORS is `*` (needed for `file://`). Set → locked to that origin. |

Client-side, `client/config.js` (gitignored, optional) can set `window.JUPUS_BACKEND_URL` and `window.JUPUS_ACCESS_TOKEN`. Local development needs neither.

---

## Everyday commands

```bash
# tests
pytest backend/tests eval/tests                                        # what pre-commit runs
pytest backend/tests eval/tests backend/supervisor/knowledge/tests      # everything (406)

# eval
python eval/run_eval.py --label <name> [--calls all|new]
python eval/replay_scenarios.py --label <name>        # REAL API calls
python eval/compare_runs.py --baseline a --candidate b
python eval/calibrate_judge.py

# instrumentation
python eval/filler_latency_report.py
python eval/concurrency_stress_test.py [--n-levels 5 10 20 40] [--mode mocked|live]
python eval/livekit_live_call.py --all --label livekit-live    # needs the backend running

# maintenance
python backend/db/seed_slots.py           # DESTRUCTIVE — drops every table
python backend/db/seed_demo_calls.py
python scripts/generate_filler_audio.py   # only after changing fillers/voice/speed
```

---

## The one-worker rule

**Run exactly one backend against a given set of LiveKit credentials.**

The agent worker registers with **automatic dispatch**, meaning it is a candidate for every room in the project. A second backend — a stale terminal, a forgotten `--reload`, or the hosted deployment while you are also running locally — will happily take calls this one expects to handle. Because the other process has its own in-memory `CALL_STATES`, the call simply **vanishes from this one's admin panel and database, with no error on either side.**

The backend logs a warning at startup for exactly this reason. It cost real debugging time during Phase 14.

If you genuinely need local and hosted at once, give production its own LiveKit project.

---

## Hosted deployment

- **Backend:** FastAPI on Railway with a persistent volume for SQLite. Auto-deploys from `master`. Needs `LIVEKIT_*` set as Railway variables or `/livekit-token` returns a 503 saying so. Railway installs from `requirements.txt`, not the editable install, and starts via the `Procfile`.
- **Client:** Firebase Hosting at `https://jupus-5661c.web.app`. `firebase deploy --only hosting`.
- **Gate:** `JUPUS_ACCESS_TOKEN` on both. This is a **casual-discovery deterrent, not an auth boundary** — one shared secret, no users, no sessions, no rotation, and `/api/*` is not gated at all. Do not put anything sensitive behind it.

The client works out its own backend from where it is served, so there is no config file to edit before deploying and change back afterwards.

**Known limits:** single instance, no autoscaling, in-memory call state, SQLite on a volume. The local setup is the primary always-works path.

---

## Pre-commit

Three local hooks on every commit:

1. **`pytest backend/tests eval/tests`** — a red suite blocks the commit. This is what makes "commit at green" real rather than aspirational.
2. **`scripts/check_architecture.py`** — greps the **staged** diff for raw `sqlite3` outside the repository package (rule 9) and direct Anthropic SDK use outside `llm_utils.py` (rule 7).
3. **`scripts/check_no_secrets.py`** — blocks a staged diff containing something shaped like an API key, independently of `.env` being gitignored.

**Be honest about what this does not catch.** A regex cannot verify "every tool call goes through `traced_call`", "each node binds only its own scoped tools", or "Realtime sees exactly one tool". Those are control flow and intent. The project's answer is an independent agent review of the DoD before a phase branch merges — see [`../workflow.md`](../workflow.md).

Note that the knowledge tests are **not** in the hook's path.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Backend boots, no call connects | LiveKit not configured | Check `LIVEKIT_*`. The startup log says explicitly that the worker will not start. |
| `POST /livekit-token` → 503 | Same | Same |
| Call connects, agent is mute | A track not published as `SOURCE_MICROPHONE` — RoomIO silently drops it | Test-harness-only in practice; the rejection is invisible. `docs/fixes/2026-08-25-003.md` |
| Call happened but is absent from `/admin` | **A second worker took it** | Kill the other backend. See the one-worker rule. |
| Agent asks the same thing twice | A capture fallback fired | Read the trace for `capture_fast_*` events — each says exactly why |
| Turn takes 10s+ | A silent retry, usually a `max_tokens` truncation | Look for `llm_retry` in the trace. `docs/fixes/2026-08-25-002.md` |
| Caller hears "Sorry, I'm having a little trouble" | `LLMCallFailed` after one retry | `llm_call_failed` in the trace carries the error. Three in a row escalates. |
| Every latency stat reads 0 | A boundary event is not being emitted | `speech_stopped` / `ask_supervisor_received` / `reply_ready` / `tts_first_audio`. This exact failure shipped once — `docs/fixes/2026-08-24-012.md` |
| Cost reads $0 for a real call | No `realtime_usage` captured at shutdown | The agent logs a loud warning when this happens — check `backend.log` |
| Hosted `/admin` loads empty | The gate 401'd the page's own assets | The cookie mechanism handles this; check it is being set. `docs/fixes/2026-08-24-011.md` |
| Email/phone accepted with a wrong value | Realtime invented the utterance | Verbatim-transcript precedence should prevent it — check `ask_supervisor_received` against the real transcript |
| Tests pass alone, fail together | Global state leaked between tests | `CALL_STATES`, `dispatcher.LOCKS`, or an un-popped `dependency_overrides` |
| Import errors from a script | A stale editable install resolving a different checkout | Use `python -m backend.db.seed_slots`. `docs/fixes/2026-08-21-005.md` |
| Every fake-repository test suddenly errors | A new abstract method with no fake implementation | Implement it in `backend/tests/fakes.py` |

### Before deep-diving

Grep [`../fixes/INDEX.md`](../fixes/INDEX.md) and [`../known-issues/INDEX.md`](../known-issues/INDEX.md) for the symptom. Roughly twenty non-trivial bugs are already written up with root causes, and several are live-only failures no unit test would have caught. Afterwards, write yours up too — that is why the index is worth grepping.

---

## Where things live at runtime

| Path | What | Gitignored |
|---|---|---|
| `backend/db/calendar.db` | Everything durable | yes |
| `backend/db/calendar_stress_test.db` | Stress-test scratch DB, reset per run | yes |
| `backend.log` | uvicorn output | yes |
| `docs/handoffs/{call_id}.md` | One markdown note per escalated call | **no** — committed |
| `.env` | Secrets | yes |
| `client/config.js` | Client overrides | yes |

---

## Debugging tools

- **`.mcp.json`** configures two MCP servers: `chrome-devtools` (inspect the caller page's console and network directly) and a **read-only** `sqlite` server for `backend/db/calendar.db`. Inspection only — application code still goes through repositories.
- **`.claude/skills/check-backend-logs/`** tails `backend.log` and cross-references it against `trace_events` for a `call_id`.
- **`/admin/graph.html`** watches a live call move through the graph.
- **`/admin/db-viewer.html`** dumps any table.
