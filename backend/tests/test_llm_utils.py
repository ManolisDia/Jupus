import json

import pytest

from backend.supervisor.llm_utils import LLMCallFailed, call_claude_tool
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
