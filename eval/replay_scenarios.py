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

STATUS: the scripted utterances below mirror docs/scenarios.md, but S2/S3
need Phase 4's real booking node and S5/S6 need Phase 5's `multiple_areas`
classification value / `is_explicit_human_request` heuristic — neither is
merged into this branch yet (see backend/tests/test_scenarios.py's module
docstring for the same caveat on the mocked-suite side). Running this script
today will still create and tag all 6 calls — that's the point of a
regression harness, it should keep working as the pipeline fills in over
time — but S2/S3/S5/S6's calls simply won't reach the outcomes docs/
scenarios.md describes until those phases land.
"""

import argparse
import asyncio
import uuid

from backend import dispatcher
from backend.config import settings
from backend.db.repositories import Repositories, get_repositories

# Each scenario: a fresh call_id, then a sequence of caller utterances fed
# through dispatcher.on_ask_supervisor in order, exactly as a live caller
# would trigger them one ask_supervisor turn at a time.
SCENARIOS: dict[str, list[str]] = {
    "S1": [
        "I got let go from my job last week and I'm not sure if that was legal.",
        "Just info for now, thanks.",
    ],
    "S2": [
        "I need some help with my tenancy.",
        "It's about my flat.",
        "Alex Smith",
        "alex.smith@example.com",
        "Yes, that's right.",
        "5551234567",
        "Yes.",
        "Thursday afternoon",
        "Yes, that works.",
    ],
    "S3": [
        "I need some help with my tenancy.",
        "It's about my flat.",
        "Alex Smith",
        "alex.smith@example.com",
        "Yes, that's right.",
        "5551234567",
        "Yes.",
        "the pre-booked slot",
        "Yes, the alternative works.",
    ],
    "S4": [
        "I need to talk to someone.",
        "It's about my flat.",
        "uh, Alesh, maybe",
        "No, it's Alex Smith.",
        "alex at example dot com",
        "Yes, that's right.",
    ],
    "S5": [
        "I have an issue that's both about my job and my immigration status.",
    ],
    "S6": [
        "Can you just put me through to a real person?",
    ],
}


async def _replay_one(repos: Repositories, scenario_id: str, utterances: list[str], label: str) -> str:
    call_id = f"replay-{scenario_id.lower()}-{uuid.uuid4().hex[:8]}"
    for i, utterance in enumerate(utterances):
        await dispatcher.on_ask_supervisor(repos, call_id, f"tool-{i}", "replay", utterance)
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
