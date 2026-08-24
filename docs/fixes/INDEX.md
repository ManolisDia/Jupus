# Fixes Index

One entry per solved non-trivial bug. Grep this (and `docs/known-issues/`) before deep-diving into a new bug — it may already be solved or ruled out.

| Date | File | Summary |
|---|---|---|
| 2026-08-21 | [2026-08-21-001.md](2026-08-21-001.md) | Remote audio dropped out mid-reply during live calls — `semantic_vad` misdetecting ambient noise as speech; fixed with `near_field` noise reduction + lower eagerness |
| 2026-08-21 | [2026-08-21-002.md](2026-08-21-002.md) | Malformed email domain (`x@...com`) passed `validate_email` — regex didn't exclude dots from domain-label character class |
| 2026-08-21 | [2026-08-21-003.md](2026-08-21-003.md) | Truncated Claude JSON response crashed `node_capture` uncaught — `call_claude_tool` only retried `anthropic.APIError`, not JSON parse failures |
| 2026-08-21 | [2026-08-21-004.md](2026-08-21-004.md) | Deferred `/bridge` reply silently dropped as "stale" by the very turn that produced it — staleness check tagged replies with the pre-invoke stage instead of the post-invoke stage |
| 2026-08-21 | [2026-08-21-005.md](2026-08-21-005.md) | Bare file-path script invocation (`python backend/db/seed_demo_calls.py`) silently pulled imports from a different checkout via a stale editable install — use `python -m backend.db.seed_demo_calls` instead |
| 2026-08-22 | [2026-08-22-001.md](2026-08-22-001.md) | Caller's first real question silently discarded — `node_greeting` was a content-blind stub that always replied with the same canned line; dispatcher now chains straight into the real node within the same turn |
| 2026-08-24 | [2026-08-24-003.md](2026-08-24-003.md) | `mark_call_abandoned` raced the per-call lock — a disconnect landing mid-turn could silently revert a real outcome back to "abandoned" or vice versa; now holds the lock like every other write |
| 2026-08-24 | [2026-08-24-004.md](2026-08-24-004.md) | Trace `seq` counter raced across the worker/event-loop threads and never survived a dev-server restart — now derived atomically from `MAX(seq)` under a lock, with a `UNIQUE` index as a backstop |
| 2026-08-24 | [2026-08-24-005.md](2026-08-24-005.md) | Background field verification's urgent-reask signal could never fire (compared a failed field against a `last_asked_field` it could never match), silently misattributing a later utterance to a stale failed field — confirmed live |
| 2026-08-24 | [2026-08-24-006.md](2026-08-24-006.md) | `response.create` colliding with an already-active response crashed the whole call — client unconditionally tore down on any Realtime API error; now queues/avoids the collision and treats it as non-fatal |
| 2026-08-24 | [2026-08-24-007.md](2026-08-24-007.md) | Low-confidence email/phone reached a confirm-back that relied on Claude's discretion to spell it out, which wasn't reliable — now gated at extraction time, re-asked deterministically instead |
| 2026-08-24 | [2026-08-24-008.md](2026-08-24-008.md) | A leftover "yep, that's correct" reacting to the previous question got misattributed as the research intro's answer, silently burning the citation — `node_research_gather` had no plausibility gate at all |
