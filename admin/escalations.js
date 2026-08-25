const app = document.getElementById("app");
const queueCountEl = document.getElementById("queue-count");

async function load() {
  const res = await fetch("/api/escalations");
  const escalations = await res.json();

  const unreachable = escalations.filter(isUnreachable).length;
  queueCountEl.textContent = escalations.length
    ? `${escalations.length} escalated` + (unreachable ? ` · ${unreachable} unreachable` : "")
    : "";
  queueCountEl.style.display = escalations.length ? "" : "none";

  if (escalations.length === 0) {
    app.innerHTML = `<div class="empty-state"><div class="big">✓</div>No calls have been escalated.</div>`;
    return;
  }

  app.innerHTML =
    `<p class="lede">Every call the agent handed to a human, newest first — why they rang, what we
     confirmed about them, and why the agent gave up. Only confirmed details appear: a value the
     caller never read back is a guess at noisy audio, not a number worth ringing.</p>` +
    escalations.map(card).join("");
}

// The one thing that makes a handoff unworkable rather than merely thin:
// nothing to reach the caller on. Flagged so it's visible at a glance
// instead of discovered after reading the whole card.
function isUnreachable(e) {
  return !e.caller_phone && !e.caller_email;
}

function card(e) {
  const unreachable = isUnreachable(e);
  return `
    <div class="card${unreachable ? " unreachable" : ""}">
      <div class="card-head">
        <h2>${escapeHtml(e.call_id)}</h2>
        <span class="when">${formatWhen(e.escalated_at)}</span>
      </div>
      <div class="badges">
        <span class="badge reason">${escapeHtml(e.escalation_reason ?? "reason not recorded")}</span>
        ${
          e.practice_area
            ? `<span class="badge area">${escapeHtml(e.practice_area)}</span>`
            : `<span class="badge undetermined">area undetermined</span>`
        }
        ${unreachable ? `<span class="badge unreachable">no way to reach them</span>` : ""}
      </div>

      <h3>Why they called</h3>
      ${prose(e.reason_for_call, "Never captured — the call didn't get that far.")}

      <h3>Why this was escalated</h3>
      ${prose(e.escalation_explanation, "Not recorded.")}

      <h3>Caller</h3>
      <div class="contact">
        ${field("Name", e.caller_name)}
        ${field("Email", e.caller_email)}
        ${field("Phone", e.caller_phone)}
      </div>

      <div class="card-foot">
        <a href="/admin?call=${encodeURIComponent(e.call_id)}">Open transcript →</a>
      </div>
    </div>
  `;
}

function prose(text, fallback) {
  return text
    ? `<p class="prose">${escapeHtml(text)}</p>`
    : `<p class="prose missing">${escapeHtml(fallback)}</p>`;
}

function field(label, value) {
  const shown = value
    ? `<span class="v">${escapeHtml(value)}</span>`
    : `<span class="v missing">not captured</span>`;
  return `<div class="field"><span class="k">${label}</span>${shown}</div>`;
}

function formatWhen(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return isNaN(date) ? iso : date.toLocaleString();
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

load();
