// ui.js: DOM, fetch and formatting helpers shared by every page (spec v2.2 §5, W4).
// The DOM is built with createElement / textContent only: no HTML strings anywhere, so nothing that comes
// from data can turn into markup. Every request goes through api(): GET only, same origin, /api/ only.

const SVG_NS = "http://www.w3.org/2000/svg";
export const MINUS = "−";
export const DASH = "—";

function put(e, attrs, svg) {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") svg ? e.setAttribute("class", v) : (e.className = v);
    else if (k === "text") e.textContent = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (k === "style") throw new Error("set styles with the CSSOM (CSP)");
    else e.setAttribute(k, v === true ? "" : String(v));
  }
}

function add(e, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    e.appendChild(typeof k === "string" || typeof k === "number" ? document.createTextNode(String(k)) : k);
  }
}

export function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  put(e, attrs, false);
  add(e, kids);
  return e;
}

export function svg(tag, attrs, ...kids) {
  const e = document.createElementNS(SVG_NS, tag);
  put(e, attrs, true);
  add(e, kids);
  return e;
}

// append kids, skipping null / false (Element.append would turn null into the text "null")
export function fill(parent, ...kids) {
  add(parent, kids);
  return parent;
}

export function clear(e) {
  while (e.firstChild) e.removeChild(e.firstChild);
  return e;
}

// ---------------------------------------------------------------------------------------------------
// the only network call in the app

export async function api(path, params) {
  if (!/^\/api\/[a-z]+$/.test(path)) throw new Error("not an API path");
  const u = new URL(path, window.location.origin);
  for (const [k, v] of Object.entries(params || {})) if (v != null && v !== "") u.searchParams.set(k, String(v));
  const r = await fetch(u.href, { method: "GET", credentials: "same-origin", headers: { Accept: "application/json" } });
  let body = null;
  try { body = await r.json(); } catch { body = null; }
  if (!r.ok) throw new Error((body && body.error) || `HTTP ${r.status}`);
  return body;
}

// ---------------------------------------------------------------------------------------------------
// numbers and dates (text always carries its sign; a minus is a real minus)

const ok = (x) => x != null && Number.isFinite(x);
const loc = (x, nd) => Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: nd, maximumFractionDigits: nd });

export function num(x, nd = 2, sign = false) {
  if (!ok(x)) return DASH;
  const s = x < 0 ? MINUS : sign && x > 0 ? "+" : "";
  return s + loc(x, nd);
}

export function money(x, nd = 0, sign = false) {
  if (!ok(x)) return DASH;
  const s = x < 0 ? MINUS : sign && x > 0 ? "+" : "";
  return `${s}$${loc(x, nd)}`;
}

// compact money for heatmap cells: $940, $1.2k, $12k
export function moneyK(x, sign = true) {
  if (!ok(x)) return DASH;
  const a = Math.abs(x), s = x < 0 ? MINUS : sign && x > 0 ? "+" : "";
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
  if (a >= 1e4) return `${s}$${(a / 1e3).toFixed(0)}k`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(1)}k`;
  return `${s}$${a.toFixed(0)}`;
}

// x is a fraction (0.12 = 12 %)
export function pct(x, nd = 0, sign = false) {
  if (!ok(x)) return DASH;
  const s = x < 0 ? MINUS : sign && x > 0 ? "+" : "";
  return `${s}${loc(x * 100, nd)}%`;
}

// x is already in percent (12 = 12 %)
export const pctPts = (x, nd = 1, sign = true) => (ok(x) ? pct(x / 100, nd, sign) : DASH);

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function fmtDate(iso, year = false) {
  if (!iso) return DASH;
  const [y, m, d] = String(iso).slice(0, 10).split("-").map(Number);
  return `${MON[m - 1]} ${d}${year ? `, ${y}` : ""}`;
}

export function daysBetween(a, b) {
  return Math.round((Date.parse(b) - Date.parse(a)) / 86400000);
}

export function compact(x) {
  if (!ok(x)) return DASH;
  const a = Math.abs(x);
  if (a >= 1e12) return `${(x / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${(x / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${(x / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${(x / 1e3).toFixed(1)}k`;
  return String(Math.round(x));
}

// ---------------------------------------------------------------------------------------------------
// tooltip (one per page; positioned with the CSSOM, which the CSP allows)

export function showTip(rows, x, y) {
  const t = document.getElementById("tip");
  if (!t) return;
  clear(t);
  for (const r of rows) {
    if (typeof r === "string") t.appendChild(el("div", { class: "tip-h", text: r }));
    else t.appendChild(el("div", { class: "tip-r" }, r[2] ? el("i", { class: `key ${r[2]}` }) : null,
      el("span", { text: r[0] }), el("b", { text: r[1] })));
  }
  t.hidden = false;
  const w = t.offsetWidth, h = t.offsetHeight;
  const left = x + 16 + w > window.innerWidth ? x - w - 12 : x + 16;
  const top = y + 16 + h > window.innerHeight ? y - h - 12 : y + 16;
  t.style.left = `${Math.max(4, left)}px`;
  t.style.top = `${Math.max(4, top)}px`;
}

export function hideTip() {
  const t = document.getElementById("tip");
  if (t) t.hidden = true;
}

// tooltip anchored to an element (keyboard focus uses the same tooltip as hover)
export function tipAt(elm, rows) {
  const b = elm.getBoundingClientRect();
  showTip(rows, b.right, b.top);
}

// ---------------------------------------------------------------------------------------------------
// small building blocks

export function card(title, ...kids) {
  return el("section", { class: "card" }, title ? el("h3", { class: "card-h", text: title }) : null, ...kids);
}

export function kv(label, value, hint) {
  return el("div", { class: "kv" }, el("span", { class: "k", text: label }), el("span", { class: "v", text: value }),
    hint ? el("span", { class: "h", text: hint }) : null);
}

export function badge(text, kind = "") {
  return el("span", { class: `badge ${kind}`, text });
}

// a status line: always an icon and a label, never colour alone
export function status(level, text) {
  const icon = { high: "▲", warn: "▲", info: "●", ok: "✔", crit: "✖" }[level] || "●";
  return el("div", { class: `status ${level}` }, el("span", { class: "icon", "aria-hidden": "true", text: icon }),
    el("span", { class: "sr", text: `${level}: ` }), el("span", { text }));
}

export function loading(text = "Loading…") {
  return el("div", { class: "loading", role: "status" }, el("span", { class: "spinner", "aria-hidden": "true" }), text);
}

export function errorBox(err) {
  return el("div", { class: "card error-card", role: "alert" }, status("crit", String(err && err.message ? err.message : err)));
}

export function link(href, kids, attrs = {}) {
  return el("a", { href, "data-link": "1", ...attrs }, kids);
}

export function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// rAF-coalesced callback (slider drags re-render at most once a frame)
export function perFrame(fn) {
  let queued = false;
  return () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; fn(); });
  };
}
