"use strict";

const KIND_LABELS = { cash: "Cash", depot: "Investments", pillar3a: "Pillar 3a", pillar2: "Pillar 2" };
const DEFAULT_ACCOUNTS = [
  { name: "Neon", institution: "Neon", kind: "cash", currency: "CHF", valuation: "balance" },
  { name: "Neon Invest", institution: "Neon", kind: "depot", currency: "CHF", valuation: "holdings" },
  { name: "DKB", institution: "DKB", kind: "cash", currency: "EUR", valuation: "balance" },
  { name: "VIAC 3a", institution: "VIAC", kind: "pillar3a", currency: "CHF", valuation: "balance" },
  { name: "AXA BVG", institution: "AXA", kind: "pillar2", currency: "CHF", valuation: "balance" },
];

const $ = (sel, root = document) => root.querySelector(sel);
const state = { accounts: [], selected: null, uploads: [], pollTimer: null };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function money(value, currency, digits = 0) {
  if (value == null || Number.isNaN(value)) return "–";
  const n = new Intl.NumberFormat("de-CH", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
  return currency ? `${currency} ${n}` : n;
}

function shortDate(iso) {
  if (!iso) return "";
  return new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 4000);
}

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("login required");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(", ") : data.detail;
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

// --- tabs & login --------------------------------------------------------------

function showTab(name) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== `tab-${name}`));
  $("#login").hidden = true;
  try { localStorage.setItem("fa-tab", name); } catch {}
  if (name === "overview") loadOverview();
  if (name === "accounts") loadAccounts();
  if (name === "inbox") loadUploads();
}

function showLogin() {
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = true));
  $("#login").hidden = false;
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", { method: "POST", body: { token: e.target.token.value } });
    showTab("overview");
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// --- overview --------------------------------------------------------------------

async function loadOverview() {
  const [summary, history] = await Promise.all([api("/api/summary"), api("/api/history")]);
  const cur = summary.base_currency;
  $("#total").textContent = money(summary.total, cur);
  const sub = [];
  if (history.length > 1) {
    const first = history[0];
    const change = summary.total - first.total;
    sub.push(`${change >= 0 ? "+" : "−"}${money(Math.abs(change), cur)} since ${shortDate(first.day)}`);
  }
  const issues = summary.accounts.filter((a) => a.problems.length || a.stale_days).length;
  if (issues) sub.push(`${issues} account${issues > 1 ? "s" : ""} need${issues > 1 ? "" : "s"} attention`);
  $("#total-sub").textContent = sub.join(" · ");
  renderSplit(summary);
  state.history = history;
  state.base = cur;
  renderHistory(history, cur);
  renderAccountsTable(summary);
  updateInboxBadge();
}

function renderSplit(summary) {
  const total = summary.by_kind.reduce((s, k) => s + Math.max(k.value, 0), 0);
  const bar = $("#split-bar");
  bar.innerHTML = total
    ? summary.by_kind.filter((k) => k.value > 0).map((k) =>
        `<span style="width:${(k.value / total) * 100}%;background:var(--kind-${k.kind})" data-tip="${esc(k.label)}: ${esc(money(k.value, summary.base_currency))}"></span>`).join("")
    : "";
  bar.setAttribute("aria-label", summary.by_kind.map((k) => `${k.label} ${money(k.value, summary.base_currency)}`).join(", "));
  $("#split-legend").innerHTML = summary.by_kind.map((k) => `
    <li><span class="swatch" style="background:var(--kind-${k.kind})"></span>${esc(k.label)}
      <span class="val">${esc(money(k.value, summary.base_currency))}</span>
      <span class="pct">${total ? Math.round((k.value / total) * 100) + "%" : ""}</span></li>`).join("");
}

function renderHistory(history, cur) {
  const box = $("#history");
  if (history.length < 2) {
    box.innerHTML = `<div class="empty">The history builds up from today: the app records your net worth every day it runs.</div>`;
    return;
  }
  // Draw at the box's real width so axis text stays legible on a phone.
  const W = Math.max(box.clientWidth, 280), H = W < 500 ? 200 : 260, L = 44, R = 8, T = 10, B = 26;
  const xs = history.map((p) => new Date(p.day + "T00:00:00").getTime());
  const ys = history.map((p) => p.total);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.1 || Math.abs(y1) * 0.02 || 1;
  y0 -= pad; y1 += pad;
  const sx = (x) => L + ((x - x0) / (x1 - x0)) * (W - L - R);
  const sy = (y) => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
  const ticks = 4;
  const grid = Array.from({ length: ticks + 1 }, (_, i) => y0 + ((y1 - y0) * i) / ticks).map((v) =>
    `<line class="gridline" x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/>
     <text x="${L - 8}" y="${sy(v) + 4}" text-anchor="end">${esc(money(v / 1000, null, 0))}k</text>`).join("");
  const xl = (W < 500 ? [0, history.length - 1] : [0, Math.floor(history.length / 2), history.length - 1]).map((i) =>
    `<text x="${sx(xs[i])}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === history.length - 1 ? "end" : "middle"}">${esc(shortDate(history[i].day))}</text>`).join("");
  const path = history.map((p, i) => `${i ? "L" : "M"}${sx(xs[i]).toFixed(1)},${sy(p.total).toFixed(1)}`).join("");
  const area = `${path}L${sx(x1)},${sy(y0)}L${sx(x0)},${sy(y0)}Z`;
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Net worth over time">
    <g class="axis">${grid}${xl}</g>
    <path class="area" d="${area}"/><path class="line" d="${path}"/>
    <line class="cross" y1="${T}" y2="${H - B}" visibility="hidden"/>
    <circle class="dot" r="5" visibility="hidden"/>
    <rect x="${L}" y="0" width="${W - L - R}" height="${H}" fill="transparent"/>
  </svg>`;
  const svg = $("svg", box), cross = $(".cross", box), dot = $(".dot", box), tip = $("#tooltip");
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const vx = ((e.clientX - r.left) / r.width) * W;
    let best = 0;
    xs.forEach((x, i) => { if (Math.abs(sx(x) - vx) < Math.abs(sx(xs[best]) - vx)) best = i; });
    const p = history[best], px = sx(xs[best]), py = sy(p.total);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", px); dot.setAttribute("cy", py); dot.setAttribute("visibility", "visible");
    const parts = Object.keys(KIND_LABELS).filter((k) => p[k]).map((k) => `${KIND_LABELS[k]} ${money(p[k], cur)}`);
    tip.innerHTML = `<strong>${esc(money(p.total, cur))}</strong> · ${esc(shortDate(p.day))}<br><span class="muted">${esc(parts.join(" · "))}</span>`;
    tip.hidden = false;
    const tx = r.left + (px / W) * r.width;
    tip.style.left = Math.min(tx + 12, window.innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = r.top + (py / H) * r.height - tip.offsetHeight - 10 + "px";
  });
  svg.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true;
  });
}

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (!$("#tab-overview").hidden && state.history) renderHistory(state.history, state.base); }, 200);
});

function accountWarnings(a) {
  const w = [...a.problems];
  if (a.stale_days) w.push(`last update ${a.stale_days} days ago`);
  return w.map((t) => `<div class="warn">${esc(t)}</div>`).join("");
}

function renderAccountsTable(summary) {
  const box = $("#accounts-table");
  if (!summary.accounts.length) {
    box.innerHTML = `<div class="empty">No accounts yet. <button class="link" onclick="showTab('accounts')">Set them up</button></div>`;
    return;
  }
  box.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Account</th><th class="hide-sm">Type</th><th class="num">Value</th><th class="num">${esc(summary.base_currency)}</th><th class="num hide-sm">As of</th></tr></thead>
    <tbody>${summary.accounts.map((a) => `<tr>
      <td><span class="kind-dot" style="background:var(--kind-${a.kind})"></span>${esc(a.name)}
        <div class="muted small">${esc(a.institution)}</div>${accountWarnings(a)}</td>
      <td class="hide-sm">${esc(KIND_LABELS[a.kind])}</td>
      <td class="num">${esc(money(a.value, a.currency))}</td>
      <td class="num">${esc(money(a.value_base, null))}</td>
      <td class="num hide-sm muted">${esc(shortDate(a.as_of))}</td></tr>`).join("")}
    </tbody></table></div>`;
}

$("#refresh").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try {
    const r = await api("/api/refresh", { method: "POST" });
    toast(r.errors.length ? `Updated with ${r.errors.length} price error(s): ${r.errors[0]}` : "Prices updated");
    await loadOverview();
  } catch (err) {
    toast(err.message);
  } finally {
    e.target.disabled = false;
  }
});

// split bar tooltips
document.addEventListener("pointermove", (e) => {
  const seg = e.target.closest?.("[data-tip]");
  const tip = $("#tooltip");
  if (seg) {
    tip.textContent = seg.dataset.tip;
    tip.hidden = false;
    tip.style.left = e.clientX + 12 + "px";
    tip.style.top = e.clientY - 36 + "px";
  } else if (!e.target.closest?.("#history")) {
    tip.hidden = true;
  }
});

// --- accounts ----------------------------------------------------------------------

async function loadAccounts() {
  state.accounts = await api("/api/accounts");
  $("#setup").hidden = state.accounts.length > 0;
  $("#account-list").innerHTML = state.accounts.map((a) => `
    <li><button data-id="${a.id}" class="${a.id === state.selected ? "active" : ""}">
      <span class="kind-dot" style="background:var(--kind-${a.kind})"></span>${esc(a.name)}
      <span class="sub">${esc(a.currency)}</span></button></li>`).join("");
  document.querySelectorAll("#account-list button").forEach((b) =>
    b.addEventListener("click", () => selectAccount(Number(b.dataset.id))));
  if (state.selected && state.accounts.some((a) => a.id === state.selected)) renderAccountDetail();
  else $("#account-detail").innerHTML = `<p class="muted">Pick an account.</p>`;
}

function selectAccount(id) {
  state.selected = id;
  document.querySelectorAll("#account-list button").forEach((b) => b.classList.toggle("active", Number(b.dataset.id) === id));
  renderAccountDetail();
}

async function renderAccountDetail() {
  const a = state.accounts.find((x) => x.id === state.selected);
  const box = $("#account-detail");
  const today = new Date().toISOString().slice(0, 10);
  box.innerHTML = `
    <div class="upload-head"><h2>${esc(a.name)}</h2>
      <span class="muted small">${esc(a.institution)} · ${esc(KIND_LABELS[a.kind])} · ${esc(a.currency)}</span></div>
    <form id="edit-account" class="form-grid">
      <label>Name <input name="name" value="${esc(a.name)}" required></label>
      <label>Institution <input name="institution" value="${esc(a.institution)}" required></label>
      <label>Currency <input name="currency" value="${esc(a.currency)}" pattern="[A-Z]{3}" maxlength="3" required></label>
      <label>Value from
        <select name="valuation">
          <option value="balance" ${a.valuation === "balance" ? "selected" : ""}>Balance I enter or import</option>
          <option value="holdings" ${a.valuation === "holdings" ? "selected" : ""}>Positions × market prices</option>
        </select></label>
      <button class="ghost">Save</button>
    </form>
    <div id="detail-body" style="margin-top:20px"></div>
    <div class="actions"><button id="archive" class="link danger">Remove account</button></div>`;
  $("#edit-account").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    await api(`/api/accounts/${a.id}`, { method: "PATCH", body: {
      name: f.name.value, institution: f.institution.value, currency: f.currency.value.toUpperCase(), valuation: f.valuation.value } });
    toast("Saved");
    loadAccounts();
  });
  $("#archive").addEventListener("click", async () => {
    if (!confirm(`Remove ${a.name}? It disappears from the overview; its data is kept.`)) return;
    await api(`/api/accounts/${a.id}`, { method: "PATCH", body: { archived: true } });
    state.selected = null;
    loadAccounts();
  });
  if (a.valuation === "holdings") renderHoldings(a);
  else renderBalances(a, today);
}

async function renderBalances(a, today) {
  const balances = await api(`/api/accounts/${a.id}/balances`);
  $("#detail-body").innerHTML = `
    <h2>Balance</h2>
    <form id="balance-form" class="row">
      <label>Date <input type="date" name="day" value="${today}" required></label>
      <label>Amount (${esc(a.currency)}) <input type="number" step="0.01" name="amount" required></label>
      <button class="primary">Add</button>
    </form>
    <p class="muted small">Or drop a screenshot/statement in the Inbox and the balance is read for you.</p>
    ${balances.length ? `<div class="table-wrap"><table><thead><tr><th>Date</th><th class="num">Amount</th><th>Source</th><th></th></tr></thead><tbody>
      ${balances.map((b) => `<tr><td>${esc(shortDate(b.day))}</td><td class="num">${esc(money(b.amount, a.currency, 2))}</td>
        <td class="muted small">${esc(b.source)}${b.note ? " · " + esc(b.note) : ""}</td>
        <td class="num"><button class="link danger" data-del="${b.id}">Delete</button></td></tr>`).join("")}
    </tbody></table></div>` : ""}`;
  $("#balance-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await api(`/api/accounts/${a.id}/balances`, { method: "POST", body: { day: e.target.day.value, amount: Number(e.target.amount.value) } });
    toast("Balance added");
    renderBalances(a, today);
  });
  document.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/balances/${b.dataset.del}`, { method: "DELETE" });
    renderBalances(a, today);
  }));
}

function holdingRow(h = {}) {
  return `<tr>
    <td><input name="name" value="${esc(h.name)}" placeholder="Vanguard FTSE All-World" required></td>
    <td><input name="symbol" value="${esc(h.symbol)}" placeholder="optional" list="symbol-options"></td>
    <td><input name="quantity" type="number" step="any" value="${h.quantity ?? ""}" required></td>
    <td><input name="manual_price" type="number" step="any" value="${h.manual_price ?? ""}" placeholder="only without ticker"></td>
    <td><input name="currency" value="${esc(h.currency || "CHF")}" maxlength="3" style="width:64px"></td>
    <td class="num muted small">${h.value_base != null ? esc(money(h.value_base, null)) : esc(h.problem || "")}</td>
    <td><button type="button" class="link danger" data-remove>✕</button></td></tr>`;
}

async function renderHoldings(a) {
  const holdings = await api(`/api/accounts/${a.id}/holdings`);
  $("#detail-body").innerHTML = `
    <h2>Positions</h2>
    <p class="muted small">Enter what you hold; prices are fetched daily. Ticker = Yahoo Finance symbol (e.g. VWRL.SW, CHSPI.SW, VT).
      For cash in the depot leave the ticker empty: quantity = amount, price = 1.</p>
    <form id="symbol-search" class="row">
      <label>Find a ticker by name or ISIN <input name="q" placeholder="IE00B3RBWM25"></label>
      <button class="ghost">Search</button>
    </form>
    <div id="symbol-results" class="small"></div>
    <datalist id="symbol-options"></datalist>
    <form id="holdings-form">
      <div class="table-wrap"><table class="holdings-editor">
        <thead><tr><th>Name</th><th>Ticker</th><th>Quantity</th><th>Price</th><th>Cur.</th><th class="num">${esc("CHF")}</th><th></th></tr></thead>
        <tbody>${holdings.map(holdingRow).join("")}</tbody></table></div>
      <div class="actions"><button type="button" id="add-row" class="ghost">Add position</button><button class="primary">Save positions</button></div>
    </form>`;
  const tbody = $("#holdings-form tbody");
  if (!holdings.length) tbody.insertAdjacentHTML("beforeend", holdingRow());
  $("#add-row").addEventListener("click", () => tbody.insertAdjacentHTML("beforeend", holdingRow()));
  tbody.addEventListener("click", (e) => { if (e.target.matches("[data-remove]")) e.target.closest("tr").remove(); });
  $("#symbol-search").addEventListener("submit", async (e) => {
    e.preventDefault();
    const out = $("#symbol-results");
    out.textContent = "Searching…";
    try {
      const found = await api(`/api/symbols?q=${encodeURIComponent(e.target.q.value)}`);
      out.innerHTML = found.length
        ? found.map((s) => `<div><strong>${esc(s.symbol)}</strong> ${esc(s.name)} <span class="muted">${esc(s.exchange)} · ${esc(s.type)}</span></div>`).join("")
        : "Nothing found.";
      $("#symbol-options").innerHTML = found.map((s) => `<option value="${esc(s.symbol)}">${esc(s.name)}</option>`).join("");
    } catch (err) {
      out.textContent = err.message;
    }
  });
  $("#holdings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = [...tbody.querySelectorAll("tr")].map((tr) => {
      const v = (n) => tr.querySelector(`[name=${n}]`).value.trim();
      return { name: v("name"), symbol: v("symbol") || null, quantity: Number(v("quantity")),
        manual_price: v("manual_price") ? Number(v("manual_price")) : null, currency: (v("currency") || "CHF").toUpperCase() };
    });
    const btn = e.submitter;
    btn.disabled = true;
    try {
      const r = await api(`/api/accounts/${a.id}/holdings`, { method: "PUT", body });
      toast(r.errors.length ? `Saved. Price problems: ${r.errors.join("; ")}` : "Saved, prices updated");
      renderHoldings(a);
    } catch (err) {
      toast(err.message);
      btn.disabled = false;
    }
  });
}

$("#account-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const created = await api("/api/accounts", { method: "POST", body: {
    name: f.name.value, institution: f.institution.value, kind: f.kind.value,
    currency: f.currency.value.toUpperCase(), valuation: f.valuation.value } });
  f.reset();
  state.selected = created.id;
  loadAccounts();
});

$("#setup-defaults").addEventListener("click", async (e) => {
  e.target.disabled = true;
  for (const a of DEFAULT_ACCOUNTS) await api("/api/accounts", { method: "POST", body: a });
  toast("Accounts created. Add a balance or positions to each.");
  loadAccounts();
});

// --- inbox ---------------------------------------------------------------------------

async function updateInboxBadge() {
  const uploads = await api("/api/uploads");
  const n = uploads.filter((u) => u.status !== "reading").length;
  const badge = $("#inbox-count");
  badge.textContent = n;
  badge.hidden = n === 0;
  return uploads;
}

async function uploadFiles(files) {
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    try {
      await api("/api/upload", { method: "POST", body: form });
    } catch (err) {
      toast(`${file.name}: ${err.message}`);
    }
  }
  loadUploads();
}

const dropzone = $("#dropzone");
$("#file-input").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("over"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("over"));
dropzone.addEventListener("drop", (e) => { e.preventDefault(); dropzone.classList.remove("over"); uploadFiles([...e.dataTransfer.files]); });

async function loadUploads() {
  if (!state.accounts.length) state.accounts = await api("/api/accounts");
  const uploads = await updateInboxBadge();
  state.uploads = uploads;
  const box = $("#uploads");
  box.innerHTML = uploads.length ? uploads.map(uploadCard).join("") : `<div class="card empty">Nothing waiting for review.</div>`;
  uploads.forEach(wireUpload);
  clearTimeout(state.pollTimer);
  if (uploads.some((u) => u.status === "reading") && !$("#tab-inbox").hidden) state.pollTimer = setTimeout(loadUploads, 2500);
}

function uploadCard(u) {
  const head = `<div class="upload-head"><h2>${esc(u.filename)}</h2>
    <a class="small" href="/api/uploads/${u.id}/file" target="_blank" rel="noopener">View file</a></div>`;
  if (u.status === "reading") return `<div class="card">${head}<p class="muted">Reading…</p></div>`;
  if (u.status === "failed") return `<div class="card" data-upload="${u.id}">${head}<p class="error">${esc(u.error)}</p>
    <div class="actions"><button class="ghost" data-act="retry">Try again</button><button class="link" data-act="dismiss">Dismiss</button></div></div>`;
  const x = u.extracted || {};
  const options = state.accounts.map((a) =>
    `<option value="${a.id}" ${a.id === x.account_id ? "selected" : ""}>${esc(a.name)} (${esc(a.currency)})</option>`).join("");
  const positions = x.positions || [];
  return `<div class="card" data-upload="${u.id}">${head}
    ${x.notes ? `<div class="notes">${esc(x.notes)}</div>` : ""}
    <div class="row">
      <label>Account <select name="account_id"><option value="">Choose…</option>${options}</select></label>
      <label>Date <input type="date" name="as_of" value="${esc(x.as_of || new Date().toISOString().slice(0, 10))}"></label>
      <label>Balance${x.currency ? ` (${esc(x.currency)})` : ""} <input type="number" step="0.01" name="balance" value="${x.balance ?? ""}"></label>
    </div>
    ${positions.length ? `<h2 style="margin-top:16px">Positions found</h2><div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>ISIN / ticker</th><th class="num">Quantity</th><th class="num">Value</th></tr></thead>
      <tbody>${positions.map((p) => `<tr><td>${esc(p.name)}</td><td class="small">${esc(p.isin || "")} ${esc(p.symbol || "")}</td>
        <td class="num">${p.quantity ?? "–"}</td><td class="num">${esc(money(p.value, p.currency, 2))}</td></tr>`).join("")}</tbody></table></div>
      <label class="check" style="margin-top:10px" data-holdings-only><input type="checkbox" name="replace_holdings">
        Replace this account's positions with these (tickers are looked up by ISIN)</label>` : ""}
    <div class="actions"><button class="primary" data-act="apply">Save to account</button><button class="link" data-act="dismiss">Dismiss</button></div>
  </div>`;
}

function wireUpload(u) {
  const card = document.querySelector(`[data-upload="${u.id}"]`);
  if (!card) return;
  const select = $("[name=account_id]", card), replace = $("[data-holdings-only]", card);
  const syncReplace = () => {
    if (!replace) return;
    const account = state.accounts.find((a) => a.id === Number(select.value));
    replace.hidden = account?.valuation !== "holdings";
    if (replace.hidden) $("input", replace).checked = false;
  };
  select?.addEventListener("change", syncReplace);
  syncReplace();
  card.addEventListener("click", async (e) => {
    const act = e.target.dataset.act;
    if (!act) return;
    e.target.disabled = true;
    try {
      if (act === "dismiss") await api(`/api/uploads/${u.id}/dismiss`, { method: "POST" });
      if (act === "retry") await api(`/api/uploads/${u.id}/retry`, { method: "POST" });
      if (act === "apply") {
        const accountId = Number($("[name=account_id]", card).value);
        if (!accountId) { toast("Choose an account first"); e.target.disabled = false; return; }
        const bal = $("[name=balance]", card).value;
        const replace = $("[name=replace_holdings]", card)?.checked || false;
        const r = await api(`/api/uploads/${u.id}/apply`, { method: "POST", body: {
          account_id: accountId, as_of: $("[name=as_of]", card).value, balance: bal === "" ? null : Number(bal),
          replace_holdings: replace, positions: replace ? u.extracted.positions : [] } });
        toast(["Saved", ...r.notes, ...r.errors].join(". "));
      }
      loadUploads();
    } catch (err) {
      toast(err.message);
      e.target.disabled = false;
    }
  });
}

// --- start -------------------------------------------------------------------------

let startTab = "overview";
try { startTab = localStorage.getItem("fa-tab") || "overview"; } catch {}
showTab(startTab);
