import json

from backend.tests.fakes import FakeTraceRepository
from eval.insights_agent import (
    average_turns_per_call,
    booking_success_rate,
    escalation_reason_histogram,
    processing_latency_percentiles,
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
