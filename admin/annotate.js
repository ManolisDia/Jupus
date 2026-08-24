const app = document.getElementById("app");
const queueCountEl = document.getElementById("queue-count");

let errorClasses = [];
let queue = [];

async function loadErrorClasses() {
  const res = await fetch("/api/eval/error-classes");
  errorClasses = await res.json();
}

async function loadQueue() {
  const res = await fetch("/api/calls/unreviewed");
  queue = await res.json();
  queueCountEl.textContent = `${queue.length} unreviewed`;
  queueCountEl.style.display = queue.length ? "" : "none";
}

async function renderNext() {
  await loadQueue();
  if (queue.length === 0) {
    app.innerHTML = `<div class="empty-state"><div class="big">✓</div>Nothing left to review.</div>`;
    return;
  }
  const call = queue[0];

  const detailRes = await fetch(`/api/calls/${encodeURIComponent(call.call_id)}`);
  const detail = await detailRes.json();

  const transcriptHtml = (detail.transcript || [])
    .map((t) => `<div class="transcript-turn ${t.role}">${escapeHtml(t.text)}</div>`)
    .join("");

  const checklistHtml = errorClasses
    .map(
      (c) => `<label><input type="checkbox" name="error_class" value="${c.id}">
        <span><span class="name">${c.name}</span><span class="desc">${escapeHtml(c.description).slice(0, 90)}...</span></span></label>`
    )
    .join("");

  const llmFlagsHtml = (detail.call_error_flags || [])
    .map((f) => `${f.error_class_id} (confidence ${f.confidence})`)
    .join(", ") || "none";

  app.innerHTML = `
    <div class="card">
      <h2>${call.call_id}</h2>
      <p class="subline"><span class="badge">${call.outcome ?? "in progress"}</span><span class="badge">${call.practice_area ?? "—"}</span></p>
      <div id="transcript-block">${transcriptHtml || "<em>No transcript.</em>"}</div>
      <div class="llm-reference"><strong>LLM judge flags (reference, not constraint):</strong> ${llmFlagsHtml}</div>

      <h3>Which error classes apply?</h3>
      <div class="checklist">${checklistHtml}</div>

      <h3>Doesn't fit any class?</h3>
      <div id="uncategorized-notes"></div>
      <button type="button" id="add-note">+ Add note</button>

      <h3>Overall note</h3>
      <textarea id="overall-note" rows="3"></textarea>

      <div class="gold-row"><label><input type="checkbox" id="is-gold"> Mark as gold example</label></div>

      <button class="primary" id="submit-review">Submit &amp; next</button>
    </div>
  `;

  document.getElementById("add-note").addEventListener("click", addNoteRow);
  document.getElementById("submit-review").addEventListener("click", () => submitReview(call.call_id));
}

function addNoteRow() {
  const container = document.getElementById("uncategorized-notes");
  const row = document.createElement("div");
  row.className = "note-row";
  row.innerHTML = `<input type="text" placeholder="Describe the issue…">`;
  container.appendChild(row);
}

async function submitReview(callId) {
  const errorClassIds = Array.from(document.querySelectorAll('input[name="error_class"]:checked')).map(
    (el) => el.value
  );
  const uncategorizedNotes = Array.from(document.querySelectorAll("#uncategorized-notes input"))
    .map((el) => el.value.trim())
    .filter(Boolean);
  const overallNote = document.getElementById("overall-note").value;
  const isGold = document.getElementById("is-gold").checked;

  await fetch(`/api/calls/${encodeURIComponent(callId)}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      error_class_ids: errorClassIds,
      uncategorized_notes: uncategorizedNotes,
      overall_note: overallNote,
      is_gold: isGold,
    }),
  });

  renderNext();
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

loadErrorClasses().then(renderNext);
