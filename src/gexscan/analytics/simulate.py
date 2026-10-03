"""Monte Carlo of a position with its exit plan (spec v2.2 §6.8). Read-only maths: it never touches a broker.

Paths: GBM under the implied distribution (drift r - q), one step per weekday close from today to the FRONT
expiry (dt = calendar gap / 365, so the total variance matches BSM's calendar-day T). Volatility is piecewise
forward from the ATM term structure with the earnings variance taken out of the diffusion and added back as a
log-jump N(-m^2/2, m) on the event date. Every leg is re-priced each day by analytics.pricing at its own IV
(with the post-event crush).

Exit engine, checked at each daily close in this order: level stop (underlying closes beyond a level), value
stop (credit: cost to close >= m x credit; debit: value <= sv x debit), profit target, time exit, expiry.
Exits fill at the model mid. A second pass holds every path to the front expiry. No rolling.

Labels the page must show: LABELS below.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict, dataclass

import numpy as np

from . import pricing as P
from .pricing import Event, Leg

LABELS = ["Simulated from implied volatility. Real paths jump, skew moves and gaps happen.",
          "Daily closes only: intraday stop touches are not seen."]
MAX_CELLS = 30_000_000          # steps x paths x legs: above it the path count is cut (and reported)
EXITS = ("level", "stop", "target", "time", "expiry")


@dataclass
class Plan:
    kind: str                   # "credit" or "debit"
    tp: float = 0.50            # credit: keep tp of the credit; debit: gain tp on the debit
    stop: float = 2.0           # credit: cost to close >= stop x credit; debit: value <= stop x debit
    below: float | None = None  # level stop: the underlying closes below
    above: float | None = None  # level stop: the underlying closes above
    texit: dt.date | None = None

    def __post_init__(self):
        self.texit = P._date(self.texit)
        if self.kind not in ("credit", "debit"):
            raise ValueError("plan kind must be credit or debit")


def default_plan(legs: list[Leg], today, tcfg: dict | None = None) -> Plan:
    """The recommend rules: credit 50% target / 2x stop; debit +50% target / 50% value stop; time exit at
    21 DTE or halfway, whichever is earlier."""
    from ..strategies.base import time_exit_date
    tcfg = tcfg or {}
    front = P.front_expiry(legs)
    texit = time_exit_date(P._date(today), front.isoformat(), tcfg.get("time_exit_dte", 21)) if front else None
    if P.net_basis(legs) < 0:
        return Plan("credit", tcfg.get("take_profit_pct", 0.50), float(tcfg.get("stop_credit_multiple", 2.0)),
                    texit=texit)
    return Plan("debit", 0.50, 0.50, texit=texit)


def step_dates(today, horizon) -> list[dt.date]:
    """Weekday closes after today up to the horizon (the horizon is always the last step)."""
    t, h = P._date(today), P._date(horizon)
    out, d = [], t + dt.timedelta(days=1)
    while d < h:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    if h > t:
        out.append(h)
    return out


def diffusion_variance(today, dates: list[dt.date], term: list[tuple] | None, sigma: float,
                       event: Event | None, min_vol: float = 0.05) -> np.ndarray:
    """Variance of each step's log return, excluding the event jump.

    term = [(expiry, atm_iv), ...]. Total implied variance w(T) = iv^2 T, less the event variance m^2 for
    expiries after the event; interpolated linearly in T (through 0 at today), made non-decreasing, and each
    step floored at min_vol^2 dt. Without a term structure, sigma is used for every expiry."""
    t0 = P._date(today)
    horizon = dates[-1]
    pts = sorted((P._date(e), float(v)) for e, v in (term or []) if v and P._date(e) > t0)
    if not pts:
        pts = [(horizon, sigma)]
    ev_d = event.date if event and t0 < event.date else None
    Ts, Ws = [0.0], [0.0]
    for e, v in pts:
        T = (e - t0).days / 365
        w = v * v * T - (event.move ** 2 if ev_d and ev_d < e else 0.0)
        Ts.append(T)
        Ws.append(max(w, Ws[-1]))
    if Ts[-1] < (horizon - t0).days / 365:         # extend at the last forward variance
        slope = (Ws[-1] - Ws[-2]) / (Ts[-1] - Ts[-2]) if Ts[-1] > Ts[-2] else pts[-1][1] ** 2
        T = (horizon - t0).days / 365
        Ts.append(T)
        Ws.append(Ws[-1] + max(slope, 0.0) * (T - Ts[-2]))
    t_steps = np.array([(d - t0).days / 365 for d in dates])
    W = np.interp(t_steps, Ts, Ws)
    dW = np.diff(np.concatenate([[0.0], W]))
    dts = np.diff(np.concatenate([[0.0], t_steps]))
    return np.maximum(dW, min_vol * min_vol * dts)


def simulate_paths(spot: float, today, dates: list[dt.date], var: np.ndarray, r: float, q: float,
                   event: Event | None, n: int, rng: np.random.Generator) -> np.ndarray:
    """-> closes, shape (len(dates), n). Draw order (fixed, so tests can rebuild it): Z (steps, n) first,
    then one jump draw (n) when an event lands inside the horizon."""
    t0 = P._date(today)
    dts = np.diff(np.concatenate([[0.0], [(d - t0).days / 365 for d in dates]]))
    Z = rng.standard_normal((len(dates), n))
    inc = ((r - q) * dts - 0.5 * var)[:, None] + np.sqrt(var)[:, None] * Z
    if event and t0 < event.date <= dates[-1]:
        k = next(i for i, d in enumerate(dates) if d >= event.date)
        m = event.move
        inc[k] += -0.5 * m * m + m * rng.standard_normal(n)
    return spot * np.exp(np.cumsum(inc, axis=0))


def _stats(pl: np.ndarray, held: np.ndarray, reason: np.ndarray | None) -> dict:
    srt = np.sort(pl)
    k = max(1, int(len(srt) * 0.05))
    out = {"p_profit": float(np.mean(pl > 0)), "ev": float(np.mean(pl)), "median": float(np.median(pl)),
           "cvar5": float(np.mean(srt[:k])), "median_days": float(np.median(held)),
           "p5": float(srt[int(0.05 * (len(srt) - 1))]), "p95": float(srt[int(0.95 * (len(srt) - 1))])}
    if reason is not None:
        mix = {e: float(np.mean(reason == i)) for i, e in enumerate(EXITS)}
        out.update(exit_mix=mix, p_stop=mix["level"] + mix["stop"], p_target=mix["target"])
    return out


def seed_of(req: dict) -> int:
    blob = json.dumps(req, sort_keys=True, default=str).encode()
    return int.from_bytes(hashlib.sha256(blob).digest()[:8], "big")


def simulate(legs: list[Leg], spot: float, today, r: float = 0.04, q: float = 0.0,
             term: list[tuple] | None = None, event: Event | None = None, plan: Plan | None = None,
             n: int = 20_000, seed: int | None = None, min_vol: float = 0.05, max_paths: int = 50_000,
             fan_pct=(5, 25, 50, 75, 95)) -> dict:
    """Simulate `legs` to the front expiry under an exit plan and as a hold. Returns JSON-ready dicts."""
    if not legs or P.front_expiry(legs) is None:
        raise ValueError("simulation needs at least one option leg")
    t0 = P._date(today)
    front = P.front_expiry(legs)
    if front <= t0:
        raise ValueError("the front expiry has passed")
    plan = plan or default_plan(legs, t0)
    dates = step_dates(t0, front)
    n = asked = int(min(max(n, 500), max_paths))
    while len(dates) * n * len(legs) > MAX_CELLS and n > 500:
        n = max(500, n // 2)
    req = {"legs": [lg.encode() for lg in legs], "spot": spot, "today": t0, "r": r, "q": q, "term": term,
           "event": [event.date, event.move] if event else None, "plan": asdict(plan), "n": n}
    seed = seed_of(req) if seed is None else int(seed)
    rng = np.random.default_rng(seed)
    ivs = [lg.iv for lg in legs if lg.type != "S" and lg.iv]
    sigma = float(np.mean(ivs)) if ivs else 0.30
    var = diffusion_variance(t0, dates, term, sigma, event, min_vol)
    S = simulate_paths(spot, t0, dates, var, r, q, event, n, rng)
    E = P.net_basis(legs)
    V = np.stack([P.position_value(legs, S[k], d, t0, r, q, 1.0, event) for k, d in enumerate(dates)])
    PL = V - E
    rs = P.risk_stats(legs, t0, r, q, 1.0, event, spot)

    # exit plan pass
    alive = np.ones(n, dtype=bool)
    exit_pl, exit_k, reason = np.zeros(n), np.full(n, len(dates) - 1), np.full(n, EXITS.index("expiry"))
    credit, debit = -E, E
    for k, d in enumerate(dates):
        if not alive.any():
            break
        v = V[k]
        hit = np.zeros(n, dtype=int) - 1
        lvl = np.zeros(n, dtype=bool)
        if plan.below is not None:
            lvl |= S[k] < plan.below
        if plan.above is not None:
            lvl |= S[k] > plan.above
        if plan.kind == "credit":
            stop, tgt = -v >= plan.stop * credit, -v <= (1 - plan.tp) * credit
        else:
            stop, tgt = v <= plan.stop * debit, v >= debit * (1 + plan.tp)
        tim = np.full(n, plan.texit is not None and d >= plan.texit)
        for i, m in enumerate((lvl, stop, tgt, tim)):
            hit = np.where((hit < 0) & m, i, hit)
        if k == len(dates) - 1:
            hit = np.where(hit < 0, EXITS.index("expiry"), hit)
        now = alive & (hit >= 0)
        exit_pl[now], exit_k[now], reason[now] = PL[k][now], k, hit[now]
        alive &= ~now

    days_of = np.array([(d - t0).days for d in dates])
    plan_fan_rows = np.where(np.arange(len(dates))[:, None] >= exit_k[None, :], exit_pl[None, :], PL)
    pct = list(fan_pct)

    def fan(M):
        return {f"p{p}": [round(float(x), 2) for x in row] for p, row in zip(pct, np.percentile(M, pct, axis=1))}

    hold_pl = PL[-1]
    lo, hi = float(min(exit_pl.min(), hold_pl.min())), float(max(exit_pl.max(), hold_pl.max()))
    edges = np.linspace(lo, hi if hi > lo else lo + 1.0, 31)
    return {
        "n": n, "seed": seed, "steps": len(dates), "horizon": front.isoformat(), "sigma": sigma,
        "dates": [d.isoformat() for d in dates], "days": days_of.tolist(),
        "plan": {**asdict(plan), "texit": plan.texit.isoformat() if plan.texit else None},
        "net_basis": E, "max_loss": rs["max_loss"], "max_profit": rs["max_profit"],
        "fan": {"plan": fan(plan_fan_rows), "hold": fan(PL),
                "spot": {f"p{p}": [round(float(x), 4) for x in row] for p, row in zip(pct, np.percentile(S, pct, axis=1))}},
        "stats": {"plan": _stats(exit_pl, days_of[exit_k], reason),
                  "hold": _stats(hold_pl, np.full(n, days_of[-1]), None)},
        "histogram": {"edges": [round(float(x), 2) for x in edges],
                      "plan": np.histogram(exit_pl, edges)[0].tolist(), "hold": np.histogram(hold_pl, edges)[0].tolist()},
        "labels": LABELS + ([f"Paths cut from {asked:,} to {n:,} to stay inside the compute budget."] if n < asked else []),
        "iv_note": "The simulation uses each leg's IV at x1 (the IV slider does not apply).",
    }


__all__ = ["EXITS", "LABELS", "Plan", "default_plan", "diffusion_variance", "seed_of", "simulate", "simulate_paths",
           "step_dates"]
