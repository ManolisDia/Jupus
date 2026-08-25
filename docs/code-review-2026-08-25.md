# Full codebase review — 2026-08-25

Four-agent review across `backend/supervisor` + `dispatcher.py`, `backend/db/repositories` + `app.py`, `eval/`, and `admin/` + `client/` + `backend/tests/`, checked against the architecture doctrine and hard rules in `CLAUDE.md`. Findings ranked most-severe first.

## Doctrine compliance: PASS

All 9 hard rules in `CLAUDE.md` were checked with file:line evidence and found fully compliant — single `ask_supervisor` tool exposed to Realtime, deterministic Python conditionals for graph routing (no LLM-decided edges), plain-regex `validate_email`/`validate_phone`, async fire-and-forget dispatch with deferred delivery and staleness checks, every LLM call routed through `call_claude_tool`, every tool call (LLM-backed or deterministic) routed through `traced_call`, no `sqlite3`/raw SQL outside `backend/db/repositories/`, and taxonomy-suggestion approval only flips a DB status column — nothing auto-writes `eval/error_classes.py`. No violations found in any of the four areas.

## 1. Admin UI renders untrusted caller-derived fields unescaped (security)
**Files:** `admin/app.js:145-148, 234-239`, `admin/annotate.js:48`

`call.call_id`, `practice_area`, `outcome`, `escalation_reason`, and especially `caller_name`/`caller_email`/`caller_phone` are interpolated into `innerHTML` with no escaping, while the same files already escape transcript text and rationale/note fields via an existing `escapeHtml()` helper.

**Failure scenario:** A caller speaks or spells out HTML-looking content that STT transcribes and the field-capture extractor stores verbatim as their "name"/"email"/"phone". When an admin opens that call's detail view, the unescaped string is inserted via `innerHTML` and renders as markup instead of text — a caller-controlled XSS injection point into the admin panel.

## 2. Multi-statement writes in `sqlite_annotations.py`/`sqlite_eval.py` lack the lock `sqlite_trace.py` already needed (concurrency)
**Files:** `backend/db/repositories/sqlite_annotations.py:28-47`, `backend/db/repositories/sqlite_eval.py` (`add_error_flags`/`add_taxonomy_suggestions`)

Connections use `check_same_thread=False` (`connection.py:18`), and `sqlite_trace.py` already needed a `threading.Lock` after concurrent threads corrupted `seq` ordering — but `save_review` (delete + insert + N inserts) and the eval-flag/suggestion batch inserts have no equivalent lock around their multi-statement sequences.

**Failure scenario:** Two threads interleave statements from separate `save_review`/`add_error_flags` calls before either commits, corrupting a review's flag set or duplicating/dropping taxonomy suggestions — the same class of bug already found and fixed once in the trace-event path.

## 3. Foreign keys declared but not enforced (correctness)
**File:** `backend/db/repositories/connection.py:17-18`

`PRAGMA foreign_keys = ON` is never set per-connection, so `REFERENCES calls(call_id)` constraints declared in `schema.sql` are silent no-ops at the SQLite engine level.

**Failure scenario:** A bug elsewhere inserts a `call_error_flags` or `human_annotations` row referencing a `call_id` that was never created (or was later deleted), and nothing catches it — the row just sits there orphaned instead of the insert failing loudly.

## 4. No indexes on `call_id` foreign-key columns (efficiency, low severity)
**Files:** `calls`, `call_error_flags`, `eval_runs`, `human_annotations`, `call_reviews` tables in `schema.sql`

Only `trace_events` has an index (a uniqueness constraint, not a perf one). Not a correctness bug at current scale (local SQLite, dozens of demo calls) — a forward-looking note only.

## 5. Unvalidated selector interpolation in caller client (low risk)
**File:** `client/app.js:149`

`` `.field-tile[data-field="${field}"]` `` is built from `caller_profile` keys sent over the WS. These keys are server-controlled (name/email/phone field names, not caller-spoken values), so risk is low, but it's still string interpolation into a selector without validation.

---

## False alarm ruled out during synthesis

One agent flagged `dispatcher.py:66-70`'s `speech_stopped` handler as still missing its `record_event` call, based on reading the inline comment there. Direct inspection showed the call is present at line 70 — the comment narrates the history of a bug already fixed per `docs/fixes/2026-08-24-012.md`, not a live gap. No action needed.

---

*Generated from a 4-agent review (supervisor/graph/dispatcher doctrine compliance, DB repositories + app.py routes, eval/taxonomy system, admin/client UI + test coverage). No fixes applied yet.*
