// app.js: the router and the pages (spec v2.2 §4). Home (pick a theme) -> Theme -> Ticker -> Options scan ->
// visualizer, plus the Strategy Builder and About. Read-only: the app recommends, it never places orders.

import { el, svg, clear, api, num, money, pct, pctPts, fmtDate, compact, DASH, card, kv, badge, status, loading,
         errorBox, link, showTip, hideTip, fill } from "./ui.js";
import { SERIES, INK, frame, extent, pathOf, sparkline, legend, hline, pointerX } from "./charts.js";
import * as E from "./engine.js";
import { mountViz } from "./viz.js";
import { mountBuilder } from "./builder.js";

const main = document.getElementById("main");
const SYM = /^[A-Z][A-Z0-9.\-]{0,9}$/;
const ALERT_ONLY = "Alert only. You place and manage every order yourself.";
let META = null, THEMES = null, SELFTEST = null, CHECK = null, gen = 0;
const cleanups = [];

// ---------------------------------------------------------------------------------------------------
// router

const ROUTES = [
  [/^\/$/, "home", pageHome],
  [/^\/theme\/([a-z0-9_\-]{1,32})$/, "home", pageTheme],
  [/^\/ticker\/([A-Z][A-Z0-9.\-]{0,9})$/, "home", pageTicker],
  [/^\/scan\/theme\/([a-z0-9_\-]{1,32})$/, "home", (k, q, alive) => pageScan({ theme: k }, q, alive)],
  [/^\/scan\/([A-Z][A-Z0-9.\-]{0,9})$/, "home", (s, q, alive) => pageScan({ symbol: s }, q, alive)],
  [/^\/builder$/, "builder", pageBuilder],
  [/^\/about$/, "about", pageAbout],
];

export function go(href, replace = false) {
  history[replace ? "replaceState" : "pushState"](null, "", href);
  route();
}

function setNav(key) {
  for (const a of document.querySelectorAll("[data-nav]")) {
    const on = a.dataset.nav === key;
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

async function route() {
  const my = ++gen;
  const alive = () => my === gen;
  while (cleanups.length) { try { cleanups.pop()(); } catch { /* already gone */ } }
  hideTip();
  clear(main);
  const path = window.location.pathname, q = new URLSearchParams(window.location.search);
  for (const [re, nav, fn] of ROUTES) {
    const m = path.match(re);
    if (!m) continue;
    setNav(nav);
    try {
      await fn(m[1], q, alive);
    } catch (err) {
      if (alive()) main.appendChild(errorBox(err));
    }
    return;
  }
  setNav("");
  main.appendChild(card("Page not found", el("p", null, "There is nothing at this address. ", link("/", "Back to the themes"), ".")));
}

function onLinkClick(ev) {
  const a = ev.target.closest && ev.target.closest("a[data-link]");
  if (!a || ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
  const u = new URL(a.href, window.location.origin);
  if (u.origin !== window.location.origin) return;
  ev.preventDefault();
  const to = u.pathname + u.search;
  if (to !== window.location.pathname + window.location.search) { go(to); window.scrollTo(0, 0); main.focus({ preventScroll: true }); }
}

// ---------------------------------------------------------------------------------------------------
// chrome: header, footer, search, self-test

function chrome() {
  const mode = document.getElementById("mode"), asof = document.getElementById("asof");
  clear(mode).appendChild(badge(META.mode_label || META.mode, "mode"));
  asof.textContent = META.market && META.market.as_of ? `Data as of ${META.market.as_of}` : "";
  const foot = clear(document.getElementById("foot"));
  fill(foot, el("p", { text: META.disclaimer }), el("p", null, el("b", { text: META.never_orders }), ` gexscan ${META.version}.`));
  const dl = clear(document.getElementById("symlist"));
  for (const s of META.symbols || []) dl.appendChild(el("option", { value: s }));
  const q = document.getElementById("q"), goBtn = document.getElementById("go");
  const open = () => {
    const s = q.value.trim().toUpperCase();
    if (!SYM.test(s)) { q.setCustomValidity("Type a ticker, e.g. NVDA"); q.reportValidity(); return; }
    q.setCustomValidity("");
    q.value = "";
    go(`/ticker/${s}`);
  };
  q.addEventListener("keydown", (ev) => { if (ev.key === "Enter") open(); });
  q.addEventListener("input", () => q.setCustomValidity(""));
  goBtn.addEventListener("click", open);
}

async function selfCheck() {
  const fail = (detail) => {
    const b = document.getElementById("banner");
    b.textContent = `Pricing self-check failed: numbers on this page may be wrong. ${detail}`;
    b.hidden = false;
    document.body.classList.add("selftest-failed");
  };
  try {
    const g = await api("/api/golden");
    SELFTEST = E.selfTest(g);
    if (!SELFTEST.ok) fail(`${SELFTEST.fails.length} of ${SELFTEST.checked} reference values disagree (first: ${SELFTEST.fails[0]}).`);
  } catch (err) {
    SELFTEST = { ok: false, error: String(err.message || err) };
    fail(`The reference values could not be loaded (${SELFTEST.error}).`);
  }
}

async function themes() {
  if (!THEMES) THEMES = (await api("/api/themes")).themes;
  return THEMES;
}

// ---------------------------------------------------------------------------------------------------
// shared bits

const crumbs = (...parts) => el("nav", { class: "crumbs", "aria-label": "Breadcrumb" },
  parts.map((p, i) => [i ? el("span", { class: "sep", "aria-hidden": "true", text: "›" }) : null,
    Array.isArray(p) ? link(p[1], p[0]) : el("span", { "aria-current": "page", text: p })]));

const pill = (x, fmt = (v) => pctPts(v, 1)) =>
  el("span", { class: `pill ${x > 0 ? "up" : x < 0 ? "down" : "flat"}`, text: `${x > 0 ? "▲ " : x < 0 ? "▼ " : ""}${fmt(x)}` });

const levelOf = { warn: "warn", high: "high", crit: "crit", info: "info", ok: "ok" };

function freshLine(text) {
  return el("div", { class: "fresh", text });
}

function pageHead(title, sub, ...actions) {
  return el("div", { class: "page-h" }, el("div", null, el("h1", { text: title }), sub ? el("p", { class: "muted", text: sub }) : null),
    actions.length ? el("div", { class: "row" }, actions) : null);
}

// ---------------------------------------------------------------------------------------------------
// Home: pick a theme

async function pageHome(_m, _q, alive) {
  document.title = "gexscan · pick a theme";
  const hero = el("section", { class: "hero" },
    el("h1", { text: "Pick an AI theme" }),
    el("p", { text: "Theme → ticker (how it has performed, catalysts, risks, outlook) → options scan → visualizer. Or build your own trade in the Strategy Builder." }),
    el("p", { class: "muted", text: "Read-only research. This app never places orders: it shows ideas with an exit plan, and you decide." }),
    el("div", { class: "row" }, link("/builder", "Open the Strategy Builder", { class: "btn" }), link("/about", "How it works", { class: "btn ghost" })));
  main.append(hero, marketStrip());
  const box = el("div", { class: "tiles" }, loading("Loading themes…"));
  main.appendChild(box);
  const list = await themes();
  if (!alive()) return;
  clear(box);
  const fx = META.mode === "fixtures";
  const order = [...list].sort((a, b) => (fx ? (b.kind === "fixtures") - (a.kind === "fixtures") : 0) || (a.kind === "outside") - (b.kind === "outside"));
  for (const t of order) {
    const cls = `tile${t.kind === "fixtures" && fx ? " feature" : ""}${t.kind === "outside" ? " outside" : ""}`;
    const chips = el("div", { class: "chips" }, t.tickers.slice(0, 9).map((s) => el("span", { class: "chip", text: s })),
      t.tickers.length > 9 ? el("span", { class: "chip", text: `+${t.tickers.length - 9}` }) : null);
    box.appendChild(link(`/theme/${t.key}`, [
      el("div", { class: "row between" }, el("h3", { text: t.name }),
        t.kind === "fixtures" ? badge("synthetic", "mode") : t.kind === "outside" ? badge("outside the themes", "medium") : badge(`${t.tickers.length} tickers`, "")),
      t.blurb ? el("p", { class: "muted", text: t.blurb }) : null, chips], { class: cls, "aria-label": `${t.name}: ${t.tickers.length} tickers` }));
  }
  if (fx) main.appendChild(el("p", { class: "note", text: "Fixtures mode: only the synthetic tickers (UPCO, DNCO, PINCO, EARN, RICH, TERM) have data. The real themes are listed so you can see the layout." }));
}

function marketStrip() {
  const m = META.market || {};
  const ts = m.ts_ratio;
  const tiles = [
    kv("VIX", num(m.vix, 2), m.vix_pct_1y != null ? `${num(m.vix_pct_1y, 0)}th percentile, 1 year` : null),
    kv("VIX / VIX3M", num(ts, 3), ts == null ? null : ts < 1 ? "contango (calm)" : "backwardation (stress)"),
    kv("VIX9D", num(m.vix9d, 2), null),
    kv("VVIX", num(m.vvix, 1), "vol of vol"),
  ];
  const ev = (m.events || []).slice(0, 3).map((e) => kv(e.event, fmtDate(e.date), `in ${e.days} days`));
  return el("section", { class: "strip", "aria-label": "Market" }, tiles, ev,
    el("div", { class: "fresh", text: `Market data as of ${m.as_of || DASH} (${Object.values(m.sources || {})[0] || "source n/a"})` }));
}

// ---------------------------------------------------------------------------------------------------
// Theme: its tickers

async function pageTheme(key, _q, alive) {
  const list = await themes();
  if (!alive()) return;
  const t = list.find((x) => x.key === key);
  if (!t) throw new Error(`No theme called "${key}".`);
  document.title = `gexscan · ${t.name}`;
  fill(main, crumbs(["Themes", "/"], t.name),
    pageHead(t.name, t.blurb || `${t.tickers.length} tickers. Pick one for the high-level view, or scan the whole theme for option trades.`,
      link(`/scan/theme/${t.key}`, "Options scan: whole theme", { class: "btn primary big" })));
  if (t.kind === "outside") main.appendChild(status("warn", t.blurb || "Outside the chosen themes: shown only with a high confidence score."));
  if (t.overlaps && t.overlaps.length) main.appendChild(el("p", { class: "muted", text: `Also in other themes: ${t.overlaps.join(", ")}. Ideas on them count toward both themes' risk.` }));
  const grid = el("div", { class: "tcards" });
  main.appendChild(grid);
  const cards = new Map();
  for (const s of t.tickers) {
    const c = link(`/ticker/${s}`, [el("div", { class: "sym", text: s }), loading("")], { class: "tcard", "aria-label": `${s}: open the ticker page` });
    cards.set(s, c);
    grid.appendChild(c);
  }
  const queue = [...t.tickers];
  const worker = async () => {
    while (queue.length && alive()) {
      const s = queue.shift(), c = cards.get(s);
      try {
        const qd = await api("/api/quote", { symbol: s });
        if (!alive()) return;
        if (qd.spot == null) {
          const why = META.mode === "fixtures" ? "Not in the synthetic fixtures: run the app live for this ticker." : (qd.errors || []).join("; ") || "No quote";
          throw new Error(why);
        }
        fill(clear(c), 
          el("div", { class: "row between" }, el("span", { class: "sym", text: s }), pill(qd.chg1d)),
          el("div", { class: "px", text: num(qd.spot, 2) }),
          sparkline(qd.closes || []),
          el("div", { class: "row between muted" }, el("span", { text: `1M ${pctPts(qd.chg1m, 1)}` }),
            el("span", { text: qd.next_earnings ? `Earnings ${fmtDate(qd.next_earnings.date)}` : "No earnings date" })),
          t.overlaps && t.overlaps.includes(s) ? el("span", { class: "chip", text: "in other themes too" }) : null,
          freshLine(`${qd.source}, ${qd.as_of}`));
      } catch (err) {
        if (!alive()) return;
        fill(clear(c), el("div", { class: "sym", text: s }), el("div", { class: "nodata", text: "No data" }), el("div", { class: "muted", text: String(err.message || err) }));
        c.classList.add("nodata");
      }
    }
  };
  await Promise.all([worker(), worker(), worker()]);
}

// ---------------------------------------------------------------------------------------------------
// Ticker: high-level view

async function pageTicker(sym, _q, alive) {
  document.title = `gexscan · ${sym}`;
  const wait = loading(`Loading ${sym}…`);
  main.appendChild(wait);
  const t = await api("/api/ticker", { symbol: sym });
  if (!alive()) return;
  wait.remove();
  const th = (t.themes || [])[0];
  const p = t.profile || {}, perf = t.performance || {}, o = t.options || {}, cat = t.catalysts || {}, out = t.outlook || {};
  main.append(crumbs(["Themes", "/"], ...(th ? [[th.name, `/theme/${th.key}`]] : []), sym));
  const r1d = (perf.returns || []).find((r) => r.window === "1W");
  main.appendChild(el("div", { class: "thead" },
    el("div", null, el("span", { class: "sym", text: sym }), el("span", { class: "muted", text: ` ${p.name || ""}` }),
      el("div", { class: "muted", text: [p.sector, p.industry].filter(Boolean).join(" · ") })),
    el("div", null, el("span", { class: "px", text: num(t.spot, 2) }), r1d ? pill(r1d.ret) : null, el("span", { class: "muted", text: r1d ? " 1 week" : "" })),
    el("div", { class: "cta" },
      link(`/scan/${sym}`, "Options scan", { class: "btn primary big", "aria-label": `Run the options scan for ${sym}` }),
      link(`/builder?symbol=${sym}`, "Open in Strategy Builder", { class: "btn" }))));
  if (!t.in_universe) main.appendChild(status("info", `${sym} is outside the chosen themes: the daily run shows it only above confidence 85.`));
  for (const e of t.errors || []) main.appendChild(status("warn", e));

  // performance
  const ret = el("table", { class: "data" }, el("thead", null, el("tr", null, ["Window", sym, "SPY", "Difference"].map((h, i) => el("th", { class: i ? "n" : "", text: h })))),
    el("tbody", null, (perf.returns || []).map((r) => el("tr", null, el("td", { text: r.window }), el("td", { class: "n" }, pill(r.ret)),
      el("td", { class: "n", text: pctPts(r.spy, 1) }), el("td", { class: "n", text: r.ret != null && r.spy != null ? `${num(r.ret - r.spy, 1, true)} pts` : DASH })))));
  const r52 = el("div", { class: "range52", role: "img", "aria-label": `52-week range ${num(perf.low52, 2)} to ${num(perf.high52, 2)}; now at ${pct(perf.pos52, 0)} of it` },
    el("div", { class: "bar" }), el("i", { class: "mark" }));
  r52.lastChild.style.left = `${Math.min(Math.max((perf.pos52 || 0) * 100, 0), 100)}%`;
  const perfCard = card("How it has performed", priceChart(t), ret,
    el("div", { class: "range52-l" }, el("span", { text: `52w low ${num(perf.low52, 2)}` }), el("span", { text: `${num(perf.from_high_pct, 1)}% from the high` }), el("span", { text: `high ${num(perf.high52, 2)}` })), r52,
    el("div", { class: "kvs" }, kv("Max drawdown, 1y", pctPts(perf.max_drawdown_1y_pct, 1, false)), kv("Realised vol, 20d", pct(perf.rv20, 1)),
      kv("ATR", `${num(perf.atr_pct, 2)}%`, "of price, 14d"), kv("RSI 14", num(perf.rsi14, 0)), kv("Beta vs SPY", num(perf.beta, 2)), kv("Correlation vs SPY", num(t.corr_spy, 2), "60d returns")),
    freshLine(`${perf.source || "?"}, to ${perf.as_of || DASH}`));

  // outlook
  const outCard = card("Outlook",
    el("div", { class: "outlook" }, el("span", { class: `badge ${/constructive|bull/i.test(out.label || "") ? "high" : /weak|bear|caution/i.test(out.label || "") ? "danger" : "medium"}`, text: out.label || DASH }),
      el("span", { class: "muted", text: out.score != null ? `score ${num(out.score, 1, true)}` : "" })),
    el("ul", { class: "reasons" }, (out.reasons || []).map((r) => el("li", null, el("span", { class: `sg ${r.sign === "+" ? "p" : r.sign === "=" ? "z" : "m"}`, "aria-label": r.sign === "+" ? "positive" : r.sign === "=" ? "neutral" : "negative", text: r.sign === "-" ? "−" : r.sign }), r.text))),
    out.range ? el("p", null, `Options price a ${num(out.range.lower, 2)} to ${num(out.range.upper, 2)} range (±${num(out.range.pct, 1)}%) by ${fmtDate(out.range.expiry, true)}.`) : null,
    el("p", { class: "note", text: out.note || "A rules-based read of the data, not a forecast." }));

  // catalysts
  const er = cat.earnings;
  const catCard = card("Catalysts",
    er ? el("div", { class: "kvs" }, kv("Earnings", fmtDate(er.date, true), `in ${er.days} days${er.timing && er.timing !== "unknown" ? `, ${er.timing}` : ""}`),
      kv("Implied move", er.implied_move_pct != null ? `±${num(er.implied_move_pct, 1)}%` : DASH, "priced into options"),
      kv("Past moves", er.hist_move_pct != null ? `±${num(er.hist_move_pct, 1)}%` : DASH, `average of ${er.n_moves || 0}`),
      kv("Implied / past", er.move_ratio != null ? `${num(er.move_ratio, 2)}×` : DASH, er.move_ratio > 1.2 ? "options price more than usual" : er.move_ratio < 0.8 ? "options price less than usual" : "about usual"))
      : el("p", { class: "muted", text: "No earnings date known." }),
    er && er.inside_monthly ? status("warn", "Earnings fall before the monthly expiry: short-premium ideas must handle the gap.") : null,
    cat.ex_dividend ? el("p", null, `Ex-dividend ${fmtDate(cat.ex_dividend.date || cat.ex_dividend, true)}.`) : null,
    el("h4", { text: "Macro calendar" }),
    el("ul", { class: "plain" }, (cat.macro || []).map((m) => el("li", { text: `${m.event}: ${fmtDate(m.date, true)} (in ${m.days} days)` }))),
    el("h4", { text: "News" }),
    (t.news && t.news.length) ? el("ul", { class: "plain" }, t.news.slice(0, 6).map((n) => el("li", null, n.title || n.headline || String(n), n.source ? el("span", { class: "muted", text: ` · ${n.source}` }) : null)))
      : el("p", { class: "muted", text: "No news loaded (offline or no source configured)." }),
    freshLine(`earnings: ${t.fresh && t.fresh.earnings || DASH}`));

  // risks
  const riskCard = card("Risks", (t.risks && t.risks.length) ? t.risks.map((r) => status(levelOf[r.level] || "info", `${r.label}${r.value ? ` (${r.value})` : ""}`))
    : el("p", { class: "muted", text: "No risk flags raised by the rules. That is not the same as no risk." }));

  // options market
  const em = (x) => (x ? `±${num(x.pct, 1)}% (${num(x.lower, 2)} to ${num(x.upper, 2)}) by ${fmtDate(x.expiry)}` : DASH);
  const optCard = card("Options market",
    el("div", { class: "kvs" }, kv("ATM IV", pct(o.atm_iv, 1)), kv("IV rank", num(o.iv_rank, 0), o.iv_rank_source), kv("IV percentile", num(o.iv_percentile, 0)),
      kv("IV − RV20", o.vrp != null ? `${num(o.vrp, 1, true)} pts` : DASH, o.vrp > 0 ? "options rich vs realised" : "options cheap vs realised"),
      kv("Expected move, front", em(o.em_front)), kv("Expected move, monthly", em(o.em_monthly)),
      kv("Term structure", o.term_shape || DASH, o.ts_ratio != null ? `front/back ${num(o.ts_ratio, 2)}` : null), kv("25Δ risk reversal", o.rr25 != null ? `${num(o.rr25, 1, true)} pts` : DASH, "put IV − call IV"),
      kv("Dealer gamma", (o.gex_regime || DASH).replace(/_/g, " "), o.net_gex != null ? `net ${compact(o.net_gex)} $/1%` : null),
      kv("Call wall", num(o.call_wall, 2)), kv("Put wall", num(o.put_wall, 2)), kv("Gamma flip", num(o.gamma_flip, 2)),
      kv("Put/call OI", num(o.pc_oi_ratio, 2)), kv("Max pain", num(o.max_pain, 2)), kv("ATM spread", o.atm_spread_pct != null ? pct(o.atm_spread_pct, 1) : DASH, "of mid")),
    termChart(o.term || []),
    freshLine(t.fresh ? t.fresh.chain : ""));

  const coCard = card("Company",
    el("p", { text: p.summary || "No profile." }),
    el("div", { class: "kvs" }, kv("Market cap", p.market_cap ? `$${compact(p.market_cap)}` : DASH), kv("Employees", p.employees ? num(p.employees, 0) : DASH),
      kv("Themes", (t.themes || []).map((x) => x.name).join(", ") || "none")),
    freshLine(p.source || ""));

  const freshCard = card("Data freshness", el("ul", { class: "plain" }, Object.entries(t.fresh || {}).map(([k, v]) => el("li", null, el("b", { text: `${k}: ` }), v))));
  main.append(el("div", { class: "grid two" }, perfCard, el("div", { class: "stack" }, outCard, riskCard)),
    el("div", { class: "grid two" }, optCard, el("div", { class: "stack" }, catCard, coCard, freshCard)),
    el("div", { class: "cta center" }, link(`/scan/${sym}`, `Options scan for ${sym}`, { class: "btn primary big" })));
}

function priceChart(t) {
  const s = (t.performance || {}).series;
  if (!s || !s.dates || s.dates.length < 2) return el("p", { class: "muted", text: "No price history." });
  const o = t.options || {}, emx = o.em_monthly;
  const d0 = s.dates[0], X = s.dates.map((d) => E.days(d0, d));
  const xEnd = emx ? Math.max(X[X.length - 1], E.days(d0, emx.expiry)) : X[X.length - 1];
  const levels = [["call_wall", "call wall"], ["put_wall", "put wall"], ["gamma_flip", "flip"]].filter(([k]) => (t.levels || {})[k]);
  const ys = [s.close, s.sma50, s.sma200, levels.map(([k]) => t.levels[k])];
  if (emx) ys.push([emx.lower, emx.upper]);
  const W = 760, H = 280;
  const fr = frame({ w: W, h: H, xd: [0, xEnd], yd: extent(ys, 0.05), xfmt: (v) => fmtDate(E.addDays(d0, Math.round(v))), yfmt: (v) => num(v, 0),
                     title: `${t.symbol} close with 50- and 200-day averages` });
  const { root, plot, x, y, box } = fr;
  // lines first, then labels top-down with a 12px minimum gap so close levels don't overprint
  let prev = -Infinity;
  for (const [k, lab] of [...levels].sort((a, b) => y(t.levels[a[0]]) - y(t.levels[b[0]]))) {
    const ly = y(t.levels[k]);
    hline(plot, ly, box, "level wall", null);
    prev = Math.max(ly - 4, prev + 12);
    plot.appendChild(svg("text", { x: box.r - 3, y: prev, "text-anchor": "end", class: "lbl level wall", text: `${lab} ${num(t.levels[k], 1)}` }));
  }
  if (emx) {
    const x0 = X[X.length - 1], x1 = E.days(d0, emx.expiry), sp = t.spot;
    let up = "", dn = "";
    for (let i = 0; i <= 20; i++) {
      const f = i / 20, xx = x0 + (x1 - x0) * f;
      up += `${i ? "L" : "M"}${x(xx).toFixed(1)},${y(sp + (emx.upper - sp) * Math.sqrt(f)).toFixed(1)}`;
      dn = `L${x(xx).toFixed(1)},${y(sp + (emx.lower - sp) * Math.sqrt(f)).toFixed(1)}` + dn;
    }
    plot.appendChild(svg("path", { d: `${up}${dn}Z`, class: "cone" }, svg("title", { text: `Options-implied range ${num(emx.lower, 2)} to ${num(emx.upper, 2)} by ${emx.expiry}` })));
  }
  plot.appendChild(svg("path", { d: pathOf(X, s.sma200, x, y), class: "ln exp dash" }));
  plot.appendChild(svg("path", { d: pathOf(X, s.sma50, x, y), class: "ln sel dash2" }));
  plot.appendChild(svg("path", { d: pathOf(X, s.close, x, y), class: "ln ink" }));
  const cross = svg("line", { class: "cross", x1: -9999, x2: -9999, y1: box.t, y2: box.b });
  const dot = svg("circle", { r: 4, class: "dot ink", cx: -9999, cy: -9999 });
  const hit = svg("rect", { x: box.l, y: box.t, width: box.r - box.l, height: box.b - box.t, class: "hit" });
  root.append(cross, dot, hit);
  hit.addEventListener("pointermove", (ev) => {
    const dv = x.inv(pointerX(root, ev, W));
    let i = 0;
    for (let j = 0; j < X.length; j++) if (Math.abs(X[j] - dv) < Math.abs(X[i] - dv)) i = j;
    cross.setAttribute("x1", x(X[i])); cross.setAttribute("x2", x(X[i]));
    dot.setAttribute("cx", x(X[i])); dot.setAttribute("cy", y(s.close[i]));
    showTip([fmtDate(s.dates[i], true), ["Close", num(s.close[i], 2), "ink"], ["50-day", num(s.sma50[i], 2), "sel"], ["200-day", num(s.sma200[i], 2), "exp"]], ev.clientX, ev.clientY);
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("x1", -9999); cross.setAttribute("x2", -9999); dot.setAttribute("cx", -9999); hideTip(); });
  const lg = legend([{ color: INK.ink, label: "Close" }, { color: SERIES.sel, label: "50-day", dash: "2 3" }, { color: SERIES.exp, label: "200-day", dash: "6 4" }]);
  if (emx) lg.appendChild(el("span", { class: "li" }, el("i", { class: "box b95" }), `Implied range to ${fmtDate(emx.expiry)}`));
  return el("div", null, lg, root);
}

function termChart(term) {
  if (term.length < 2) return null;
  const W = 520, H = 170, X = term.map((r) => r.dte), Y = term.map((r) => r.atm_iv);
  const fr = frame({ w: W, h: H, m: { t: 12, r: 12, b: 26, l: 48 }, xd: [0, Math.max(...X) * 1.04], yd: extent([Y], 0.15), xfmt: (v) => `${num(v, 0)}d`, yfmt: (v) => pct(v, 0),
                     xticks: 5, yticks: 4, title: "ATM implied vol by days to expiry" });
  fr.plot.appendChild(svg("path", { d: pathOf(X, Y, fr.x, fr.y), class: "ln now" }));
  term.forEach((r) => {
    const c = svg("circle", { cx: fr.x(r.dte), cy: fr.y(r.atm_iv), r: 4, class: "dot now" });
    c.addEventListener("pointermove", (ev) => showTip([fmtDate(r.expiry, true), ["ATM IV", pct(r.atm_iv, 1), "now"], ["Days", num(r.dte, 0)], ["Expected move", r.em_pct != null ? `±${num(r.em_pct, 1)}%` : DASH]], ev.clientX, ev.clientY));
    c.addEventListener("pointerleave", hideTip);
    fr.plot.appendChild(c);
  });
  return el("div", null, el("h4", { text: "Term structure" }), fr.root);
}

// ---------------------------------------------------------------------------------------------------
// Options scan -> visualizer

function scanParams(q) {
  const o = {};
  for (const k of ["strategies", "dte_min", "dte_max", "min_confidence", "top", "defined_only"]) if (q.get(k) != null && q.get(k) !== "") o[k] = q.get(k);
  return o;
}

async function pageScan(target, q, alive) {
  const list = await themes();
  if (!alive()) return;
  const th = target.theme ? list.find((x) => x.key === target.theme) : null;
  if (target.theme && !th) throw new Error(`No theme called "${target.theme}".`);
  const what = th ? th.name : target.symbol;
  document.title = `gexscan · scan ${what}`;
  fill(main, th ? crumbs(["Themes", "/"], [th.name, `/theme/${th.key}`], "Options scan") : crumbs(["Themes", "/"], [target.symbol, `/ticker/${target.symbol}`], "Options scan"),
    pageHead(`Options scan: ${what}`, "The engine checks every strategy against its gates, sizes it to your risk budget and ranks it by confidence. Pick an idea to visualize it.",
      target.symbol ? link(`/builder?symbol=${target.symbol}`, "Build your own instead", { class: "btn" }) : null));
  const params = scanParams(q);
  main.appendChild(scanControls(params, (np) => {
    const u = new URLSearchParams(np);
    go(`${window.location.pathname}${u.toString() ? `?${u}` : ""}`, true);
  }));
  const res = el("div", null, loading(th ? `Scanning ${th.tickers.length} tickers for the best trades…` : `Scanning ${target.symbol} for the best trades…`));
  main.appendChild(res);
  const s = await api("/api/scan", { ...target, ...params });
  if (!alive()) return;
  clear(res);
  for (const e of s.errors || []) res.appendChild(status("warn", typeof e === "string" ? e : `${e.symbol || ""}: ${e.error || JSON.stringify(e)}`));
  for (const w of s.warnings || []) res.appendChild(status("warn", w));
  res.appendChild(el("div", { class: "warnbox" }, el("b", { text: s.alert_only || ALERT_ONLY }), " ", s.not_saved || ""));
  const ideas = s.ideas || [];
  if (!ideas.length) {
    res.appendChild(card("No idea passed the gates", el("p", { text: "Nothing cleared the rules today. That is a valid answer: no trade is a position too. See why below." })));
  }
  const listBox = el("div", { class: "ideas", "aria-label": "Trade ideas" });
  const vizHost = el("div", { id: "viz" });
  const detail = el("div");
  let vz = null;
  cleanups.push(() => vz && vz.destroy());
  const pick = (i, scroll) => {
    [...listBox.children].forEach((b, j) => b.setAttribute("aria-pressed", j === i ? "true" : "false"));
    const idea = ideas[i];
    const spec = specFromIdea(idea, s);
    const href = builderHref(idea);
    if (vz) vz.destroy();
    clear(vizHost);
    vz = mountViz(vizHost, spec, { builderHref: href });
    clear(detail).appendChild(ideaDetail(idea));
    const u = new URLSearchParams(window.location.search);
    u.set("pick", String(i));
    history.replaceState(null, "", `${window.location.pathname}?${u}`);
    if (scroll) vizHost.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  ideas.forEach((idea, i) => listBox.appendChild(ideaCard(idea, () => pick(i, true))));
  if (ideas.length) res.append(el("h2", { text: `${ideas.length} idea${ideas.length > 1 ? "s" : ""}, best first` }), listBox, vizHost, detail);
  const pk = Number(q.get("pick"));
  if (ideas.length) pick(Number.isInteger(pk) && pk >= 0 && pk < ideas.length ? pk : 0, false);

  if (s.rejected && s.rejected.length) {
    res.appendChild(el("details", { class: "card" }, el("summary", { text: `Rejected or cut (${s.rejected.length})` }),
      el("table", { class: "data" }, el("thead", null, el("tr", null, ["Ticker", "Idea", "Why not", "Confidence"].map((h, i) => el("th", { class: i === 3 ? "n" : "", text: h })))),
        el("tbody", null, s.rejected.map((r) => el("tr", null, el("td", { text: r.symbol }), el("td", { text: r.summary || r.strategy }), el("td", { text: r.why }),
          el("td", { class: "n", text: r.confidence != null ? num(r.confidence, 0) : DASH })))))));
  }
  const ex = Object.entries(s.explain || {});
  if (ex.length) {
    res.appendChild(el("details", { class: "card" }, el("summary", { text: "Strategy-by-strategy explanation" }),
      ex.map(([k, lines]) => el("div", null, el("h4", { text: k }), el("ul", { class: "plain" }, lines.map((l) => el("li", { text: l })))))));
  }
  const f = s.filters || {};
  res.appendChild(el("p", { class: "note", text: `Filters: confidence ≥ ${f.min_confidence ?? DASH} (floor ${f.floor ?? DASH}), top ${f.top_n ?? DASH}, at most ${f.max_per_ticker ?? DASH} per ticker, ${s.defined_only ? "defined risk only" : "undefined risk allowed"}. Generated ${s.generated || DASH}; ${s.disclaimer || ""}` }));
}

function scanControls(params, apply) {
  const box = el("section", { class: "card controls", "aria-label": "Scan settings" });
  const chosen = new Set((params.strategies || "").split(",").filter(Boolean));
  const chips = el("div", { class: "chips", role: "group", "aria-label": "Strategies" });
  for (const st of META.strategies || []) {
    const b = el("button", { type: "button", class: "chip", "aria-pressed": chosen.has(st.key) ? "true" : "false", text: st.label });
    b.addEventListener("click", () => { if (chosen.has(st.key)) chosen.delete(st.key); else chosen.add(st.key); b.setAttribute("aria-pressed", chosen.has(st.key) ? "true" : "false"); });
    chips.appendChild(b);
  }
  const numIn = (label, key, min, max, ph) => {
    const i = el("input", { type: "number", min, max, step: 1, value: params[key] ?? "", placeholder: ph, "aria-label": label });
    return [el("label", { class: "field" }, el("span", { text: label }), i), i];
  };
  const [f1, dmin] = numIn("Min DTE", "dte_min", 0, 400, "auto");
  const [f2, dmax] = numIn("Max DTE", "dte_max", 0, 400, "auto");
  const [f3, minc] = numIn("Min confidence", "min_confidence", 0, 100, "default");
  const [f4, top] = numIn("Show top", "top", 1, 30, "default");
  const locked = !META.allow_undefined_risk;
  const def = el("input", { type: "checkbox" });
  def.checked = locked || params.defined_only !== "0";
  def.disabled = locked;
  const defL = el("label", { class: `chk${locked ? " locked" : ""}`, title: locked ? "Undefined-risk structures are off in the config (engine.allow_undefined_risk)" : null }, def,
    locked ? "Defined risk only (locked by config)" : "Defined risk only");
  const run = el("button", { type: "button", class: "btn primary", text: "Run scan" });
  run.addEventListener("click", () => {
    const np = {};
    if (chosen.size) np.strategies = [...chosen].join(",");
    for (const [k, i] of [["dte_min", dmin], ["dte_max", dmax], ["min_confidence", minc], ["top", top]]) if (i.value !== "") np[k] = i.value;
    if (!locked && !def.checked) np.defined_only = "0";
    apply(np);
  });
  const reset = el("button", { type: "button", class: "btn ghost", text: "Reset" });
  reset.addEventListener("click", () => apply({}));
  box.append(el("div", { class: "row between" }, el("h3", { class: "card-h", text: "Scan settings" }), el("span", { class: "muted", text: chosen.size ? "" : "All strategies" })),
    chips, el("div", { class: "row" }, f1, f2, f3, f4, defL, el("span", { class: "spacer" }), reset, run));
  return box;
}

function ideaCard(idea, onPick) {
  const c = idea.confidence || {};
  const head = el("div", { class: "head" }, el("span", { class: "sym", text: idea.symbol }), el("span", { class: "strat", text: idea.strategy_label || idea.label }),
    el("span", { class: "spacer" }), badge(`${c.bucket || ""} ${num(c.score, 0)}`.trim(), (c.bucket || "").toLowerCase()),
    idea.risk === "undefined" ? badge("UNDEFINED RISK", "danger") : null, idea.outside ? badge("outside themes", "medium") : null);
  const legs = el("div", { class: "legs" }, (idea.legs || []).map((l) => el("div", { class: l.action === "BUY" ? "B" : "S" },
    el("span", { class: "act", text: l.action }), `${l.qty > 1 ? `${l.qty}× ` : ""}${l.type === "S" ? "shares" : `${num(l.strike, l.strike % 1 ? 1 : 0)}${l.type} ${fmtDate(l.expiry)}`} @ ${num(l.mid, 2)}`)));
  const net = idea.net;
  const mini = el("div", { class: "mini" },
    kv(idea.kind === "credit" ? "Credit" : "Debit", money(Math.abs(net) * 100, 0), "per lot"),
    kv("Max profit", idea.max_profit == null ? "Unlimited" : money(idea.max_profit, 0)),
    kv("Max loss", idea.max_loss == null ? "UNDEFINED" : money(idea.max_loss, 0)),
    kv("Chance of profit", pct(idea.pop, 0), "model"),
    kv("DTE", num(idea.dte, 0)),
    kv("Size", `${idea.contracts || 0} lots`, "to your risk budget"));
  const stop = String(idea.stop || "").replace(/\s*No rolling\.?\s*$/i, "");
  // a card, not a <button>: it holds block content. Keyboard: Enter or Space picks it.
  const box = el("div", { class: "card idea", role: "button", tabindex: "0", "aria-pressed": "false", "aria-label": `Visualize: ${idea.summary}`, onclick: onPick },
    head, legs, mini, el("div", { class: "exitline" }, el("b", { text: "Exit: " }), `${idea.take_profit}. Stop: ${stop}`),
    (idea.themes || []).length ? el("div", { class: "chips" }, idea.themes.map((t) => el("span", { class: "chip", text: t.name }))) : null);
  box.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); onPick(); } });
  return box;
}

function specFromIdea(idea, scan) {
  const v = idea.viz || {};
  const rt = idea.metrics && idea.metrics.roundtrip_cost != null ? idea.metrics.roundtrip_cost * 100 : null;
  return { ...v, label: idea.strategy_label || idea.label, as_of: idea.as_of, roundtrip: rt,
           exits: { stop: idea.stop, target: idea.take_profit, time: idea.time_stop, invalidation: idea.invalidation },
           alertOnly: scan.alert_only, notSaved: scan.not_saved, fillNote: "per 1 lot, at mid" };
}

function builderHref(idea) {
  const v = idea.viz || {};
  const u = new URLSearchParams({ symbol: idea.symbol, legs: (v.legs || []).join(",") });
  return `/builder?${u}`;
}

function ideaDetail(idea) {
  const r = idea.rationale || {};
  const lst = (title, arr) => (arr && arr.length ? [el("h4", { text: title }), el("ul", { class: "plain" }, arr.map((x) => el("li", { text: x })))] : null);
  const why = card("Why this trade", lst("Why the premium is there", r.why_premium), lst("What the market is pricing", r.market_pricing),
    lst("Second-order effects", r.second_order), r.invalidation ? [el("h4", { text: "What would prove it wrong" }), el("p", { text: r.invalidation })] : null);
  const stop = String(idea.stop || "").replace(/\s*No rolling\.?\s*$/i, "");
  const exit = card("Exit plan (decided before entry)",
    el("div", { class: "kvs" }, kv("Take profit", idea.take_profit), kv("Stop", stop), kv("Time exit", idea.time_stop), kv("Invalidation", idea.invalidation),
      kv("Adjustments", "No rolling.")),
    el("p", { class: "note", text: idea.stop_rule || "" }));
  const d = (idea.gates || {}).decisions || [];
  const gates = card("Gates", el("div", { class: "gates" }, d.map((g) => el("div", { class: `gate ${g.outcome === "pass" ? "pass" : g.outcome === "fail" ? "fail" : "warn"}` },
    el("b", { text: `${g.outcome === "pass" ? "✔" : g.outcome === "fail" ? "✖" : "▲"} ${g.gate}` }), el("span", { class: "muted", text: ` ${g.kind}, wants ${g.want}` }), el("div", { text: g.detail || g.state || "" })))));
  const c = idea.confidence || {};
  const sub = Object.entries(c.sub || {});
  const conf = card(`Confidence ${num(c.score, 0)} (${c.bucket || DASH})`,
    el("table", { class: "data" }, el("tbody", null, sub.map(([k, v]) => el("tr", null, el("td", { text: k.replace(/_/g, " ") }),
      el("td", { class: "n", text: `${num(v.points, 1)} / ${num(v.max, 0)}` }), el("td", { class: "muted", text: v.notes || "" }))))),
    (c.penalties || []).length ? el("ul", { class: "plain" }, c.penalties.map((p) => el("li", { text: typeof p === "string" ? p : `${p.name || p.key}: ${num(p.points, 1)}` }))) : null);
  const notes = [...(idea.warnings || []).map((w) => status("warn", w)), ...(idea.flags || []).map((w) => status("info", w)), ...(idea.notes || []).map((w) => status("info", w))];
  return el("div", { class: "grid two" }, el("div", { class: "stack" }, why, notes.length ? card("Flags and notes", notes) : null),
    el("div", { class: "stack" }, exit, gates, conf));
}

// ---------------------------------------------------------------------------------------------------
// Strategy Builder and About

async function pageBuilder(_m, q, alive) {
  document.title = "gexscan · Strategy Builder";
  const b = mountBuilder(main, { meta: META, symbol: q.get("symbol"), legs: q.get("legs"), alive });
  cleanups.push(() => b.destroy());
}

async function pageAbout(_m, _q, alive) {
  document.title = "gexscan · about";
  const checkKv = (st) => kv("Pricing self-check", st == null ? "running" : st.ok ? `passed (${st.checked} values)` : "FAILED",
    st && st.ok ? `worst error ${num(st.worst * 100, 4)}%` : null);
  const slot = checkKv(SELFTEST);
  fill(main, pageHead("How it works", "A local, read-only research site for options on AI and tech themes."),
    el("div", { class: "grid two" },
      card("The flow", el("ol", null,
        el("li", { text: "Pick a theme on the home page (GPUs, memory, optics, power and so on)." }),
        el("li", { text: "Pick a ticker: how it has performed, catalysts, risks and a rules-based outlook." }),
        el("li", { text: "Press Options scan: every strategy runs through its gates, is sized to your risk budget and ranked by confidence." }),
        el("li", { text: "Pick an idea: the visualizer shows P/L by price and date, P/L curves, a Monte Carlo of the exit plan and the greeks." }),
        el("li", { text: "Or open the Strategy Builder, choose any ticker and set up calls, puts, spreads, calendars, condors and more." }))),
      card("Rules that never bend", el("ul", { class: "plain" },
        el("li", null, el("b", { text: "No orders. " }), META.never_orders),
        el("li", null, el("b", { text: "Exit plan first. " }), "Short premium: close when the cost to close reaches 2× the credit (a loss of 1× the credit), take profit at a set share of the credit, time exit. Debit trades: max loss is the debit, plus a time stop and an invalidation price."),
        el("li", null, el("b", { text: "Adjustments: " }), "No rolling."),
        el("li", null, el("b", { text: "Defined risk by default. " }), "Anything with unlimited loss is labelled UNDEFINED RISK."),
        el("li", null, el("b", { text: "Estimates. " }), "Probabilities, GEX and simulations are models on delayed data, not forecasts.")))),
    el("div", { class: "grid two" },
      card("How the numbers are made", el("ul", { class: "plain" },
        el("li", { text: "Option values: Black-Scholes at each leg's implied vol, with an earnings move added before the print and removed after it." }),
        el("li", { text: "Chance of profit: a lognormal at the market's at-the-money implied vol." }),
        el("li", { text: "Simulation: thousands of daily price paths at the term structure of implied vol, applying the exit plan path by path." }),
        el("li", { text: "The browser and the server share one set of reference values (golden.json); the page checks itself against them on load." }),
        el("li", { text: "The full method, formulas and limits are in docs/specs/v2.2-visualizer-spec.md; how this site was built and tested is in docs/PROCESS.md (both in the repo)." }))),
      card("This session", el("div", { class: "kvs" }, kv("Mode", META.mode_label || META.mode), kv("Version", META.version), kv("Data as of", (META.market || {}).as_of || DASH),
        slot,
        kv("Risk-free rate", pct(META.r, 2)), kv("Simulation paths", num(META.paths, 0))))),
    el("p", { class: "note", text: META.disclaimer }));
  if (SELFTEST == null && CHECK) {           // landed here first: fill the result in when the check finishes
    await CHECK;
    if (alive()) slot.replaceWith(checkKv(SELFTEST));
  }
}

// ---------------------------------------------------------------------------------------------------
// boot

async function boot() {
  document.addEventListener("click", onLinkClick);
  window.addEventListener("popstate", route);
  try {
    META = await api("/api/meta");
  } catch (err) {
    main.appendChild(errorBox(err));
    return;
  }
  chrome();
  CHECK = selfCheck();
  route();
}

boot();
