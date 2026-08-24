from unittest.mock import patch

from backend.db.repositories import Repositories
from backend.db.repositories.sqlite_annotations import SQLiteAnnotationRepository
from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.db.repositories.sqlite_eval import SQLiteEvalRepository
from backend.db.repositories.sqlite_trace import SQLiteTraceRepository
from backend.db.repositories.testing import create_in_memory_connection
from backend.db.seed_demo_calls import seed
from eval.run_eval import run

EXPECTED_FLAGGED = {
    "demo-repetition-1": "repetition",
    "demo-tool-failure-1": "tool_or_system_failure_surfaced",
    "demo-premature-escalation-1": "premature_escalation",
    "demo-unconfirmed-action-1": "unconfirmed_action",
}


def _fake_classify(call_row, trace, error_classes):
    class_id = EXPECTED_FLAGGED.get(call_row["call_id"])
    if class_id is None:
        return {"flags": []}
    return {"flags": [{"error_class_id": class_id, "confidence": 0.9, "evidence": "mocked"}]}


def _repos():
    conn = create_in_memory_connection()
    seed(conn)
    return Repositories(
        calls=SQLiteCallRepository(conn),
        slots=None,
        trace=SQLiteTraceRepository(conn),
        evals=SQLiteEvalRepository(conn),
        annotations=SQLiteAnnotationRepository(conn),
    )


def test_run_eval_populates_eval_runs_and_call_error_flags():
    repos = _repos()

    with (
        patch("backend.supervisor.tools.classify_call_errors", side_effect=_fake_classify),
        patch("backend.supervisor.tools.propose_taxonomy_updates", return_value={"suggestions": []}),
    ):
        result = run(repos, "test1", "new")

    assert len(result["calls"]) == 8
    for call_id in EXPECTED_FLAGGED:
        flags = repos.evals.get_error_flags(call_id)
        assert len(flags) == 1
        assert flags[0]["error_class_id"] == EXPECTED_FLAGGED[call_id]

    unflagged = [c["call_id"] for c in result["calls"] if c["call_id"] not in EXPECTED_FLAGGED]
    for call_id in unflagged:
        assert repos.evals.get_error_flags(call_id) == []


def test_deterministic_stats_match_seed_data():
    repos = _repos()

    with (
        patch("backend.supervisor.tools.classify_call_errors", side_effect=_fake_classify),
        patch("backend.supervisor.tools.propose_taxonomy_updates", return_value={"suggestions": []}),
    ):
        result = run(repos, "test1", "new")

    # 6 booked, 2 escalated out of 8 seeded calls
    assert result["deterministic"]["booking_success_rate"] == 6 / 8
    assert result["deterministic"]["escalation_reason_histogram"] == {"unable_to_classify": 2}


def test_rerun_with_all_flag_rejudges_existing_calls():
    repos = _repos()

    with (
        patch("backend.supervisor.tools.classify_call_errors", side_effect=_fake_classify),
        patch("backend.supervisor.tools.propose_taxonomy_updates", return_value={"suggestions": []}),
    ):
        run(repos, "a", "new")
        # a second run with --calls new should now find 0 new calls (all
        # already tagged in eval_runs, under label "a")
        result_new = run(repos, "b", "new")
        assert result_new["calls"] == []

        # --calls all re-judges every call regardless of prior runs
        result_all = run(repos, "c", "all")
        assert len(result_all["calls"]) == 8
