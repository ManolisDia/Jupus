const callsTbody = document.getElementById("calls-tbody");
const callsCountEl = document.getElementById("calls-count");
const detailPanel = document.getElementById("detail-panel");
const summaryEl = document.getElementById("summary");
const taxonomyPanel = document.getElementById("taxonomy-panel");
const insightsBadge = document.getElementById("insights-badge");

let selectedCallId = null;

for (const tabBtn of document.querySelectorAll("#tabnav button")) {
  tabBtn.addEventListener("click", () => switchView(tabBtn.dataset.view));
}

function switchView(view) {
  for (const tabBtn of document.querySelectorAll("#tabnav button")) {
    tabBtn.classList.toggle("active", tabBtn.dataset.view === view);
  }
  document.getElementById("view-calls").classList.toggle("hidden", view !== "calls");
  document.getElementById("view-insights").classList.toggle("hidden", view !== "insights");
}

function chipRow(entries, formatValue) {
  if (!entries.length) {
    return `<div class="chip-row"><span class="chip empty">none recorded</span></div>`;
  }
  return `<div class="chip-row">${entries
    .map(([k, v]) => `<span class="chip">${escapeHtml(k)} <span class="n">${formatValue(v)}</span></span>`)
    .join("")}</div>`;
}

async function loadSummary() {
  const res = await fetch("/api/eval/summary");
  const summary = await res.json();

  const escalationEntries = Object.entries(summary.escalation_reason_histogram || {});
  const errorRateEntries = Object.entries(summary.error_rates || {});

  summaryEl.innerHTML = `
    <div class="stat-card accent-good">
      <span class="label">Booking success</span>
      <span class="value">${(summary.booking_success_rate * 100).toFixed(0)}<span class="unit">%</span></span>
    </div>
    <div class="stat-card">
      <span class="label">Avg turns / call</span>
      <span class="value">${summary.average_turns_per_call.toFixed(1)}</span>
    </div>
    <div class="stat-card">
      <span class="label">Latency p50</span>
      <span class="value">${summary.latency.p50.toFixed(0)}<span class="unit">ms</span></span>
    </div>
    <div class="stat-card">
      <span class="label">Latency p95</span>
      <span class="value">${summary.latency.p95.toFixed(0)}<span class="unit">ms</span></span>
    </div>
    <div class="breakdown-card">
      <span class="label">Escalation reasons</span>
      ${chipRow(escalationEntries, (v) => v)}
    </div>
    <div class="breakdown-card">
      <span class="label">Error rates (all runs)</span>
      ${chipRow(errorRateEntries, (v) => `${(v * 100).toFixed(0)}%`)}
    </div>
  `;
}

async function loadTaxonomySuggestions() {
  const res = await fetch("/api/eval/taxonomy-suggestions?status=pending");
  const suggestions = await res.json();
  if (suggestions.length === 0) {
    taxonomyPanel.innerHTML = "";
    insightsBadge.classList.add("hidden");
    return;
  }
  insightsBadge.textContent = suggestions.length;
  insightsBadge.classList.remove("hidden");
  taxonomyPanel.innerHTML = `
    <div class="taxonomy-card">
      <div class="taxonomy-head"><span class="dot"></span>Pending taxonomy suggestions (${suggestions.length})</div>
      <div class="taxonomy-list">
        ${suggestions
          .map(
            (s) => `<div class="suggestion-row" data-id="${s.id}">
              <span class="suggestion-text"><span class="tag">${s.suggestion_type}</span>${s.suggested_name ? `${s.suggested_name} — ` : ""}${s.related_error_class_id ? `(${s.related_error_class_id}) ` : ""}${escapeHtml(s.rationale)}</span>
              <span class="actions">
                <button class="approve-suggestion" data-id="${s.id}">Approve</button>
                <button class="reject-suggestion" data-id="${s.id}">Reject</button>
              </span>
            </div>`
          )
          .join("")}
      </div>
    </div>
  `;

  for (const btn of taxonomyPanel.querySelectorAll(".approve-suggestion")) {
    btn.addEventListener("click", () => resolveSuggestion(btn.dataset.id, "approve"));
  }
  for (const btn of taxonomyPanel.querySelectorAll(".reject-suggestion")) {
    btn.addEventListener("click", () => resolveSuggestion(btn.dataset.id, "reject"));
  }
}

async function resolveSuggestion(id, action) {
  await fetch(`/api/eval/taxonomy-suggestions/${id}/${action}`, { method: "POST" });
  loadTaxonomySuggestions();
}

async function loadCalls() {
  const res = await fetch("/api/calls");
  const calls = await res.json();
  callsCountEl.textContent = calls.length;
  callsTbody.innerHTML = "";
  for (const call of calls) {
    const tr = document.createElement("tr");
    tr.className = "call-row";
    tr.dataset.callId = call.call_id;
    const errorBadges = (call.error_classes || [])
      .map((c) => `<span class="error-badge">${c}</span>`)
      .join("") || `<span class="no-errors">—</span>`;
    tr.innerHTML = `
      <td>${call.call_id}</td>
      <td>${call.practice_area ?? "—"}</td>
      <td><span class="badge ${call.outcome ?? ""}">${call.outcome ?? "in progress"}</span></td>
      <td>${errorBadges}</td>
      <td><span class="reviewed-dot ${call.reviewed ? "yes" : "no"}" title="${call.reviewed ? "reviewed" : "needs review"}"></span></td>
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

  const errorFlagsHtml = (detail.call_error_flags || [])
    .map(
      (f) => `<li><strong>${f.error_class_id}</strong> <span class="meta">— confidence ${f.confidence}, run ${f.eval_run_label}</span><br>${escapeHtml(f.evidence ?? "")}</li>`
    )
    .join("");

  const humanReviewHtml = detail.human_review
    ? `<div class="review-card">
         <strong>BD review${detail.human_review.is_gold ? `<span class="gold-tag">Gold</span>` : ""}</strong>
         <p style="margin:6px 0 0;">${escapeHtml(detail.human_review.overall_note ?? "")}</p>
         <ul>${(detail.human_review.annotations || [])
           .map((a) => `<li>${a.error_class_id ? a.error_class_id : "uncategorized"}: ${escapeHtml(a.note ?? "")}</li>`)
           .join("")}</ul>
       </div>`
    : `<div class="review-card empty">Not yet reviewed by the Benevolent Dictator.</div>`;

  detailPanel.innerHTML = `
    <h2>${detail.call_id}</h2>
    <div class="meta-card">
      <div class="meta-item"><span class="k">Area</span><span class="v">${detail.practice_area ?? "—"}</span></div>
      <div class="meta-item"><span class="k">Outcome</span><span class="v">${detail.outcome ?? "in progress"}</span></div>
      ${detail.escalation_reason ? `<div class="meta-item"><span class="k">Reason</span><span class="v">${detail.escalation_reason}</span></div>` : ""}
      <div class="meta-item"><span class="k">Name</span><span class="v">${detail.caller_name ?? "—"}</span></div>
      <div class="meta-item"><span class="k">Email</span><span class="v">${detail.caller_email ?? "—"}</span></div>
      <div class="meta-item"><span class="k">Phone</span><span class="v">${detail.caller_phone ?? "—"}</span></div>
    </div>
    <h3>Error-class flags</h3>
    ${errorFlagsHtml ? `<ul class="flag-list">${errorFlagsHtml}</ul>` : `<div class="flag-list none-flagged">None flagged.</div>`}
    <h3>Human review</h3>
    ${humanReviewHtml}
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
loadTaxonomySuggestions();
