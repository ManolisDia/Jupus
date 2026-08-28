// Where the hosted client's backend lives (Railway, Phase 9).
const HOSTED_BACKEND_URL = "https://jupus-production.up.railway.app";

// The backend is derived from where this page is being served rather than
// configured, because config.js is gitignored and Firebase deploys whatever
// copy happens to be on the deploying machine's disk. Hand-editing it before
// every deploy and remembering to change it back is a step that only has to be
// half-forgotten once to leave the hosted client silently pointing at
// localhost, or local dev pointing at production.
//
// An explicit window.JUPUS_BACKEND_URL still wins, for pointing the local page
// at some other backend.
function defaultBackendUrl() {
  const host = window.location.hostname;
  // A file:// page has an empty hostname — that's the documented local flow.
  if (!host || host === "localhost" || host === "127.0.0.1") {
    return "http://localhost:8000";
  }
  return HOSTED_BACKEND_URL;
}

const BACKEND_URL = window.JUPUS_BACKEND_URL || defaultBackendUrl();
// Only the gated hosted deployment needs this; local dev leaves it empty.
const ACCESS_TOKEN = window.JUPUS_ACCESS_TOKEN || "";

const startBtn = document.getElementById("start-call");
const endBtn = document.getElementById("end-call");
const statusTextEl = document.getElementById("status-text");
const statusDotEl = document.getElementById("status-dot");
const orbIconEl = document.getElementById("orb-icon");
const orbCanvas = document.getElementById("orb");
const callIdChipEl = document.getElementById("call-id-chip");
const transcriptEl = document.getElementById("transcript");

// Everything this file used to track about the call itself — the peer
// connection, the data channel, the bridge socket, the ASR/tool-call race, the
// response-collision queue, the tts_first_audio timers — is gone with the
// hand-rolled transport (Phase 14). LiveKit owns the connection
// (client/livekit-transport.js) and the agent owns the Realtime session
// server-side. What's left here is presentation.
let callId = null;
let audioCtx = null;
let localAnalyser = null;
let remoteAnalyser = null;
let vizRafId = null;
let connected = false;
let thinkingBubbleEl = null;
// segment id -> the bubble showing that segment. LiveKit re-publishes a
// segment as its transcription is refined, so a turn has to be updated in
// place rather than appended again — see upsertTranscriptTurn.
const transcriptBubbles = new Map();

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
  } else if (connected) {
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
  transcriptBubbles.clear();
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

function upsertTranscriptTurn(role, text, segmentId) {
  if (!text) return;
  const empty = transcriptEl.querySelector(".empty-transcript");
  if (empty) empty.remove();

  // One utterance arrives as several text streams sharing an lk.segment_id:
  // interim results as ASR refines them, then a final one. Each carries the
  // whole segment, not a delta, so the right move is to overwrite the bubble
  // we already made for it. Appending instead is what turned a single spoken
  // "Hello" into "Hello" / "Hello." / "Hello." / "Hello." on screen.
  const existing = segmentId ? transcriptBubbles.get(segmentId) : null;
  if (existing) {
    existing.textContent = text;
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    return;
  }

  const div = document.createElement("div");
  div.className = `transcript-turn ${role}`;
  div.textContent = text;
  // The "…" is always the last bubble while it is up, so a turn that starts
  // during the supervisor round trip goes above it rather than below.
  if (thinkingBubbleEl && thinkingBubbleEl.parentNode === transcriptEl) {
    transcriptEl.insertBefore(div, thinkingBubbleEl);
  } else {
    transcriptEl.appendChild(div);
  }
  // No segment id (an SDK that doesn't set one) degrades to the old
  // append-every-stream behaviour rather than dropping the transcript.
  if (segmentId) transcriptBubbles.set(segmentId, div);
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

// The mic stream comes from LiveKit's published microphone track — this page
// no longer calls getUserMedia itself.
function setupVisualizer(micStream) {
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  audioCtx.resume?.().catch(() => {});

  const localSource = audioCtx.createMediaStreamSource(micStream);
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
    // Pre-Phase-14 this came from Realtime's own VAD events over the data
    // channel, which this page no longer sees. Amplitude is a coarser signal,
    // but it only drives the orb — no turn-taking decision depends on it, and
    // turn-taking is LiveKit's and Realtime's job now regardless.
    const callerSpeaking = localAmp > 0.05;

    setSpeakerState(callerSpeaking ? "caller" : agentSpeaking ? "agent" : null);

    // base circle
    ctx.beginPath();
    ctx.arc(cx, cy, baseRadius, 0, Math.PI * 2);
    ctx.fillStyle = "#171b23";
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = "#262b35";
    ctx.stroke();

    // caller ring (cyan)
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

// The ONE path every failure and the normal "End Call" click both go through.
function teardown(statusMessage) {
  connected = false;
  teardownLiveKit();
  teardownVisualizer();
  startBtn.disabled = false;
  endBtn.disabled = true;
  setStatus(statusMessage);
}

async function startCall() {
  startBtn.disabled = true;
  setStatus("connecting");
  resetLiveUi();

  try {
    // The call_id is minted here and used as the LiveKit room name, which is
    // how it reaches the agent (ctx.job.room.name) and keys every trace event,
    // CallState and DB row for this call.
    callId = crypto.randomUUID();
    showCallIdChip(callId);
    await startLiveKitCall(callId);
    connected = true;
    endBtn.disabled = false;
    setStatus("connected");
  } catch (err) {
    teardown("error: " + (err.message || "failed to start call"));
  }
}


startBtn.addEventListener("click", startCall);
endBtn.addEventListener("click", () => teardown("idle"));
