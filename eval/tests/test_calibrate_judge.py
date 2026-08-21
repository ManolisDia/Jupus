from backend.db.repositories import Repositories
from backend.tests.fakes import (
    FakeAnnotationRepository,
    FakeCallRepository,
    FakeEvalRepository,
    FakeTraceRepository,
)
from backend.supervisor.state import new_call_state
from eval.calibrate_judge import build_calibration


def _repos():
    return Repositories(
        calls=FakeCallRepository(),
        slots=None,
        trace=FakeTraceRepository(),
        evals=FakeEvalRepository(),
        annotations=FakeAnnotationRepository(),
    )


def _seed_call(repos, call_id):
    state = new_call_state(call_id)
    state["stage"] = "ended"
    state["booking_confirmed"] = True
    repos.calls.upsert(state, outcome_override="booked")


def test_calibration_true_positive_when_human_and_llm_agree():
    repos = _repos()
    _seed_call(repos, "c1")
    repos.annotations.save_review("c1", "bd", ["repetition"], [], "", False)
    repos.evals.add_error_flags("c1", [{"error_class_id": "repetition", "confidence": 0.9, "evidence": "x"}], "run-a")

    calibration = build_calibration(repos)

    assert calibration["per_class"]["repetition"]["true_positive"] == 1
    assert calibration["per_class"]["repetition"]["false_positive"] == 0
    assert calibration["per_class"]["repetition"]["false_negative"] == 0


def test_calibration_false_positive_when_llm_over_flags():
    repos = _repos()
    _seed_call(repos, "c1")
    repos.annotations.save_review("c1", "bd", [], [], "", False)  # BD found nothing
    repos.evals.add_error_flags("c1", [{"error_class_id": "repetition", "confidence": 0.9, "evidence": "x"}], "run-a")

    calibration = build_calibration(repos)

    assert calibration["per_class"]["repetition"]["false_positive"] == 1
    assert calibration["per_class"]["repetition"]["true_positive"] == 0


def test_calibration_false_negative_when_llm_misses_bd_flag():
    repos = _repos()
    _seed_call(repos, "c1")
    repos.annotations.save_review("c1", "bd", ["unconfirmed_action"], [], "", False)
    # LLM flagged nothing for this call

    calibration = build_calibration(repos)

    assert calibration["per_class"]["unconfirmed_action"]["false_negative"] == 1
    assert calibration["per_class"]["unconfirmed_action"]["true_positive"] == 0


def test_calibration_only_considers_reviewed_calls():
    repos = _repos()
    _seed_call(repos, "c1")  # never reviewed
    repos.evals.add_error_flags("c1", [{"error_class_id": "repetition", "confidence": 0.9, "evidence": "x"}], "run-a")

    calibration = build_calibration(repos)

    assert calibration["reviewed_call_count"] == 0
    assert calibration["per_class"]["repetition"]["false_positive"] == 0


def test_calibration_counts_uncategorized_notes_separately():
    repos = _repos()
    _seed_call(repos, "c1")
    repos.annotations.save_review("c1", "bd", [], ["something doesn't fit"], "", False)

    calibration = build_calibration(repos)

    assert calibration["uncategorized_note_count"] == 1
    assert calibration["reviewed_call_count"] == 1


def test_calibration_label_filter_scopes_llm_flags():
    repos = _repos()
    _seed_call(repos, "c1")
    repos.annotations.save_review("c1", "bd", ["repetition"], [], "", False)
    repos.evals.add_error_flags("c1", [{"error_class_id": "repetition", "confidence": 0.9, "evidence": "x"}], "run-a")
    repos.evals.add_error_flags("c1", [{"error_class_id": "unconfirmed_action", "confidence": 0.9, "evidence": "y"}], "run-b")

    calibration_a = build_calibration(repos, label="run-a")
    calibration_b = build_calibration(repos, label="run-b")

    assert calibration_a["per_class"]["repetition"]["true_positive"] == 1
    assert calibration_b["per_class"]["repetition"]["false_negative"] == 1
