// builder.js: the Strategy Builder (spec v2.2 §4.5). Any ticker, any legs: calls, puts, shares, spreads,
// calendars, diagonals, condors, butterflies. Templates pick strikes by delta from the live (or fixture) chain;
// every leg stays editable. The visualizer below redraws on every change. Nothing here talks to a broker.

import { el, svg, clear, api, num, money, pct, fmtDate, DASH, card, badge, status, loading, errorBox, link, showTip,
         hideTip, debounce, fill } from "./ui.js";
import { frame, lin, niceStep, vlabels } from "./charts.js";
import * as E from "./engine.js";
import { mountViz } from "./viz.js";

const SYM = /^[A-Z][A-Z0-9.\-]{0,9}$/;

// ---------------------------------------------------------------------------------------------------
// templates: f(ctx) -> legs [{side, type, strike, expiry, qty}] ; ctx gives strike pickers on the chosen expiry

const T = (key, label, group, build, ur = false) => ({ key, label, group, build, ur });
const TEMPLATES = [
  T("long_call", "Long call", "Basic", (c) => [c.leg(1, "C", c.atm())]),
  T("long_put", "Long put", "Basic", (c) => [c.leg(1, "P", c.atm())]),
  T("short_call", "Short call", "Basic", (c) => [c.leg(-1, "C", c.byDelta("C", 0.3))], true),
  T("csp", "Cash-secured put", "Income", (c) => [c.leg(-1, "P", c.byDelta("P", 0.25))]),
  T("covered_call", "Covered call", "Income", (c) => [c.shares(1, 100), c.leg(-1, "C", c.byDelta("C", 0.3))]),
  T("collar", "Collar", "Income", (c) => [c.shares(1, 100), c.leg(-1, "C", c.byDelta("C", 0.25)), c.leg(1, "P", c.byDelta("P", 0.25))]),
  T("bull_call", "Bull call spread", "Verticals", (c) => [c.leg(1, "C", c.atm()), c.leg(-1, "C", c.byDelta("C", 0.3, c.atm()))]),
  T("bear_put", "Bear put spread", "Verticals", (c) => [c.leg(1, "P", c.atm()), c.leg(-1, "P", c.byDelta("P", 0.3, null, c.atm()))]),
  T("bull_put", "Bull put spread", "Verticals", (c) => { const k = c.byDelta("P", 0.3); return [c.leg(-1, "P", k), c.leg(1, "P", c.below(k, c.byDelta("P", 0.15)))]; }),
  T("bear_call", "Bear call spread", "Verticals", (c) => { const k = c.byDelta("C", 0.3); return [c.leg(-1, "C", k), c.leg(1, "C", c.above(k, c.byDelta("C", 0.15)))]; }),
  T("long_straddle", "Long straddle", "Volatility", (c) => [c.leg(1, "C", c.atm()), c.leg(1, "P", c.atm())]),
  T("long_strangle", "Long strangle", "Volatility", (c) => [c.leg(1, "C", c.byDelta("C", 0.25)), c.leg(1, "P", c.byDelta("P", 0.25))]),
  T("short_straddle", "Short straddle", "Volatility", (c) => [c.leg(-1, "C", c.atm()), c.leg(-1, "P", c.atm())], true),
  T("short_strangle", "Short strangle", "Volatility", (c) => [c.leg(-1, "C", c.byDelta("C", 0.16)), c.leg(-1, "P", c.byDelta("P", 0.16))], true),
  T("iron_condor", "Iron condor", "Neutral", (c) => {
    const sc = c.byDelta("C", 0.2), sp = c.byDelta("P", 0.2);
    return [c.leg(1, "P", c.below(sp, c.byDelta("P", 0.1))), c.leg(-1, "P", sp), c.leg(-1, "C", sc), c.leg(1, "C", c.above(sc, c.byDelta("C", 0.1)))];
  }),
  T("iron_fly", "Iron butterfly", "Neutral", (c) => {
    const k = c.atm(), w = c.width();
    return [c.leg(1, "P", c.near(k - w, -1)), c.leg(-1, "P", k), c.leg(-1, "C", k), c.leg(1, "C", c.near(k + w, 1))];
  }),
  T("call_fly", "Call butterfly", "Neutral", (c) => {
    const k = c.atm(), w = c.width();
    return [c.leg(1, "C", c.near(k - w, -1)), c.leg(-1, "C", k, 2), c.leg(1, "C", c.near(k + w, 1))];
  }),
  T("bwb", "Broken-wing butterfly (calls)", "Neutral", (c) => {
    // wings 1 : 2, the near one half the expected move: the wide (broken) wing carries the upside risk
    const k = c.atm(), w = c.width() / 2;
    return [c.leg(1, "C", c.near(k - w, -1)), c.leg(-1, "C", k, 2), c.leg(1, "C", c.near(k + 2 * w, 1))];
  }),
  T("call_calendar", "Call calendar", "Time", (c) => [c.leg(-1, "C", c.atm()), c.leg(1, "C", c.atm(), 1, c.back)]),
  T("put_calendar", "Put calendar", "Time", (c) => [c.leg(-1, "P", c.atm()), c.leg(1, "P", c.atm(), 1, c.back)]),
  T("call_diagonal", "Call diagonal", "Time", (c) => [c.leg(-1, "C", c.byDelta("C", 0.3)), c.leg(1, "C", c.atm(), 1, c.back)]),
  T("double_calendar", "Double calendar", "Time", (c) => {
    const kc = c.byDelta("C", 0.35), kp = c.byDelta("P", 0.35);
    return [c.leg(-1, "P", kp), c.leg(1, "P", kp, 1, c.back), c.leg(-1, "C", kc), c.leg(1, "C", kc, 1, c.back)];
  }),
];
const GROUPS = ["Basic", "Income", "Verticals", "Volatility", "Neutral", "Time"];

// ---------------------------------------------------------------------------------------------------

export function mountBuilder(root, { meta, symbol, legs: legsParam, alive }) {
  const maxLegs = meta.max_legs || 8;
  let sym = null, base = null, expiry = null, legs = [], vz = null, tplLabel = "Custom", gen = 0;
  const chains = new Map();
  const isAlive = () => (alive ? alive() : true);

  const wrap = el("div", { class: "builder" });
  const head = el("section", { class: "card bhead" });
  const body = el("div");
  fill(wrap, el("div", { class: "page-h" }, el("div", null, el("h1", { text: "Strategy Builder" }),
    el("p", { class: "muted", text: "Pick any ticker, set up calls, puts, spreads, calendars or condors, and see how the position behaves below. Read-only: nothing is sent to a broker." }))),
    head, body);
  root.appendChild(wrap);

  // ---- ticker picker -------------------------------------------------------------------------------
  const inp = el("input", { class: "sym", type: "text", placeholder: "Ticker", maxlength: 10, spellcheck: "false", autocomplete: "off", list: "symlist", "aria-label": "Ticker" });
  const loadBtn = el("button", { type: "button", class: "btn primary", text: "Load chain" });
  const quick = el("div", { class: "chips", "aria-label": "Quick picks" });
  const info = el("div", { class: "row" });
  const submit = () => {
    const s = inp.value.trim().toUpperCase();
    if (!SYM.test(s)) { inp.setCustomValidity("Type a ticker, e.g. NVDA"); inp.reportValidity(); return; }
    inp.setCustomValidity("");
    load(s, null);
  };
  inp.addEventListener("keydown", (ev) => { if (ev.key === "Enter") submit(); });
  inp.addEventListener("input", () => inp.setCustomValidity(""));
  loadBtn.addEventListener("click", submit);
  head.append(el("div", { class: "row" }, el("label", { class: "field" }, el("span", { text: "Ticker" }), inp), loadBtn, quick, el("span", { class: "spacer" }), info));
  api("/api/themes").then((t) => {
    const fx = t.themes.find((x) => x.kind === "fixtures");
    const picks = (meta.mode === "fixtures" && fx ? fx.tickers : (t.themes[0] || { tickers: [] }).tickers).slice(0, 8);
    for (const s of picks) quick.appendChild(el("button", { type: "button", class: "chip", text: s, onclick: () => { inp.value = s; load(s, null); } }));
  }).catch(() => {});

  // ---- chain access ----------------------------------------------------------------------------------
  async function chainFor(e) {
    if (chains.has(e)) return chains.get(e);
    const s = sym, c = await api("/api/chain", { symbol: s, expiry: e });
    if (s === sym) chains.set(c.expiry, c);
    return c;
  }
  const quoteOf = (l) => {
    if (l.type === "S") return { bid: base.spot, ask: base.spot, mid: base.spot, iv: null, delta: 1 };
    const c = chains.get(l.expiry);
    const row = c && c.strikes.find((r) => Math.abs(r.strike - l.strike) < 1e-9);
    return row ? row[l.type] || null : null;
  };
  const expInfo = (e) => base.expiries.find((x) => x.expiry === e);

  async function load(s, legCodes) {
    const my = ++gen;
    sym = s;
    chains.clear();
    legs = [];
    inp.value = s;
    clear(info);
    clear(body).appendChild(loading(`Loading the ${s} option chain…`));
    if (vz) { vz.destroy(); vz = null; }
    try {
      base = await chainFor(null);
      if (my !== gen || !isAlive()) return;
      chains.set(base.expiry, base);
      expiry = base.expiry;
      if (legCodes) {
        const dec = legCodes.split(",").filter(Boolean).slice(0, maxLegs).map((c) => E.decodeLeg(c));
        const exps = [...new Set(dec.filter((l) => l.type !== "S").map((l) => l.expiry))];
        await Promise.all(exps.map((e) => chainFor(e).catch(() => null)));
        if (my !== gen || !isAlive()) return;
        legs = dec.map((l) => {
          const q = quoteOf(l);
          const midOk = q && Math.abs(q.mid - l.fill) < 0.005;
          const ivOk = !q || l.iv == null || Math.abs((q.iv || 0) - l.iv) < 0.0005;
          return { side: l.side, type: l.type, strike: l.strike, expiry: l.expiry, qty: l.qty,
                   mode: midOk ? "mid" : "custom", fill: l.fill, ivo: ivOk || l.type === "S" ? null : l.iv };
        });
        if (exps[0]) expiry = exps.sort()[0];
        tplLabel = "From the scan";
      }
    } catch (err) {
      if (my !== gen) return;
      clear(body).appendChild(errorBox(err));
      return;
    }
    clear(info).append(el("span", { class: "px", text: `${sym} ${num(base.spot, 2)}` }), badge(base.mode === "fixtures" ? "synthetic" : base.mode, "mode"),
      el("span", { class: "fresh", text: base.fresh }), link(`/ticker/${sym}`, "Ticker page", { class: "btn small ghost" }));
    build();
  }

  // ---- strike pickers for templates --------------------------------------------------------------------
  function ctxFor(c) {
    const rows = c.strikes, K = rows.map((r) => r.strike), spot = base.spot;
    const ok = (k, type) => { const r = rows.find((x) => x.strike === k); return r && r[type] && r[type].mid > 0; };
    const near = (price, dir = 0, type = null) => {
      let best = null;
      for (const k of K) {
        if (dir > 0 && k < price - 1e-9) continue;
        if (dir < 0 && k > price + 1e-9) continue;
        if (type && !ok(k, type)) continue;
        if (best == null || Math.abs(k - price) < Math.abs(best - price)) best = k;
      }
      return best ?? (dir > 0 ? K[K.length - 1] : dir < 0 ? K[0] : K[0]);
    };
    const atm = () => near(spot);
    const byDelta = (type, d, minK = null, maxK = null) => {
      let best = null, err = Infinity;
      for (const r of rows) {
        const q = r[type];
        if (!q || !(q.mid > 0) || q.delta == null) continue;
        if (minK != null && r.strike <= minK) continue;
        if (maxK != null && r.strike >= maxK) continue;
        const e2 = Math.abs(Math.abs(q.delta) - d);
        if (e2 < err) { err = e2; best = r.strike; }
      }
      return best ?? atm();
    };
    const step = (k, n) => { const i = K.indexOf(k); return K[Math.min(Math.max(i + n, 0), K.length - 1)]; };
    const below = (k, cand) => (cand < k ? cand : step(k, -1));
    const above = (k, cand) => (cand > k ? cand : step(k, 1));
    const ei = expInfo(c.expiry) || {};
    const em = spot * (ei.atm_iv || 0.3) * Math.sqrt(Math.max(ei.dte || 30, 1) / 365);
    const width = () => { const k = atm(); const w = near(k + em, 1) - k; return w > 0 ? w : step(k, 1) - k || 1; };
    const fr = expInfo(c.expiry) || { dte: 0 };
    const backE = base.expiries.find((x) => x.dte >= fr.dte + 21) || base.expiries[base.expiries.length - 1];
    const leg = (side, type, strike, qty = 1, e = c.expiry) => ({ side, type, strike, expiry: e, qty, mode: "mid", fill: null, ivo: null });
    const shares = (side, n) => ({ side, type: "S", strike: 0, expiry: null, qty: n, mode: "mid", fill: null, ivo: null });
    return { atm, byDelta, near, below, above, width, leg, shares, back: backE.expiry };
  }

  async function applyTemplate(t) {
    const c = await chainFor(expiry);
    const ctx = ctxFor(c);
    if (ctx.back === c.expiry && t.group === "Time") {
      body.prepend(status("warn", "No later expiry to pair with: pick an earlier front expiry for calendars."));
      return;
    }
    const nl = t.build(ctx);
    await Promise.all([...new Set(nl.filter((l) => l.expiry).map((l) => l.expiry))].map((e) => chainFor(e)));
    if (!isAlive()) return;
    legs = nl;
    tplLabel = t.label;
    build();
  }

  // ---- the page body -------------------------------------------------------------------------------------
  const legsBox = el("div"), ladderBox = el("div"), vizHost = el("div"), warnBox = el("div");
  const expTabs = el("div", { class: "exptabs", role: "tablist", "aria-label": "Expiry" });

  function build() {
    clear(body);
    clear(expTabs);
    for (const x of base.expiries) {
      const b = el("button", { type: "button", role: "tab", "aria-selected": x.expiry === expiry ? "true" : "false" }, fmtDate(x.expiry), el("small", { text: `${x.dte}d` }));
      b.addEventListener("click", async () => {
        expiry = x.expiry;
        for (const o of expTabs.children) o.setAttribute("aria-selected", o === b ? "true" : "false");
        await chainFor(expiry).catch(() => null);
        drawLadder();
      });
      expTabs.appendChild(b);
    }
    const groups = el("div", { class: "tgroups" });
    for (const g of GROUPS) {
      groups.appendChild(el("div", { class: "tgroup" }, el("h4", { text: g }), el("div", { class: "chips" },
        TEMPLATES.filter((t) => t.group === g).map((t) => el("button", { type: "button", class: "chip", title: t.ur ? "Undefined risk: the loss has no limit" : null,
          onclick: () => applyTemplate(t).catch((err) => body.prepend(errorBox(err))) }, t.label, t.ur ? el("span", { class: "ur", text: " UNDEFINED RISK" }) : null)))));
    }
    groups.appendChild(el("div", { class: "tgroup" }, el("h4", { text: "Custom" }), el("div", { class: "chips" },
      el("button", { type: "button", class: "chip", text: "+ Call", onclick: () => addLeg("C") }),
      el("button", { type: "button", class: "chip", text: "+ Put", onclick: () => addLeg("P") }),
      el("button", { type: "button", class: "chip", text: "+ Shares", onclick: () => addLeg("S") }),
      el("button", { type: "button", class: "chip", text: "Clear", onclick: () => { legs = []; tplLabel = "Custom"; changed(); } }))));
    body.append(card("1 · Expiry", expTabs, el("p", { class: "muted", text: "Templates and new legs use this expiry; calendars pair it with the first expiry at least 3 weeks later." })),
      card("2 · Strategy", groups, el("p", { class: "muted", text: "Strikes are picked by delta on the chosen expiry. Change anything in the legs below." })),
      card("3 · Legs", legsBox, warnBox), card("Strike ladder", ladderBox), vizHost);
    changed();
  }

  async function addLeg(type) {
    if (legs.length >= maxLegs) return;
    if (type === "S") legs.push({ side: 1, type: "S", strike: 0, expiry: null, qty: 100, mode: "mid", fill: null, ivo: null });
    else {
      const c = await chainFor(expiry);
      legs.push({ side: 1, type, strike: ctxFor(c).atm(), expiry: c.expiry, qty: 1, mode: "mid", fill: null, ivo: null });
    }
    tplLabel = "Custom";
    changed();
  }

  // ---- leg editor --------------------------------------------------------------------------------------
  const fillOf = (l, q) => (l.type === "S" ? (l.mode === "custom" && l.fill > 0 ? l.fill : base.spot)
    : l.mode === "custom" ? l.fill : !q ? null : l.mode === "natural" ? (l.side > 0 ? q.ask : q.bid) : q.mid);
  const ivOf = (l, q) => (l.type === "S" ? null : l.ivo != null ? l.ivo : q && q.iv > 0 ? q.iv : (expInfo(l.expiry) || {}).atm_iv || null);

  function drawLegs() {
    clear(legsBox);
    if (!legs.length) {
      legsBox.appendChild(el("p", { class: "muted", text: "No legs yet: pick a strategy above or add a call, put or shares." }));
      return;
    }
    const tbl = el("table", { class: "data legsx" }, el("thead", null, el("tr", null,
      ["Side", "Type", "Qty", "Expiry", "Strike", "Bid / ask", "Δ", "Fill", "Price", "IV", ""].map((h, i) => el("th", { class: i === 2 || i >= 5 && i < 10 ? "n" : "", text: h })))));
    const tb = el("tbody");
    legs.forEach((l, i) => {
      const q = quoteOf(l);
      const side = el("button", { type: "button", class: `btn small ${l.side > 0 ? "B" : "S"}`, text: l.side > 0 ? "BUY" : "SELL", "aria-label": `Leg ${i + 1}: switch buy/sell`,
        onclick: () => { l.side = -l.side; tplLabel = "Custom"; changed(); } });
      const type = el("select", { "aria-label": `Leg ${i + 1} type` }, [["C", "Call"], ["P", "Put"], ["S", "Shares"]].map(([v, t]) => el("option", { value: v, text: t })));
      type.value = l.type;
      type.addEventListener("change", async () => {
        const was = l.type;
        l.type = type.value;
        if (l.type === "S") { l.strike = 0; l.expiry = null; l.qty = 100; }
        else if (was === "S") { const c = await chainFor(expiry); l.expiry = c.expiry; l.strike = ctxFor(c).atm(); l.qty = 1; }
        l.mode = l.mode === "custom" ? "mid" : l.mode;
        tplLabel = "Custom";
        changed();
      });
      const qty = el("input", { type: "number", class: "q", min: 1, max: 1000, step: 1, value: l.qty, "aria-label": `Leg ${i + 1} quantity` });
      qty.addEventListener("change", () => { const v = Math.round(Number(qty.value)); if (v >= 1 && v <= 1000) { l.qty = v; changed(); } else qty.value = l.qty; });
      let exp = el("span", { class: "muted", text: DASH }), strike = el("span", { class: "muted", text: DASH });
      if (l.type !== "S") {
        exp = el("select", { "aria-label": `Leg ${i + 1} expiry` }, base.expiries.map((x) => el("option", { value: x.expiry, text: `${fmtDate(x.expiry)} (${x.dte}d)` })));
        exp.value = l.expiry;
        exp.addEventListener("change", async () => {
          const c = await chainFor(exp.value);
          l.expiry = c.expiry;
          if (!c.strikes.some((r) => r.strike === l.strike)) l.strike = ctxFor(c).near(l.strike);
          changed();
        });
        const c = chains.get(l.expiry);
        strike = el("select", { "aria-label": `Leg ${i + 1} strike` }, (c ? c.strikes : []).filter((r) => r[l.type]).map((r) => el("option", { value: String(r.strike), text: num(r.strike, r.strike % 1 ? 2 : 0) })));
        strike.value = String(l.strike);
        strike.addEventListener("change", () => { l.strike = Number(strike.value); changed(); });
      }
      const mode = el("select", { "aria-label": `Leg ${i + 1} fill` }, [["mid", "Mid"], ["natural", "Natural"], ["custom", "Custom"]].map(([v, t]) => el("option", { value: v, text: t })));
      mode.value = l.mode;
      const f = fillOf(l, q);
      const price = el("input", { type: "number", class: "q", min: 0, step: 0.01, value: f != null ? f.toFixed(2) : "", disabled: l.mode !== "custom", "aria-label": `Leg ${i + 1} fill price` });
      mode.addEventListener("change", () => { l.mode = mode.value; if (l.mode === "custom") l.fill = fillOf({ ...l, mode: "mid" }, q); changed(); });
      price.addEventListener("change", () => { const v = Number(price.value); if (v >= 0 && v < 1e6) { l.fill = v; changed(); } else price.value = l.fill; });
      const ivv = ivOf(l, q);
      const iv = l.type === "S" ? el("span", { class: "muted", text: DASH })
        : el("input", { type: "number", class: "q", min: 1, max: 500, step: 0.1, value: ivv != null ? (ivv * 100).toFixed(1) : "", "aria-label": `Leg ${i + 1} implied vol, percent`,
                         title: l.ivo != null ? "Your override (clear it to use the chain's IV)" : "The chain's IV for this leg" });
      if (l.type !== "S") {
        iv.addEventListener("change", () => {
          if (iv.value === "") { l.ivo = null; changed(); return; }
          const v = Number(iv.value) / 100;
          if (v > 0.005 && v <= 5) { l.ivo = v; changed(); } else iv.value = ivv != null ? (ivv * 100).toFixed(1) : "";
        });
        if (l.ivo != null) iv.classList.add("over");
      }
      const rm = el("button", { type: "button", class: "btn small ghost", text: "✕", "aria-label": `Remove leg ${i + 1}`, onclick: () => { legs.splice(i, 1); tplLabel = "Custom"; changed(); } });
      tb.appendChild(el("tr", { class: !q && l.type !== "S" ? "bad" : "" }, el("td", null, side), el("td", null, type), el("td", { class: "n" }, qty), el("td", null, exp), el("td", null, strike),
        el("td", { class: "n", text: l.type === "S" ? num(base.spot, 2) : q ? `${num(q.bid, 2)} / ${num(q.ask, 2)}` : "no quote" }),
        el("td", { class: "n", text: q && q.delta != null ? num(q.delta * (l.type === "S" ? 1 : 100), 0) : DASH }),
        el("td", { class: "n" }, mode), el("td", { class: "n" }, price), el("td", { class: "n" }, iv), el("td", null, rm)));
    });
    tbl.appendChild(tb);
    const net = E.netBasis(validLegs());
    legsBox.append(tbl, el("div", { class: "row" },
      el("span", { class: "muted", text: `${legs.length} of ${maxLegs} legs.` }),
      el("b", { text: net >= 0 ? `Net debit ${money(net, 2)}` : `Net credit ${money(-net, 2)}` }),
      el("span", { class: "muted", text: "Δ is per contract ×100 (shares: per share). Natural fill = pay the ask, sell at the bid." })));
  }

  function validLegs() {
    const out = [];
    for (const l of legs) {
      const q = quoteOf(l);
      if (l.type !== "S" && !q) continue;
      const fill = fillOf(l, q), iv = ivOf(l, q);
      if (!(fill >= 0) || (l.type !== "S" && !(iv > 0))) continue;
      out.push({ side: l.side, type: l.type, strike: l.type === "S" ? 0 : l.strike, expiry: l.type === "S" ? null : l.expiry, qty: l.qty, fill, iv });
    }
    return out;
  }

  // ---- strike ladder ---------------------------------------------------------------------------------------
  function drawLadder() {
    clear(ladderBox);
    const c = chains.get(expiry);
    if (!c) { ladderBox.appendChild(loading("")); return; }
    const rows = c.strikes, spot = base.spot;
    // ±30 % around spot, widened so every leg on this expiry stays on the ladder
    const ks = legs.filter((l) => l.type !== "S" && l.expiry === expiry).map((l) => l.strike);
    const lo = Math.min(spot * 0.7, ...ks), hi = Math.max(spot * 1.3, ...ks);
    const vis = rows.filter((r) => r.strike >= lo && r.strike <= hi);
    if (!vis.length) { ladderBox.appendChild(el("p", { class: "muted", text: "No strikes near the money." })); return; }
    const W = 880, H = 190, mid = 96;
    const xd = [vis[0].strike, vis[vis.length - 1].strike];
    const fr = frame({ w: W, h: H, m: { t: 10, r: 16, b: 26, l: 16 }, xd: [xd[0] - (xd[1] - xd[0]) * 0.02, xd[1] + (xd[1] - xd[0]) * 0.02], yd: [0, 1], yticks: 0,
                       xfmt: (v) => num(v, 0), yfmt: () => "", title: `Open interest by strike, ${fmtDate(expiry, true)}` });
    const { root, plot, x, box } = fr;
    const maxOI = Math.max(1, ...vis.map((r) => Math.max(r.C ? r.C.oi || 0 : 0, r.P ? r.P.oi || 0 : 0)));
    const hgt = lin(0, maxOI, 0, mid - box.t - 14);
    const bw = Math.max(2, Math.min(14, ((x(xd[1]) - x(xd[0])) / vis.length) * 0.35));
    for (const r of vis) {
      const cx = x(r.strike);
      if (r.C && r.C.oi) plot.appendChild(svg("rect", { x: cx - bw - 0.5, y: mid - hgt(r.C.oi), width: bw, height: hgt(r.C.oi), rx: 1.5, class: "oi-c" }));
      if (r.P && r.P.oi) plot.appendChild(svg("rect", { x: cx + 0.5, y: mid - hgt(r.P.oi), width: bw, height: hgt(r.P.oi), rx: 1.5, class: "oi-p" }));
      const hit = svg("rect", { x: cx - bw - 2, y: box.t, width: 2 * bw + 4, height: box.b - box.t, class: "hit" });
      hit.addEventListener("pointermove", (ev) => showTip([`Strike ${num(r.strike, 2)}`,
        ["Call bid / ask", r.C ? `${num(r.C.bid, 2)} / ${num(r.C.ask, 2)}` : DASH], ["Call IV · Δ", r.C ? `${pct(r.C.iv, 1)} · ${num(r.C.delta, 2)}` : DASH], ["Call OI", r.C ? num(r.C.oi, 0) : DASH],
        ["Put bid / ask", r.P ? `${num(r.P.bid, 2)} / ${num(r.P.ask, 2)}` : DASH], ["Put IV · Δ", r.P ? `${pct(r.P.iv, 1)} · ${num(r.P.delta, 2)}` : DASH], ["Put OI", r.P ? num(r.P.oi, 0) : DASH]], ev.clientX, ev.clientY));
      hit.addEventListener("pointerleave", hideTip);
      root.appendChild(hit);
    }
    plot.appendChild(svg("line", { x1: box.l, x2: box.r, y1: mid, y2: mid, class: "baseline" }));
    plot.appendChild(svg("line", { x1: x(spot), x2: x(spot), y1: box.t, y2: box.b, class: "spotline" }));
    plot.appendChild(svg("text", { x: x(spot) + 4, y: box.t + 10, class: "lbl spotline", text: `spot ${num(spot, 2)}` }));
    const lv = c.levels || {};
    const wl = [];
    for (const [k, lab] of [["put_wall", "put wall"], ["call_wall", "call wall"], ["gamma_flip", "flip"]]) {
      if (lv[k] > xd[0] && lv[k] < xd[1]) {
        plot.appendChild(svg("line", { x1: x(lv[k]), x2: x(lv[k]), y1: mid, y2: box.b, class: "level wall" }));
        wl.push({ x: x(lv[k]), text: lab });
      }
    }
    vlabels(plot, wl, box.b - 4);
    // the legs on this expiry, as chips under the axis
    const on = legs.filter((l) => l.type !== "S" && l.expiry === expiry && l.strike >= xd[0] && l.strike <= xd[1]);
    const stack = new Map();
    for (const l of on) {
      const n = stack.get(l.strike) || 0;
      stack.set(l.strike, n + 1);
      const cx = x(l.strike), cy = mid + 18 + n * 18;
      const txt = `${l.side > 0 ? "+" : "−"}${l.qty}${l.type}`;
      const g = svg("g", { class: `legchip ${l.side > 0 ? "B" : "S"}` }, svg("rect", { x: cx - 17, y: cy - 8, width: 34, height: 15, rx: 4 }),
        svg("text", { x: cx, y: cy + 3.5, "text-anchor": "middle", text: txt }), svg("title", { text: `${l.side > 0 ? "Buy" : "Sell"} ${l.qty} ${num(l.strike, 2)} ${l.type === "C" ? "call" : "put"}` }));
      plot.appendChild(g);
    }
    const other = legs.filter((l) => l.type !== "S" && l.expiry !== expiry).length;
    fill(ladderBox, el("div", { class: "legend" }, el("span", { class: "li" }, el("i", { class: "box oi-c" }), "Call open interest"),
      el("span", { class: "li" }, el("i", { class: "box oi-p" }), "Put open interest"),
      el("span", { class: "li", text: "Chips: your legs on this expiry (+ buy, − sell)" })), root,
      other ? el("p", { class: "note", text: `${other} leg${other > 1 ? "s are" : " is"} on another expiry: pick its tab to see ${other > 1 ? "them" : "it"} here.` }) : null,
      el("p", { class: "fresh", text: c.fresh }));
  }

  // ---- the visualizer ------------------------------------------------------------------------------------
  const syncUrl = () => {
    const vl = validLegs();
    const u = new URLSearchParams({ symbol: sym });
    if (vl.length) u.set("legs", vl.map((l) => E.encodeLeg(l)).join(","));
    history.replaceState(null, "", `/builder?${u}`);
  };

  const pushViz = debounce(() => {
    if (!isAlive()) return;
    const vl = validLegs();
    clear(warnBox);
    const bad = legs.length - vl.length;
    if (bad) warnBox.appendChild(status("warn", `${bad} leg${bad > 1 ? "s have" : " has"} no quote or no IV and ${bad > 1 ? "are" : "is"} left out of the charts.`));
    if (!vl.some((l) => l.type !== "S")) {
      if (vz) { vz.destroy(); vz = null; }
      clear(vizHost).appendChild(card("Visualizer", el("p", { class: "muted", text: "Add at least one option leg to see the P/L table, graphs and simulation." })));
      return;
    }
    const rt = legs.reduce((a, l) => { const q = quoteOf(l); return a + (l.type !== "S" && q ? (q.ask - q.bid) * l.qty * 100 : 0); }, 0);
    const spec = { symbol: sym, spot: base.spot, today: base.today, r: base.r, q: base.q, event: base.event, legs: vl, term: base.term, levels: base.levels,
                   label: tplLabel, as_of: base.as_of, roundtrip: rt, per: "as entered", fillNote: "as entered, at the fills shown" };
    if (vz) vz.update(spec);
    else { clear(vizHost); vz = mountViz(vizHost, spec, {}); }
  }, 120);

  function changed() {
    drawLegs();
    drawLadder();
    syncUrl();
    pushViz();
  }

  // ---- start -----------------------------------------------------------------------------------------------
  const start = symbol && SYM.test(symbol.toUpperCase()) ? symbol.toUpperCase() : null;
  if (start) load(start, legsParam || null);
  else body.appendChild(card("Start here", el("p", { text: "Type a ticker above, or pick one of the quick picks." }),
    el("p", { class: "muted", text: "Then choose an expiry and a strategy. Every leg can be edited: side, type, quantity, expiry, strike, fill and IV." })));

  return { destroy() { gen++; if (vz) vz.destroy(); hideTip(); } };
}
