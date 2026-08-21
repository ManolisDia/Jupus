import time
from typing import Callable

from backend.db.repositories.base import TraceRepository


def summarize(value, max_len: int = 500):
    text = repr(value)
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def traced_call(trace_repo: TraceRepository, call_id: str, node: str, tool_name: str, fn: Callable, *args, **kwargs):
    start = time.monotonic()
    trace_repo.record_event(
        call_id, "tool_call_start", node=node, tool_name=tool_name, args=summarize((args, kwargs))
    )
    try:
        result = fn(*args, **kwargs)
        trace_repo.record_event(
            call_id,
            "tool_call_end",
            node=node,
            tool_name=tool_name,
            result_summary=summarize(result),
            duration_ms=int((time.monotonic() - start) * 1000),
            success=True,
        )
        return result
    except Exception as e:
        trace_repo.record_event(
            call_id,
            "tool_call_end",
            node=node,
            tool_name=tool_name,
            duration_ms=int((time.monotonic() - start) * 1000),
            success=False,
            error=str(e),
        )
        raise
