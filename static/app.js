// Housekeeping frontend. Plain JS, no build step.
// Append ?as_of=YYYY-MM-DD to the page URL to preview the plan for another date.

const AS_OF = new URLSearchParams(location.search).get("as_of");
const CATEGORIES = {
  appliance: "Appliances",
  home: "Home systems",
  outdoor: "Outdoor features",
  plant: "Plants & lawn",
  vehicle: "Vehicles",
};
const MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const $ = (sel) => document.querySelector(sel);
const ic = (name) => `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let state = { assets: [], tasks: [], settings: {} };

// ---------- helpers ----------

async function api(method, path, body) {
  const url = new URL(path, location.origin);
  if (AS_OF) url.searchParams.set("as_of", AS_OF);
  const isForm = body instanceof FormData;
  const res = await fetch(url, {
    method,
    headers: body && !isForm ? { "Content-Type": "application/json" } : {},
    body: isForm ? body : body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      const data = await res.json();
      if (data.detail) msg = typeof data.detail === "string" ? data.detail : data.detail.map((d) => d.msg).join("; ");
    } catch {}
    toast(msg, true);
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, isError = false) {
  const el = document.createElement("div");
  el.className = "toast " + (isError ? "error" : "ok");
  el.setAttribute("role", isError ? "alert" : "status");
  el.innerHTML = `${ic(isError ? "alert" : "check")}<span>${esc(msg)}</span>`;
  const stack = $("#toasts");
  stack.appendChild(el);
  while (stack.children.length > 3) stack.firstChild.remove();
  setTimeout(() => el.remove(), isError ? 7000 : 3000);
}

function fmtMinutes(min) {
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60), m = min % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function todayISO() {
  if (AS_OF) return AS_OF;
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function fmtDate(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-").map(Number);
  return `${MONTHS[m]} ${d}, ${y}`;
}

function scheduleText(t) {
  if (t.recurrence === "seasonal") return "Yearly in " + (t.months || "").split(",").map((m) => MONTHS[+m]).join(", ");
  if (t.recurrence === "mileage") {
    const parts = [`Every ${Number(t.mileage_interval).toLocaleString()} mi`];
    if (t.interval_days) parts.push(`or ${t.interval_days} days`);
    return parts.join(" ");
  }
  const d = t.interval_days;
  if (d % 365 === 0) return d === 365 ? "Yearly" : `Every ${d / 365} years`;
  if (d % 30 === 0 && d >= 30) return d === 30 ? "Monthly" : `Every ${d / 30} months`;
  if (d % 7 === 0) return d === 7 ? "Weekly" : `Every ${d / 7} weeks`;
  return `Every ${d} days`;
}

// Seasonal scores run 1.0 → 2.0 across their window, so only the last stretch counts as overdue.
function urgency(t) {
  if (t.score == null) return "";
  if (t.score >= (t.recurrence === "seasonal" ? 1.8 : 1.15)) return "overdue";
  if (t.score >= 0.85) return "due";
  return "ok";
}

// Two-click confirm, since the page can't rely on confirm() dialogs everywhere.
function armConfirm(btn, label, action) {
  if (btn.dataset.armed) return action();
  const original = btn.innerHTML;
  btn.dataset.armed = "1";
  btn.textContent = label;
  setTimeout(() => {
    delete btn.dataset.armed;
    btn.innerHTML = original;
  }, 3000);
}

async function busy(btn, label, fn) {
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner" aria-hidden="true"></span>${esc(label)}`;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

// ---------- dialogs ----------

function openDialog(id) {
  const d = document.getElementById(id);
  if (!d.open) d.showModal();
  return d;
}
const closeDialog = (id) => document.getElementById(id).close();
document.querySelectorAll("dialog.modal").forEach((d) => {
  d.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => d.close()));
  // Close when the backdrop (the dialog element itself, outside its form) is clicked.
  d.addEventListener("click", (e) => { if (e.target === d) d.close(); });
});

// File pickers styled as drop zones show the chosen file's name.
function dropzone(inputSel, labelSel, emptyText) {
  const input = $(inputSel), label = $(labelSel), zone = input.closest(".dropzone");
  const sync = () => {
    const f = input.files[0];
    label.textContent = f ? `${f.name} · ${(f.size / 1024 / 1024).toFixed(1)} MB` : emptyText;
    zone.classList.toggle("has-file", !!f);
  };
  input.addEventListener("change", sync);
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, () => zone.classList.add("drag")));
  ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove("drag")));
  return sync;
}
const syncUploadZone = dropzone("#upload-file", "#upload-file-name", "Choose a PDF, photo or text file");
const syncGuideZone = dropzone("#guide-file", "#guide-file-name", "Or upload a PDF or photo");

// ---------- tabs ----------

function showTab(name) {
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== `tab-${name}`));
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === name));
  history.replaceState(null, "", `${location.search}#${name}`);
  ({ plan: loadPlan, inventory: loadInventory, documents: loadDocuments, tasks: loadTasks, history: loadHistory, settings: loadSettings })[name]();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
document.addEventListener("click", (e) => {
  const link = e.target.closest("[data-goto]");
  if (!link) return;
  e.preventDefault();
  showTab(link.dataset.goto);
  scrollTo({ top: 0 });
});

// ---------- plan ----------

async function loadPlan(data) {
  const plan = data || (await api("GET", "/api/plan"));
  const weekly = plan.mode === "weekly";
  $("#plan-eyebrow").textContent = weekly ? `Week of ${fmtDate(plan.start)}` : fmtDate(plan.start);
  $("#plan-title").textContent = weekly ? "This week's maintenance" : "Today's maintenance";

  const done = plan.items.filter((i) => i.done).length;
  const minutesLeft = plan.items.filter((i) => !i.done).reduce((sum, i) => sum + (i.est_minutes || 0), 0);
  $("#plan-progress").style.width = plan.items.length ? `${(done / plan.items.length) * 100}%` : "0";
  $("#plan-progress-wrap").hidden = $("#plan-stats").hidden = !plan.items.length;
  $("#plan-stat-done").textContent = `${done} of ${plural(plan.items.length, "task")} done`;
  $("#plan-stat-time").textContent = done === plan.items.length ? "All done for this period" : minutesLeft ? `About ${fmtMinutes(minutesLeft)} left` : "";

  const list = $("#plan-list");
  list.innerHTML = plan.items.map(planItemHTML).join("");
  list.querySelectorAll("[data-check]").forEach((b) => b.addEventListener("click", () => onCheck(plan, +b.dataset.check)));
  list.querySelectorAll("[data-snooze]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api("POST", `/api/tasks/${b.dataset.snooze}/snooze?days=7`);
      toast("Snoozed for a week");
      loadPlan();
    })
  );

  const empty = $("#plan-empty");
  empty.hidden = plan.items.length > 0;
  if (!plan.items.length) empty.innerHTML = await emptyPlanHTML();

  $("#up-next-wrap").hidden = !plan.backlog_count;
  $("#backlog-count").textContent = plan.backlog_count ? `· ${plan.backlog_count} beyond this plan` : "";
  $("#up-next").innerHTML = plan.up_next
    .map((t) => `<li><span>${esc(t.title)}${t.asset_name ? ` <span class="muted">· ${esc(t.asset_name)}</span>` : ""}</span><span>${esc(t.reason)}</span></li>`)
    .join("");
  $("#plan-more").hidden = !plan.backlog_count;
  $("#plan-regen").hidden = !plan.items.length && !plan.backlog_count;
}

// First run: a checklist of setup steps. Afterwards: a quiet "nothing due" note.
async function emptyPlanHTML() {
  const [settings, assets, tasks] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/assets"), api("GET", "/api/tasks")]);
  const active = tasks.filter((t) => t.status === "active").length;
  if (active) {
    return `<div class="empty">${ic("check")}<strong>Nothing is due right now</strong><p>Every active task is up to date. Enjoy the time off.</p></div>`;
  }
  const suggested = tasks.filter((t) => t.status === "suggested").length;
  const steps = [
    [!!settings.location, "Tell it where you live", "Location and climate decide when seasonal work happens.", "settings", "Settings"],
    [assets.length > 0, "Add what you own", "Appliances, home systems, yard and plants, cars.", "inventory", "Inventory"],
    [suggested > 0, "Get maintenance tasks", "Have Claude suggest a schedule for each item, or import your own guide.", "inventory", "Suggest tasks"],
    [false, "Approve the ones you want", "Your weekly plan fills up from approved tasks.", "tasks", "Review"],
  ];
  const next = steps.findIndex(([done]) => !done);
  return `
    <div class="onboard">
      <h2>Set up your home</h2>
      <p>Housekeeping turns everything you own into a short list of maintenance for each ${settings.plan_mode === "daily" ? "day" : "week"}. Four steps to your first plan:</p>
      <ol class="steps">
        ${steps.map(([done, title, sub, tab, cta], i) => `
          <li class="${done ? "done" : ""}">
            <div><div class="step-title">${title}</div><div class="step-sub">${sub}</div></div>
            ${done ? "" : `<a class="btn small ${i === next ? "" : "ghost"}" href="#${tab}" data-goto="${tab}">${cta}</a>`}
          </li>`).join("")}
      </ol>
    </div>`;
}

function planItemHTML(t) {
  const u = t.done ? "done" : urgency(t);
  const tagLabel = t.done ? "Done" : u === "overdue" ? "Overdue" : "Due";
  const meta = [
    t.asset_name && esc(t.asset_name),
    esc(scheduleText(t)),
    t.est_minutes && `~${t.est_minutes} min`,
    !t.done && esc(t.reason),
  ].filter(Boolean);
  return `
    <li class="plan-item ${u}" id="plan-${t.id}">
      <button class="check" data-check="${t.id}" aria-label="${t.done ? "Mark not done" : "Mark done"}: ${esc(t.title)}">${t.done ? ic("check") : ""}</button>
      <div class="plan-body">
        <div class="plan-title">${esc(t.title)}</div>
        <div class="plan-meta">${meta.map((m) => `<span>${m}</span>`).join("")}</div>
        ${t.description ? `<p class="plan-desc">${esc(t.description)}</p>` : ""}
      </div>
      <div class="plan-side">
        <span class="tag-row">
          ${t.priority === 1 && !t.done ? `<span class="tag">High priority</span>` : ""}
          <span class="tag ${u === "ok" ? "due" : u}">${tagLabel}</span>
        </span>
        ${t.done ? "" : `<button class="btn ghost small" data-snooze="${t.id}">${ic("clock")}Snooze a week</button>`}
      </div>
    </li>`;
}

async function onCheck(plan, taskId) {
  const item = plan.items.find((i) => i.id === taskId);
  if (item.done) {
    await api("DELETE", `/api/completions/${item.completion_id}`);
    return loadPlan();
  }
  if (item.recurrence === "mileage") return showMileageForm(item);
  await api("POST", "/api/completions", { task_id: taskId });
  toast(`Logged: ${item.title}`);
  loadPlan();
}

function showMileageForm(item) {
  const li = $(`#plan-${item.id}`);
  if (li.querySelector(".inline-done")) return;
  const form = document.createElement("form");
  form.className = "inline-done";
  form.innerHTML = `
    <label>Odometer reading <input id="mi-${item.id}" type="number" min="0" placeholder="${item.asset_mileage ?? ""}"></label>
    <label>Notes <input id="notes-${item.id}" placeholder="Shop, oil type, cost"></label>
    <button class="btn small" type="submit">${ic("check")}Log it</button>`;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const mileage = form.querySelector(`#mi-${item.id}`).value;
    await api("POST", "/api/completions", {
      task_id: item.id,
      mileage: mileage ? +mileage : null,
      notes: form.querySelector(`#notes-${item.id}`).value,
    });
    toast(`Logged: ${item.title}`);
    loadPlan();
  });
  li.appendChild(form);
  form.querySelector("input").focus();
}

$("#plan-regen").addEventListener("click", (e) => busy(e.currentTarget, "Rebuilding…", async () => { await loadPlan(await api("POST", "/api/plan/regenerate")); toast("Plan rebuilt"); }));
$("#plan-more").addEventListener("click", async () => loadPlan(await api("POST", "/api/plan/more?n=1")));

// ---------- inventory ----------

async function loadInventory() {
  state.assets = await api("GET", "/api/assets");
  const wrap = $("#inventory-list");
  if (!state.assets.length) {
    wrap.innerHTML = `<div class="empty">${ic("inventory")}<strong>Nothing here yet</strong><p>Start with the things that cost the most to neglect: furnace, water heater, roof and gutters, your cars.</p><button class="btn" data-new-asset>${ic("plus")}Add your first item</button></div>`;
    wrap.querySelector("[data-new-asset]").addEventListener("click", () => openAssetForm());
    return;
  }
  const groups = {};
  state.assets.forEach((a) => (groups[a.category] ||= []).push(a));
  wrap.innerHTML = Object.keys(CATEGORIES)
    .filter((c) => groups[c])
    .map(
      (c) => `
      <div class="cat-block">
        <h2 class="cat-title">${CATEGORIES[c]} <span class="muted">${groups[c].length}</span></h2>
        <div class="rows">${groups[c].map(assetRowHTML).join("")}</div>
      </div>`
    )
    .join("");
  wrap.querySelectorAll("[data-suggest]").forEach((b) =>
    b.addEventListener("click", () =>
      busy(b, "Asking Claude…", async () => {
        const r = await api("POST", `/api/assets/${b.dataset.suggest}/suggest`);
        toast(r.added ? `${r.added} suggestions ready to review under Tasks` : "No new tasks suggested");
        refreshBadge();
        loadInventory();
      })
    )
  );
  wrap.querySelectorAll("[data-find]").forEach((b) =>
    b.addEventListener("click", () =>
      busy(b, "Searching the web…", async () => {
        const r = await api("POST", `/api/assets/${b.dataset.find}/find-documents`);
        toast(findSummary(r), !r.added.length && r.skipped.length > 0);
        loadInventory();
      })
    )
  );
  wrap.querySelectorAll("[data-edit-asset]").forEach((b) => b.addEventListener("click", () => openAssetForm(state.assets.find((a) => a.id === +b.dataset.editAsset))));
  wrap.querySelectorAll("[data-del-asset]").forEach((b) =>
    b.addEventListener("click", () =>
      armConfirm(b, "Delete item and its tasks?", async () => {
        await api("DELETE", `/api/assets/${b.dataset.delAsset}`);
        toast("Deleted");
        loadInventory();
      })
    )
  );
}

function assetRowHTML(a) {
  const sub = [a.details, a.year, a.mileage != null && `${Number(a.mileage).toLocaleString()} mi`].filter(Boolean).map(esc).join(" · ");
  const chips = [
    a.task_count && `<span class="chip">${plural(a.task_count, "active task")}</span>`,
    a.suggested_count && `<a class="chip accent" href="#tasks" data-goto="tasks">${a.suggested_count} to review</a>`,
    a.doc_count && `<a class="chip" href="#documents" data-goto="documents">${plural(a.doc_count, "document")}</a>`,
  ].filter(Boolean).join("");
  return `
    <div class="asset-row">
      <div class="asset-info">
        <div class="asset-name">${esc(a.name)}</div>
        <div class="asset-sub">${sub || "No details yet. Add the brand and model for better suggestions."}</div>
        ${chips ? `<div class="chips">${chips}</div>` : ""}
      </div>
      <div class="actions">
        <button class="btn ghost small" data-find="${a.id}">${ic("search")}Find manuals</button>
        <button class="btn small" data-suggest="${a.id}">${ic("sparkle")}Suggest tasks</button>
        <button class="btn ghost small" data-edit-asset="${a.id}" aria-label="Edit ${esc(a.name)}">${ic("edit")}</button>
        <button class="btn danger small" data-del-asset="${a.id}" aria-label="Delete ${esc(a.name)}">${ic("trash")}</button>
      </div>
    </div>`;
}

function openAssetForm(a = {}) {
  openDialog("asset-dialog");
  $("#asset-dialog-title").textContent = a.id ? `Edit ${a.name}` : "Add item";
  $("#asset-id").value = a.id ?? "";
  $("#asset-category").value = a.category ?? "appliance";
  $("#asset-name").value = a.name ?? "";
  $("#asset-details").value = a.details ?? "";
  $("#asset-year").value = a.year ?? "";
  $("#asset-mileage").value = a.mileage ?? "";
  $("#asset-notes").value = a.notes ?? "";
  toggleMileage();
  $("#asset-name").focus();
}
function toggleMileage() {
  $("#asset-mileage-wrap").hidden = $("#asset-category").value !== "vehicle";
}
$("#asset-category").addEventListener("change", toggleMileage);
$("#asset-new").addEventListener("click", () => openAssetForm());
$("#asset-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#asset-id").value;
  const body = {
    category: $("#asset-category").value,
    name: $("#asset-name").value.trim(),
    details: $("#asset-details").value.trim(),
    year: $("#asset-year").value ? +$("#asset-year").value : null,
    mileage: $("#asset-category").value === "vehicle" && $("#asset-mileage").value ? +$("#asset-mileage").value : null,
    notes: $("#asset-notes").value.trim(),
  };
  await api(id ? "PUT" : "POST", id ? `/api/assets/${id}` : "/api/assets", body);
  closeDialog("asset-dialog");
  toast(id ? "Saved" : `Added ${body.name}`);
  loadInventory();
});

// ---------- documents ----------

const KIND_LABELS = { manual: "Owner's manual", maintenance: "Maintenance schedule", guide: "Guide", photo: "Photo", other: "Document" };
const SOURCE_LABELS = { found: "Found by Claude", uploaded: "Uploaded", link: "From a link" };

function findSummary(r) {
  if (!r.found) return "Claude couldn't find manuals for this item. Try adding the exact model under Edit, or upload one.";
  const parts = [];
  if (r.added.length) parts.push(`Saved ${r.added.length} document${r.added.length > 1 ? "s" : ""}: ${r.added.map((d) => d.title).join(", ")}`);
  if (r.skipped.length) parts.push(`Skipped ${r.skipped.length} (${r.skipped.map((d) => d.reason).join("; ")})`);
  const unread = r.added.filter((d) => d.read_error);
  if (unread.length) parts.push(`Couldn't read ${unread.length}: ${unread[0].read_error}`);
  return parts.join(". ");
}

function fillAssetSelects() {
  const opts = `<option value="">General (whole home)</option>` + state.assets.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
  document.querySelectorAll(".asset-select").forEach((sel) => {
    const v = sel.value;
    sel.innerHTML = opts;
    sel.value = v;
  });
}

async function loadDocuments() {
  const [docs, assets] = await Promise.all([api("GET", "/api/documents"), api("GET", "/api/assets")]);
  state.assets = assets;
  fillAssetSelects();
  const wrap = $("#documents-list");
  if (!docs.length) {
    wrap.innerHTML = `<div class="empty">${ic("documents")}<strong>No documents yet</strong><p>Upload a manual or a photo of an appliance's data plate, or use <a href="#inventory" data-goto="inventory">Find manuals</a> on an item under Inventory.</p></div>`;
    return;
  }
  const groups = new Map();
  docs.forEach((d) => {
    const key = d.asset_name || "General";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(d);
  });
  const ordered = [...groups.keys()].sort((a, b) => (a === "General" ? -1 : b === "General" ? 1 : a.localeCompare(b)));
  wrap.innerHTML = ordered.map((k) => `<div class="cat-block"><h2 class="cat-title">${esc(k)} <span class="muted">${groups.get(k).length}</span></h2><div class="rows">${groups.get(k).map(docHTML).join("")}</div></div>`).join("");

  wrap.querySelectorAll("[data-read]").forEach((b) =>
    b.addEventListener("click", () =>
      busy(b, "Reading…", async () => {
        await api("POST", `/api/documents/${b.dataset.read}/read`);
        toast("Notes updated");
        loadDocuments();
      })
    )
  );
  wrap.querySelectorAll("[data-doc-tasks]").forEach((b) =>
    b.addEventListener("click", () =>
      busy(b, "Asking Claude…", async () => {
        const r = await api("POST", `/api/documents/${b.dataset.docTasks}/tasks`);
        toast(r.added ? `${r.added} suggestions ready to review under Tasks` : "No new tasks suggested");
        refreshBadge();
      })
    )
  );
  wrap.querySelectorAll("[data-del-doc]").forEach((b) =>
    b.addEventListener("click", () =>
      armConfirm(b, "Delete document?", async () => {
        await api("DELETE", `/api/documents/${b.dataset.delDoc}`);
        loadDocuments();
      })
    )
  );
}

function docHTML(d) {
  const meta = [
    KIND_LABELS[d.kind] || "Document",
    d.pages && `${d.pages} pages`,
    SOURCE_LABELS[d.source],
    d.read_mode === "text" && "read as text (long PDF)",
  ].filter(Boolean);
  const n = d.notes;
  const notes = n
    ? `<details>
        <summary>What Claude found${n.maintenance.length ? ` · ${n.maintenance.length} maintenance item${n.maintenance.length > 1 ? "s" : ""}` : ""}${n.parts.length ? ` · ${n.parts.length} part${n.parts.length > 1 ? "s" : ""}` : ""}</summary>
        <div class="notes">
          <p>${esc(n.summary)}${n.model_identified ? ` <strong>Model: ${esc(n.model_identified)}</strong>` : ""}</p>
          ${n.maintenance.length ? `<div><h4>Maintenance</h4><ul>${n.maintenance.map((m) => `<li><strong>${esc(m.task)}</strong>: ${esc(m.frequency)}. ${esc(m.details)} ${m.page ? `<span class="page">p. ${esc(m.page)}</span>` : ""}</li>`).join("")}</ul></div>` : ""}
          ${n.parts.length ? `<div><h4>Parts &amp; specs</h4><ul>${n.parts.map((p) => `<li>${esc(p.name)}: <span class="mono">${esc(p.spec)}</span></li>`).join("")}</ul></div>` : ""}
          ${n.warnings.length ? `<div><h4>Warnings</h4><ul class="warn">${n.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
        </div>
      </details>`
    : `<div class="unread">${ic("alert")}Claude hasn't read this yet, so suggestions won't use it.</div>`;
  return `
    <div class="doc">
      <div class="doc-head">
        <div>
          <span class="doc-icon">${ic(d.kind === "photo" ? "search" : "documents")}</span>
          <div>
            <a class="doc-title" href="/api/documents/${d.id}/file" target="_blank" rel="noopener">${esc(d.title)}</a>
            <div class="plan-meta">${meta.map((m) => `<span>${esc(m)}</span>`).join("")}${d.source_url ? `<a href="${esc(d.source_url)}" target="_blank" rel="noopener noreferrer">Source ${ic("external")}</a>` : ""}</div>
          </div>
        </div>
        <div class="actions">
          <button class="btn small" data-doc-tasks="${d.id}">${ic("sparkle")}${d.asset_id ? "Suggest tasks" : "Turn into tasks"}</button>
          <button class="btn ghost small" data-read="${d.id}">${ic("refresh")}${n ? "Read again" : "Read now"}</button>
          <button class="btn danger small" data-del-doc="${d.id}" aria-label="Delete ${esc(d.title)}">${ic("trash")}</button>
        </div>
      </div>
      ${notes}
    </div>`;
}

$("#upload-open").addEventListener("click", () => openDialog("upload-dialog"));
$("#link-open").addEventListener("click", () => { openDialog("link-dialog"); $("#link-url").focus(); });

function afterAdd(r) {
  if (r.read_error) toast(`Saved, but Claude couldn't read it: ${r.read_error}`, true);
  else toast(`Saved and read: ${r.document.title}`);
  loadDocuments();
}

$("#upload-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy($("#upload-submit"), "Uploading and reading…", async () => {
    const form = new FormData();
    form.append("file", $("#upload-file").files[0]);
    if ($("#upload-asset").value) form.append("asset_id", $("#upload-asset").value);
    form.append("kind", $("#upload-kind").value);
    form.append("title", $("#upload-title").value.trim());
    const r = await api("POST", "/api/documents/upload", form);
    $("#upload-form").reset();
    syncUploadZone();
    closeDialog("upload-dialog");
    afterAdd(r);
  });
});

$("#link-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy($("#link-submit"), "Downloading and reading…", async () => {
    const r = await api("POST", "/api/documents/link", {
      url: $("#link-url").value.trim(),
      asset_id: $("#link-asset").value ? +$("#link-asset").value : null,
      title: $("#link-title").value.trim(),
    });
    $("#link-form").reset();
    closeDialog("link-dialog");
    afterAdd(r);
  });
});

// ---------- tasks ----------

async function loadTasks() {
  [state.tasks, state.assets] = await Promise.all([api("GET", "/api/tasks"), api("GET", "/api/assets")]);
  const suggested = state.tasks.filter((t) => t.status === "suggested");
  const active = state.tasks.filter((t) => t.status === "active").sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  const paused = state.tasks.filter((t) => t.status === "paused");
  refreshBadge(suggested.length);

  $("#suggested-wrap").hidden = !suggested.length;
  $("#suggested-count").textContent = `· ${suggested.length}`;
  $("#suggested-list").innerHTML = suggested.map(suggestionHTML).join("");
  $("#suggested-list").querySelectorAll("[data-approve]").forEach((b) =>
    b.addEventListener("click", async () => {
      const last = $(`#last-${b.dataset.approve}`).value || null;
      await api("POST", `/api/tasks/${b.dataset.approve}/approve`, { last_done_on: last });
      loadTasks();
    })
  );
  $("#suggested-list").querySelectorAll("[data-dismiss]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api("DELETE", `/api/tasks/${b.dataset.dismiss}`);
      loadTasks();
    })
  );
  $("#suggested-list").querySelectorAll("[data-edit-task]").forEach((b) => b.addEventListener("click", () => openTaskForm(state.tasks.find((t) => t.id === +b.dataset.editTask))));
  if (suggested.length > 1) {
    $("#suggested-list").insertAdjacentHTML("beforeend", `<div class="form-actions"><button class="btn ghost" id="approve-all">${ic("check")}Approve all ${suggested.length}</button></div>`);
    $("#approve-all").addEventListener("click", (e) =>
      busy(e.currentTarget, "Approving…", async () => {
        for (const t of suggested) {
          await api("POST", `/api/tasks/${t.id}/approve`, { last_done_on: $(`#last-${t.id}`).value || null });
        }
        toast(`Approved ${plural(suggested.length, "task")}`);
        loadTasks();
      })
    );
  }

  const head = `<thead><tr><th>Task</th><th>Schedule</th><th>Last done</th><th>Status</th><th><span class="sr-only">Actions</span></th></tr></thead>`;
  $("#active-title").innerHTML = `Active <span class="muted">${active.length || ""}</span>`;
  $("#task-table").innerHTML = active.length
    ? head + `<tbody>${active.map((t) => taskRowHTML(t, false)).join("")}</tbody>`
    : `<tr><td class="empty-cell">No active tasks yet. Approve some suggestions, or add your own.</td></tr>`;
  $("#paused-wrap").hidden = !paused.length;
  $("#paused-table").innerHTML = head + `<tbody>${paused.map((t) => taskRowHTML(t, true)).join("")}</tbody>`;

  document.querySelectorAll("#tab-tasks table [data-edit-task]").forEach((b) => b.addEventListener("click", () => openTaskForm(state.tasks.find((t) => t.id === +b.dataset.editTask))));
  document.querySelectorAll("[data-pause]").forEach((b) => b.addEventListener("click", async () => { await api("POST", `/api/tasks/${b.dataset.pause}/status/paused`); loadTasks(); }));
  document.querySelectorAll("[data-resume]").forEach((b) => b.addEventListener("click", async () => { await api("POST", `/api/tasks/${b.dataset.resume}/status/active`); loadTasks(); }));
  document.querySelectorAll("[data-done-now]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api("POST", "/api/completions", { task_id: +b.dataset.doneNow });
      toast("Logged as done today");
      loadTasks();
    })
  );
  document.querySelectorAll("[data-del-task]").forEach((b) =>
    b.addEventListener("click", () => armConfirm(b, "Delete for good?", async () => { await api("DELETE", `/api/tasks/${b.dataset.delTask}`); loadTasks(); }))
  );
}

function suggestionHTML(t) {
  return `
    <div class="suggestion">
      <div class="suggestion-head">
        <div>
          <div class="plan-title">${esc(t.title)}</div>
          <div class="plan-meta">
            <span>${esc(t.asset_name || "General")}</span><span>${esc(scheduleText(t))}</span>
            ${t.est_minutes ? `<span>~${t.est_minutes} min</span>` : ""}
            <span>${t.source === "guide" ? "From your guide" : "Suggested by Claude"}</span>
          </div>
        </div>
        ${t.priority === 1 ? `<span class="tag">High priority</span>` : ""}
      </div>
      ${t.description ? `<p class="plan-desc">${esc(t.description)}</p>` : ""}
      <div class="suggestion-actions">
        <label>Last done (optional) <input type="date" id="last-${t.id}" max="${todayISO()}"></label>
        <button class="btn small" data-approve="${t.id}">${ic("check")}Approve</button>
        <button class="btn ghost small" data-edit-task="${t.id}">${ic("edit")}Edit</button>
        <button class="btn danger small" data-dismiss="${t.id}">${ic("x")}Dismiss</button>
      </div>
    </div>`;
}

function taskRowHTML(t, paused) {
  const u = paused ? "" : urgency(t);
  return `
    <tr>
      <td><div>${esc(t.title)}</div><div class="sub">${esc(t.asset_name || "General")}</div></td>
      <td>${esc(scheduleText(t))}</td>
      <td class="num">${fmtDate(t.last_done_on)}</td>
      <td>${paused ? `<span class="sub">Paused</span>` : `<span class="status"><span class="status-dot ${u}"></span>${esc(t.reason || "")}</span>`}</td>
      <td class="actions-cell">
        ${paused
          ? `<button class="btn ghost small" data-resume="${t.id}">${ic("play")}Resume</button><button class="btn danger small" data-del-task="${t.id}" aria-label="Delete ${esc(t.title)}">${ic("trash")}</button>`
          : `<button class="btn ghost small" data-done-now="${t.id}">${ic("check")}Done today</button><button class="btn ghost small" data-edit-task="${t.id}" aria-label="Edit ${esc(t.title)}">${ic("edit")}</button><button class="btn ghost small" data-pause="${t.id}" aria-label="Pause ${esc(t.title)}">${ic("pause")}</button>`}
      </td>
    </tr>`;
}

function openTaskForm(t = {}) {
  openDialog("task-dialog");
  $("#task-dialog-title").textContent = t.id ? (t.status === "suggested" ? "Edit suggestion" : "Edit task") : "Add task";
  $("#task-asset").innerHTML = `<option value="">General (no item)</option>` + state.assets.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
  $("#task-id").value = t.id ?? "";
  $("#task-status").value = t.status ?? "active";
  $("#task-source").value = t.source ?? "manual";
  $("#task-title").value = t.title ?? "";
  $("#task-description").value = t.description ?? "";
  $("#task-asset").value = t.asset_id ?? "";
  $("#task-recurrence").value = t.recurrence ?? "interval";
  $("#task-interval").value = t.interval_days ?? "";
  $("#task-months").value = t.months ?? "";
  $("#task-mileage").value = t.mileage_interval ?? "";
  $("#task-priority").value = t.priority ?? 2;
  $("#task-minutes").value = t.est_minutes ?? "";
  toggleRecurrence();
  $("#task-title").focus();
}
function toggleRecurrence() {
  const r = $("#task-recurrence").value;
  document.querySelectorAll("#task-form [data-rec]").forEach((el) => (el.hidden = !el.dataset.rec.split(" ").includes(r)));
}
$("#task-recurrence").addEventListener("change", toggleRecurrence);
$("#task-new").addEventListener("click", () => openTaskForm());
$("#task-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#task-id").value;
  const num = (sel) => ($(sel).value ? +$(sel).value : null);
  const rec = $("#task-recurrence").value;
  const body = {
    asset_id: num("#task-asset"),
    title: $("#task-title").value.trim(),
    description: $("#task-description").value.trim(),
    recurrence: rec,
    interval_days: rec === "seasonal" ? null : num("#task-interval"),
    months: rec === "seasonal" ? $("#task-months").value.trim() : null,
    mileage_interval: rec === "mileage" ? num("#task-mileage") : null,
    priority: +$("#task-priority").value,
    est_minutes: num("#task-minutes"),
    status: $("#task-status").value,
    source: $("#task-source").value,
  };
  await api(id ? "PUT" : "POST", id ? `/api/tasks/${id}` : "/api/tasks", body);
  closeDialog("task-dialog");
  toast("Saved");
  loadTasks();
});

$("#guide-open").addEventListener("click", () => { openDialog("guide-dialog"); $("#guide-text").focus(); });
$("#guide-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const file = $("#guide-file").files[0];
  if (!file && !$("#guide-text").value.trim()) return toast("Paste your guide or choose a file first", true);
  busy($("#guide-submit"), "Reading your guide…", async () => {
    let r;
    if (file) {
      // Keep the file under Documents as a general guide, then turn it into tasks.
      const form = new FormData();
      form.append("file", file);
      form.append("kind", "guide");
      form.append("read", "false");
      const up = await api("POST", "/api/documents/upload", form);
      r = await api("POST", `/api/documents/${up.document.id}/tasks`);
      $("#guide-file").value = "";
      syncGuideZone();
    } else {
      r = await api("POST", "/api/guide", { text: $("#guide-text").value });
    }
    toast(`${plural(r.added, "task")} ready to review`);
    closeDialog("guide-dialog");
    $("#guide-text").value = "";
    loadTasks();
  });
});

async function refreshBadge(n) {
  if (n == null) n = (await api("GET", "/api/tasks")).filter((t) => t.status === "suggested").length;
  const b = $("#suggested-badge");
  b.hidden = !n;
  b.textContent = n;
}

// ---------- history ----------

async function loadHistory() {
  const rows = await api("GET", "/api/history");
  const t = $("#history-table");
  if (!rows.length) {
    t.innerHTML = `<tr><td class="empty-cell">Nothing logged yet. Check items off your plan and they show up here.</td></tr>`;
    return;
  }
  t.innerHTML =
    `<thead><tr><th>Date</th><th>Task</th><th>Notes</th><th><span class="sr-only">Actions</span></th></tr></thead>` +
    rows
      .map(
        (r) => `
      <tr>
        <td class="num">${fmtDate(r.done_on)}</td>
        <td><div>${esc(r.title)}</div><div class="sub">${esc(r.asset_name || "General")}</div></td>
        <td>${[r.mileage && `${Number(r.mileage).toLocaleString()} mi`, r.notes].filter(Boolean).map(esc).join(" · ") || ""}</td>
        <td class="actions-cell"><button class="btn danger small" data-undo="${r.id}" aria-label="Remove entry">${ic("trash")}</button></td>
      </tr>`
      )
      .join("");
  t.querySelectorAll("[data-undo]").forEach((b) =>
    b.addEventListener("click", () => armConfirm(b, "Remove entry?", async () => { await api("DELETE", `/api/completions/${b.dataset.undo}`); loadHistory(); }))
  );
}

// ---------- settings ----------

async function loadSettings() {
  const s = (state.settings = await api("GET", "/api/settings"));
  $("#set-location").value = s.location;
  $("#set-climate").value = s.climate_notes;
  $("#set-hemisphere").value = s.hemisphere;
  $("#set-mode").value = s.plan_mode;
  $("#set-count").value = s.plan_count;
}
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("PUT", "/api/settings", {
    location: $("#set-location").value.trim(),
    climate_notes: $("#set-climate").value.trim(),
    hemisphere: $("#set-hemisphere").value,
    plan_mode: $("#set-mode").value,
    plan_count: String(Math.max(1, +$("#set-count").value || 5)),
  });
  toast("Settings saved");
});

// ---------- boot ----------

// ---------- theme, status, boot ----------

const THEMES = ["system", "light", "dark"];
function applyTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  const btn = $("#theme-btn");
  btn.innerHTML = ic({ system: "system", light: "sun", dark: "moon" }[t]);
  btn.setAttribute("aria-label", `Theme: ${t}. Switch theme`);
  btn.title = `Theme: ${t}`;
}
let theme = "system";
try { theme = localStorage.getItem("hk-theme") || "system"; } catch {}
if (!THEMES.includes(theme)) theme = "system";
applyTheme(theme);
$("#theme-btn").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  try { localStorage.setItem("hk-theme", theme); } catch {}
  applyTheme(theme);
});

async function loadStatus() {
  const s = await api("GET", "/api/status");
  $("#footer-version").textContent = `v${s.version}`;
  $("#about-version").textContent = s.version;
  $("#about-ai").innerHTML = s.ai_configured ? `Connected <span class="muted">· ${esc(s.model)}</span>` : "Not configured (set ANTHROPIC_API_KEY)";
  let dismissed = false;
  try { dismissed = localStorage.getItem("hk-banner-dismissed") === "1"; } catch {}
  $("#setup-banner").hidden = s.ai_configured || dismissed;
}
$("#banner-close").addEventListener("click", () => {
  $("#setup-banner").hidden = true;
  try { localStorage.setItem("hk-banner-dismissed", "1"); } catch {}
});

$("#today-label").textContent = (AS_OF ? "Previewing " : "") + fmtDate(todayISO());
loadStatus();
refreshBadge();
const startTab = location.hash.slice(1);
showTab(["plan", "inventory", "documents", "tasks", "history", "settings"].includes(startTab) ? startTab : "plan");
