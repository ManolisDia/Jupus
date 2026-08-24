"""python eval/replay_scenarios.py --label <name>

Drives the 6 canonical scenarios (docs/scenarios.md, S1-S6) through the real,
UNMOCKED pipeline (live Claude/OpenAI calls) — unlike backend/tests/
test_scenarios.py, which mocks every Claude-backed tools.py function for fast
CI-style checks. Tags each resulting call in eval_runs with (label,
scenario_id) so a fresh, comparable batch exists before/after a
prompt-engineering change: run once as a baseline (--label baseline), make
the change, run again (--label after-tweak), then
`python eval/compare_runs.py --baseline baseline --candidate after-tweak`.

See docs/phases/phase-6c-benevolent-dictator.md.

Phase 4/5 are now merged (real booking node, real multiple_areas
classification value, real is_explicit_human_request heuristic), so all 6
scenarios run against the real pipeline end to end, live Claude/OpenAI
calls and all. `dispatcher.process_supervisor_call` (the real dispatch
entry point) is awaited directly turn by turn rather than going through
`on_bridge_message`'s fire-and-forget `asyncio.create_task` wrapping, since
there's no real caller audio/VAD stream here to keep servicing between
turns — awaiting each turn directly keeps this script's utterances strictly
ordered. `send_over_bridge` will log a harmless "no active /bridge
connection" warning per turn, since there's no real WebSocket either; the
call's resulting state/trace/eval rows are what this script cares about.
"""

import argparse
import asyncio
import uuid

from backend import dispatcher
from backend.config import settings
from backend.db.repositories import Repositories, get_repositories

# Each scenario: a fresh call_id, then a sequence of caller utterances fed
# through dispatcher.process_supervisor_call in order, exactly as a live
# caller would trigger them one ask_supervisor turn at a time.
SCENARIOS: dict[str, list[str]] = {
    "S1": [
        "I got let go from my job last week and I'm not sure if that was legal.",
        "Just info for now, thanks.",
    ],
    "S2": [
        # NB: this already names "tenancy" explicitly, unlike S4's
        # deliberately vague opener — the real classify_practice_area call
        # confidently resolves it on this first turn alone (0.95+ confidence,
        # confirmed live), so no separate clarifying utterance is scripted
        # here. One WAS previously scripted ("It's about my flat.") but that
        # became actively harmful once Phase 7 (optimistic capture) started
        # asking about "name" within the same turn: with no shape-check gate
        # for "name", that utterance got optimistically accepted as a name
        # answer instead of being caught immediately the way the old
        # synchronous node_capture always did — see docs/fixes/2026-08-24-005.md.
        #
        # Utterance order also rewritten for Phase 7's actual shape: name,
        # email, and phone are now asked back-to-back with no confirm-back
        # in between (that's the whole point — see docs/phases/
        # phase-7-optimistic-capture.md) — email/phone's mandatory
        # confirmations are both deferred to the batched drain phase after
        # phone, not interleaved one field at a time the way the pre-Phase-7
        # script assumed. Matches backend/tests/test_scenarios.py's S2.
        "I need some help with my tenancy.",
        "Alex Smith",
        "alex.smith@example.com",
        "5551234567",
        "Yes, that's right.",  # drain item 1: confirm email
        "Yes.",  # drain item 2: confirm phone
        "Thursday afternoon",
        "Yes, that works.",
    ],
    "S3": [
        # See S2's comment above — same reasoning for both the dropped
        # clarifying utterance and the reordered fast-pass-then-drain shape.
        "I need some help with my tenancy.",
        "Alex Smith",
        "alex.smith@example.com",
        "5551234567",
        "Yes, that's right.",  # drain item 1: confirm email
        "Yes.",  # drain item 2: confirm phone
        # backend/db/repositories/sqlite_slots.py pre-books 10am and 2pm on
        # the first seeded business day for every area — this deterministically
        # collides so the alternative-slot branch of node_booking fires for real
        "tomorrow at 10am",
        "Yes, the alternative works.",
    ],
    "S4": [
        # NB: avoid heuristics.EXPLICIT_REQUEST_PHRASES here (e.g. "talk to
        # someone") — that would short-circuit straight to an S6-style
        # explicit-request escalation instead of an ordinary routing turn.
        "I think I need some legal advice.",
        "It's about my flat.",
        "uh, Alesh, maybe",
        "No, it's Alex Smith.",
        "alex at example dot com",
        # phone isn't part of docs/scenarios.md's original S4 transcript,
        # but is structurally required under Phase 7: email's confirm-back
        # only happens once the drain phase starts, which only begins once
        # every field (including phone) has been fast-asked about — see
        # test_scenarios.py's S4 for the identical restructuring.
        "555-123-4567",
        "Yes, that's right.",  # drain item: confirm email
    ],
    "S5": [
        "I have an issue that's both about my job and my immigration status.",
        # node_routing's "multiple_areas" branch only sets stage="escalation"
        # within its own turn (the graph runs exactly one node per invoke) —
        # node_escalation's own logic (summary, handoff note, final "ended"
        # stage) needs one more turn to actually run; see
        # backend/tests/test_scenarios.py's S5 test for the same two-turn shape.
        "(silence)",
    ],
    "S6": [
        "Can you just put me through to a real person?",
    ],
}


async def _replay_one(repos: Repositories, scenario_id: str, utterances: list[str], label: str) -> str:
    call_id = f"replay-{scenario_id.lower()}-{uuid.uuid4().hex[:8]}"
    for i, utterance in enumerate(utterances):
        await dispatcher.process_supervisor_call(repos, call_id, f"tool-{i}", utterance)
    repos.evals.tag_eval_run(call_id, label, scenario_id=scenario_id)
    return call_id


async def replay_all(repos: Repositories, label: str) -> dict[str, str]:
    results = {}
    for scenario_id, utterances in SCENARIOS.items():
        call_id = await _replay_one(repos, scenario_id, utterances, label)
        results[scenario_id] = call_id
        print(f"{scenario_id} -> {call_id}")
    return results


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    args = parser.parse_args(argv)

    repos = get_repositories(settings)
    results = asyncio.run(replay_all(repos, args.label))
    print(f"\nTagged {len(results)} call(s) with eval_run_label={args.label!r}, one per scenario.")


if __name__ == "__main__":
    main()
