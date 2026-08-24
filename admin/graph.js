const SVG_NS = "http://www.w3.org/2000/svg";

// Phase 7 (optimistic capture) added node_capture_fast in front of the
// original capture node — "capture" here is still exactly node_capture,
// unchanged, reached both by the confirm/drain phase AND by
// node_capture_fast's own fallback paths (gate/urgent/pending-confirm),
// which is why its own trace events are always node="capture", never a
// separate "capture_confirm" label — there isn't one in real trace data.
const NODES = [
  { id: "greeting", label: "Greeting", x: 20, y: 70, w: 165, h: 68 },
  { id: "routing", label: "Routing", x: 220, y: 70, w: 165, h: 68 },
  { id: "capture_fast", label: "Capture (fast)", x: 420, y: 70, w: 165, h: 68 },
  { id: "capture", label: "Capture (confirm)", x: 620, y: 70, w: 165, h: 68 },
  { id: "booking", label: "Booking", x: 820, y: 70, w: 165, h: 68 },
  { id: "escalation", label: "Escalation", x: 520, y: 280, w: 165, h: 68 },
];

const MAIN_EDGES = [
  { key: "greeting-routing", d: "M185,104 L220,104", labelPos: [202, 96] },
  { key: "routing-capture_fast", d: "M385,104 L420,104", labelPos: [402, 96] },
  { key: "capture_fast-capture", d: "M585,104 L620,104", labelPos: [602, 96] },
  { key: "capture-booking", d: "M785,104 L820,104", labelPos: [802, 96] },
];

const LOOP_EDGES = [
  { key: "routing-routing", d: "M273,70 C273,14 332,14 332,70", labelPos: [302, 26] },
  { key: "capture_fast-capture_fast", d: "M473,70 C473,14 532,14 532,70", labelPos: [502, 26] },
  { key: "capture-capture", d: "M673,70 C673,14 732,14 732,70", labelPos: [702, 26] },
  { key: "booking-booking", d: "M873,70 C873,14 932,14 932,70", labelPos: [902, 26] },
];

const ANY_EDGES = [
  { key: "greeting-escalation", d: "M102,138 C102,220 400,262 545,280" },
  { key: "routing-escalation", d: "M302,138 C302,200 460,252 565,280" },
  { key: "capture_fast-escalation", d: "M502,138 C502,210 545,250 585,280" },
  { key: "capture-escalation", d: "M702,138 C702,210 670,250 630,280" },
  { key: "booking-escalation", d: "M902,138 C902,200 750,252 660,280" },
];

const nodeEls = {};
const edgeEls = {};
const labelEls = {};

let ws = null;
let currentCallId = null;
let activeNode = null;
let recentToolResults = []; // ring buffer of {node, tool_name, summary}
let toolCallCount = 0;
let toolDurationSum = 0;
let deferredCount = 0;

function buildGraph() {
  const nodesG = document.getElementById("nodes");
  const mainG = document.getElementById("edges-main");
  const loopG = document.getElementById("edges-loop");
  const anyG = document.getElementById("edges-any");

  for (const e of ANY_EDGES) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", e.d);
    path.setAttribute("class", "edge any-edge");
    anyG.appendChild(path);
    edgeEls[e.key] = path;
  }

  for (const e of MAIN_EDGES) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", e.d);
    path.setAttribute("class", "edge");
    mainG.appendChild(path);
    edgeEls[e.key] = path;
  }

  for (const e of LOOP_EDGES) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", e.d);
    path.setAttribute("class", "edge");
    loopG.appendChild(path);
    edgeEls[e.key] = path;

    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", e.labelPos[0]);
    label.setAttribute("y", e.labelPos[1]);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", "edge-label");
    label.textContent = e.key.split("-")[0] + " re-prompt";
    loopG.appendChild(label);
    labelEls[e.key] = label;
  }

  // small "any stage, any time" marker near the escalation node
  const anyLabel = document.createElementNS(SVG_NS, "text");
  anyLabel.setAttribute("x", 300);
  anyLabel.setAttribute("y", 224);
  anyLabel.setAttribute("text-anchor", "middle");
  anyLabel.setAttribute("class", "edge-label");
  anyLabel.textContent = "any stage, any time";
  document.getElementById("edges-any").appendChild(anyLabel);

  for (const n of NODES) {
    const g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("class", "node-box");
    g.setAttribute("data-node", n.id);

    if (n.id === "capture_fast") {
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent =
        "Phase 7: asks through name/email/phone optimistically with zero Claude calls on the " +
        "hot path, while the real extraction/validation for each field runs in a background " +
        "task. Falls back to the Capture (confirm) node whenever there's real doubt it's safe " +
        "to guess (an off-topic aside, a field that doesn't look right, a field already " +
        "mid-confirmation, or a background check that already failed).";
      g.appendChild(title);
    }
    if (n.id === "capture") {
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent =
        "This is node_capture, unchanged — reused both as the batched confirm/drain phase " +
        "(reading back email/phone once every field's been fast-asked) AND as Capture (fast)'s " +
        "own fallback path, which is why its trace events are always labeled \"capture\", never " +
        "a separate confirm label. N/E/P below show each field's live status.";
      g.appendChild(title);
    }
    if (n.id === "booking") {
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent =
        "Slot proposal, decline, and reschedule are plain deterministic branching inside this " +
        "one node, not separate graph nodes — the line below shows the live proposal state.";
      g.appendChild(title);
    }

    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", n.x);
    rect.setAttribute("y", n.y);
    rect.setAttribute("width", n.w);
    rect.setAttribute("height", n.h);
    g.appendChild(rect);

    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("x", n.x + n.w / 2);
    text.setAttribute("y", n.y + n.h / 2 + 3);
    text.setAttribute("text-anchor", "middle");
    text.textContent = n.label;
    g.appendChild(text);

    const sub = document.createElementNS(SVG_NS, "text");
    sub.setAttribute("x", n.x + n.w / 2);
    sub.setAttribute("y", n.y + n.h / 2 + 21);
    sub.setAttribute("text-anchor", "middle");
    sub.setAttribute("class", "node-sub");
    sub.textContent = "idle";
    g.appendChild(sub);
    g._sub = sub;

    nodesG.appendChild(g);
    nodeEls[n.id] = g;
  }
}

// Capture (both nodes) and booking render their own richer sub-state (see
// renderCallStateBadges below) driven by real CallState, not the generic
// idle/working/done label — don't let node_entered/node_exited stomp it.
const RICH_SUBSTATE_NODES = new Set(["capture_fast", "capture", "booking"]);

function setNodeState(nodeId, cls, subText) {
  const el = nodeEls[nodeId];
  if (!el) return;
  el.classList.remove("active", "visited", "escalation-active");
  if (cls) el.classList.add(cls);
  if (subText !== undefined && !RICH_SUBSTATE_NODES.has(nodeId)) el._sub.textContent = subText;
}

function fieldPillMarkup(letter, status) {
  const color =
    status === "confirmed" ? "var(--green)" : status === "pending_confirm" ? "var(--amber)" : "var(--text-faint)";
  const suffix = status === "confirmed" ? "✓" : status === "pending_confirm" ? "…" : "";
  return `<tspan fill="${color}">${letter}${suffix}</tspan>`;
}

function renderCallStateBadges(msg) {
  const profile = msg.caller_profile || {};
  if (profile.name && profile.email && profile.phone) {
    const pills = [
      fieldPillMarkup("N", profile.name.status),
      fieldPillMarkup("E", profile.email.status),
      fieldPillMarkup("P", profile.phone.status),
    ].join("   ");
    // Both capture nodes show the same live field-status pills — whichever
    // one is actually active at a given moment, the pills tell the same
    // story (real CallState, not per-node display state).
    for (const nodeId of ["capture_fast", "capture"]) {
      if (nodeEls[nodeId]) nodeEls[nodeId]._sub.innerHTML = pills;
    }
  }

  const bookingNode = nodeEls.booking;
  const booking = msg.booking || {};
  if (bookingNode) {
    let text = "no slot proposed yet";
    if (booking.proposed_slot_id) {
      text = `slot #${booking.proposed_slot_id} proposed`;
    } else if (booking.declined_count > 0) {
      text = `${booking.declined_count} declined — retrying`;
    } else if (booking.requested_date) {
      text = `requested ${booking.requested_date} (${booking.requested_window || "?"})`;
    }
    bookingNode._sub.textContent = text;
  }
}

function fireEdge(key, labelText) {
  for (const el of Object.values(edgeEls)) el.classList.remove("fired", "escalation-edge");
  for (const el of Object.values(labelEls)) el.classList.remove("fired");
  const edge = edgeEls[key];
  if (edge) {
    edge.classList.add("fired");
    if (key.endsWith("-escalation")) edge.classList.add("escalation-edge");
  }
  const label = labelEls[key];
  if (label) label.classList.add("fired");
  if (labelText) document.getElementById("condition-line").textContent = labelText;
}

function eventPayload(e) {
  if (e.payload && typeof e.payload === "object") return e.payload;
  if (e.payload_json) {
    try {
      return JSON.parse(e.payload_json);
    } catch {
      return {};
    }
  }
  return {};
}

function extractConfidence(node) {
  for (let i = recentToolResults.length - 1; i >= 0; i--) {
    const r = recentToolResults[i];
    if (r.node !== node) continue;
    const match = /'confidence':\s*([0-9.]+)/.exec(r.summary);
    if (match) return parseFloat(match[1]);
  }
  return null;
}

function describeTransition(node, stageFrom, stageTo, pendingReply) {
  const reply = pendingReply || "";

  if (node === "greeting" && stageTo === "routing") {
    return { edge: "greeting-routing", text: "caller stated intent → entering routing" };
  }
  if (node === "routing" && stageTo === "routing") {
    return { edge: "routing-routing", text: "classification unclear (1st attempt) → re-prompt for area" };
  }
  if (node === "routing" && stageTo === "capture") {
    const areaMatch = /falls under (\w+) law/.exec(reply);
    const area = areaMatch ? areaMatch[1] : "an";
    return { edge: "routing-capture_fast", text: `classify_practice_area → "${area}" → entering capture (fast)` };
  }
  if (node === "routing" && stageTo === "escalation") {
    if (reply.includes("more than one area")) {
      return { edge: "routing-escalation", text: 'area="multiple_areas" → escalate (out_of_scope_multi_area)' };
    }
    return { edge: "routing-escalation", text: "2nd unclear classification → escalate (unable_to_classify)" };
  }
  // Phase 7: node_capture_fast's own trace events (node="capture_fast")
  // never distinguish "advanced to the next field" from "handed off to the
  // confirm/drain phase" via stage_from/stage_to alone — both are
  // stage="capture" the whole time, since fast/confirm are sub-phases of
  // the one "capture" stage, not separate stages. The reply text is the
  // only signal (same text-sniffing style already used above for routing/
  // booking) — _finish_fast_pass's handoff reply always starts with the
  // "let me just quickly confirm" preamble.
  if (node === "capture_fast" && stageTo === "capture") {
    if (reply.startsWith("Great, let me just quickly confirm")) {
      return { edge: "capture_fast-capture", text: "every field fast-asked → entering confirm/drain phase" };
    }
    return { edge: "capture_fast-capture_fast", text: "plausible direct answer → advance to next field, verifying in background" };
  }
  if (node === "capture_fast" && stageTo === "booking") {
    return { edge: null, text: "every field already confirmed → skipping straight to booking" };
  }
  if (node === "capture_fast" && stageTo === "escalation") {
    return { edge: "capture_fast-escalation", text: "attempts ≥ 3 on the last field, live → escalate (capture_failed)" };
  }
  if (node === "capture" && stageTo === "capture") {
    const conf = extractConfidence("capture");
    if (conf !== null) {
      if (conf < 0.4) return { edge: "capture-capture", text: `confidence ${conf} < 0.4 → discard, re-ask field` };
      if (conf < 0.75) return { edge: "capture-capture", text: `confidence ${conf} in [0.4, 0.75) → confirm-back` };
    }
    return { edge: "capture-capture", text: "field not yet confirmed → re-prompt or confirm-back" };
  }
  if (node === "capture" && stageTo === "booking") {
    return { edge: "capture-booking", text: "name + email + phone all status=confirmed → entering booking" };
  }
  if (node === "capture" && stageTo === "escalation") {
    return { edge: "capture-escalation", text: "attempts ≥ 3 on one field → escalate (capture_failed)" };
  }
  if (node === "booking" && stageTo === "booking") {
    if (reply.includes("other day") || reply.includes("what other")) {
      return { edge: "booking-booking", text: "caller declined slot → asking for another time" };
    }
    if (reply.includes("didn't catch")) {
      return { edge: "booking-booking", text: "no parseable date/time → re-ask" };
    }
    return { edge: "booking-booking", text: "slot proposed → awaiting caller confirmation" };
  }
  if (node === "booking" && stageTo === "escalation") {
    return { edge: "booking-escalation", text: "2 declines / no alternatives → escalate (no_acceptable_slot)" };
  }
  if (node === "escalation" && stageTo === "ended") {
    return { edge: null, text: "handoff note written → call ended (escalated)" };
  }
  if (node === "booking" && stageTo === "ended") {
    return { edge: null, text: "book_consultation succeeded → call ended (booked)" };
  }
  return { edge: null, text: `${node}: ${stageFrom} → ${stageTo}` };
}

function markVisited(nodeId) {
  const el = nodeEls[nodeId];
  if (el && !el.classList.contains("active")) el.classList.add("visited");
}

function handleTraceEvent(e) {
  const payload = eventPayload(e);
  appendFeedRow(e, payload);

  if (e.event_type === "node_entered") {
    if (activeNode) markVisited(activeNode);
    activeNode = e.node;
    setNodeState(e.node, "active", "working…");
    return;
  }

  if (e.event_type === "node_exited") {
    const { stage_from: stageFrom, stage_to: stageTo, pending_reply: pendingReply } = payload;
    setStagePill(stageTo);
    const { edge, text } = describeTransition(e.node, stageFrom, stageTo, pendingReply);
    if (edge) fireEdge(edge, text);
    else document.getElementById("condition-line").textContent = text;
    markVisited(e.node);
    if (activeNode === e.node) activeNode = null;
    setNodeState(e.node, "visited", stageTo === e.node ? "looped" : "done");
    return;
  }

  if (e.event_type === "tool_call_start") {
    return;
  }

  // Phase 7: the three reasons node_capture_fast declines to guess and
  // falls back to the real Capture (confirm) node — surfaced in the
  // condition line too, not just the raw feed, since they're the whole
  // point of the fast path's safety net.
  if (e.event_type === "capture_fast_gate_fallback") {
    document.getElementById("condition-line").textContent =
      `"${payload.utterance || ""}" doesn't look like a direct answer for ${payload.field} → falling back to real check`;
    return;
  }
  if (e.event_type === "capture_fast_urgent_reask") {
    document.getElementById("condition-line").textContent =
      `${payload.field}'s background check already failed → re-asking now instead of advancing`;
    return;
  }
  if (e.event_type === "capture_fast_pending_confirm_fallback") {
    document.getElementById("condition-line").textContent =
      `${payload.field} is already mid-confirmation → treating this as its answer, not a new field`;
    return;
  }

  if (e.event_type === "tool_call_end") {
    toolCallCount++;
    toolDurationSum += payload.duration_ms || 0;
    document.getElementById("hud-tool-count").textContent = toolCallCount;
    document.getElementById("hud-avg-duration").innerHTML =
      Math.round(toolDurationSum / toolCallCount) + '<span class="unit">ms</span>';
    recentToolResults.push({ node: e.node, tool_name: payload.tool_name, summary: payload.result_summary || payload.args || "" });
    if (recentToolResults.length > 12) recentToolResults.shift();
    return;
  }

  if (e.event_type === "reply_deferred") {
    deferredCount++;
    document.getElementById("hud-deferred-count").textContent = deferredCount;
    const badge = document.getElementById("async-badge");
    badge.className = "deferred";
    badge.textContent = "⏳ caller kept talking — reply queued, not sent yet";
    return;
  }

  if (e.event_type === "reply_delivered") {
    const badge = document.getElementById("async-badge");
    if (payload.was_deferred) {
      badge.className = "delivered";
      badge.textContent = `✓ deferred reply delivered after ${payload.wait_ms}ms — caller never noticed the wait`;
    } else {
      badge.className = "";
      badge.textContent = "reply delivered immediately (caller had stopped talking)";
    }
    return;
  }

  if (e.event_type === "reply_dropped_stale") {
    const badge = document.getElementById("async-badge");
    badge.className = "";
    badge.textContent = "a stale deferred reply was dropped (conversation had already moved on)";
    return;
  }

  if (e.event_type === "call_ended") {
    if (activeNode) markVisited(activeNode);
    activeNode = null;
    const outcome = payload.outcome;
    const banner = document.getElementById("status-banner");
    banner.className = outcome === "booked" ? "ended-booked" : outcome === "escalated" ? "ended-escalated" : "";
    document.getElementById("stage-pill").textContent = "ended";
    document.getElementById("status-text").textContent = `Call ended — outcome: ${outcome}`;
    return;
  }

  if (e.event_type === "call_abandoned") {
    const banner = document.getElementById("status-banner");
    banner.className = "ended-abandoned";
    document.getElementById("stage-pill").textContent = "abandoned";
    document.getElementById("status-text").textContent = "Caller disconnected before the call finished.";
    return;
  }
}

function setStagePill(stage) {
  document.getElementById("stage-pill").textContent = stage;
  document.getElementById("status-text").textContent = `Call in progress — currently in "${stage}"`;
}

function appendFeedRow(e, payload) {
  const feed = document.getElementById("feed");
  const empty = feed.querySelector(".empty-state");
  if (empty) empty.remove();

  const row = document.createElement("div");
  const failed = e.event_type === "tool_call_end" && payload.success === false;
  row.className = `feed-row t-${e.event_type}${failed ? " fail" : ""}`;

  let detail = "";
  if (e.event_type === "node_entered") detail = "";
  else if (e.event_type === "node_exited") detail = `${payload.stage_from} → ${payload.stage_to}`;
  else if (e.event_type === "tool_call_start") detail = payload.tool_name || "";
  else if (e.event_type === "tool_call_end")
    detail = `${payload.tool_name || ""} (${payload.duration_ms ?? "?"}ms)${failed ? " FAILED: " + (payload.error || "") : ""}`;
  else if (e.event_type === "reply_deferred") detail = payload.reason || "";
  else if (e.event_type === "reply_delivered") detail = `wait_ms=${payload.wait_ms}, deferred=${payload.was_deferred}`;
  else if (e.event_type === "reply_dropped_stale") detail = `dispatch_stage=${payload.dispatch_stage}, now=${payload.current_stage}`;
  else if (e.event_type === "call_ended") detail = `outcome=${payload.outcome}`;
  else if (e.event_type === "unhandled_error") detail = payload.error || "";
  else if (e.event_type === "capture_fast_gate_fallback") detail = `field=${payload.field}, utterance="${payload.utterance || ""}"`;
  else if (e.event_type === "capture_fast_urgent_reask" || e.event_type === "capture_fast_pending_confirm_fallback")
    detail = `field=${payload.field}`;

  row.innerHTML =
    `<span class="ftype">${e.event_type}</span>` +
    (e.node ? `<span class="fnode">${e.node}</span>` : "") +
    `<span class="fdetail">${escapeHtml(detail)}</span>`;
  feed.appendChild(row);
  feed.scrollTop = feed.scrollHeight;
}

function resetGraphState() {
  activeNode = null;
  recentToolResults = [];
  toolCallCount = 0;
  toolDurationSum = 0;
  deferredCount = 0;
  for (const nodeId of Object.keys(nodeEls)) setNodeState(nodeId, null, "idle");
  const idlePills = [fieldPillMarkup("N", "missing"), fieldPillMarkup("E", "missing"), fieldPillMarkup("P", "missing")].join("   ");
  for (const nodeId of ["capture_fast", "capture"]) {
    if (nodeEls[nodeId]) nodeEls[nodeId]._sub.innerHTML = idlePills;
  }
  if (nodeEls.booking) nodeEls.booking._sub.textContent = "no slot proposed yet";
  for (const el of Object.values(edgeEls)) el.classList.remove("fired", "escalation-edge");
  for (const el of Object.values(labelEls)) el.classList.remove("fired");
  document.getElementById("condition-line").textContent = "waiting for the next transition…";
  document.getElementById("hud-tool-count").textContent = "0";
  document.getElementById("hud-avg-duration").innerHTML = '—<span class="unit">ms</span>';
  document.getElementById("hud-deferred-count").textContent = "0";
  const badge = document.getElementById("async-badge");
  badge.className = "";
  badge.textContent = "No async activity yet — replies deliver as soon as the caller stops talking.";
  document.getElementById("status-banner").className = "";
  document.getElementById("stage-pill").textContent = "connecting…";
  document.getElementById("status-text").textContent = "Waiting for the first event…";
  document.getElementById("feed").innerHTML = '<div class="empty-state">Nothing streamed yet.</div>';
}

function watchCall(callId) {
  if (!callId) return;
  if (ws) {
    ws.onclose = null;
    ws.close();
  }
  currentCallId = callId;
  resetGraphState();

  const wsUrl = `ws://${location.host}/admin/trace/${encodeURIComponent(callId)}`;
  ws = new WebSocket(wsUrl);
  ws.onopen = () => setConnStatus(true, `watching ${callId}`);
  ws.onclose = () => setConnStatus(false, "disconnected");
  ws.onerror = () => setConnStatus(false, "connection error");
  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === "trace_events") {
      for (const e of msg.events) handleTraceEvent(e);
      return;
    }
    if (msg.type === "call_state") {
      renderCallStateBadges(msg);
      return;
    }
  };
}

function setConnStatus(live, text) {
  document.getElementById("conn-dot").classList.toggle("live", live);
  document.getElementById("conn-text").textContent = text;
}

async function refreshCallList() {
  try {
    const res = await fetch("/api/calls");
    const calls = await res.json();
    const select = document.getElementById("call-select");
    const prevValue = select.value;
    select.innerHTML = '<option value="">— recent calls —</option>';
    for (const call of calls.slice(0, 40)) {
      const opt = document.createElement("option");
      opt.value = call.call_id;
      const state = call.outcome ?? "in progress";
      opt.textContent = `${call.call_id}  (${state})`;
      select.appendChild(opt);
    }
    if (prevValue) select.value = prevValue;
  } catch {
    // admin panel already surfaces connectivity errors elsewhere; a failed
    // poll here just means the dropdown doesn't refresh this cycle
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

buildGraph();
refreshCallList();
setInterval(refreshCallList, 4000);

document.getElementById("call-select").addEventListener("change", (e) => {
  if (e.target.value) watchCall(e.target.value);
});
document.getElementById("watch-btn").addEventListener("click", () => {
  const val = document.getElementById("call-id-input").value.trim();
  if (val) watchCall(val);
});
document.getElementById("call-id-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("watch-btn").click();
});
