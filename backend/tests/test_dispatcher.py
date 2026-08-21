from unittest.mock import patch

import pytest

from backend.db.repositories import Repositories
from backend.dispatcher import mark_call_abandoned, on_ask_supervisor
from backend.supervisor.state import CALL_STATES
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


@pytest.fixture(autouse=True)
def clear_call_states():
    CALL_STATES.clear()
    yield
    CALL_STATES.clear()


@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


async def test_creates_new_state_for_unseen_call_id(repos):
    assert "call-1" not in CALL_STATES
    await on_ask_supervisor(repos, "call-1", "tool-1", "wants info", "hi there")
    assert "call-1" in CALL_STATES
    # greeting node runs on the first call, leaving the state at "routing"
    assert CALL_STATES["call-1"]["stage"] == "routing"


async def test_reuses_existing_state_for_known_call_id(repos):
    await on_ask_supervisor(repos, "call-1", "tool-1", "wants info", "hi there")
    assert CALL_STATES["call-1"]["stage"] == "routing"

    with patch(
        "backend.supervisor.tools.classify_practice_area",
        return_value={"area": "employment", "confidence": 0.9},
    ):
        await on_ask_supervisor(repos, "call-1", "tool-2", "continuing", "employment law question")
    assert CALL_STATES["call-1"]["stage"] == "capture"


async def test_guards_against_ended_call(repos):
    await on_ask_supervisor(repos, "call-1", "tool-1", "r", "u")  # greeting -> routing
    CALL_STATES["call-1"]["stage"] = "ended"

    reply = await on_ask_supervisor(repos, "call-1", "tool-2", "r", "u")

    assert reply == "This call has already been completed."
    assert CALL_STATES["call-1"]["stage"] == "ended"


async def test_mark_call_abandoned_ends_in_progress_call(repos):
    await on_ask_supervisor(repos, "call-1", "tool-1", "r", "u")  # greeting -> routing

    mark_call_abandoned(repos, "call-1")

    assert CALL_STATES["call-1"]["stage"] == "ended"
    assert repos.calls.get("call-1")["outcome"] == "abandoned"
    assert any(e["event_type"] == "call_abandoned" for e in repos.trace.get_trace("call-1"))


async def test_mark_call_abandoned_noop_for_unseen_call(repos):
    # Should not raise even if the call never reached CALL_STATES
    mark_call_abandoned(repos, "never-started")
    assert repos.calls.get("never-started") is None
