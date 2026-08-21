# Phase 1 — Raw Voice Loop

## Goal

Prove the audio round-trip works — browser mic → OpenAI Realtime → spoken reply — with zero business logic. Every later phase depends on this working reliably; do not proceed to Phase 2 until this is solid.

## Non-goals (explicitly do NOT build these in this phase)
- No `ask_supervisor` tool, no `/bridge` WebSocket, no LangGraph, no database. The Realtime session in this phase has **zero tools**.
- No admin panel, no transcript persistence.

## Prerequisite reading
`CLAUDE.md` (architecture doctrine), `docs/PLAN.md` (call sequence steps 1–5 only — steps 6–12 are Phase 2+).

---

## Files to create

### `backend/config.py`
Pydantic `BaseSettings` (same pattern as Femca's `src/config.py` — one settings object, no scattered `os.environ` calls elsewhere in the codebase).

```python
class Settings(BaseSettings):
    openai_api_key: str
    anthropic_api_key: str
    port: int = 8000
    db_path: str = "backend/db/calendar.db"

    model_config = SettingsConfigDict(env_file=".env")
```
- Instantiated once as `settings = Settings()` at module load.
- If `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is missing, Pydantic raises `ValidationError` at import time — this is intentional fail-fast behavior, do not wrap it in a try/except that silently continues.

### `backend/app.py`
FastAPI app, this phase only needs one route.

```python
POST /session
  request body:  {"call_id": str}
  behavior:
    - calls OpenAI's realtime session-create REST endpoint
      (POST https://api.openai.com/v1/realtime/sessions,
      Authorization: Bearer {settings.openai_api_key},
      body includes model + voice — confirm exact current field
      names against OpenAI's Realtime API docs at implementation
      time, this API has changed field names before)
    - on success, returns 200:
      {"client_secret": str, "session_id": str, "expires_at": str}
    - on missing/invalid key or OpenAI error response, returns 502
      with {"error": "<short message>"} — do not leak the raw
      OpenAI error body or the API key in the response
```
Use `httpx.AsyncClient` for the outbound call (already an implicit dependency of `fastapi`/`starlette`'s ecosystem; add explicitly to `pyproject.toml` if not already resolved).

### `client/index.html`
Minimal page: a "Start Call" button, a "End Call" button (disabled until a call is active), a status line ("idle" / "connecting" / "connected" / "error: ..."), an empty `<div id="transcript">` (unused until Phase 2, but present so later phases don't need a layout change), and a hidden `<audio id="remote-audio" autoplay>` element for playback.

### `client/app.js`
```
on Start Call click:
  1. call_id = crypto.randomUUID()
  2. POST /session {call_id} → {client_secret, session_id, expires_at}
  3. pc = new RTCPeerConnection()
  4. localStream = await navigator.mediaDevices.getUserMedia({audio: true})
  5. pc.addTrack(localStream.getAudioTracks()[0], localStream)
  6. pc.ontrack = (e) => remoteAudioEl.srcObject = e.streams[0]
  7. dataChannel = pc.createDataChannel("oai-events")
     dataChannel.onopen = () => sendSessionUpdate()  // see below
     dataChannel.onmessage = (e) => console.log("oai event", JSON.parse(e.data))
     // Phase 1: just log events to console. Phase 2 adds real handling.
  8. offer = await pc.createOffer(); await pc.setLocalDescription(offer)
  9. POST offer.sdp to OpenAI's realtime WebRTC endpoint with
     Authorization: Bearer {client_secret}, Content-Type: application/sdp
     → response body is answer SDP
  10. await pc.setRemoteDescription({type: "answer", sdp: answerSdp})
  11. update status to "connected"

sendSessionUpdate():
  send a "session.update" event on the data channel with:
    - instructions: short system prompt — greet the caller naturally,
      ask what they need; NO mention of tools in Phase 1 (none exist yet)
    - voice: pick one OpenAI Realtime voice
    - tools: []   (empty in Phase 1 — Phase 2 adds ask_supervisor)
    - turn_detection: {"type": "semantic_vad", "eagerness": "auto"}
      — NOT the default silence-duration "server_vad". This is the
      "caller says umm and pauses to think, don't barge in" requirement
      — confirm exact field names/allowed eagerness values against
      current OpenAI Realtime docs at implementation time (same caveat
      as elsewhere in this doc: this API's field names have shifted
      before). See docs/DECISIONS.md for why this was chosen over a
      custom model or a third-party turn-detection package.

on End Call click:
  - call teardown("idle")  // see below — same path as any failure teardown
```

### Connection-failure handling
Nothing above covers what happens if the connection dies *after* it's established — an ICE/network failure, OpenAI sending an error over the data channel, or the initial handshake itself failing. Left unhandled, the UI just hangs at "connecting"/"connected" forever with no indication anything's wrong, and — once `/bridge` exists in Phase 2 — the backend never finds out the call ended, leaking `CALL_STATES`/`CONNECTIONS` entries indefinitely (Phase 5's `mark_call_abandoned` only fires on a clean WebSocket disconnect, which a dead WebRTC leg doesn't necessarily trigger on its own). Build this in now, not as an afterthought:

```
function teardown(statusMessage):
  // the ONE path every failure and the normal "End Call" click both go
  // through — nothing above may close pc/ws directly outside this function
  if dataChannel: dataChannel.close()
  if pc: pc.close()
  if localStream: localStream.getTracks().forEach(t => t.stop())
  if ws (Phase 2+): ws.close()   // proactively close /bridge ourselves —
                                   // this is what lets the backend's existing
                                   // WebSocketDisconnect handler (Phase 5) do
                                   // its cleanup regardless of which side
                                   // (WebRTC leg or bridge leg) failed first
  update status to statusMessage

pc.oniceconnectionstatechange:
  if pc.iceConnectionState in ("failed", "disconnected", "closed"):
    teardown("error: connection lost")

pc.onconnectionstatechange:
  if pc.connectionState == "failed":
    teardown("error: connection failed")

dataChannel.onmessage (extend the Phase 1 console.log stub):
  parsed = JSON.parse(e.data)
  if parsed.type == "error":
    teardown("error: " + (parsed.error?.message or "realtime session error"))
    // confirm exact OpenAI error event shape against current docs —
    // same caveat as the function-call event names elsewhere in this doc

on any step in the Start Call sequence throwing (getUserMedia denied,
offer/answer POST failing, setRemoteDescription rejecting):
  catch it, call teardown("error: <short reason>") — never leave the UI
  stuck on "connecting" with an unhandled promise rejection in the console
```

Keep this file dependency-free (no bundler, no npm) — plain `<script>` tag, matches the "no Docker, minimal client" decision in `docs/DECISIONS.md`.

---

## Tests

### `backend/tests/test_session_endpoint.py`
All three cases mock the outbound OpenAI call (via `httpx_mock` or `unittest.mock.patch` on the client method) — **no live API calls in this test file, ever**.

1. `test_session_success_returns_client_secret` — mock OpenAI returning a valid session payload; assert the endpoint returns 200 with `client_secret`, `session_id`, `expires_at` present and correctly extracted from the mocked response shape.
2. `test_session_openai_error_returns_502` — mock OpenAI returning a 4xx/5xx; assert the endpoint returns 502 and the response body does not contain the string `settings.openai_api_key`'s value (i.e. the key never leaks into an error response).
3. `test_session_missing_call_id_returns_422` — POST with an empty body; assert FastAPI's validation returns 422 (this is default Pydantic/FastAPI behavior — the test exists to confirm the request model is actually enforced, not to add custom logic).

### `backend/tests/test_config.py`
1. `test_settings_raises_when_openai_key_missing` — instantiate `Settings` with an env lacking `OPENAI_API_KEY`, assert it raises.
2. `test_settings_loads_from_env` — instantiate with both keys set via monkeypatched env vars, assert values are read correctly.

---

## Definition of Done

- [x] `pip install -e ".[dev]"` completes with no errors.
- [x] `pytest backend/tests/test_session_endpoint.py backend/tests/test_config.py` — all pass, zero live network calls made during the run.
- [x] `uvicorn backend.app:app --reload` starts cleanly with a valid `.env`.
- [x] Manual: with the backend running, opening `client/index.html` and clicking "Start Call" reaches status "connected" within ~3 seconds.
- [x] Manual: speaking into the mic produces an audible spoken reply from the agent within a reasonable turnaround (no tool calls involved — this is pure conversation).
- [x] Manual: a 2-minute back-and-forth conversation produces zero unhandled exceptions in the backend terminal and zero uncaught errors in the browser console.
- [x] Manual: mid-sentence, pause for 2-3 seconds while saying "umm... let me think..." — confirm the agent does **not** jump in and start talking during the pause. This is the specific behavior `semantic_vad` is there to fix over default VAD — don't skip this check, it's easy for it to pass "close enough" on a quick test and still barge in under real hesitation patterns.
- [x] Manual: clicking "End Call" cleanly tears down the connection (no lingering mic indicator in the browser tab/OS after clicking it).
- [x] Manual: starting a call with an intentionally invalid `OPENAI_API_KEY` in `.env` shows the "error: ..." status in the UI rather than hanging silently or crashing the backend.
- [x] Manual: mid-call, disable your network adapter (or use browser devtools to simulate offline) for a few seconds — confirm the UI status changes to an "error: connection lost"-style message within a reasonable window, rather than staying stuck on "connected" indefinitely.
- [x] Manual: mid-call, deliberately deny the mic permission on a *second* call attempt after having previously granted it (or revoke it in browser settings and retry) — confirm `teardown()` fires with a clear error status rather than an unhandled promise rejection silently logged to console.
- [x] Manual: confirm every teardown path (normal End Call, ICE failure, mic-permission denial, invalid key) leaves zero lingering mic indicator in the browser tab/OS — not just the normal End Call path.

If any manual check fails intermittently rather than consistently, do not mark this phase done — log it in `docs/known-issues/` first (per `CLAUDE.md`'s rule on checking/logging before moving on), since Phase 2+ builds directly on this connection being reliable.
