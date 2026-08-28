# Frontend

Two front ends, both plain HTML and vanilla JavaScript. **No build step, no framework, no bundler, no npm.** Edit a file, reload the page.

---

## The caller page — `client/`

Open `client/index.html` directly in a browser. It is not served by the backend.

### Files

| File | Role |
|---|---|
| `index.html` | The whole page: inline CSS, the orb canvas, three field tiles, the transcript panel, `<audio id="remote-audio">`, and four script tags |
| `app.js` | **Presentation only.** Status, the orb visualiser, transcript rendering, field tiles, teardown, backend-URL derivation |
| `livekit-transport.js` | Token fetch, room connect, mic publish, data and transcript handlers |
| `config.js` | Optional overrides. Gitignored. Local development needs none. |
| `config.example.js` | Documents the two overrides |

Script order matters. They are classic scripts, not modules, so top-level bindings in `app.js` are visible to `livekit-transport.js`, which calls `setStatus`, `appendTranscriptTurn`, `renderCallState`, `attachRemoteAnalyser` and `teardown` directly.

```html
<script src="https://unpkg.com/livekit-client@2.22.0/dist/livekit-client.umd.js"></script>
<script src="config.js"></script>
<script src="app.js"></script>
<script src="livekit-transport.js"></script>
```

> The LiveKit SDK is loaded from **unpkg at a pinned version**. The page needs internet access to load it, and `config.js` must exist or the browser logs a harmless 404.

### Backend URL derivation

```javascript
window.JUPUS_BACKEND_URL              // explicit override always wins
  ?? (hostname is "" | localhost | 127.0.0.1 ? "http://localhost:8000"
                                             : HOSTED_BACKEND_URL)
```

A `file://` page has an empty hostname — that is the documented local flow. This is derived rather than configured because `config.js` is gitignored and Firebase deploys whatever copy happens to be on the deploying machine's disk; hand-editing it before every deploy and remembering to change it back only has to be half-forgotten once to leave the hosted client silently pointing at localhost.

### The call flow

```javascript
callId = crypto.randomUUID();          // the call id is minted HERE
await startLiveKitCall(callId);        // POST /livekit-token → connect → enable mic
```

The room name is the call id. `setupVisualizer` takes the mic stream from **LiveKit's published microphone track** — the page no longer calls `getUserMedia` itself.

### The two incoming channels

**1. Call state** — data messages on topic `jupus.call_state`, published by the agent after every turn.

```javascript
room.on(RoomEvent.DataReceived, (payload, _p, _kind, topic) => {
  if (topic !== CALL_STATE_TOPIC) return;
  renderCallState(JSON.parse(new TextDecoder().decode(payload)));
});
```

Topic-scoped rather than sniffing every data message, so a future data topic cannot be misread as call state. It is `dispatcher.call_state_snapshot()`'s output, and it is **display-only and strictly one-way** — nothing the client does with it can reach back into the call. `renderCallState` sets each field tile's class to `status-missing` / `status-pending_confirm` / `status-confirmed`, which is what drives the grey → amber → green colouring.

**2. Transcripts** — LiveKit text streams on the reserved `lk.transcription` topic.

```javascript
room.registerTextStreamHandler("lk.transcription", async (reader, participant) => {
  const isAgent = participant?.identity !== "caller";   // "caller" is set by /livekit-token
  ...
});
```

Wrapped in `try/catch`: this is the client API most likely to move between SDK minors, and a missing transcript must never break the actual call.

The **"…" thinking bubble** hangs off the *caller's* final transcript, because that is when the turn starts and the supervisor round trip begins right after it. It is removed on the next agent transcript. This is deliberately driven by transcripts rather than the agent's published state attribute — the transcript uses the same two signals, so the bubble can never be stranded by an attribute name changing between SDK versions.

### The orb

A canvas animation: a base circle, a cyan ring for the caller and an indigo ring for the agent, both scaled by `averageAmplitude` of a Web Audio analyser.

Speaker state comes from **amplitude thresholds** (`> 0.05`), which is coarser than the Realtime VAD events the pre-Phase-14 client used to receive over the data channel. That is acceptable because it only drives the orb — **no turn-taking decision depends on it**, and turn-taking belongs to LiveKit and Realtime now regardless.

### Teardown

`teardown(statusMessage)` is the one path every failure and the normal "End Call" click both go through: disconnect the room, close the audio context, cancel the animation frame, reset the buttons. `teardownLiveKit` nulls `lkRoom` *first*, so the `Disconnected` handler can tell a deliberate hang-up from a lost connection.

### What was deleted in Phase 14

`app.js` used to own the entire transport: `RTCPeerConnection`, SDP offer/answer, ICE, the data channel, Realtime event parsing, `session.update`, the tool schema, a `responseActive`/`pendingResponseCreate` collision queue, and the `transcriptionPending`/`awaitingToolCall` race fix. All of it is either LiveKit's job now or has moved server-side to `backend/transport/`. **That deletion is most of the point of the phase** — and it also means the Realtime system prompt no longer ships as JavaScript to the caller's machine.

---

## The admin panel — `admin/`

Served by the backend at `http://localhost:8000/admin`. Five pages, same fetch-and-render style throughout.

### `index.html` / `app.js` — calls and insights

Two tabs.

**Calls** — `GET /api/calls` renders a table with outcome and error-class badges and a reviewed flag. Selecting a row fires `GET /api/calls/{id}` and `GET /api/calls/{id}/latency` in parallel and renders the transcript, the caller fields, the judge's flags with their evidence, any human review, and a per-call latency and cost breakdown. `GET /api/calls/{id}/trace` renders the full ordered decision trace.

**Insights** — `GET /api/eval/summary` for booking success rate, the escalation histogram, average turns, pooled latency percentiles and average cost; `GET /api/eval/taxonomy-suggestions?status=pending` for the suggestion queue, with approve and reject buttons.

`LATENCY_STAGE_LABELS` in `app.js` mirrors `eval.insights_agent.LATENCY_STAGES` exactly. **Add a stage there and you must add it here**, or it silently will not render.

### `graph.html` / `graph.js` — live supervisor

The largest admin file (626 lines). Pick a call, open `WS /admin/trace/{call_id}`, and watch it move through the graph in real time: nodes light up from `node_entered` / `node_exited`, and the `call_state` snapshots drive per-node sub-state badges — which field is pending, which slot is proposed.

That sub-state lives *inside* the capture and booking nodes rather than being its own graph node, by design (rule 2). This page is what makes it visible without promoting it.

### `annotate.html` / `annotate.js` — the Benevolent Dictator

`GET /api/eval/error-classes` builds the checklist, `GET /api/calls/unreviewed` is the queue, `GET /api/calls/{id}` shows the call, `POST /api/calls/{id}/review` saves.

The free-text "doesn't fit any class" note is the highest-value control on this page — it becomes a `human_annotations` row with `error_class_id = NULL`, which is the strongest input into the taxonomy-critique pass. See [`eval.md`](eval.md).

### `stress-test.html` / `stress-test.js`

`POST /api/stress-test/run` then `WS /admin/stress-test-stream/{run_id}`, rendering each concurrency level as it completes plus a final verdict. **One run at a time** — all runs share a stress-test DB file that is reset at the start of each.

### `db-viewer.html` / `db-viewer.js`

`GET /api/dev/tables` and `GET /api/dev/tables/{table}?limit=&offset=`. Paginated raw rows for debugging. The table name is checked against the `SCHEMA_TABLES` allowlist server-side.

---

## Working on the front end

- **No build.** Edit, hard-reload. Admin assets are served by `StaticFiles`, so a normal reload may serve a cached copy — use a hard reload.
- **Adding an admin page:** drop the `.html` and `.js` into `admin/`. `StaticFiles(html=True)` serves it automatically at `/admin/yourpage.html`. A path without `.html` needs its own route, which is why `/admin/annotate` exists explicitly.
- **Adding a field tile to the caller page:** add the markup with `data-field="x"` to `index.html`, add the field to `CallerProfile` and `FIELD_PRIORITY` in `state.py`, and make sure `call_state_snapshot` includes it — `renderCallState` iterates whatever the snapshot contains.
- **Keep business logic out.** The transport layer is thin by rule. The client renders what it is told and mints a call id; it decides nothing.
- **The hosted client is gated.** `window.JUPUS_ACCESS_TOKEN` in `config.js` is appended as `?access_token=` to the token request. Locally it is empty and the gate is a no-op.
- **Debugging:** `.mcp.json` configures a `chrome-devtools` MCP server, so an agent session can inspect the caller page's console and network activity directly instead of asking someone to paste devtools output.
