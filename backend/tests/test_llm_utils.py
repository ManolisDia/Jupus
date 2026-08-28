import json

import pytest

from unittest.mock import patch

from backend.supervisor.llm_utils import (
    NO_THINKING,
    LLMCallFailed,
    NoTextBlock,
    _first_text_block,
    call_claude_tool,
)
from backend.tests.fakes import FakeTraceRepository


@pytest.fixture
def trace_repo():
    return FakeTraceRepository()


def test_call_succeeds_first_try(trace_repo):
    calls = []

    def fn(x):
        calls.append(x)
        return x * 2

    result = call_claude_tool(trace_repo, "call-1", "capture", "some_tool", fn, 21)

    assert result == 42
    assert len(calls) == 1


def test_call_retries_once_then_succeeds(trace_repo):
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise json.JSONDecodeError("truncated", "doc", 0)
        return "ok"

    result = call_claude_tool(trace_repo, "call-1", "capture", "some_tool", fn)

    assert result == "ok"
    assert len(calls) == 2


def test_call_raises_llm_call_failed_after_retry_exhausted(trace_repo):
    calls = []

    def fn():
        calls.append(1)
        raise json.JSONDecodeError("truncated", "doc", 0)

    with pytest.raises(LLMCallFailed):
        call_claude_tool(trace_repo, "call-1", "capture", "some_tool", fn)

    assert len(calls) == 2


class _Block:
    def __init__(self, type_, text=None):
        self.type = type_
        self.text = text


class _Usage:
    output_tokens = 512


class _Response:
    """A response that stopped on max_tokens with only a thinking block —
    exactly the shape claude-sonnet-5 returns when adaptive thinking eats the
    whole budget before the answer."""

    stop_reason = "max_tokens"
    usage = _Usage()
    content = [_Block("thinking")]


def test_no_text_block_raises_a_diagnosable_error_not_an_empty_one():
    # Regression: this was `next(block.text for block in ...)`, whose bare
    # StopIteration has an EMPTY str(). A real production failure logged
    # `success: false, error: ""` and could not be traced from the trace.
    with pytest.raises(NoTextBlock) as excinfo:
        _first_text_block(_Response())
    message = str(excinfo.value)
    assert message, "the whole point is that this error is never empty"
    assert "max_tokens" in message
    assert "thinking" in message
    assert "512" in message


def test_no_text_block_is_retryable(trace_repo):
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            return _first_text_block(_Response())
        return "ok"

    assert call_claude_tool(trace_repo, "call-1", "booking", "extract_datetime", fn) == "ok"
    assert len(calls) == 2
    retry = next(e for e in trace_repo.events if e["event_type"] == "llm_retry")
    # the retry event carried an empty error string before this fix, which is
    # exactly what made the live failure impossible to diagnose from the trace
    assert retry["payload"]["error"]
    assert "max_tokens" in retry["payload"]["error"]


def test_structured_calls_disable_thinking():
    # On claude-sonnet-5, OMITTING `thinking` opts INTO adaptive thinking. That
    # is what let a reasoning pass consume max_tokens and return no text block,
    # costing ~10s and a retry on a live booking turn.
    from backend.supervisor import llm_utils

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return type("R", (), {
            "content": [_Block("text", '{"ok": true}')],
            "stop_reason": "end_turn",
            "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
            "model": "claude-sonnet-5",
        })()

    with patch.object(llm_utils._client.messages, "create", fake_create):
        llm_utils.call_claude_json(system="s", user_content="u", json_schema={"type": "object"})
    assert captured["thinking"] == NO_THINKING == {"type": "disabled"}


def test_extract_datetime_prompt_pins_the_bare_weekday_reading():
    # "Friday" said ON a Friday resolved either way depending on whether the
    # model happened to reason about it. The prompt now states which it is.
    import datetime

    from backend.supervisor import prompts

    friday = datetime.date(2026, 8, 28)
    rendered = prompts.EXTRACT_DATETIME_PROMPT.format(
        today=friday.isoformat(),
        weekday=friday.strftime("%A"),
        next_same_weekday=(friday + datetime.timedelta(days=7)).isoformat(),
    )
    assert "Today is a Friday" in rendered
    assert '"Friday" on its own means 2026-09-04, NOT 2026-08-28' in rendered
