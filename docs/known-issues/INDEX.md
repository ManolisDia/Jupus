# Known Issues Index

Bugs investigated but not solved — what was ruled out, failed workarounds, current best hypothesis. If one of these later gets solved, move its file into `docs/fixes/`, refresh the title, and remove the entry here.

| Date | File | Summary |
|---|---|---|
| 2026-08-22 | [2026-08-22-001.md](2026-08-22-001.md) | Interrupted realtime response silently drops the `ask_supervisor` tool call — likely `semantic_vad`/`interrupt_response` race, no client-side recovery. (A related but distinct symptom — colliding with an active response crashing the whole call — is fixed, see `docs/fixes/2026-08-24-006.md`; this entry's own drop-on-cancellation subject is still open.) |
| 2026-08-24 | [2026-08-24-001.md](2026-08-24-001.md) | Real LLM judge never flags the 4 hand-seeded error-class demo calls — judge reads only `trace`, hand-seeded calls have no `trace_events` |
| 2026-08-24 | [2026-08-24-002.md](2026-08-24-002.md) | Shared `sqlite3.Connection` now reachable from multiple worker threads after the `asyncio.to_thread` dispatcher fix — thread-safety unverified, low practical risk for now |
| 2026-08-25 | [2026-08-25-001.md](2026-08-25-001.md) | Live call got stuck confirming the wrong field (`name`) against an utterance that was actually spelling out an email — loops until the caller gives up, never escalates despite repeated failures |
| 2026-08-25 | [2026-08-25-002.md](2026-08-25-002.md) | Several "fully mocked" scenario tests silently call the real Anthropic API on turn 1 (greeting→routing dispatch chaining) — pre-existing, not caused by Phase 13 |
| 2026-08-25 | [2026-08-25-003.md](2026-08-25-003.md) | `eval/replay_scenarios.py` intermittently loses the background statute-search task entirely (no error, no trace, no result) — not reproducible when the same utterances are driven directly, not root-caused |
