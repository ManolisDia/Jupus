import json
import threading
import time
from typing import Callable

import anthropic

from backend.config import settings
from backend.db.repositories.base import TraceRepository
from backend.supervisor.tracing import traced_call

# claude-sonnet-5: started on Haiku for latency (still synchronous/blocking
# per turn until Phase 5), but live testing showed it unreliably following
# precise instructions (e.g. converting spoken "at"/"dot" into @/.) even
# after repeated prompt tightening — upgraded per the revisit condition
# logged in docs/DECISIONS.md.
MODEL_ID = "claude-sonnet-5"
# Phase 13 (latency reduction), Decision 3 — a candidate override for
# specific closed-set classification tool calls (select_offered_slot,
# confirm_field_answer, confirm_booking_answer, classify_practice_area),
# never a project-wide default. Only used via call_claude_tool's `model=`
# kwarg, and only after a live-transcript eval/compare_runs.py check shows
# no error-class regression for that specific tool.
HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"
RETRY_BACKOFF_SECONDS = 0.5
# json.JSONDecodeError/StopIteration: a malformed or truncated response is
# functionally the same failure as an API error from the caller's
# perspective — retry it the same way, don't let it escape as an unhandled
# exception that kills the whole graph invocation.
RETRYABLE_ERRORS = (anthropic.APIError, json.JSONDecodeError, StopIteration)

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)


class LLMCallFailed(Exception):
    pass


# Phase 11 (latency + cost instrumentation), Decision 8 — a thread-local
# stash rather than threading call_id/trace_repo through every tools.py
# function that calls call_claude_json/call_claude_text. Safe because
# call_claude_tool's call to `fn` and any nested call_claude_json/
# call_claude_text call inside it always run synchronously in the same OS
# thread (each asyncio.to_thread invocation owns one worker thread for its
# own duration) — there is no cross-thread read of the stashed value, and
# it's cleared immediately after being read in call_claude_tool below, so a
# thread later reused by the pool never sees a stale value.
_last_usage = threading.local()

# Phase 13 (latency reduction), Decision 3 — same thread-local-stash shape
# as _last_usage above, and safe for the identical reason (call_claude_tool's
# call to `fn` and any nested call_claude_json/call_claude_text call inside
# it always run synchronously in the same OS thread). Set by call_claude_tool
# before invoking `fn`, read by call_claude_json/call_claude_text, cleared
# in call_claude_tool's `finally` so it can never leak into an unrelated
# later call on a reused worker thread.
_model_override = threading.local()


def _resolve_model() -> str:
    return getattr(_model_override, "value", None) or MODEL_ID


# Phase 13 (latency reduction), Decision 1 — every node's system prompt
# (backend/supervisor/prompts.py) is static per node and resent verbatim on
# every turn, so marking it as an ephemeral cache block costs nothing in
# correctness (the prompt content sent to the model is unchanged) and lets
# repeat calls within the ~5-minute TTL read it at a fraction of the input
# token cost instead of paying full price every turn.
def _cached_system_block(system: str) -> list[dict]:
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _record_usage(response) -> None:
    _last_usage.value = {
        # Read off the response itself, not the MODEL_ID constant — accurate
        # regardless of whether a per-call model override (Decision 3) was
        # in effect, which cost accounting depends on to price the right
        # model's tokens at the right rate (see eval/pricing.py).
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        # Anthropic reports cache activity as separate usage fields, not
        # folded into input_tokens — getattr'd defensively since a response
        # from a mocked/older test double may not carry them at all.
        "cache_write_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
        "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
    }


def call_claude_json(system: str, user_content: str, json_schema: dict, max_tokens: int = 512) -> dict:
    response = _client.messages.create(
        model=_resolve_model(),
        max_tokens=max_tokens,
        system=_cached_system_block(system),
        messages=[{"role": "user", "content": user_content}],
        output_config={"format": {"type": "json_schema", "schema": json_schema}},
    )
    _record_usage(response)
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def call_claude_text(system: str, user_content: str, max_tokens: int = 512) -> str:
    response = _client.messages.create(
        model=_resolve_model(),
        max_tokens=max_tokens,
        system=_cached_system_block(system),
        messages=[{"role": "user", "content": user_content}],
    )
    _record_usage(response)
    return next(block.text for block in response.content if block.type == "text")


def _run_and_record_usage(
    trace_repo: TraceRepository, call_id: str, node: str, tool_name: str, fn: Callable, *args, **kwargs
):
    # Cleared before every attempt (not just read-then-cleared after a
    # success) so a failed attempt whose exception propagates past this
    # point can never leave a stale value sitting in threading.local for
    # this worker thread's NEXT, unrelated call_claude_tool invocation to
    # pick up.
    _last_usage.value = None
    result = traced_call(trace_repo, call_id, node, tool_name, fn, *args, **kwargs)
    usage = getattr(_last_usage, "value", None)
    if usage is not None:
        trace_repo.record_event(call_id, "llm_usage", node=node, tool_name=tool_name, **usage)
        _last_usage.value = None
    return result


def call_claude_tool(
    trace_repo: TraceRepository, call_id: str, node: str, tool_name: str, fn: Callable, *args,
    model: str | None = None, **kwargs,
):
    # Phase 13, Decision 3 — `model` is consumed here, never forwarded to
    # `fn` itself (tools.py functions take no model parameter; they call
    # call_claude_json/call_claude_text, which read the override via the
    # thread-local instead). Cleared in `finally` so a retry attempt still
    # sees it (same call, same intended model) but no later, unrelated
    # call_claude_tool invocation on a reused worker thread ever could.
    _model_override.value = model
    try:
        try:
            return _run_and_record_usage(trace_repo, call_id, node, tool_name, fn, *args, **kwargs)
        except RETRYABLE_ERRORS as e:
            trace_repo.record_event(call_id, "llm_retry", node=node, tool_name=tool_name, attempt=1, error=str(e))
            time.sleep(RETRY_BACKOFF_SECONDS)
            try:
                return _run_and_record_usage(trace_repo, call_id, node, tool_name, fn, *args, **kwargs)
            except RETRYABLE_ERRORS as retry_error:
                trace_repo.record_event(call_id, "llm_call_failed", node=node, tool_name=tool_name, error=str(retry_error))
                raise LLMCallFailed(retry_error) from retry_error
    finally:
        _model_override.value = None
