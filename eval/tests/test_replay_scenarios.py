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
    with patch("backend.dispatcher.on_ask_supervisor", new=AsyncMock(return_value="ok")):
        results = await replay_all(repos, "test-label")

    assert set(results.keys()) == set(SCENARIOS.keys())
    assert len(results) == 6

    tagged = repos.evals.eval_runs.get("test-label", set())
    assert len(tagged) == 6
    assert tagged == set(results.values())
