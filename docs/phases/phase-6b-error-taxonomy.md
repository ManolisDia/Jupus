# Phase 6b — Error Taxonomy + LLM Judge

## Goal

Classify calls against the editable error taxonomy (`docs/error_taxonomy.md`) using the traces built in Phase 2/5 and the admin panel scaffold from 6a. This is the LLM-as-judge pass, without yet involving the Benevolent Dictator's own annotations — that's 6c.

**Dependency note**: depends on 6a (admin panel scaffold, `run_deterministic_pass`, traces). Nothing here reads `call_reviews`, `human_annotations`, or `taxonomy_suggestions` (all introduced in 6c) — `classify_call_errors` and the classification pass are fully self-contained without any human input. 6c depends forward on this sub-phase; this sub-phase never depends on 6c.

## Non-goals
- No taxonomy-critique pass (`propose_taxonomy_updates`) yet — it needs Benevolent Dictator input to be worth building well, and that's 6c's scope. `taxonomy_suggestions` as a table doesn't exist until 6c.
- No annotation UI, no calibration, no regression harness — 6c.

## Prerequisite
Phase 6a DoD met — admin panel + traces + cross-cutting closeout all verified.

---

## `eval/error_classes.py` — the taxonomy registry

```python
ERROR_CLASSES = [
    {"id": "repetition", "name": "Repeated question/request",
     "description": "..."},   # full text from docs/error_taxonomy.md
    {"id": "tool_or_system_failure_surfaced", "name": "Tool/system failure surfaced to caller",
     "description": "..."},
    {"id": "premature_escalation", "name": "Premature or unnecessary escalation",
     "description": "..."},
    {"id": "unconfirmed_action", "name": "Action taken without confirmation",
     "description": "..."},
]

def get_active_error_classes() -> list[dict]:
    return ERROR_CLASSES   # no inactive/retired classes yet — the hook exists
                            # for when one is retired per docs/error_taxonomy.md
```
Single source of truth — the judge prompt is built from this, not a separately-maintained copy of the descriptions.

---

## Database schema additions

```sql
CREATE TABLE call_error_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT REFERENCES calls(call_id),
    error_class_id TEXT NOT NULL,     -- references eval/error_classes.py ids,
                                       -- not FK-enforced (taxonomy lives in code)
    confidence REAL,
    evidence TEXT,                    -- short quote/description, ideally citing
                                       -- specific trace event(s), see below
    eval_run_label TEXT NOT NULL,
    evaluated_at TEXT
);

-- Associates calls with a named eval run, so run_eval.py (this sub-phase)
-- and 6c's replay_scenarios.py can tag batches for later comparison.
CREATE TABLE eval_runs (
    call_id TEXT REFERENCES calls(call_id),
    eval_run_label TEXT NOT NULL,
    scenario_id TEXT,     -- e.g. "S2", set only when created via 6c's replay_scenarios.py
    created_at TEXT,
    PRIMARY KEY (call_id, eval_run_label)
);
```
Update `backend/db/schema.sql` accordingly. Implemented via `backend/db/repositories/sqlite_eval.py` (`SQLiteEvalRepository`) per `docs/architecture.md` — no raw SQL outside that file.

---

## `backend/supervisor/tools.py` addition

```python
def classify_call_errors(call_row: dict, trace: list[dict], error_classes: list[dict]) -> list[dict]:
    # Claude call (via call_claude_tool), forced tool_choice, schema:
    # {"flags": [{"error_class_id": str, "confidence": float, "evidence": str}, ...]}
    # Input: outcome, escalation_reason, AND the full trace (repos.trace.get_trace(
    # call_id) — docs/phases/cross-cutting.md section 0), not just the flat
    # transcript. The trace is what makes several of the seed error classes
    # actually detectable with real evidence rather than surface-text
    # guessing — e.g. "repetition" is visible as two tool_call_start events
    # for update_caller_profile targeting the same field with the field
    # already confirmed; "tool_or_system_failure_surfaced" is visible
    # directly as an llm_call_failed or a system_error escalation event.
    # "evidence" in the returned flag should cite the specific trace
    # event(s) that support it where possible (e.g. "duplicate
    # tool_call_start for update_caller_profile on field=email at seq 4
    # and seq 9"), not just a transcript quote. Returns [] if no errors
    # detected — that's valid, expected, not a failure.
    ...
```

## `eval/insights_agent.py` — extends 6a's version

```python
def run_classification_pass(repos: Repositories, calls: list[dict], eval_run_label: str) -> list[dict]:
    # for each call: classify_call_errors(call, repos.trace.get_trace(call["call_id"]),
    # get_active_error_classes()), then repos.evals.add_error_flags(call_id,
    # flags, eval_run_label) — zero rows written if the call had no errors.
    # Returns the full per-call results (6c's taxonomy-critique pass will
    # take this as input, but nothing in this sub-phase depends on that).
    ...

def compute_error_rates(repos: Repositories, eval_run_label: str) -> dict[str, float]:
    # delegates to repos.evals.compute_error_rates(eval_run_label) —
    # {error_class_id: (# distinct calls flagged) / (# distinct calls in
    # the run)} for every id in get_active_error_classes(), including ones
    # with rate 0.0 (meaningful information, not absence of information).
    ...
```

## `eval/run_eval.py` (new — classification only in this sub-phase, 6c extends it)
```
python eval/run_eval.py --label <name> [--calls all|new]
  - "new" (default): calls not yet present in eval_runs for ANY label
  - "all": re-judge every call with outcome IS NOT NULL, regardless of
    prior runs — useful when the taxonomy itself changed
  1. inserts (call_id, label, scenario_id=NULL, now) into eval_runs
  2. run_deterministic_pass (from 6a) -> print
  3. run_classification_pass -> call_error_flags rows
  4. print compute_error_rates(label) as a table
  (6c adds a step 5: run_taxonomy_critique)
```

## `backend/db/seed_demo_calls.py` — extends 6a's 3-row version
Add 5 more rows (8 total):
- 1 row exhibiting `repetition` — transcript has the agent asking for the caller's email a second time after it was already extracted and confirmed earlier in the same transcript.
- 1 row exhibiting `tool_or_system_failure_surfaced` — transcript includes a generic "having a little trouble, could you repeat that" turn.
- 1 row exhibiting `premature_escalation` — `outcome="escalated"`, but the transcript shows a straightforward, clearly-answerable request with no genuine ambiguity or repeated failure before the escalation.
- 1 row exhibiting `unconfirmed_action` — `outcome="booked"` but the transcript has no read-back/confirmation turn before the booking.
- 1 deliberately bad row — `outcome="booked"` with an invalid-format `caller_email` baked directly into the row, proving the judge can catch a data-quality issue distinct from the conversational error classes above.

(6c further extends this file with pre-populated BD annotations on 2 of these rows.)

---

## Admin panel — extends 6a's base

- `GET /api/calls` — now includes an error-class badge list per row (from `call_error_flags`)
- `GET /api/calls/{call_id}` — now also includes **all** `call_error_flags` rows for that call (across every `eval_run_label`), each with its `evidence` text
- `GET /api/eval/summary?label=<name>` — now includes `error_rates` (from `compute_error_rates`) alongside 6a's deterministic stats

### `admin/index.html` + `admin/app.js` — extended
Calls list gains a small badge per row for each LLM-assigned error class. Detail pane lists each flag's `evidence` text.

---

## Tests

### `eval/tests/test_error_classes.py`
1. `test_all_seed_classes_have_id_name_description`.
2. `test_ids_are_unique`.

### `eval/tests/test_insights_agent.py` additions (extends 6a's file)
1. `test_classify_call_errors_mocked_returns_expected_shape` — mock the underlying Claude call to return a 2-flag response; assert `classify_call_errors` returns exactly that structure.
2. `test_classify_call_errors_empty_result_is_valid` — mock `{"flags": []}`; assert accepted, not treated as an error.
3. `test_run_classification_pass_writes_one_row_per_flag` — mock 2 calls, one returning 1 flag and one returning 2 flags; assert 3 total `call_error_flags` rows, correctly attributed by `call_id`.
4. `test_compute_error_rates_includes_zero_rate_classes` — a run where no call was flagged with `premature_escalation`; assert the returned dict still includes `"premature_escalation": 0.0`.

### `backend/tests/test_seed_demo_calls.py` additions
1. `test_seed_creates_eight_calls_total`.
2. `test_seed_includes_one_example_per_error_class` — for each `id` in `ERROR_CLASSES`, assert at least one seeded transcript is structurally shaped to plausibly exhibit it (a structural check, e.g. for `unconfirmed_action`, no transcript turn between the last field capture and the booking event — doesn't prove the LLM will classify it correctly, only that the fixture is well-formed).
3. `test_seed_includes_deliberately_bad_invalid_email_call`.

### `eval/tests/test_run_eval_integration.py`
1. `test_run_eval_populates_eval_runs_and_call_error_flags` — after running with `--label test1` against seeded data (Claude calls mocked), assert `eval_runs` has one row per seeded call tagged `test1`, and `call_error_flags` has rows for the calls expected to have errors.
2. `test_deterministic_stats_match_seed_data`.
3. `test_rerun_with_all_flag_rejudges_existing_calls` — run once with `--label a`, run again with `--label b --calls all`; assert both labels have full `eval_runs` coverage.

### `backend/tests/test_admin_routes.py` additions (extends 6a's file)
1. `test_api_calls_list_includes_error_badges`.
2. `test_api_call_detail_includes_all_error_flags_for_that_call` — seed a call with 2 flags, assert both appear.
3. `test_api_eval_summary_includes_error_rates`.

---

## Definition of Done

- [x] `python backend/db/seed_demo_calls.py` runs clean, now produces 8 calls with the expected variety. (verified live via `python -m backend.db.seed_demo_calls` against a real `calendar.db` — see `docs/fixes/2026-08-21-004.md` for why `-m` matters in this worktree.)
- [ ] `python eval/run_eval.py --label smoke_test` runs clean: prints deterministic stats + error rates, populates `call_error_flags`. **STILL BLOCKED — `classify_call_errors` is a real Claude call, and this environment's `.env` only has a placeholder `ANTHROPIC_API_KEY` value (a placeholder string, not a real one), not a real one — running against it would just produce upstream API failures, not a meaningful check.** The harness (argument parsing, call selection, tagging, error-rate computation) is fully verified in `eval/tests/test_run_eval_integration.py` with the Claude call mocked; `--help` and the CLI wiring were also smoke-tested live.
- [x] `pytest eval/tests backend/tests/test_seed_demo_calls.py backend/tests/test_admin_routes.py` — all pass.
- [ ] Each of the 4 error-class-specific seeded calls is confirmed flagged with its intended class (spot-check in the DB — the mocked unit tests only prove the harness works, not that a real judge call classifies correctly). **STILL BLOCKED — same placeholder-key reason as above, no real judge call possible here.** `eval/tests/test_run_eval_integration.py::test_run_eval_populates_eval_runs_and_call_error_flags` proves the harness attributes flags to the correct `call_id` when the classifier returns the expected class per fixture, which is the strongest check available without a real key.
- [x] Manual: `/admin` shows error-class badges on the calls list; clicking a flagged call shows its evidence text. (verified live: manually inserted a `repetition` flag on `demo-repetition-1` via the repository layer, confirmed it appears as a badge in `GET /api/calls` and with its evidence text in `GET /api/calls/{id}`.)
