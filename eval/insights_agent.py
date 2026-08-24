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
import logging
from datetime import datetime
from typing import Optional

from backend.db.repositories import Repositories
from backend.db.repositories.base import TraceRepository
from backend.supervisor import tools
from backend.supervisor.llm_utils import LLMCallFailed, call_claude_tool
from eval.error_classes import get_active_error_classes

logger = logging.getLogger(__name__)


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


def run_classification_pass(repos: Repositories, calls: list[dict], eval_run_label: str) -> list[dict]:
    """Phase 6b — the LLM-judge pass. For each call: classify_call_errors
    against the current active taxonomy, using the full trace as evidence,
    then persist any flags via repos.evals.add_error_flags (zero rows
    written if the call had no errors — that's valid). Returns the full
    per-call classification results (call_id + flags), which 6c's
    run_taxonomy_critique takes as input."""
    error_classes = get_active_error_classes()
    results = []
    for call in calls:
        call_id = call["call_id"]
        trace = repos.trace.get_trace(call_id)
        try:
            classification = call_claude_tool(
                repos.trace, call_id, "eval_judge", "classify_call_errors",
                tools.classify_call_errors, call, trace, error_classes,
            )
        except LLMCallFailed as e:
            # One call's judge call failing (e.g. a malformed/empty Claude
            # response) must not abort the whole batch — every other call in
            # this run still deserves its classification. llm_call_failed is
            # already recorded in this call's own trace by call_claude_tool;
            # just log and move on rather than losing the rest of the batch.
            logger.warning("classify_call_errors failed for call_id=%s: %s — skipping", call_id, e)
            results.append({"call_id": call_id, "flags": [], "classification_failed": True})
            continue
        flags = classification.get("flags", [])
        if flags:
            repos.evals.add_error_flags(call_id, flags, eval_run_label)
        results.append({"call_id": call_id, "flags": flags})
    return results


def compute_error_rates(repos: Repositories, eval_run_label: str) -> dict[str, float]:
    """Delegates to repos.evals.compute_error_rates — {error_class_id: rate}
    for every id in get_active_error_classes(), including 0.0 rates
    (meaningful information, not absence of information)."""
    return repos.evals.compute_error_rates(eval_run_label)


def run_taxonomy_critique(repos: Repositories, batch_results: list[dict], eval_run_label: str) -> list[dict]:
    """Phase 6c — the taxonomy-critique pass. Fetches any Benevolent Dictator
    annotation for every call_id in batch_results (None for calls with no
    call_reviews row — most calls, especially early on, and that's fine),
    then calls propose_taxonomy_updates with the batch's own classifications
    plus that human-annotation context. Every returned suggestion is
    persisted with status="pending" — only a human approving it in the admin
    panel should ever precede a hand-edit to eval/error_classes.py."""
    human_annotations_by_call: dict[str, Optional[dict]] = {}
    for result in batch_results:
        call_id = result["call_id"]
        review = repos.annotations.get_review(call_id)
        if review is None:
            human_annotations_by_call[call_id] = None
        else:
            human_annotations_by_call[call_id] = {
                "flags": [a["error_class_id"] for a in review["annotations"] if a["error_class_id"]],
                "note": review.get("overall_note"),
                "uncategorized_notes": [a["note"] for a in review["annotations"] if a["error_class_id"] is None],
            }

    try:
        proposal = call_claude_tool(
            repos.trace, "eval_run:" + eval_run_label, "eval_judge", "propose_taxonomy_updates",
            tools.propose_taxonomy_updates, batch_results, human_annotations_by_call, get_active_error_classes(),
        )
    except LLMCallFailed as e:
        # This is a single call over the whole batch, not per-item like
        # run_classification_pass above - there's nothing to "skip and
        # continue" to. But the taxonomy critique is a nice-to-have on top
        # of the classification pass that already ran and already persisted
        # its own results; failing here must not throw away that real work
        # or crash a script that's otherwise done everything it needed to.
        logger.warning("propose_taxonomy_updates failed for eval_run_label=%s: %s — no suggestions this run", eval_run_label, e)
        return []
    suggestions = proposal.get("suggestions", [])
    if suggestions:
        repos.evals.add_taxonomy_suggestions(suggestions, eval_run_label)
    return suggestions


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
