const BACKEND_URL = "http://localhost:8000";
const REALTIME_CALLS_URL = "https://api.openai.com/v1/realtime/calls";

const startBtn = document.getElementById("start-call");
const endBtn = document.getElementById("end-call");
const statusEl = document.getElementById("status");

let pc = null;
let dataChannel = null;
let localStream = null;
let ws = null; // wired up in Phase 2

function setStatus(message) {
  statusEl.textContent = message;
}

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
  startBtn.disabled = false;
  endBtn.disabled = true;
  setStatus(statusMessage);
}

function sendSessionUpdate() {
  dataChannel.send(
    JSON.stringify({
      type: "session.update",
      session: {
        type: "realtime",
        instructions:
          "You are the friendly voice receptionist for a law firm. Greet the " +
          "caller naturally and ask what they need help with. CRITICAL: every " +
          "single reply must be one short sentence, phone-call style — never " +
          "more. Do not explain, list options, or elaborate. If you catch " +
          "yourself about to say a second sentence, stop and just ask a " +
          "short follow-up question instead.",
        tools: [],
        audio: {
          output: { voice: "marin" },
          input: {
            noise_reduction: { type: "near_field" },
            turn_detection: {
              type: "semantic_vad",
              eagerness: "low",
              create_response: true,
              interrupt_response: true,
            },
          },
        },
      },
    })
  );
}

async function startCall() {
  startBtn.disabled = true;
  setStatus("connecting");

  try {
    const callId = crypto.randomUUID();

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
    };

    localStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    pc.addTrack(localStream.getAudioTracks()[0], localStream);

    dataChannel = pc.createDataChannel("oai-events");
    dataChannel.onopen = () => sendSessionUpdate();
    dataChannel.onmessage = (e) => {
      const parsed = JSON.parse(e.data);
      if (parsed.type === "error") {
        teardown("error: " + (parsed.error?.message || "realtime session error"));
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
