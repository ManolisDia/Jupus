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

// Phase 11 (latency + cost instrumentation) — stage keys match
// eval.insights_agent.LATENCY_STAGES exactly.
const LATENCY_STAGE_LABELS = {
  stt_and_dialogue_decision: "STT + dialogue decision",
  supervisor_processing: "Supervisor processing",
  deferred_wait: "Deferred wait",
  tts_first_audio: "TTS first audio",
  total_perceived: "Total perceived",
};

function latencyAndCostCard(latency, cost) {
  const stageRows = Object.entries(LATENCY_STAGE_LABELS)
    .map(([key, label]) => {
      const s = (latency && latency[key]) || { p50: 0, p95: 0, avg: 0 };
      return `<div class="meta-item"><span class="k">${label}</span><span class="v">${s.avg.toFixed(0)}ms avg &middot; ${s.p50.toFixed(0)}ms p50 &middot; ${s.p95.toFixed(0)}ms p95</span></div>`;
    })
    .join("");
  const c = cost || { average_usd: 0, p50_usd: 0, p95_usd: 0 };
  const costRow = `<div class="meta-item"><span class="k">Est. cost / call</span><span class="v">$${c.average_usd.toFixed(4)} avg &middot; p50 $${c.p50_usd.toFixed(4)} &middot; p95 $${c.p95_usd.toFixed(4)}</span></div>`;
  return `
    <div class="breakdown-card">
      <span class="label">Latency &amp; cost (estimated)</span>
      <div class="meta-card">${stageRows}${costRow}</div>
    </div>
  `;
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
      <span class="label">Avg latency / turn</span>
      <span class="value">${(summary.latency?.total_perceived?.avg ?? 0).toFixed(0)}<span class="unit">ms</span></span>
    </div>
    ${latencyAndCostCard(summary.latency, summary.cost)}
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
  // ?call=<id> deep-links straight into one call's detail — how the handoff
  // queue (admin/escalations.html) hands off to the transcript.
  const requested = new URLSearchParams(location.search).get("call");
  if (requested && calls.some((c) => c.call_id === requested)) {
    selectCall(requested);
    document
      .querySelector(`tr.call-row[data-call-id="${CSS.escape(requested)}"]`)
      ?.scrollIntoView({ block: "center" });
  }
}

function _mean(values) {
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
}

function callLatencyCard(latency) {
  // Real per-call breakdown for GET /api/calls/{call_id}/latency — this is
  // the thing worth pointing at for one specific call (Q1's answer + the
  // cost question), distinct from the aggregate panel above. A stage/call
  // can have multiple turns worth of data; averaged here for a single
  // display number per stage.
  if (!latency) {
    return `<div class="review-card empty">No latency data recorded for this call.</div>`;
  }
  const stageRows = Object.entries(LATENCY_STAGE_LABELS)
    .map(([key, label]) => {
      const values = (latency.stages && latency.stages[key]) || [];
      const avg = _mean(values);
      return `<div class="meta-item"><span class="k">${label}</span><span class="v">${
        avg === null ? "no data" : `${avg.toFixed(0)}ms (${values.length} turn${values.length === 1 ? "" : "s"})`
      }</span></div>`;
    })
    .join("");
  const cost = latency.cost || {};
  const costRow = `<div class="meta-item"><span class="k">Est. cost</span><span class="v">$${(cost.cost_usd ?? 0).toFixed(4)}</span></div>`;
  return `<div class="meta-card">${stageRows}${costRow}</div>`;
}

async function selectCall(callId) {
  selectedCallId = callId;
  for (const row of callsTbody.querySelectorAll("tr")) {
    row.classList.toggle("selected", row.dataset.callId === callId);
  }

  const [res, latencyRes] = await Promise.all([
    fetch(`/api/calls/${encodeURIComponent(callId)}`),
    fetch(`/api/calls/${encodeURIComponent(callId)}/latency`),
  ]);
  if (!res.ok) {
    detailPanel.innerHTML = `<div class="empty-state">Call not found.</div>`;
    return;
  }
  const detail = await res.json();
  // Phase 11 — a call with zero trace_events (shouldn't normally happen for
  // a listed call, but degrade gracefully rather than breaking the rest of
  // the detail view over it) 404s this endpoint independently of the call
  // row above.
  const latency = latencyRes.ok ? await latencyRes.json() : null;

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

  const callLatencyHtml = callLatencyCard(latency);

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
    <h3>Latency &amp; cost (estimated)</h3>
    ${callLatencyHtml}
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
