# Error Taxonomy — Design & Evolution

## Philosophy

Error classes should be derived from what you actually observe in real data. We don't have real data yet, so this seed taxonomy is a best guess based on where this specific architecture is most likely to fail — not a generic "voice agent error list." It's expected to change. The insights agent's job is explicitly two-fold: classify calls against the *current* taxonomy (`docs/phases/phase-6b-error-taxonomy.md`), and separately critique whether the taxonomy itself still fits what it's seeing — flag likely misclassifications, recurring problems that don't cleanly match any class, and candidate new/split/merged classes (`docs/phases/phase-6c-benevolent-dictator.md`). A human reviews those suggestions and edits `eval/error_classes.py` accordingly. **The system never auto-mutates its own taxonomy** — that would make the eval history impossible to reason about across runs.

## Seed classes (v1)

| id | Name | What it catches | Why we expect to see it here |
|---|---|---|---|
| `repetition` | Repeated question/request | Agent re-asks for a field already captured and `"confirmed"`, or repeats substantially the same question without the caller introducing new ambiguity or asking for clarification. | The per-field confirm-back loop (Phase 3) and the async deferred-delivery path (Phase 5) both create real opportunities for the agent to lose track of what it already has and ask again. |
| `tool_or_system_failure_surfaced` | Tool/system failure surfaced to caller | A generic fallback reply fired (`docs/phases/cross-cutting.md`'s `LLMCallFailed` path), an escalation with `reason="system_error"`, or a reply that doesn't logically follow from what the caller said. | The error-handling wrapper is designed to degrade gracefully, but "graceful" still means the caller noticed something went wrong — worth tracking how often, not just whether it crashed. |
| `premature_escalation` | Premature or unnecessary escalation | Call escalated to a human, but the transcript suggests the caller's need was actually answerable/resolvable. | Multiple independent escalation triggers exist (classification, capture, booking, explicit request, system error) — easy for one to fire too eagerly without this being caught by any single node's own logic. |
| `unconfirmed_action` | Action taken without confirmation | A consultation was booked, or a field was treated as `"confirmed"`, without an actual read-back-and-assent turn in the transcript. | This is a hard invariant we care about (`CLAUDE.md`) — the eval agent is the automated check that the invariant is actually holding in practice, not just in the code review. |

Each class is identified by a stable `id` — historical `call_error_flags` rows reference this id, so renaming an id (not just editing its description) breaks the ability to compare old and new eval runs. Prefer editing `name`/`description` over renaming `id`; if a class is retired, keep its id in the registry (marked inactive) rather than deleting it outright, so old runs stay interpretable.

## How the taxonomy is expected to evolve

After running the judge over a real batch of calls (`eval/run_eval.py`), review `taxonomy_suggestions` (see Phase 6c). A suggestion is one of:
- **`new_class`** — a recurring pattern across multiple calls that doesn't fit any current class well.
- **`misclassification`** — a specific `call_id` where the judge itself now believes it applied the wrong class, given cross-call context it didn't have per-call.
- **`refine_existing`** — an existing class's description should change (too broad, too narrow, ambiguous wording caused inconsistent application).

Suggestions are generated from two sources, not just the judge second-guessing itself: its own cross-call self-critique, and — the stronger signal — disagreement with the **Benevolent Dictator**'s human annotations (see `docs/benevolent_dictator.md`). Only the BD approves a suggestion; only an approved suggestion should ever result in a hand-edit to `eval/error_classes.py`. Re-run the judge (and `eval/calibrate_judge.py`) after a change to confirm it actually improved consistency, not just plausibility. This is a manual, reviewed loop — not automatic.
