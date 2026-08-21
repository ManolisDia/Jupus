# Fixes Index

One entry per solved non-trivial bug. Grep this (and `docs/known-issues/`) before deep-diving into a new bug — it may already be solved or ruled out.

| Date | File | Summary |
|---|---|---|
| 2026-08-21 | [2026-08-21-001.md](2026-08-21-001.md) | Remote audio dropped out mid-reply during live calls — `semantic_vad` misdetecting ambient noise as speech; fixed with `near_field` noise reduction + lower eagerness |
| 2026-08-21 | [2026-08-21-002.md](2026-08-21-002.md) | Malformed email domain (`x@...com`) passed `validate_email` — regex didn't exclude dots from domain-label character class |
| 2026-08-21 | [2026-08-21-003.md](2026-08-21-003.md) | Truncated Claude JSON response crashed `node_capture` uncaught — `call_claude_tool` only retried `anthropic.APIError`, not JSON parse failures |
| 2026-08-21 | [2026-08-21-004.md](2026-08-21-004.md) | Deferred `/bridge` reply silently dropped as "stale" by the very turn that produced it — staleness check tagged replies with the pre-invoke stage instead of the post-invoke stage |
