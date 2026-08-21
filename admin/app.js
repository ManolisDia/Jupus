const callsTbody = document.getElementById("calls-tbody");
const detailPanel = document.getElementById("detail-panel");
const summaryEl = document.getElementById("summary");

let selectedCallId = null;

async function loadSummary() {
  const res = await fetch("/api/eval/summary");
  const summary = await res.json();
  summaryEl.innerHTML = `
    <div class="stat"><span class="label">Booking success rate</span><span class="value">${(summary.booking_success_rate * 100).toFixed(0)}%</span></div>
    <div class="stat"><span class="label">Avg turns / call</span><span class="value">${summary.average_turns_per_call.toFixed(1)}</span></div>
    <div class="stat"><span class="label">Latency p50 / p95</span><span class="value">${summary.latency.p50.toFixed(0)}ms / ${summary.latency.p95.toFixed(0)}ms</span></div>
    <div class="stat"><span class="label">Escalation reasons</span><span class="value">${
      Object.entries(summary.escalation_reason_histogram).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"
    }</span></div>
  `;
}

async function loadCalls() {
  const res = await fetch("/api/calls");
  const calls = await res.json();
  callsTbody.innerHTML = "";
  for (const call of calls) {
    const tr = document.createElement("tr");
    tr.className = "call-row";
    tr.dataset.callId = call.call_id;
    tr.innerHTML = `
      <td>${call.call_id}</td>
      <td>${call.practice_area ?? "—"}</td>
      <td><span class="badge ${call.outcome ?? ""}">${call.outcome ?? "in progress"}</span></td>
    `;
    tr.addEventListener("click", () => selectCall(call.call_id));
    callsTbody.appendChild(tr);
  }
}

async function selectCall(callId) {
  selectedCallId = callId;
  for (const row of callsTbody.querySelectorAll("tr")) {
    row.classList.toggle("selected", row.dataset.callId === callId);
  }

  const res = await fetch(`/api/calls/${encodeURIComponent(callId)}`);
  if (!res.ok) {
    detailPanel.innerHTML = `<div class="empty-state">Call not found.</div>`;
    return;
  }
  const detail = await res.json();

  const transcriptHtml = (detail.transcript || [])
    .map(
      (turn) => `<div class="transcript-turn ${turn.role}">
        <div class="role">${turn.role}</div>
        <div>${escapeHtml(turn.text)}</div>
      </div>`
    )
    .join("");

  detailPanel.innerHTML = `
    <h2>${detail.call_id}</h2>
    <p>
      <strong>Area:</strong> ${detail.practice_area ?? "—"} &nbsp;
      <strong>Outcome:</strong> ${detail.outcome ?? "in progress"} &nbsp;
      ${detail.escalation_reason ? `<strong>Reason:</strong> ${detail.escalation_reason}` : ""}
    </p>
    <p>
      <strong>Name:</strong> ${detail.caller_name ?? "—"} &nbsp;
      <strong>Email:</strong> ${detail.caller_email ?? "—"} &nbsp;
      <strong>Phone:</strong> ${detail.caller_phone ?? "—"}
    </p>
    <h3>Transcript</h3>
    <div id="transcript">${transcriptHtml || "<em>No transcript.</em>"}</div>
    <button id="trace-toggle">Show full trace</button>
    <div id="trace-view" style="display:none;"></div>
  `;

  traceLoaded = false;
  document.getElementById("trace-toggle").addEventListener("click", () => toggleTrace(callId));
}

let traceLoaded = false;

async function toggleTrace(callId) {
  const traceView = document.getElementById("trace-view");
  const button = document.getElementById("trace-toggle");
  const showing = traceView.style.display !== "none";
  if (showing) {
    traceView.style.display = "none";
    button.textContent = "Show full trace";
    return;
  }
  button.textContent = "Hide full trace";
  traceView.style.display = "block";
  if (traceLoaded) return;

  const res = await fetch(`/api/calls/${encodeURIComponent(callId)}/trace`);
  const events = await res.json();
  traceLoaded = true;
  traceView.innerHTML = events
    .map(
      (e) => `<div class="trace-event">
        <span class="seq">#${e.seq}</span>
        <span class="event-type">${e.event_type}</span>
        <span class="node">${e.node ?? ""}</span>
        <span class="payload">${escapeHtml(JSON.stringify(e.payload ?? e.payload_json ?? {}))}</span>
      </div>`
    )
    .join("");
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

loadSummary();
loadCalls();
