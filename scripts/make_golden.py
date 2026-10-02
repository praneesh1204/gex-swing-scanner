"""Generate src/gexscan/web/static/golden.json: reference values for the visualizer's pricing (spec v2.2 §6.7).

This is an INDEPENDENT implementation (scipy.stats.norm + scipy.optimize.brentq). It does not import
gexscan.analytics.pricing. tests/test_pricing.py checks pricing.py against it to 1e-9; tests/js/parity.mjs
checks web/static/engine.js to 1e-6; the browser re-checks engine.js on every page load (/api/golden).

    uv run --with scipy python scripts/make_golden.py
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

OUT = Path(__file__).resolve().parents[1] / "src" / "gexscan" / "web" / "static" / "golden.json"
TODAY = dt.date(2026, 10, 2)
MULT = {"C": 100, "P": 100, "S": 1}


def bsm(S, K, T, v, cp, r, q):
    if T <= 0:
        return max(S - K, 0.0) if cp == "C" else max(K - S, 0.0)
    S = max(S, 1e-12)
    d1 = (math.log(S / K) + (r - q + v * v / 2) * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)
    if cp == "C":
        return S * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * math.exp(-q * T) * norm.cdf(-d1)


def bsm_greeks(S, K, T, v, cp, r, q):
    d1 = (math.log(S / K) + (r - q + v * v / 2) * T) / (v * math.sqrt(T))
    dq = math.exp(-q * T)
    delta = dq * norm.cdf(d1) if cp == "C" else dq * (norm.cdf(d1) - 1)
    gamma = dq * norm.pdf(d1) / (S * v * math.sqrt(T))
    vega = S * dq * norm.pdf(d1) * math.sqrt(T) / 100
    return delta, gamma, vega


def days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def sigma_at(leg, on, today, ivm, ev):
    s = leg["iv"] * ivm
    if ev is None or leg["type"] == "S":
        return s
    if not (dt.date.fromisoformat(today) < dt.date.fromisoformat(ev["date"]) < dt.date.fromisoformat(leg["expiry"])):
        return s
    T0 = days(today, leg["expiry"]) / 365
    var = (s * s * T0 - ev["move"] ** 2) / T0
    post = 0.25 * s if var < (0.25 * s) ** 2 else math.sqrt(var)
    if on >= ev["date"]:
        return post
    T = days(on, leg["expiry"]) / 365
    return math.sqrt(post * post + ev["move"] ** 2 / T) if T > 0 else post


def value(legs, S, on, today, r, q, ivm=1.0, ev=None):
    tot = 0.0
    for lg in legs:
        k = lg["side"] * lg["qty"] * MULT[lg["type"]]
        if lg["type"] == "S":
            tot += k * S
            continue
        T = max(days(on, lg["expiry"]), 0) / 365
        tot += k * bsm(S, lg["strike"], T, max(sigma_at(lg, on, today, ivm, ev), 1e-4), lg["type"], r, q)
    return tot


def basis(legs):
    return sum(lg["side"] * lg["qty"] * MULT[lg["type"]] * lg["fill"] for lg in legs)


def grid(legs, spot):
    ks = [lg["strike"] for lg in legs if lg["type"] != "S"]
    top = 3 * max(ks + [spot, 1.0])
    return np.unique(np.concatenate([np.linspace(0, top, 2001), np.array(ks, dtype=float)]))


def breakevens(legs, on, today, r, q, ivm, ev, spot):
    E = basis(legs)
    xs = grid(legs, spot)
    pl = np.array([value(legs, x, on, today, r, q, ivm, ev) - E for x in xs])
    out = []
    for i in range(len(xs) - 1):
        if (pl[i] > 0) != (pl[i + 1] > 0):     # "P/L > 0" flips: a flat zero stretch is not a breakeven
            out.append(brentq(lambda x: value(legs, x, on, today, r, q, ivm, ev) - E, xs[i], xs[i + 1],
                              xtol=1e-13, rtol=1e-15, maxiter=500))
    return xs, pl, out


def front(legs):
    return min(lg["expiry"] for lg in legs if lg["type"] != "S")


def risk(legs, today, r, q, ev, spot):
    at = front(legs)
    xs, pl, be = breakevens(legs, at, today, r, q, 1.0, ev, spot)
    slope = (pl[-1] - pl[-2]) / (xs[-1] - xs[-2])
    return {"at": at, "max_profit": None if slope > 0.01 else float(pl.max()),
            "max_loss": None if slope < -0.01 else float(pl.min()), "breakevens": be,
            "unbounded_profit": bool(slope > 0.01), "unbounded_loss": bool(slope < -0.01), "net_basis": basis(legs)}


def lncdf(x, S, T, v, r, q):
    if x <= 0:
        return 0.0
    return norm.cdf((math.log(x / S) - (r - q - v * v / 2) * T) / (v * math.sqrt(T)))


def pop(legs, S, on, today, v, r, q, ev):
    on = min(on, front(legs))
    xs, pl, be = breakevens(legs, on, today, r, q, 1.0, ev, S)
    T = days(today, on) / 365
    cuts = [0.0] + be + [math.inf]
    tot = 0.0
    for a, b in zip(cuts[:-1], cuts[1:]):
        m = (a + b) / 2 if math.isfinite(b) else max(2 * a, xs[-1])
        if value(legs, m, on, today, r, q, 1.0, ev) - basis(legs) > 0:
            tot += (1.0 if not math.isfinite(b) else lncdf(b, S, T, v, r, q)) - lncdf(a, S, T, v, r, q)
    return tot


def greeks(legs, S, on, today, r, q, ev):
    d = g = vg = 0.0
    for lg in legs:
        k = lg["side"] * lg["qty"] * MULT[lg["type"]]
        if lg["type"] == "S":
            d += k
            continue
        T = max(days(on, lg["expiry"]), 0) / 365
        a, b, c = bsm_greeks(S, lg["strike"], T, max(sigma_at(lg, on, today, 1.0, ev), 1e-4), lg["type"], r, q)
        d, g, vg = d + k * a, g + k * b, vg + k * c
    nxt = (dt.date.fromisoformat(on) + dt.timedelta(days=1)).isoformat()
    th = value(legs, S, nxt, today, r, q, 1.0, ev) - value(legs, S, on, today, r, q, 1.0, ev)
    return {"delta": d, "gamma": g, "vega": vg, "theta": th}


def bsm_cases():
    rng = np.random.default_rng(20261002)
    fixed = [  # hand-picked edges: deep ITM/OTM, very short/long T, high q, low/high vol
        (100, 100, 30 / 365, 0.30, "C", 0.04, 0.00), (100, 100, 30 / 365, 0.30, "P", 0.04, 0.00),
        (100, 150, 30 / 365, 0.30, "C", 0.04, 0.00), (100, 50, 30 / 365, 0.30, "P", 0.04, 0.00),
        (100, 60, 30 / 365, 0.30, "C", 0.04, 0.00), (100, 140, 30 / 365, 0.30, "P", 0.04, 0.00),
        (100, 100, 1 / 365, 0.30, "C", 0.04, 0.00), (100, 100, 1 / 365, 0.30, "P", 0.04, 0.00),
        (100, 100, 2.0, 0.30, "C", 0.04, 0.02), (100, 100, 2.0, 0.30, "P", 0.04, 0.02),
        (250, 262.5, 45 / 365, 0.42, "C", 0.04, 0.005), (250, 237.5, 45 / 365, 0.42, "P", 0.04, 0.005),
        (100, 100, 0.5, 0.05, "C", 0.04, 0.00), (100, 100, 0.5, 1.80, "C", 0.04, 0.00),
        (100, 100, 0.5, 0.30, "C", 0.00, 0.06), (100, 100, 0.5, 0.30, "P", 0.00, 0.06),
    ]
    out = list(fixed)
    while len(out) < 40:
        S = float(rng.uniform(5, 900))
        K = float(round(S * rng.uniform(0.6, 1.4), 1))
        out.append((round(S, 2), K, float(rng.integers(1, 800)) / 365, float(round(rng.uniform(0.08, 1.4), 3)),
                    "C" if len(out) % 2 else "P", float(round(rng.uniform(0, 0.06), 4)),
                    float(round(rng.uniform(0, 0.04), 4))))
    rows = []
    for S, K, T, v, cp, r, q in out:
        dl, gm, vg = bsm_greeks(S, K, T, v, cp, r, q)
        rows.append({"S": S, "K": K, "T": T, "sigma": v, "cp": cp, "r": r, "q": q, "price": bsm(S, K, T, v, cp, r, q),
                     "delta": dl, "gamma": gm, "vega": vg})
    return rows


def positions():
    t = TODAY.isoformat()
    demo = {  # the synthetic DEMO bull call spread used in the docs (not a real position)
        "name": "DEMO bull call spread 105/115", "today": t, "spot": 108.0, "r": 0.04, "q": 0.0, "event": None,
        "legs": [{"side": 1, "type": "C", "strike": 105, "expiry": "2026-12-11", "qty": 2, "fill": 6.30, "iv": 0.61},
                 {"side": -1, "type": "C", "strike": 115, "expiry": "2026-12-11", "qty": 2, "fill": 2.85, "iv": 0.58}]}
    condor = {
        "name": "iron condor with dividend yield", "today": t, "spot": 250.0, "r": 0.04, "q": 0.012, "event": None,
        "legs": [{"side": 1, "type": "P", "strike": 220, "expiry": "2026-11-13", "qty": 1, "fill": 1.10, "iv": 0.48},
                 {"side": -1, "type": "P", "strike": 230, "expiry": "2026-11-13", "qty": 1, "fill": 2.40, "iv": 0.45},
                 {"side": -1, "type": "C", "strike": 270, "expiry": "2026-11-13", "qty": 1, "fill": 2.20, "iv": 0.38},
                 {"side": 1, "type": "C", "strike": 280, "expiry": "2026-11-13", "qty": 1, "fill": 0.95, "iv": 0.37}]}
    cal = {
        "name": "earnings calendar with IV crush", "today": t, "spot": 80.0, "r": 0.04, "q": 0.0,
        "event": {"date": "2026-10-23", "move": 0.09},
        "legs": [{"side": -1, "type": "C", "strike": 80, "expiry": "2026-10-30", "qty": 1, "fill": 4.10, "iv": 0.72},
                 {"side": 1, "type": "C", "strike": 80, "expiry": "2026-12-18", "qty": 1, "fill": 7.05, "iv": 0.55}]}
    out = []
    for p in (demo, condor, cal):
        legs, S, r, q, ev, td = p["legs"], p["spot"], p["r"], p["q"], p["event"], p["today"]
        ons = [td, "2026-10-20", "2026-10-26", front(legs)]
        vals = []
        for on in ons:
            for x in (S * 0.85, S, S * 1.1):
                for ivm in (1.0, 1.5):
                    vals.append({"S": x, "on": on, "ivmult": ivm, "V": value(legs, x, on, td, r, q, ivm, ev)})
        atm = float(np.mean([lg["iv"] for lg in legs]))
        out.append({**p, "values": vals, "risk": risk(legs, td, r, q, ev, S),
                    "pop": {"on": front(legs), "sigma": atm, "value": pop(legs, S, front(legs), td, atm, r, q, ev)},
                    "greeks": {"S": S, "on": td, **greeks(legs, S, td, td, r, q, ev)}})
    return out


def main():
    ev = []
    for s, T0, m in ((0.72, 28 / 365, 0.09), (0.40, 10 / 365, 0.12), (0.55, 77 / 365, 0.09)):
        var = (s * s * T0 - m * m) / T0
        floored = var < (0.25 * s) ** 2
        ev.append({"sigma": s, "T0": T0, "move": m, "post": 0.25 * s if floored else math.sqrt(var), "floored": floored})
    doc = {"version": 1, "generated_by": "scripts/make_golden.py (independent scipy reference)",
           "tolerance": {"python": 1e-9, "js": 1e-6}, "bsm": bsm_cases(), "event_sigma": ev, "positions": positions()}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1))
    print(f"wrote {OUT} ({len(doc['bsm'])} BSM cases, {len(doc['positions'])} positions)")


if __name__ == "__main__":
    main()
