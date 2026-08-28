# HTTP and WebSocket API

`backend/app.py`. Everything the browser, the admin panel and the eval tooling talk to.

---

## Route order matters

Two ordering constraints are load-bearing and are commented in the source:

1. **`GET /api/calls/unreviewed` is registered before `GET /api/calls/{call_id}`.** Starlette matches in registration order, so the path-parameter route would otherwise greedily match `"unreviewed"` as a call id.
2. **The `/admin` static mount is registered last**, so it never shadows `/api/*` or `/admin/annotate`.

---

## Call transport

### `POST /livekit-token`

Mints a LiveKit room JWT. **The room name is the call id** — that is how the id reaches the agent (`ctx.job.room.name`) and keys every trace event, `CallState` and DB row, with no side channel.

```
Request:  {"call_id": "<uuid minted by the browser>"}
Response: {"url": "wss://...", "token": "<jwt>", "room": "<call_id>"}
503:      {"error": "LiveKit is not configured (set LIVEKIT_URL/API_KEY/API_SECRET)"}
```

Gated by `Depends(verify_access_token)` — see the access gate below. Grants `room_join`, `can_publish`, `can_subscribe`, `can_publish_data`, with identity `"caller"`. That identity string is what `client/livekit-transport.js` uses to tell caller transcripts from agent transcripts.

The browser holds **no OpenAI credential at all**. The Realtime session is opened server-side by the agent worker. This replaced a `POST /session` endpoint that used to hand out an ephemeral OpenAI client secret.

> There is no longer a `WS /bridge`. The hand-rolled WebRTC transport it belonged to was retired in Phase 14.

---

## Admin data API

All read-only unless noted. No per-route auth — the whole `/admin` surface is behind the middleware gate, and `/api/*` is not gated at all (a local-prototype tradeoff).

| Method | Path | Returns |
|---|---|---|
| GET | `/api/calls` | Every call: `call_id`, `started_at`, `practice_area`, `outcome`, `escalation_reason`, `booking_slot_id`, `error_classes` (sorted unique judge flags), `reviewed` (bool) |
| GET | `/api/calls/unreviewed` | Calls with no `call_reviews` row, oldest first — the BD's queue |
| GET | `/api/calls/{call_id}` | Full detail: caller fields, decoded `transcript`, `call_error_flags`, `human_review`. 404 if unknown. |
| GET | `/api/calls/{call_id}/trace` | The raw ordered `trace_events` rows |
| GET | `/api/calls/{call_id}/latency` | `{"stages": {...}, "cost": {...}}` for this one call, from `_stage_durations_for_call` and `_cost_for_call`. 404 if the call has no trace. |
| GET | `/api/eval/summary?label=` | `run_deterministic_pass` output plus `error_rates`. With `label`, rates are scoped to that run; without, pooled across all runs. |
| GET | `/api/eval/error-classes` | The active taxonomy from `eval/error_classes.py` |
| GET | `/api/eval/taxonomy-suggestions?label=&status=` | Suggestion rows, filterable |
| POST | `/api/eval/taxonomy-suggestions/{id}/approve` | Sets `status='approved'` |
| POST | `/api/eval/taxonomy-suggestions/{id}/reject` | Sets `status='rejected'` |
| GET | `/api/eval/compare?baseline=&candidate=` | `build_comparison` output — the same data `eval/compare_runs.py` prints |
| GET | `/api/calls/{call_id}/review` | The BD's review. 404 if not yet reviewed. |
| POST | `/api/calls/{call_id}/review` | Saves a review; returns the saved row |
| GET | `/api/dev/tables` | Table names (the `SCHEMA_TABLES` allowlist) |
| GET | `/api/dev/tables/{table}?limit=&offset=` | Paginated rows. `limit` capped at 500, `offset` floored at 0. 404 on an unknown table. |

### `POST /api/calls/{call_id}/review`

```json
{
  "error_class_ids": ["repetition"],
  "uncategorized_notes": ["agent talked over the caller twice"],
  "overall_note": "otherwise clean",
  "is_gold": true
}
```

The `annotator` is **not** taken from the request — it comes from `settings.annotator_name` (default `"benevolent_dictator"`). This is a fixed identity label for a single-user local tool, not an auth system. Saving is delete-then-insert: exactly one active review per call.

An entry in `uncategorized_notes` becomes a `human_annotations` row with `error_class_id = NULL`. That is the strongest signal into the taxonomy-critique pass — a domain expert saying "something is wrong here and we have no name for it".

### Approving a suggestion does not change the taxonomy

`/approve` flips a row's status and nothing else. Applying an approved suggestion means a **human hand-editing `eval/error_classes.py`**. Neither the judge nor its own self-critique may auto-apply a taxonomy change; that is the whole point of [`../benevolent_dictator.md`](../benevolent_dictator.md).

---

## Concurrency stress test

The admin-panel front end for `eval/concurrency_stress_test.py`. Fire-and-forget: the POST returns immediately with a `run_id` and the WebSocket streams results.

### `POST /api/stress-test/run`

```json
{"mode": "mocked", "n_levels": [5, 10, 20, 40]}   →   {"run_id": "a1b2c3d4e5f6"}
```

- `mode` must be `"mocked"` or `"live"` (400 otherwise)
- `n_levels` must be non-empty (400 otherwise)
- **`mode: "live"` refuses any `N > LIVE_SAFETY_CAP`** (400). The cap is `10`; it bounds real API spend on an accidental large live run, and it is deliberate. Default levels are `(5, 10, 20, 40)`, chosen to span below and above the default `asyncio.to_thread` executor cap.

**One run at a time, by design.** All runs share `eval.concurrency_stress_test.STRESS_DB_PATH` — never `backend/db/calendar.db` — and `build_stress_repos()` resets that file at the start of each run. Starting a second run before the first finishes would race on that reset. Not guarded against; this is a single-operator local tool.

---

## WebSockets

Both poll rather than push. That is the simplest thing that works at this scale, and neither can send anything back into a call path.

### `WS /admin/trace/{call_id}` — the live supervisor view

Polls `repos.trace.get_trace(call_id)` every 400ms and sends only what is new. Also reads `dispatcher.CALL_STATES[call_id]` directly (in-memory, no repository — it is the same live-process state a live call already holds) and sends a `call_state` snapshot whenever it changes.

```json
{"type": "trace_events", "events": [ ... ]}
{"type": "call_state", "stage": "capture", "caller_profile": {...}, "booking": {...}}
```

Read-only spectator feed. It is a rendering layer over instrumentation that already exists, not a second logging path, and it has zero ability to affect a live call or add latency to it. The `call_state` half is what lets the page show the field-by-field and slot-proposal sub-state that lives *inside* the capture and booking nodes without those being real graph nodes.

### `WS /admin/stress-test-stream/{run_id}`

Polls `STRESS_RUNS[run_id]` every 300ms.

```json
{"type": "level_result", "result": {...}}
{"type": "run_finished", "status": "done", "verdict": {...}, "error": null}
{"type": "error", "error": "unknown run_id"}
```

---

## Static

| Path | Serves |
|---|---|
| `GET /admin/annotate` | `admin/annotate.html` — needs its own route because `StaticFiles(html=True)` only auto-serves `index.html` for a bare directory path |
| `/admin` (mount) | The whole `admin/` directory, `html=True`, so both `/admin` and `/admin/` serve `index.html` |

The caller page (`client/`) is **not** served by the backend. Open `client/index.html` as a file, or deploy it to Firebase Hosting.

---

## CORS and the access gate

### CORS

```python
allow_origins = [settings.public_client_origin] if settings.public_client_origin else ["*"]
```

Wide open by default, because `client/index.html` is opened directly as a `file://` page and its origin is literally `"null"` — nothing narrower would let it reach the backend at all. Setting `PUBLIC_CLIENT_ORIGIN` locks it down to the real deployed origin.

### The access gate — two mechanisms

Both are complete no-ops when `JUPUS_ACCESS_TOKEN` is unset, which is the local-development default.

**1. `verify_access_token`** — a FastAPI dependency on `/livekit-token`. Requires `?access_token=` to match exactly, else 401.

**2. `admin_access_gate`** — HTTP middleware over everything under `/admin`. Accepts either the query param **or** the `jupus_admin_token` cookie, and sets that cookie (httponly, samesite=lax) on the first successful query-param request.

The cookie is not decoration. The browser's follow-up requests for the page's own JS and CSS never carry the query param — only the link a person was given does — so a query-param-only check 401s every asset and the page never finishes loading. That shipped and had to be fixed (`docs/fixes/2026-08-24-011.md`).

It is scoped to a single middleware rather than per-route dependencies because `StaticFiles` serves many files under the mount.

> **This is a casual-discovery deterrent, not an auth boundary.** One shared secret, no users, no sessions, no rotation. `/api/*` is not gated at all. Do not put anything sensitive behind it. `docs/DECISIONS.md` says so explicitly.

---

## Adding an endpoint

1. Add the route to `backend/app.py`, taking `repos: Repositories = Depends(get_repos)`.
2. Read data through repositories only — no `sqlite3`, no raw SQL. Pre-commit blocks it.
3. If the path could collide with an existing `{param}` route, register it **before** that route.
4. Register it before the `/admin` mount if it lives under `/admin`.
5. Add a test in `backend/tests/test_admin_routes.py` (or `test_annotation_routes.py`) using a `TestClient` with fake repositories injected via `app.dependency_overrides`.
