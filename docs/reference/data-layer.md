# Data Layer

`backend/db/`. The only place in the codebase that knows SQL or table names exist.

Rule 9 is one of two rules enforced by pre-commit: `scripts/check_architecture.py` greps the staged diff for `import sqlite3` or `sqlite3.connect(` anywhere outside `backend/db/repositories/` and blocks the commit. Everything above this layer takes a `Repositories` object (or one repository) as a parameter.

---

## Schema

`backend/db/schema.sql`. Eight tables. The DB file is `backend/db/calendar.db`, gitignored, created by `python backend/db/seed_slots.py`.

### `slots` — the calendar

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `area` | TEXT | employment / tenancy / immigration |
| `start_time` | TEXT | ISO local, e.g. `2026-08-27T14:30:00`. No timezone. |
| `is_booked` | INTEGER | 0 or 1 |

Seeded as 10 business days × 16 half-hour slots (09:00–16:30) × 3 areas = 480 rows, with **10:00 and 14:00 on the first seeded day pre-booked for every area** so the conflict path in scenario S3 is deterministically reachable.

### `calls` — one row per call

| Column | Notes |
|---|---|
| `call_id` | TEXT PK — the LiveKit room name |
| `started_at` | Set on first insert only — `started_at` is deliberately absent from the `ON CONFLICT DO UPDATE SET` list, so later upserts preserve it. |
| `ended_at` | Set only when `stage == "ended"` |
| `practice_area` | |
| `outcome` | `booked` / `escalated` / `info_only` / `abandoned` / NULL while in progress |
| `escalation_reason` | One of the six reasons |
| `caller_name`, `caller_email`, `caller_phone` | **Only written when that field's status is `confirmed`.** A `pending_confirm` value never reaches this table. |
| `booking_slot_id` | Only when `booking_confirmed` |
| `transcript_json` | The full `CallState["transcript"]`, JSON-encoded |

### `trace_events` — the decision record

| Column | Notes |
|---|---|
| `id` | INTEGER PK AUTOINCREMENT |
| `call_id` | |
| `seq` | Per-call ordinal, derived from `MAX(seq)+1` under a lock |
| `ts` | ISO UTC |
| `event_type` | See [`tracing.md`](tracing.md) for the full list |
| `node` | Node name, or `"transport"`, `"dispatcher"`, `"eval_judge"` |
| `payload_json` | Everything else, JSON-encoded |

```sql
CREATE UNIQUE INDEX idx_trace_events_call_seq ON trace_events(call_id, seq);
```

**UNIQUE, not just an index.** A duplicate `(call_id, seq)` means the `seq_lock` was bypassed; this turns that into an immediate loud failure instead of silently corrupting trace ordering. The counter used to live in an in-memory dict with a non-atomic read-modify-write, which both raced across threads and reset to empty on every `uvicorn --reload` (`docs/fixes/2026-08-24-004.md`).

### Eval tables

| Table | Holds |
|---|---|
| `eval_runs` | `(call_id, eval_run_label)` PK, plus `scenario_id`. Which calls belong to which labelled batch. |
| `call_error_flags` | One row per judge flag: `error_class_id`, `confidence`, `evidence`, `eval_run_label`. `error_class_id` references `eval/error_classes.py` and is **not** FK-enforced — the taxonomy lives in code. |
| `taxonomy_suggestions` | LLM-proposed taxonomy changes. `suggestion_type` ∈ new_class / misclassification / refine_existing. `status` defaults to `'pending'`. |
| `call_reviews` | One row per human-reviewed call: `annotator`, `is_gold`, `overall_note`. |
| `human_annotations` | The Benevolent Dictator's flags. `error_class_id` **NULL means "an issue with no fitting class"**, with the text in `note` — the single most valuable signal the system produces. |

The `call_error_flags` comment in `schema.sql` says it is "superseded" — that refers to a Phase 2 placeholder of the same name. The table as it stands is live and current.

---

## Repository interfaces

`backend/db/repositories/base.py`. Six ABCs plus `SlotAlreadyBookedError`.

### `CallRepository`
```python
upsert(state: CallState, outcome_override: Optional[str] = None) -> None
get(call_id) -> Optional[dict]
list(*, with_outcome_only=False, reviewed: Optional[bool] = None) -> list[dict]
```
`upsert` derives everything from the `CallState` — outcome, `ended_at`, the confirmed-only caller fields, `booking_slot_id`. `outcome_override` exists for one caller: `mark_call_abandoned`.

> `list(reviewed=...)` raises `NotImplementedError` in the SQLite implementation. The admin route filters reviewed status in Python instead, by calling `annotations.get_review` per row. Fine at this scale; an obvious first thing to push down if the call volume ever grows.

### `SlotRepository`
```python
check_availability(date, window, area, exact_time=None, exclude_ids=None) -> Optional[dict]
suggest_alternatives(date, area, exclude_ids) -> list[dict]     # up to 3, from `date` onward
book(slot_id) -> int                                            # raises SlotAlreadyBookedError
seed(areas, business_days) -> None
```

`book` is an **atomic guarded update**, not select-then-update:
```sql
UPDATE slots SET is_booked = 1 WHERE id = ? AND is_booked = 0
```
`rowcount == 0` raises. This makes the check-then-act race impossible rather than merely unlikely.

`suggest_alternatives` omits the `NOT IN` clause entirely when `exclude_ids` is empty. `id NOT IN (NULL)` is never true for any row under SQL's three-valued logic and silently matched zero slots — a real bug that was fixed.

### `TraceRepository`
```python
record_event(call_id, event_type, node=None, **payload) -> None
get_trace(call_id) -> list[dict]          # ordered by seq
```

### `EvalRepository`
```python
add_error_flags(call_id, flags, eval_run_label)
add_taxonomy_suggestions(suggestions, eval_run_label)     # always status='pending'
update_suggestion_status(suggestion_id, status)
tag_eval_run(call_id, eval_run_label, scenario_id=None)
compute_error_rates(eval_run_label) -> dict[str, float]
compute_error_rates_all() -> dict[str, float]
list_taxonomy_suggestions(eval_run_label, status) -> list[dict]
get_error_flags(call_id) -> list[dict]
call_ids_already_evaluated() -> set[str]
```
Rates are `COUNT(DISTINCT flagged call_id) / COUNT(DISTINCT calls in the run)`, computed **for every active error class including zeros** — a 0% rate is information, not missing data.

### `AnnotationRepository`
```python
save_review(call_id, annotator, error_class_ids, uncategorized_notes, overall_note, is_gold)
get_review(call_id) -> Optional[dict]      # review row + nested "annotations" list
list_unreviewed() -> list[dict]            # LEFT JOIN calls against call_reviews
```
`save_review` is **delete-then-insert**, not accumulate: exactly one active human review per call at any time.

### `DevRepository`
```python
list_tables() -> list[str]
get_table(table, *, limit=100, offset=0) -> dict
```
Read-only, for the admin DB viewer. The table name is checked against `SCHEMA_TABLES` **before** interpolation. That check is the only thing standing between a query parameter and an f-string in a SQL statement — do not remove it, and do not add a method that skips it.

---

## Wiring

```python
@dataclass
class Repositories:
    calls: CallRepository
    slots: SlotRepository
    trace: TraceRepository
    evals: Optional[EvalRepository] = None
    annotations: Optional[AnnotationRepository] = None
    dev: Optional[DevRepository] = None

def get_repositories(settings) -> Repositories:
    if settings.db_backend == "sqlite":
        conn = connect(settings.db_path)
        return Repositories(calls=SQLiteCallRepository(conn), ...)
    raise NotImplementedError(...)
```

The last three are `Optional` so a test can build a minimal `Repositories` with only what it needs. Production code that touches them guards with `if repos.evals is not None`.

**One `sqlite3.Connection`, shared by all six repositories**, opened with `check_same_thread=False`. Reachable from multiple worker threads since the `asyncio.to_thread` change — thread safety is unverified and logged as `docs/known-issues/2026-08-24-002.md`. Low practical risk at this scale, but it is a known open item, not an oversight.

### Injection

- **App:** `REPOS = get_repositories(settings)` at module level in `backend/app.py`; routes get it via `Depends(get_repos)`, so tests can substitute fakes.
- **Graph:** through the LangGraph config — `GRAPH.invoke(state, config={"configurable": {"repos": repos}})`, read back inside nodes by `_repos(config)`.
- **Agent:** stashed as `_REPOS` by `start_agent_server` and held on each `JupusAgent` instance.
- **Eval and scripts:** call `get_repositories(settings)` themselves.

---

## Creating and resetting a database

```python
connect(db_path)          # sqlite3.connect(..., check_same_thread=False)
reset_schema(conn)        # DROP every table in SCHEMA_TABLES, then run schema.sql
```

`reset_schema` **drops everything**, including all calls, traces, annotations and eval history. `seed_slots.py` calls it — so **`python backend/db/seed_slots.py` on an existing database destroys the call history.** There is no migration system; schema changes mean editing `schema.sql` and re-seeding.

`SCHEMA_TABLES` is ordered for drop-safety and doubles as the allowlist for `SQLiteDevRepository`. A new table must be added there or the DB viewer will not see it and `reset_schema` will leave it behind.

### Seeding

| Script | Does |
|---|---|
| `python backend/db/seed_slots.py` | Resets the schema, seeds the calendar. **Run this first on a fresh checkout.** |
| `python backend/db/seed_demo_calls.py` | Inserts 8 canned calls plus 2 BD annotations. Assumes the schema exists. Idempotent (the upsert is `ON CONFLICT DO UPDATE`), and never touches `slots`. |

Both go through repository classes, never raw SQL — even the seed scripts obey rule 9.

> Invoke these as `python backend/db/seed_slots.py` from the repo root. A stale editable install once made bare file-path invocation silently pull imports from a *different* checkout; `python -m backend.db.seed_slots` is the safer form if you hit anything strange (`docs/fixes/2026-08-21-005.md`).

---

## Adding a repository method

1. Add the abstract method to the ABC in `base.py`.
2. Implement it in the `sqlite_*.py` file that owns that table.
3. Implement it in the corresponding fake in `backend/tests/fakes.py` — **the fakes subclass the ABCs, so an unimplemented abstract method breaks every test that constructs one.** This is the most common way to get a confusing test failure after a data-layer change.
4. Add a repository test (`test_sqlite_*_repository.py` against temp SQLite).
5. Call it from above via `traced_call` if it happens inside a node.

## Swapping SQLite for something else

This is the scenario the layer was built for, and it is genuinely contained:

1. Write `PostgresCallRepository` etc. against the same ABCs.
2. Add a `db_backend == "postgres"` branch to `get_repositories`.
3. Set `DATABASE_URL` and `db_backend` in the environment. The setting already exists in `backend/config.py`.

**Nothing outside `backend/db/repositories/` needs to change.** The `NotImplementedError` in `get_repositories` says exactly this, and it is the only place a new backend is wired in.

Two things do not travel: the `UNIQUE(call_id, seq)` trace invariant, which needs an equivalent constraint; and `SQLiteDevRepository`'s `SCHEMA_TABLES` allowlist, which the DB viewer depends on.
