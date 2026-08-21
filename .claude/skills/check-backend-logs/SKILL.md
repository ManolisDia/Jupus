---
name: check-backend-logs
description: Check the running local backend's logs and trace events without the user pasting them. Use whenever debugging a live call, a crash, a stuck WebSocket, or any "why did this happen" question about backend/uvicorn behavior.
---

# Checking backend logs

The user runs the backend locally via uvicorn. Don't ask them to paste terminal output — read it yourself.

## 1. Raw process log

The dev command should redirect output to a file:

```bash
uvicorn backend.app:app --reload > backend.log 2>&1
```

If `backend.log` doesn't exist yet, ask the user to relaunch with that redirect (or add a `run-backend` script that does it) before continuing — don't guess from memory.

Read it with `tail`/`Grep`, not by asking the user:

```bash
tail -n 100 backend.log
```

```
Grep pattern="ERROR|Traceback|WebSocketDisconnect" path="backend.log" -n -C 3
```

Reload cycles reset uvicorn's own startup banner but append new request/exception output — always check the tail, not the head, for the most recent activity.

## 2. Structured traces (the more useful source once Phase 2+ exists)

Every call has a `call_id` and a full ordered `trace_events` table (see `docs/phases/cross-cutting.md` section 0) — this is usually more useful than the raw log because it's structured per call: every tool call, retry, stage transition, and delivery decision.

Query it via the `sqlite` MCP server (already configured against `backend/db/calendar.db`, read-only use only — never write through it, and never suggest app code use it; all writes must go through the repository classes per `CLAUDE.md` rule #9):

```sql
SELECT event_type, node, payload_json FROM trace_events
WHERE call_id = ?
ORDER BY seq;
```

If you don't have the `call_id`, get the most recent one first:

```sql
SELECT call_id, started_at FROM calls ORDER BY started_at DESC LIMIT 5;
```

## 3. Correlating the two

`backend.log` tells you *that* something broke (stack trace, exception message). `trace_events` tells you *where in the call* it happened (which node, which tool, which retry). For any live-call bug: pull the trace for the relevant `call_id` first to find the failing node/tool, then grep `backend.log` around that timestamp for the actual exception detail.

## 4. Browser-side console logs

If the issue might be client-side (WebRTC/mic/ICE failures in `client/index.html`), use the `chrome-devtools` MCP server against the open tab instead of asking the user to open devtools and paste — it can read console messages and network activity directly.
