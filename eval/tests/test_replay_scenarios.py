from unittest.mock import AsyncMock, patch

from backend.db.repositories import Repositories
from backend.tests.fakes import FakeCallRepository, FakeEvalRepository, FakeTraceRepository
from eval.replay_scenarios import SCENARIOS, replay_all


def _repos():
    return Repositories(
        calls=FakeCallRepository(),
        slots=None,
        trace=FakeTraceRepository(),
        evals=FakeEvalRepository(),
        annotations=None,
    )


async def test_replay_creates_one_call_per_scenario():
    repos = _repos()

    # mock the pipeline entirely — this test file is harness-only, no live
    # API calls, per docs/phases/phase-6c-benevolent-dictator.md
    with patch("backend.dispatcher.process_supervisor_call", new=AsyncMock(return_value=None)):
        results = await replay_all(repos, "test-label")

    assert set(results.keys()) == set(SCENARIOS.keys())
    # S1-S6 plus S7's two variants (S7a, S7b), added alongside Phase 8's
    # case-research node — not hardcoded to 6 so this doesn't silently
    # break (or silently stop testing anything) the next time a scenario
    # is added or removed.
    assert len(results) == len(SCENARIOS)

    tagged = repos.evals.eval_runs.get("test-label", set())
    assert len(tagged) == len(SCENARIOS)
    assert tagged == set(results.values())
