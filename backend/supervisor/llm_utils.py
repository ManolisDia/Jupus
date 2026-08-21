import json
import time
from typing import Callable

import anthropic

from backend.config import settings
from backend.db.repositories.base import TraceRepository
from backend.supervisor.tracing import traced_call

# claude-haiku-4-5: these calls are synchronous/blocking per conversational
# turn until Phase 5's async dispatcher exists, and each task here (4-way
# classification, single-field extraction) is simple enough that Haiku's
# speed matters more than Opus-tier reasoning. See docs/DECISIONS.md.
MODEL_ID = "claude-haiku-4-5"
RETRY_BACKOFF_SECONDS = 0.5

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)


class LLMCallFailed(Exception):
    pass


def call_claude_json(system: str, user_content: str, json_schema: dict) -> dict:
    response = _client.messages.create(
        model=MODEL_ID,
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_content}],
        output_config={"format": {"type": "json_schema", "schema": json_schema}},
    )
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def call_claude_text(system: str, user_content: str) -> str:
    response = _client.messages.create(
        model=MODEL_ID,
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return next(block.text for block in response.content if block.type == "text")


def call_claude_tool(
    trace_repo: TraceRepository, call_id: str, node: str, tool_name: str, fn: Callable, *args, **kwargs
):
    try:
        return traced_call(trace_repo, call_id, node, tool_name, fn, *args, **kwargs)
    except anthropic.APIError as e:
        trace_repo.record_event(call_id, "llm_retry", node=node, tool_name=tool_name, attempt=1, error=str(e))
        time.sleep(RETRY_BACKOFF_SECONDS)
        try:
            return traced_call(trace_repo, call_id, node, tool_name, fn, *args, **kwargs)
        except anthropic.APIError as retry_error:
            trace_repo.record_event(call_id, "llm_call_failed", node=node, tool_name=tool_name, error=str(retry_error))
            raise LLMCallFailed(retry_error) from retry_error
