from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.supervisor.llm_utils import call_claude_json, call_claude_tool
from backend.tests.fakes import FakeTraceRepository


@pytest.fixture
def trace_repo():
    return FakeTraceRepository()


def _mock_response(text='{"ok": true}', input_tokens=123, output_tokens=45):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def test_call_claude_tool_records_llm_usage_event(trace_repo):
    def fn():
        return call_claude_json("system", "user", {"type": "object"})

    with patch(
        "backend.supervisor.llm_utils._client.messages.create",
        return_value=_mock_response(input_tokens=123, output_tokens=45),
    ):
        call_claude_tool(trace_repo, "call-1", "capture", "extract_field", fn)

    events = trace_repo.get_trace("call-1")
    usage_events = [e for e in events if e["event_type"] == "llm_usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["payload"]["input_tokens"] == 123
    assert usage_events[0]["payload"]["output_tokens"] == 45


def test_last_usage_cleared_after_read(trace_repo):
    def fn_with_claude_call():
        return call_claude_json("system", "user", {"type": "object"})

    def fn_without_claude_call():
        return "deterministic result, no Claude call"

    with patch(
        "backend.supervisor.llm_utils._client.messages.create",
        return_value=_mock_response(),
    ):
        call_claude_tool(trace_repo, "call-1", "capture", "extract_field", fn_with_claude_call)

    call_claude_tool(trace_repo, "call-1", "booking", "check_availability", fn_without_claude_call)

    events = trace_repo.get_trace("call-1")
    usage_events = [e for e in events if e["event_type"] == "llm_usage"]
    # Only the first, real Claude-backed call should have recorded usage —
    # the second call must not carry over a stale value from the first.
    assert len(usage_events) == 1
    assert usage_events[0]["payload"]["tool_name"] == "extract_field"


def test_deterministic_fn_records_no_llm_usage_event(trace_repo):
    def fn():
        return "deterministic result, no Claude call"

    call_claude_tool(trace_repo, "call-1", "booking", "check_availability", fn)

    events = trace_repo.get_trace("call-1")
    assert [e for e in events if e["event_type"] == "llm_usage"] == []
