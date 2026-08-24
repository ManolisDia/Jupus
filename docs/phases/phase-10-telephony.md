# Phase 10 — Real Telephony (OpenAI Realtime native SIP) + Warm Transfer

## Revision note

This doc originally specified Twilio Media Streams (backend-owned audio bridge, backend relays every audio frame between Twilio and a server-side OpenAI Realtime WebSocket). Revised after researching OpenAI's native SIP connectivity for the Realtime API (GA since August 2025): Twilio can SIP-trunk a call directly to OpenAI, so the backend never touches call audio at all. Chosen over the Media Streams design for two reasons (Decision 1): lower latency (~250–350ms end-to-end for native SIP vs. ~300–450ms for a media-relay bridge, per independent benchmarking), and because warm transfer turned out to be a real, documented, Twilio-published pattern under native SIP too — the concern that native SIP might make warm transfer materially harder didn't hold up under actual research. This also *simplifies* the backend's job versus the original draft: there is no audio pump loop, no format handling, nothing DSP-shaped to build at all. The backend's entire telephony footprint is webhooks, a control-only WebSocket (events, not audio), and Twilio's REST API for conference/participant orchestration.

## Goal

Give this agent a real phone number. A caller dials a real PSTN number, the call is SIP-trunked directly to the same OpenAI Realtime session type already driving the WebRTC path (same instructions, same single `ask_supervisor` tool, same supervisor graph on the other end of it) — and when the call escalates, the agent adds a real human to the call live, briefs them, and lets them take over, rather than just writing a handoff note and hoping. This is the one piece of the brief explicitly marked optional ("if you want to go the extra mile you can definitely wire up a telephony system anyway") and the one this project deliberately takes on anyway, because it's the most direct proof of the production-voice-agent experience this build exists to demonstrate.

Answers Question 4 (telephony/warm transfer/failure handling) with real code instead of design-only prose — `docs/answers.md`'s Q4 section should be rewritten once this phase is done to reference this doc's actual mechanisms.

## Prerequisite

Phase 5 (async dispatcher) DoD met — this phase depends on the fire-and-forget `process_supervisor_call`/`deliver_or_defer`/`SPEAKING`/`DEFERRED` machinery already existing and working. Read `backend/dispatcher.py` in full before starting; this phase's core design move (below) is making that file transport-agnostic, so understand its current WebRTC-only assumptions first. Not otherwise dependent on Phase 8/9 — can be built in parallel with either.

## Why this exists

Every other stretch in this project (eval/observability, case research, optimistic capture) makes the *reasoning* better. This one makes the *product* real — a browser tab with a mic is a fine way to prototype and evaluate a voice agent, but it is not what "production phone agent" means, and this build's stated purpose is to demonstrate exactly that. Building this for real, rather than answering Q4 in the abstract, is the difference between describing SIP failure handling and actually triggering a busy signal, watching the fallback fire, and hearing it happen.

## Non-goals

- **Not a custom audio bridge.** Explicitly superseded by Decision 1 — no Media Streams, no server-side audio relay, no transcoding. If native SIP's "actively evolving" feature surface turns out to block something load-bearing during implementation, falling back to a Media-Streams bridge is the documented contingency (see Decision 1's fallback note), not a silent scope cut.
- **Not a general multi-provider SIP abstraction.** Twilio-specific (SIP trunking to OpenAI's connector + REST API + TwiML + Conferences), documented as a deliberate vendor choice, same reasoning as the existing OpenAI/Anthropic two-vendor decision in `docs/DECISIONS.md`.
- **Not DTMF/IVR menus.** The whole premise of this project is "talk to it like a person," not press-1-for-employment.
- **Not call recording or transcription storage of the conference itself.** Recording introduces consent/retention questions out of scope here; only the existing caller-facing transcript/trace (already built) is persisted.
- **Not queueing/ACD/multiple on-call humans.** One fixed number in config (`settings.escalation_human_number`) is dialed for every warm transfer.
- **Not warm transfer for the WebRTC/browser channel.** Browser demo calls keep today's handoff-note-only escalation, completely unchanged — gated on `state["channel"] == "telephony"` (Decision 6, unchanged from the original draft).
- **Not re-litigating anything about the LangGraph supervisor, tool catalog, or graph nodes.** Zero changes to `graph.py`'s nodes/edges or `tools.py`'s existing tools.

## Decisions made, not left open for the implementer

**1. OpenAI's native SIP connectivity, not a Twilio Media Streams bridge.** Twilio still sells/hosts the phone number and terminates the actual PSTN call — that part is identical either way. The difference is what happens next: native SIP has Twilio SIP-trunk the call straight to OpenAI's SIP connector (`sip:$OPENAI_PROJECT_ID@sip.api.openai.com;transport=tls`), so audio never touches this backend. Chosen over a self-hosted Media Streams bridge for two independently-verified reasons: lower latency (~250–350ms end-to-end vs. ~300–450ms for a relay bridge — a real but modest difference, one benchmark source notes it "gets lost in normal network jitter" at the low end), and because it removes an entire category of backend work (audio pump loop, format/transcoding handling) that a bridge would require. Warm transfer — the thing that made a bridge look necessary in an earlier draft of this doc — turns out to be a documented pattern under native SIP too (Decision 5). **Fallback if this proves wrong during implementation**: native SIP is newer and "actively evolving" (per third-party assessment); if some part of the design below turns out to be blocked by an undocumented API gap, the Media-Streams-bridge design this doc originally specified is the fallback, not a redesign from scratch — keep this revision note as the record of why the swap happened either way.

**2. No audio-format decision needed — moot under native SIP.** The backend never sees raw call audio in either direction, so there's nothing to transcode and no dependency to add.

**3. The backend's only per-call connection to OpenAI is a control WebSocket carrying events, not audio.** When OpenAI's SIP connector receives the AI leg's inbound SIP INVITE (Decision 5 explains why there even *is* an AI leg to dial), OpenAI POSTs a `realtime.call.incoming` webhook to a backend-owned endpoint. The backend accepts the call (`POST /v1/realtime/calls/{openai_call_id}/accept`, same session config as the WebRTC path — same `SUPERVISOR_INSTRUCTIONS`, same single `ask_supervisor` tool schema, same voice) and opens `wss://api.openai.com/v1/realtime?call_id={openai_call_id}` to receive `response.function_call_arguments.done`/`speech_started`/`speech_stopped` events — exactly the same event types the browser's data channel already surfaces today, just delivered to the backend directly instead of relayed from client JS.

**4. `dispatcher.py` becomes transport-agnostic via one small interface — unchanged from the original draft's Decision 4.** `CallTransport` (a `Protocol` with `deliver_supervisor_result(tool_call_id, reply)`), `dispatcher.CONNECTIONS: dict[str, CallTransport]`. `backend/app.py`'s existing `/bridge` route wraps its `WebSocket` in `BrowserBridgeTransport`; telephony calls wrap their control WebSocket in `TelephonySupervisorChannel` (renamed from the original draft's `TelephonyCallSession` — no longer an audio bridge, just an event channel, so the name should say so). `dispatcher.send_over_bridge` calls `.deliver_supervisor_result(...)` either way — every dispatcher behavior built across Phases 5–8 (deferred delivery, staleness checks, the unhandled-exception catch-all) applies to telephony calls with zero duplicated logic.

**5. Every telephony call is a Twilio Conference from the moment it's answered — not a plain two-party call — because that's what makes warm transfer possible without re-plumbing call topology mid-call.** This is the actual mechanism (verified against a published Twilio tutorial doing exactly this): `POST /telephony/incoming` answers the caller's inbound call with TwiML that drops them into a Twilio Conference named after this call's `call_id`. In the same handler, the backend places a **second**, backend-initiated Twilio call — `client.calls.create(to="sip:{openai_project_id}@sip.api.openai.com;transport=tls", ...)` — whose TwiML also joins that same conference. That second call is the "AI leg": it rings OpenAI's SIP connector, which is what triggers the `realtime.call.incoming` webhook in Decision 3. From the caller's perspective this is invisible — they're in a conference with exactly one other party, the AI — but the conference structure is what lets a third party (the human) be added later without moving the caller to a different call.

**6. Warm transfer is telephony-only, gated on `CallState["channel"]`, not a separate escalation reason.** Unchanged from the original draft — `escalation_reason` keeps its existing five meanings; a warm-transfer attempt is an orthogonal side effect layered on top of any escalation reason, gated purely on transport.

**7. Two escalation reply templates, chosen by channel.** Unchanged from the original draft: WebRTC keeps `"I've passed this to our team, someone will follow up shortly."`; telephony speaks `"Let me try connecting you with someone on our team right now, one moment."` — because on a real call the caller's line stays open and a transfer is genuinely about to be attempted.

**8. Warm transfer, mechanically: add the human as a third Conference Participant, then remove the AI's leg.** On escalation, the backend calls Twilio's Participant API to dial `settings.escalation_human_number` directly into the same conference (`client.conferences(conference_sid).participants.create(from_=twilio_number, to=escalation_human_number, ...)`). A conference status callback (`POST /telephony/conference-events`) reports when that participant joins. Once the human joins, the backend removes the AI leg's participant from the conference (ends that one call leg only — the caller's own leg is untouched) — leaving caller and human directly bridged, exactly the "AI drops off once a human is briefed and connected" behavior warm transfer is supposed to produce. Briefing is spoken to the human leg before they're merged into the conference proper — a short `<Say>` of the same summary text `generate_call_summary` already produces for the handoff note, played as that leg's own TwiML before it joins (same idea as the original draft's briefing step, just via Twilio's native `<Say>`/`<Dial><Conference>` sequencing instead of a second Realtime session, which was already the original design's choice, not a downgrade).

**9. The original WebRTC-flavored escalation reply is repurposed as the transfer *failure* fallback, not replaced.** Unchanged from the original draft: if the human never joins (no answer, busy, retries exhausted), the caller's conference leg gets `"I've passed this to our team, someone will follow up shortly"` via `<Say>`, then the call ends.

**10. Failure handling at the Conference-Participant layer is this project's own design — Twilio's published pattern doesn't cover it, so it isn't "borrowed," it's built here specifically.** The reference tutorial this design is based on demonstrates the happy path (human joins, AI leaves) but explicitly does not address no-answer/busy/disconnect. Those are handled the same way the original Media-Streams draft specified: Twilio call-status callbacks on the human participant's leg (`no-answer`/`busy`/`failed`/`canceled` — Twilio's abstraction over the underlying SIP signaling, e.g. `busy` corresponds to a SIP 486) drive the fallback path (Decision 9); a mid-bridge disconnect (human leg's status flips to `completed` while the caller's conference leg is still active) triggers one re-engage-and-retry attempt (`TRANSFER_MAX_RECONNECT_ATTEMPTS = 1`) before falling through to the same fallback.

**11. Webhook signature verification is required on every inbound route this backend exposes, from either direction.** Twilio's webhooks (`/telephony/incoming`, `/telephony/conference-events`, `/telephony/human-status`) are verified via Twilio's standard `X-Twilio-Signature` + `RequestValidator`. OpenAI's `realtime.call.incoming` webhook (`/openai/sip-incoming`) is verified via OpenAI's own webhook-signing mechanism (confirm the exact header/verification helper against current OpenAI webhook docs at implementation time — same "verify against current docs, don't hardcode from a training-data snapshot" caution `docs/DECISIONS.md` already applies to Realtime event names elsewhere in this project). An unauthenticated webhook that can trigger state mutations, outbound calls, or accept an OpenAI SIP session on request is a real production concern, not a take-home nicety to skip.

**12. Hosting, not local setup, is how this gets evaluated.** Unchanged from the original draft's Decision 11: both Twilio's webhooks and OpenAI's SIP-incoming webhook need a publicly reachable URL. The core WebRTC path stays completely unaffected — local setup and the primary submission are untouched — and telephony is demoed via a number the project owner hosts (Railway + a stable domain, gated behind a shared-secret param, hard spend caps set on OpenAI/Anthropic/Twilio before going live), never something the evaluator needs their own Twilio/OpenAI-SIP-project credentials for.

---

## Config additions (`backend/config.py`)

```python
class Settings(BaseSettings):
    ...
    # Phase 10 (telephony) — all Optional, all default None. Telephony
    # routes check for these at request time and return a clear 503 if
    # unconfigured; the WebRTC path must keep working with zero telephony
    # config present, exactly as it does today.
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_number: Optional[str] = None
    escalation_human_number: Optional[str] = None
    # The OpenAI project id used to build the SIP connector URI
    # (sip:{openai_project_id}@sip.api.openai.com;transport=tls) — a
    # one-time OpenAI dashboard setting (which webhook URL that project's
    # incoming SIP calls hit) pairs with this, not something set in code.
    openai_project_id: Optional[str] = None
    # OpenAI's webhook-signing secret, for verifying realtime.call.incoming
    # requests to /openai/sip-incoming (Decision 11).
    openai_webhook_secret: Optional[str] = None
    # The publicly reachable base URL both Twilio's webhooks/TwiML and
    # OpenAI's SIP-incoming webhook must reference (an ngrok URL locally,
    # the Railway URL when hosted).
    public_base_url: Optional[str] = None
```

`pyproject.toml` gains one new dependency: `"twilio>=9.0"` (REST API client + TwiML helpers + the request-signature validator). No audio/DSP dependency of any kind — the entire reason Decision 1/2 matter.

---

## New module: `backend/telephony/`

```
backend/telephony/
  __init__.py
  routes.py     # FastAPI routes: Twilio-facing + the one OpenAI-facing webhook
  channel.py    # TelephonySupervisorChannel — the control-WS CallTransport for a telephony call
  transfer.py   # warm transfer orchestration + conference/participant status-callback state machine
  auth.py       # Twilio X-Twilio-Signature + OpenAI webhook-signature validation
```

### `backend/telephony/auth.py`
```python
def verify_twilio_signature(request: Request, form_data: dict) -> None: ...
def verify_openai_webhook_signature(request: Request, body: bytes) -> None: ...
# Both raise HTTPException(403) on failure. Applied as FastAPI dependencies
# on every route below except /telephony/stream-equivalent — there is no
# such route in this design (Decision 2/3), which is itself one fewer
# thing to secure compared to the original Media-Streams draft.
```

### `backend/telephony/routes.py`
```python
router = APIRouter()

@router.post("/telephony/incoming", dependencies=[Depends(verify_twilio_signature)])
async def telephony_incoming(request: Request, repos: Repositories = Depends(get_repos)):
    # call_id = str(uuid4()); create_call_state(call_id, channel="telephony").
    # Returns TwiML joining the caller into a conference named call_id:
    #   <Response><Dial><Conference statusCallback="{base}/telephony/conference-events"
    #     statusCallbackEvent="start end join leave">{call_id}</Conference></Dial></Response>
    # Then, still in this handler: client.calls.create(...) dials the AI leg
    # (Decision 5) — to=f"sip:{settings.openai_project_id}@sip.api.openai.com;transport=tls",
    # twiml=<Response><Dial><Conference>{call_id}</Conference></Dial></Response>.
    # Records the resulting Twilio CallSid on CallState as ai_leg_call_sid.
    ...

@router.post("/openai/sip-incoming", dependencies=[Depends(verify_openai_webhook_signature)])
async def openai_sip_incoming(request: Request, repos: Repositories = Depends(get_repos)):
    # Fired when OpenAI's SIP connector receives the AI leg's INVITE
    # (i.e. right after telephony_incoming's client.calls.create above
    # rings it). Payload includes OpenAI's own call_id for this SIP leg —
    # correlated back to OUR call_id via the SIP headers Twilio's call
    # carries (confirm exact correlation mechanism — a custom SIP header
    # or the From/To/Call-ID headers — against current OpenAI SIP docs at
    # implementation time). POSTs /v1/realtime/calls/{openai_call_id}/accept
    # with session config (SUPERVISOR_INSTRUCTIONS, single ask_supervisor
    # tool, voice) — same shape client/app.js already sends today for
    # WebRTC — then spawns a TelephonySupervisorChannel that opens the
    # control WebSocket and registers itself in dispatcher.CONNECTIONS.
    ...

@router.post("/telephony/conference-events", dependencies=[Depends(verify_twilio_signature)])
async def telephony_conference_events(request: Request, repos: Repositories = Depends(get_repos)):
    # join/leave/start/end for the conference. Used to: capture the
    # ConferenceSid once (needed by transfer.py's Participant API calls),
    # and — once a human participant is dialed in (Decision 8) — detect
    # the human joining (drives the "brief, then remove the AI leg" step)
    # and detect a mid-bridge disconnect (Decision 10).
    ...

@router.post("/telephony/human-status", dependencies=[Depends(verify_twilio_signature)])
async def telephony_human_status(request: Request, repos: Repositories = Depends(get_repos)):
    # Call-status callback scoped to the human leg specifically
    # (no-answer/busy/failed/canceled) — drives transfer.py's fallback
    # path (Decision 9).
    ...
```

### `backend/telephony/channel.py`
```python
class TelephonySupervisorChannel:
    """The CallTransport for a telephony call (Decision 4). Owns the
    control WebSocket to OpenAI opened after /openai/sip-incoming accepts
    the call — carries function-call/VAD events only, never audio
    (Decision 2/3). Structurally much smaller than the original
    Media-Streams draft's TelephonyCallSession: no audio pump loop, no
    format handling, because there's no audio for this class to touch at
    all — Twilio and OpenAI's SIP connector exchange RTP directly."""

    def __init__(self, call_id: str, repos: Repositories): ...

    async def run(self) -> None:
        # Opens wss://api.openai.com/v1/realtime?call_id={openai_call_id}.
        # On response.function_call_arguments.done (ask_supervisor):
        #   dispatcher.on_bridge_message(repos, call_id, {"type": "ask_supervisor", ...})
        # On input_audio_buffer.speech_started/speech_stopped: same
        #   dispatcher.on_bridge_message(..., {"type": "speech_started"/"speech_stopped"})
        # On CallState reaching stage == "ended" with escalation_reason set:
        #   this channel's job is done — closes the WS. The conference/
        #   participant orchestration in transfer.py owns the call from
        #   this point on (Decision 5's conference structure is what makes
        #   this handoff clean: closing this WS doesn't hang up anyone,
        #   it just stops the AI's own leg from being driven by dialogue
        #   logic any further; transfer.py separately removes that leg's
        #   Participant once a human has joined, per Decision 8).
        ...

    async def deliver_supervisor_result(self, tool_call_id: str, reply: str) -> None:
        # conversation.item.create (function_call_output) + response.create
        # over the owned WS — identical two-message sequence client/app.js
        # already sends today, just from Python.
        ...
```

### `backend/telephony/transfer.py`
```python
TRANSFER_MAX_RECONNECT_ATTEMPTS = 1

async def initiate_warm_transfer(repos: Repositories, call_id: str, state: CallState) -> None:
    # Fire-and-forget, spawned by dispatcher.py per Decision 6 (unchanged
    # gating logic from the original draft). Never raises out to its
    # caller — every branch is a terminal outcome recorded as a
    # trace_event, same discipline as Phase 8's background search task.
    #
    # client.conferences(state["conference_sid"]).participants.create(
    #     from_=settings.twilio_number, to=settings.escalation_human_number,
    #     early_media=True,  # lets the human hear the <Say> briefing before formally "joining"
    #     status_callback=f"{base_url}/telephony/human-status", ...)
    # Everything past this point is driven by conference-events/human-status
    # webhook callbacks landing asynchronously (Decision 8/9/10).
    ...

async def on_human_joined(repos: Repositories, call_id: str) -> None:
    # Removes the AI leg's Participant from the conference
    # (client.conferences(sid).participants(ai_participant_sid).update(status="completed")).
    # Records trace_event "transfer_connected".
    ...

async def on_human_leg_status(repos: Repositories, call_id: str, status: str) -> None:
    # no-answer/busy/failed/canceled -> Decision 9's fallback: <Say> the
    # original ESCALATION_REPLY_WEBRTC text into the caller's conference
    # leg, then end the conference. trace_event "transfer_failed", reason=status.
    ...

async def on_possible_mid_bridge_disconnect(repos: Repositories, call_id: str) -> None:
    # conference-events shows the human's participant left while the
    # caller's own leg is still in the conference and the AI leg has
    # already been removed (i.e. a real bridge existed and then broke, not
    # a normal call end) -> re-engage: <Say> "looks like we got
    # disconnected from our team, let me try again" into the caller's leg,
    # retry initiate_warm_transfer once (TRANSFER_RECONNECT_ATTEMPTS keyed
    # by call_id, mirrors dispatcher.py's existing per-call-id dict
    # pattern). Exhausting the retry falls through to the same
    # fallback-and-end path as on_human_leg_status's failure branches.
    ...
```

---

## Changes to existing files

### `backend/supervisor/state.py`
```python
class CallState(TypedDict):
    ...
    channel: Literal["webrtc", "telephony"]     # Decision 6, set once at creation
    conference_sid: Optional[str]               # telephony only — Twilio ConferenceSid, needed by transfer.py
    ai_leg_call_sid: Optional[str]               # telephony only — the AI's own conference leg, removed on successful transfer
```
`get_or_create_state`/`new_call_state` gain `channel: Literal["webrtc", "telephony"] = "webrtc"` (default preserves every existing call site unchanged).

### `backend/supervisor/graph.py` — `node_escalation`, channel-aware reply only
Identical to the original draft's Decision 7 change — `ESCALATION_REPLY_WEBRTC` vs. `ESCALATION_REPLY_TELEPHONY_TRANSFERRING`, chosen by `state["channel"]`. Nothing else about this node changes.

### `backend/dispatcher.py`
- `CallTransport` protocol + `CONNECTIONS: dict[str, CallTransport]` (Decision 4) — mechanically identical to the original draft.
- `backend/app.py`'s `/bridge` route wraps its `WebSocket` in `BrowserBridgeTransport` — the only change to that existing route.
- `process_supervisor_call`, right where `call_ended` is already recorded: if `updated["channel"] == "telephony" and updated.get("escalation_reason") and settings.escalation_human_number`, spawn `asyncio.create_task(transfer.initiate_warm_transfer(repos, call_id, updated))`.
- `mark_call_abandoned` — unchanged; keyed off `call_id`/`CONNECTIONS`, not transport type.

### `backend/db/repositories/` — `calls` table gains one column
```sql
ALTER TABLE calls ADD COLUMN channel TEXT NOT NULL DEFAULT 'webrtc';
```
`CallRepository.upsert` persists `state["channel"]`. `admin/app.js`'s calls list gains a small channel badge (☎ / 🌐).

No new table for transfer attempts — every state transition (`ai_leg_dialed`, `human_leg_dialed`, `transfer_connected`, `transfer_failed`, `transfer_reconnect_attempted`, `transfer_gave_up`) is a `trace_events` row via the existing `TraceRepository` (rule #8) — the admin panel's existing trace viewer renders these inline with zero new UI surface.

---

## Worked example — successful warm transfer

1. Caller dials the Twilio number. `POST /telephony/incoming` creates `call_id`, `channel="telephony"`, returns Conference TwiML, and simultaneously dials the AI leg out to OpenAI's SIP connector.
2. OpenAI's SIP connector rings, fires `realtime.call.incoming` → `POST /openai/sip-incoming` accepts it, `TelephonySupervisorChannel` opens its control WS. Both legs are now in the same Twilio Conference — caller and AI are talking, audio flowing entirely through Twilio↔OpenAI's own SIP/RTP path.
3. Caller says something needing a person; `is_explicit_human_request` fires in `dispatcher.process_supervisor_call`, same as it always does — no telephony-specific code involved in this part at all.
4. `node_escalation` runs, writes the handoff note, returns `ESCALATION_REPLY_TELEPHONY_TRANSFERRING`. Caller hears *"Let me try connecting you with someone on our team right now, one moment."*
5. `dispatcher.py` spawns `initiate_warm_transfer`. Twilio dials `settings.escalation_human_number` as a new Participant on the same conference, with `early_media=True` so the human hears the `<Say>` briefing (built from `generate_call_summary`'s text) as their leg connects.
6. Human accepts; `POST /telephony/conference-events` reports the join → `on_human_joined` removes the AI leg's Participant. Caller and human are now directly bridged, live, with the AI off the line.
7. `trace_events` for this `call_id`: `call_ended` (outcome=escalated) → `ai_leg_dialed` → `human_leg_dialed` → `transfer_connected` — visible in the admin panel's existing trace viewer, zero new UI code needed.

A no-answer variant: step 6's callback instead reports `no-answer` after Twilio's ring timeout → `on_human_leg_status` speaks the original `ESCALATION_REPLY_WEBRTC` line into the caller's conference leg and ends the call → `trace_events` shows `transfer_failed, reason=no-answer`.

A mid-bridge-disconnect variant: step 6 succeeds, then the human's leg drops mid-conversation → `on_possible_mid_bridge_disconnect` re-engages the caller ("looks like we got disconnected... let me try again"), retries once, then falls through to the same fallback-and-end path if the retry also fails.

---

## Tests

Nothing here hits real Twilio or OpenAI infrastructure in CI — everything mocked at the `twilio.rest.Client` boundary and the OpenAI control-WS boundary.

### `backend/tests/test_telephony_channel.py`
1. `test_function_call_done_invokes_dispatcher_directly` — a mocked `response.function_call_arguments.done` event results in `dispatcher.on_bridge_message` being called with `type="ask_supervisor"`.
2. `test_speech_started_stopped_feed_dispatcher_speaking_flag`.
3. `test_deliver_supervisor_result_sends_function_call_output_then_response_create` — asserts the exact two-message sequence over the mocked WS.
4. `test_session_ended_with_escalation_closes_control_ws_without_touching_twilio` — simulate `stage=="ended"` + `escalation_reason` set on a telephony-channel state; assert the control WS close was called and no Twilio API call was made from this class (that's transfer.py's job, per Decision 5's handoff).

### `backend/tests/test_telephony_transfer.py`
1. `test_transfer_success_adds_human_participant_and_removes_ai_leg` — mock `client.conferences(...).participants.create`; simulate a "join" conference event for the human; assert the AI leg's participant was subsequently removed (`on_human_joined`).
2. `test_transfer_no_answer_falls_back_to_original_webrtc_style_line` — simulate `no-answer` on `/telephony/human-status`; assert the caller's conference leg is updated with TwiML containing `ESCALATION_REPLY_WEBRTC`'s exact text.
3. `test_transfer_busy_falls_back_same_as_no_answer` — parametrized alongside #2 for `busy`/`failed`/`canceled`.
4. `test_mid_bridge_disconnect_triggers_one_reconnect_attempt` — simulate a connected transfer, then the human leg leaving while the caller leg is still active; assert `initiate_warm_transfer` is invoked a second time and `transfer_reconnect_attempted` is recorded.
5. `test_reconnect_cap_falls_through_to_fallback_after_max_attempts` — the retried transfer also fails; assert no third attempt (`TRANSFER_MAX_RECONNECT_ATTEMPTS == 1`) and the standard fallback fires, with `transfer_gave_up` recorded.
6. `test_warm_transfer_only_spawned_for_telephony_channel` — an escalating `channel=="webrtc"` call never calls `initiate_warm_transfer`.
7. `test_warm_transfer_skipped_when_human_number_unconfigured` — `settings.escalation_human_number is None`; assert no transfer task is spawned and the call ends cleanly on `ESCALATION_REPLY_TELEPHONY_TRANSFERRING` alone.

### `backend/tests/test_telephony_routes.py`
1. `test_incoming_call_creates_state_with_telephony_channel`.
2. `test_incoming_call_returns_valid_conference_twiml_and_dials_ai_leg` — asserts both the TwiML response shape and that `client.calls.create` was invoked targeting the OpenAI SIP connector URI.
3. `test_openai_sip_incoming_accepts_call_and_opens_channel` — mocked `realtime.call.incoming` payload results in a `POST /v1/realtime/calls/{id}/accept` call and a `TelephonySupervisorChannel` being registered in `dispatcher.CONNECTIONS`.
4. `test_twilio_webhooks_reject_invalid_signature` — parametrized over `/telephony/incoming`, `/telephony/conference-events`, `/telephony/human-status`; a missing/wrong `X-Twilio-Signature` is rejected `403` with no state mutation.
5. `test_openai_webhook_rejects_invalid_signature` — same for `/openai/sip-incoming` against OpenAI's signature scheme.

### `backend/tests/test_dispatcher_transport.py`
Identical to the original draft — `test_send_over_bridge_calls_transport_interface_not_raw_websocket`, `test_browser_bridge_transport_wraps_existing_websocket_behavior`.

---

## Definition of Done

- [ ] `pytest backend/tests` — full suite, including every new file above, zero regressions in existing WebRTC-path tests (`test_dispatcher_async.py` especially, since the `CallTransport` refactor is the one change with real blast radius onto existing code).
- [ ] `docs/DECISIONS.md` entry: native-SIP-over-Media-Streams (Decision 1, including the fallback contingency), the conference-from-the-start warm-transfer mechanism (Decision 5/8), and the hosted-not-locally-required evaluation model (Decision 12).
- [ ] Config documented in README under a clearly-optional "Telephony (stretch, not required to run the core project)" section, including the one-time manual OpenAI-dashboard step (configuring the project's SIP-incoming webhook URL) that isn't expressible as an env var.
- [ ] Manual, live, real phone call: dial the Twilio number from an actual phone, complete a full booking end-to-end over the real PSTN line — confirms the conference-based AI leg and the transport-abstraction refactor didn't break ordinary (non-escalating) calls.
- [ ] Manual, live: trigger an explicit-human-request escalation; confirm the caller hears `ESCALATION_REPLY_TELEPHONY_TRANSFERRING`, a second phone genuinely rings, the human hears the spoken briefing before formally joining, and both parties are audible to each other once bridged with the AI off the line.
- [ ] Manual, live: repeat, but decline/ignore the second phone until it times out; confirm the caller hears the fallback line and the call ends cleanly.
- [ ] Manual, live: repeat the successful-bridge case, then hang up the human's leg deliberately while still bridged; confirm the caller hears the reconnect line and a second transfer attempt genuinely fires.
- [ ] `docs/scenarios.md` gets a new **S8 — Telephony warm transfer** entry, explicitly marked manual-live-only (this cannot run through the mocked `test_scenarios.py`/`replay_scenarios.py` regression suite — the mocked coverage for this phase's *mechanism* lives entirely in `test_telephony_transfer.py`'s state-machine tests above).
- [ ] `docs/answers.md`'s Q4 answer rewritten to reference this doc's actual mechanisms (the conference/participant model, the SIP-layer-vs-application-layer distinction realized as Twilio status-callback values vs. the reconnect-attempt counter) rather than a design-only sketch.
- [ ] Deployed and reachable per Decision 12 (Railway + stable public URL, shared-secret-gated, spend caps confirmed set on OpenAI/Anthropic/Twilio) for the evaluation window, with the phone number included in the submission email/README's telephony section.
- [ ] `admin/app.js`'s calls list channel badge (☎ / 🌐) implemented and visually confirmed against at least one real telephony call's row.

---

## Note on scope relative to the rest of this project

This is, by a wide margin, the largest single phase added beyond the original brief's four user stories. That's deliberate — everything else added (Phase 6a–c's eval stack, Phase 7's optimistic capture, Phase 8's case research) makes an already-complete submission more impressive; this phase is the one piece that most directly answers "have you actually shipped a production phone agent," which nothing else in this project can demonstrate by itself. If time runs short, this phase should be cut *whole* rather than partially built — a half-built telephony integration (calls connect, warm transfer doesn't) is worse to demo than not attempting it, since it invites exactly the kind of live failure this doc spends most of its design budget trying to handle gracefully.
