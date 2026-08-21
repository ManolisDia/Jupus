const BACKEND_URL = "http://localhost:8000";
const BRIDGE_WS_URL = "ws://localhost:8000/bridge";
const REALTIME_CALLS_URL = "https://api.openai.com/v1/realtime/calls";

const startBtn = document.getElementById("start-call");
const endBtn = document.getElementById("end-call");
const statusEl = document.getElementById("status");

let pc = null;
let dataChannel = null;
let localStream = null;
let ws = null;

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
4. While waiting on ask_supervisor, you may say a brief natural
   acknowledgment ("Let me check that for you" / "One moment") — keep it
   to a few words, and never guess at what the answer will be.
5. When ask_supervisor returns a reply, speak it naturally in your own
   voice — you may lightly rephrase for tone, but never alter facts,
   names, dates, or numbers it gives you.
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
  };
  dataChannel.send(JSON.stringify(payload));
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

    ws = new WebSocket(`${BRIDGE_WS_URL}?call_id=${callId}`);
    ws.onmessage = (e) => {
      const parsed = JSON.parse(e.data);
      if (parsed.type !== "supervisor_result") return;
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
      dataChannel.send(JSON.stringify({ type: "response.create" }));
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
      if (parsed.type === "response.function_call_arguments.done") {
        const args = JSON.parse(parsed.arguments);
        ws.send(
          JSON.stringify({
            type: "ask_supervisor",
            tool_call_id: parsed.call_id,
            reason: args.reason,
            last_caller_utterance: args.last_caller_utterance,
          })
        );
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
