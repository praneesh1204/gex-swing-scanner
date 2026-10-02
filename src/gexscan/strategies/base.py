"""Strategy plugin base (spec §4): Candidate, payoff/PoP/breakevens, liquidity, exit plans, sizing.

A plugin is a class with `name` and `evaluate(universe, signals) -> list[Candidate]`. The base class runs the
sheet's gates for every name, calls `build()` only where all required gates pass, then applies the
trade-level gates (earnings inside the trade, leg liquidity, calendar round-trip cost). Anything that fails
is recorded in the SignalBook explain log and never surfaced.

PoP: probability that the expiry P/L is >= 0 under a lognormal at the expiry's ATM IV, integrated over every
profitable region of the payoff (the method optionlab uses; reimplemented here, optionlab is GPL and is
not imported). It is an estimate under a model, not a forecast.

Ideas only. Nothing here talks to a broker.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from ..analytics import bs
from ..config import stop_multiple
from ..signals.book import NameContext, SignalBook
from ..signals.ruleset import GateResult, evaluate_gates

NO_ROLL = "No rolling."


@dataclass
class OptLeg:
    action: str          # BUY / SELL
    type: str            # C / P
    strike: float
    expiry: str
    dte: int
    bid: float
    ask: float
    mid: float
    iv: float
    delta: float
    oi: float
    volume: float
    qty: int = 1

    @property
    def sign(self) -> int:
        return 1 if self.action == "BUY" else -1

    @property
    def spread(self) -> float:
        return max(self.ask - self.bid, 0.0)

    @property
    def spread_pct(self) -> float:
        return self.spread / self.mid if self.mid > 0 else math.inf

    def text(self) -> str:
        return f"{self.action} {self.qty if self.qty > 1 else ''}{self.strike:g}{self.type} {self.expiry}"

    def to_dict(self):
        return asdict(self)


@dataclass
class Confidence:
    score: float = 0.0
    bucket: str = "Low"
    sub: dict = field(default_factory=dict)        # category -> {points, max, score, gate, metric, notes}
    penalties: list = field(default_factory=list)  # [{reason, points}]

    def to_dict(self):
        return asdict(self)


@dataclass
class Candidate:
    symbol: str
    strategy: str
    label: str
    direction: str
    vol_view: str
    kind: str                       # credit / debit
    risk: str                       # defined / undefined
    expiry: str
    dte: int
    legs: list = field(default_factory=list)          # [OptLeg]
    back_expiry: str | None = None
    net: float = 0.0                # per share at mid: + credit / - debit
    net_natural: float = 0.0        # worst-case fill (cross every spread)
    width: float | None = None
    max_profit: float | None = None  # $ per contract (estimate for calendars)
    max_loss: float | None = None    # $ per contract; None = undefined risk
    breakevens: list = field(default_factory=list)
    pop: float | None = None
    spot: float = 0.0
    contracts: int = 1
    # exit plan (text + machine-readable)
    take_profit: str = ""
    stop: str = ""
    time_stop: str = ""
    invalidation: str = ""
    stop_rule: str = ""             # the headline rule: "Stop = 2x credit; no rolling" / "Max loss = debit"
    tp_value: float | None = None
    stop_value: float | None = None
    stop_below: float | None = None
    stop_above: float | None = None
    time_exit: str | None = None
    metrics: dict = field(default_factory=dict)       # credit/width, liquidity score, wheel score, ...
    flags: list = field(default_factory=list)         # UNDEFINED RISK, EARNINGS TRADE, PROXY FLOW, ...
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    gates: GateResult | None = None
    confidence: Confidence | None = None
    rationale: dict = field(default_factory=dict)
    as_of: str = ""
    sources: dict = field(default_factory=dict)

    def summary(self) -> str:
        legs = " / ".join(l.text() for l in self.legs)
        return f"{self.symbol} {self.label}: {legs} @ {abs(self.net):.2f} {self.kind}"

    def journal_legs(self) -> list[dict]:
        return [{"action": l.action, "type": l.type, "strike": l.strike, "expiry": l.expiry, "qty": l.qty}
                for l in self.legs]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["legs"] = [l.to_dict() for l in self.legs]
        d["gates"] = self.gates.to_dict() if self.gates else None
        d["confidence"] = self.confidence.to_dict() if self.confidence else None
        d["summary"] = self.summary()
        return d


# ---------------------------------------------------------------------------------------------------
# chain helpers

def leg_table(ctx: NameContext, expiry: str, cp: str) -> pd.DataFrame:
    o = ctx.chain.options
    e = o[(o["expiry"].astype(str) == expiry) & (o["type"] == cp)].copy()
    e = e[(e["bid"] >= 0) & (e["ask"] > 0) & (e["mid"] > 0)].sort_values("strike")
    if e.empty:
        return e
    S = ctx.chain.spot
    T = max(int(e["dte"].iloc[0]), 1) / 365.0
    iv = e["iv"].where((e["iv"] > 0.02) & (e["iv"] < 5.0)).fillna(e["iv"].median())
    bsd = bs.delta(S, e["strike"].to_numpy(), T, iv.to_numpy(), np.array([cp] * len(e)))
    e["delta_use"] = np.where(e["delta"].abs() > 1e-4, e["delta"], bsd)
    e["iv_use"] = iv
    e["spread_pct"] = (e["ask"] - e["bid"]) / e["mid"]
    return e


def mk_leg(row, action: str, expiry: str, qty: int = 1) -> OptLeg:
    return OptLeg(action, str(row["type"]), float(row["strike"]), expiry, int(row["dte"]), float(row["bid"]),
                  float(row["ask"]), float(row["mid"]), float(row["iv_use"]), float(row["delta_use"]),
                  float(row.get("oi", 0) or 0), float(row.get("volume", 0) or 0), qty)


def expiries(ctx: NameContext) -> list[tuple[str, int]]:
    o = ctx.chain.options
    return sorted({(str(e), int(d)) for e, d in o[["expiry", "dte"]].itertuples(index=False)}, key=lambda x: x[1])


def pick_expiry(ctx: NameContext, dte_range, target: int | None = None) -> tuple[str, int] | None:
    lo, hi = dte_range
    c = [(e, d) for e, d in expiries(ctx) if lo <= d <= hi]
    if not c:
        return None
    t = target if target is not None else (lo + hi) / 2
    return min(c, key=lambda x: abs(x[1] - t))


def nearest_strike(t: pd.DataFrame, k: float):
    return t.iloc[(t["strike"] - k).abs().argsort()].iloc[0] if len(t) else None


# ---------------------------------------------------------------------------------------------------
# payoff / PoP / breakevens / max loss

def _grid(S: float, sigma: float, T: float, strikes: list[float], n: int = 2001) -> np.ndarray:
    s = max(sigma, 0.05) * math.sqrt(max(T, 1 / 365))
    lo, hi = S * math.exp(-7 * s), S * math.exp(7 * s)
    g = np.linspace(min(lo, min(strikes) * 0.8), max(hi, max(strikes) * 1.2), n)
    return np.unique(np.concatenate([g, np.asarray(strikes, dtype=float), [1e-6]]))


def pnl_at(cand_legs: list[OptLeg], net: float, ST: np.ndarray, at_expiry: str, today: dt.date) -> np.ndarray:
    """P/L per share at `at_expiry`. Legs expiring then pay intrinsic; later legs are valued with Black-Scholes
    at their own IV held constant (an estimate). net: + credit received / - debit paid."""
    out = np.full_like(ST, net, dtype=float)
    t0 = dt.date.fromisoformat(at_expiry)
    for l in cand_legs:
        e = dt.date.fromisoformat(l.expiry)
        if e <= t0:
            v = np.maximum(ST - l.strike, 0) if l.type == "C" else np.maximum(l.strike - ST, 0)
        else:
            T = max((e - t0).days, 1) / 365
            v = bs.price(ST, l.strike, T, l.iv, l.type)
        out += l.sign * l.qty * v
    return out


def lognormal_cdf(x: np.ndarray, S: float, sigma: float, T: float) -> np.ndarray:
    s = max(sigma, 1e-4) * math.sqrt(max(T, 1e-6))
    with np.errstate(divide="ignore"):
        z = (np.log(np.maximum(x, 1e-12) / S) + 0.5 * s * s) / s
    return bs._ncdf(z)


def calls_covered(legs: list[OptLeg]) -> bool:
    """Every short call is matched by a long call expiring no earlier (verticals, condors, calendars,
    diagonals). Then the upside is bounded whatever the grid says; BS noise on a long back-month call far
    in the money must not turn a calendar into 'undefined risk'."""
    longs = sorted(([l.expiry, l.qty] for l in legs if l.type == "C" and l.action == "BUY"), reverse=True)
    for s in sorted((l for l in legs if l.type == "C" and l.action == "SELL"), key=lambda l: l.expiry, reverse=True):
        need = s.qty
        for lg in longs:
            if lg[0] >= s.expiry and lg[1] > 0 and need > 0:
                take = min(lg[1], need)
                lg[1] -= take
                need -= take
        if need > 0:
            return False
    return True


def payoff_stats(legs: list[OptLeg], net: float, S: float, sigma: float, at_expiry: str, today: dt.date) -> dict:
    """max_profit/max_loss ($/contract, max_loss None if unbounded), breakevens, PoP (lognormal, estimate)."""
    T = max((dt.date.fromisoformat(at_expiry) - today).days, 1) / 365
    ks = [l.strike for l in legs]
    g = _grid(S, sigma, T, ks)
    p = pnl_at(legs, net, g, at_expiry, today)
    # PoP: probability mass where P/L >= 0, regions split at sign changes
    cdf = lognormal_cdf(g, S, sigma, T)
    mid_ok = (p[:-1] >= 0) & (p[1:] >= 0)
    pop = float(np.sum(np.diff(cdf)[mid_ok]))
    tail_hi = 1 - cdf[-1]
    if p[-1] >= 0:
        pop += float(tail_hi)
    # breakevens: linear interpolation at sign changes
    be = []
    sgn = np.sign(p)
    for i in np.where(sgn[:-1] * sgn[1:] < 0)[0]:
        x0, x1, y0, y1 = g[i], g[i + 1], p[i], p[i + 1]
        be.append(round(float(x0 - y0 * (x1 - x0) / (y1 - y0)), 2))
    # max loss: unbounded if the position is net short calls at the top of the grid (slope < 0 far out)
    far = np.array([g[-1], g[-1] * 4])
    pf = pnl_at(legs, net, far, at_expiry, today)
    unbounded = pf[1] < pf[0] - 1e-6 and pf[1] < 0 and not calls_covered(legs)
    lo = float(min(p.min(), pf.min()))
    hi = float(max(p.max(), pf.max()))
    return {"pop": round(min(max(pop, 0.0), 1.0), 3), "breakevens": be,
            "max_loss": None if unbounded else round(max(-lo, 0) * 100, 2),
            "max_profit": round(hi * 100, 2), "grid": g, "pnl": p}


# ---------------------------------------------------------------------------------------------------
# liquidity (options-scanner weights: spread .40 / OI .30 / volume .20 / staleness .10)

def _ramp(x, lo, hi):
    if hi == lo:
        return 1.0
    return float(min(max((x - lo) / (hi - lo), 0.0), 1.0))


def leg_liquidity(l: OptLeg, fresh: bool = True) -> float:
    s = 0.40 * _ramp(-l.spread_pct, -0.25, -0.02) + 0.30 * _ramp(l.oi, 50, 1000) + \
        0.20 * _ramp(l.volume, 0, 200) + 0.10 * (1.0 if fresh and l.bid > 0 else 0.0)
    return round(s, 3)


def check_liquidity(c: Candidate, liq: dict, fresh: bool = True) -> tuple[bool, str]:
    """Hard per-leg rules + the score. Long wings get twice the spread allowance (they're cheap)."""
    bad = []
    for l in c.legs:
        lim = liq["max_leg_spread_pct"] * (1 if l.action == "SELL" else 2)
        if l.spread_pct > lim:
            bad.append(f"{l.strike:g}{l.type} {l.expiry} bid-ask {l.spread_pct:.0%} of mid > {lim:.0%}")
        if l.oi < liq["min_leg_oi"]:
            bad.append(f"{l.strike:g}{l.type} {l.expiry} OI {l.oi:.0f} < {liq['min_leg_oi']}")
        if l.action == "SELL" and l.bid <= 0:
            bad.append(f"{l.strike:g}{l.type} has no bid")
    scores = [leg_liquidity(l, fresh) for l in c.legs]
    c.metrics["liquidity_score"] = round(float(np.mean(scores)), 3) if scores else 0.0
    c.metrics["roundtrip_cost"] = round(sum(l.spread * l.qty for l in c.legs), 2)
    if abs(c.net) > 0:
        c.metrics["roundtrip_pct_of_net"] = round(c.metrics["roundtrip_cost"] / abs(c.net), 3)
    return (not bad), "; ".join(bad)


def check_calendar_cost(c: Candidate, liq: dict) -> tuple[bool, str]:
    """timps0n guard: if crossing every leg's spread costs more than X of the debit, the edge is gone."""
    r = c.metrics.get("roundtrip_pct_of_net")
    lim = liq.get("calendar_max_roundtrip", 0.25)
    if r is None or r > lim:
        return False, f"round-trip bid-ask {c.metrics.get('roundtrip_cost')} = {r:.0%} of the debit > {lim:.0%}" \
            if r is not None else "no debit"
    return True, ""


# ---------------------------------------------------------------------------------------------------
# exits and sizing

def time_exit_date(today: dt.date, expiry: str, time_exit_dte: int = 21) -> dt.date:
    """Earlier of (expiry - time_exit_dte) and halfway to expiry; never before tomorrow."""
    e = dt.date.fromisoformat(expiry)
    dte = (e - today).days
    half = today + dt.timedelta(days=max(dte // 2, 1))
    by_dte = e - dt.timedelta(days=time_exit_dte)
    d = min(half, by_dte) if by_dte > today else half
    return max(d, today + dt.timedelta(days=1))


def credit_exits(c: Candidate, tcfg: dict, today: dt.date, time_exit: dt.date | None = None) -> None:
    """Short premium: TP at 50% of credit; stop when cost to close = 2x credit (loss = 1x credit), or the
    underlying closes beyond a short strike; time exit. No rolling."""
    credit, m, tp = c.net, stop_multiple(tcfg), tcfg.get("take_profit_pct", 0.50)
    texit = time_exit or time_exit_date(today, c.expiry, tcfg.get("time_exit_dte", 21))
    c.tp_value = round(credit * (1 - tp), 2)
    c.stop_value = round(credit * m, 2)
    c.time_exit = texit.isoformat()
    c.stop_rule = f"Stop = {m:g}x credit; {NO_ROLL.lower().rstrip('.')}"
    c.take_profit = f"Buy back at {c.tp_value:.2f} ({int(tp * 100)}% of the {credit:.2f} credit kept)"
    loss = credit * (m - 1) * 100
    capped = c.max_loss is not None and loss > c.max_loss
    if capped:  # F2: a wide multiple on a narrow spread cannot lose more than the spread's max loss
        loss = c.max_loss
    lvl = []
    if c.stop_below:
        lvl.append(f"{c.symbol} closes below {c.stop_below:g}")
    if c.stop_above:
        lvl.append(f"{c.symbol} closes above {c.stop_above:g}")
    loss_txt = f"loss capped at max loss = ${loss:,.0f}/contract" if capped else \
        f"loss = 1x credit = ${loss:,.0f}/contract"
    c.stop = ("Close if cost to close >= " + f"{c.stop_value:.2f} ({m:g}x the {credit:.2f} credit: {loss_txt})"
              + ("" if not lvl else " or " + " or ".join(lvl)) + f". {NO_ROLL}")
    c.metrics["stop_loss_usd"] = round(loss, 2)
    c.time_stop = f"Close on {c.time_exit} if neither target nor stop has hit"
    if c.width and c.stop_value >= c.width:
        c.notes.append(f"Credit >= 1/{m:g} of the width, so the {m:g}x stop sits at max loss: the underlying-close "
                       "stop is the one that matters")


def debit_exits(c: Candidate, build: dict, today: dt.date, invalid_lo: float | None, invalid_hi: float | None,
                exit_date: dt.date) -> None:
    """Long premium (calendars/diagonals): max loss = debit; TP +X% of debit; stop when value <= Y% of debit;
    time exit before the front expiry; invalidation = underlying closes outside the profit tent. No rolling."""
    d = -c.net
    tp, sv = build.get("take_profit_pct", 0.25), build.get("stop_value_pct", 0.50)
    c.tp_value = round(d * (1 + tp), 2)
    c.stop_value = round(d * sv, 2)
    c.stop_below, c.stop_above = invalid_lo, invalid_hi
    c.time_exit = exit_date.isoformat()
    c.stop_rule = f"Debit: max loss = the {d:.2f} debit; stop at {int(sv * 100)}% of it; no rolling"
    c.take_profit = f"Sell the spread at {c.tp_value:.2f} (+{int(tp * 100)}% on the debit)"
    lvl = " or ".join(x for x in (f"{c.symbol} closes below {invalid_lo:g}" if invalid_lo else "",
                                  f"{c.symbol} closes above {invalid_hi:g}" if invalid_hi else "") if x)
    c.stop = (f"Max loss = the {d:.2f} debit (${d * 100:,.0f}/contract). Close if the spread is worth <= {c.stop_value:.2f}"
              + (f" or {lvl}" if lvl else "") + f". {NO_ROLL}")
    c.invalidation = (f"{lvl} (outside the profit tent)" if lvl else "") or c.invalidation
    c.time_stop = f"Close on {c.time_exit} at the latest (before the front month's gamma takes over)"


def size(c: Candidate, tcfg: dict, factor: float = 1.0) -> None:
    budget = tcfg.get("account_size", 25000) * tcfg.get("max_risk_per_trade_pct", 0.02) * factor
    if c.strategy == "csp":
        c.contracts = 1
        c.metrics["cash_required"] = round(c.legs[0].strike * 100 - c.net * 100)
        return
    risk = c.max_loss if c.max_loss is not None else (c.net * (stop_multiple(tcfg) - 1) * 100 if c.net > 0 else None)
    if not risk:
        return
    c.contracts = max(1, int(budget // max(risk, 1)))
    c.metrics["risk_budget"] = round(budget)
    if risk > budget:
        c.metrics["over_budget"] = True  # F4: the selector drops these when risk.enforce_budget is on
        c.warnings.append(f"1 contract risks ${risk:,.0f} > the ${budget:,.0f} per-trade budget"
                          + (f" (x{factor:g} for this strategy)" if factor != 1 else ""))


# ---------------------------------------------------------------------------------------------------
# trade-level gates shared by builders

def earnings_inside(ctx: NameContext, last_expiry: str, buffer_days: int) -> tuple[bool | None, str]:
    """True if the next report lands before last_expiry + buffer. None if the date is unknown (non-ETF)."""
    info = ctx.info
    if info is not None and info.is_etf:
        return False, ""
    if info is None or info.next_earnings is None:
        return None, "next earnings date unknown: check it is after expiry before trading"
    e = dt.date.fromisoformat(last_expiry)
    if ctx.today <= info.next_earnings <= e + dt.timedelta(days=buffer_days):
        return True, f"earnings {info.next_earnings} {info.timing} fall inside the trade (expiry {last_expiry} + {buffer_days}d)"
    return False, ""


def exdiv_check(ctx: NameContext, c: Candidate) -> None:
    """Short calls before an ex-dividend date with extrinsic < dividend risk early assignment."""
    info = ctx.info
    if info is None or not info.ex_div_date or not info.dividend:
        return
    for l in c.legs:
        if l.action != "SELL" or l.type != "C":
            continue
        if ctx.today <= info.ex_div_date <= dt.date.fromisoformat(l.expiry):
            intrinsic = max(ctx.chain.spot - l.strike, 0)
            extrinsic = l.mid - intrinsic
            msg = (f"ex-dividend {info.ex_div_date} (${info.dividend:.2f}) before {l.expiry}: short {l.strike:g}C "
                   f"extrinsic {extrinsic:.2f}")
            if extrinsic < info.dividend:
                c.warnings.append(msg + " < dividend: early-assignment risk; close before ex-date")
            else:
                c.notes.append(msg + " > dividend: early assignment unlikely")


# ---------------------------------------------------------------------------------------------------
# plugin base

class Strategy:
    name = ""

    def __init__(self, sheet: dict, cfg: dict):
        self.sheet, self.cfg = sheet, cfg
        self.spec = sheet["strategies"][self.name]
        self.build_cfg = self.spec.get("build", {})
        self.liq = sheet["signals"]["liquidity"]
        self.tcfg = cfg["trades"]
        self.unknown_credit = sheet.get("confidence", {}).get("unknown_credit", 0.5)

    # override
    def build(self, ctx: NameContext, gates: GateResult, book: SignalBook) -> list[Candidate]:
        raise NotImplementedError

    def eligible(self, ctx: NameContext) -> tuple[bool, str]:
        return True, ""

    def new(self, ctx: NameContext, expiry: str, dte: int, legs: list[OptLeg], gates: GateResult, **kw) -> Candidate:
        net = sum(-l.sign * l.qty * l.mid for l in legs)
        nat = sum((l.bid if l.action == "SELL" else -l.ask) * l.qty for l in legs)
        sp = self.spec
        c = Candidate(ctx.symbol, kw.pop("strategy", self.name), kw.pop("label", sp.get("label", self.name)),
                      sp.get("direction", ""), sp.get("vol_view", ""), "credit" if net > 0 else "debit",
                      kw.pop("risk", sp.get("risk", "defined")), expiry, dte, legs, net=round(net, 2),
                      net_natural=round(nat, 2), spot=ctx.chain.spot, as_of=ctx.as_of, sources=dict(ctx.fresh),
                      **kw)
        c.gates = GateResult(gates.strategy, gates.symbol, gates.passed, list(gates.decisions), list(gates.warnings),
                             list(gates.failed))
        c.warnings += c.gates.warnings
        if ctx.fs is not None and ctx.fs.approx:
            c.flags.append("PROXY FLOW")
        return c

    def finish(self, ctx: NameContext, c: Candidate, at_expiry: str | None = None, sigma: float | None = None) -> None:
        """payoff stats from the legs (PoP / breakevens / max loss / max profit)."""
        at = at_expiry or min(l.expiry for l in c.legs)
        tp = ctx.nv.tent(at) if ctx.nv else None
        iv = sigma or (tp.atm_iv if tp and tp.atm_iv else float(np.mean([l.iv for l in c.legs])))
        st = payoff_stats(c.legs, c.net, ctx.chain.spot, iv, at, ctx.today)
        c.pop, c.breakevens = st["pop"], st["breakevens"]
        if c.max_loss is None and st["max_loss"] is not None and c.risk == "defined":
            c.max_loss = st["max_loss"]
        if c.max_profit is None:
            c.max_profit = st["max_profit"]
        if st["max_loss"] is None:
            c.risk = "undefined"
            c.max_loss = None
        c.metrics["pop_model"] = f"lognormal at {at} ATM IV {iv:.1%} (estimate)"
        if tp and tp.straddle:
            c.metrics["expected_move"] = {"expiry": at, "em": tp.straddle, "em_pct": tp.em_pct,
                                          "lower": tp.lower, "upper": tp.upper}

    def trade_gate(self, c: Candidate, ok: bool, gate: str, why: str, category: str = "") -> bool:
        if not ok:
            c.gates.reject(gate, why, category)
        else:
            from ..signals.ruleset import GateDecision
            c.gates.add(GateDecision(gate, "trade", "pass", "pass", "pass", category, None, why))
        return ok

    def common_checks(self, ctx: NameContext, c: Candidate, avoid_earnings: bool | None = None) -> bool:
        avoid = self.spec.get("avoid_earnings_in_trade", False) if avoid_earnings is None else avoid_earnings
        last = max(l.expiry for l in c.legs)
        if avoid:
            inside, why = earnings_inside(ctx, last, self.tcfg.get("earnings_buffer_days", 5))
            if inside is None:
                c.warnings.append(why)
            elif not self.trade_gate(c, not inside, "earnings_in_trade", why or "no report before expiry", "event"):
                return False
        ok, why = check_liquidity(c, self.liq, ctx.fresh_ok)
        if not self.trade_gate(c, ok, "leg_liquidity", why or f"score {c.metrics['liquidity_score']:.2f}", "liquidity"):
            return False
        if c.net > 0 and not self.credit_cost_check(c):
            return False
        exdiv_check(ctx, c)
        return True

    def credit_cost_check(self, c: Candidate) -> bool:
        """F3: crossing every leg's spread to open and close must not eat the credit. Above `credit_roundtrip_max`
        the idea is dropped; above `credit_roundtrip_warn` it is flagged and confidence takes a liquidity penalty."""
        r = c.metrics.get("roundtrip_pct_of_net")
        if r is None:
            return True
        hi, warn = self.liq.get("credit_roundtrip_max", 1.0), self.liq.get("credit_roundtrip_warn", 0.5)
        msg = f"round-trip bid-ask {c.metrics.get('roundtrip_cost')} = {r:.0%} of the {c.net:.2f} credit"
        if not self.trade_gate(c, r <= hi, "credit_roundtrip", msg + (f" > {hi:.0%}" if r > hi else f" <= {hi:.0%}"),
                               "liquidity"):
            return False
        if r > warn:
            c.warnings.append(msg + f" > {warn:.0%}: fills at mid matter")
            c.metrics["roundtrip_penalty"] = round(min((r - warn) / max(hi - warn, 1e-9), 1.0), 3)
        return True

    def evaluate(self, universe: list[NameContext], signals: SignalBook) -> list[Candidate]:
        out: list[Candidate] = []
        for ctx in universe:
            ok, why = self.eligible(ctx)
            if not ok:
                signals.log(ctx.symbol, self.name, f"not eligible: {why}")
                continue
            states = signals.states(ctx.symbol)
            gr = evaluate_gates(self.name, self.spec, states, ctx.symbol, self.unknown_credit)
            signals.gates.setdefault(ctx.symbol, {})[self.name] = gr
            if not gr.passed:
                signals.log(ctx.symbol, self.name, "gates failed: " + "; ".join(gr.failed))
                continue
            try:
                built = self.build(ctx, gr, signals)
            except Exception as e:  # a builder bug must not kill the run
                signals.log(ctx.symbol, self.name, f"builder error: {type(e).__name__}: {e}")
                continue
            if not built:
                signals.log(ctx.symbol, self.name, "gates passed but no structure met the build rules")
            for c in built:
                if c.gates.passed:
                    out.append(c)
                else:
                    signals.log(ctx.symbol, self.name, f"{c.label} rejected: " + "; ".join(c.gates.failed))
        return out
