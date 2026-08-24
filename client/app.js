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
let lastVerbatimTranscript = null;
let toolCalledThisResponse = false;
let cancelledRetryCount = 0;
let responseActive = false;
const MAX_CANCELLED_RETRIES = 2;

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
        output: { voice: "marin" },
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
      if (parsed.type === "conversation.item.input_audio_transcription.completed") {
        // The verbatim ASR transcript of what the caller actually said —
        // more trustworthy than the model's own last_caller_utterance
        // argument, which has been observed "helpfully" completing
        // malformed input (e.g. inventing an "@domain.com" the caller
        // never said). See docs/DECISIONS.md.
        lastVerbatimTranscript = parsed.transcript;
        return;
      }
      if (parsed.type === "input_audio_buffer.speech_started") {
        ws.send(JSON.stringify({ type: "speech_started" }));
        return;
      }
      if (parsed.type === "input_audio_buffer.speech_stopped") {
        ws.send(JSON.stringify({ type: "speech_stopped" }));
        return;
      }
      if (parsed.type === "response.function_call_arguments.done") {
        toolCalledThisResponse = true;
        cancelledRetryCount = 0;
        const args = JSON.parse(parsed.arguments);
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
      if (parsed.type === "response.created") {
        toolCalledThisResponse = false;
        responseActive = true;
        return;
      }
      if (parsed.type === "response.done") {
        responseActive = false;
        // Known failure mode (docs/known-issues/2026-08-22-001.md): semantic_vad's
        // interrupt_response can cancel a response mid-generation, after the model
        // has started but before response.function_call_arguments.done fires. That
        // can drop the turn with no recovery. BUT a cancellation is usually caused
        // by the caller's own next utterance, which the API already auto-creates a
        // new response for (create_response: true) — retrying on top of that would
        // collide ("Conversation already has an active response in progress").
        // Only retry when nothing else has already picked it up.
        const status = parsed.response?.status;
        if (
          (status === "cancelled" || status === "incomplete") &&
          !toolCalledThisResponse &&
          !responseActive &&
          cancelledRetryCount < MAX_CANCELLED_RETRIES
        ) {
          cancelledRetryCount += 1;
          dataChannel.send(JSON.stringify({ type: "response.create" }));
        } else {
          cancelledRetryCount = 0;
        }
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
