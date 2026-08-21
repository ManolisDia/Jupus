# Fixes Index

One entry per solved non-trivial bug. Grep this (and `docs/known-issues/`) before deep-diving into a new bug — it may already be solved or ruled out.

| Date | File | Summary |
|---|---|---|
| 2026-08-21 | [2026-08-21-001.md](2026-08-21-001.md) | Remote audio dropped out mid-reply during live calls — `semantic_vad` misdetecting ambient noise as speech; fixed with `near_field` noise reduction + lower eagerness |
| 2026-08-21 | [2026-08-21-002.md](2026-08-21-002.md) | Malformed email domain (`x@...com`) passed `validate_email` — regex didn't exclude dots from domain-label character class |
