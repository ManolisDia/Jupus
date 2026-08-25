// Phase 14 — the LiveKit half of the caller client.
//
// Loaded after app.js, so it can call app.js's UI functions directly
// (setStatus, appendTranscriptTurn, renderCallState, ...). Those are top-level
// bindings in a classic script, visible to every script that follows.
//
// What this file deliberately does NOT contain, compared to the WebRTC path it
// replaces in app.js: no SDP offer/answer, no ICE handling, no data channel,
// no Realtime event parsing, no session.update, no tool schema, no
// responseActive/pendingResponseCreate collision queue, and no
// transcriptionPending/awaitingToolCall race fix. All of that is either
// LiveKit's job now or has moved server-side to backend/transport/. That
// deletion is most of the point of the phase.

let lkRoom = null;

// Published by the agent alongside its own speech (see livekit_agent.py).
// Topic-scoped rather than sniffing every data message, so an unrelated future
// data topic can't be misread as call state.
const CALL_STATE_TOPIC = "jupus.call_state";

async function startLiveKitCall(callId) {
  const tokenUrl =
    `${BACKEND_URL}/livekit-token` + (ACCESS_TOKEN ? `?access_token=${ACCESS_TOKEN}` : "");
  const response = await fetch(tokenUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ call_id: callId }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `token request failed (${response.status})`);
  }
  const { url, token } = await response.json();

  const room = new LivekitClient.Room({
    // The agent is the only other participant and it always publishes audio;
    // adaptive stream/dynacast exist to throttle video subscriptions we never
    // have, and turning them off keeps the audio path as direct as possible.
    adaptiveStream: false,
    dynacast: false,
  });
  lkRoom = room;

  room.on(LivekitClient.RoomEvent.TrackSubscribed, (track) => {
    if (track.kind !== LivekitClient.Track.Kind.Audio) return;
    // Reuse the existing <audio id="remote-audio"> element so autoplay policy
    // and the orb's analyser behave exactly as they did under WebRTC.
    const el = document.getElementById("remote-audio");
    track.attach(el);
    if (el.srcObject) attachRemoteAnalyser(el.srcObject);
  });

  room.on(LivekitClient.RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
    if (topic !== CALL_STATE_TOPIC) return;
    try {
      renderCallState(JSON.parse(new TextDecoder().decode(payload)));
    } catch (err) {
      console.warn("unparseable call_state payload", err);
    }
  });

  room.on(LivekitClient.RoomEvent.Disconnected, () => {
    // Guarded the same way the old ws.onclose was: teardown() nulls lkRoom
    // first, so our own hang-up doesn't report itself as a lost connection.
    if (lkRoom !== null) teardown("error: lost connection");
  });

  await room.connect(url, token);
  await room.localParticipant.setMicrophoneEnabled(true);

  const micTrack = room.localParticipant.getTrackPublication(
    LivekitClient.Track.Source.Microphone
  );
  if (micTrack && micTrack.track && micTrack.track.mediaStream) {
    setupVisualizer(micTrack.track.mediaStream);
  }

  registerTranscriptHandlers(room);
  return room;
}

function registerTranscriptHandlers(room) {
  // LiveKit Agents publishes transcriptions as text streams on a reserved
  // topic. Wrapped in a guard because this is the one client API most likely
  // to move between SDK minors, and a missing transcript must never be able to
  // break the actual call.
  try {
    room.registerTextStreamHandler("lk.transcription", async (reader, participant) => {
      const isAgent = participant?.identity !== "caller";
      const text = await reader.readAll();
      if (!text) return;
      if (isAgent) removeThinkingBubble();
      appendTranscriptTurn(isAgent ? "agent" : "caller", text);
    });
  } catch (err) {
    console.warn("transcription stream unavailable on this SDK version", err);
  }
}

async function teardownLiveKit() {
  const room = lkRoom;
  lkRoom = null;
  if (room) {
    try {
      await room.disconnect();
    } catch (err) {
      console.warn("error disconnecting from LiveKit room", err);
    }
  }
}
