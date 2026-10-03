// engine.js: the browser twin of src/gexscan/analytics/pricing.py (spec v2.2 §6.6). Same conventions, same
// grid, same breakeven rule. Both are pinned to golden.json (an independent scipy reference): Python to 1e-9,
// this file to 1e-6 (tests/js/parity.mjs, and selfTest() on every page load).
//
// Conventions
//   side +1 long, -1 short. Options carry 100 shares per contract; stock legs (type "S") count shares.
//   V = sum(side*qty*mult*price) (what it is worth), E = sum(side*qty*mult*fill) (what it cost, + = debit).
//   P/L $ = V - E, P/L % = P/L / |E|, % of max risk = P/L / |max loss|.
//   T = calendar days / 365. On or after its expiry a leg is worth intrinsic.
//   Dates are ISO strings "YYYY-MM-DD" (they compare correctly as strings).

export const MULT = { C: 100, P: 100, S: 1 };
export const GRID_POINTS = 2001;
export const SLOPE_EPS = 0.01;
export const EVENT_FLOOR = 0.25;
const DAY = 86400000;
const SQRT2PI = Math.sqrt(2 * Math.PI);

// Normal CDF, West (2005) / Hart double-precision algorithm (absolute error ~1e-15).
export function cnd(x) {
  const a = Math.abs(x);
  let c;
  if (a > 37) c = 0;
  else {
    const e = Math.exp(-a * a / 2);
    if (a < 7.07106781186547) {
      let b = 3.52624965998911e-02 * a + 0.700383064443688;
      b = b * a + 6.37396220353165; b = b * a + 33.912866078383; b = b * a + 112.079291497871;
      b = b * a + 221.213596169931; b = b * a + 220.206867912376;
      c = e * b;
      b = 8.83883476483184e-02 * a + 1.75566716318264;
      b = b * a + 16.064177579207; b = b * a + 86.7807322029461; b = b * a + 296.564248779674;
      b = b * a + 637.333633378831; b = b * a + 793.826512519948; b = b * a + 440.413735824752;
      c = c / b;
    } else {
      let b = a + 0.65;
      b = a + 4 / b; b = a + 3 / b; b = a + 2 / b; b = a + 1 / b;
      c = e / b / 2.506628274631;
    }
  }
  return x > 0 ? 1 - c : c;
}

export const npdf = (x) => Math.exp(-0.5 * x * x) / SQRT2PI;
export const days = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / DAY);
export const addDays = (a, n) => new Date(Date.parse(a) + n * DAY).toISOString().slice(0, 10);
export const tYears = (on, expiry) => Math.max(days(on, expiry), 0) / 365;

export function bsm(S, K, T, v, cp, r = 0, q = 0) {
  T = Math.max(T, 1e-6); v = Math.max(v, 1e-4); S = Math.max(S, 1e-12);
  const sq = Math.sqrt(T);
  const d1 = (Math.log(S / K) + (r - q + 0.5 * v * v) * T) / (v * sq), d2 = d1 - v * sq;
  const disc = Math.exp(-r * T), dq = Math.exp(-q * T);
  return cp === "C" ? S * dq * cnd(d1) - K * disc * cnd(d2) : K * disc * cnd(-d2) - S * dq * cnd(-d1);
}

// -> {delta, gamma, vega (per 1 vol point)} for one share of option.
export function bsmGreeks(S, K, T, v, cp, r = 0, q = 0) {
  T = Math.max(T, 1e-6); v = Math.max(v, 1e-4);
  const sq = Math.sqrt(T);
  const d1 = (Math.log(S / K) + (r - q + 0.5 * v * v) * T) / (v * sq);
  const dq = Math.exp(-q * T), n1 = npdf(d1), N1 = cnd(d1);
  return { delta: cp === "C" ? dq * N1 : dq * (N1 - 1), gamma: dq * n1 / (S * v * sq), vega: S * dq * n1 * sq / 100 };
}

// -> {post, floored}. T0 = years from today to the leg's expiry.
export function eventSigma(sigma, T0, move) {
  if (T0 <= 0 || sigma <= 0) return { post: sigma, floored: false };
  const v = (sigma * sigma * T0 - move * move) / T0;
  const lo = (EVENT_FLOOR * sigma) ** 2;
  return v < lo ? { post: EVENT_FLOOR * sigma, floored: true } : { post: Math.sqrt(v), floored: false };
}

// The sigma a leg is valued at on date `on`. An event (earnings: {date, move}) only bites a leg that
// expires after it; before the print sigma rises into it, after the print the event variance is gone.
export function legSigma(leg, on, today, ivmult = 1, event = null) {
  const s = (leg.iv || 0) * ivmult;
  if (!event || leg.type === "S" || !(today < event.date && event.date < leg.expiry)) return s;
  const { post } = eventSigma(s, tYears(today, leg.expiry), event.move);
  if (on >= event.date) return post;
  const T = tYears(on, leg.expiry);
  return T > 0 ? Math.sqrt(post * post + event.move * event.move / T) : post;
}

export function legPrice(leg, S, on, today, r = 0, q = 0, ivmult = 1, event = null) {
  if (leg.type === "S") return S;
  const T = tYears(on, leg.expiry);
  if (T <= 0) return leg.type === "C" ? Math.max(S - leg.strike, 0) : Math.max(leg.strike - S, 0);
  return bsm(Math.max(S, 1e-12), leg.strike, T, legSigma(leg, on, today, ivmult, event), leg.type, r, q);
}

export function positionValue(legs, S, on, today, r = 0, q = 0, ivmult = 1, event = null) {
  let v = 0;
  for (const lg of legs) v += lg.side * lg.qty * MULT[lg.type] * legPrice(lg, S, on, today, r, q, ivmult, event);
  return v;
}

export const netBasis = (legs) => legs.reduce((a, lg) => a + lg.side * lg.qty * MULT[lg.type] * lg.fill, 0);

export function frontExpiry(legs) {
  const ex = legs.filter((l) => l.type !== "S").map((l) => l.expiry).sort();
  return ex.length ? ex[0] : null;
}

// 2,001 points from 0 to 3x the top strike, plus every strike exactly (numpy linspace + unique).
export function priceGrid(legs, spot) {
  const ks = legs.filter((l) => l.type !== "S").map((l) => l.strike);
  const top = 3 * Math.max(...ks, spot || 0, 1);
  const step = top / (GRID_POINTS - 1);
  const xs = [];
  for (let i = 0; i < GRID_POINTS - 1; i++) xs.push(i * step);
  xs.push(top);
  const all = xs.concat(ks).sort((a, b) => a - b);
  return all.filter((x, i) => i === 0 || x !== all[i - 1]);
}

// -> {xs, pl, be}. A breakeven is where "P/L > 0" flips between two grid points, refined by 60 bisections.
export function breakevensAt(legs, on, today, r = 0, q = 0, ivmult = 1, event = null, spot = null) {
  const E = netBasis(legs), xs = priceGrid(legs, spot);
  const pl = xs.map((x) => positionValue(legs, x, on, today, r, q, ivmult, event) - E);
  const up = (x) => positionValue(legs, x, on, today, r, q, ivmult, event) - E > 0;
  const be = [];
  for (let i = 0; i < xs.length - 1; i++) {
    const a = pl[i] > 0;
    if (a !== pl[i + 1] > 0) {
      let lo = xs[i], hi = xs[i + 1];
      for (let k = 0; k < 60; k++) {
        const mid = 0.5 * (lo + hi);
        if (up(mid) === a) lo = mid; else hi = mid;
      }
      be.push(0.5 * (lo + hi));
    }
  }
  return { xs, pl, be };
}

// Max profit / max loss / breakevens at the FRONT expiry (back legs at model value). null = unbounded.
export function riskStats(legs, today, r = 0, q = 0, ivmult = 1, event = null, spot = null) {
  const at = frontExpiry(legs) || today;
  const { xs, pl, be } = breakevensAt(legs, at, today, r, q, ivmult, event, spot);
  const n = xs.length, slope = (pl[n - 1] - pl[n - 2]) / (xs[n - 1] - xs[n - 2]);
  const upP = slope > SLOPE_EPS, upL = slope < -SLOPE_EPS;
  let mx = -Infinity, mn = Infinity;
  for (const v of pl) { if (v > mx) mx = v; if (v < mn) mn = v; }
  return { at, max_profit: upP ? null : mx, max_loss: upL ? null : mn, breakevens: be,
           unbounded_profit: upP, unbounded_loss: upL, net_basis: netBasis(legs) };
}

// ---------------------------------------------------------------------------------------------------
// probability (a model of the future; kept apart from pricing, which is a model of now)

// P(S_T <= x) with S_T = S exp((r - q - sigma^2/2) T + sigma sqrt(T) Z).
export function lognormalCdf(x, S, T, sigma, r = 0, q = 0) {
  if (T <= 0 || sigma <= 0) return x >= S ? 1 : 0;
  if (x <= 0) return 0;
  return cnd((Math.log(x / S) - (r - q - 0.5 * sigma * sigma) * T) / (sigma * Math.sqrt(T)));
}

export function lognormalPdf(x, S, T, sigma, r = 0, q = 0) {
  if (T <= 0 || sigma <= 0 || x <= 0) return 0;
  const sd = sigma * Math.sqrt(T), z = (Math.log(x / S) - (r - q - 0.5 * sigma * sigma) * T) / sd;
  return Math.exp(-0.5 * z * z) / (x * sd * SQRT2PI);
}

// P(P/L > 0 on date `on`), lognormal at `sigma`. An estimate: real returns have fat tails.
export function pop(legs, S, on, today, sigma, r = 0, q = 0, ivmult = 1, event = null) {
  const front = frontExpiry(legs);
  if (front && on > front) on = front;
  const { xs, be } = breakevensAt(legs, on, today, r, q, ivmult, event, S);
  const T = tYears(today, on), E = netBasis(legs);
  const cuts = [0, ...be, Infinity];
  let tot = 0;
  for (let i = 0; i < cuts.length - 1; i++) {
    const a = cuts[i], b = cuts[i + 1];
    const m = Number.isFinite(b) ? 0.5 * (a + b) : Math.max(2 * a, xs[xs.length - 1]);
    if (positionValue(legs, m, on, today, r, q, ivmult, event) - E > 0) {
      tot += (Number.isFinite(b) ? lognormalCdf(b, S, T, sigma, r, q) : 1) - lognormalCdf(a, S, T, sigma, r, q);
    }
  }
  return Math.min(Math.max(tot, 0), 1);
}

// Position Greeks at (S, on): delta (shares), gamma (per $1), vega ($ per vol point), theta ($ per calendar
// day: the change in value over the next day, so it includes an event or an expiry).
export function greeks(legs, S, on, today, r = 0, q = 0, ivmult = 1, event = null) {
  let d = 0, g = 0, v = 0;
  for (const lg of legs) {
    const k = lg.side * lg.qty * MULT[lg.type];
    if (lg.type === "S") { d += k; continue; }
    const T = tYears(on, lg.expiry);
    if (T <= 0) {
      d += k * (lg.type === "C" && S > lg.strike ? 1 : lg.type === "P" && S < lg.strike ? -1 : 0);
      continue;
    }
    const x = bsmGreeks(S, lg.strike, T, Math.max(legSigma(lg, on, today, ivmult, event), 1e-4), lg.type, r, q);
    d += k * x.delta; g += k * x.gamma; v += k * x.vega;
  }
  const th = positionValue(legs, S, addDays(on, 1), today, r, q, ivmult, event)
           - positionValue(legs, S, on, today, r, q, ivmult, event);
  return { delta: d, gamma: g, theta: th, vega: v };
}

// ---------------------------------------------------------------------------------------------------
// legs in the URL: action:type:strike:expiry:qty:fill:iv, e.g. B:C:105:2026-12-11:1:6.30:0.61

const fmtG = (x) => String(Number(Number(x).toPrecision(6)));

export function encodeLeg(l) {
  return [l.side > 0 ? "B" : "S", l.type, fmtG(l.strike || 0), l.expiry || "", fmtG(l.qty), fmtG(l.fill),
          l.iv == null ? "" : fmtG(l.iv)].join(":");
}

export function decodeLeg(code) {
  const p = String(code).split(":");
  if (p.length !== 7 || (p[0] !== "B" && p[0] !== "S")) throw new Error("leg must be action:type:strike:expiry:qty:fill:iv");
  if (!(p[1] in MULT)) throw new Error("leg type must be C, P or S");
  const leg = { side: p[0] === "B" ? 1 : -1, type: p[1], strike: Number(p[2] || 0), expiry: p[3] || null,
                qty: Number(p[4]), fill: Number(p[5]), iv: p[6] === "" ? null : Number(p[6]) };
  if (leg.type !== "S" && (!(leg.strike > 0) || !/^\d{4}-\d{2}-\d{2}$/.test(leg.expiry || ""))) {
    throw new Error("an option leg needs a strike > 0 and an expiry");
  }
  for (const k of ["strike", "qty", "fill"]) if (!Number.isFinite(leg[k])) throw new Error(`bad ${k}`);
  if (leg.iv != null && !Number.isFinite(leg.iv)) throw new Error("bad iv");
  return leg;
}

// ---------------------------------------------------------------------------------------------------
// what the visualizer draws

// The table: V for every (price, date) -> rows[priceIndex][dateIndex].
export function table(legs, prices, dates, today, r = 0, q = 0, ivmult = 1, event = null) {
  return prices.map((S) => dates.map((on) => positionValue(legs, S, on, today, r, q, ivmult, event)));
}

// One P/L-vs-price curve at date `on`.
export function curve(legs, xs, on, today, r = 0, q = 0, ivmult = 1, event = null) {
  const E = netBasis(legs);
  return xs.map((x) => positionValue(legs, x, on, today, r, q, ivmult, event) - E);
}

// metric of a value V: "pl" | "pl_pct" | "value" | "pct_risk"
export function metric(kind, V, E, maxLoss) {
  const pl = V - E;
  if (kind === "pl") return pl;
  if (kind === "value") return V;
  if (kind === "pl_pct") return E ? pl / Math.abs(E) : NaN;
  return maxLoss ? pl / Math.abs(maxLoss) : NaN;
}

// ---------------------------------------------------------------------------------------------------
// self test against golden.json (run by tests/js/parity.mjs and by the page on load)

export function selfTest(golden) {
  const tol = golden.tolerance.js;
  let worst = 0, checked = 0;
  const fails = [];
  const chk = (what, a, b) => {
    checked++;
    if (a === null || b === null) { if (a !== b) fails.push(`${what}: ${a} vs ${b}`); return; }
    const err = Math.abs(a - b) / Math.max(1, Math.abs(b));
    worst = Math.max(worst, err);
    if (!(err <= tol)) fails.push(`${what}: ${a} vs ${b}`);
  };
  for (const c of golden.bsm) {
    const id = `bsm ${c.cp}${c.K}@${c.S}`;
    chk(`${id} price`, bsm(c.S, c.K, c.T, c.sigma, c.cp, c.r, c.q), c.price);
    const x = bsmGreeks(c.S, c.K, c.T, c.sigma, c.cp, c.r, c.q);
    chk(`${id} delta`, x.delta, c.delta); chk(`${id} gamma`, x.gamma, c.gamma); chk(`${id} vega`, x.vega, c.vega);
  }
  for (const e of golden.event_sigma) {
    const r = eventSigma(e.sigma, e.T0, e.move);
    chk(`event_sigma ${e.sigma}`, r.post, e.post);
    if (r.floored !== e.floored) fails.push(`event_sigma ${e.sigma} floored`);
  }
  for (const p of golden.positions) {
    const { legs, today, r, q, event: ev, spot } = p;
    for (const v of p.values) chk(`${p.name} V(${v.S},${v.on},${v.ivmult})`, positionValue(legs, v.S, v.on, today, r, q, v.ivmult, ev), v.V);
    const rs = riskStats(legs, today, r, q, 1, ev, spot), ref = p.risk;
    if (rs.at !== ref.at) fails.push(`${p.name} risk.at`);
    chk(`${p.name} max_profit`, rs.max_profit, ref.max_profit);
    chk(`${p.name} max_loss`, rs.max_loss, ref.max_loss);
    chk(`${p.name} net_basis`, rs.net_basis, ref.net_basis);
    if (rs.breakevens.length !== ref.breakevens.length) fails.push(`${p.name} breakeven count`);
    else rs.breakevens.forEach((b, i) => chk(`${p.name} breakeven ${i}`, b, ref.breakevens[i]));
    chk(`${p.name} pop`, pop(legs, spot, p.pop.on, today, p.pop.sigma, r, q, 1, ev), p.pop.value);
    const g = greeks(legs, p.greeks.S, p.greeks.on, today, r, q, 1, ev);
    for (const k of ["delta", "gamma", "vega", "theta"]) chk(`${p.name} ${k}`, g[k], p.greeks[k]);
  }
  return { ok: fails.length === 0, worst, checked, fails };
}
