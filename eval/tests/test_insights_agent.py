import json
from unittest.mock import patch

import pytest

from backend.db.repositories import Repositories
from backend.tests.fakes import (
    FakeAnnotationRepository,
    FakeCallRepository,
    FakeEvalRepository,
    FakeTraceRepository,
)
from eval.insights_agent import (
    LATENCY_STAGES,
    _cost_for_call,
    _stage_durations_for_call,
    average_cost_per_call,
    average_turns_per_call,
    booking_success_rate,
    compute_error_rates,
    escalation_reason_histogram,
    latency_breakdown_percentiles,
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


def _push(trace_repo, call_id, event_type, ts, **payload):
    # record_event() stamps "now" for ts — these tests need known,
    # deterministic gaps, so events are pushed directly instead.
    seq = trace_repo._seq_counters.get(call_id, 0)
    trace_repo._seq_counters[call_id] = seq + 1
    trace_repo.events.append(
        {"call_id": call_id, "seq": seq, "event_type": event_type, "node": None, "ts": ts, "payload": payload}
    )


def test_stage_durations_computed_for_complete_turn():
    trace_repo = FakeTraceRepository()
    _push(trace_repo, "c1", "speech_stopped", "2026-01-01T00:00:00.000000+00:00")
    _push(trace_repo, "c1", "ask_supervisor_received", "2026-01-01T00:00:00.100000+00:00", tool_call_id="t1")  # 100ms
    _push(trace_repo, "c1", "reply_delivered", "2026-01-01T00:00:00.400000+00:00", tool_call_id="t1", was_deferred=False, wait_ms=0)  # 300ms
    _push(trace_repo, "c1", "tts_first_audio", "2026-01-01T00:00:00.400000+00:00", tool_call_id="t1", ms_since_reply_delivered=150)

    stages = _stage_durations_for_call(trace_repo.get_trace("c1"))

    assert stages["stt_and_dialogue_decision"] == [100.0]
    assert stages["supervisor_processing"] == [300.0]
    assert stages["deferred_wait"] == [0.0]
    assert stages["tts_first_audio"] == [150.0]
    assert stages["total_perceived"] == [550.0]


def test_immediate_delivery_has_zero_deferred_wait():
    trace_repo = FakeTraceRepository()
    _push(trace_repo, "c1", "ask_supervisor_received", "2026-01-01T00:00:00.000000+00:00", tool_call_id="t1")
    _push(trace_repo, "c1", "reply_delivered", "2026-01-01T00:00:00.100000+00:00", tool_call_id="t1", was_deferred=False, wait_ms=0)

    stages = _stage_durations_for_call(trace_repo.get_trace("c1"))

    assert stages["deferred_wait"] == [0.0]


def test_deferred_delivery_uses_existing_wait_ms_field():
    trace_repo = FakeTraceRepository()
    _push(trace_repo, "c1", "ask_supervisor_received", "2026-01-01T00:00:00.000000+00:00", tool_call_id="t1")
    _push(trace_repo, "c1", "reply_deferred", "2026-01-01T00:00:00.100000+00:00", tool_call_id="t1")
    _push(trace_repo, "c1", "reply_delivered", "2026-01-01T00:00:05.000000+00:00", tool_call_id="t1", was_deferred=True, wait_ms=777)

    stages = _stage_durations_for_call(trace_repo.get_trace("c1"))

    # deferred_wait must come from the reply_delivered payload's own wait_ms
    # field (Phase 5's single source of truth), not recomputed from the
    # timestamp gap (which would be ~4900ms here, not 777).
    assert stages["deferred_wait"] == [777.0]
    assert stages["supervisor_processing"] == [100.0]  # ask_supervisor_received -> reply_deferred


def test_missing_boundary_event_yields_no_data_for_that_stage_not_a_crash():
    trace_repo = FakeTraceRepository()
    # No speech_stopped at all for this turn (e.g. caller was already
    # mid-utterance when the call started).
    _push(trace_repo, "c1", "ask_supervisor_received", "2026-01-01T00:00:00.000000+00:00", tool_call_id="t1")
    _push(trace_repo, "c1", "reply_delivered", "2026-01-01T00:00:00.100000+00:00", tool_call_id="t1", was_deferred=False, wait_ms=0)

    stages = _stage_durations_for_call(trace_repo.get_trace("c1"))

    assert stages["stt_and_dialogue_decision"] == []
    assert stages["supervisor_processing"] == [100.0]


def test_multiple_turns_in_one_call_all_contribute():
    trace_repo = FakeTraceRepository()
    _push(trace_repo, "c1", "speech_stopped", "2026-01-01T00:00:00.000000+00:00")
    _push(trace_repo, "c1", "ask_supervisor_received", "2026-01-01T00:00:00.100000+00:00", tool_call_id="t1")
    _push(trace_repo, "c1", "reply_delivered", "2026-01-01T00:00:00.200000+00:00", tool_call_id="t1", was_deferred=False, wait_ms=0)
    _push(trace_repo, "c1", "speech_stopped", "2026-01-01T00:00:01.000000+00:00")
    _push(trace_repo, "c1", "ask_supervisor_received", "2026-01-01T00:00:01.300000+00:00", tool_call_id="t2")
    _push(trace_repo, "c1", "reply_delivered", "2026-01-01T00:00:01.500000+00:00", tool_call_id="t2", was_deferred=False, wait_ms=0)

    stages = _stage_durations_for_call(trace_repo.get_trace("c1"))

    assert stages["stt_and_dialogue_decision"] == [100.0, 300.0]
    assert stages["supervisor_processing"] == [100.0, 200.0]


def test_latency_breakdown_percentiles_empty_calls_returns_zeroed_stages():
    trace_repo = FakeTraceRepository()
    result = latency_breakdown_percentiles(trace_repo, [])
    assert result == {stage: {"p50": 0.0, "p95": 0.0, "avg": 0.0} for stage in LATENCY_STAGES}


def test_latency_breakdown_percentiles_pools_across_multiple_calls():
    trace_repo = FakeTraceRepository()
    _push(trace_repo, "c1", "ask_supervisor_received", "2026-01-01T00:00:00.000000+00:00", tool_call_id="t1")
    _push(trace_repo, "c1", "reply_delivered", "2026-01-01T00:00:00.200000+00:00", tool_call_id="t1", was_deferred=False, wait_ms=0)  # 200ms
    _push(trace_repo, "c2", "ask_supervisor_received", "2026-01-01T00:00:00.000000+00:00", tool_call_id="t2")
    _push(trace_repo, "c2", "reply_delivered", "2026-01-01T00:00:01.000000+00:00", tool_call_id="t2", was_deferred=False, wait_ms=0)  # 1000ms

    result = latency_breakdown_percentiles(trace_repo, ["c1", "c2"])

    assert result["supervisor_processing"]["p50"] == 600.0
    assert round(result["supervisor_processing"]["p95"], 1) == 960.0


def test_cost_for_call_sums_multiple_llm_usage_events():
    trace_repo = FakeTraceRepository()
    _push(trace_repo, "c1", "llm_usage", "2026-01-01T00:00:00+00:00", node="routing", input_tokens=100, output_tokens=50)
    _push(trace_repo, "c1", "llm_usage", "2026-01-01T00:00:01+00:00", node="capture", input_tokens=200, output_tokens=75)

    cost = _cost_for_call(trace_repo.get_trace("c1"))

    assert cost["claude_input_tokens"] == 300
    assert cost["claude_output_tokens"] == 125
    assert cost["cost_usd"] > 0


def test_cost_for_call_prices_mixed_models_at_their_own_rate():
    # Phase 13, Decision 3 — a per-tool Haiku override means one call_id's
    # llm_usage events can legitimately mix models; each must be priced at
    # its OWN rate, not all lumped in at Sonnet's (which would overstate
    # the whole point of using a cheaper model for that tool).
    from eval.pricing import estimate_claude_cost_usd

    trace_repo = FakeTraceRepository()
    _push(
        trace_repo, "c1", "llm_usage", "2026-01-01T00:00:00+00:00",
        node="capture", tool_name="extract_field", model="claude-sonnet-5", input_tokens=100, output_tokens=50,
    )
    _push(
        trace_repo, "c1", "llm_usage", "2026-01-01T00:00:01+00:00",
        node="booking", tool_name="select_offered_slot", model="claude-haiku-4-5-20251001",
        input_tokens=100, output_tokens=50,
    )

    cost = _cost_for_call(trace_repo.get_trace("c1"))

    expected = (
        estimate_claude_cost_usd("claude-sonnet-5", 100, 50)
        + estimate_claude_cost_usd("claude-haiku-4-5-20251001", 100, 50)
    )
    assert cost["cost_usd"] == pytest.approx(expected)
    # Haiku's real, lower rate must actually bite — this total must be less
    # than pricing BOTH events as Sonnet would produce.
    assert cost["cost_usd"] < 2 * estimate_claude_cost_usd("claude-sonnet-5", 100, 50)


def test_cost_for_call_includes_realtime_usage_even_without_supervisor_call():
    trace_repo = FakeTraceRepository()
    _push(
        trace_repo, "c1", "realtime_usage", "2026-01-01T00:00:00+00:00",
        tool_call_id=None, input_audio_tokens=1000, output_audio_tokens=500,
        input_text_tokens=10, output_text_tokens=5,
    )

    cost = _cost_for_call(trace_repo.get_trace("c1"))

    assert cost["realtime_audio_input_tokens"] == 1000
    assert cost["cost_usd"] > 0


def test_cost_for_call_includes_cache_usage_tokens():
    """Phase 13 (prompt caching) — cache_write_tokens/cache_read_tokens on
    an llm_usage event must be summed and priced, not silently dropped
    (they're billed at different rates than plain input/output tokens)."""
    trace_repo = FakeTraceRepository()
    _push(
        trace_repo, "c1", "llm_usage", "2026-01-01T00:00:00+00:00",
        node="capture", input_tokens=50, output_tokens=20,
        cache_write_tokens=800, cache_read_tokens=1500,
    )

    cost = _cost_for_call(trace_repo.get_trace("c1"))

    assert cost["claude_cache_write_tokens"] == 800
    assert cost["claude_cache_read_tokens"] == 1500
    assert cost["cost_usd"] > 0


def test_average_cost_per_call_empty_input_returns_zero():
    trace_repo = FakeTraceRepository()
    assert average_cost_per_call([], trace_repo) == {"average_usd": 0.0, "p50_usd": 0.0, "p95_usd": 0.0}


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


def test_run_classification_pass_skips_failed_call_without_aborting_batch():
    # A call whose judge call fails twice in a row (e.g. StopIteration from
    # a malformed/empty Claude response - the exact failure hit live against
    # a real call) must not abort the rest of the batch. Regression test for
    # a real bug: run_eval.py's classification loop used to have zero
    # per-call error handling, so one bad call crashed the whole script and
    # silently skipped every call after it.
    repos = _repos()
    calls = [_call(outcome="booked", call_id="fails"), _call(outcome="booked", call_id="fine")]
    with patch(
        "backend.supervisor.tools.classify_call_errors",
        side_effect=[StopIteration(), StopIteration(), {"flags": [{"error_class_id": "repetition", "confidence": 0.9, "evidence": "x"}]}],
    ):
        results = run_classification_pass(repos, calls, "label-a")

    assert results == [
        {"call_id": "fails", "flags": [], "classification_failed": True},
        {"call_id": "fine", "flags": [{"error_class_id": "repetition", "confidence": 0.9, "evidence": "x"}]},
    ]
    assert repos.evals.get_error_flags("fails") == []
    assert len(repos.evals.get_error_flags("fine")) == 1


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


def test_run_taxonomy_critique_returns_empty_list_on_llm_failure_without_crashing():
    # Same category of bug as run_classification_pass's fix above, but this
    # is a single call over the whole batch (not per-item) - there's no
    # "skip and continue", so failing here must degrade to no suggestions
    # rather than crash a script that already did real, valuable work
    # (the classification pass) before reaching this point.
    repos = _repos()
    batch_results = [{"call_id": "c1", "flags": []}]
    with patch(
        "backend.supervisor.tools.propose_taxonomy_updates",
        side_effect=[StopIteration(), StopIteration()],
    ):
        result = run_taxonomy_critique(repos, batch_results, "label-a")

    assert result == []
    assert repos.evals.list_taxonomy_suggestions("label-a", "pending") == []


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
