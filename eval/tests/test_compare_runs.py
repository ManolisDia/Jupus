from backend.db.repositories import Repositories
from backend.tests.fakes import FakeAnnotationRepository, FakeCallRepository, FakeEvalRepository, FakeTraceRepository
from eval.compare_runs import build_comparison


def _repos():
    return Repositories(
        calls=FakeCallRepository(),
        slots=None,
        trace=FakeTraceRepository(),
        evals=FakeEvalRepository(),
        annotations=FakeAnnotationRepository(),
    )


def _tag_and_flag(repos, label, call_id, flagged_classes):
    repos.evals.tag_eval_run(call_id, label)
    for class_id in flagged_classes:
        repos.evals.add_error_flags(call_id, [{"error_class_id": class_id, "confidence": 0.9, "evidence": "x"}], label)


def test_compare_computes_delta_correctly():
    repos = _repos()
    _tag_and_flag(repos, "baseline", "c1", ["repetition"])
    _tag_and_flag(repos, "baseline", "c2", [])
    _tag_and_flag(repos, "candidate", "c3", ["repetition"])
    _tag_and_flag(repos, "candidate", "c4", ["repetition"])

    comparison = build_comparison(repos, "baseline", "candidate")

    row = next(r for r in comparison["rows"] if r["error_class_id"] == "repetition")
    assert row["baseline_rate"] == 0.5
    assert row["candidate_rate"] == 1.0
    assert round(row["delta"], 2) == 0.5


def test_compare_flags_regression_above_threshold():
    repos = _repos()
    _tag_and_flag(repos, "baseline", "c1", [])
    _tag_and_flag(repos, "candidate", "c2", ["repetition"])

    comparison = build_comparison(repos, "baseline", "candidate", threshold=0.1)

    assert comparison["any_regression"] is True
    row = next(r for r in comparison["rows"] if r["error_class_id"] == "repetition")
    assert row["regressed"] is True


def test_compare_no_regression_when_rates_stable_or_improved():
    repos = _repos()
    _tag_and_flag(repos, "baseline", "c1", ["repetition"])
    _tag_and_flag(repos, "candidate", "c2", [])  # candidate improved (0% vs 100%)

    comparison = build_comparison(repos, "baseline", "candidate")

    assert comparison["any_regression"] is False


def test_compare_same_label_against_itself_is_zero_delta():
    repos = _repos()
    _tag_and_flag(repos, "baseline", "c1", ["repetition"])

    comparison = build_comparison(repos, "baseline", "baseline")

    assert comparison["any_regression"] is False
    for row in comparison["rows"]:
        assert row["delta"] == 0.0
