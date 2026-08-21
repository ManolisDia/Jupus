"""The eval / insights agent — docs/phases/phase-6a-observability.md (this
sub-phase: the deterministic pass only). 6b extends this module with the
LLM-judge classification pass (`classify_call_errors`, `run_classification_pass`,
`compute_error_rates`), and 6c extends it further with the taxonomy-critique
pass (`run_taxonomy_critique`) — same incremental-extension pattern used for
`backend/dispatcher.py` across Phases 2 and 5.

Never imports sqlite3 directly (CLAUDE.md rule 9) — everything here goes
through the injected `Repositories` / `TraceRepository`.
"""

import json
from datetime import datetime
from typing import Optional

from backend.db.repositories import Repositories
from backend.db.repositories.base import TraceRepository


def booking_success_rate(calls: list[dict]) -> float:
    """Among calls that reached a real conclusion (outcome in booked/escalated),
    the fraction that were booked. 0.0 on an empty denominator, never raises."""
    concluded = [c for c in calls if c.get("outcome") in ("booked", "escalated")]
    if not concluded:
        return 0.0
    booked = sum(1 for c in concluded if c["outcome"] == "booked")
    return booked / len(concluded)


def escalation_reason_histogram(calls: list[dict]) -> dict[str, int]:
    """{escalation_reason: count} over calls with outcome == "escalated"."""
    histogram: dict[str, int] = {}
    for call in calls:
        if call.get("outcome") != "escalated":
            continue
        reason = call.get("escalation_reason") or "unknown"
        histogram[reason] = histogram.get(reason, 0) + 1
    return histogram


def average_turns_per_call(calls: list[dict]) -> float:
    """Mean transcript length (# of turns) across the given calls. 0.0 if
    `calls` is empty, never raises (including on a missing/malformed
    transcript_json — treated as 0 turns for that call rather than crashing
    the whole pass over one bad row)."""
    if not calls:
        return 0.0
    lengths = []
    for call in calls:
        raw = call.get("transcript_json")
        if not raw:
            lengths.append(0)
            continue
        try:
            lengths.append(len(json.loads(raw)))
        except (json.JSONDecodeError, TypeError):
            lengths.append(0)
    return sum(lengths) / len(lengths)


def _percentile(sorted_values: list[float], pct: float) -> float:
    # Nearest-rank method — simple, dependency-free, adequate for a local
    # eval tool's summary stats (not a load-testing-grade percentile impl).
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def processing_latency_percentiles(trace_repo: TraceRepository, call_ids: list[str]) -> dict[str, float]:
    """Round-trip latency (ms) for every ask_supervisor turn, pooled across
    the given call_ids: for each "reply_delivered" trace event, the gap
    between it and the closest preceding "user_message" event in the same
    call's sequence order. Returns {"p50": 0.0, "p95": 0.0} if there is no
    latency data at all — never raises."""
    latencies_ms: list[float] = []
    for call_id in call_ids:
        events = trace_repo.get_trace(call_id)
        last_user_message_ts: Optional[datetime] = None
        for event in events:
            if event["event_type"] == "user_message":
                last_user_message_ts = _parse_ts(event["ts"])
            elif event["event_type"] == "reply_delivered" and last_user_message_ts is not None:
                delivered_ts = _parse_ts(event["ts"])
                if delivered_ts is not None:
                    delta_ms = (delivered_ts - last_user_message_ts).total_seconds() * 1000
                    latencies_ms.append(max(delta_ms, 0.0))

    if not latencies_ms:
        return {"p50": 0.0, "p95": 0.0}

    ordered = sorted(latencies_ms)
    return {"p50": _percentile(ordered, 0.50), "p95": _percentile(ordered, 0.95)}


def _parse_ts(raw: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def run_deterministic_pass(repos: Repositories, calls: list[dict]) -> dict:
    """Combines the four metric functions above into the summary dict served
    by GET /api/eval/summary. `calls` is passed in (not re-fetched here) so
    callers can scope it — e.g. run_eval.py might scope to one eval_run_label
    once that concept exists from Phase 6b onward; this sub-phase always
    passes the full call list."""
    call_ids = [c["call_id"] for c in calls]
    return {
        "booking_success_rate": booking_success_rate(calls),
        "escalation_reason_histogram": escalation_reason_histogram(calls),
        "average_turns_per_call": average_turns_per_call(calls),
        "latency": processing_latency_percentiles(repos.trace, call_ids),
    }
