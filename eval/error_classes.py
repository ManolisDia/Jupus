"""The editable error-taxonomy registry — docs/error_taxonomy.md,
docs/phases/phase-6b-error-taxonomy.md. This is the single source of truth
for the judge prompt (eval/insights_agent.py builds its classification prompt
from this list, not a separately-maintained copy of the descriptions) and for
the admin panel's taxonomy-suggestions UI (6c).

Each class's `id` is stable once used — historical `call_error_flags` rows
reference it, so renaming an `id` (not just its `name`/`description`) breaks
the ability to compare old and new eval runs. If a class is ever retired,
keep its `id` in this list (e.g. with an `"active": False` flag) rather than
deleting it outright — `get_active_error_classes()` is the hook for that,
even though nothing is inactive yet.

The taxonomy never auto-mutates itself: only a human (the Benevolent
Dictator, docs/benevolent_dictator.md) editing this file after approving a
`taxonomy_suggestions` row (6c) should ever change it.
"""

ERROR_CLASSES: list[dict] = [
    {
        "id": "repetition",
        "name": "Repeated question/request",
        "description": (
            "Agent re-asks for a field already captured and \"confirmed\", or repeats "
            "substantially the same question without the caller introducing new "
            "ambiguity or asking for clarification. The per-field confirm-back loop "
            "and the async deferred-delivery path both create real opportunities for "
            "the agent to lose track of what it already has and ask again."
        ),
    },
    {
        "id": "tool_or_system_failure_surfaced",
        "name": "Tool/system failure surfaced to caller",
        "description": (
            "A generic fallback reply fired (the LLMCallFailed graceful-degradation "
            "path), an escalation with reason=\"system_error\", or a reply that "
            "doesn't logically follow from what the caller said. The error-handling "
            "wrapper is designed to degrade gracefully, but \"graceful\" still means "
            "the caller noticed something went wrong — worth tracking how often, not "
            "just whether it crashed."
        ),
    },
    {
        "id": "premature_escalation",
        "name": "Premature or unnecessary escalation",
        "description": (
            "Call escalated to a human, but the transcript suggests the caller's need "
            "was actually answerable/resolvable. Multiple independent escalation "
            "triggers exist (classification, capture, booking, explicit request, "
            "system error) — easy for one to fire too eagerly without this being "
            "caught by any single node's own logic."
        ),
    },
    {
        "id": "unconfirmed_action",
        "name": "Action taken without confirmation",
        "description": (
            "A consultation was booked, or a field was treated as \"confirmed\", "
            "without an actual read-back-and-assent turn in the transcript. This is a "
            "hard invariant the architecture cares about — the eval agent is the "
            "automated check that the invariant is actually holding in practice, not "
            "just in the code review."
        ),
    },
]


def get_active_error_classes() -> list[dict]:
    """No inactive/retired classes yet — the hook exists for when one is
    retired per docs/error_taxonomy.md (an `"active": False` entry would be
    filtered out here rather than deleted from ERROR_CLASSES)."""
    return [c for c in ERROR_CLASSES if c.get("active", True)]
