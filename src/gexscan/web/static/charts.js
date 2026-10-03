// charts.js: SVG primitives (scales, ticks, axes) and the heatmap colour ramps (spec v2.2 §6.9).
// One y axis per chart, recessive grid, thin marks, 2 px lines. Colours are tokens: text never wears a series colour.

import { svg, num, MINUS } from "./ui.js";

export const SERIES = { now: "#1f6fcf", sel: "#c4501a", exp: "#0d8a5c" };
export const INK = { ink: "#15181c", ink2: "#3a3f46", muted: "#5a6068", grid: "#c3c7cd", axis: "#8d939b" };

// ---------------------------------------------------------------------------------------------------
// scales and ticks

export function lin(d0, d1, r0, r1) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  const f = (x) => r0 + (x - d0) * k;
  f.inv = (y) => (k ? d0 + (y - r0) / k : d0);
  f.domain = [d0, d1];
  f.range = [r0, r1];
  return f;
}

export function niceStep(span, count) {
  const raw = Math.abs(span) / Math.max(count, 1);
  if (!(raw > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(raw)), m = raw / p;
  return (m < 1.5 ? 1 : m < 2.25 ? 2 : m < 3.5 ? 2.5 : m < 7.5 ? 5 : 10) * p;
}

// decimals the tick labels need so neighbours never print the same (step 2.5 -> 1, 0.05 -> 2)
export function tickDecimals(lo, hi, count = 5) {
  const st = niceStep(hi - lo, count);
  let d = 0;
  while (d < 6 && Math.abs(Math.round(st * 10 ** d) - st * 10 ** d) > 1e-6) d++;
  return d;
}

export function ticks(lo, hi, count = 5) {
  const st = niceStep(hi - lo, count), out = [];
  for (let v = Math.ceil(lo / st) * st; v <= hi + st * 1e-9; v += st) out.push(Math.abs(v) < st * 1e-9 ? 0 : v);
  return out;
}

export function extent(arrs, pad = 0.06, includeZero = false) {
  let lo = Infinity, hi = -Infinity;
  for (const a of arrs) for (const v of a) if (Number.isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (includeZero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
  if (lo === hi) { lo -= 1; hi += 1; }
  const p = (hi - lo) * pad;
  return [lo - p, hi + p];
}

export function pathOf(xs, ys, x, y) {
  let d = "", pen = false;
  for (let i = 0; i < xs.length; i++) {
    const v = ys[i];
    if (!Number.isFinite(v)) { pen = false; continue; }
    d += `${pen ? "L" : "M"}${x(xs[i]).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  }
  return d;
}

// ---------------------------------------------------------------------------------------------------
// a chart frame: <svg viewBox> with plot box, y grid + labels, x labels. Returns {root, plot, x, y, box}.

export function frame({ w = 760, h = 300, m = { t: 14, r: 16, b: 28, l: 58 }, xd, yd, xfmt = (v) => num(v, 0),
                        yfmt = (v) => num(v, 0), xticks = 6, yticks = 5, cls = "chart", title = "", xTickVals = null }) {
  const root = svg("svg", { viewBox: `0 0 ${w} ${h}`, class: cls, role: "img", "aria-label": title || "chart",
                            preserveAspectRatio: "xMidYMid meet" });
  const x = lin(xd[0], xd[1], m.l, w - m.r), y = lin(yd[0], yd[1], h - m.b, m.t);
  const g = svg("g", { class: "axes" });
  for (const v of ticks(yd[0], yd[1], yticks)) {
    g.appendChild(svg("line", { x1: m.l, x2: w - m.r, y1: y(v), y2: y(v), class: v === 0 ? "zero" : "grid" }));
    g.appendChild(svg("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end", class: "tick", text: yfmt(v) }));
  }
  for (const v of xTickVals || ticks(xd[0], xd[1], xticks)) {
    if (v < xd[0] || v > xd[1]) continue;
    g.appendChild(svg("line", { x1: x(v), x2: x(v), y1: h - m.b, y2: h - m.b + 4, class: "tickmark" }));
    g.appendChild(svg("text", { x: x(v), y: h - m.b + 17, "text-anchor": "middle", class: "tick", text: xfmt(v) }));
  }
  g.appendChild(svg("line", { x1: m.l, x2: w - m.r, y1: h - m.b, y2: h - m.b, class: "baseline" }));
  root.appendChild(g);
  const plot = svg("g", { class: "plot" });
  root.appendChild(plot);
  return { root, plot, x, y, box: { l: m.l, r: w - m.r, t: m.t, b: h - m.b, w, h } };
}

// pointer x in viewBox units
export function pointerX(root, ev, w) {
  const b = root.getBoundingClientRect();
  return ((ev.clientX - b.left) / b.width) * w;
}

export function vline(plot, x, box, cls, label) {
  plot.appendChild(svg("line", { x1: x, x2: x, y1: box.t, y2: box.b, class: cls }));
  if (label) plot.appendChild(svg("text", { x: x + 3, y: box.t + 10, class: `lbl ${cls}`, text: label }));
}

export function hline(plot, y, box, cls, label) {
  plot.appendChild(svg("line", { x1: box.l, x2: box.r, y1: y, y2: y, class: cls }));
  if (label) plot.appendChild(svg("text", { x: box.r - 3, y: y - 4, "text-anchor": "end", class: `lbl ${cls}`, text: label }));
}

// labels for vertical lines, left to right: each takes the lowest row (12px apart, going up from y0)
// where it clears the labels already placed, so close levels stack instead of overprinting
export function vlabels(plot, items, y0, cls = "lbl") {
  const rows = [];                                   // right edge of the last label in each row
  for (const it of [...items].sort((a, b) => a.x - b.x)) {
    const w = it.text.length * 6.2 + 6, x0 = it.x + 3;
    let r = rows.findIndex((edge) => x0 > edge);
    if (r < 0) { r = rows.length; rows.push(0); }
    rows[r] = x0 + w;
    plot.appendChild(svg("text", { x: x0, y: y0 - r * 12, class: cls, text: it.text }));
  }
}

// legend: swatch + text (identity is never colour alone: lines also differ in dash)
export function legend(items) {
  const g = document.createElement("div");
  g.className = "legend";
  for (const it of items) {
    const sw = svg("svg", { viewBox: "0 0 22 8", class: "sw", "aria-hidden": "true" },
      svg("line", { x1: 1, x2: 21, y1: 4, y2: 4, stroke: it.color, "stroke-width": it.width || 2.5,
                    "stroke-dasharray": it.dash || null, "stroke-linecap": "round" }));
    const s = document.createElement("span");
    s.className = "li";
    s.appendChild(sw);
    s.appendChild(document.createTextNode(it.label));
    g.appendChild(s);
  }
  return g;
}

export function sparkline(values, { w = 120, h = 34 } = {}) {
  const v = values.filter(Number.isFinite);
  const root = svg("svg", { viewBox: `0 0 ${w} ${h}`, class: "spark", "aria-hidden": "true" });
  if (v.length < 2) return root;
  const [lo, hi] = extent([v], 0.08);
  const x = lin(0, v.length - 1, 2, w - 4), y = lin(lo, hi, h - 3, 3);
  root.appendChild(svg("path", { d: pathOf(v.map((_, i) => i), v, x, y), class: "spark-line" }));
  root.appendChild(svg("circle", { cx: x(v.length - 1), cy: y(v[v.length - 1]), r: 2.5, class: "spark-dot" }));
  return root;
}

// ---------------------------------------------------------------------------------------------------
// colour: OKLab interpolation for the diverging heatmap (each arm normalised to its own extreme)

const hex2rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
const toLin = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const toSrgb = (c) => (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055);

export function oklab(hex) {
  const [r, g, b] = hex2rgb(hex).map(toLin);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
          1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
          0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s];
}

function lab2hex([L, A, B]) {
  const l = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3;
  const m = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3;
  const s = (L - 0.0894841775 * A - 1.291485548 * B) ** 3;
  const rgb = [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
               -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
               -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s];
  return "#" + rgb.map((c) => Math.round(Math.min(Math.max(toSrgb(Math.min(Math.max(c, 0), 1)), 0), 1) * 255)
    .toString(16).padStart(2, "0")).join("");
}

export const RAMPS = {
  zero: "#cfd3d8",
  loss: ["#e7a9a1", "#e47c72", "#ce5249", "#a83630", "#7f2924"],     // light (near zero) -> dark (max loss)
  gain: ["#98c9a1", "#5cb572", "#139948", "#057836", "#025a26"],     // light -> dark (max profit)
  gainCvd: ["#9abdeb", "#6c9edc", "#3f7fc6", "#2463a8", "#124a89"],  // colour-blind safe profit arm (blue)
};

const LABS = new Map();
const labOf = (h) => { if (!LABS.has(h)) LABS.set(h, oklab(h)); return LABS.get(h); };

// t in [-1, 1]: -1 = max loss, 0 = break-even, +1 = max profit. Any non-zero value is at least the lightest step,
// so profit and loss read at a glance. Returns {fill, dark} (dark = use dark ink: L > 0.65).
export function heatColor(t, cvd = false) {
  if (!Number.isFinite(t) || Math.abs(t) < 1e-9) return { fill: RAMPS.zero, dark: true };
  const steps = t > 0 ? (cvd ? RAMPS.gainCvd : RAMPS.gain) : RAMPS.loss;
  const p = Math.min(Math.abs(t), 1) * (steps.length - 1);
  const i = Math.min(Math.floor(p), steps.length - 2), f = p - i;
  const a = labOf(steps[i]), b = labOf(steps[i + 1]);
  const lab = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
  return { fill: lab2hex(lab), dark: lab[0] > 0.65 };
}

export const signed = (v, fmt) => (v > 0 ? "+" : v < 0 ? MINUS : "") + fmt(Math.abs(v));
