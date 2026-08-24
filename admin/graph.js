const SVG_NS = "http://www.w3.org/2000/svg";

const NODES = [
  { id: "greeting", label: "Greeting", x: 20, y: 70, w: 190, h: 68 },
  { id: "routing", label: "Routing", x: 280, y: 70, w: 190, h: 68 },
  { id: "capture", label: "Capture", x: 540, y: 70, w: 190, h: 68 },
  { id: "booking", label: "Booking", x: 800, y: 70, w: 190, h: 68 },
  { id: "escalation", label: "Escalation", x: 540, y: 280, w: 190, h: 68 },
];

const MAIN_EDGES = [
  { key: "greeting-routing", d: "M210,104 L280,104", labelPos: [245, 96] },
  { key: "routing-capture", d: "M470,104 L540,104", labelPos: [505, 96] },
  { key: "capture-booking", d: "M730,104 L800,104", labelPos: [765, 96] },
];

const LOOP_EDGES = [
  { key: "routing-routing", d: "M340,70 C340,14 410,14 410,70", labelPos: [375, 26] },
  { key: "capture-capture", d: "M600,70 C600,14 670,14 670,70", labelPos: [635, 26] },
  { key: "booking-booking", d: "M860,70 C860,14 930,14 930,70", labelPos: [895, 26] },
];

const ANY_EDGES = [
  { key: "greeting-escalation", d: "M115,138 C115,220 400,262 560,280" },
  { key: "routing-escalation", d: "M375,138 C375,200 500,252 605,280" },
  { key: "capture-escalation", d: "M635,138 L635,280" },
  { key: "booking-escalation", d: "M895,138 C895,200 780,252 675,280" },
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
  anyLabel.setAttribute("x", 405);
  anyLabel.setAttribute("y", 224);
  anyLabel.setAttribute("text-anchor", "middle");
  anyLabel.setAttribute("class", "edge-label");
  anyLabel.textContent = "any stage, any time";
  document.getElementById("edges-any").appendChild(anyLabel);

  for (const n of NODES) {
    const g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("class", "node-box");
    g.setAttribute("data-node", n.id);

    if (n.id === "capture") {
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent =
        "Field-by-field capture (name/email/phone) is plain deterministic branching inside " +
        "this one node, not separate graph nodes — N/E/P below show each field's live status.";
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

// Capture and booking render their own richer sub-state (see
// renderCallStateBadges below) driven by real CallState, not the generic
// idle/working/done label — don't let node_entered/node_exited stomp it.
const RICH_SUBSTATE_NODES = new Set(["capture", "booking"]);

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
  const captureNode = nodeEls.capture;
  if (captureNode && profile.name && profile.email && profile.phone) {
    captureNode._sub.innerHTML = [
      fieldPillMarkup("N", profile.name.status),
      fieldPillMarkup("E", profile.email.status),
      fieldPillMarkup("P", profile.phone.status),
    ].join("   ");
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
    return { edge: "routing-capture", text: `classify_practice_area → "${area}" → entering capture` };
  }
  if (node === "routing" && stageTo === "escalation") {
    if (reply.includes("more than one area")) {
      return { edge: "routing-escalation", text: 'area="multiple_areas" → escalate (out_of_scope_multi_area)' };
    }
    return { edge: "routing-escalation", text: "2nd unclear classification → escalate (unable_to_classify)" };
  }
  if (node === "capture" && stageTo === "capture") {
    const conf = extractConfidence("capture");
    if (conf !== null) {
      if (conf < 0.4) return { edge: "capture-capture", text: `confidence ${conf} < 0.4 → discard, re-ask field` };
      if (conf < 0.75) return { edge: "capture-capture", text: `confidence ${conf} in [0.4, 0.75) → confirm-back` };
    }
    return { edge: "capture-capture", text: "field not yet confirmed → re-prompt" };
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
  if (nodeEls.capture) nodeEls.capture._sub.innerHTML = [fieldPillMarkup("N", "missing"), fieldPillMarkup("E", "missing"), fieldPillMarkup("P", "missing")].join("   ");
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
