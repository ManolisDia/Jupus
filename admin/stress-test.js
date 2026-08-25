const modeSelect = document.getElementById("mode-select");
const nLevelsInput = document.getElementById("n-levels-input");
const runBtn = document.getElementById("run-btn");
const liveNote = document.getElementById("live-note");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const tbody = document.getElementById("results-tbody");
const verdictEl = document.getElementById("verdict");

const LIVE_SAFETY_CAP = 10;

modeSelect.addEventListener("change", () => {
  liveNote.classList.toggle("shown", modeSelect.value === "live");
});

function parseNLevels() {
  return nLevelsInput.value
    .split(",")
    .map((s) => parseInt(s.trim(), 10))
    .filter((n) => Number.isFinite(n) && n > 0);
}

function setStatus(state, text) {
  statusDot.className = state;
  statusText.textContent = text;
}

function resetResults() {
  tbody.innerHTML = "";
  verdictEl.style.display = "none";
  verdictEl.className = "";
}

function appendResultRow(result) {
  const empty = tbody.querySelector(".empty-state");
  if (empty) empty.closest("tr").remove();

  const row = document.createElement("tr");
  row.className = "new-row";
  const leak = result.cross_call_leakage_found;
  row.innerHTML =
    `<td>${result.n}</td>` +
    `<td>${result.wall_clock_ms.toFixed(1)}ms</td>` +
    `<td>${result.mean_per_call_ms.toFixed(1)}ms</td>` +
    `<td>${result.median_per_call_ms.toFixed(1)}ms</td>` +
    `<td>${result.p95_per_call_ms.toFixed(1)}ms</td>` +
    `<td><span class="leak-pill ${leak ? "yes" : "no"}">${leak ? "LEAKAGE" : "clean"}</span></td>`;
  tbody.appendChild(row);
}

function renderVerdict(verdict, status, error) {
  verdictEl.style.display = "block";
  if (status === "error") {
    verdictEl.className = "error";
    verdictEl.textContent = `Run failed: ${error || "unknown error"}`;
    return;
  }
  if (!verdict) {
    verdictEl.className = "";
    verdictEl.textContent = "Run finished with no results.";
    return;
  }
  const leakWarning = verdict.any_leakage
    ? " ⚠ Cross-call state leakage was detected at one or more N levels — see the table above."
    : "";
  verdictEl.className = verdict.any_leakage ? "leak" : "ok";
  verdictEl.textContent =
    `Verdict: holds up cleanly through N=${verdict.holds_through_n} ` +
    `(per-call median latency stayed within ${verdict.degradation_multiple}x of ` +
    `N=${verdict.baseline_n}'s ${verdict.baseline_median_ms.toFixed(1)}ms baseline).${leakWarning}`;
}

function watchRun(runId) {
  const ws = new WebSocket(`ws://${location.host}/admin/stress-test-stream/${encodeURIComponent(runId)}`);
  ws.onopen = () => setStatus("running", `Run ${runId} in progress…`);
  ws.onerror = () => setStatus("error", "Stream connection error.");
  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === "level_result") {
      appendResultRow(msg.result);
      setStatus("running", `Run ${runId} in progress — N=${msg.result.n} complete…`);
      return;
    }
    if (msg.type === "run_finished") {
      setStatus(msg.status, msg.status === "done" ? `Run ${runId} finished.` : `Run ${runId} failed.`);
      renderVerdict(msg.verdict, msg.status, msg.error);
      runBtn.disabled = false;
      ws.close();
      return;
    }
    if (msg.type === "error") {
      setStatus("error", msg.error || "Unknown stream error.");
      runBtn.disabled = false;
      ws.close();
    }
  };
}

async function startRun() {
  const mode = modeSelect.value;
  const nLevels = parseNLevels();
  if (nLevels.length === 0) {
    alert("Enter at least one valid N level, e.g. 5, 10, 20");
    return;
  }
  if (mode === "live") {
    const over = nLevels.filter((n) => n > LIVE_SAFETY_CAP);
    if (over.length > 0) {
      alert(`Live mode refuses any N > ${LIVE_SAFETY_CAP} to bound real API spend. Remove: ${over.join(", ")}`);
      return;
    }
    const confirmed = confirm(
      `This will make real, billed Claude API calls for N levels [${nLevels.join(", ")}]. Continue?`
    );
    if (!confirmed) return;
  }

  runBtn.disabled = true;
  resetResults();
  setStatus("running", "Starting run…");

  try {
    const res = await fetch("/api/stress-test/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, n_levels: nLevels }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      setStatus("error", body.detail || `Failed to start run (${res.status}).`);
      runBtn.disabled = false;
      return;
    }
    const { run_id } = await res.json();
    watchRun(run_id);
  } catch (err) {
    setStatus("error", `Failed to start run: ${err}`);
    runBtn.disabled = false;
  }
}

runBtn.addEventListener("click", startRun);
