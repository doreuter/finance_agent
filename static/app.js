"use strict";

const KIND_LABELS = { cash: "Cash", depot: "Investments", pillar3a: "Pillar 3a", pillar2: "Pillar 2" };
const DEFAULT_ACCOUNTS = [
  { name: "Main account", institution: "Neon", kind: "cash", currency: "CHF", valuation: "balance" },
  { name: "Neon Invest", institution: "Neon", kind: "depot", currency: "CHF", valuation: "holdings" },
  { name: "Girokonto", institution: "DKB", kind: "cash", currency: "EUR", valuation: "balance" },
  { name: "VIAC 3a", institution: "VIAC", kind: "pillar3a", currency: "CHF", valuation: "balance" },
  { name: "AXA BVG", institution: "AXA", kind: "pillar2", currency: "CHF", valuation: "balance" },
];

const $ = (sel, root = document) => root.querySelector(sel);
const state = { accounts: [], selected: null, uploads: [], pollTimer: null, me: null, family: [], owner: "all" };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function money(value, currency, digits = 0) {
  if (value == null || Number.isNaN(value)) return "–";
  const n = new Intl.NumberFormat("de-CH", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
  return currency ? `${currency} ${n}` : n;
}

// Shares can be fractional (Neon Invest): show the decimals there are, without padding, so 12.5 never
// reads as "12.500" (twelve thousand five hundred in German notation).
function quantity(value) {
  if (value == null) return "–";
  return new Intl.NumberFormat("de-CH", { maximumFractionDigits: 6 }).format(value);
}

function shortDate(iso) {
  if (!iso) return "";
  return new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

function groupByBank(accounts) {
  const groups = new Map();
  for (const a of accounts) {
    if (!groups.has(a.institution)) groups.set(a.institution, []);
    groups.get(a.institution).push(a);
  }
  return [...groups.entries()];
}

function setBankOptions() {
  $("#bank-options").innerHTML = [...new Set(state.accounts.map((a) => a.institution))]
    .map((b) => `<option value="${esc(b)}">`).join("");
}

function ownerName(ownerId) {
  if (ownerId == null) return "Joint";
  return state.family.find((u) => u.id === ownerId)?.name || "?";
}

function ownerOptions(selected) {
  return state.family.map((u) => `<option value="${u.id}" ${u.id === selected ? "selected" : ""}>${esc(u.name)}</option>`).join("")
    + `<option value="" ${selected == null ? "selected" : ""}>Joint</option>`;
}

const ownerValue = (v) => (v === "" ? null : Number(v));

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

const SCREENS = ["#login", "#setup-first"];

function showTab(name) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== `tab-${name}`));
  SCREENS.forEach((sel) => ($(sel).hidden = true));
  $("nav").hidden = false;
  try { localStorage.setItem("fa-tab", name); } catch {}
  if (name === "overview") loadOverview();
  if (name === "accounts") loadAccounts();
  if (name === "inbox") loadUploads();
  if (name === "family") loadFamily();
  if (name === "insights") loadInsights();
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-goto]");
  if (b) showTab(b.dataset.goto);
});

function showScreen(sel) {
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = true));
  SCREENS.forEach((s) => ($(s).hidden = s !== sel));
  $("nav").hidden = true;
}

function showLogin() {
  showScreen("#login");
}

async function loadMe() {
  const r = await api("/api/me");
  state.me = r.user;
  state.family = r.family;
  document.querySelectorAll(".owner-select").forEach((sel) => (sel.innerHTML = ownerOptions(state.me.id)));
}

async function enterApp() {
  await loadMe();
  let startTab = "overview";
  try {
    startTab = localStorage.getItem("fa-tab") || "overview";
    state.owner = localStorage.getItem("fa-owner") || "all";
  } catch {}
  showTab(startTab);
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", { method: "POST", body: { username: e.target.username.value, password: e.target.password.value } });
    e.target.reset();
    $("#login-error").textContent = "";
    await enterApp();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.submitter;
  btn.disabled = true;
  try {
    const form = new FormData(e.target);
    if (!e.target.import_db.files.length) form.delete("import_db");
    await api("/api/setup", { method: "POST", body: form });
    await enterApp();
  } catch (err) {
    $("#setup-error").textContent = err.message;
    btn.disabled = false;
  }
});

document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// --- overview --------------------------------------------------------------------

function renderOwnerFilter() {
  const options = [["all", "Family"], ...state.family.map((u) => [String(u.id), u.name]), ["joint", "Joint"]];
  if (!options.some(([v]) => v === state.owner)) state.owner = "all";
  $("#owner-filter").innerHTML = options.map(([v, label]) =>
    `<button class="${v === state.owner ? "active" : ""}" data-owner="${esc(v)}" aria-pressed="${v === state.owner}">${esc(label)}</button>`).join("");
  $("#total-label").textContent = state.owner === "all" ? "Family net worth"
    : state.owner === "joint" ? "Joint accounts" : `Net worth · ${options.find(([v]) => v === state.owner)[1]}`;
}

$("#owner-filter").addEventListener("click", (e) => {
  const b = e.target.closest("[data-owner]");
  if (!b) return;
  state.owner = b.dataset.owner;
  try { localStorage.setItem("fa-owner", state.owner); } catch {}
  loadOverview();
});

async function loadOverview() {
  renderOwnerFilter();
  const q = `?owner=${encodeURIComponent(state.owner)}`;
  const [summary, history, portfolio] = await Promise.all([api("/api/summary" + q), api("/api/history" + q), api("/api/portfolio" + q)]);
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
  renderPortfolio(portfolio);
  updateInboxBadge();
  updateTasksBadge();
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

function pct(x, digits = 1) {
  if (x == null) return "–";
  return `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x * 100).toFixed(digits)}%`;
}

function renderPortfolio(p) {
  state.portfolio = p;
  const card = $("#holdings-card");
  card.hidden = !p.positions.length;
  if (!p.positions.length) return;
  const cur = p.base_currency;
  const day = p.day_change_base;
  $("#holdings-sub").textContent = `${money(p.total, cur)}${day != null ? ` · today ${day >= 0 ? "+" : "−"}${money(Math.abs(day), cur)}` : ""}`;
  const signed = (v) => (v == null ? "" : `${v >= 0 ? "+" : "−"}${money(Math.abs(v), null)}`);
  // One group per bank, largest first; inside, positions stay sorted by value (as the server sends them).
  const banks = groupByBank(p.positions).map(([bank, list]) => {
    const total = list.reduce((sum, x) => sum + (x.value_base ?? 0), 0);
    const moves = list.filter((x) => x.day_change_base != null);
    return { bank, list, total, day: moves.length ? moves.reduce((sum, x) => sum + x.day_change_base, 0) : null,
      accounts: new Set(list.map((x) => `${x.account}|${x.owner_id}`)).size };
  }).sort((a, b) => b.total - a.total);
  const row = (x, showAccount) => `<tr>
      <td class="indent">${x.symbol ? `<button class="link position-link" data-quote="${x.id}">${esc(x.name)}</button>` : esc(x.name)}
        <div class="muted small">${esc([x.symbol, x.value_only ? "value from statement" : "",
          showAccount ? `${x.account}${state.owner === "all" ? " · " + ownerName(x.owner_id) : ""}` : ""].filter(Boolean).join(" · "))}</div>
        ${x.problem ? `<div class="warn">${esc(x.problem)}</div>` : ""}</td>
      <td class="num hide-sm">${esc(quantity(x.quantity))}</td>
      <td class="num hide-sm">${x.price == null ? "–" : esc(money(x.price, x.price_currency, 2))}</td>
      <td class="num">${esc(money(x.value_base, null))}</td>
      <td class="num"><span class="share-bar" style="--w:${Math.round((x.share || 0) * 100)}%"></span>${x.share != null ? Math.round(x.share * 100) + "%" : "–"}</td>
      <td class="num ${x.day_change > 0 ? "up" : x.day_change < 0 ? "down" : "muted"}">${esc(pct(x.day_change))}</td></tr>`;
  $("#holdings").innerHTML = `<div class="table-wrap"><table class="holdings">
    <thead><tr><th>Position</th><th class="num hide-sm">Quantity</th><th class="num hide-sm">Price</th>
      <th class="num">${esc(cur)}</th><th class="num">Share</th><th class="num">Today</th></tr></thead>
    ${banks.map((g) => {
      // a bank with one depot names it in the header; with several, each position names its account
      const single = g.accounts === 1 ? g.list[0] : null;
      const label = single ? `${g.bank} · ${single.account}${state.owner === "all" && state.family.length > 1 ? " · " + ownerName(single.owner_id) : ""}` : g.bank;
      return `<tbody><tr class="bank-row"><th>${esc(label)}</th><th class="hide-sm"></th><th class="hide-sm"></th>
          <th class="num">${esc(money(g.total, null))}</th><th class="num">${p.total ? Math.round((g.total / p.total) * 100) + "%" : ""}</th>
          <th class="num ${g.day > 0 ? "up" : g.day < 0 ? "down" : ""}">${esc(signed(g.day))}</th></tr>
        ${g.list.map((x) => row(x, !single)).join("")}</tbody>`;
    }).join("")}
    </table></div>`;
}

// --- position pop-up: price chart from Yahoo Finance --------------------------------------

const quoteState = { position: null, range: "1y" };

$("#holdings").addEventListener("click", (e) => {
  const b = e.target.closest("[data-quote]");
  if (!b) return;
  quoteState.position = state.portfolio.positions.find((x) => x.id === Number(b.dataset.quote));
  openQuote();
});

$("#quote-ranges").addEventListener("click", (e) => {
  const b = e.target.closest("[data-range]");
  if (!b) return;
  quoteState.range = b.dataset.range;
  loadQuote();
});

$("#quote-dialog").addEventListener("close", () => ($(".tooltip", $("#quote-dialog")).hidden = true));

$("#quote-dialog").addEventListener("click", (e) => {
  // a click on the backdrop (the dialog element itself) or the ✕ closes it
  if (e.target === e.currentTarget || e.target.closest("[data-close]")) e.currentTarget.close();
});

function openQuote() {
  const x = quoteState.position;
  $("#quote-title").textContent = x.name;
  $("#quote-sub").textContent = x.symbol;
  $("#quote-yahoo").href = `https://finance.yahoo.com/quote/${encodeURIComponent(x.symbol)}`;
  $("#quote-position").innerHTML = `You hold <strong>${esc(quantity(x.quantity))}</strong> · worth ${esc(money(x.value_base, state.base, 0))}
    · ${esc(x.account)}${state.family.length > 1 ? " · " + esc(ownerName(x.owner_id)) : ""}`;
  $("#quote-dialog").showModal();
  loadQuote();
}

async function loadQuote() {
  const x = quoteState.position;
  document.querySelectorAll("#quote-ranges button").forEach((b) => {
    b.classList.toggle("active", b.dataset.range === quoteState.range);
    b.setAttribute("aria-pressed", b.dataset.range === quoteState.range);
  });
  const box = $("#quote-chart");
  box.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const q = await api(`/api/quote/${encodeURIComponent(x.symbol)}?range=${quoteState.range}`);
    if (quoteState.position !== x) return;  // another position was opened meanwhile
    $("#quote-title").textContent = q.name || x.name;
    $("#quote-sub").textContent = [q.symbol, q.exchange, q.currency].filter(Boolean).join(" · ");
    const first = q.points[0]?.close, last = q.price ?? q.points.at(-1)?.close;
    const change = first ? last / first - 1 : null;
    const label = { "1mo": "1 month", "6mo": "6 months", "1y": "1 year", "5y": "5 years", max: "all time" }[quoteState.range];
    $("#quote-figures").innerHTML = `
      <div><div class="muted small">Price</div><div class="figure">${esc(money(last, q.currency, 2))}</div></div>
      <div><div class="muted small">${esc(label)}</div><div class="figure ${change > 0 ? "up" : change < 0 ? "down" : ""}">${esc(pct(change))}</div></div>
      <div class="hide-sm"><div class="muted small">52-week range</div><div class="figure small-figure">${esc(money(q.low_52w, null, 2))} – ${esc(money(q.high_52w, null, 2))}</div></div>`;
    lineChart(box, q.points, (v) => money(v, q.currency, 2), `${q.name} price`);
  } catch (err) {
    box.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

function lineChart(box, points, fmt, label) {
  if (points.length < 2) {
    box.innerHTML = `<div class="empty">Not enough price data.</div>`;
    return;
  }
  const W = Math.max(box.clientWidth, 260), H = W < 500 ? 180 : 220, L = 52, R = 8, T = 10, B = 24;
  const xs = points.map((p) => new Date(p.day + "T00:00:00").getTime());
  const ys = points.map((p) => p.close);
  const x0 = xs[0], x1 = xs.at(-1);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.08 || y1 * 0.02 || 1;
  y0 -= pad; y1 += pad;
  const sx = (x) => L + ((x - x0) / (x1 - x0 || 1)) * (W - L - R);
  const sy = (y) => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
  const digits = y1 - y0 < 10 ? 2 : 0;
  const grid = [0, 1, 2, 3].map((i) => y0 + ((y1 - y0) * i) / 3).map((v) =>
    `<line class="gridline" x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/>
     <text x="${L - 8}" y="${sy(v) + 4}" text-anchor="end">${esc(money(v, null, digits))}</text>`).join("");
  const xl = [0, points.length - 1].map((i) =>
    `<text x="${sx(xs[i])}" y="${H - 6}" text-anchor="${i ? "end" : "start"}">${esc(shortDate(points[i].day))}</text>`).join("");
  const path = points.map((p, i) => `${i ? "L" : "M"}${sx(xs[i]).toFixed(1)},${sy(p.close).toFixed(1)}`).join("");
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
    <g class="axis">${grid}${xl}</g>
    <path class="area" d="${path}L${sx(x1)},${sy(y0)}L${sx(x0)},${sy(y0)}Z"/><path class="line" d="${path}"/>
    <line class="cross" y1="${T}" y2="${H - B}" visibility="hidden"/>
    <circle class="dot" r="5" visibility="hidden"/>
    <rect x="${L}" y="0" width="${W - L - R}" height="${H}" fill="transparent"/>
  </svg>`;
  // inside a modal dialog (top layer) the page's tooltip would be hidden behind it
  const svg = $("svg", box), cross = $(".cross", box), dot = $(".dot", box);
  const tip = box.closest("dialog") ? $(".tooltip", box.closest("dialog")) : $("#tooltip");
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const vx = ((e.clientX - r.left) / r.width) * W;
    let best = 0;
    xs.forEach((x, i) => { if (Math.abs(sx(x) - vx) < Math.abs(sx(xs[best]) - vx)) best = i; });
    const p = points[best], px = sx(xs[best]), py = sy(p.close);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", px); dot.setAttribute("cy", py); dot.setAttribute("visibility", "visible");
    tip.innerHTML = `<strong>${esc(fmt(p.close))}</strong> · ${esc(shortDate(p.day))}`;
    tip.hidden = false;
    tip.style.left = Math.min(r.left + (px / W) * r.width + 12, window.innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = r.top + (py / H) * r.height - tip.offsetHeight - 10 + "px";
  });
  svg.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true;
  });
}

function accountWarnings(a) {
  const w = [...a.problems];
  if (a.stale_days) w.push(`last update ${a.stale_days} days ago`);
  return w.map((t) => `<div class="warn">${esc(t)}</div>`).join("");
}

function renderAccountsTable(summary) {
  const box = $("#accounts-table");
  if (!summary.accounts.length) {
    box.innerHTML = state.owner === "all"
      ? `<div class="empty">No accounts yet. <button class="link" data-goto="accounts">Set them up</button></div>`
      : `<div class="empty">No accounts for this filter.</div>`;
    return;
  }
  const row = (a) => `<tr>
      <td class="indent"><span class="kind-dot" style="background:var(--kind-${a.kind})"></span>${esc(a.name)}
        ${state.owner === "all" ? `<span class="owner-tag">${esc(ownerName(a.owner_id))}</span>` : ""}${accountWarnings(a)}</td>
      <td class="hide-sm">${esc(KIND_LABELS[a.kind])}</td>
      <td class="num">${esc(money(a.value, a.currency))}</td>
      <td class="num">${esc(money(a.value_base, null))}</td>
      <td class="num hide-sm muted">${esc(shortDate(a.as_of))}</td></tr>`;
  box.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Account</th><th class="hide-sm">Type</th><th class="num">Value</th><th class="num">${esc(summary.base_currency)}</th><th class="num hide-sm">As of</th></tr></thead>
    ${groupByBank(summary.accounts).map(([bank, list]) => {
      const subtotal = list.reduce((sum, a) => sum + (a.value_base ?? 0), 0);
      return `<tbody><tr class="bank-row"><th colspan="3">${esc(bank)}</th><th class="num">${esc(money(subtotal, null))}</th><th class="hide-sm"></th></tr>
        ${list.map(row).join("")}</tbody>`;
    }).join("")}
    </table></div>`;
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
  setBankOptions();
  $("#account-list").innerHTML = groupByBank(state.accounts).map(([bank, list]) => `
    <li class="bank-head"><span>${esc(bank)}</span><button class="link small" data-add-to="${esc(bank)}">+ Account</button></li>
    ${list.map((a) => `<li><button data-id="${a.id}" class="${a.id === state.selected ? "active" : ""}">
      <span class="kind-dot" style="background:var(--kind-${a.kind})"></span>${esc(a.name)}
      <span class="sub">${esc(ownerName(a.owner_id))} · ${esc(a.currency)}</span></button></li>`).join("")}`).join("");
  document.querySelectorAll("#account-list [data-id]").forEach((b) =>
    b.addEventListener("click", () => selectAccount(Number(b.dataset.id))));
  document.querySelectorAll("#account-list [data-add-to]").forEach((b) => b.addEventListener("click", () => {
    const form = $("#account-form");
    form.closest("details").open = true;
    form.institution.value = b.dataset.addTo;
    form.name.focus();
  }));
  if (state.selected && state.accounts.some((a) => a.id === state.selected)) renderAccountDetail();
  else $("#account-detail").innerHTML = `<p class="muted">Pick an account.</p>`;
}

function selectAccount(id) {
  state.selected = id;
  document.querySelectorAll("#account-list [data-id]").forEach((b) => b.classList.toggle("active", Number(b.dataset.id) === id));
  renderAccountDetail();
}

async function renderAccountDetail() {
  const a = state.accounts.find((x) => x.id === state.selected);
  const box = $("#account-detail");
  const today = new Date().toISOString().slice(0, 10);
  box.innerHTML = `
    <div class="upload-head"><h2>${esc(a.name)}</h2>
      <span class="muted small">${esc(a.institution)} · ${esc(KIND_LABELS[a.kind])} · ${esc(a.currency)} · ${esc(ownerName(a.owner_id))}</span></div>
    <form id="edit-account" class="form-grid">
      <label>Name <input name="name" value="${esc(a.name)}" required></label>
      <label>Bank <input name="institution" value="${esc(a.institution)}" list="bank-options" required></label>
      <label>Currency <input name="currency" value="${esc(a.currency)}" pattern="[A-Z]{3}" maxlength="3" required></label>
      <label>Belongs to <select name="owner_id">${ownerOptions(a.owner_id)}</select></label>
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
      name: f.name.value, institution: f.institution.value, currency: f.currency.value.toUpperCase(), valuation: f.valuation.value,
      owner_id: ownerValue(f.owner_id.value) } });
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
  if (a.kind === "pillar3a") renderContributions(a, today);
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

async function renderContributions(a, today) {
  const list = await api(`/api/accounts/${a.id}/contributions`);
  let box = $("#contributions");
  if (!box) {
    $("#detail-body").insertAdjacentHTML("afterend", `<div id="contributions" style="margin-top:20px"></div>`);
    box = $("#contributions");
  }
  box.innerHTML = `<h2>Payments into this 3a account</h2>
    <p class="muted small">Used for the 3a reminders (how much is left this year). A statement upload can fill in the year's total.</p>
    <form id="contribution-form" class="row">
      <label>Date <input type="date" name="day" value="${today}" required></label>
      <label>Amount (CHF) <input type="number" step="0.01" min="0.01" name="amount" required></label>
      <button class="primary">Add</button>
    </form>
    ${list.length ? `<div class="table-wrap"><table><tbody>${list.map((c) => `<tr><td>${esc(shortDate(c.day))}</td>
      <td class="num">${esc(money(c.amount, "CHF", 2))}</td>
      <td class="muted small">${c.source === "upload" ? "year total from statement" : "payment"}${c.note ? " · " + esc(c.note) : ""}</td>
      <td class="num"><button class="link danger" data-del-contribution="${c.id}">Delete</button></td></tr>`).join("")}</tbody></table></div>` : ""}`;
  $("#contribution-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await api(`/api/accounts/${a.id}/contributions`, { method: "POST", body: { day: e.target.day.value, amount: Number(e.target.amount.value) } });
    toast("Payment added");
    renderContributions(a, today);
    updateTasksBadge();
  });
  box.querySelectorAll("[data-del-contribution]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/contributions/${b.dataset.delContribution}`, { method: "DELETE" });
    renderContributions(a, today);
  }));
}

function holdingRow(h = {}) {
  return `<tr>
    <td><input name="name" value="${esc(h.name)}" placeholder="Vanguard FTSE All-World" required></td>
    <td><input name="symbol" value="${esc(h.symbol)}" placeholder="optional" list="symbol-options"></td>
    <td><input name="quantity" type="number" step="any" value="${h.quantity ?? ""}" placeholder="unknown"></td>
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
      For cash in the depot leave the ticker empty: quantity = amount, price = 1.
      Quantity unknown: leave it empty and put the position's total value under Price; it then stays at that value until you change it.</p>
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
      return { name: v("name"), symbol: v("symbol") || null, quantity: v("quantity") === "" ? null : Number(v("quantity")),
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
    currency: f.currency.value.toUpperCase(), valuation: f.valuation.value, owner_id: ownerValue(f.owner_id.value) } });
  f.reset();
  f.owner_id.innerHTML = ownerOptions(state.me.id);
  state.selected = created.id;
  loadAccounts();
});

$("#setup-defaults").addEventListener("click", async (e) => {
  e.target.disabled = true;
  for (const a of DEFAULT_ACCOUNTS) await api("/api/accounts", { method: "POST", body: { ...a, owner_id: state.me.id } });
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
  state.accounts = await api("/api/accounts");
  setBankOptions();
  const uploads = await updateInboxBadge();
  state.uploads = uploads;
  const box = $("#uploads");
  box.innerHTML = uploads.length ? uploads.map(uploadCard).join("") : `<div class="card empty">Nothing waiting for review.</div>`;
  uploads.forEach(wireUpload);
  clearTimeout(state.pollTimer);
  if (uploads.some((u) => u.status === "reading") && !$("#tab-inbox").hidden) state.pollTimer = setTimeout(loadUploads, 2500);
}

function accountOptions(selectedId) {
  return groupByBank(state.accounts).map(([bank, list]) => `<optgroup label="${esc(bank)}">${list.map((a) =>
    `<option value="${a.id}" ${a.id === selectedId ? "selected" : ""}>${esc(a.name)} · ${esc(ownerName(a.owner_id))} (${esc(a.currency)})</option>`).join("")}</optgroup>`).join("");
}

function foundBlock(f, i, bank) {
  const positions = f.positions || [];
  // Unmatched accounts default to "new" when the bank has no account yet that could be meant.
  const target = f.account_id ?? "new";
  return `<div class="found" data-found="${i}">
    <label class="check found-head"><input type="checkbox" name="include" checked>
      <strong>${esc(f.name || "Account")}</strong>
      <span class="muted small">${esc(KIND_LABELS[f.kind] || "")}</span></label>
    <div class="row">
      <label>Book to <select name="target">
        <option value="new" ${target === "new" ? "selected" : ""}>+ New account at ${esc(bank || "this bank")}</option>
        ${accountOptions(f.account_id)}</select></label>
      <label data-new-only>Name of new account <input name="new_name" value="${esc(f.name)}" maxlength="80"></label>
      <label>Balance${f.currency ? ` (${esc(f.currency)})` : ""} <input type="number" step="0.01" name="balance" value="${f.balance ?? ""}"></label>
      ${f.contributions_ytd != null ? `<label>Paid in this year (CHF) <input type="number" step="0.01" name="contributions_ytd" value="${f.contributions_ytd}"></label>` : ""}
      ${f.pillar2_buyin != null ? `<label>Buy-in potential (CHF) <input type="number" step="1" name="pillar2_buyin" value="${f.pillar2_buyin}"></label>` : ""}
    </div>
    ${positions.length ? `<div class="positions-review">
      <div class="table-wrap"><table class="review">
      <thead><tr><th></th><th>Position</th><th>Ticker</th><th class="num">Quantity</th><th class="num hide-sm">Price</th><th class="num">Value</th>
        <th data-updates-col>Updates</th></tr></thead>
      <tbody>${positions.map(positionRow).join("")}</tbody></table></div>
      <p class="muted small">Check the quantity and ticker; you can change both. A ticker marked ✓ matches the price in the file,
        so the position follows the market. Without a ticker the position keeps the value from the file.</p>
      <label class="check" data-holdings-only><input type="checkbox" name="replace_holdings" checked>
        Update this account's positions with these</label>
      <label class="check" data-holdings-only><input type="checkbox" name="remove_missing">
        Also remove positions that aren't in this file (only for a complete statement)</label></div>` : ""}
  </div>`;
}

function positionRow(p, j) {
  const m = p.match;
  const status = m ? `<span class="ok" title="Market price ${esc(money(m.price, m.currency, 2))}">✓</span>`
    : `<span class="muted" title="No ticker with a matching market price found">?</span>`;
  return `<tr data-pos="${j}">
    <td><input type="checkbox" name="pos_include" checked aria-label="Include ${esc(p.name)}"></td>
    <td><input name="pos_name" value="${esc(p.name)}" maxlength="120"></td>
    <td class="ticker-cell"><input name="pos_symbol" value="${esc(p.symbol || "")}" placeholder="none" maxlength="30">${status}</td>
    <td class="num"><input name="pos_quantity" type="number" step="any" value="${p.quantity ?? ""}" placeholder="unknown"></td>
    <td class="num hide-sm">${p.price != null ? esc(money(p.price, p.currency, 2)) : "–"}</td>
    <td class="num">${esc(money(p.value, p.currency, 2))}</td>
    <td data-updates-col><select name="pos_replaces" aria-label="Existing position this updates"></select></td></tr>`;
}

// Same rules as the server (match.same_name): a shortened or slightly different name is the same position.
const normName = (n) => (n || "").toLowerCase().replace(/[^a-z0-9]/g, "");
function bigrams(t) { const b = new Set(); for (let i = 0; i < t.length - 1; i++) b.add(t.slice(i, i + 2)); return b; }
function sameName(a, b) {
  const truncated = [a, b].some((n) => /(\.\.\.|…)\s*$/.test(n || ""));
  const x = normName(a), y = normName(b);
  if (!x || !y) return false;
  if (x === y) return true;
  const [short, long] = x.length <= y.length ? [x, y] : [y, x];
  if (truncated && short.length >= 5 && long.startsWith(short)) return true;
  const bx = bigrams(x), by = bigrams(y);
  const common = [...bx].filter((g) => by.has(g)).length;
  return bx.size > 0 && by.size > 0 && (2 * common) / (bx.size + by.size) >= 0.85;
}

function samePosition(h, p, symbol) {
  return (symbol && h.symbol === symbol.toUpperCase()) || (p.isin && h.isin === p.isin) || sameName(h.name, p.name);
}

// Fill the "Updates" choice of each detected position with the target account's current positions.
async function fillReplaces(block, f, accountId) {
  const col = block.querySelectorAll("[data-updates-col]");
  if (!accountId) { col.forEach((c) => (c.hidden = true)); return; }
  const existing = await api(`/api/accounts/${accountId}/holdings`);
  col.forEach((c) => (c.hidden = !existing.length));
  const taken = new Set();
  block.querySelectorAll("[data-pos]").forEach((tr) => {
    const p = f.positions[Number(tr.dataset.pos)];
    const symbol = $("[name=pos_symbol]", tr).value.trim();
    const guess = existing.find((h) => !taken.has(h.id) && samePosition(h, p, symbol));
    if (guess) taken.add(guess.id);
    $("[name=pos_replaces]", tr).innerHTML = `<option value="">New position</option>` + existing.map((h) =>
      `<option value="${h.id}" ${guess?.id === h.id ? "selected" : ""}>${esc(h.name)}${h.symbol ? " · " + esc(h.symbol) : ""}</option>`).join("");
  });
}

function reviewedPositions(block, f) {
  return [...block.querySelectorAll("[data-pos]")].filter((tr) => $("[name=pos_include]", tr).checked).map((tr) => {
    const p = f.positions[Number(tr.dataset.pos)];
    const q = $("[name=pos_quantity]", tr).value;
    return { name: $("[name=pos_name]", tr).value.trim() || p.name, symbol: $("[name=pos_symbol]", tr).value.trim() || null,
      isin: p.isin || null, quantity: q === "" ? null : Number(q), value: p.value ?? null, currency: p.currency || null,
      replaces_id: Number($("[name=pos_replaces]", tr).value) || null };
  });
}

function uploadCard(u) {
  const head = `<div class="upload-head"><h2>${esc(u.filename)}</h2>
    <span class="small">${u.uploaded_by ? `<span class="muted">from ${esc(u.uploaded_by)} · </span>` : ""}<a href="/api/uploads/${u.id}/file" target="_blank" rel="noopener">View file</a></span></div>`;
  if (u.status === "reading") return `<div class="card">${head}<p class="muted">Reading…</p></div>`;
  if (u.status === "failed") return `<div class="card" data-upload="${u.id}">${head}<p class="error">${esc(u.error)}</p>
    <div class="actions"><button class="ghost" data-act="retry">Try again</button><button class="link" data-act="dismiss">Dismiss</button></div></div>`;
  const x = u.extracted || {};
  const found = x.accounts || [];
  return `<div class="card" data-upload="${u.id}">${head}
    ${x.notes ? `<div class="notes">${esc(x.notes)}</div>` : ""}
    <div class="row">
      <label>Bank <input name="institution" value="${esc(x.institution || "")}" list="bank-options" maxlength="80"></label>
      <label>Date <input type="date" name="as_of" value="${esc(x.as_of || new Date().toISOString().slice(0, 10))}"></label>
      <label>New accounts belong to <select name="owner_id">${ownerOptions(u.user_id ?? state.me.id)}</select></label>
    </div>
    ${found.length ? found.map((f, i) => foundBlock(f, i, x.institution)).join("") : `<p class="muted">No account values found in this file.</p>`}
    <div class="actions">${found.length ? `<button class="primary" data-act="apply">Save ${found.length > 1 ? "selected" : "to account"}</button>` : ""}
      <button class="link" data-act="dismiss">Dismiss</button></div>
  </div>`;
}

function wireUpload(u) {
  const card = document.querySelector(`[data-upload="${u.id}"]`);
  if (!card) return;
  const found = u.extracted?.accounts || [];
  card.querySelectorAll("[data-found]").forEach((block) => {
    const select = $("[name=target]", block), newOnly = $("[data-new-only]", block);
    const include = $("[name=include]", block);
    const sync = () => {
      const isNew = select.value === "new";
      newOnly.hidden = !isNew;
      // A new account gets its positions anyway; an existing one only if it is valued by positions.
      const account = state.accounts.find((a) => a.id === Number(select.value));
      const holdings = !isNew && account?.valuation === "holdings";
      block.querySelectorAll("[data-holdings-only]").forEach((l) => (l.hidden = !holdings));
      const review = $(".positions-review", block);
      if (review) review.hidden = !isNew && !holdings;
      if (review && select.value !== block.dataset.filledFor) {
        block.dataset.filledFor = select.value;
        fillReplaces(block, found[Number(block.dataset.found)], holdings ? Number(select.value) : null).catch((err) => toast(err.message));
      }
      block.classList.toggle("off", !include.checked);
    };
    select.addEventListener("change", sync);
    include.addEventListener("change", sync);
    sync();
  });
  card.addEventListener("click", async (e) => {
    const act = e.target.dataset.act;
    if (!act) return;
    e.target.disabled = true;
    try {
      if (act === "dismiss") await api(`/api/uploads/${u.id}/dismiss`, { method: "POST" });
      if (act === "retry") await api(`/api/uploads/${u.id}/retry`, { method: "POST" });
      if (act === "apply") {
        const bank = $("[name=institution]", card).value.trim();
        const items = [];
        for (const block of card.querySelectorAll("[data-found]")) {
          if (!$("[name=include]", block).checked) continue;
          const f = found[Number(block.dataset.found)];
          const bal = $("[name=balance]", block).value;
          const item = { balance: bal === "" ? null : Number(bal), positions: reviewedPositions(block, f), replace_holdings: false };
          for (const field of ["contributions_ytd", "pillar2_buyin"]) {
            const v = $(`[name=${field}]`, block)?.value;
            if (v) item[field] = Number(v);
          }
          if ($("[name=target]", block).value === "new") {
            if (!bank) throw new Error("Enter the bank for the new account");
            const hasQuantities = item.positions.length > 0;
            item.new_account = { name: $("[name=new_name]", block).value.trim() || f.name || bank, institution: bank,
              kind: f.kind || "cash", currency: (f.currency || "CHF").toUpperCase(), valuation: hasQuantities ? "holdings" : "balance",
              owner_id: ownerValue($("[name=owner_id]", card).value) };
            item.replace_holdings = hasQuantities;
          } else {
            item.account_id = Number($("[name=target]", block).value);
            item.replace_holdings = $("[name=replace_holdings]", block)?.checked || false;
            item.remove_missing = $("[name=remove_missing]", block)?.checked || false;
            item.positions_reviewed = true;
          }
          if (!item.replace_holdings) item.positions = [];
          items.push(item);
        }
        if (!items.length) throw new Error("Select at least one account");
        const r = await api(`/api/uploads/${u.id}/apply`, { method: "POST", body: { as_of: $("[name=as_of]", card).value, items } });
        toast([`Saved ${items.length} account${items.length > 1 ? "s" : ""}`, ...r.notes, ...r.errors].join(". "));
        state.accounts = await api("/api/accounts");
        updateTasksBadge();
      }
      loadUploads();
    } catch (err) {
      toast(err.message);
      e.target.disabled = false;
    }
  });
}

// --- insights: tasks, pillar 2/3a, weekly briefing ------------------------------------

const CATEGORY_LABELS = { pillar3a: "Pillar 3a", pillar2: "Pillar 2", portfolio: "Portfolio" };
const STANCE_LABELS = { buy: "Buy", hold: "Hold", reduce: "Reduce", watch: "Watch" };

async function updateTasksBadge() {
  const t = await api("/api/tasks");
  const badge = $("#tasks-count");
  badge.textContent = t.open.length;
  badge.hidden = !t.open.length;
  return t;
}

async function loadInsights() {
  const [tasks] = await Promise.all([updateTasksBadge(), loadPension(), loadBriefing()]);
  renderTasks(tasks);
}

function renderTasks(t) {
  const item = (task) => `<li class="task" data-task="${task.id}">
      <div class="task-head"><span class="chip chip-${esc(task.category)}">${esc(CATEGORY_LABELS[task.category] || task.category)}</span>
        <strong>${esc(task.title)}</strong></div>
      <p class="small">${esc(task.body)}</p>
      ${task.status === "snoozed"
        ? `<div class="actions small"><span class="muted">Reminds you on ${esc(shortDate(task.snooze_until))}</span>
            <button class="link" data-task-act="open">Show now</button></div>`
        : `<div class="actions"><button class="ghost" data-task-act="done">Done</button>
            <label class="snooze">Remind me
              <select data-task-snooze aria-label="Remind me later">
                <option value="">…</option><option value="7">in 1 week</option><option value="30">in 1 month</option>
                <option value="91">in 3 months</option><option value="182">in 6 months</option></select></label>
            <button class="link" data-task-act="dismissed">Not relevant</button></div>`}
    </li>`;
  $("#tasks").innerHTML = (t.open.length ? `<ul class="tasks">${t.open.map(item).join("")}</ul>` : `<div class="empty">Nothing to do right now.</div>`)
    + (t.snoozed.length ? `<details class="snoozed"><summary>${t.snoozed.length} snoozed</summary><ul class="tasks">${t.snoozed.map(item).join("")}</ul></details>` : "");
}

async function taskAction(id, body) {
  await api(`/api/tasks/${id}`, { method: "POST", body });
  renderTasks(await updateTasksBadge());
}

$("#tasks").addEventListener("click", (e) => {
  const b = e.target.closest("[data-task-act]");
  if (b) taskAction(b.closest("[data-task]").dataset.task, { action: b.dataset.taskAct }).catch((err) => toast(err.message));
});
$("#tasks").addEventListener("change", (e) => {
  const sel = e.target.closest("[data-task-snooze]");
  if (sel?.value) taskAction(sel.closest("[data-task]").dataset.task, { action: "snooze", days: Number(sel.value) })
    .then(() => toast("Snoozed")).catch((err) => toast(err.message));
});

async function loadPension() {
  const people = await api("/api/pension");
  const rows = people.filter((p) => p.has_3a || p.has_pillar2);
  $("#pension").innerHTML = rows.length ? rows.map((p) => {
    const pct3a = p.max_3a ? Math.min(p.paid_3a / p.max_3a, 1) : null;
    return `<div class="pension-row">
      <div class="pension-name"><strong>${esc(p.name)}</strong>
        <span class="muted small">${p.birth_year ? `age ${p.year - p.birth_year}` : "age unknown"}
          · ${p.has_pension_fund === 1 ? "employed with pension fund" : p.has_pension_fund === 0 ? "no pension fund" : "employment unknown"}</span></div>
      ${p.has_3a ? `<div><div class="muted small">Pillar 3a ${p.year}</div>
        ${pct3a != null ? `<div class="meter" role="img" aria-label="${Math.round(pct3a * 100)}% of the maximum paid"><span style="width:${pct3a * 100}%"></span></div>
          <div class="small">${esc(money(p.paid_3a, "CHF"))} of ${esc(money(p.max_3a, "CHF"))} · <strong>${esc(money(p.room_3a, "CHF"))} left</strong></div>`
          : `<div class="small">${esc(money(p.paid_3a, "CHF"))} paid · limit depends on income</div>`}</div>` : ""}
      ${p.has_pillar2 ? `<div><div class="muted small">Pillar 2 buy-in potential</div>
        <div class="small">${p.pillar2_buyin != null ? `<strong>${esc(money(p.pillar2_buyin, "CHF"))}</strong>${p.pillar2_buyin_as_of ? ` <span class="muted">as of ${esc(shortDate(p.pillar2_buyin_as_of))}</span>` : ""}`
          : `<span class="muted">unknown, upload the pension certificate</span>`}</div></div>` : ""}
    </div>`;
  }).join("") : `<div class="empty">No pillar 2 or 3a accounts yet.</div>`;
}

let briefingPoll;
async function loadBriefing() {
  const b = await api("/api/briefing");
  const box = $("#briefing");
  $("#write-briefing").disabled = b.running;
  $("#write-briefing").textContent = b.running ? "Writing…" : "Write now";
  clearTimeout(briefingPoll);
  if (b.running && !$("#tab-insights").hidden) briefingPoll = setTimeout(loadBriefing, 8000);
  const status = b.running ? `<p class="muted small">Claude is researching this week's news and writing the briefing; this takes a few minutes.</p>`
    : b.last_error ? `<p class="error small">The last attempt failed: ${esc(b.last_error)}</p>` : "";
  if (!b.briefing) {
    box.innerHTML = status || `<div class="empty">No briefing yet. The first one is written on Monday, or click <em>Write now</em>.</div>`;
    return;
  }
  const x = b.briefing;
  const notes = (list) => `<ul class="notes-list">${list.map((n) => `<li><strong>${esc(n.title)}.</strong> ${esc(n.detail)}</li>`).join("")}</ul>`;
  box.innerHTML = `${status}
    <div class="muted small">Week ${esc(b.week.split("-W")[1])} · written ${esc(shortDate(b.created_at.slice(0, 10)))}</div>
    <h3 class="headline">${esc(x.headline)}</h3>
    <p>${esc(x.summary)}</p>
    <h4>Market</h4>${notes(x.market)}
    <h4>Your positions</h4>
    <div class="table-wrap"><table class="stances"><tbody>${x.positions.map((p) => `<tr>
      <td><span class="stance stance-${esc(p.stance)}">${esc(STANCE_LABELS[p.stance])}</span></td>
      <td><strong>${esc(p.name)}</strong>${p.symbol ? ` <span class="muted small">${esc(p.symbol)}</span>` : ""}
        <div class="small">${esc(p.reasoning)}</div></td></tr>`).join("")}</tbody></table></div>
    <h4>To consider</h4>${notes(x.considerations)}
    ${b.sources?.length ? `<details class="small sources"><summary>${b.sources.length} sources</summary><ul>${b.sources.filter((s) => /^https?:\/\//.test(s.url)).map((s) =>
      `<li><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a></li>`).join("")}</ul></details>` : ""}`;
}

$("#write-briefing").addEventListener("click", async () => {
  try {
    await api("/api/briefing", { method: "POST" });
    toast("Writing the briefing; this takes a few minutes");
    loadBriefing();
  } catch (err) {
    toast(err.message);
  }
});

// --- family -------------------------------------------------------------------------

async function loadFamily() {
  await loadMe();
  const profiles = Object.fromEntries((await api("/api/pension")).map((p) => [p.user_id, p]));
  $("#family-list").innerHTML = state.family.map((u) => {
    const p = profiles[u.id] || {};
    return `<li><strong>${esc(u.name)}</strong> <span class="muted small">${esc(u.username)}${u.id === state.me.id ? " · you" : ""}
      ${u.has_upload_token ? " · Shortcut set up" : ""}</span>
      <form class="form-grid profile-form" data-user="${u.id}">
        <label>Year of birth <input name="birth_year" type="number" min="1920" max="2020" value="${p.birth_year ?? ""}"></label>
        <label>Employment <select name="has_pension_fund">
          <option value="" ${p.has_pension_fund == null ? "selected" : ""}>Unknown</option>
          <option value="1" ${p.has_pension_fund === 1 ? "selected" : ""}>Employed, with pension fund</option>
          <option value="0" ${p.has_pension_fund === 0 ? "selected" : ""}>No pension fund (e.g. self-employed)</option></select></label>
        <label>Pillar 2 buy-in potential (CHF) <input name="pillar2_buyin" type="number" step="1" min="0" value="${p.pillar2_buyin ?? ""}"
          placeholder="from the pension certificate"></label>
        <button class="ghost">Save</button>
      </form></li>`;
  }).join("");
  $("#upload-url").textContent = `${location.origin}/api/upload`;
  loadUsage();
  $("#new-upload-token").textContent = state.me.has_upload_token ? "Replace my upload token" : "Create my upload token";
}

const TASK_LABELS = { extract: "Reading uploads", extract_retry: "Reading again (stronger model)",
  research: "Briefing: web research", briefing: "Briefing: writing" };

async function loadUsage() {
  const u = await api("/api/usage");
  const usd = (v) => `$${(v || 0).toFixed(2)}`;
  const month = new Date(u.month + "-01T00:00:00").toLocaleDateString("en-GB", { month: "long", year: "numeric" });
  $("#usage").innerHTML = `<p class="small">${esc(month)}: <strong>${usd(u.total_usd)}</strong>
      <span class="muted">· each task uses the cheapest model that does it well</span></p>
    ${u.by_task.length ? `<div class="table-wrap"><table><thead><tr><th>Task</th><th class="hide-sm">Model</th><th class="num">Calls</th>
      <th class="num hide-sm">Searches</th><th class="num">Cost</th></tr></thead><tbody>${u.by_task.map((r) => `<tr>
      <td>${esc(TASK_LABELS[r.task] || r.task)}</td><td class="hide-sm muted small">${esc(r.model.replace("claude-", ""))}</td>
      <td class="num">${r.calls}</td><td class="num hide-sm">${r.web_searches || ""}</td><td class="num">${usd(r.cost_usd)}</td></tr>`).join("")}
      </tbody></table></div>` : ""}`;
}

$("#family-list").addEventListener("submit", async (e) => {
  const f = e.target.closest(".profile-form");
  if (!f) return;
  e.preventDefault();
  const num = (v) => (v === "" ? null : Number(v));
  const body = { birth_year: num(f.birth_year.value), pillar2_buyin: num(f.pillar2_buyin.value),
    has_pension_fund: f.has_pension_fund.value === "" ? null : f.has_pension_fund.value === "1" };
  if (body.pillar2_buyin != null) body.pillar2_buyin_as_of = new Date().toISOString().slice(0, 10);
  try {
    await api(`/api/users/${f.dataset.user}`, { method: "PATCH", body });
    toast("Saved");
    updateTasksBadge();
  } catch (err) {
    toast(err.message);
  }
});

$("#member-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("/api/users", { method: "POST", body: { name: f.name.value, username: f.username.value, password: f.password.value } });
    toast(`${f.name.value} can sign in now`);
    f.reset();
    loadFamily();
  } catch (err) {
    toast(err.message);
  }
});

$("#new-upload-token").addEventListener("click", async () => {
  if (state.me.has_upload_token && !confirm("Your current Shortcut stops working until you put the new token in. Continue?")) return;
  const r = await api("/api/me/upload-token", { method: "POST" });
  const out = $("#upload-token-out");
  out.hidden = false;
  out.innerHTML = `<div class="muted">Your token, shown only now:</div>
    <div class="copy-row"><code class="token" id="upload-token">${esc(r.token)}</code><button class="link" data-copy="#upload-token">Copy</button></div>`;
  loadFamily();
});

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b) return;
  try {
    await navigator.clipboard.writeText($(b.dataset.copy).textContent.trim());
    toast("Copied");
  } catch {
    toast("Copy failed; select the text and copy it");
  }
});

$("#password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/me/password", { method: "POST", body: { current: e.target.current.value, new: e.target.new.value } });
    e.target.reset();
    toast("Password changed; other devices are signed out");
  } catch (err) {
    toast(err.message);
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showLogin();
});

// --- start -------------------------------------------------------------------------

(async () => {
  const status = await api("/api/status");
  if (status.needs_setup) {
    $("#setup-token-field").hidden = !status.setup_token_required;
    $("#setup-form").setup_token.required = status.setup_token_required;
    showScreen("#setup-first");
    return;
  }
  try {
    await enterApp();
  } catch {
    showLogin();
  }
})();
