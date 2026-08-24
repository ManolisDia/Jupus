"""Phase 8 (case research) — direct unit coverage for the research node's
mechanics and the dispatcher's background-search plumbing, complementing
the end-to-end S7 scenario in test_scenarios.py. See
docs/phases/phase-8-legal-research.md."""

from unittest.mock import patch

import pytest

from backend import dispatcher
from backend.db.repositories import Repositories
from backend.supervisor.graph import GRAPH, STATUTE_DISCLAIMER
from backend.supervisor.llm_utils import LLMCallFailed
from backend.supervisor.state import new_call_state
from backend.tests.fakes import FakeCallRepository, FakeTraceRepository


@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())


@pytest.fixture(autouse=True)
def clear_statute_searches():
    dispatcher.STATUTE_SEARCHES.clear()
    yield
    dispatcher.STATUTE_SEARCHES.clear()


def _research_state(research_phase, area="tenancy", utterance="some utterance", **overrides):
    state = new_call_state("call-1")
    state["stage"] = "research"
    state["research_phase"] = research_phase
    state["practice_area"] = area
    state["transcript"] = [{"role": "caller", "text": utterance, "ts": "t"}]
    state.update(overrides)
    return state


def _invoke(state, repos):
    return GRAPH.invoke(state, config={"configurable": {"repos": repos}})


# --- node_research_gather -----------------------------------------------


def test_research_gather_spawns_background_search_with_zero_llm_calls(repos):
    state = _research_state("gather", area="tenancy", utterance="My landlord is trying to evict me.")
    with patch("backend.supervisor.tools.ground_statute_citation") as mock_ground:
        result = _invoke(state, repos)
    mock_ground.assert_not_called()
    assert result["research_phase"] == "deliver"
    assert result["background_search_query"] == "My landlord is trying to evict me."
    assert "writing" in result["pending_reply"].lower()  # RESEARCH_FILLER_QUESTIONS["tenancy"]


def test_research_gather_skip_phrase_goes_straight_to_booking(repos):
    state = _research_state("gather", area="employment", utterance="Honestly, let's just book me in.")
    result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert "background_search_query" not in result or not result["background_search_query"]
    assert dispatcher.STATUTE_SEARCHES == {}


def test_research_gather_bare_affirmation_reasks_without_spawning_search(repos):
    # Regression for a real, live-reproduced bug: a caller's trailing "yep,
    # that's correct" (still reacting to the phone confirm-back right
    # before the capture->research handoff, which has no extra round-trip)
    # got misattributed to the NEW research intro question and silently
    # burned the one shot at a citation. Must re-ask instead of treating
    # this as the substantive answer.
    state = _research_state("gather", area="tenancy", utterance="Yep, that's correct.")
    with patch("backend.supervisor.tools.search_statute_candidates") as mock_search:
        result = _invoke(state, repos)
    mock_search.assert_not_called()
    assert result["research_phase"] == "gather"  # stays in gather, not advanced to deliver
    assert "background_search_query" not in result or not result["background_search_query"]
    assert result["retry_counts"]["research_gather"] == 1
    assert "caught that" in result["pending_reply"].lower()
    assert dispatcher.STATUTE_SEARCHES == {}


def test_research_gather_gives_up_after_second_bare_affirmation(repos):
    # Best-effort enrichment only (Decision 4, docs/phases/
    # phase-8-legal-research.md) — never loops or escalates over this.
    state = _research_state("gather", area="tenancy", utterance="Yeah, correct.")
    state["retry_counts"] = {"research_gather": 1}
    result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert result["retry_counts"]["research_gather"] == 2
    assert "day and time" in result["pending_reply"].lower()
    assert dispatcher.STATUTE_SEARCHES == {}


# --- node_research_deliver -----------------------------------------------


def test_research_deliver_includes_citation_and_disclaimer_when_found(repos):
    citation = {"citation": "Protection from Eviction Act 1977, s.5", "text": "...", "spoken_framing": "Landlords generally need to give four weeks' notice."}
    state = _research_state("deliver", statute_citation=citation)
    result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert citation["spoken_framing"] in result["pending_reply"]
    assert STATUTE_DISCLAIMER in result["pending_reply"]


def test_research_deliver_silent_when_no_citation_found(repos):
    state = _research_state("deliver", statute_citation=None)
    result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert STATUTE_DISCLAIMER not in result["pending_reply"]
    assert "day and time" in result["pending_reply"].lower()


def test_research_deliver_treats_unresolved_background_task_as_no_citation(repos):
    # statute_citation was never populated (still None, the new_call_state
    # default) because the background task simply hasn't resolved yet by
    # the time this turn runs — Decision 4: treated identically to "not
    # found", not blocked/awaited.
    state = _research_state("deliver")
    assert state["statute_citation"] is None
    result = _invoke(state, repos)
    assert result["stage"] == "booking"
    assert STATUTE_DISCLAIMER not in result["pending_reply"]


# --- dispatcher._search_statutes_in_background ---------------------------


async def test_search_statute_candidates_is_traced(repos):
    with patch("backend.supervisor.tools.search_statute_candidates", return_value=[]):
        await dispatcher._search_statutes_in_background(repos, "call-traced", "tenancy", "some utterance")
    events = repos.trace.get_trace("call-traced")
    event_types = [(e["event_type"], e["payload"].get("tool_name")) for e in events]
    assert ("tool_call_start", "search_statute_candidates") in event_types
    assert ("tool_call_end", "search_statute_candidates") in event_types


async def test_search_statutes_in_background_rejects_id_not_in_candidates(repos):
    fake_candidates = [
        {"id": "tenancy-poe1977-s5", "citation": "c1", "text": "t1", "score": 5.0, "jurisdiction": "x", "topic_tags": []},
    ]
    with (
        patch("backend.supervisor.tools.search_statute_candidates", return_value=fake_candidates),
        patch(
            "backend.supervisor.tools.ground_statute_citation",
            return_value={"selected_id": "not-a-real-id", "spoken_framing": "should never be used"},
        ),
    ):
        result = await dispatcher._search_statutes_in_background(repos, "call-1", "tenancy", "some utterance")
    assert result is None


async def test_search_statutes_in_background_returns_none_below_relevance_floor(repos):
    fake_candidates = [
        {"id": "x", "citation": "c1", "text": "t1", "score": 0.1, "jurisdiction": "x", "topic_tags": []},
    ]
    with (
        patch("backend.supervisor.tools.search_statute_candidates", return_value=fake_candidates),
        patch("backend.supervisor.tools.ground_statute_citation") as mock_ground,
    ):
        result = await dispatcher._search_statutes_in_background(repos, "call-1", "tenancy", "some utterance")
    mock_ground.assert_not_called()
    assert result is None


async def test_search_statutes_in_background_returns_none_on_grounding_failure(repos):
    fake_candidates = [
        {"id": "x", "citation": "c1", "text": "t1", "score": 5.0, "jurisdiction": "x", "topic_tags": []},
    ]
    with (
        patch("backend.supervisor.tools.search_statute_candidates", return_value=fake_candidates),
        patch("backend.supervisor.tools.ground_statute_citation", side_effect=LLMCallFailed("boom")),
    ):
        result = await dispatcher._search_statutes_in_background(repos, "call-1", "tenancy", "some utterance")
    assert result is None


async def test_search_failure_does_not_count_toward_system_error_escalation(repos):
    call_id = "call-escalation-check"
    from backend.supervisor.state import CALL_STATES

    CALL_STATES.pop(call_id, None)
    state = new_call_state(call_id)
    fake_candidates = [
        {"id": "x", "citation": "c1", "text": "t1", "score": 5.0, "jurisdiction": "x", "topic_tags": []},
    ]
    with (
        patch("backend.supervisor.tools.search_statute_candidates", return_value=fake_candidates),
        patch("backend.supervisor.tools.ground_statute_citation", side_effect=LLMCallFailed("boom")),
    ):
        await dispatcher._search_statutes_in_background(repos, call_id, "tenancy", "some utterance")
    assert state["consecutive_llm_failures"] == 0


def test_reconcile_statute_search_merges_finished_task(repos):
    import asyncio

    async def _fake_done_result():
        return {"citation": "c", "text": "t", "spoken_framing": "f"}

    call_id = "call-reconcile"
    state = new_call_state(call_id)

    async def _run():
        task = asyncio.create_task(_fake_done_result())
        await task  # ensure it's actually done before reconciling
        dispatcher.STATUTE_SEARCHES[call_id] = task
        dispatcher._reconcile_statute_search(state, call_id)

    asyncio.run(_run())
    assert state["statute_citation"] == {"citation": "c", "text": "t", "spoken_framing": "f"}
    assert call_id not in dispatcher.STATUTE_SEARCHES
