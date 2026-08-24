const BACKEND_URL = "http://localhost:8000";
const BRIDGE_WS_URL = "ws://localhost:8000/bridge";
const REALTIME_CALLS_URL = "https://api.openai.com/v1/realtime/calls";

const startBtn = document.getElementById("start-call");
const endBtn = document.getElementById("end-call");
const statusTextEl = document.getElementById("status-text");
const statusDotEl = document.getElementById("status-dot");
const orbIconEl = document.getElementById("orb-icon");
const orbCanvas = document.getElementById("orb");
const callIdChipEl = document.getElementById("call-id-chip");
const transcriptEl = document.getElementById("transcript");

let pc = null;
let dataChannel = null;
let localStream = null;
let ws = null;
let lastVerbatimTranscript = null;

// Response.create collision avoidance (see docs/known-issues/2026-08-22-001.md
// and docs/fixes/ for the write-up this session added). true between a
// response.created and its matching response.done — the Realtime API
// rejects a second response.create while one is already active
// ("Conversation already has an active response in progress"), which is
// expected under the async dispatcher's design (a deferred supervisor
// reply can land right as the caller's own new utterance auto-creates its
// own response). Tracked so a supervisor_result arriving mid-response can
// wait for the active one to finish rather than racing it.
let responseActive = false;
let pendingResponseCreate = false;

// ---------------------------------------------------------------------------
// Presentational-only state (Phase 7 caller-facing visual polish stretch).
// Nothing in this section reads from or writes to pc/ws/dataChannel — it
// only renders from state the call logic below already produces, or from
// the new read-only "call_state" bridge message (see docs/phases/
// phase-7-polish-submission.md). No WebRTC/data-channel/bridge signaling
// logic is changed by any of it.
// ---------------------------------------------------------------------------

let callId = null;
let audioCtx = null;
let localAnalyser = null;
let remoteAnalyser = null;
let vizRafId = null;
let callerSpeaking = false; // driven by the real VAD events, not amplitude guessing
let thinkingBubbleEl = null;

function setStatus(message) {
  statusTextEl.textContent = message;

  let key = "idle";
  if (message.startsWith("error")) key = "error";
  else if (message === "connecting") key = "connecting";
  else if (message === "connected") key = "connected";
  else if (message === "idle") key = "idle";

  statusDotEl.className = key === "connecting" ? "connecting pulsing" : key;
  orbIconEl.className = key === "connecting" ? "state-connecting" : "";
  if (key === "idle") {
    orbIconEl.textContent = "Press start\nto begin";
  } else if (key === "connecting") {
    orbIconEl.textContent = "Connecting…";
  } else if (key === "connected") {
    orbIconEl.textContent = "Listening…";
  } else if (key === "error") {
    orbIconEl.textContent = "Call ended";
  }
}

function setSpeakerState(who) {
  // who: "caller" | "agent" | null
  orbIconEl.classList.remove("state-caller", "state-agent");
  if (who === "caller") {
    orbIconEl.classList.add("state-caller");
    orbIconEl.textContent = "Listening to you…";
  } else if (who === "agent") {
    orbIconEl.classList.add("state-agent");
    orbIconEl.textContent = "Agent speaking…";
  } else if (pc && pc.connectionState === "connected") {
    orbIconEl.textContent = "Listening…";
  }
}

function resetLiveUi() {
  transcriptEl.innerHTML = '<div class="empty-transcript">Nothing said yet.</div>';
  for (const tile of document.querySelectorAll(".field-tile")) {
    tile.className = "field-tile";
    tile.querySelector(".field-value").textContent = "—";
  }
  thinkingBubbleEl = null;
}

function showCallIdChip(id) {
  callIdChipEl.textContent = `call_id: ${id}`;
  callIdChipEl.classList.remove("hidden");
  callIdChipEl.onclick = () => {
    navigator.clipboard?.writeText(id).catch(() => {});
    const original = callIdChipEl.textContent;
    callIdChipEl.textContent = "copied!";
    setTimeout(() => (callIdChipEl.textContent = original), 900);
  };
}

function appendTranscriptTurn(role, text) {
  if (!text) return;
  const empty = transcriptEl.querySelector(".empty-transcript");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = `transcript-turn ${role}`;
  div.textContent = text;
  transcriptEl.appendChild(div);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function showThinkingBubble() {
  const empty = transcriptEl.querySelector(".empty-transcript");
  if (empty) empty.remove();
  thinkingBubbleEl = document.createElement("div");
  thinkingBubbleEl.className = "transcript-turn thinking";
  thinkingBubbleEl.textContent = "…";
  transcriptEl.appendChild(thinkingBubbleEl);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function removeThinkingBubble() {
  if (thinkingBubbleEl) {
    thinkingBubbleEl.remove();
    thinkingBubbleEl = null;
  }
}

function renderCallState(snapshot) {
  const profile = snapshot.caller_profile || {};
  for (const [field, data] of Object.entries(profile)) {
    const tile = document.querySelector(`.field-tile[data-field="${field}"]`);
    if (!tile) continue;
    tile.className = `field-tile status-${data.status}`;
    tile.querySelector(".field-value").textContent =
      data.status === "missing" ? "—" : data.value || "—";
  }
}

function setupVisualizer() {
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  audioCtx.resume?.().catch(() => {});

  const localSource = audioCtx.createMediaStreamSource(localStream);
  localAnalyser = audioCtx.createAnalyser();
  localAnalyser.fftSize = 256;
  localAnalyser.smoothingTimeConstant = 0.75;
  localSource.connect(localAnalyser);

  drawOrb();
}

function attachRemoteAnalyser(remoteStream) {
  if (!audioCtx) return;
  const remoteSource = audioCtx.createMediaStreamSource(remoteStream);
  remoteAnalyser = audioCtx.createAnalyser();
  remoteAnalyser.fftSize = 256;
  remoteAnalyser.smoothingTimeConstant = 0.75;
  remoteSource.connect(remoteAnalyser);
}

function averageAmplitude(analyser) {
  if (!analyser) return 0;
  const data = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteFrequencyData(data);
  let sum = 0;
  for (const v of data) sum += v;
  return sum / data.length / 255; // 0..1
}

function drawOrb() {
  const ctx = orbCanvas.getContext("2d");
  const w = orbCanvas.width;
  const h = orbCanvas.height;
  const cx = w / 2;
  const cy = h / 2;
  const baseRadius = 46;

  function frame() {
    vizRafId = requestAnimationFrame(frame);
    ctx.clearRect(0, 0, w, h);

    const localAmp = averageAmplitude(localAnalyser);
    const remoteAmp = averageAmplitude(remoteAnalyser);
    const agentSpeaking = remoteAmp > 0.05;

    setSpeakerState(callerSpeaking ? "caller" : agentSpeaking ? "agent" : null);

    // base circle
    ctx.beginPath();
    ctx.arc(cx, cy, baseRadius, 0, Math.PI * 2);
    ctx.fillStyle = "#171b23";
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = "#262b35";
    ctx.stroke();

    // caller ring (cyan) — driven mostly by real VAD state, amplitude for size
    const callerRadius = baseRadius + 8 + (callerSpeaking ? localAmp * 55 + 6 : localAmp * 10);
    ctx.beginPath();
    ctx.arc(cx, cy, callerRadius, 0, Math.PI * 2);
    ctx.strokeStyle = callerSpeaking ? "rgba(34, 211, 238, 0.9)" : "rgba(34, 211, 238, 0.25)";
    ctx.lineWidth = callerSpeaking ? 3 : 1.5;
    ctx.stroke();

    // agent ring (indigo)
    const agentRadius = baseRadius + 20 + remoteAmp * 60;
    ctx.beginPath();
    ctx.arc(cx, cy, agentRadius, 0, Math.PI * 2);
    ctx.strokeStyle = agentSpeaking ? "rgba(129, 140, 248, 0.9)" : "rgba(129, 140, 248, 0.18)";
    ctx.lineWidth = agentSpeaking ? 3 : 1.5;
    ctx.stroke();
  }
  frame();
}

function teardownVisualizer() {
  if (vizRafId) cancelAnimationFrame(vizRafId);
  vizRafId = null;
  localAnalyser = null;
  remoteAnalyser = null;
  if (audioCtx) {
    audioCtx.close().catch(() => {});
    audioCtx = null;
  }
  orbIconEl.classList.remove("state-caller", "state-agent");
}

// ---------------------------------------------------------------------------

// The ONE path every failure and the normal "End Call" click both go
// through — nothing else may close pc/ws directly.
function teardown(statusMessage) {
  if (dataChannel) {
    dataChannel.close();
    dataChannel = null;
  }
  if (pc) {
    pc.close();
    pc = null;
  }
  if (localStream) {
    localStream.getTracks().forEach((t) => t.stop());
    localStream = null;
  }
  if (ws) {
    ws.close();
    ws = null;
  }
  teardownVisualizer();
  callerSpeaking = false;
  responseActive = false;
  pendingResponseCreate = false;
  startBtn.disabled = false;
  endBtn.disabled = true;
  setStatus(statusMessage);
}

// The ONE path anything wanting the model to speak/act next must go
// through — never call dataChannel.send({type: "response.create"})
// directly. If a response is already active, queues the request instead
// of firing it immediately (avoiding the collision in the common case);
// the response.done handler below flushes at most one queued request once
// the active response actually finishes. This is a preventative check, not
// a retry-after-the-fact — it doesn't need to distinguish which response
// is active, just whether the slot is currently claimed, so it isn't
// exposed to the per-response-ID race that sank the retry-on-cancel
// mechanism this doc's known-issues entry describes. The error handler
// below is still the backstop for whatever race slips through anyway.
function requestResponse() {
  if (responseActive) {
    pendingResponseCreate = true;
    return;
  }
  dataChannel.send(JSON.stringify({ type: "response.create" }));
}

const SUPERVISOR_INSTRUCTIONS = `You are the phone-answering voice for a law firm. You have exactly one
capability beyond talking: a tool called ask_supervisor.

Rules, always:
1. Greet the caller warmly and ask what they need. You may handle
   greetings, small talk, and simple acknowledgments ("okay", "got it",
   "sounds good") yourself, without calling any tool.
2. The moment the caller describes a legal issue, asks about scheduling,
   gives you any personal detail (name, email, phone, a date or time), or
   asks anything you are not completely certain how to answer — call
   ask_supervisor. Do this every time, even if you think you already
   know the answer. Never state legal information, confirm a booking, or
   promise anything on your own — you do not have real information about
   the firm's calendar, policies, or legal positions; only
   ask_supervisor does.
3. If the caller asks to speak to a person, still call ask_supervisor —
   do not handle that yourself, and do not argue or try to talk them out
   of it.
3a. Call ask_supervisor separately for each new thing the caller says —
   never wait to combine two or more caller utterances into a single
   call. If the caller says something new before you've replied to
   their previous utterance, that is a fresh, separate call to
   ask_supervisor with last_caller_utterance set to only that new
   utterance, not a merge of it with anything said before.
4. The instant you decide to call ask_supervisor, call it — as the very
   first thing you do in your turn, before speaking any words out loud.
   Never narrate that you're checking, looking something up, or
   thinking — no "one moment," "let me check that for you," "just a
   second," or anything similar, and never speak any other sentence
   first either. Do not promise a follow-up you can't immediately
   deliver. If there's a brief pause before your next reply, that's
   natural and fine — a person doesn't announce every small pause
   either. When ask_supervisor returns, treat its reply as your next
   conversational turn and flow straight into it, the way a person
   continuing a conversation would — not as the payoff to an earlier
   promise.
5. When ask_supervisor returns a reply, speak ONLY that reply, naturally
   in your own voice — you may lightly rephrase for tone, but never
   alter facts, names, dates, or numbers it gives you, and never append
   a follow-up question or next step of your own after it, even if it
   seems like the obvious next thing to ask. ask_supervisor decides what
   to ask next, not you. After speaking its reply, stop and wait for the
   caller to speak again — whatever they say next is a brand new
   ask_supervisor call (per rule 3a), never something you answer
   yourself just because you can guess what they're going to say.
6. Never invent details about the firm, its lawyers, its fees, or the
   law itself. If you don't have an answer from ask_supervisor yet, say
   you'll check rather than guessing.
7. When calling ask_supervisor, transcribe last_caller_utterance EXACTLY
   as the caller said it — never paraphrase, complete, or "clean up"
   what they said. If they say an email or phone number that sounds
   incomplete or malformed, pass it along exactly as spoken, incomplete
   or malformed — do not fill in a plausible-looking domain, symbol, or
   digit they didn't actually say.
Keep every reply short and conversational, like a real phone
receptionist — one or two short sentences at a time, never a long
monologue.`;

const ASK_SUPERVISOR_TOOL = {
  type: "function",
  name: "ask_supervisor",
  description:
    "Call this whenever the caller needs anything beyond simple greetings or " +
    "small talk — routing, booking, detail capture, or escalation.",
  parameters: {
    type: "object",
    properties: {
      reason: { type: "string" },
      last_caller_utterance: {
        type: "string",
        description:
          "The caller's most recent utterance, transcribed EXACTLY as spoken — word for " +
          "word, verbatim. Never paraphrase, complete, normalize, or invent missing detail " +
          "(e.g. never insert an '@' symbol or a domain like 'example.com' into an email " +
          "the caller didn't actually say). If what they said is incomplete or malformed, " +
          "reproduce it exactly as-is, incomplete or malformed.",
      },
    },
    required: ["reason", "last_caller_utterance"],
  },
};

function sendSessionUpdate() {
  const payload = {
    type: "session.update",
    session: {
      type: "realtime",
      instructions: SUPERVISOR_INSTRUCTIONS,
      tools: [ASK_SUPERVISOR_TOOL],
      audio: {
        output: { voice: "marin", speed: 1.5 },
        input: {
          noise_reduction: { type: "near_field" },
          transcription: { model: "gpt-transcribe" },
          turn_detection: {
            type: "semantic_vad",
            eagerness: "low",
            create_response: true,
            interrupt_response: true,
          },
        },
      },
    },
  };
  dataChannel.send(JSON.stringify(payload));
}

async function startCall() {
  startBtn.disabled = true;
  setStatus("connecting");
  resetLiveUi();

  try {
    callId = crypto.randomUUID();
    showCallIdChip(callId);

    const sessionResp = await fetch(`${BACKEND_URL}/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ call_id: callId }),
    });
    if (!sessionResp.ok) {
      const body = await sessionResp.json().catch(() => ({}));
      throw new Error(body.error || `session request failed (${sessionResp.status})`);
    }
    const { client_secret: clientSecret } = await sessionResp.json();

    ws = new WebSocket(`${BRIDGE_WS_URL}?call_id=${callId}`);
    ws.onmessage = (e) => {
      const parsed = JSON.parse(e.data);
      if (parsed.type === "call_state") {
        // Read-only projection for the "captured details" panel — see
        // dispatcher.broadcast_call_state / docs/phases/
        // phase-7-polish-submission.md. Never fed back into the call.
        renderCallState(parsed);
        return;
      }
      if (parsed.type !== "supervisor_result") return;
      // Rendered once the agent actually speaks it (response.audio_transcript.done
      // below) rather than here — Realtime speaks this exact reply (see rule 5
      // in SUPERVISOR_INSTRUCTIONS), so appending it at both points doubled
      // every agent line in the transcript.
      dataChannel.send(
        JSON.stringify({
          type: "conversation.item.create",
          item: {
            type: "function_call_output",
            call_id: parsed.tool_call_id,
            output: parsed.reply,
          },
        })
      );
      requestResponse();
    };
    ws.onerror = () => teardown("error: lost connection to backend");
    ws.onclose = () => {
      // Only a failure if we didn't just close it ourselves via teardown()
      if (ws !== null) teardown("error: lost connection to backend");
    };

    pc = new RTCPeerConnection();

    pc.oniceconnectionstatechange = () => {
      if (["failed", "disconnected", "closed"].includes(pc?.iceConnectionState)) {
        teardown("error: connection lost");
      }
    };
    pc.onconnectionstatechange = () => {
      if (pc?.connectionState === "failed") {
        teardown("error: connection failed");
      }
    };

    const remoteAudioEl = document.getElementById("remote-audio");
    pc.ontrack = (e) => {
      remoteAudioEl.srcObject = e.streams[0];
      attachRemoteAnalyser(e.streams[0]);
    };

    localStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    pc.addTrack(localStream.getAudioTracks()[0], localStream);
    setupVisualizer();

    dataChannel = pc.createDataChannel("oai-events");
    dataChannel.onopen = () => sendSessionUpdate();
    dataChannel.onmessage = (e) => {
      const parsed = JSON.parse(e.data);
      if (parsed.type === "error") {
        const errMessage = parsed.error?.message || "";
        if (errMessage.includes("already has an active response in progress")) {
          // Expected, recoverable race (see the responseActive/
          // pendingResponseCreate machinery above and docs/known-issues/
          // 2026-08-22-001.md) — our own response.create lost a race
          // against a response the caller's own speech already triggered.
          // The conversation.item.create for the tool result already
          // succeeded (only response.create was rejected), so nothing
          // was lost — whichever response is actually active will pick
          // it up, or the response.done handler's queued flush will.
          // Must NOT tear down an otherwise-healthy call over this.
          console.warn("oai realtime: response.create collided with an active response — call continues", parsed.error);
          return;
        }
        teardown("error: " + (errMessage || "realtime session error"));
        return;
      }
      if (parsed.type === "response.created") {
        responseActive = true;
        return;
      }
      if (parsed.type === "response.done") {
        responseActive = false;
        if (pendingResponseCreate) {
          pendingResponseCreate = false;
          requestResponse();
        }
        return;
      }
      if (parsed.type === "conversation.item.input_audio_transcription.completed") {
        // The verbatim ASR transcript of what the caller actually said —
        // more trustworthy than the model's own last_caller_utterance
        // argument, which has been observed "helpfully" completing
        // malformed input (e.g. inventing an "@domain.com" the caller
        // never said). See docs/DECISIONS.md.
        lastVerbatimTranscript = parsed.transcript;
        appendTranscriptTurn("caller", parsed.transcript);
        return;
      }
      if (parsed.type === "input_audio_buffer.speech_started") {
        callerSpeaking = true;
        ws.send(JSON.stringify({ type: "speech_started" }));
        return;
      }
      if (parsed.type === "input_audio_buffer.speech_stopped") {
        callerSpeaking = false;
        ws.send(JSON.stringify({ type: "speech_stopped" }));
        return;
      }
      if (parsed.type === "response.function_call_arguments.done") {
        const args = JSON.parse(parsed.arguments);
        showThinkingBubble();
        ws.send(
          JSON.stringify({
            type: "ask_supervisor",
            tool_call_id: parsed.call_id,
            reason: args.reason,
            last_caller_utterance: lastVerbatimTranscript ?? args.last_caller_utterance,
          })
        );
        return;
      }
      if (
        parsed.type === "response.audio_transcript.done" ||
        parsed.type === "response.output_audio_transcript.done"
      ) {
        // The single source of truth for agent lines in the transcript —
        // covers both ask_supervisor turns and small-talk turns the
        // Realtime model handles itself without ever calling ask_supervisor
        // (rule 1 above). Clears the thinking bubble here too, once the
        // agent actually has something to say, rather than the instant
        // ask_supervisor resolves.
        removeThinkingBubble();
        appendTranscriptTurn("agent", parsed.transcript);
        return;
      }
      console.log("oai event", parsed);
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    const sdpResp = await fetch(REALTIME_CALLS_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${clientSecret}`,
        "Content-Type": "application/sdp",
      },
      body: offer.sdp,
    });
    if (!sdpResp.ok) {
      throw new Error(`realtime handshake failed (${sdpResp.status})`);
    }
    const answerSdp = await sdpResp.text();
    await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });

    endBtn.disabled = false;
    setStatus("connected");
  } catch (err) {
    teardown("error: " + (err.message || "failed to start call"));
  }
}

startBtn.addEventListener("click", startCall);
endBtn.addEventListener("click", () => teardown("idle"));
