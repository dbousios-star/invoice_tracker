"use strict";

// ---------- utils ----------
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const eur = (n) => (n == null ? "–" : new Intl.NumberFormat("el-GR", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(n));
const num = (n, d = 2) => (n == null ? "–" : new Intl.NumberFormat("el-GR", { maximumFractionDigits: d }).format(n));
const pct = (n) => (n == null ? "" : `${n > 0 ? "+" : ""}${num(n, 1)}%`);
const signedEur = (n) => (n == null ? "" : `${n > 0 ? "+" : ""}${eur(n)}`);
const fmtDate = (iso) => { if (!iso) return "–"; const [y, m, d] = iso.split("-"); return `${d}/${m}/${y}`; };

const STATUS = {
  INCREASED: { icon: "🔴", label: "Αύξηση", plural: "Αυξήσεις", card: "border-red-500 bg-red-50 dark:bg-red-950/40", text: "text-red-700 dark:text-red-300" },
  DECREASED: { icon: "🟢", label: "Μείωση", plural: "Μειώσεις", card: "border-green-500 bg-green-50 dark:bg-green-950/40", text: "text-green-700 dark:text-green-300" },
  NEW_ITEM: { icon: "🔵", label: "Νέο", plural: "Νέα", card: "border-blue-500 bg-blue-50 dark:bg-blue-950/40", text: "text-blue-700 dark:text-blue-300" },
  UNCHANGED: { icon: "⚪", label: "Σταθερό", plural: "Σταθερά", card: "border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800", text: "text-slate-600 dark:text-slate-300" },
};

let pendingQr = null;   // QR text from live scan, attached to the next upload
let currentReport = null;
let qrScanner = null;

async function api(url, opts = {}) {
  let res;
  try {
    res = await fetch(url, opts);
  } catch {
    throw new Error("Δεν υπάρχει σύνδεση με τον διακομιστή. Ελέγξτε ότι ο υπολογιστής είναι ανοιχτός και στο ίδιο δίκτυο.");
  }
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const detail = body?.detail;
    const msg = typeof detail === "string" ? detail : Array.isArray(detail) ? "Μη έγκυρα δεδομένα." : `Σφάλμα διακομιστή (${res.status}).`;
    throw new Error(msg);
  }
  return body;
}

function showAlert(msg, kind = "error") {
  const el = $("#alert");
  const cls = {
    error: "bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-100",
    warn: "bg-amber-100 text-amber-900 dark:bg-amber-900/60 dark:text-amber-100",
    info: "bg-blue-100 text-blue-900 dark:bg-blue-900/60 dark:text-blue-100",
    ok: "bg-green-100 text-green-900 dark:bg-green-900/60 dark:text-green-100",
  }[kind];
  el.className = `mb-4 rounded-xl p-4 text-sm font-medium ${cls}`;
  el.innerHTML = msg;
  el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
const hideAlert = () => $("#alert").classList.add("hidden");

// ---------- tabs ----------
function showTab(name) {
  document.querySelectorAll("[data-tab]").forEach((s) => s.classList.toggle("hidden", s.id !== `tab-${name}`));
  document.querySelectorAll("[data-nav]").forEach((b) => {
    const active = b.dataset.nav === name;
    b.classList.toggle("text-blue-600", active);
    b.classList.toggle("dark:text-blue-400", active);
  });
  hideAlert();
  if (name !== "scan") stopQr();
  if (name === "history") loadHistory();
}
document.querySelectorAll("[data-nav]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.nav)));

// ---------- loading ----------
let loadingTimers = [];
function setLoading(on, steps = []) {
  loadingTimers.forEach(clearTimeout);
  loadingTimers = [];
  $("#loading").classList.toggle("hidden", !on);
  $("#loading").classList.toggle("flex", on);
  $("#scan-actions").classList.toggle("hidden", on);
  if (on) {
    $("#results").innerHTML = "";
    $("#batch").innerHTML = "";
    steps.forEach(([ms, text]) => loadingTimers.push(setTimeout(() => ($("#loading-text").textContent = text), ms)));
  }
}

// ---------- upload ----------
async function handleFile(file) {
  if (!file) return;
  hideAlert();
  stopQr();
  setLoading(true, [[0, "Ανάγνωση QR..."], [1500, "Εξαγωγή προϊόντων AI..."], [9000, "Σύγκριση τιμών..."], [20000, "Σχεδόν έτοιμο..."]]);
  const fd = new FormData();
  fd.append("file", file);
  if (pendingQr) fd.append("qr_text", pendingQr);
  try {
    const report = await api("/api/upload", { method: "POST", body: fd });
    pendingQr = null;
    $("#pending-qr").classList.add("hidden");
    renderReport(report);
    if (report.duplicate) showAlert(`⚠️ ${esc(report.warnings[0] || "Το τιμολόγιο έχει ήδη καταχωρηθεί.")} Εμφανίζεται η υπάρχουσα σύγκριση.`, "warn");
  } catch (e) {
    showAlert(`❌ ${esc(e.message)}`);
  } finally {
    setLoading(false);
  }
}
["#file-camera", "#file-pick"].forEach((id) => $(id).addEventListener("change", (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  if (files.length > 1) handleBatch(files);
  else handleFile(files[0]);
}));

// ---------- batch upload ----------
const MAX_BATCH = 20;
let batchRunning = false;
window.addEventListener("beforeunload", (e) => {
  if (batchRunning) { e.preventDefault(); e.returnValue = ""; }
});

async function handleBatch(files) {
  if (batchRunning) return;
  hideAlert();
  stopQr();
  if (files.length > MAX_BATCH) {
    showAlert(`⚠️ Επιλέξατε ${files.length} αρχεία· θα επεξεργαστούν τα πρώτα ${MAX_BATCH}.`, "warn");
    files = files.slice(0, MAX_BATCH);
  }
  batchRunning = true;
  $("#scan-actions").classList.add("hidden");
  $("#results").innerHTML = "";

  const box = $("#batch");
  box.innerHTML = `
    <div class="rounded-2xl bg-white dark:bg-slate-800 shadow p-4">
      <div class="flex justify-between items-center mb-3">
        <h2 class="font-bold">Μαζική καταχώρηση</h2>
        <span id="batch-progress" class="text-sm font-semibold text-blue-600 dark:text-blue-400">0 / ${files.length}</span>
      </div>
      <div id="batch-rows" class="space-y-2">
        ${files.map((f, i) => `
          <div id="batch-row-${i}" class="rounded-xl border border-slate-200 dark:border-slate-700 p-3">
            <div class="flex justify-between gap-2 text-sm">
              <span class="truncate font-medium">${esc(f.name)}</span>
              <span class="row-status shrink-0">⏳ Αναμονή</span>
            </div>
            <div class="row-detail text-xs text-slate-500 dark:text-slate-400"></div>
          </div>`).join("")}
      </div>
      <div id="batch-summary" class="mt-3 text-sm font-semibold"></div>
    </div>`;

  const counts = { ok: 0, dup: 0, err: 0, withIncreases: 0 };
  for (let i = 0; i < files.length; i++) {
    const row = $(`#batch-row-${i}`);
    const status = row.querySelector(".row-status");
    const detail = row.querySelector(".row-detail");
    status.textContent = "🔄 Επεξεργασία…";
    row.classList.add("border-blue-400");
    const fd = new FormData();
    fd.append("file", files[i]);
    try {
      const r = await api("/api/upload", { method: "POST", body: fd });
      const inv = r.invoice;
      const inc = r.summary.INCREASED || 0;
      const dec = r.summary.DECREASED || 0;
      row.dataset.open = inv.id;
      row.classList.add("cursor-pointer", "active:bg-slate-50", "dark:active:bg-slate-700");
      detail.innerHTML = `${esc(inv.supplier_name)} · ${fmtDate(inv.invoice_date)}` +
        (inc ? ` · <span class="text-red-600 dark:text-red-400 font-semibold">🔴 ${inc}</span>` : "") +
        (dec ? ` · <span class="text-green-600 dark:text-green-400 font-semibold">🟢 ${dec}</span>` : "") +
        ` · <span class="underline">προβολή</span>`;
      if (r.duplicate) {
        counts.dup++;
        status.textContent = "⚠️ Ήδη καταχωρημένο";
      } else {
        counts.ok++;
        if (inc) counts.withIncreases++;
        status.textContent = "✅ Καταχωρήθηκε";
      }
    } catch (e) {
      counts.err++;
      status.textContent = "❌ Σφάλμα";
      detail.textContent = e.message;
      detail.classList.add("text-red-600", "dark:text-red-400");
    }
    row.classList.remove("border-blue-400");
    $("#batch-progress").textContent = `${i + 1} / ${files.length}`;
  }

  $("#batch-summary").innerHTML = [
    `✅ ${counts.ok} καταχωρήθηκαν`,
    counts.dup && `⚠️ ${counts.dup} διπλά`,
    counts.err && `❌ ${counts.err} σφάλματα`,
    counts.withIncreases && `<span class="text-red-600 dark:text-red-400">🔴 ${counts.withIncreases} με αυξήσεις</span>`,
  ].filter(Boolean).join(" · ");
  batchRunning = false;
  $("#scan-actions").classList.remove("hidden");
}

$("#batch").addEventListener("click", async (e) => {
  const row = e.target.closest("[data-open]");
  if (!row) return;
  await openReport(row.dataset.open);
  $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
});

// ---------- QR-only scan ----------
async function startQr() {
  hideAlert();
  if (typeof Html5Qrcode === "undefined") return showAlert("Η βιβλιοθήκη QR δεν φορτώθηκε (απαιτείται σύνδεση στο internet).");
  if (!window.isSecureContext) {
    return showAlert("Η ζωντανή κάμερα απαιτεί HTTPS. Χρησιμοποιήστε tunnel (π.χ. cloudflared) ή φωτογραφίστε το τιμολόγιο — το QR διαβάζεται και από τη φωτογραφία.", "warn");
  }
  $("#qr-box").classList.remove("hidden");
  qrScanner = new Html5Qrcode("qr-reader", { formatsToSupport: [Html5QrcodeSupportedFormats.QR_CODE], verbose: false });
  try {
    await qrScanner.start({ facingMode: "environment" }, { fps: 10, qrbox: (w, h) => { const s = Math.floor(Math.min(w, h) * 0.75); return { width: s, height: s }; } }, onQrDecoded, () => {});
  } catch (e) {
    stopQr();
    showAlert(`Δεν ήταν δυνατή η πρόσβαση στην κάμερα: ${esc(e?.message || e)}`);
  }
}

async function stopQr() {
  if (qrScanner) {
    try { if (qrScanner.isScanning) await qrScanner.stop(); qrScanner.clear(); } catch { /* ignore */ }
    qrScanner = null;
  }
  $("#qr-box").classList.add("hidden");
}

async function onQrDecoded(text) {
  await stopQr();
  if (navigator.vibrate) navigator.vibrate(80);
  try {
    const r = await api("/api/qr-check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ qr_text: text }) });
    const q = r.qr;
    const meta = [q.mark && `ΜΑΡΚ: <b>${esc(q.mark)}</b>`, q.afm && `ΑΦΜ: ${esc(q.afm)}`, q.invoice_date && fmtDate(q.invoice_date), q.total_gross != null && `Σύνολο: ${eur(q.total_gross)}`].filter(Boolean).join(" · ");
    if (r.duplicate) {
      showAlert(`⚠️ ${esc(r.message)}<br><span class="font-normal">${meta}</span><br><button id="btn-view-existing" class="mt-3 px-4 py-2 rounded-lg bg-amber-600 text-white">Προβολή σύγκρισης</button>`, "warn");
      $("#btn-view-existing").onclick = () => { hideAlert(); renderReport(r.existing_report); };
    } else {
      pendingQr = text;
      $("#pending-qr").textContent = "✔ Το QR θα συνδεθεί με την επόμενη φωτογραφία.";
      $("#pending-qr").classList.remove("hidden");
      showAlert(`✅ ${esc(r.message)}<br><span class="font-normal">${meta || esc(q.raw_url)}</span><br>Φωτογραφίστε τώρα το τιμολόγιο για καταχώρηση.`, q.mark ? "ok" : "info");
    }
  } catch (e) {
    showAlert(`❌ ${esc(e.message)}`);
  }
}
$("#btn-qr").addEventListener("click", startQr);
$("#btn-qr-stop").addEventListener("click", stopQr);

// ---------- report rendering ----------
function itemCard(it) {
  const s = STATUS[it.status];
  const change = it.old_price != null && it.status !== "UNCHANGED"
    ? `<div class="mt-2 flex items-baseline justify-between gap-2">
         <span class="text-sm text-slate-500 dark:text-slate-400 line-through">${eur(it.old_price)}</span>
         <span class="text-lg font-bold ${s.text}">${eur(it.unit_price_net)}</span>
       </div>
       <div class="flex justify-between text-sm ${s.text} font-semibold">
         <span>${signedEur(it.diff_amount)}</span><span>${pct(it.diff_percent)}</span>
       </div>
       <div class="text-xs text-slate-500 dark:text-slate-400 mt-1">Προηγ.: ${fmtDate(it.previous_date)}${it.match_type === "fuzzy" ? ` · ταίριασμα ${num(it.match_score, 0)}% με «${esc(it.matched_description)}»` : ""}</div>`
    : `<div class="mt-2 flex items-baseline justify-between">
         <span class="text-xs text-slate-500 dark:text-slate-400">${it.previous_date ? `Προηγ.: ${fmtDate(it.previous_date)} (${eur(it.old_price)})` : "Πρώτη αγορά από αυτόν τον προμηθευτή"}</span>
         <span class="text-lg font-bold">${eur(it.unit_price_net)}</span>
       </div>`;
  return `
    <div class="rounded-xl border-l-4 ${s.card} p-3 shadow-sm" data-item="${it.item_id}">
      <div class="flex justify-between gap-2">
        <div class="min-w-0">
          <div class="font-semibold leading-snug">${s.icon} ${esc(it.description)}</div>
          <div class="text-xs text-slate-500 dark:text-slate-400">${it.product_code ? `Κωδ. ${esc(it.product_code)} · ` : ""}${num(it.quantity, 3)} τεμ. · ${eur(it.line_total_net)}${it.discount_percent ? ` · έκπτ. ${num(it.discount_percent, 2)}%` : ""}</div>
        </div>
        <button class="btn-edit shrink-0 w-10 h-10 rounded-lg bg-slate-200/70 dark:bg-slate-700 text-lg" title="Διόρθωση" aria-label="Διόρθωση">✏️</button>
      </div>
      ${change}
      <div class="edit-form hidden mt-3 grid grid-cols-2 gap-2">
        <label class="text-xs">Ποσότητα<input type="number" inputmode="decimal" step="any" min="0" class="inp-qty w-full mt-1 p-2 rounded-lg border dark:bg-slate-900 dark:border-slate-600 text-base" value="${it.quantity}"></label>
        <label class="text-xs">Καθ. τιμή μονάδας €<input type="number" inputmode="decimal" step="any" min="0" class="inp-price w-full mt-1 p-2 rounded-lg border dark:bg-slate-900 dark:border-slate-600 text-base" value="${it.unit_price_net}"></label>
        <button class="btn-save col-span-2 py-3 rounded-lg bg-blue-600 text-white font-semibold">Αποθήκευση & επανυπολογισμός</button>
      </div>
    </div>`;
}

function renderReport(report) {
  currentReport = report;
  const inv = report.invoice;
  const sm = report.summary;
  const groups = { INCREASED: [], DECREASED: [], NEW_ITEM: [], UNCHANGED: [] };
  report.items.forEach((it) => groups[it.status].push(it));

  const badges = ["INCREASED", "DECREASED", "UNCHANGED", "NEW_ITEM"]
    .map((k) => `<span class="px-3 py-1.5 rounded-full text-sm font-semibold ${STATUS[k].card} border">${STATUS[k].icon} ${sm[k] || 0} ${sm[k] === 1 ? STATUS[k].label : STATUS[k].plural}</span>`)
    .join("");

  const warnings = (report.warnings || []).filter((w) => !report.duplicate)
    .map((w) => `<li>⚠️ ${esc(w)}</li>`).join("");

  $("#results").innerHTML = `
    <div class="rounded-2xl bg-white dark:bg-slate-800 shadow p-4 mb-4">
      <div class="flex justify-between items-start gap-2">
        <div class="min-w-0">
          <h2 class="text-lg font-bold leading-tight">${esc(inv.supplier_name)}</h2>
          <div class="text-sm text-slate-500 dark:text-slate-400">ΑΦΜ ${esc(inv.supplier_afm)} · ${fmtDate(inv.invoice_date)}${inv.invoice_number ? ` · Αρ. ${esc(inv.invoice_number)}` : ""}</div>
          ${inv.mark ? `<div class="text-xs text-slate-500 dark:text-slate-400 break-all">ΜΑΡΚ ${esc(inv.mark)}</div>` : ""}
        </div>
        <div class="text-right shrink-0">
          <div class="text-xs text-slate-500">Καθαρή</div><div class="font-semibold">${eur(inv.total_net)}</div>
          <div class="text-xs text-slate-500 mt-1">Σύνολο</div><div class="font-bold text-lg">${eur(inv.total_gross)}</div>
        </div>
      </div>
      <div class="flex flex-wrap gap-2 mt-3">${badges}</div>
      ${warnings ? `<ul class="mt-3 text-sm text-amber-700 dark:text-amber-300 space-y-1">${warnings}</ul>` : ""}
    </div>
    <div class="space-y-3">
      ${[...groups.INCREASED, ...groups.DECREASED, ...groups.NEW_ITEM].map(itemCard).join("")}
    </div>
    ${groups.UNCHANGED.length ? `
      <details class="mt-4 rounded-2xl bg-white dark:bg-slate-800 shadow">
        <summary class="p-4 font-semibold cursor-pointer select-none">⚪ ${groups.UNCHANGED.length} σταθερές τιμές ▾</summary>
        <div class="space-y-3 p-3 pt-0">${groups.UNCHANGED.map(itemCard).join("")}</div>
      </details>` : ""}
    <div class="grid grid-cols-2 gap-3 mt-6">
      <button id="btn-new-scan" class="py-4 rounded-2xl bg-blue-600 text-white font-semibold">📷 Νέα σάρωση</button>
      <button id="btn-delete-inv" class="py-4 rounded-2xl bg-white dark:bg-slate-800 border border-red-300 text-red-600 font-semibold">🗑️ Διαγραφή</button>
    </div>`;

  $("#btn-new-scan").onclick = () => { $("#results").innerHTML = ""; if (!batchRunning) $("#batch").innerHTML = ""; hideAlert(); window.scrollTo({ top: 0, behavior: "smooth" }); };
  $("#btn-delete-inv").onclick = () => deleteInvoice(inv.id);
  showTab("scan");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// Inline edit (event delegation)
$("#results").addEventListener("click", async (e) => {
  const card = e.target.closest("[data-item]");
  if (!card) return;
  if (e.target.closest(".btn-edit")) {
    card.querySelector(".edit-form").classList.toggle("hidden");
    return;
  }
  if (e.target.closest(".btn-save")) {
    const qty = parseFloat(card.querySelector(".inp-qty").value.replace(",", "."));
    const price = parseFloat(card.querySelector(".inp-price").value.replace(",", "."));
    if (!(qty > 0) || !(price >= 0)) return showAlert("Μη έγκυρη ποσότητα ή τιμή.");
    try {
      const report = await api(`/api/items/${card.dataset.item}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ quantity: qty, unit_price_net: price }),
      });
      renderReport(report);
      showAlert("✅ Η διόρθωση αποθηκεύτηκε και η σύγκριση ενημερώθηκε.", "ok");
    } catch (err) {
      showAlert(`❌ ${esc(err.message)}`);
    }
  }
});

async function deleteInvoice(id) {
  if (!confirm("Διαγραφή αυτού του τιμολογίου και των ειδών του;")) return;
  try {
    await api(`/api/invoices/${id}`, { method: "DELETE" });
    $("#results").innerHTML = "";
    showAlert("Το τιμολόγιο διαγράφηκε.", "info");
  } catch (e) {
    showAlert(`❌ ${esc(e.message)}`);
  }
}

async function openReport(id) {
  try {
    renderReport(await api(`/api/invoices/${id}/report`));
  } catch (e) {
    showAlert(`❌ ${esc(e.message)}`);
  }
}

// ---------- history ----------
async function loadSuppliers() {
  const sel = $("#supplier-filter");
  const current = sel.value;
  try {
    const suppliers = await api("/api/suppliers");
    sel.innerHTML = `<option value="">Όλοι οι προμηθευτές</option>` +
      suppliers.map((s) => `<option value="${s.id}">${esc(s.name)} (${s.invoice_count})</option>`).join("");
    sel.value = current;
  } catch { /* non-fatal */ }
}

async function loadHistory() {
  await loadSuppliers();
  const list = $("#history-list");
  list.innerHTML = `<div class="flex justify-center py-8"><div class="spinner"></div></div>`;
  try {
    const sid = $("#supplier-filter").value;
    const invoices = await api(`/api/invoices${sid ? `?supplier_id=${sid}` : ""}`);
    if (!invoices.length) {
      list.innerHTML = `<p class="text-center text-slate-500 py-8">Δεν υπάρχουν καταχωρημένα τιμολόγια.</p>`;
      return;
    }
    list.innerHTML = invoices.map((i) => `
      <button data-open="${i.id}" class="w-full text-left rounded-xl bg-white dark:bg-slate-800 shadow-sm p-4 flex justify-between items-center gap-3 active:bg-slate-50 dark:active:bg-slate-700">
        <div class="min-w-0">
          <div class="font-semibold truncate">${esc(i.supplier_name)}</div>
          <div class="text-sm text-slate-500 dark:text-slate-400">${fmtDate(i.invoice_date)} · ${i.item_count} είδη${i.invoice_number ? ` · ${esc(i.invoice_number)}` : ""}</div>
        </div>
        <div class="text-right shrink-0">
          <div class="font-bold">${eur(i.total_gross ?? i.total_net)}</div>
          ${i.has_increases ? `<span class="inline-block mt-1 px-2 py-0.5 rounded-full text-xs font-semibold bg-red-100 text-red-700 dark:bg-red-900/60 dark:text-red-200">🔴 ${i.increases}</span>` : ""}
          ${i.decreases ? `<span class="inline-block mt-1 px-2 py-0.5 rounded-full text-xs font-semibold bg-green-100 text-green-700 dark:bg-green-900/60 dark:text-green-200">🟢 ${i.decreases}</span>` : ""}
        </div>
      </button>`).join("");
  } catch (e) {
    list.innerHTML = "";
    showAlert(`❌ ${esc(e.message)}`);
  }
}
$("#supplier-filter").addEventListener("change", loadHistory);
$("#history-list").addEventListener("click", (e) => {
  const b = e.target.closest("[data-open]");
  if (b) openReport(b.dataset.open);
});

// ---------- products ----------
let searchTimer;
$("#product-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => searchProducts(e.target.value.trim()), 300);
});

async function searchProducts(q) {
  const out = $("#product-results");
  if (q.length < 2) { out.innerHTML = ""; return; }
  try {
    const groups = await api(`/api/products/search?q=${encodeURIComponent(q)}`);
    if (!groups.length) {
      out.innerHTML = `<p class="text-center text-slate-500 py-8">Δεν βρέθηκαν προϊόντα.</p>`;
      return;
    }
    out.innerHTML = groups.map((g) => `
      <div class="rounded-xl bg-white dark:bg-slate-800 shadow-sm p-4">
        <div class="font-semibold">${esc(g.name)}</div>
        ${g.product_code ? `<div class="text-xs text-slate-500">Κωδ. ${esc(g.product_code)}</div>` : ""}
        ${g.supplier_count > 1 ? `<div class="mt-2 text-sm font-semibold text-green-700 dark:text-green-300">🏆 Φθηνότερος: ${esc(g.cheapest.supplier)} — ${eur(g.cheapest.unit_price)}</div>` : ""}
        <table class="w-full mt-2 text-sm">
          <thead><tr class="text-left text-xs text-slate-500"><th class="py-1">Ημ/νία</th><th>Προμηθευτής</th><th class="text-right">Τιμή</th></tr></thead>
          <tbody>
            ${g.timeline.map((t) => `
              <tr data-open="${t.invoice_id}" class="border-t border-slate-100 dark:border-slate-700 cursor-pointer ${g.supplier_count > 1 && t.supplier === g.cheapest.supplier && t.date === g.cheapest.date ? "bg-green-50 dark:bg-green-950/40" : ""}">
                <td class="py-2 whitespace-nowrap">${fmtDate(t.date)}</td>
                <td class="truncate max-w-[10rem]">${esc(t.supplier)}</td>
                <td class="text-right font-semibold">${eur(t.unit_price)}</td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>`).join("");
  } catch (e) {
    showAlert(`❌ ${esc(e.message)}`);
  }
}
$("#product-results").addEventListener("click", (e) => {
  const r = e.target.closest("[data-open]");
  if (r) openReport(r.dataset.open);
});

// ---------- init ----------
showTab("scan");
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}
api("/api/health").then((h) => {
  if (!h.ai_configured) showAlert("⚠️ Δεν έχει οριστεί API_KEY στο .env — η εξαγωγή προϊόντων με AI δεν θα λειτουργήσει.", "warn");
}).catch(() => {});
