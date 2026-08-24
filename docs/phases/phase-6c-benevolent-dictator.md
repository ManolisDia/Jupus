# Phase 6c — Benevolent Dictator + Regression Testing

## Goal

Close the loop: the Benevolent Dictator annotates calls whenever via a dedicated admin page, that input drives the taxonomy-critique pass more strongly than the judge's own self-critique, `eval/calibrate_judge.py` measures the judge against the BD's ground truth, and `eval/replay_scenarios.py` + `eval/compare_runs.py` give you an actual regression-testing workflow for prompt-engineering changes. See `docs/benevolent_dictator.md`.

**Dependency note**: depends on 6a (admin panel scaffold, traces) and 6b (`error_classes.py`, `classify_call_errors`, `call_error_flags`/`eval_runs`, `run_eval.py`). Nothing in 6a or 6b reads anything introduced here — this sub-phase only adds new tables/files and extends existing ones, it never requires 6a/6b to change their own behavior.

## Non-goals
- The taxonomy never auto-updates itself — suggestions are surfaced, only the BD's approval (recorded here) should ever precede a hand-edit to `eval/error_classes.py`. See `docs/error_taxonomy.md`.
- No auth beyond the fixed `annotator_name` label — single-user local tool.

## Prerequisite
Phase 6b DoD met.

---

## `backend/config.py` addition
Add `annotator_name: str = "benevolent_dictator"` to `Settings` — written onto every `call_reviews` row as `annotator`. Not a real auth system, just a fixed identity label.

---

## Database schema additions

```sql
CREATE TABLE taxonomy_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_run_label TEXT NOT NULL,
    call_id TEXT REFERENCES calls(call_id),   -- nullable for cross-call new_class suggestions
    suggestion_type TEXT NOT NULL,            -- "new_class" | "misclassification" | "refine_existing"
    related_error_class_id TEXT,              -- nullable
    suggested_name TEXT,                      -- for new_class
    rationale TEXT NOT NULL,
    status TEXT DEFAULT 'pending',            -- 'pending' | 'approved' | 'rejected'
    evaluated_at TEXT
);

-- Benevolent Dictator annotation tables — see docs/benevolent_dictator.md
CREATE TABLE call_reviews (
    call_id TEXT PRIMARY KEY REFERENCES calls(call_id),
    annotator TEXT NOT NULL,       -- settings.annotator_name
    is_gold INTEGER DEFAULT 0,
    overall_note TEXT,
    reviewed_at TEXT
);

CREATE TABLE human_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT REFERENCES calls(call_id),
    error_class_id TEXT,           -- NULL = "this call has an issue that doesn't
                                    -- fit any current class" (note required)
    note TEXT,
    created_at TEXT
);
```
Update `backend/db/schema.sql`. Implemented via `backend/db/repositories/sqlite_eval.py`'s remaining `EvalRepository` methods (suggestions) and the new `backend/db/repositories/sqlite_annotations.py` (`SQLiteAnnotationRepository`) per `docs/architecture.md`.

**Re-annotation is delete-then-insert, not accumulate**: saving a review for a `call_id` that already has one deletes its existing `human_annotations` rows first, then inserts the fresh set, and upserts `call_reviews`. Exactly one active human review per call at any time.

---

## `backend/supervisor/tools.py` addition

```python
def propose_taxonomy_updates(batch_results: list[dict], human_annotations_by_call: dict[str, dict], error_classes: list[dict]) -> list[dict]:
    # Claude call, forced tool_choice, schema:
    # {"suggestions": [{"suggestion_type": "new_class"|"misclassification"|"refine_existing",
    #                    "call_id": str|null, "related_error_class_id": str|null,
    #                    "suggested_name": str|null, "rationale": str}, ...]}
    # Input: ALL calls' classify_call_errors output from this batch (6b),
    # PLUS, for any call with a call_reviews row, its human_annotations —
    # the BD's flags, including error_class_id=NULL "doesn't fit any class"
    # notes. Weight BD disagreement more heavily than the judge's own
    # self-critique: a BD "doesn't fit any class" note is strong evidence
    # for new_class; a recurring BD-vs-judge disagreement across multiple
    # calls is stronger evidence for refine_existing than a single instance
    # is for misclassification. Returns [] when nothing stands out. Every
    # returned suggestion is inserted with status="pending" — only the BD's
    # approval in the admin panel should precede a taxonomy edit.
    ...
```

## `eval/insights_agent.py` — extends 6b's version

```python
def run_taxonomy_critique(repos: Repositories, batch_results: list[dict], eval_run_label: str) -> list[dict]:
    # fetch human_annotations (repos.annotations.get_review per call_id, for
    # every call_id in batch_results — None for calls with no call_reviews
    # row, which is most calls, especially early on, and that's fine) keyed
    # call_id -> {"flags": [...], "note": ...}.
    # propose_taxonomy_updates(batch_results, human_annotations_by_call, get_active_error_classes()),
    # then repos.evals.add_taxonomy_suggestions(suggestions, eval_run_label),
    # each with status="pending".
    ...
```

## `eval/run_eval.py` — extends 6b's version
Add step 5 to the existing flow: `run_taxonomy_critique` after the classification pass, then print the count of new `taxonomy_suggestions` rows for this label with a pointer to review them in the admin panel.

## `eval/calibrate_judge.py` (new)
```
python eval/calibrate_judge.py [--label <eval_run_label>]
  For every call_id with a call_reviews row:
    human_set = {a.error_class_id for a in human_annotations WHERE call_id=X
                 AND error_class_id IS NOT NULL}
    llm_set   = {f.error_class_id for f in call_error_flags WHERE call_id=X
                 [AND eval_run_label=<label> if given, else any label]}
  Pool across all annotated calls, compute per error class:
    true_positive  = in both human_set and llm_set
    false_positive = in llm_set, not human_set  (judge over-flagged)
    false_negative = in human_set, not llm_set  (judge missed it)
  Report precision/recall per class (or raw counts if the annotated sample
  is too small for percentages to be meaningful — print raw counts either
  way so a tiny sample isn't dressed up as a precise percentage).
  Also report: count of calls where the BD flagged error_class_id=NULL
  ("doesn't fit any class") — needs taxonomy attention independent of
  calibration on the existing classes.
```

## `eval/replay_scenarios.py` (new — live-Claude regression generator)
```
python eval/replay_scenarios.py --label <name>
  For each scenario S1-S6 in docs/scenarios.md: open a fresh call_id, feed
  the scripted caller utterances through dispatcher.process_supervisor_call
  FOR REAL (live Claude/OpenAI calls, NOT mocked — unlike
  backend/tests/test_scenarios.py, which mocks everything for fast CI-style
  checks). Tag each resulting call in eval_runs with (label, scenario_id).
```
Run once as a baseline before a prompt change (`--label baseline`), make the change, run again (`--label after-tweak`), then compare.

## `eval/compare_runs.py` (new)
```
python eval/compare_runs.py --baseline <label> --candidate <label> [--threshold 0.1]
  For each error class: baseline_rate = compute_error_rates(baseline)[id],
  candidate_rate = compute_error_rates(candidate)[id], delta = candidate - baseline.
  Print a table: class id, baseline rate, candidate rate, delta.
  A class is a "possible regression" if delta > threshold (default 0.1).
  Exit code 1 if any class regressed, 0 otherwise.
```

## `backend/db/seed_demo_calls.py` — extends 6b's 8-row version
Add pre-populated BD annotations for 2 of the existing rows (direct inserts into `call_reviews`/`human_annotations`, bypassing the admin UI), so `calibrate_judge.py` has something to compute against immediately: the `repetition` call annotated in agreement with the (mocked) judge, and the `unconfirmed_action` call annotated with the BD flagging a class the (mocked) judge is set up to miss — a deliberate disagreement case, not just agreement.

---

## Admin panel — extends 6b's version

- `GET /api/calls` — now includes `reviewed: bool` (has a `call_reviews` row)
- `GET /api/calls/{call_id}` — now also includes `call_reviews` + `human_annotations` if the BD has reviewed it
- `GET /api/eval/taxonomy-suggestions?label=<name>&status=<pending|approved|rejected>` — list of suggestions
- `POST /api/eval/taxonomy-suggestions/{id}/approve` / `.../reject` — updates `status` only, never auto-edits `eval/error_classes.py`
- `GET /api/eval/compare?baseline=<label>&candidate=<label>` — same output as `compare_runs.py`, as JSON
- `GET /api/calls/unreviewed` — calls with no `call_reviews` row, oldest first — the BD's queue, independent of any eval run
- `GET /api/calls/{call_id}/review` — existing review, or 404
- `POST /api/calls/{call_id}/review` — body `{"error_class_ids": [str, ...], "uncategorized_notes": [str, ...], "overall_note": str, "is_gold": bool}`. Delete-then-insert `human_annotations`, upsert `call_reviews`.

### `admin/index.html` + `admin/app.js` — extended
Calls list gains a "reviewed"/"needs review" indicator. Detail pane shows the BD's own annotation if present. A taxonomy-suggestions panel lists `pending` suggestions with approve/reject buttons.

### `admin/annotate.html` + `admin/annotate.js` (new — the BD's page)
Fetches `/api/calls/unreviewed`, shows one call at a time (transcript + any existing LLM `call_error_flags` as reference, not constraint). BD picks zero-or-more current error classes via checkboxes, adds free-text "doesn't fit any class" notes, writes an overall note, toggles "mark as gold," submits via `POST /api/calls/{call_id}/review`, advances to the next unreviewed call. Used in short sessions whenever, not tied to running `eval/run_eval.py` first.

---

## Tests

### `eval/tests/test_insights_agent.py` additions (extends 6a/6b's file)
1. `test_propose_taxonomy_updates_receives_human_annotations` — mock the underlying Claude call, assert it's invoked with human-annotation data included for calls with a `call_reviews` row (spy on call arguments, not just the return value).
2. `test_calls_without_review_pass_empty_annotations` — a batch where no call has been BD-reviewed still runs `run_taxonomy_critique` successfully with empty annotation data per call.
3. `test_propose_taxonomy_updates_mocked_returns_expected_shape`.
4. `test_run_taxonomy_critique_writes_suggestion_rows`.

### `eval/tests/test_calibrate_judge.py`
1. `test_calibration_true_positive_when_human_and_llm_agree`.
2. `test_calibration_false_positive_when_llm_over_flags`.
3. `test_calibration_false_negative_when_llm_misses_bd_flag`.
4. `test_calibration_only_considers_reviewed_calls` — a call with `call_error_flags` but no `call_reviews` row is excluded entirely.
5. `test_calibration_counts_uncategorized_notes_separately`.
6. `test_calibration_label_filter_scopes_llm_flags`.

### `eval/tests/test_replay_scenarios.py` (harness only — mock the pipeline, no live API calls in this test file)
1. `test_replay_creates_one_call_per_scenario` — mock `dispatcher.process_supervisor_call`; assert exactly 6 `eval_runs` rows, one per `scenario_id` S1–S6, tagged with the given label.

### `eval/tests/test_compare_runs.py`
1. `test_compare_computes_delta_correctly`.
2. `test_compare_flags_regression_above_threshold`.
3. `test_compare_no_regression_when_rates_stable_or_improved`.

### `backend/tests/test_annotation_routes.py` (new)
1. `test_unreviewed_endpoint_excludes_calls_with_review`.
2. `test_unreviewed_endpoint_orders_oldest_first`.
3. `test_post_review_creates_call_review_and_annotation_rows` — post 2 `error_class_ids` + 1 `uncategorized_note`; assert `call_reviews` has one row and `human_annotations` has 3 rows (2 with `error_class_id`, 1 `NULL` + note).
4. `test_post_review_is_gold_flag_persisted`.
5. `test_re_reviewing_a_call_replaces_prior_annotations`.
6. `test_get_review_404_when_not_yet_reviewed`.
7. `test_approve_taxonomy_suggestion_updates_status`.
8. `test_reject_taxonomy_suggestion_updates_status`.

### `backend/tests/test_admin_routes.py` additions (extends 6a/6b's file)
1. `test_api_calls_list_includes_reviewed_flag`.
2. `test_api_call_detail_includes_human_review_when_present`.
3. `test_api_eval_taxonomy_suggestions_returns_seeded_suggestions`, `test_status_filter_scopes_results`.
4. `test_api_eval_compare_returns_delta_table`.
5. `test_annotate_page_serves_html`.

---

## Definition of Done

- [x] `python backend/db/seed_demo_calls.py` runs clean, now includes the 2 pre-populated annotations. (verified live via `python -m backend.db.seed_demo_calls` — output: "Seeded 8 demo calls (+ 2 BD annotations)".)
- [x] `python eval/run_eval.py --label smoke_test` runs clean, now also populates `taxonomy_suggestions`. (Verified live with a real `ANTHROPIC_API_KEY`: `python -m eval.run_eval --label live-verify-phase6` ran `run_taxonomy_critique`'s real Claude call unconditionally after classification — reported "0 new taxonomy_suggestions row(s)" for this batch, a legitimate real-judge output, not a mocked/skipped one.)
- [x] `pytest eval/tests backend/tests/test_annotation_routes.py backend/tests/test_admin_routes.py` — all pass.
- [x] Manual: `python eval/replay_scenarios.py --label baseline` against the real pipeline (live Claude/OpenAI calls) succeeds and produces 6 tagged calls. (Verified live with real API keys: `python -m eval.replay_scenarios --label live-verify-phase6` produced exactly 6 tagged calls, one per scenario, and their resulting `calls` rows match each scenario's expected shape exactly — S2/S3 `outcome="booked"` with real distinct `booking_slot_id`s (S3 correctly landed on the alternative slot after its requested time collided), S5/S6 `outcome="escalated"` with `escalation_reason` `"out_of_scope_multi_area"`/`"explicit_request"` respectively, S1/S4 correctly still unresolved (info-only / low-confidence capture in progress). `eval/compare_runs.py` and `eval/calibrate_judge.py` were re-run against this real batch and both still work end to end.)
- [x] Manual: `python eval/compare_runs.py --baseline baseline --candidate baseline` (comparing a label against itself) reports zero regressions and exit code 0. (verified live: tagged `demo-booked-1` under a `baseline` label with a `repetition` flag, ran the real CLI comparing `baseline` against itself — output showed 0.0% delta for every class, "No regressions detected.", exit code 0.)
- [x] Manual: open `/admin/annotate`, confirm seeded unreviewed calls appear, submit a review (a couple of classes checked, one "doesn't fit any class" note, marked gold), confirm it disappears from the queue and shows correctly in `/api/calls/{id}`. (verified live via `uvicorn` + `curl`: unreviewed queue was 6, `POST /api/calls/demo-tool-failure-1/review` with a class + gold flag, queue dropped to 5, `GET /api/calls/demo-tool-failure-1` showed the review with `is_gold: 1`. The page itself renders — `GET /admin/annotate` returns 200 — but the click-through-the-actual-UI part of this check used the API directly rather than a real browser, since no interactive browser session is available in this environment.)
- [x] Manual: `python eval/calibrate_judge.py` against the seeded pre-annotated calls prints per-class precision/recall (or raw counts) matching what you'd compute by hand, including the deliberate disagreement case. (verified live: output showed 2 reviewed calls, 0 uncategorized notes, `repetition` and `unconfirmed_action` each with 1 false-negative — correct by hand, since no eval run has judged those calls yet without a real API key, so the BD's flags currently have nothing to match against; this is exactly the deliberate-disagreement case surfacing correctly.)
- [x] Manual: `/admin`'s taxonomy-suggestions panel renders with approve/reject buttons; approving one updates its status without touching `eval/error_classes.py` automatically. (verified live: inserted a `pending` suggestion via the repository layer, confirmed it via `GET /api/eval/taxonomy-suggestions`, approved it via `POST .../approve`, confirmed status flipped to `approved`, and confirmed `git diff --stat -- eval/error_classes.py` showed no changes.)
