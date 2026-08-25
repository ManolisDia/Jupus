const tablesList = document.getElementById("tables-list");
const tableView = document.getElementById("table-view");

const PAGE_SIZE = 100;
let currentTable = null;
let currentOffset = 0;

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function cellHtml(value) {
  if (value === null || value === undefined) return `<td class="null">null</td>`;
  return `<td>${escapeHtml(value)}</td>`;
}

async function loadTables() {
  const res = await fetch("/api/dev/tables");
  const tables = await res.json();
  tablesList.innerHTML = "";
  for (const name of tables) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = name;
    btn.addEventListener("click", () => selectTable(name));
    tablesList.appendChild(btn);
  }
}

async function selectTable(name, offset = 0) {
  currentTable = name;
  currentOffset = offset;
  for (const btn of tablesList.querySelectorAll("button")) {
    btn.classList.toggle("active", btn.textContent === name);
  }

  const res = await fetch(`/api/dev/tables/${encodeURIComponent(name)}?limit=${PAGE_SIZE}&offset=${offset}`);
  if (!res.ok) {
    tableView.innerHTML = `<div class="empty-state">Failed to load table "${escapeHtml(name)}".</div>`;
    return;
  }
  const data = await res.json();
  renderTable(data);
}

function renderTable(data) {
  const { table, columns, rows, total, limit, offset } = data;
  const from = rows.length ? offset + 1 : 0;
  const to = offset + rows.length;

  const headerHtml = `
    <div id="table-head">
      <div>
        <h2>${escapeHtml(table)}</h2>
        <div class="count">${total} row${total === 1 ? "" : "s"} — showing ${from}–${to}</div>
      </div>
      <div class="pager">
        <button type="button" id="prev-page" ${offset === 0 ? "disabled" : ""}>&larr; Prev</button>
        <button type="button" id="next-page" ${to >= total ? "disabled" : ""}>Next &rarr;</button>
      </div>
    </div>`;

  const theadHtml = `<thead><tr>${columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>`;
  const tbodyHtml = rows.length
    ? `<tbody>${rows.map((row) => `<tr>${columns.map((c) => cellHtml(row[c])).join("")}</tr>`).join("")}</tbody>`
    : "";

  tableView.innerHTML = `${headerHtml}<div class="table-scroll"><table>${theadHtml}${tbodyHtml}</table></div>`;
  if (!rows.length) {
    tableView.querySelector(".table-scroll").innerHTML = `<div class="empty-state">This table is empty.</div>`;
  }

  document.getElementById("prev-page").addEventListener("click", () => {
    selectTable(table, Math.max(0, offset - limit));
  });
  document.getElementById("next-page").addEventListener("click", () => {
    selectTable(table, offset + limit);
  });
}

loadTables();
