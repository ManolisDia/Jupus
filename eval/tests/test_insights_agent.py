import json
from unittest.mock import patch

from backend.db.repositories import Repositories
from backend.tests.fakes import (
    FakeAnnotationRepository,
    FakeCallRepository,
    FakeEvalRepository,
    FakeTraceRepository,
)
from eval.insights_agent import (
    average_turns_per_call,
    booking_success_rate,
    compute_error_rates,
    escalation_reason_histogram,
    processing_latency_percentiles,
    run_classification_pass,
    run_taxonomy_critique,
)


def _call(outcome=None, escalation_reason=None, transcript=None, call_id="c1"):
    return {
        "call_id": call_id,
        "outcome": outcome,
        "escalation_reason": escalation_reason,
        "transcript_json": json.dumps(transcript if transcript is not None else []),
    }


def test_booking_success_rate_exact_value():
    calls = [
        _call(outcome="booked", call_id="c1"),
        _call(outcome="booked", call_id="c2"),
        _call(outcome="booked", call_id="c3"),
        _call(outcome="escalated", escalation_reason="capture_failed", call_id="c4"),
    ]
    assert booking_success_rate(calls) == 0.75


def test_booking_success_rate_zero_denominator():
    assert booking_success_rate([]) == 0.0
    assert booking_success_rate([_call(outcome="info_only")]) == 0.0


def test_escalation_histogram_counts_correctly():
    calls = [
        _call(outcome="escalated", escalation_reason="capture_failed", call_id="c1"),
        _call(outcome="escalated", escalation_reason="capture_failed", call_id="c2"),
        _call(outcome="escalated", escalation_reason="unable_to_classify", call_id="c3"),
        _call(outcome="booked", call_id="c4"),
    ]
    assert escalation_reason_histogram(calls) == {"capture_failed": 2, "unable_to_classify": 1}


def test_average_turns_per_call():
    calls = [
        _call(transcript=[{"role": "caller", "text": "a"}, {"role": "agent", "text": "b"}], call_id="c1"),
        _call(transcript=[{"role": "caller", "text": "a"}], call_id="c2"),
    ]
    assert average_turns_per_call(calls) == 1.5


def test_average_turns_per_call_empty_list():
    assert average_turns_per_call([]) == 0.0


def test_latency_percentiles_from_trace_events():
    trace_repo = FakeTraceRepository()
    # call-1: immediate delivery (0 wait), call-2: a deferred delivery.
    # record_event() stamps "now" for ts — this test needs known,
    # deterministic gaps, so events are pushed directly instead.

    def _push(call_id, event_type, ts, **payload):
        seq = trace_repo._seq_counters.get(call_id, 0)
        trace_repo._seq_counters[call_id] = seq + 1
        trace_repo.events.append(
            {"call_id": call_id, "seq": seq, "event_type": event_type, "node": None, "ts": ts, "payload": payload}
        )

    _push("call-1", "user_message", "2026-01-01T00:00:00+00:00")
    _push("call-1", "reply_delivered", "2026-01-01T00:00:00.200000+00:00", wait_ms=0)  # 200ms
    _push("call-2", "user_message", "2026-01-01T00:00:00+00:00")
    _push("call-2", "reply_delivered", "2026-01-01T00:00:01.000000+00:00", wait_ms=800)  # 1000ms, deferred

    result = processing_latency_percentiles(trace_repo, ["call-1", "call-2"])

    assert result["p50"] > 0
    assert result["p95"] >= result["p50"]
    # exact pooled values: [200, 1000] -> p50 halfway = 600, p95 close to 1000
    assert result["p50"] == 600.0
    assert round(result["p95"], 1) == 960.0


def test_latency_percentiles_no_data_returns_zeros():
    trace_repo = FakeTraceRepository()
    assert processing_latency_percentiles(trace_repo, []) == {"p50": 0.0, "p95": 0.0}
    assert processing_latency_percentiles(trace_repo, ["nonexistent"]) == {"p50": 0.0, "p95": 0.0}


# -- Phase 6b: classification pass ------------------------------------------


def _repos():
    return Repositories(
        calls=FakeCallRepository(),
        slots=None,
        trace=FakeTraceRepository(),
        evals=FakeEvalRepository(),
        annotations=FakeAnnotationRepository(),
    )


def test_classify_call_errors_mocked_returns_expected_shape():
    repos = _repos()
    calls = [_call(outcome="booked", call_id="c1")]
    mocked = {
        "flags": [
            {"error_class_id": "repetition", "confidence": 0.8, "evidence": "asked twice"},
            {"error_class_id": "unconfirmed_action", "confidence": 0.6, "evidence": "no read-back"},
        ]
    }
    with patch("backend.supervisor.tools.classify_call_errors", return_value=mocked):
        results = run_classification_pass(repos, calls, "label-a")

    assert results == [{"call_id": "c1", "flags": mocked["flags"]}]


def test_classify_call_errors_empty_result_is_valid():
    repos = _repos()
    calls = [_call(outcome="booked", call_id="c1")]
    with patch("backend.supervisor.tools.classify_call_errors", return_value={"flags": []}):
        results = run_classification_pass(repos, calls, "label-a")

    assert results == [{"call_id": "c1", "flags": []}]
    assert repos.evals.get_error_flags("c1") == []


def test_run_classification_pass_writes_one_row_per_flag():
    repos = _repos()
    calls = [_call(outcome="booked", call_id="c1"), _call(outcome="booked", call_id="c2")]
    responses = [
        {"flags": [{"error_class_id": "repetition", "confidence": 0.9, "evidence": "x"}]},
        {
            "flags": [
                {"error_class_id": "repetition", "confidence": 0.5, "evidence": "y"},
                {"error_class_id": "unconfirmed_action", "confidence": 0.7, "evidence": "z"},
            ]
        },
    ]
    with patch("backend.supervisor.tools.classify_call_errors", side_effect=responses):
        run_classification_pass(repos, calls, "label-a")

    total_rows = len(repos.evals.get_error_flags("c1")) + len(repos.evals.get_error_flags("c2"))
    assert total_rows == 3
    assert len(repos.evals.get_error_flags("c1")) == 1
    assert len(repos.evals.get_error_flags("c2")) == 2


def test_compute_error_rates_includes_zero_rate_classes():
    repos = _repos()
    calls = [_call(outcome="booked", call_id="c1"), _call(outcome="escalated", call_id="c2")]
    for call in calls:
        repos.evals.tag_eval_run(call["call_id"], "label-a")
    with patch(
        "backend.supervisor.tools.classify_call_errors",
        side_effect=[
            {"flags": [{"error_class_id": "repetition", "confidence": 0.9, "evidence": "x"}]},
            {"flags": []},
        ],
    ):
        run_classification_pass(repos, calls, "label-a")

    rates = compute_error_rates(repos, "label-a")
    assert rates["repetition"] == 0.5
    assert rates["premature_escalation"] == 0.0


# -- Phase 6c: taxonomy critique ---------------------------------------------


def test_propose_taxonomy_updates_receives_human_annotations():
    repos = _repos()
    repos.annotations.save_review(
        "c1", "benevolent_dictator", [], ["this call has a problem with no matching class"], "note", False
    )
    batch_results = [{"call_id": "c1", "flags": []}]

    with patch(
        "backend.supervisor.tools.propose_taxonomy_updates", return_value={"suggestions": []}
    ) as mock_propose:
        run_taxonomy_critique(repos, batch_results, "label-a")

    mock_propose.assert_called_once()
    human_annotations_arg = mock_propose.call_args.args[1]
    assert human_annotations_arg["c1"] is not None
    assert human_annotations_arg["c1"]["uncategorized_notes"] == [
        "this call has a problem with no matching class"
    ]


def test_calls_without_review_pass_empty_annotations():
    repos = _repos()
    batch_results = [{"call_id": "c1", "flags": []}]

    with patch(
        "backend.supervisor.tools.propose_taxonomy_updates", return_value={"suggestions": []}
    ) as mock_propose:
        result = run_taxonomy_critique(repos, batch_results, "label-a")

    assert result == []
    human_annotations_arg = mock_propose.call_args.args[1]
    assert human_annotations_arg["c1"] is None


def test_propose_taxonomy_updates_mocked_returns_expected_shape():
    repos = _repos()
    batch_results = [{"call_id": "c1", "flags": []}]
    mocked = {
        "suggestions": [
            {
                "suggestion_type": "new_class",
                "call_id": None,
                "related_error_class_id": None,
                "suggested_name": "caller_confusion",
                "rationale": "recurring pattern",
            }
        ]
    }
    with patch("backend.supervisor.tools.propose_taxonomy_updates", return_value=mocked):
        result = run_taxonomy_critique(repos, batch_results, "label-a")

    assert result == mocked["suggestions"]


def test_run_taxonomy_critique_writes_suggestion_rows():
    repos = _repos()
    batch_results = [{"call_id": "c1", "flags": []}]
    mocked = {
        "suggestions": [
            {"suggestion_type": "refine_existing", "call_id": "c1", "related_error_class_id": "repetition",
             "suggested_name": None, "rationale": "ambiguous wording"}
        ]
    }
    with patch("backend.supervisor.tools.propose_taxonomy_updates", return_value=mocked):
        run_taxonomy_critique(repos, batch_results, "label-a")

    assert len(repos.evals.list_taxonomy_suggestions("label-a", "pending")) == 1
