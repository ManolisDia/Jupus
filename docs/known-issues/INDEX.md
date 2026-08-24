# Known Issues Index

Bugs investigated but not solved — what was ruled out, failed workarounds, current best hypothesis. If one of these later gets solved, move its file into `docs/fixes/`, refresh the title, and remove the entry here.

| Date | File | Summary |
|---|---|---|
| 2026-08-22 | [2026-08-22-001.md](2026-08-22-001.md) | Interrupted realtime response silently drops the `ask_supervisor` tool call — likely `semantic_vad`/`interrupt_response` race, no client-side recovery |
| 2026-08-24 | [2026-08-24-001.md](2026-08-24-001.md) | Real LLM judge never flags the 4 hand-seeded error-class demo calls — judge reads only `trace`, hand-seeded calls have no `trace_events` |
