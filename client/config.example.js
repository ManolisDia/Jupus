window.JUPUS_BACKEND_URL = "http://localhost:8000";
window.JUPUS_BRIDGE_WS_URL = "ws://localhost:8000/bridge";
window.JUPUS_ACCESS_TOKEN = "";   // only needed against a gated deployment

// Phase 14 migration seam: "webrtc" (hand-rolled, the pre-Phase-14 path) or
// "livekit". MUST match the backend's JUPUS_TRANSPORT — the two halves of a
// call have to agree on the transport. Both this setting and the webrtc path
// are removed once LiveKit is verified live.
window.JUPUS_TRANSPORT = "webrtc";
