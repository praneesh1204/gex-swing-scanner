"""Synthetic backtest harness (spec §9): replay each strategy's entry rules and exit plan on history.

There are no free historical option chains, so this is a *synthetic* replay, and every report says so:
  * IV proxy on day t = max(RV20, RV60) x iv_premium (1.10), from closes up to t only. Flat smile, flat term.
  * Strikes from Black-Scholes deltas on that IV, rounded to a strike grid; prices are BS on the same IV.
  * Market gates replayed from the VIX family on the entry date (no lookahead): vix_ts (backwardation =
    no short premium), vix_level (extreme), vvix spike; plus an IV-rank gate on the proxy's own trailing
    252-day rank (labelled proxy) and the no-earnings-in-trade rule from past report dates.
  * Name gates that need a chain (walls, GEX, flow, liquidity, VRP) are not replayable and are listed.
  * Exits are the live ones: credit trades close at the 50% target, at 2x the credit received (loss = 1x
    credit; the underlying-close stop for verticals too), or at the time exit; debit calendars at +25%,
    at 50% of the debit or 5 days before the front expiry. No rolling, ever. Slippage on both sides.
  * One contract per entry; entries every N sessions per name; overlapping trades are independent.
Ideas behind this follow optopsy / optionlab (strategy-as-legs replay, payoff/PoP); nothing is copied from
them (AGPL-3.0 / GPL-3.0). If you install optopsy yourself, `backtest.optopsy_adapter` can run it on real
chains you supply.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from statistics import NormalDist

import numpy as np
import pandas as pd

from ..analytics import bs
from ..config import stop_multiple
from ..signals.ruleset import UNKNOWN, matches
from ..strategies.base import time_exit_date

N = NormalDist()
LABEL = ("SYNTHETIC: Black-Scholes repricing on an RV-based IV proxy (max(RV20, RV60) x premium), flat smile, "
         "no historical option quotes. Use it to compare rules and exits, not to forecast returns.")
SUPPORTED = ("csp", "bull_put", "bear_call", "iron_condor", "calendar")
NOT_REPLAYED = ("support/resistance, walls and GEX (need historical chains); lean and range are replayed from "
                "price only (SMA20/SMA50 trend, % vs SMA20), without their gamma/wall parts", "flow and liquidity",
                "VRP (the IV proxy is built from RV, so it is positive by construction)",
                "calendar any_of edge (term structure / forward factor: the proxy has a flat term structure)",
                "news and macro events")


@dataclass
class Trade:
    symbol: str
    strategy: str
    entry: str
    exit: str
    expiry: str
    legs: str
    spot: float
    iv: float
    net: float               # + credit / - debit per share at entry (before slippage)
    exit_value: float        # net value when closed (same sign convention)
    pnl: float               # $ per contract, after slippage
    reason: str              # tp / stop / stop_strike / time / expiry / open (data ended first)
    days: int

    def to_dict(self):
        return asdict(self)


@dataclass
class StratStats:
    strategy: str
    n: int = 0
    wins: int = 0
    win_rate: float | None = None
    avg_pnl: float | None = None
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    avg_days: float | None = None
    open: int = 0                                     # still open at the data end (not in the stats)
    exits: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)       # gate -> entries it blocked
    by_symbol: dict = field(default_factory=dict)     # symbol -> {n, pnl}

    def to_dict(self):
        return asdict(self)


@dataclass
class BacktestResult:
    start: str
    end: str
    symbols: list
    stats: dict                                      # strategy -> StratStats
    trades: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    label: str = LABEL

    def to_dict(self):
        return {"label": self.label, "start": self.start, "end": self.end, "symbols": self.symbols,
                "stats": {k: v.to_dict() for k, v in self.stats.items()}, "assumptions": self.assumptions,
                "errors": self.errors, "trades": [t.to_dict() for t in self.trades]}


# ---------------------------------------------------------------------------------------------------
# pricing helpers

def strike_step(S: float) -> float:
    return 0.5 if S < 25 else 1.0 if S < 100 else 2.5 if S < 300 else 5.0


def rnd(K: float, step: float) -> float:
    return round(round(K / step) * step, 4)


def strike_at_delta(S: float, T: float, v: float, cp: str, d: float) -> float:
    """BS strike whose |delta| = d (r = 0)."""
    d = min(max(d, 0.01), 0.99)
    d1 = N.inv_cdf(d) if cp == "C" else -N.inv_cdf(d)
    return S * math.exp(-d1 * v * math.sqrt(T) + 0.5 * v * v * T)


def friday_on_or_after(d: dt.date) -> dt.date:
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def net_value(legs: list[tuple], S: float, day: dt.date, v: float) -> float:
    """SELL legs +value, BUY legs -value (credit structures > 0). Expired legs at intrinsic."""
    sgn = np.array([1.0 if a == "SELL" else -1.0 for a, _, _, _ in legs])
    K = np.array([k for _, _, k, _ in legs], dtype=float)
    cp = np.array([c for _, c, _, _ in legs])
    T = np.array([max((e - day).days, 0) / 365 for _, _, _, e in legs])
    px = np.asarray(bs.price(S, K, np.maximum(T, 1e-6), v, cp), dtype=float)
    intr = np.where(cp == "C", np.maximum(S - K, 0), np.maximum(K - S, 0))
    px = np.where(T <= 0, intr, px)
    return float((sgn * px).sum())


def iv_proxy(closes: pd.Series, premium: float) -> pd.Series:
    r = np.log(closes).diff()
    rv20 = r.rolling(20).std() * math.sqrt(252)
    rv60 = r.rolling(60).std() * math.sqrt(252)
    return np.maximum(rv20, rv60) * premium            # row t uses closes <= t only


def proxy_rank(iv: pd.Series, n: int = 252) -> pd.Series:
    lo = iv.rolling(n, min_periods=120).min()
    hi = iv.rolling(n, min_periods=120).max()
    return (iv - lo) / (hi - lo) * 100


def bs_delta(S: float, K: float, T: float, v: float, cp: str) -> float:
    d1 = (math.log(S / K) + 0.5 * v * v * T) / (v * math.sqrt(T))
    return N.cdf(d1) if cp == "C" else N.cdf(d1) - 1


def pick_vertical(S, T, v, cp, band, target_delta, width, min_ctw, target_ctw, step) -> tuple[float, float] | None:
    """Same search as strategies.verticals.pick_vertical, on a synthetic strike grid: shorts nearest the target
    delta first, wings nearest the target width (<= 1.25x); first spread at >= target credit/width wins, else
    the best one at >= the floor. -> (short strike, width) or None."""
    target_ctw = max(target_ctw or min_ctw, min_ctw)
    lo, hi = band if isinstance(band, (list, tuple)) else (band * 0.6, band * 1.5)
    k0 = rnd(strike_at_delta(S, T, v, cp, target_delta), step)
    grid = [k0 + i * step for i in range(-40, 41) if k0 + i * step > 0]
    shorts = [(abs(abs(bs_delta(S, k, T, v, cp)) - target_delta), k) for k in grid
              if lo <= abs(bs_delta(S, k, T, v, cp)) <= hi]
    best = None
    for _, k in sorted(shorts):
        wings = [i * step for i in range(1, int(width * 1.25 / step) + 1)]
        for w in sorted(wings, key=lambda w: abs(w - width)):
            k2 = k - w if cp == "P" else k + w
            if k2 <= 0:
                continue
            cr = float(bs.price(S, k, T, v, cp) - bs.price(S, k2, T, v, cp))
            if cr < min_ctw * w:
                continue
            if cr >= target_ctw * w:
                return k, w
            if best is None or cr / w > best[2]:
                best = (k, w, cr / w)
    return (best[0], best[1]) if best else None


def trend_states(closes: pd.Series, sheet: dict) -> pd.DataFrame:
    """Price-only proxies of the live `lean` and `range` signals (no GEX / walls): same SMA20/SMA50 trend label
    and +-2% / max_abs_pct_vs_sma20 cuts as signals.ruleset. Row t uses closes <= t."""
    sma20, sma50 = closes.rolling(20).mean(), closes.rolling(50).mean()
    pct = (closes / sma20 - 1) * 100
    up = (closes > sma20) & (sma50.isna() | (sma20 > sma50))
    dn = (closes < sma20) & (sma50.isna() | (sma20 < sma50))
    sc = np.where(up, 1.0, np.where(dn, -1.0, 0.0)) + np.where(pct > 2, 0.5, np.where(pct < -2, -0.5, 0.0))
    lean = np.where(sc >= 1, "bullish", np.where(sc <= -1, "bearish", "neutral"))
    rng = np.where(pct.abs() <= sheet["signals"]["range"]["max_abs_pct_vs_sma20"], "range_bound", "trending")
    out = pd.DataFrame({"lean": lean, "range": rng}, index=closes.index)
    out[sma20.isna()] = UNKNOWN
    return out


# ---------------------------------------------------------------------------------------------------
# structures (same build parameters as the live plugins, from the signal sheet)

def build(strategy: str, S: float, v: float, day: dt.date, b: dict) -> tuple[list[tuple], dt.date, dict] | None:
    """-> (legs [(action, type, strike, expiry)], expiry, extra) or None if no valid structure."""
    step = strike_step(S)
    if strategy == "calendar":
        lo, hi = b.get("front_dte", [21, 45])
        g_lo, g_hi = b.get("back_gap_days", [21, 45])
        front = friday_on_or_after(day + dt.timedelta(days=(lo + hi) // 2))
        back = friday_on_or_after(front + dt.timedelta(days=(g_lo + g_hi) // 2))
        K = rnd(S, step)
        return [("SELL", "C", K, front), ("BUY", "C", K, back)], front, {}
    dte = b.get("target_dte") or sum(b.get("dte", [30, 45])) // 2
    exp = friday_on_or_after(day + dt.timedelta(days=dte))
    T = (exp - day).days / 365
    tdel = b.get("target_delta", b.get("delta", 0.2) if not isinstance(b.get("delta"), list) else 0.22)
    width = max(rnd(S * b.get("width_pct", 0.04), step), step)
    if strategy == "csp":
        K = rnd(strike_at_delta(S, T, v, "P", tdel), step)
        return [("SELL", "P", K, exp)], exp, {"stop_below": None}
    if strategy in ("bull_put", "bear_call"):
        cp = "P" if strategy == "bull_put" else "C"
        picked = pick_vertical(S, T, v, cp, b.get("delta", [0.16, 0.30]), tdel, S * b.get("width_pct", 0.04),
                               b.get("min_credit_to_width", 0.25), b.get("target_credit_to_width"), step)
        if picked is None:
            return None
        K, w = picked
        wing = K - w if cp == "P" else K + w
        return ([("SELL", cp, K, exp), ("BUY", cp, wing, exp)], exp,
                {"width": w, "stop_below" if cp == "P" else "stop_above": K})
    if strategy == "iron_condor":
        d = b.get("delta", 0.16) if not isinstance(b.get("delta"), list) else 0.16
        kp = rnd(strike_at_delta(S, T, v, "P", d), step)
        kc = rnd(strike_at_delta(S, T, v, "C", d), step)
        if kc <= kp:
            return None
        return ([("BUY", "P", kp - width, exp), ("SELL", "P", kp, exp), ("SELL", "C", kc, exp), ("BUY", "C", kc + width, exp)],
                exp, {"width": width})
    return None


# ---------------------------------------------------------------------------------------------------
# gates replayed on the entry date

def _asof(s: pd.Series | None, day: pd.Timestamp) -> float | None:
    if s is None or not len(s):
        return None
    x = s[s.index <= day]
    return float(x.iloc[-1]) if len(x) else None


def market_states(sheet: dict, series: dict, day: pd.Timestamp) -> dict[str, str]:
    """vix_ts / vix_level / vvix states on `day` from closes <= day, same cuts as signals.ruleset.classify_market."""
    s = sheet["signals"]
    out = {}
    vix, vix3m = _asof(series.get("VIX"), day), _asof(series.get("VIX3M"), day)
    if vix and vix3m:
        r, p = vix / vix3m, s["vix_ts"]
        out["vix_ts"] = "backwardation" if r >= p["backwardation_at"] else "flat" if r >= p["flat_from"] else "contango"
    if vix:
        p = s["vix_level"]
        out["vix_level"] = ("extreme" if vix > p["extreme_above"] else "stressed" if vix > p["stressed_above"]
                            else "calm" if vix < p["calm_below"] else "normal")
    vv = series.get("VVIX")
    if vv is not None and len(vv):
        x = vv[vv.index <= day]
        if len(x):
            p, v = s["vvix"], float(x.iloc[-1])
            chg = (v / float(x.iloc[-6]) - 1) * 100 if len(x) > 5 and float(x.iloc[-6]) > 0 else None
            spike = v >= p["spike_at"] or (chg is not None and chg >= p["spike_5d_pct"])
            out["vvix"] = "spike" if spike else "elevated" if v >= p["elevated_at"] else "normal"
    return out


def ivr_state(sheet: dict, rank: float | None) -> str:
    if rank is None or np.isnan(rank):
        return UNKNOWN
    s = sheet["signals"]["iv_rank"]
    return ("rich" if rank > s["rich_above"] else "sell" if rank > s["sell_above"]
            else "low" if rank < s["low_below"] else "mid")


REPLAYED = ("vix_ts", "vix_level", "vvix", "iv_rank", "lean", "range")
PROXY = ("iv_rank", "lean", "range")
OPEN = "open"                                       # still open when the data ends: listed, not scored


def gate_fail(spec: dict, states: dict[str, str]) -> str | None:
    """-> the first required gate (of the replayable ones) that fails, or None. Unknown passes unless the
    gate says `unknown: fail` (as live)."""
    req = (spec.get("gates") or {}).get("required") or {}
    for g in REPLAYED:
        if g not in req:
            continue
        st = states.get(g, UNKNOWN)
        ok = matches(req[g], st)
        if ok is False or (ok is None and req[g].get("unknown") == "fail"):
            return f"{g} ({'proxy ' if g in PROXY else ''}{st})"
    return None


# ---------------------------------------------------------------------------------------------------
# the replay

def _walk(legs, net0, kind, expiry, entry_i, idx, closes, ivs, rules) -> tuple[int, float, str]:
    """Walk forward from the day after entry; -> (exit bar, value, reason)."""
    last = entry_i
    for j in range(entry_i + 1, len(idx)):
        day = idx[j].date()
        last = j
        S, v = float(closes.iloc[j]), float(ivs.iloc[j]) if not np.isnan(ivs.iloc[j]) else float(ivs.iloc[j - 1])
        if day >= expiry:
            return j, net_value(legs, S, expiry, v), "expiry"
        val = net_value(legs, S, day, v)
        if kind == "credit":
            if val <= rules["tp"]:
                return j, val, "tp"
            if val >= rules["stop"]:
                return j, val, "stop"
            if rules.get("stop_below") and S < rules["stop_below"]:
                return j, val, "stop_strike"
            if rules.get("stop_above") and S > rules["stop_above"]:
                return j, val, "stop_strike"
        else:
            if -val >= rules["tp"]:
                return j, val, "tp"
            if -val <= rules["stop"]:
                return j, val, "stop"
        if day >= rules["time"]:
            return j, val, "time"
    S, v = float(closes.iloc[last]), float(ivs.iloc[last])
    return last, net_value(legs, S, idx[last].date(), v), OPEN


def run_symbol(sym: str, bars: pd.DataFrame, strategy: str, spec: dict, sheet: dict, cfg: dict, series: dict,
               earnings: list[dt.date], start: pd.Timestamp, stats: StratStats) -> list[Trade]:
    bcfg, tcfg = cfg["backtest"], cfg["trades"]
    b = spec.get("build") or {}
    closes = bars["close"].astype(float)
    ivs = iv_proxy(closes, float(bcfg.get("iv_premium", 1.10)))
    ranks = proxy_rank(ivs)
    trend = trend_states(closes, sheet)
    idx = closes.index
    every = int(bcfg.get("entry_every_days", 5))
    slip = float(bcfg.get("slippage_pct_of_credit", 0.05))
    mult, tp_pct = stop_multiple(tcfg), tcfg.get("take_profit_pct", 0.50)
    avoid_earn = spec.get("avoid_earnings_in_trade") or "earnings" in ((spec.get("gates") or {}).get("required") or {})
    min_ctw = b.get("min_credit_to_width")
    out = []
    first = max(int(idx.searchsorted(start)), 60)
    for i in range(first, len(idx) - 1, every):
        day = idx[i]
        S, v = float(closes.iloc[i]), float(ivs.iloc[i])
        if np.isnan(v) or v <= 0:
            continue
        why = gate_fail(spec, {**market_states(sheet, series, day), "iv_rank": ivr_state(sheet, float(ranks.iloc[i])),
                               "lean": trend["lean"].iloc[i], "range": trend["range"].iloc[i]})
        built = build(strategy, S, v, day.date(), b) if not why else None
        if not why and built is None:
            why = "no valid structure"
        if not why:
            legs, expiry, extra = built
            if avoid_earn and any(day.date() < e <= expiry for e in earnings):
                why = "earnings in the trade window"
        if not why:
            net0 = net_value(legs, S, day.date(), v)
            kind = "credit" if net0 > 0 else "debit"
            if strategy == "calendar" and kind != "debit":
                why = "no valid structure"
            elif extra.get("width") and min_ctw and net0 / extra["width"] < min_ctw:
                why = f"credit/width under {min_ctw:.0%}"
        if why:
            stats.skipped[why] = stats.skipped.get(why, 0) + 1
            continue
        if kind == "credit":
            rules = {"tp": net0 * (1 - tp_pct), "stop": net0 * mult,
                     "time": time_exit_date(day.date(), expiry.isoformat(), tcfg.get("time_exit_dte", 21)),
                     "stop_below": extra.get("stop_below"), "stop_above": extra.get("stop_above")}
        else:
            d = -net0
            rules = {"tp": d * (1 + b.get("take_profit_pct", 0.25)), "stop": d * b.get("stop_value_pct", 0.50),
                     "time": expiry - dt.timedelta(days=b.get("exit_days_before_front", 5))}
        j, val, reason = _walk(legs, net0, kind, expiry, i, idx, closes, ivs, rules)
        # credit: keep (credit - cost to close); debit: (value sold - debit paid). Same formula in net terms.
        pnl = ((net0 - val) - 2 * abs(net0) * slip) * 100
        out.append(Trade(sym, strategy, day.date().isoformat(), idx[j].date().isoformat(), expiry.isoformat(),
                         " / ".join(f"{a} {k:g}{c} {e:%m-%d}" for a, c, k, e in legs), round(S, 2), round(v, 4),
                         round(net0, 3), round(val, 3), round(pnl, 2), reason, (idx[j] - day).days))
    return out


def summarize(stats: StratStats, trades: list[Trade]) -> StratStats:
    stats.open = sum(t.reason == OPEN for t in trades)
    trades = sorted((t for t in trades if t.reason != OPEN), key=lambda t: (t.exit, t.entry))
    if not trades:
        return stats
    p = np.array([t.pnl for t in trades])
    eq = np.cumsum(p)
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    stats.n = len(trades)
    stats.wins = int((p > 0).sum())
    stats.win_rate = round(stats.wins / stats.n * 100, 1)
    stats.avg_pnl = round(float(p.mean()), 2)
    stats.total_pnl = round(float(p.sum()), 2)
    stats.max_drawdown = round(float((peak - eq).max()), 2)
    stats.avg_days = round(float(np.mean([t.days for t in trades])), 1)
    for t in trades:
        stats.exits[t.reason] = stats.exits.get(t.reason, 0) + 1
        s = stats.by_symbol.setdefault(t.symbol, {"n": 0, "pnl": 0.0})
        s["n"] += 1
        s["pnl"] = round(s["pnl"] + t.pnl, 2)
    return stats


def backtest(bars: dict[str, pd.DataFrame], series: dict, earnings: dict[str, list[dt.date]], cfg: dict, sheet: dict,
             strategies: list[str] | None = None, years: float | None = None, end: dt.date | None = None) -> BacktestResult:
    """bars: symbol -> daily OHLC; series: VIX-family closes (date index); earnings: past report dates."""
    bcfg = cfg["backtest"]
    strategies = [s for s in (strategies or bcfg.get("strategies") or SUPPORTED)]
    errors = [f"{s}: not replayable synthetically (supported: {', '.join(SUPPORTED)})" for s in strategies if s not in SUPPORTED]
    strategies = [s for s in strategies if s in SUPPORTED]
    bars = {k: v for k, v in bars.items() if v is not None and len(v) > 80}
    if not bars:
        return BacktestResult("", "", [], {}, errors=errors + ["no daily bars with enough history"])
    last = max(v.index[-1] for v in bars.values())
    end_ts = min(pd.Timestamp(end), last) if end else last
    yrs = float(years or bcfg.get("years", 3))
    start = end_ts - pd.Timedelta(days=int(yrs * 365))
    stats, trades = {}, []
    for strat in strategies:
        spec = sheet["strategies"].get(strat, {})
        st = StratStats(strat)
        tr = []
        for sym, df in bars.items():
            df = df[df.index <= end_ts]
            tr += run_symbol(sym, df, strat, spec, sheet, cfg, series, earnings.get(sym, []), start, st)
        stats[strat] = summarize(st, tr)
        trades += tr
    first = min((max(df.index[0], start) for df in bars.values()), default=start)
    assumptions = [LABEL, f"IV proxy = max(RV20, RV60) x {bcfg.get('iv_premium', 1.10)}",
                   f"slippage {bcfg.get('slippage_pct_of_credit', 0.05):.0%} of the net, each side",
                   f"one entry per name every {bcfg.get('entry_every_days', 5)} sessions, 1 contract",
                   "exits: credit TP 50% / stop 2x credit (+ underlying beyond the short strike for verticals) / "
                   "time exit; calendars +25% / 50% of debit / 5 days before the front. No rolling.",
                   "not replayed: " + "; ".join(NOT_REPLAYED)]
    return BacktestResult(str(first.date()), str(end_ts.date()), sorted(bars), stats, trades, assumptions, errors)
