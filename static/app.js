// Housekeeping frontend. Plain JS, no build step.
// Append ?as_of=YYYY-MM-DD to the page URL to preview the plan for another date.

const AS_OF = new URLSearchParams(location.search).get("as_of");
const CATEGORIES = {
  appliance: "Appliances",
  home: "Home systems",
  outdoor: "Outdoor",
  plant: "Plants and lawn",
  vehicle: "Vehicles",
};
const MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const $ = (sel) => document.querySelector(sel);
const ic = (name) => `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const meta = (parts) => parts.filter(Boolean).join(" · ");

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
    let msg = `Something went wrong (${res.status})`;
    try {
      const data = await res.json();
      if (data.detail) msg = typeof data.detail === "string" ? data.detail : data.detail.map((d) => d.msg).join("; ");
    } catch {}
    toast(msg, true);
    throw new Error(msg);
  }
  return res.json();
}

// Toasts enter and leave along the same path. Timers pause while the tab is hidden, so nothing expires unseen.
function toast(msg, isError = false) {
  const stack = $("#toasts");
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.setAttribute("role", isError ? "alert" : "status");
  el.textContent = msg;
  stack.appendChild(el);
  [...stack.children].filter((t) => !t.classList.contains("leaving")).slice(0, -3).forEach(dismissToast);
  let remaining = isError ? 7000 : 3200;
  let started = Date.now();
  let timer = setTimeout(() => dismissToast(el), remaining);
  el._pause = () => { clearTimeout(timer); remaining -= Date.now() - started; };
  el._resume = () => { started = Date.now(); timer = setTimeout(() => dismissToast(el), Math.max(remaining, 800)); };
}
function dismissToast(el) {
  if (el.classList.contains("leaving")) return;
  el.classList.add("leaving");
  el.addEventListener("transitionend", () => el.remove(), { once: true });
  setTimeout(() => el.remove(), 400);
}
document.addEventListener("visibilitychange", () => {
  document.querySelectorAll(".toast:not(.leaving)").forEach((t) => (document.hidden ? t._pause?.() : t._resume?.()));
});

function fmtMinutes(min) {
  if (min < 60) return `${min} min`;
  const quarters = Math.round(min / 15), h = Math.floor(quarters / 4), m = (quarters % 4) * 15;
  return m ? `${h} h ${m} min` : `${h} h`;
}

function todayISO() {
  if (AS_OF) return AS_OF;
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function fmtDate(iso, withYear = true) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  return withYear ? `${MONTHS[m]} ${d}, ${y}` : `${MONTHS[m]} ${d}`;
}

function fmtRange(start, end) {
  const [, m1, d1] = start.split("-").map(Number);
  const [, m2, d2] = end.split("-").map(Number);
  return m1 === m2 ? `${MONTHS[m1]} ${d1}–${d2}` : `${MONTHS[m1]} ${d1} – ${MONTHS[m2]} ${d2}`;
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
  const width = btn.classList.contains("icon");
  btn.dataset.armed = "1";
  btn.textContent = label;
  if (width) btn.classList.remove("icon");
  setTimeout(() => {
    delete btn.dataset.armed;
    btn.innerHTML = original;
    if (width) btn.classList.add("icon");
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

// Not every mobile browser focuses a tapped row, so do it here; :focus-within then reveals the row's actions.
document.addEventListener("pointerdown", (e) => {
  const row = e.target.closest('.row[tabindex="-1"]');
  if (row && !e.target.closest("button, a, input, select, textarea, summary")) row.focus({ preventScroll: true });
});

const on = (root, sel, fn) => root.querySelectorAll(sel).forEach((el) => el.addEventListener("click", (e) => fn(el, e)));

// ---------- dialogs ----------

function openDialog(id) {
  const d = document.getElementById(id);
  if (!d.open) d.showModal();
  return d;
}
const closeDialog = (id) => document.getElementById(id).close();
document.querySelectorAll("dialog.modal").forEach((d) => {
  d.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => d.close()));
  // Close when the scrim (the dialog element itself, outside its form) is clicked.
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
const syncGuideZone = dropzone("#guide-file", "#guide-file-name", "Or choose a PDF or photo");

// ---------- tabs (switching is instant: it happens many times a day) ----------

const LOADERS = { plan: () => loadPlan(), inventory: () => loadInventory(), documents: () => loadDocuments(), tasks: () => loadTasks(), history: () => loadHistory(), settings: () => loadSettings() };
function showTab(name) {
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== `tab-${name}`));
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === name));
  history.replaceState(null, "", `${location.search}#${name}`);
  LOADERS[name]();
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
  const done = plan.items.filter((i) => i.done).length;
  const total = plan.items.length;
  const minutesLeft = plan.items.filter((i) => !i.done).reduce((sum, i) => sum + (i.est_minutes || 0), 0);

  $("#plan-title").textContent = weekly ? "This week" : "Today";
  $("#plan-sub").textContent = meta([
    weekly ? fmtRange(plan.start, plan.end) : fmtDate(plan.start),
    total && (done === total ? "All done" : `${done} of ${total} done`),
    total && done < total && minutesLeft && `about ${fmtMinutes(minutesLeft)} left`,
  ]);
  $("#plan-progress-wrap").hidden = !total;
  $("#plan-progress").style.transform = `scaleX(${total ? done / total : 0})`;

  const list = $("#plan-list");
  list.hidden = !total;
  list.innerHTML = plan.items.map(planRowHTML).join("");
  on(list, "[data-check]", (b) => onCheck(plan, +b.dataset.check, b));
  on(list, "[data-snooze]", async (b) => {
    await api("POST", `/api/tasks/${b.dataset.snooze}/snooze?days=7`);
    toast("Snoozed for a week");
    loadPlan();
  });

  const empty = $("#plan-empty");
  empty.hidden = total > 0;
  if (!total) empty.innerHTML = await emptyPlanHTML(weekly);

  $("#up-next-wrap").hidden = !plan.backlog_count;
  $("#backlog-count").textContent = plan.backlog_count || "";
  $("#up-next").innerHTML = plan.up_next
    .map((t) => `<li class="row" tabindex="-1"><div class="row-main"><div class="row-title">${esc(t.title)}</div><div class="row-meta">${esc(meta([t.asset_name, t.reason]))}</div></div></li>`)
    .join("");
  $("#plan-more").hidden = !plan.backlog_count;
  $("#plan-regen").hidden = !total && !plan.backlog_count;
}

// First run: the setup steps, drawn as the same rows the plan uses. Afterwards: a quiet "nothing due".
async function emptyPlanHTML(weekly) {
  const [settings, assets, tasks] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/assets"), api("GET", "/api/tasks")]);
  if (tasks.some((t) => t.status === "active")) {
    return `<div class="empty"><p class="empty-title">Nothing due ${weekly ? "this week" : "today"}</p><p>Everything is up to date.</p></div>`;
  }
  const suggested = tasks.some((t) => t.status === "suggested");
  const steps = [
    [!!settings.location, "Set your location", "So seasonal work lands in the right months.", "settings", "Open settings"],
    [assets.length > 0, "Add what you own", "Furnace, water heater, gutters, cars.", "inventory", "Add items"],
    [suggested, "Get a schedule for each item", "Suggested from the make, model and manual.", "inventory", "Suggest tasks"],
    [false, "Approve the tasks you want", "Your plan fills up from these.", "tasks", "Review"],
  ];
  const next = steps.findIndex(([done]) => !done);
  return `
    <ol class="list">
      ${steps.map(([done, title, sub, tab, cta], i) => `
        <li class="row task-row setup-row ${done ? "done" : ""}">
          <span class="check" aria-hidden="true">${ic("check")}</span>
          <div class="row-main"><div class="row-title">${title}</div><div class="row-meta">${sub}</div></div>
          ${done ? `<span class="status done">Done</span>` : `<a class="btn small ${i === next ? "" : "quiet"}" href="#${tab}" data-goto="${tab}">${cta}</a>`}
        </li>`).join("")}
    </ol>`;
}

function planRowHTML(t) {
  const u = t.done ? "done" : urgency(t);
  const status = t.done ? "Done" : u === "overdue" ? "Overdue" : "Due";
  return `
    <li class="row task-row ${t.done ? "done" : ""}" id="plan-${t.id}" tabindex="-1">
      <button class="check" data-check="${t.id}" aria-pressed="${t.done}" aria-label="${esc(t.title)}">${ic("check")}</button>
      <div class="row-main">
        <div class="row-title">${esc(t.title)}</div>
        <div class="row-meta">${esc(meta([t.priority === 1 && !t.done && "High priority", t.asset_name, scheduleText(t), t.est_minutes && `${t.est_minutes} min`, !t.done && t.reason]))}</div>
        ${t.description ? `<p class="row-desc">${esc(t.description)}</p>` : ""}
      </div>
      <div class="task-side">
        <span class="status ${u === "ok" ? "" : u}">${status}</span>
        ${t.done ? "" : `<button class="btn quiet small reveal no-print" data-snooze="${t.id}">Snooze</button>`}
      </div>
    </li>`;
}

// The check responds on press: the row flips to done right away, then the server catches up.
async function onCheck(plan, taskId, btn) {
  const item = plan.items.find((i) => i.id === taskId);
  const row = btn.closest(".row");
  if (!item.done && item.recurrence === "mileage") return showMileageForm(item);
  row.classList.toggle("done", !item.done);
  try {
    if (item.done) await api("DELETE", `/api/completions/${item.completion_id}`);
    else await api("POST", "/api/completions", { task_id: taskId });
  } catch {
    row.classList.toggle("done", item.done);
    return;
  }
  setTimeout(loadPlan, 320);   // let the check finish drawing before the list re-renders
}

function showMileageForm(item) {
  const li = $(`#plan-${item.id}`);
  if (li.querySelector(".inline-done")) return li.querySelector("input").focus();
  const form = document.createElement("form");
  form.className = "inline-done";
  form.innerHTML = `
    <label>Odometer <input id="mi-${item.id}" type="number" min="0" inputmode="numeric" placeholder="${item.asset_mileage ?? ""}"></label>
    <label>Notes <input id="notes-${item.id}" placeholder="Shop, oil, cost"></label>
    <button class="btn small" type="submit">Log</button>`;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const mileage = form.querySelector(`#mi-${item.id}`).value;
    li.classList.add("done");
    await api("POST", "/api/completions", {
      task_id: item.id,
      mileage: mileage ? +mileage : null,
      notes: form.querySelector(`#notes-${item.id}`).value,
    });
    setTimeout(loadPlan, 320);
  });
  li.appendChild(form);
  form.querySelector("input").focus();
}

$("#plan-regen").addEventListener("click", (e) => busy(e.currentTarget, "Rebuilding", async () => loadPlan(await api("POST", "/api/plan/regenerate"))));
$("#plan-more").addEventListener("click", async () => loadPlan(await api("POST", "/api/plan/more?n=1")));

// ---------- inventory ----------

async function loadInventory() {
  state.assets = await api("GET", "/api/assets");
  const wrap = $("#inventory-list");
  if (!state.assets.length) {
    wrap.innerHTML = `<div class="empty"><p class="empty-title">Nothing here yet</p><p>Start with what costs the most to neglect: furnace, water heater, roof and gutters, cars.</p><button class="btn" data-new-asset>Add an item</button></div>`;
    on(wrap, "[data-new-asset]", () => openAssetForm());
    return;
  }
  const groups = {};
  state.assets.forEach((a) => (groups[a.category] ||= []).push(a));
  wrap.innerHTML = Object.keys(CATEGORIES)
    .filter((c) => groups[c])
    .map((c) => `<h2 class="section-title">${CATEGORIES[c]} <span class="count">${groups[c].length}</span></h2><ul class="list">${groups[c].map(assetRowHTML).join("")}</ul>`)
    .join("");
  on(wrap, "[data-suggest]", (b) =>
    busy(b, "Thinking", async () => {
      const r = await api("POST", `/api/assets/${b.dataset.suggest}/suggest`);
      toast(r.added ? `${plural(r.added, "suggestion")} to review in Tasks` : "No new tasks to suggest");
      refreshBadge();
      loadInventory();
    })
  );
  on(wrap, "[data-find]", (b) =>
    busy(b, "Searching", async () => {
      const r = await api("POST", `/api/assets/${b.dataset.find}/find-documents`);
      toast(findSummary(r), !r.added.length);
      loadInventory();
    })
  );
  on(wrap, "[data-edit-asset]", (b) => openAssetForm(state.assets.find((a) => a.id === +b.dataset.editAsset)));
  on(wrap, "[data-del-asset]", (b) =>
    armConfirm(b, "Delete with its tasks?", async () => {
      await api("DELETE", `/api/assets/${b.dataset.delAsset}`);
      toast("Deleted");
      loadInventory();
    })
  );
}

function assetRowHTML(a) {
  const facts = meta([a.details, a.year, a.mileage != null && `${Number(a.mileage).toLocaleString()} mi`].map((x) => x && esc(x)));
  const links = [
    a.task_count && plural(a.task_count, "task"),
    a.suggested_count && `<a href="#tasks" data-goto="tasks">${a.suggested_count} to review</a>`,
    a.doc_count && `<a href="#documents" data-goto="documents">${plural(a.doc_count, "manual")}</a>`,
  ];
  return `
    <li class="row" tabindex="-1">
      <div class="row-main">
        <div class="row-title">${esc(a.name)}</div>
        <div class="row-meta">${meta([facts || "Add the make and model for better suggestions", ...links])}</div>
      </div>
      <div class="row-actions">
        <button class="btn quiet small icon danger reveal" data-del-asset="${a.id}" aria-label="Delete ${esc(a.name)}">${ic("trash")}</button>
        <button class="btn quiet small icon reveal" data-edit-asset="${a.id}" aria-label="Edit ${esc(a.name)}">${ic("edit")}</button>
        <button class="btn quiet small reveal" data-find="${a.id}">Find manuals</button>
        <button class="btn secondary small" data-suggest="${a.id}">Suggest tasks</button>
      </div>
    </li>`;
}

function openAssetForm(a = {}) {
  resetScan(!a.id);
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
  const saved = await api(id ? "PUT" : "POST", id ? `/api/assets/${id}` : "/api/assets", body);
  closeDialog("asset-dialog");
  toast(id ? "Saved" : `Added ${body.name}`);
  if (!id && scanned) {
    // Keep the label photo with the item, with what the scan read from it, so suggestions can use it.
    const form = new FormData();
    form.append("file", scanned.photo, "label.jpg");
    form.append("asset_id", saved.id);
    form.append("kind", "photo");
    form.append("title", `${body.name} label`);
    form.append("notes", JSON.stringify(scanned.label_notes));
    await api("POST", "/api/documents/upload", form).catch(() => {});
  }
  scanned = null;
  loadInventory();
});

// ---------- scanning a label ----------

let scanned = null;  // { photo, label_notes } from the last scan, saved with the new item
const SCAN_PROMPT = $("#scan-text").innerHTML;

function resetScan(show) {
  scanned = null;
  $("#scan-zone").hidden = !show;
  $("#scan-zone").classList.remove("has-file", "busy");
  $("#scan-text").innerHTML = SCAN_PROMPT;
  $("#scan-preview").hidden = true;
  $("#scan-result").hidden = true;
  $("#scan-file").value = "";
}

// Phone photos are often 5 MB or more, and HEIC on iPhones. Re-encode as a JPEG small enough to send,
// but still sharp enough for the small print on a label.
async function shrinkPhoto(file, maxSide = 2000) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.88));
}

$("#scan-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const zone = $("#scan-zone");
  let photo;
  try {
    photo = await shrinkPhoto(file);
  } catch {
    toast("Couldn't open that photo. Try a JPEG or PNG.", true);
    return resetScan(true);
  }
  $("#scan-preview").src = URL.createObjectURL(photo);
  $("#scan-preview").hidden = false;
  zone.classList.add("busy");
  $("#scan-text").innerHTML = `<span class="spinner" aria-hidden="true"></span> Reading the label…`;
  const form = new FormData();
  form.append("file", photo, "label.jpg");
  let r;
  try {
    r = await api("POST", "/api/assets/scan", form);
  } catch {
    return resetScan(true);
  } finally {
    zone.classList.remove("busy");
  }
  scanned = { photo, label_notes: r.label_notes };
  zone.classList.add("has-file");
  $("#scan-text").innerHTML = `<strong>${esc(r.details || r.name)}</strong><br>Read from the photo. Choose another to scan again.`;
  $("#asset-category").value = r.category;
  $("#asset-name").value = r.name;
  $("#asset-details").value = r.details;
  $("#asset-year").value = r.year ?? "";
  $("#asset-mileage").value = r.mileage ?? "";
  $("#asset-notes").value = r.notes;
  toggleMileage();
  const result = $("#scan-result");
  result.textContent = (r.year ? `Year: ${r.year_basis || "printed on the label"}. ` : "No year on the label, so add it if you know it. ")
    + "Check the details, then save.";
  result.hidden = false;
});

// ---------- manuals ----------

const KIND_LABELS = { manual: "Owner's manual", maintenance: "Maintenance schedule", guide: "Checklist", photo: "Photo", other: "Document" };
const SOURCE_LABELS = { found: "Found online", uploaded: "Uploaded", link: "From a link" };

function findSummary(r) {
  if (!r.found) return "No manuals found. Adding the exact model usually helps, or upload one.";
  const notManuals = r.skipped.filter((d) => d.reason.startsWith("no maintenance")).length;
  const other = r.skipped.filter((d) => !d.reason.startsWith("no maintenance") && d.reason !== "Already saved");
  const parts = [];
  if (r.added.length) parts.push(`Saved ${r.added.map((d) => d.title).join(", ")}`);
  else if (notManuals) parts.push(`Found ${plural(notManuals, "document")}, but no care instructions in ${notManuals === 1 ? "it" : "them"}. Upload the manual if you have it`);
  else parts.push("Nothing new found");
  if (r.added.length && notManuals) parts.push(`Left out ${notManuals} without care instructions`);
  if (other.length) parts.push(`Couldn't download ${other.length}: ${other[0].reason}`);
  const unread = r.added.filter((d) => d.read_error);
  if (unread.length) parts.push(`Couldn't read ${unread.length}: ${unread[0].read_error}`);
  return parts.join(". ") + ".";
}

function fillAssetSelects() {
  const opts = `<option value="">Whole home</option>` + state.assets.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
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
    wrap.innerHTML = `<div class="empty"><p class="empty-title">No manuals yet</p><p>Upload a PDF or a photo of a data plate, or use <a href="#inventory" data-goto="inventory">Find manuals</a> on an item.</p></div>`;
    return;
  }
  const groups = new Map();
  docs.forEach((d) => {
    const key = d.asset_name || "Whole home";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(d);
  });
  const ordered = [...groups.keys()].sort((a, b) => (a === "Whole home" ? -1 : b === "Whole home" ? 1 : a.localeCompare(b)));
  wrap.innerHTML = ordered.map((k) => `<h2 class="section-title">${esc(k)}</h2><ul class="list">${groups.get(k).map(docRowHTML).join("")}</ul>`).join("");

  on(wrap, "[data-read]", (b) =>
    busy(b, "Reading", async () => {
      await api("POST", `/api/documents/${b.dataset.read}/read`);
      toast("Notes updated");
      loadDocuments();
    })
  );
  on(wrap, "[data-doc-tasks]", (b) =>
    busy(b, "Thinking", async () => {
      const r = await api("POST", `/api/documents/${b.dataset.docTasks}/tasks`);
      toast(r.added ? `${plural(r.added, "suggestion")} to review in Tasks` : "No new tasks to suggest");
      refreshBadge();
    })
  );
  on(wrap, "[data-del-doc]", (b) =>
    armConfirm(b, "Delete?", async () => {
      await api("DELETE", `/api/documents/${b.dataset.delDoc}`);
      loadDocuments();
    })
  );
}

function docRowHTML(d) {
  const n = d.notes;
  const counts = n && meta([n.maintenance.length && plural(n.maintenance.length, "interval"), n.parts.length && plural(n.parts.length, "part")]);
  const notes = n
    ? `<details class="doc-notes">
        <summary>Notes${counts ? ` · ${counts}` : ""}</summary>
        <div class="notes">
          <p>${esc(n.summary)}${n.model_identified ? ` Model ${esc(n.model_identified)}.` : ""}</p>
          ${n.maintenance.length ? `<div><h3>Maintenance</h3><ul>${n.maintenance.map((m) => `<li>${esc(m.task)}, ${esc(m.frequency)}. ${esc(m.details)} ${m.page ? `<span class="page">p. ${esc(m.page)}</span>` : ""}</li>`).join("")}</ul></div>` : ""}
          ${n.parts.length ? `<div><h3>Parts</h3><ul>${n.parts.map((p) => `<li>${esc(p.name)}: ${esc(p.spec)}</li>`).join("")}</ul></div>` : ""}
          ${n.warnings.length ? `<div><h3>Warnings</h3><ul class="warn">${n.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
        </div>
      </details>`
    : `<div class="doc-notes row-meta unread">Not read yet, so suggestions won't use it.</div>`;
  return `
    <li class="row" tabindex="-1">
      <div class="row-main">
        <a class="row-title" href="/api/documents/${d.id}/file" target="_blank" rel="noopener">${esc(d.title)}</a>
        <div class="row-meta">${meta([esc(KIND_LABELS[d.kind] || "Document"), d.pages && plural(d.pages, "page"), d.source_url ? `<a href="${esc(d.source_url)}" target="_blank" rel="noopener noreferrer">${SOURCE_LABELS[d.source]}</a>` : SOURCE_LABELS[d.source]])}</div>
      </div>
      <div class="row-actions">
        <button class="btn quiet small icon danger reveal" data-del-doc="${d.id}" aria-label="Delete ${esc(d.title)}">${ic("trash")}</button>
        <button class="btn quiet small reveal" data-read="${d.id}">${n ? "Read again" : "Read now"}</button>
        <button class="btn secondary small" data-doc-tasks="${d.id}">${d.asset_id ? "Suggest tasks" : "Make tasks"}</button>
      </div>
      ${notes}
    </li>`;
}

$("#upload-open").addEventListener("click", () => openDialog("upload-dialog"));
$("#link-open").addEventListener("click", () => { openDialog("link-dialog"); $("#link-url").focus(); });

function afterAdd(r) {
  if (r.read_error) toast(`Saved, but it couldn't be read: ${r.read_error}`, true);
  else toast(`Added ${r.document.title}`);
  loadDocuments();
}

$("#upload-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy($("#upload-submit"), "Reading", async () => {
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
  busy($("#link-submit"), "Downloading", async () => {
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

const openTaskById = (b) => openTaskForm(state.tasks.find((t) => t.id === +b.dataset.editTask));

async function loadTasks() {
  [state.tasks, state.assets] = await Promise.all([api("GET", "/api/tasks"), api("GET", "/api/assets")]);
  const suggested = state.tasks.filter((t) => t.status === "suggested");
  const active = state.tasks.filter((t) => t.status === "active").sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  const paused = state.tasks.filter((t) => t.status === "paused");
  refreshBadge(suggested.length);

  const sList = $("#suggested-list");
  $("#suggested-wrap").hidden = !suggested.length;
  $("#suggested-count").textContent = suggested.length;
  sList.innerHTML = suggested.map(suggestionRowHTML).join("");
  on(sList, "[data-approve]", async (b) => {
    await api("POST", `/api/tasks/${b.dataset.approve}/approve`, { last_done_on: $(`#last-${b.dataset.approve}`).value || null });
    loadTasks();
  });
  on(sList, "[data-dismiss]", async (b) => {
    await api("DELETE", `/api/tasks/${b.dataset.dismiss}`);
    loadTasks();
  });
  on(sList, "[data-edit-task]", openTaskById);
  $("#approve-all-wrap").innerHTML = suggested.length > 1 ? `<button class="btn secondary small" id="approve-all">Approve all ${suggested.length}</button>` : "";
  if (suggested.length > 1) {
    $("#approve-all").addEventListener("click", (e) =>
      busy(e.currentTarget, "Approving", async () => {
        for (const t of suggested) {
          await api("POST", `/api/tasks/${t.id}/approve`, { last_done_on: $(`#last-${t.id}`).value || null });
        }
        toast(`Approved ${plural(suggested.length, "task")}`);
        loadTasks();
      })
    );
  }

  $("#active-title").innerHTML = `Active <span class="count">${active.length || ""}</span>`;
  $("#task-list").innerHTML = active.length
    ? active.map((t) => taskRowHTML(t, false)).join("")
    : `<li class="empty-row">No active tasks yet. Approve a suggestion or add your own.</li>`;
  $("#paused-wrap").hidden = !paused.length;
  $("#paused-list").innerHTML = paused.map((t) => taskRowHTML(t, true)).join("");

  const tab = $("#tab-tasks");
  on(tab, "#task-list [data-edit-task], #paused-list [data-edit-task]", openTaskById);
  on(tab, "[data-pause]", async (b) => { await api("POST", `/api/tasks/${b.dataset.pause}/status/paused`); loadTasks(); });
  on(tab, "[data-resume]", async (b) => { await api("POST", `/api/tasks/${b.dataset.resume}/status/active`); loadTasks(); });
  on(tab, "[data-done-now]", async (b) => {
    await api("POST", "/api/completions", { task_id: +b.dataset.doneNow });
    toast("Logged for today");
    loadTasks();
  });
  on(tab, "[data-del-task]", (b) => armConfirm(b, "Delete?", async () => { await api("DELETE", `/api/tasks/${b.dataset.delTask}`); loadTasks(); }));
}

function suggestionRowHTML(t) {
  return `
    <li class="row suggestion">
      <div class="row-main">
        <div class="row-title">${esc(t.title)}</div>
        <div class="row-meta">${esc(meta([t.priority === 1 && "High priority", t.asset_name || "Whole home", scheduleText(t), t.est_minutes && `${t.est_minutes} min`, t.source === "guide" && "From your checklist"]))}</div>
        ${t.description ? `<p class="row-desc">${esc(t.description)}</p>` : ""}
      </div>
      <div class="suggestion-foot">
        <label>Last done <input type="date" id="last-${t.id}" max="${todayISO()}" aria-label="Last done, optional"></label>
        <button class="btn quiet small" data-dismiss="${t.id}">Dismiss</button>
        <button class="btn quiet small" data-edit-task="${t.id}">Edit</button>
        <button class="btn small" data-approve="${t.id}">Approve</button>
      </div>
    </li>`;
}

function taskRowHTML(t, paused) {
  const u = paused ? "" : urgency(t);
  return `
    <li class="row" tabindex="-1">
      <div class="row-main">
        <div class="row-title">${esc(t.title)}</div>
        <div class="row-meta">${esc(meta([t.asset_name || "Whole home", scheduleText(t), t.last_done_on && `last ${fmtDate(t.last_done_on)}`]))}</div>
      </div>
      <div class="row-actions">
        ${paused
          ? `<button class="btn quiet small icon danger reveal" data-del-task="${t.id}" aria-label="Delete ${esc(t.title)}">${ic("trash")}</button>
             <button class="btn quiet small" data-resume="${t.id}">Resume</button>`
          : `<button class="btn quiet small icon reveal" data-edit-task="${t.id}" aria-label="Edit ${esc(t.title)}">${ic("edit")}</button>
             <button class="btn quiet small reveal" data-pause="${t.id}">Pause</button>
             <button class="btn quiet small reveal" data-done-now="${t.id}">Done today</button>
             <span class="status ${u === "ok" ? "" : u}"><span class="dot ${u}"></span>${esc(t.reason || "")}</span>`}
      </div>
    </li>`;
}

function openTaskForm(t = {}) {
  openDialog("task-dialog");
  $("#task-dialog-title").textContent = t.id ? (t.status === "suggested" ? "Edit suggestion" : "Edit task") : "Add task";
  $("#task-asset").innerHTML = `<option value="">Whole home</option>` + state.assets.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
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
  if (!file && !$("#guide-text").value.trim()) return $("#guide-text").focus();
  busy($("#guide-submit"), "Reading", async () => {
    let r;
    if (file) {
      // Keep the file under Manuals as a whole-home checklist, then turn it into tasks.
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
    toast(`${plural(r.added, "task")} to review`);
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
  const list = $("#history-list");
  if (!rows.length) {
    list.innerHTML = `<li class="empty-row">Nothing logged yet. Tick things off your plan and they'll show up here.</li>`;
    return;
  }
  list.innerHTML = rows
    .map(
      (r) => `
      <li class="row" tabindex="-1">
        <div class="row-main">
          <div class="row-title">${esc(r.title)}</div>
          <div class="row-meta">${esc(meta([r.asset_name || "Whole home", r.mileage && `${Number(r.mileage).toLocaleString()} mi`, r.notes]))}</div>
        </div>
        <div class="row-actions">
          <button class="btn quiet small icon danger reveal" data-undo="${r.id}" aria-label="Remove entry">${ic("trash")}</button>
          <span class="status">${fmtDate(r.done_on)}</span>
        </div>
      </li>`
    )
    .join("");
  on(list, "[data-undo]", (b) => armConfirm(b, "Remove?", async () => { await api("DELETE", `/api/completions/${b.dataset.undo}`); loadHistory(); }));
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
  toast("Saved");
});

// ---------- theme, status, boot ----------

const THEMES = ["system", "light", "dark"];
function applyTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  const btn = $("#theme-btn");
  btn.innerHTML = ic({ system: "system", light: "sun", dark: "moon" }[t]);
  btn.setAttribute("aria-label", `Theme: ${t}`);
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
  $("#about-version").textContent = s.version;
  $("#about-ai").innerHTML = s.ai_configured ? `Connected <span class="muted">· ${esc(s.model)}</span>` : `Not connected. Set <code>ANTHROPIC_API_KEY</code> and restart.`;
  let dismissed = false;
  try { dismissed = localStorage.getItem("hk-banner-dismissed") === "1"; } catch {}
  $("#setup-banner").hidden = s.ai_configured || dismissed;
}
$("#banner-close").addEventListener("click", () => {
  $("#setup-banner").hidden = true;
  try { localStorage.setItem("hk-banner-dismissed", "1"); } catch {}
});

$("#today-label").textContent = (AS_OF ? "Previewing " : "") + fmtDate(todayISO(), false);
loadStatus();
refreshBadge();
const startTab = location.hash.slice(1);
showTab(LOADERS[startTab] ? startTab : "plan");
