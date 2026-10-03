// viz.js: the strategy visualizer (spec v2.2 §6.7-6.9). One instance per position.
// Table (shiny heatmap: green profit, red loss), Graph (P/L vs price at three dates + probability strip),
// Over time (Monte Carlo fan from /api/simulate), Simulation (plan vs hold) and Greeks.
// All pricing is engine.js (pinned to golden.json); the simulation runs on the server. Everything is an estimate.

import { el, svg, clear, api, num, money, moneyK, pct, fmtDate, DASH, showTip, hideTip, kv, badge, loading,
         errorBox, debounce, perFrame, fill } from "./ui.js";
import { SERIES, INK, frame, extent, pathOf, niceStep, pointerX, vline, hline, vlabels, legend, heatColor, RAMPS, tickDecimals } from "./charts.js";
import * as E from "./engine.js";

let UID = 0;

export const METRICS = [
  { key: "pl", label: "P/L $" },
  { key: "pl_pct", label: "P/L %" },
  { key: "value", label: "Value" },
  { key: "pct_risk", label: "% of max risk" },
];
const TABS = [["table", "Table"], ["graph", "Graph"], ["time", "Over time"], ["sim", "Simulation"], ["greeks", "Greeks"]];
const ALERT_ONLY = "Alert only. You place and manage every order yourself.";

const isWeekday = (d) => { const w = new Date(`${d}T00:00:00Z`).getUTCDay(); return w !== 0 && w !== 6; };
const clamp = (x, lo, hi) => Math.min(Math.max(x, lo), hi);

function fmtMetric(kind, x, compact = false) {
  if (!Number.isFinite(x)) return DASH;
  if (kind === "pl") return compact ? moneyK(x, true) : money(x, 0, true);
  if (kind === "value") return compact ? moneyK(x, false) : money(x, 0);
  return pct(x, 0, true);
}
const axisFmt = (kind) => (kind === "pl" ? (v) => moneyK(v, true) : kind === "value" ? (v) => moneyK(v, false) : (v) => pct(v, 0, true));
const priceFmt = (lo, hi) => {
  const s = niceStep(hi - lo, 6), nd = Number.isInteger(s) ? 0 : s >= 0.1 ? 1 : 2;
  return (v) => num(v, nd);
};

function legText(l) {
  if (l.type === "S") return `${l.side > 0 ? "Long" : "Short"} ${num(l.qty, 0)} shares`;
  return `${l.side > 0 ? "Buy" : "Sell"} ${num(l.qty, 0)} ${num(l.strike, 2)} ${l.type === "C" ? "call" : "put"} ${fmtDate(l.expiry)}`;
}

// price rows for the table: nice steps across ±range around spot, plus the spot row; high prices on top
function priceRows(spot, rangePct) {
  const lo = spot * (1 - rangePct / 100), hi = spot * (1 + rangePct / 100);
  const st = niceStep(hi - lo, 20), rows = [];
  for (let v = Math.ceil(lo / st) * st; v <= hi + st * 1e-9; v += st) rows.push(Number(v.toFixed(6)));
  if (!rows.some((r) => Math.abs(r - spot) < st * 1e-6)) rows.push(spot);
  return rows.sort((a, b) => b - a);
}

// at most 16 date columns, always today, the event date and the expiry
function pickCols(dates, must) {
  const n = dates.length;
  if (n <= 16) return dates.map((_, i) => i);
  const idx = new Set([0, n - 1, ...must.filter((i) => i > 0)]);
  const k = 16 - idx.size;
  for (let j = 1; j <= k; j++) idx.add(Math.round((j * (n - 1)) / (k + 1)));
  return [...idx].sort((a, b) => a - b);
}

export function mountViz(root, spec0, opts = {}) {
  const uid = ++UID;
  const defaults = () => ({ metric: "pl", tab: opts.tab || "table", di: 0, range: null, ivmult: 1, crush: true, cvd: false, fan: "plan" });
  let st = defaults();
  let spec, legs, E0, front, last, dates, cols, rs, sigma, evOk, simKey = "", simSeq = 0;
  let sim = null, simErr = null, simBusy = false, focusRK = null;
  const ui = {};

  const host = el("section", { class: "card viz", "aria-label": "Strategy visualizer" });
  const head = el("div", { class: "viz-h" });
  const legsBox = el("div", { class: "legs" });
  const metricsRow = el("div", { class: "vmetrics" });
  const exitBox = el("div", { class: "exitplan", "aria-label": "Exit plan" });
  const controls = el("div", { class: "vcontrols" });
  const tabsRow = el("div", { class: "vtabs" });
  const panel = el("div", { class: "vpanel", role: "tabpanel" });
  host.append(head, legsBox, metricsRow, exitBox, controls, tabsRow, panel);
  root.appendChild(host);

  const ev = () => (evOk && st.crush ? spec.event : null);
  const ML = () => (rs && rs.max_loss != null && rs.max_loss < 0 ? rs.max_loss : null);
  const on = () => dates[st.di];

  // ---- spec ----------------------------------------------------------------------------------------
  function setSpec(s) {
    spec = { ...s, r: s.r ?? 0.04, q: s.q ?? 0 };
    legs = spec.legs.map((l) => (typeof l === "string" ? E.decodeLeg(l) : { ...l }));
    spec.event = s.event && s.event.date && s.event.move > 0 ? { date: s.event.date, move: s.event.move } : null;
    E0 = E.netBasis(legs);
    front = E.frontExpiry(legs) || spec.today;
    last = legs.filter((l) => l.type !== "S").map((l) => l.expiry).sort().pop() || front;
    evOk = !!(spec.event && spec.today < spec.event.date && spec.event.date < last);
    const prevDate = dates ? dates[st.di] : null;
    dates = [spec.today];
    for (let d = spec.today; d < front;) { d = E.addDays(d, 1); if (isWeekday(d) || d === front) dates.push(d); }
    if (evOk && spec.event.date <= front && !dates.includes(spec.event.date)) dates.push(spec.event.date);
    dates.sort();
    cols = pickCols(dates, [evOk ? dates.indexOf(spec.event.date) : -1]);
    st.di = prevDate && dates.includes(prevDate) ? dates.indexOf(prevDate) : Math.min(st.di, dates.length - 1);
    // the front expiry's ATM IV (the market's sigma for the probability model)
    const t = (spec.term || []).find((x) => x[0] === front);
    const fl = legs.filter((l) => l.type !== "S" && l.expiry === front && l.iv);
    sigma = t ? t[1] : fl.length ? fl.reduce((a, l) => a + l.iv, 0) / fl.length : 0.3;
    if (st.range == null) {
      const em = sigma * Math.sqrt(Math.max(E.tYears(spec.today, front), 1 / 365)) * 100;
      st.range = clamp(Math.ceil((2 * em) / 5) * 5, 10, 60);
    }
    recalcRisk();
    buildHead();
    buildControls();
    buildTabs();
    renderExit();
    draw();
    requestSim();
  }

  function recalcRisk() {
    rs = E.riskStats(legs, spec.today, spec.r, spec.q, 1, ev(), spec.spot);
  }

  // ---- header, legs, exit plan ---------------------------------------------------------------------
  function buildHead() {
    clear(head);
    const left = el("div", null, el("h2", { text: `${spec.symbol} · ${spec.label || "Position"}` }),
      el("div", { class: "muted", text: `Spot ${num(spec.spot, 2)} · priced ${spec.as_of || spec.today} · ${spec.per || "per 1 lot as entered"}` }));
    const right = el("div", { class: "row" });
    if (rs.unbounded_loss) right.appendChild(badge("Undefined risk", "danger"));
    if (spec.suggested_contracts) right.appendChild(badge(`Sized: ${spec.suggested_contracts} contracts`, ""));
    if (opts.builderHref) right.appendChild(el("a", { class: "btn", href: opts.builderHref, "data-link": "1", text: "Open in Strategy Builder" }));
    head.append(left, right);
    clear(legsBox);
    for (const l of legs) {
      legsBox.appendChild(el("div", { class: l.side > 0 ? "B" : "S" }, el("span", { class: "act", text: l.side > 0 ? "BUY" : "SELL" }),
        `${legText(l).replace(/^(Buy|Sell|Long|Short) /, "")} @ ${num(l.fill, 2)}${l.iv ? ` · IV ${pct(l.iv, 1)}` : ""}`));
    }
  }

  function exitText() {
    if (spec.exits) return spec.exits;
    const p = (sim && sim.plan) || { kind: E0 < 0 ? "credit" : "debit", tp: 0.5, stop: E0 < 0 ? 2 : 0.5, texit: null };
    const sym = spec.symbol;
    const lv = p.below || p.above ? `${sym} closes${p.below ? ` below ${num(p.below, 2)}` : ""}${p.below && p.above ? " or" : ""}${p.above ? ` above ${num(p.above, 2)}` : ""}` : "None set (Strategy Builder default plan)";
    if (p.kind === "credit") {
      const C = -E0;
      return { stop: `Close if the cost to close reaches ${num(p.stop, 1)}× the ${money(C)} credit (${money(C * p.stop)}; loss ≈ ${money(C * (p.stop - 1))}).`,
               target: `Buy back at ${money(C * (1 - p.tp))}: keep ${pct(p.tp)} of the credit.`,
               time: p.texit ? `Close by ${fmtDate(p.texit, true)} at the latest.` : "Shown when the simulation loads.", invalidation: lv };
    }
    return { stop: `Max loss = the ${money(E0)} debit. Close if the position is worth ${money(E0 * p.stop)} or less.`,
             target: `Sell at ${money(E0 * (1 + p.tp))} (+${pct(p.tp)} on the debit).`,
             time: p.texit ? `Close by ${fmtDate(p.texit, true)} at the latest.` : "Shown when the simulation loads.", invalidation: lv };
  }

  function renderExit() {
    clear(exitBox);
    const x = exitText();
    const it = (k, v) => el("div", { class: "it" }, el("div", { class: "k", text: k }), el("div", { class: "v", text: v || DASH }));
    fill(exitBox, it("Stop", String(x.stop || "").replace(/\s*No rolling\.?\s*$/i, "")), it("Target", x.target),
      it("Time exit", x.time), it("Invalidation", x.invalidation),
      el("div", { class: "it" }, el("div", { class: "k", text: "Adjustments" }), el("div", { class: "v norolling", text: "No rolling." })),
      el("div", { class: "alert-only", text: `${spec.alertOnly || ALERT_ONLY} ${spec.notSaved || ""}`.trim() }));
  }

  // ---- metrics row ---------------------------------------------------------------------------------
  function renderMetrics() {
    clear(metricsRow);
    const e = ev();
    const ml = rs.max_loss, mp = rs.max_profit;
    const cents = Math.abs(E0 - Math.round(E0)) > 0.004 ? 2 : 0;  // e.g. $605.50: the scan card rounds to the dollar
    const net = E0 >= 0 ? ["Net debit", money(E0, cents)] : ["Net credit", money(-E0, cents)];
    const be = rs.breakevens.length ? rs.breakevens.map((b) => num(b, 2)).join(" · ") : "none";
    const popF = E.pop(legs, spec.spot, front, spec.today, sigma * st.ivmult, spec.r, spec.q, st.ivmult, e);
    const plNow = E.positionValue(legs, spec.spot, on(), spec.today, spec.r, spec.q, st.ivmult, e) - E0;
    const tiles = [
      kv(net[0], net[1], spec.fillNote || "per 1 lot, at the fills shown"),
      kv("Max loss", ml == null ? "UNDEFINED" : money(ml, 0), ml == null ? "loss grows without limit" : `at ${fmtDate(rs.at)}`),
      kv("Max profit", mp == null ? "Unlimited" : money(mp, 0), `at ${fmtDate(rs.at)}${legs.some((l) => l.expiry && l.expiry > front) ? ", back month at model value" : ""}`),
      kv("Breakevens", be, `at ${fmtDate(rs.at)}`),
      kv("Chance of profit", pct(popF, 0), `lognormal, IV ${pct(sigma * st.ivmult, 1)} (estimate)`),
      kv(`P/L at spot, ${fmtDate(on())}`, money(plNow, 0, true), `IV ×${num(st.ivmult, 2)}`),
      kv("Round-trip cost", spec.roundtrip == null ? DASH : money(spec.roundtrip, 0), "bid-ask paid in and out"),
    ];
    if (ml == null) tiles[1].classList.add("alert");
    metricsRow.append(...tiles);
  }

  // ---- controls ------------------------------------------------------------------------------------
  function slider(label, attrs, onInput) {
    const val = el("b");
    const inp = el("input", { type: "range", ...attrs, "aria-label": label });
    inp.addEventListener("input", () => onInput(Number(inp.value), val));
    const box = el("div", { class: "slider" }, el("div", { class: "lab" }, el("span", { text: label }), val), inp);
    return { box, inp, val };
  }

  const redraw = perFrame(() => draw());

  function dateLabel() {
    const d = on(), n = E.days(spec.today, d), left = E.days(d, front);
    const tag = d === spec.today ? "today" : d === front ? "front expiry" : evOk && d === spec.event.date ? "earnings" : `${n}d from today`;
    ui.date.val.textContent = `${fmtDate(d, true)} · ${tag} · ${left} DTE`;
  }

  function buildControls() {
    clear(controls);
    ui.date = slider("Date", { min: 0, max: dates.length - 1, step: 1, value: st.di }, (v) => { st.di = v; dateLabel(); redraw(); });
    const jump = (i) => { st.di = i; ui.date.inp.value = String(i); dateLabel(); draw(); };
    const quick = el("div", { class: "presets" },
      el("button", { class: "btn small", type: "button", text: "Today", onclick: () => jump(0) }),
      evOk && dates.includes(spec.event.date) ? el("button", { class: "btn small", type: "button", text: "Earnings", onclick: () => jump(dates.indexOf(spec.event.date)) }) : null,
      el("button", { class: "btn small", type: "button", text: "Expiry", onclick: () => jump(dates.length - 1) }));
    ui.date.box.appendChild(quick);
    dateLabel();

    ui.range = slider("Price range", { min: 5, max: 60, step: 1, value: st.range }, (v, b) => { st.range = v; b.textContent = `±${v}%`; redraw(); });
    ui.range.val.textContent = `±${st.range}%`;

    ui.iv = slider("Implied vol", { min: 0.3, max: 3, step: 0.05, value: st.ivmult }, (v, b) => { st.ivmult = v; b.textContent = ivText(); redraw(); });
    const ivText = () => `×${num(st.ivmult, 2)} (${pct(sigma * st.ivmult, 1)})`;
    ui.iv.val.textContent = ivText();
    const preset = (m) => el("button", { class: "btn small", type: "button", text: `×${m}`, onclick: () => { st.ivmult = m; ui.iv.inp.value = String(m); ui.iv.val.textContent = ivText(); draw(); } });
    ui.iv.box.appendChild(el("div", { class: "presets" }, preset(1), preset(2), preset(3)));

    const toggles = el("div", { class: "slider" });
    if (evOk) {
      const c = el("input", { type: "checkbox" });
      c.checked = st.crush;
      c.addEventListener("change", () => { st.crush = c.checked; recalcRisk(); buildHead(); draw(); requestSim(); });
      toggles.appendChild(el("label", { class: "chk" }, c,
        `Earnings IV crush on ${fmtDate(spec.event.date)} (±${pct(spec.event.move, 1)} priced)`));
    }
    const cv = el("input", { type: "checkbox" });
    cv.checked = st.cvd;
    cv.addEventListener("change", () => { st.cvd = cv.checked; draw(); });
    toggles.appendChild(el("label", { class: "chk" }, cv, "Colour-blind palette (blue = profit)"));
    toggles.appendChild(el("div", { class: "presets" }, el("button", { class: "btn small", type: "button", text: "Reset view",
      onclick: () => { const tab = st.tab; st = defaults(); st.tab = tab; setSpec(spec0Current); } })));
    controls.append(ui.date.box, ui.range.box, ui.iv.box, toggles);
  }

  function buildTabs() {
    clear(tabsRow);
    const tl = el("div", { class: "seg", role: "tablist", "aria-label": "View" });
    for (const [k, label] of TABS) {
      tl.appendChild(el("button", { type: "button", role: "tab", "aria-selected": st.tab === k ? "true" : "false", text: label,
        onclick: () => { st.tab = k; buildTabs(); draw(); } }));
    }
    const ms = el("div", { class: "seg", role: "group", "aria-label": "Metric" });
    for (const m of METRICS) {
      const dis = m.key === "pct_risk" && ML() == null;
      ms.appendChild(el("button", { type: "button", "aria-pressed": st.metric === m.key ? "true" : "false", text: m.label, disabled: dis,
        title: dis ? "Max risk is undefined for this position" : null,
        onclick: () => { st.metric = m.key; buildTabs(); draw(); } }));
    }
    if (st.metric === "pct_risk" && ML() == null) st.metric = "pl";
    tabsRow.append(tl, el("span", { class: "spacer" }), el("span", { class: "muted", text: "Show" }), ms);
  }

  // ---- draw ----------------------------------------------------------------------------------------
  function draw() {
    hideTip();
    renderMetrics();
    const keep = panel.contains(document.activeElement);
    clear(panel);
    try {
      if (st.tab === "table") drawTable(keep);
      else if (st.tab === "graph") drawGraph();
      else if (st.tab === "time") drawTime();
      else if (st.tab === "sim") drawSim();
      else drawGreeks();
    } catch (e) {
      panel.appendChild(errorBox(e));
    }
  }

  // ---- table (the heatmap) -------------------------------------------------------------------------
  function drawTable(keepFocus) {
    const e = ev(), prices = priceRows(spec.spot, st.range), cd = cols.map((i) => dates[i]);
    const V = E.table(legs, prices, cd, spec.today, spec.r, spec.q, st.ivmult, e);
    let hiP = 0, loP = 0;
    for (const row of V) for (const v of row) { const p = v - E0; if (p > hiP) hiP = p; if (p < loP) loP = p; }
    const P = rs.max_profit != null && rs.max_profit > 0 ? rs.max_profit : Math.max(hiP, 1e-9);
    const L = rs.max_loss != null && rs.max_loss < 0 ? -rs.max_loss : Math.max(-loP, 1e-9);
    let sel = 0;
    cols.forEach((ix, k) => { if (ix <= st.di) sel = k; });
    const be = E.breakevensAt(legs, on(), spec.today, spec.r, spec.q, st.ivmult, e, spec.spot).be;
    const ml = ML();

    const thead = el("thead", null, el("tr", null, el("th", { scope: "col", text: "Price" }),
      cd.map((d, k) => {
        const tag = d === spec.today ? "today" : d === front ? "expiry" : evOk && d === spec.event.date ? "earnings" : `${E.days(d, front)} DTE`;
        const th = el("th", { scope: "col", class: [k === sel ? "sel" : "", d === front ? "exp" : "", evOk && d === spec.event.date ? "evt" : ""].join(" ").trim() },
          fmtDate(d), el("small", { text: tag }));
        th.addEventListener("click", () => pickCol(k));
        return th;
      })));
    const tbody = el("tbody");
    const cells = [];
    prices.forEach((S, r) => {
      const isSpot = Math.abs(S - spec.spot) < 1e-9;
      const lower = prices[r + 1];
      const beHere = be.filter((b) => b <= S && (lower == null || b > lower));
      const th = el("th", { scope: "row", class: isSpot ? "spot" : "" }, num(S, S >= 100 ? 1 : 2),
        isSpot ? el("span", { class: "be", text: " spot" }) : null,
        beHere.length ? el("span", { class: "be", title: `Breakeven ${beHere.map((b) => num(b, 2)).join(", ")} on ${fmtDate(on())}`, text: " ◆ BE" }) : null);
      const tr = el("tr", { class: isSpot ? "spotrow" : "" }, th);
      cells.push([]);
      V[r].forEach((v, k) => {
        const pl = v - E0;
        const t = pl > 0 ? pl / P : pl / L;
        const c = heatColor(clamp(t, -1, 1), st.cvd);
        const m = E.metric(st.metric, v, E0, ml);
        const td = el("td", { class: `c${pl < 0 ? " loss" : ""}${c.dark ? " dark" : ""}${k === sel ? " selcol" : ""}`, tabindex: "-1",
          dataset: { r: String(r), k: String(k) }, text: fmtMetric(st.metric, m, true) });
        td.style.backgroundColor = c.fill;
        cells[r].push({ td, S, on: cd[k], v });
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    const [fr, fk] = focusRK && cells[focusRK[0]] && cells[focusRK[0]][focusRK[1]] ? focusRK : [prices.findIndex((p) => Math.abs(p - spec.spot) < 1e-9), sel];
    const start = cells[Math.max(fr, 0)][fk];
    start.td.tabIndex = 0;

    const tbl = el("table", { class: "heat", "aria-label": `${METRICS.find((m) => m.key === st.metric).label} by price and date` }, thead, tbody);
    const rowsFor = (c) => {
      const pl = c.v - E0;
      return [`${num(c.S, 2)} on ${fmtDate(c.on, true)}`,
        ["P/L", money(pl, 0, true)], ["P/L %", E0 ? pct(pl / Math.abs(E0), 1, true) : DASH],
        ["Value", money(c.v, 0)], ["% of max risk", ml == null ? "UNDEFINED" : pct(pl / Math.abs(ml), 1, true)],
        ["vs spot", pct(c.S / spec.spot - 1, 1, true)]];
    };
    const cellOf = (t) => { const td = t.closest && t.closest("td.c"); return td ? cells[+td.dataset.r][+td.dataset.k] : null; };
    tbl.addEventListener("mousemove", (ev2) => { const c = cellOf(ev2.target); if (c) showTip(rowsFor(c), ev2.clientX, ev2.clientY); else hideTip(); });
    tbl.addEventListener("mouseleave", hideTip);
    tbl.addEventListener("focusin", (ev2) => { const c = cellOf(ev2.target); if (c) { const b = c.td.getBoundingClientRect(); showTip(rowsFor(c), b.right, b.bottom); } });
    tbl.addEventListener("focusout", hideTip);
    tbl.addEventListener("click", (ev2) => { const td = ev2.target.closest("td.c"); if (td) { focusRK = [+td.dataset.r, +td.dataset.k]; pickCol(+td.dataset.k); } });
    tbl.addEventListener("keydown", (ev2) => {
      const td = ev2.target.closest("td.c");
      if (!td) return;
      let r = +td.dataset.r, k = +td.dataset.k;
      if (ev2.key === "Enter" || ev2.key === " ") { ev2.preventDefault(); focusRK = [r, k]; pickCol(k); return; }
      if (ev2.key === "ArrowUp") r--; else if (ev2.key === "ArrowDown") r++; else if (ev2.key === "ArrowLeft") k--; else if (ev2.key === "ArrowRight") k++;
      else return;
      ev2.preventDefault();
      const nx = cells[r] && cells[r][k];
      if (nx) { td.tabIndex = -1; nx.td.tabIndex = 0; focusRK = [r, k]; nx.td.focus(); }
    });

    const ramp = (arr, cls) => el("span", { class: "ramp" }, arr.map((h) => { const i = el("i", { class: cls }); i.style.backgroundColor = h; return i; }));
    const lossArr = [...RAMPS.loss].reverse();
    const lg = el("div", { class: "heat-legend" },
      el("span", { text: `Max loss ${ml == null ? "(undefined: table extreme)" : money(ml, 0)}` }), ramp(lossArr, "loss"),
      ramp([RAMPS.zero], ""), ramp(st.cvd ? RAMPS.gainCvd : RAMPS.gain, ""),
      el("span", { text: `Max profit ${rs.max_profit == null ? "(unlimited: table extreme)" : money(rs.max_profit, 0)}` }));
    const tray = el("div", { class: "heat-tray" }, tbl, lg);
    panel.append(tray, el("p", { class: "note" },
      "Each colour arm is scaled to its own extreme (red to the max loss, ", st.cvd ? "blue" : "green",
      " to the max profit); loss cells are also hatched. Click a cell or a date to move the date slider. Model values at mid, ",
      `IV ×${num(st.ivmult, 2)}${evOk ? (st.crush ? ", with the earnings crush" : ", earnings crush off") : ""}. Estimates, not quotes.`));
    if (keepFocus && focusRK) { const c = cells[focusRK[0]] && cells[focusRK[0]][focusRK[1]]; if (c) c.td.focus(); }
  }

  function pickCol(k) {
    st.di = cols[k];
    ui.date.inp.value = String(st.di);
    dateLabel();
    draw();
  }

  // ---- graph (P/L vs price) ------------------------------------------------------------------------
  function priceXs(n = 240) {
    const lo = spec.spot * (1 - st.range / 100), hi = spec.spot * (1 + st.range / 100), xs = [];
    for (let i = 0; i <= n; i++) xs.push(lo + ((hi - lo) * i) / n);
    for (const l of legs) if (l.type !== "S" && l.strike > lo && l.strike < hi) xs.push(l.strike);
    return { lo, hi, xs: xs.sort((a, b) => a - b) };
  }

  function drawGraph() {
    const e = ev(), d = on(), { lo, hi, xs } = priceXs();
    const ml = ML(), f = (V) => E.metric(st.metric, V, E0, ml);
    const base = st.metric === "value" ? E0 : 0;
    const series = [
      { key: "exp", on: front, label: `Expiry ${fmtDate(front)}`, dash: "6 4" },
      { key: "now", on: spec.today, label: "Today" },
      { key: "sel", on: d, label: `${fmtDate(d)} (selected)` },
    ];
    for (const s of series) s.ys = xs.map((x) => f(E.positionValue(legs, x, s.on, spec.today, spec.r, spec.q, st.ivmult, e)));
    const W = 880, H = 340;
    const fr = frame({ w: W, h: H, xd: [lo, hi], yd: extent(series.map((s) => s.ys).concat([[base]]), 0.08), xfmt: priceFmt(lo, hi),
                       yfmt: axisFmt(st.metric), title: `${METRICS.find((m) => m.key === st.metric).label} against the price of ${spec.symbol}` });
    const { root, plot, x, y, box } = fr;
    const yb = clamp(y(base), box.t, box.b);
    const gid = `vz${uid}g`, lid = `vz${uid}l`;
    root.insertBefore(svg("defs", null,
      svg("clipPath", { id: gid }, svg("rect", { x: box.l, y: box.t, width: box.r - box.l, height: Math.max(0, yb - box.t) })),
      svg("clipPath", { id: lid }, svg("rect", { x: box.l, y: yb, width: box.r - box.l, height: Math.max(0, box.b - yb) }))), root.firstChild);
    const sel = series[2];
    let area = `M${x(xs[0]).toFixed(1)},${yb.toFixed(1)}`;
    xs.forEach((v, i) => { if (Number.isFinite(sel.ys[i])) area += `L${x(v).toFixed(1)},${y(sel.ys[i]).toFixed(1)}`; });
    area += `L${x(xs[xs.length - 1]).toFixed(1)},${yb.toFixed(1)}Z`;
    plot.append(svg("path", { d: area, class: "area-gain", "clip-path": `url(#${gid})` }), svg("path", { d: area, class: "area-loss", "clip-path": `url(#${lid})` }));
    if (st.metric === "value") hline(plot, y(base), box, "level", "break-even (value = cost)");
    // levels and spot
    const lv = spec.levels || {};
    const wl = [];
    for (const [k, lab] of [["put_wall", "put wall"], ["call_wall", "call wall"], ["gamma_flip", "flip"]]) {
      const v = lv[k];
      if (v > lo && v < hi) {
        plot.appendChild(svg("line", { x1: x(v), x2: x(v), y1: box.t, y2: box.b, class: "level wall" }));
        wl.push({ x: x(v), text: `${lab} ${num(v, 1)}` });
      }
    }
    vlabels(plot, wl, box.b - 14);
    vline(plot, x(spec.spot), box, "spotline", `spot ${num(spec.spot, 2)}`);
    for (const s of series) plot.appendChild(svg("path", { d: pathOf(xs, s.ys, x, y), class: `ln ${s.key}` }));
    // strikes and breakevens
    for (const l of legs) {
      if (l.type === "S" || !(l.strike > lo && l.strike < hi)) continue;
      const cx = x(l.strike);
      plot.appendChild(svg("path", { d: `M${cx},${box.b - 1}l-5,-8h10z`, class: "strike" }, svg("title", { text: legText(l) })));
    }
    const be = E.breakevensAt(legs, d, spec.today, spec.r, spec.q, st.ivmult, e, spec.spot).be.filter((b) => b > lo && b < hi);
    for (const b of be) {
      plot.appendChild(svg("circle", { cx: x(b), cy: yb, r: 4.5, class: "be-dot" }, svg("title", { text: `Breakeven ${num(b, 2)} on ${fmtDate(d)}` })));
      plot.appendChild(svg("text", { x: x(b), y: yb - 9, "text-anchor": "middle", class: "lbl", text: num(b, 2) }));
    }
    // crosshair
    const cross = svg("line", { class: "cross", x1: -9999, x2: -9999, y1: box.t, y2: box.b });
    const dots = series.map((s) => svg("circle", { r: 4.5, class: `dot ${s.key}`, cx: -9999, cy: -9999 }));
    const hit = svg("rect", { x: box.l, y: box.t, width: box.r - box.l, height: box.b - box.t, class: "hit" });
    root.append(cross, ...dots, hit);
    root.setAttribute("tabindex", "0");
    let cur = spec.spot;
    const show = (S, cx, cy) => {
      cur = clamp(S, lo, hi);
      const px = x(cur);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px);
      const rows = [`${spec.symbol} at ${num(cur, 2)} (${pct(cur / spec.spot - 1, 1, true)})`];
      series.slice().reverse().forEach((s, i) => {
        const val = f(E.positionValue(legs, cur, s.on, spec.today, spec.r, spec.q, st.ivmult, e));
        const dot = dots[series.length - 1 - i];
        dot.setAttribute("cx", px);
        dot.setAttribute("cy", Number.isFinite(val) ? clamp(y(val), box.t, box.b) : -9999);
        rows.push([s.label, fmtMetric(st.metric, val), s.key]);
      });
      showTip(rows, cx, cy);
    };
    const hideX = () => { cross.setAttribute("x1", -9999); cross.setAttribute("x2", -9999); dots.forEach((dd) => dd.setAttribute("cx", -9999)); hideTip(); };
    hit.addEventListener("pointermove", (ev2) => show(x.inv(pointerX(root, ev2, W)), ev2.clientX, ev2.clientY));
    hit.addEventListener("pointerleave", hideX);
    root.addEventListener("keydown", (ev2) => {
      if (ev2.key !== "ArrowLeft" && ev2.key !== "ArrowRight") return;
      ev2.preventDefault();
      cur += ((ev2.key === "ArrowRight" ? 1 : -1) * (hi - lo)) / 60;
      const b = root.getBoundingClientRect();
      show(cur, b.left + (x(clamp(cur, lo, hi)) / W) * b.width, b.top + 20);
    });
    root.addEventListener("blur", hideX);

    panel.append(legend(series.slice().reverse().map((s) => ({ color: SERIES[s.key], label: s.label, dash: s.dash }))), root);
    panel.appendChild(probStrip(d, e, lo, hi, sel.ys, xs, base));
    panel.appendChild(el("p", { class: "note" }, "Model values (Black-Scholes at each leg's IV ×", num(st.ivmult, 2),
      evOk ? (st.crush ? "; the earnings move is added before the print and removed after it" : "; earnings crush off") : "",
      "). Triangles are strikes; dots are breakevens on the selected date. Arrow keys move the crosshair."));
  }

  function probStrip(d, e, lo, hi, ys, xs, base) {
    const T = E.tYears(spec.today, d), sg = sigma * st.ivmult, W = 880, H = 120;
    const box = el("div");
    if (T <= 0) {
      box.appendChild(el("p", { class: "note", text: "Pick a later date to see the price distribution (today the price is known)." }));
      return box;
    }
    const dens = xs.map((v) => E.lognormalPdf(v, spec.spot, T, sg, spec.r, spec.q));
    const mx = Math.max(...dens, 1e-12);
    const fr = frame({ w: W, h: H, m: { t: 8, r: 16, b: 24, l: 58 }, xd: [lo, hi], yd: [0, mx * 1.12], yticks: 1, yfmt: () => "",
                       xfmt: priceFmt(lo, hi), title: "Model price distribution on the selected date" });
    const { root, plot, x, y, box: b } = fr;
    const poly = (from, to) => {
      let p = `M${x(xs[from]).toFixed(1)},${y(0).toFixed(1)}`;
      for (let i = from; i <= to; i++) p += `L${x(xs[i]).toFixed(1)},${y(dens[i]).toFixed(1)}`;
      return `${p}L${x(xs[to]).toFixed(1)},${y(0).toFixed(1)}Z`;
    };
    plot.appendChild(svg("path", { d: poly(0, xs.length - 1), class: "dens" }));
    let start = -1;
    for (let i = 0; i <= xs.length; i++) {
      const up = i < xs.length && ys[i] > base;
      if (up && start < 0) start = i;
      if (!up && start >= 0) { plot.appendChild(svg("path", { d: poly(start, Math.max(start, i - 1)), class: "dens-gain" })); start = -1; }
    }
    vline(plot, x(spec.spot), b, "spotline", null);
    const p = E.pop(legs, spec.spot, d, spec.today, sg, spec.r, spec.q, st.ivmult, e);
    box.append(el("div", { class: "row between" }, el("h4", { text: `Chance of profit on ${fmtDate(d)} ≈ ${pct(p, 0)}` }),
      el("span", { class: "muted", text: `lognormal at the ${fmtDate(front)} ATM IV ${pct(sg, 1)}; real returns have fat tails` })), root);
    return box;
  }

  // ---- over time (Monte Carlo fan) -----------------------------------------------------------------
  function simState() {
    if (sim) return null;
    if (simErr) return errorBox(simErr);
    return loading("Simulating price paths…");
  }

  function planLines(f) {
    const p = sim.plan || {}, out = [];
    if (p.kind === "credit") {
      const C = -E0;
      out.push({ cls: "target", y: f(p.tp * C), label: `target +${pct(p.tp)} of credit` });
      out.push({ cls: "stopl", y: f(-(p.stop - 1) * C), label: `stop ${num(p.stop, 1)}× credit` });
    } else {
      out.push({ cls: "target", y: f(p.tp * E0), label: `target +${pct(p.tp)}` });
      out.push({ cls: "stopl", y: f(-(1 - p.stop) * E0), label: `stop at ${pct(p.stop)} of debit` });
    }
    return out;
  }

  function drawTime() {
    const s = simState();
    if (s) { panel.appendChild(s); return; }
    const e = ev(), ml = ML();
    const f = (pl) => (st.metric === "pl" ? pl : st.metric === "value" ? pl + E0 : st.metric === "pl_pct" ? (E0 ? pl / Math.abs(E0) : NaN) : ml ? pl / Math.abs(ml) : NaN);
    const fan = sim.fan[st.fan];
    const xd = [0, ...sim.days], sd = [spec.today, ...sim.dates];
    const p0 = E.positionValue(legs, spec.spot, spec.today, spec.today, spec.r, spec.q, 1, e) - E0;
    const K = ["p5", "p25", "p50", "p75", "p95"];
    const Y = Object.fromEntries(K.map((k) => [k, [p0, ...fan[k]].map(f)]));
    const flat = sd.map((dd) => f(E.positionValue(legs, spec.spot, dd, spec.today, spec.r, spec.q, 1, e) - E0));
    const pl = planLines(f);
    const W = 880, H = 340;
    const fr = frame({ w: W, h: H, xd: [0, xd[xd.length - 1]], yd: extent([...K.map((k) => Y[k]), flat, pl.map((l) => l.y), [f(0)]], 0.08),
      xfmt: (v) => fmtDate(E.addDays(spec.today, Math.round(v))), yfmt: axisFmt(st.metric),
      title: `Simulated ${METRICS.find((m) => m.key === st.metric).label} over time` });
    const { root, plot, x, y, box } = fr;
    const band = (a, b2, cls) => {
      let d = "";
      xd.forEach((v, i) => { d += `${i ? "L" : "M"}${x(v).toFixed(1)},${y(Y[b2][i]).toFixed(1)}`; });
      for (let i = xd.length - 1; i >= 0; i--) d += `L${x(xd[i]).toFixed(1)},${y(Y[a][i]).toFixed(1)}`;
      plot.appendChild(svg("path", { d: `${d}Z`, class: cls }));
    };
    band("p5", "p95", "band95");
    band("p25", "p75", "band50");
    for (const l of pl) if (Number.isFinite(l.y)) hline(plot, y(l.y), box, l.cls, l.label);
    const p = sim.plan || {};
    if (p.texit && p.texit > spec.today && p.texit <= front) vline(plot, x(E.days(spec.today, p.texit)), box, "texit", `time exit ${fmtDate(p.texit)}`);
    if (e && e.date <= front) vline(plot, x(E.days(spec.today, e.date)), box, "evt", "earnings");
    plot.appendChild(svg("path", { d: pathOf(xd, flat, x, y), class: "ln now thin dash" }));
    plot.appendChild(svg("path", { d: pathOf(xd, Y.p50, x, y), class: "ln ink" }));
    // crosshair: nearest simulated day
    const cross = svg("line", { class: "cross", x1: -9999, x2: -9999, y1: box.t, y2: box.b });
    const dot = svg("circle", { r: 4.5, class: "dot ink", cx: -9999, cy: -9999 });
    const hit = svg("rect", { x: box.l, y: box.t, width: box.r - box.l, height: box.b - box.t, class: "hit" });
    root.append(cross, dot, hit);
    hit.addEventListener("pointermove", (ev2) => {
      const dv = x.inv(pointerX(root, ev2, W));
      let i = 0;
      xd.forEach((v, j) => { if (Math.abs(v - dv) < Math.abs(xd[i] - dv)) i = j; });
      const px = x(xd[i]);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px);
      dot.setAttribute("cx", px); dot.setAttribute("cy", y(Y.p50[i]));
      showTip([`${fmtDate(sd[i], true)} (day ${xd[i]})`, ["95th pct", fmtMetric(st.metric, Y.p95[i])], ["75th pct", fmtMetric(st.metric, Y.p75[i])],
        ["Median", fmtMetric(st.metric, Y.p50[i]), "ink"], ["25th pct", fmtMetric(st.metric, Y.p25[i])], ["5th pct", fmtMetric(st.metric, Y.p5[i])],
        ["Price stays at spot", fmtMetric(st.metric, flat[i]), "now"]], ev2.clientX, ev2.clientY);
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("x1", -9999); cross.setAttribute("x2", -9999); dot.setAttribute("cx", -9999); hideTip(); });

    const tg = el("div", { class: "seg", role: "group", "aria-label": "Paths" },
      ["plan", "hold"].map((k) => el("button", { type: "button", "aria-pressed": st.fan === k ? "true" : "false",
        text: k === "plan" ? "With the exit plan" : "Hold to expiry", onclick: () => { st.fan = k; draw(); } })));
    const lg = legend([{ color: INK.ink, label: "Median path" }, { color: SERIES.now, label: "Price stays at spot", dash: "5 4", width: 1.6 }]);
    lg.append(el("span", { class: "li" }, el("i", { class: "box b50" }), "25-75% of paths"), el("span", { class: "li" }, el("i", { class: "box b95" }), "5-95% of paths"));
    panel.append(el("div", { class: "row between" }, tg, el("span", { class: "muted", text: `${num(sim.n, 0)} paths to ${fmtDate(sim.horizon, true)}` })), lg, root);

    // the underlying itself
    const sp = sim.fan.spot, SY = Object.fromEntries(K.map((k) => [k, [spec.spot, ...sp[k]]]));
    const lvls = [p.below, p.above].filter((v) => v > 0);
    const f2 = frame({ w: W, h: 200, xd: [0, xd[xd.length - 1]], yd: extent([...K.map((k) => SY[k]), lvls, [spec.spot]], 0.06),
      xfmt: (v) => fmtDate(E.addDays(spec.today, Math.round(v))), yfmt: (v) => num(v, 0), title: `Simulated ${spec.symbol} price` });
    const b2 = (a, c, cls) => {
      let d = "";
      xd.forEach((v, i) => { d += `${i ? "L" : "M"}${f2.x(v).toFixed(1)},${f2.y(SY[c][i]).toFixed(1)}`; });
      for (let i = xd.length - 1; i >= 0; i--) d += `L${f2.x(xd[i]).toFixed(1)},${f2.y(SY[a][i]).toFixed(1)}`;
      f2.plot.appendChild(svg("path", { d: `${d}Z`, class: cls }));
    };
    b2("p5", "p95", "band95");
    b2("p25", "p75", "band50");
    f2.plot.appendChild(svg("path", { d: pathOf(xd, SY.p50, f2.x, f2.y), class: "ln ink" }));
    hline(f2.plot, f2.y(spec.spot), f2.box, "spotline", `spot ${num(spec.spot, 2)}`);
    for (const v of lvls) hline(f2.plot, f2.y(v), f2.box, "stopl", `invalidation ${num(v, 2)}`);
    panel.append(el("h4", { class: "muted", text: `${spec.symbol} price paths (same simulation)` }), f2.root,
      el("p", { class: "note" }, sim.labels.join(" "), " ", sim.iv_note, ` Vol source: ${sim.term_source}.`));
  }

  // ---- simulation summary --------------------------------------------------------------------------
  function drawSim() {
    const s = simState();
    if (s) { panel.appendChild(s); return; }
    const col = (k, title) => {
      const x = sim.stats[k];
      const tiles = [kv("Chance of profit", pct(x.p_profit, 0)), kv("Expected P/L", money(x.ev, 0, true)), kv("Median P/L", money(x.median, 0, true)),
        kv("Worst 5% (avg)", money(x.cvar5, 0, true)), kv("5th / 95th pct", `${money(x.p5, 0, true)} / ${money(x.p95, 0, true)}`),
        kv(k === "plan" ? "Median days held" : "Days held", num(x.median_days, 0))];
      const box = el("div", { class: "card" }, el("h3", { class: "card-h", text: title }), el("div", { class: "kvs" }, tiles));
      if (x.exit_mix) {
        const order = [["target", "Target hit"], ["stop", "Stop hit"], ["level", "Price level hit"], ["time", "Time exit"], ["expiry", "Held to expiry"]];
        const bar = el("div", { class: "mix", role: "img", "aria-label": order.map(([kk, l]) => `${l} ${pct(x.exit_mix[kk] || 0, 0)}`).join(", ") });
        for (const [kk] of order) { const v = x.exit_mix[kk] || 0; if (v > 0) { const i = el("i", { class: kk }); i.style.flexGrow = String(v); bar.appendChild(i); } }
        fill(box, el("h4", { class: "muted", text: "How the plan exits" }), bar,
          el("div", { class: "mixleg" }, order.map(([kk, l]) => el("span", null, el("i", { class: kk }), `${l} ${pct(x.exit_mix[kk] || 0, 0)}`))));
      }
      return box;
    };
    panel.append(el("div", { class: "simcols" }, col("plan", "With the exit plan"), col("hold", "Hold to expiry")));
    // histogram
    const h = sim.histogram, counts = h[st.fan], tot = counts.reduce((a, b) => a + b, 0) || 1;
    const W = 880, H = 240;
    const fr = frame({ w: W, h: H, xd: [h.edges[0], h.edges[h.edges.length - 1]], yd: [0, (Math.max(...counts) / tot) * 1.12 || 1],
      xfmt: (v) => moneyK(v, true), yfmt: (v) => pct(v, 0), title: "Distribution of final P/L" });
    counts.forEach((c, i) => {
      const a = h.edges[i], b = h.edges[i + 1], mid = (a + b) / 2;
      const x0 = fr.x(a) + 1, x1 = fr.x(b) - 1, yy = fr.y(c / tot);
      const r = svg("rect", { x: x0, y: yy, width: Math.max(1, x1 - x0), height: Math.max(0, fr.box.b - yy), rx: 2, class: mid >= 0 ? "bar-gain" : "bar-loss" });
      r.addEventListener("pointermove", (ev2) => showTip([`${money(a, 0, true)} to ${money(b, 0, true)}`, ["Share of paths", pct(c / tot, 1)], ["Paths", num(c, 0)]], ev2.clientX, ev2.clientY));
      r.addEventListener("pointerleave", hideTip);
      fr.plot.appendChild(r);
    });
    vline(fr.plot, fr.x(0), fr.box, "zero", "break-even");
    const tg = el("div", { class: "seg", role: "group", "aria-label": "Paths" },
      ["plan", "hold"].map((k) => el("button", { type: "button", "aria-pressed": st.fan === k ? "true" : "false",
        text: k === "plan" ? "With the exit plan" : "Hold to expiry", onclick: () => { st.fan = k; draw(); } })));
    const lg = el("div", { class: "legend" }, el("span", { class: "li" }, el("i", { class: "box gain" }), "profit"), el("span", { class: "li" }, el("i", { class: "box loss" }), "loss"));
    panel.append(el("div", { class: "row between" }, el("h4", { text: "Final P/L across paths (1 lot)" }), tg), lg, fr.root,
      el("p", { class: "note" }, sim.labels.join(" "), " ", sim.iv_note, ` ${num(sim.n, 0)} paths, ${sim.steps} daily steps, vol from ${sim.term_source}. Seed ${sim.seed}.`));
  }

  // ---- greeks --------------------------------------------------------------------------------------
  function drawGreeks() {
    const e = ev(), d = on(), { lo, hi } = priceXs(), xs = [];
    for (let i = 0; i <= 120; i++) xs.push(lo + ((hi - lo) * i) / 120);
    const G = xs.map((v) => E.greeks(legs, v, d, spec.today, spec.r, spec.q, st.ivmult, e));
    const at = E.greeks(legs, spec.spot, d, spec.today, spec.r, spec.q, st.ivmult, e);
    const K = [["delta", "Delta (shares)", (v) => num(v, 1)], ["gamma", "Gamma (delta per $1)", (v) => num(v, 2)],
               ["theta", "Theta ($ per day)", (v) => money(v, 2, true)], ["vega", "Vega ($ per vol point)", (v) => money(v, 2, true)]];
    const grid = el("div", { class: "smalls" });
    for (const [k, title, fm] of K) {
      const ys = G.map((g) => g[k]), W = 440, H = 190, yd = extent([ys, [0]], 0.1), nd = tickDecimals(yd[0], yd[1], 4);
      const fr = frame({ w: W, h: H, m: { t: 10, r: 10, b: 24, l: 58 }, xd: [lo, hi], yd, xfmt: priceFmt(lo, hi), xticks: 4, yticks: 4,
                         yfmt: k === "theta" || k === "vega" ? (v) => (nd ? money(v, nd, true) : moneyK(v, true)) : (v) => num(v, nd), title });
      vline(fr.plot, fr.x(spec.spot), fr.box, "spotline", null);
      fr.plot.appendChild(svg("path", { d: pathOf(xs, ys, fr.x, fr.y), class: "ln sel" }));
      const hit = svg("rect", { x: fr.box.l, y: fr.box.t, width: fr.box.r - fr.box.l, height: fr.box.b - fr.box.t, class: "hit" });
      const dot = svg("circle", { r: 4, class: "dot sel", cx: -9999, cy: -9999 });
      fr.root.append(dot, hit);
      hit.addEventListener("pointermove", (ev2) => {
        const S = clamp(fr.x.inv(pointerX(fr.root, ev2, W)), lo, hi);
        const g = E.greeks(legs, S, d, spec.today, spec.r, spec.q, st.ivmult, e);
        dot.setAttribute("cx", fr.x(S)); dot.setAttribute("cy", fr.y(g[k]));
        showTip([`${spec.symbol} at ${num(S, 2)} on ${fmtDate(d)}`, [title, fm(g[k]), "sel"]], ev2.clientX, ev2.clientY);
      });
      hit.addEventListener("pointerleave", () => { dot.setAttribute("cx", -9999); hideTip(); });
      grid.appendChild(el("div", null, el("h4", { text: title }), fr.root));
    }
    const tbl = el("table", { class: "data" }, el("thead", null, el("tr", null, el("th", { text: `At spot ${num(spec.spot, 2)}, ${fmtDate(d, true)}` }),
      K.map(([, t]) => el("th", { class: "n", text: t })))),
      el("tbody", null, el("tr", null, el("td", { text: "Position (1 lot as entered)" }), K.map(([k, , fm]) => el("td", { class: "n", text: fm(at[k]) })))));
    panel.append(el("p", { class: "muted", text: `Greeks against price on ${fmtDate(d, true)} (move the date slider to see them change).` }), grid,
      el("div", { class: "card" }, tbl),
      el("p", { class: "note", text: "Theta is the model change in value over the next calendar day, so it includes an expiry or the earnings crush when one is due." }));
  }

  // ---- simulation request ----------------------------------------------------------------------------
  function simParams() {
    const e = ev(), p = spec.plan;
    const q = { legs: legs.map(E.encodeLeg).join(","), spot: spec.spot, symbol: spec.symbol || null, event: e ? `${e.date}:${e.move}` : null };
    if (p) Object.assign(q, { tp: p.tp, stop: p.stop, below: p.below, above: p.above, texit: p.texit });
    return q;
  }

  const fetchSim = debounce(async (key, params) => {
    const my = ++simSeq;
    try {
      const r = await api("/api/simulate", params);
      if (my !== simSeq) return;
      sim = r; simErr = null;
    } catch (err) {
      if (my !== simSeq) return;
      sim = null; simErr = err;
    }
    simBusy = false;
    if (!spec.exits) renderExit();
    if (st.tab === "time" || st.tab === "sim") draw();
  }, 600);

  function requestSim() {
    const params = simParams(), key = JSON.stringify(params);
    if (key === simKey && (sim || simBusy)) return;
    simKey = key;
    sim = null; simErr = null; simBusy = true;
    if (front <= spec.today) { simBusy = false; simErr = new Error("The front expiry has passed: nothing to simulate."); return; }
    fetchSim(key, params);
  }

  let spec0Current = spec0;
  setSpec(spec0);

  return {
    update(s) { spec0Current = s; setSpec(s); },
    destroy() { simSeq++; hideTip(); host.remove(); },
    get element() { return host; },
  };
}
