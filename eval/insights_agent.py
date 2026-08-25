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
from eval.pricing import estimate_claude_cost_usd, estimate_cost_usd

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


LATENCY_STAGES = ("stt_and_dialogue_decision", "supervisor_processing", "deferred_wait", "tts_first_audio", "total_perceived")


def _parse_ts(raw: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def _payload(event: dict) -> dict:
    """Normalizes the two shapes a trace_events row can arrive in: the real
    SQLiteTraceRepository's raw row (payload as a `payload_json` string) and
    FakeTraceRepository's in-memory row (payload already a dict) — both are
    valid `TraceRepository.get_trace` results and this module must read
    either without caring which repo produced it."""
    if "payload" in event:
        return event["payload"] or {}
    raw = event.get("payload_json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _stage_durations_for_call(events: list[dict]) -> dict[str, list[float]]:
    """Walks one call's trace_events in order, matching each ask_supervisor
    turn's boundary events into stage durations (ms). A turn missing an
    expected boundary (e.g. a telephony call before Phase 10 wired up the
    same events, or a turn where the caller never paused so speech_stopped
    never fired before ask_supervisor) simply contributes no data for that
    stage on that turn — never raises, never fabricates a number. Returns
    per-stage lists of durations found across every turn in this one call's
    trace (a call can have many ask_supervisor turns)."""
    stages: dict[str, list[float]] = {stage: [] for stage in LATENCY_STAGES}
    last_speech_stopped_ts: Optional[datetime] = None
    open_turns: dict[str, dict] = {}

    for event in events:
        event_type = event["event_type"]
        payload = _payload(event)
        ts = _parse_ts(event["ts"])

        if event_type == "speech_stopped":
            last_speech_stopped_ts = ts
        elif event_type == "ask_supervisor_received":
            tool_call_id = payload.get("tool_call_id")
            stt_ms = None
            if last_speech_stopped_ts is not None and ts is not None:
                stt_ms = max((ts - last_speech_stopped_ts).total_seconds() * 1000, 0.0)
            open_turns[tool_call_id] = {"ask_ts": ts, "stt_ms": stt_ms}
        elif event_type == "reply_deferred":
            turn = open_turns.get(payload.get("tool_call_id"))
            if turn is not None:
                turn["supervisor_end_ts"] = ts
        elif event_type in ("reply_delivered", "reply_ready"):
            # Two transports, one turn-end boundary. Under /bridge this is
            # reply_delivered, emitted once deliver_or_defer decided the caller
            # wasn't mid-sentence — hence the deferral bookkeeping below. Under
            # LiveKit (Phase 14) it's reply_ready: LiveKit's own turn-taking
            # owns that decision, so there is no deferral to account for and
            # deferred_wait is a real, honest zero rather than a missing
            # measurement.
            #
            # The other three stages depend on speech_stopped and
            # tts_first_audio, which the browser used to report over /bridge.
            # backend/transport/livekit_agent.py re-produces both agent-side
            # from LiveKit's user_state_changed / agent_state_changed, so all
            # five stages stay alive under the new transport. tts_first_audio
            # is in fact more accurate now: a real playout signal rather than
            # the old amplitude heuristic the browser had to infer it from.
            tool_call_id = payload.get("tool_call_id")
            turn = open_turns.get(tool_call_id)
            if turn is None:
                continue
            was_deferred = payload.get("was_deferred", False)
            supervisor_end_ts = turn.get("supervisor_end_ts") if was_deferred else ts
            deferred_wait_ms = float(payload.get("wait_ms", 0)) if was_deferred else 0.0

            supervisor_ms = None
            if turn.get("ask_ts") is not None and supervisor_end_ts is not None:
                supervisor_ms = max((supervisor_end_ts - turn["ask_ts"]).total_seconds() * 1000, 0.0)

            if turn.get("stt_ms") is not None:
                stages["stt_and_dialogue_decision"].append(turn["stt_ms"])
            if supervisor_ms is not None:
                stages["supervisor_processing"].append(supervisor_ms)
            stages["deferred_wait"].append(deferred_wait_ms)

            turn["supervisor_ms"] = supervisor_ms
            turn["deferred_wait_ms"] = deferred_wait_ms
        elif event_type == "tts_first_audio":
            tool_call_id = payload.get("tool_call_id")
            ms = payload.get("ms_since_reply_delivered")
            if ms is None:
                continue
            stages["tts_first_audio"].append(float(ms))
            turn = open_turns.pop(tool_call_id, None)
            if turn is not None:
                total = float(ms)
                for key in ("stt_ms", "supervisor_ms", "deferred_wait_ms"):
                    value = turn.get(key)
                    if value is not None:
                        total += value
                stages["total_perceived"].append(total)

    return stages


def latency_breakdown_percentiles(trace_repo: TraceRepository, call_ids: list[str]) -> dict[str, dict[str, float]]:
    """Pools _stage_durations_for_call's output across every given call_id,
    then computes {stage: {"p50": ..., "p95": ..., "avg": ...}} for each of
    LATENCY_STAGES, degrading any stage with zero data points to
    {"p50": 0.0, "p95": 0.0, "avg": 0.0} — same never-raises contract the
    function it replaces had. Reuses _percentile unchanged. "avg" (mean
    latency per turn) is a distinct, simpler-to-reason-about number from the
    p50/p95 pair — admin panel headline stat, not a replacement for the
    percentile view."""
    pooled: dict[str, list[float]] = {stage: [] for stage in LATENCY_STAGES}
    for call_id in call_ids:
        durations = _stage_durations_for_call(trace_repo.get_trace(call_id))
        for stage in LATENCY_STAGES:
            pooled[stage].extend(durations[stage])

    result = {}
    for stage in LATENCY_STAGES:
        values = pooled[stage]
        ordered = sorted(values)
        result[stage] = {
            "p50": _percentile(ordered, 0.50),
            "p95": _percentile(ordered, 0.95),
            "avg": (sum(values) / len(values)) if values else 0.0,
        }
    return result


def _cost_for_call(events: list[dict]) -> dict:
    """Sums every llm_usage and realtime_usage event in one call's trace
    into token totals, then converts via pricing.estimate_cost_usd. A call
    with zero usage events (e.g. abandoned before any real turn) returns
    all-zero totals and $0.00 — never raises."""
    claude_input_tokens = claude_output_tokens = 0
    claude_cache_write_tokens = claude_cache_read_tokens = 0
    claude_cost = 0.0
    realtime_audio_in = realtime_audio_out = realtime_text_in = realtime_text_out = 0

    for event in events:
        payload = _payload(event)
        if event["event_type"] == "llm_usage":
            input_tokens = payload.get("input_tokens", 0)
            output_tokens = payload.get("output_tokens", 0)
            cache_write_tokens = payload.get("cache_write_tokens", 0)
            cache_read_tokens = payload.get("cache_read_tokens", 0)
            claude_input_tokens += input_tokens
            claude_output_tokens += output_tokens
            claude_cache_write_tokens += cache_write_tokens
            claude_cache_read_tokens += cache_read_tokens
            # Phase 13, Decision 3 — priced per-event by whichever model
            # that specific call actually used (a per-tool override may mix
            # Sonnet and Haiku calls within one call_id), not assumed to be
            # uniformly Sonnet across the whole call. Falls back to Sonnet
            # for events recorded before this field existed.
            claude_cost += estimate_claude_cost_usd(
                payload.get("model", "claude-sonnet-5"),
                input_tokens, output_tokens, cache_write_tokens, cache_read_tokens,
            )
        elif event["event_type"] == "realtime_usage":
            realtime_audio_in += payload.get("input_audio_tokens", 0)
            realtime_audio_out += payload.get("output_audio_tokens", 0)
            realtime_text_in += payload.get("input_text_tokens", 0)
            realtime_text_out += payload.get("output_text_tokens", 0)

    realtime_cost = estimate_cost_usd(0, 0, realtime_audio_in, realtime_audio_out, realtime_text_in, realtime_text_out)

    return {
        "claude_input_tokens": claude_input_tokens,
        "claude_output_tokens": claude_output_tokens,
        "claude_cache_write_tokens": claude_cache_write_tokens,
        "claude_cache_read_tokens": claude_cache_read_tokens,
        "realtime_audio_input_tokens": realtime_audio_in,
        "realtime_audio_output_tokens": realtime_audio_out,
        "realtime_text_input_tokens": realtime_text_in,
        "realtime_text_output_tokens": realtime_text_out,
        "cost_usd": claude_cost + realtime_cost,
    }


def average_cost_per_call(calls: list[dict], trace_repo: TraceRepository) -> dict:
    """{"average_usd": ..., "p50_usd": ..., "p95_usd": ...} pooled across
    every given call via _cost_for_call + _percentile — same
    never-raises-on-empty-input contract as every other stat in this
    module."""
    costs = [_cost_for_call(trace_repo.get_trace(call["call_id"]))["cost_usd"] for call in calls]
    if not costs:
        return {"average_usd": 0.0, "p50_usd": 0.0, "p95_usd": 0.0}
    ordered = sorted(costs)
    return {
        "average_usd": sum(costs) / len(costs),
        "p50_usd": _percentile(ordered, 0.50),
        "p95_usd": _percentile(ordered, 0.95),
    }


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
        "latency": latency_breakdown_percentiles(repos.trace, call_ids),
        "cost": average_cost_per_call(calls, repos.trace),
    }
