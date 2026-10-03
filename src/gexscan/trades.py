"""Build concrete defined-risk trade ideas from real bid/ask quotes, each with a full exit plan.

Exit rules (docs/strategy_rules.md):
  * Short premium: take profit at 50% of the credit; stop when the cost to close reaches 2x the
    credit received (i.e. the loss equals the credit), or the underlying closes beyond the short
    strike; time exit at 21 DTE or halfway to expiry, whichever comes first. No rolling: a stopped
    trade is closed, full stop.
  * Debit spreads: max loss = debit; take profit at 75% of max profit; invalidation level
    (underlying close below the put wall / long strike); same time exit.
Ideas only. Nothing here talks to a broker.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .analytics import bs
from .analytics.gex import Levels
from .analytics.technicals import Technicals
from .analytics.volatility import prob_touch
from .config import stop_multiple
from .data.cboe import Chain
from .scoring import Score


@dataclass
class Leg:
    action: str      # SELL / BUY
    type: str        # C / P
    strike: float
    bid: float
    ask: float
    mid: float
    delta: float
    oi: float


@dataclass
class Trade:
    symbol: str
    setup: str
    expiry: str
    dte: int
    legs: list = field(default_factory=list)
    net_mid: float = 0.0            # + credit / - debit, per share
    net_natural: float = 0.0        # worst-case fill
    max_profit: float = 0.0         # per contract ($)
    max_loss: float = 0.0           # per contract ($)
    breakevens: list = field(default_factory=list)
    pop_est: float | None = None    # rough probability of profit (estimate)
    p_touch_short: float | None = None
    take_profit: str = ""
    stop: str = ""
    time_stop: str = ""
    contracts: int = 1
    notes: list = field(default_factory=list)
    # machine-readable exit plan, used by the journal review
    width: float | None = None
    tp_value: float | None = None       # close when the spread's value reaches this (per share)
    stop_value: float | None = None     # credit: close when cost-to-close >= this
    stop_below: float | None = None     # close if the underlying CLOSES below this
    stop_above: float | None = None     # close if the underlying CLOSES above this
    time_exit: str | None = None        # close on/after this date
    alternatives: list = field(default_factory=list)   # e.g. cash-secured put at the same strike

    def to_dict(self):
        return asdict(self)

    @property
    def kind(self) -> str:
        return "credit" if self.net_mid > 0 else "debit"

    def summary(self) -> str:
        legs = " / ".join(f"{_g(l, 'action')} {_g(l, 'strike'):g}{_g(l, 'type')}" for l in self.legs)
        return f"{self.symbol} {self.expiry} {legs} @ {abs(self.net_mid):.2f} {self.kind}"

    def journal_legs(self) -> list[dict]:
        return [{"action": _g(l, "action"), "type": _g(l, "type"), "strike": _g(l, "strike")} for l in self.legs]


def _g(leg, k):
    return leg[k] if isinstance(leg, dict) else getattr(leg, k)


def _pick_expiry(chain: Chain, cfg: dict, earnings: dt.date | None, today: dt.date) -> tuple[dt.date | None, str | None]:
    lo, hi = cfg["target_dte_min"], cfg["target_dte_max"]
    exps = sorted({(e, int(d)) for e, d in chain.options[["expiry", "dte"]].itertuples(index=False)})
    cands = [(e, d) for e, d in exps if lo <= d <= hi]
    if not cands:
        cands = [(e, d) for e, d in exps if d >= lo][:1]
    if not cands:
        return None, "no expiry in target DTE window"
    if earnings is not None and earnings >= today:
        buf = dt.timedelta(days=cfg.get("earnings_buffer_days", 5))
        safe = [(e, d) for e, d in cands if e + buf < earnings]
        if not safe:
            return None, f"earnings {earnings} inside every candidate expiry (+{buf.days}d buffer)"
        cands = safe
    target = cfg.get("target_dte_ideal", (lo + hi) / 2)
    return min(cands, key=lambda x: abs(x[1] - target))[0], None


def time_exit_date(today: dt.date, expiry: dt.date, cfg: dict) -> dt.date:
    """Earlier of (expiry - time_exit_dte) and halfway to expiry; never before tomorrow."""
    dte = (expiry - today).days
    half = today + dt.timedelta(days=max(dte // 2, 1))
    by_dte = expiry - dt.timedelta(days=cfg.get("time_exit_dte", 21))
    d = min(half, by_dte) if by_dte > today else half
    return max(d, today + dt.timedelta(days=1))


def _leg_table(chain: Chain, expiry: dt.date, cp: str) -> pd.DataFrame:
    e = chain.options[(chain.options["expiry"] == expiry) & (chain.options["type"] == cp)].copy()
    e = e[(e["bid"] >= 0) & (e["ask"] > 0)].sort_values("strike")
    S = chain.spot
    T = max(int(e["dte"].iloc[0]) if len(e) else 1, 1) / 365.0
    iv = e["iv"].where((e["iv"] > 0.02) & (e["iv"] < 5.0)).fillna(e["iv"].median() if len(e) else 0.5)
    bsd = bs.delta(S, e["strike"].to_numpy(), T, iv.to_numpy(), np.array([cp] * len(e)))
    e["delta_use"] = np.where(e["delta"].abs() > 1e-4, e["delta"], bsd)
    e["iv_use"] = iv
    e["spread_pct"] = np.where(e["mid"] > 0, (e["ask"] - e["bid"]) / e["mid"], np.inf)
    return e


def _mk_leg(row, action) -> Leg:
    return Leg(action, row["type"], float(row["strike"]), float(row["bid"]), float(row["ask"]), float(row["mid"]),
               float(row["delta_use"]), float(row["oi"]))


def _touch(S, K, dte, iv, cp):
    try:
        return round(prob_touch(S, K, max(dte, 1) / 365, iv or 0.5, cp), 2)
    except Exception:
        return None


def _credit_spread(chain: Chain, lv: Levels, expiry, cp: str, cfg: dict, credit_ratio: float | None = None):
    S = chain.spot
    t = _leg_table(chain, expiry, cp)
    if t.empty:
        return None, "no quotes"
    dmin, dmax = cfg["short_delta_min"], cfg["short_delta_max"]
    width_target = S * cfg["credit_spread_width_pct"]
    if cp == "P":
        wall = lv.put_wall if (lv.put_wall and lv.put_wall < S) else S
        shorts = t[(t["strike"] <= wall) & (t["delta_use"].abs().between(dmin, dmax))].sort_values("strike", ascending=False)
    else:
        wall = lv.call_wall if (lv.call_wall and lv.call_wall > S) else S
        shorts = t[(t["strike"] >= wall) & (t["delta_use"].abs().between(dmin, dmax))].sort_values("strike")
    min_ratio = cfg["min_credit_to_width"] if credit_ratio is None else credit_ratio
    for _, sh in shorts.iterrows():
        if sh["spread_pct"] > cfg["max_bid_ask_pct"] or sh["bid"] <= 0:
            continue
        longs = t[t["strike"] < sh["strike"]] if cp == "P" else t[t["strike"] > sh["strike"]]
        target = sh["strike"] - width_target if cp == "P" else sh["strike"] + width_target
        if longs.empty:
            continue
        # try the 3 long strikes closest to the target width (narrower spreads keep a higher credit/width ratio)
        for _, lg in longs.iloc[(longs["strike"] - target).abs().argsort()].head(3).iterrows():
            if lg["spread_pct"] > cfg["max_bid_ask_pct"] * 2:
                continue
            width = abs(sh["strike"] - lg["strike"])
            credit_mid = sh["mid"] - lg["mid"]
            credit_nat = sh["bid"] - lg["ask"]
            if width <= 0 or credit_mid < min_ratio * width:
                continue
            return (_mk_leg(sh, "SELL"), _mk_leg(lg, "BUY"), width, credit_mid, credit_nat, float(sh["iv_use"])), None
    return None, f"no short strike meets delta {dmin}-{dmax} / credit >= {min_ratio:.0%} of width / liquidity rules"


def _credit_exits(tr: Trade, credit: float, width: float, cfg: dict, texit: dt.date) -> None:
    tp, m = cfg["take_profit_pct"], stop_multiple(cfg)
    tr.width = width
    tr.tp_value = round(credit * (1 - tp), 2)
    tr.stop_value = round(credit * m, 2)
    tr.time_exit = texit.isoformat()
    tr.take_profit = f"Buy back at {tr.tp_value:.2f} ({int(tp * 100)}% of the {credit:.2f} credit kept)"
    tr.time_stop = f"Close on {tr.time_exit} if neither target nor stop has hit"
    loss_stop = (f"cost to close >= {tr.stop_value:.2f} ({m:g}x the credit; loss = {m - 1:g}x credit "
                 f"= ${credit * (m - 1) * 100:,.0f}/contract)")
    if tr.stop_value >= width:
        tr.notes.append(f"Credit is >= 1/{m:g} of width, so the {m:g}x-credit stop sits at max loss; "
                        "the underlying-close stop is the one that matters")
    lvl = []
    if tr.stop_below:
        lvl.append(f"{tr.symbol} closes below {tr.stop_below:g}")
    if tr.stop_above:
        lvl.append(f"{tr.symbol} closes above {tr.stop_above:g}")
    tr.stop = "Close if " + " or ".join(lvl + [loss_stop]) + ". No rolling."


def _size(cfg: dict, max_loss_per_contract: float) -> int:
    acct, risk_pct = cfg.get("account_size", 25000), cfg.get("max_risk_per_trade_pct", 0.02)
    return max(1, int((acct * risk_pct) // max(max_loss_per_contract, 1)))


def _csp_alternative(chain: Chain, expiry, short: Leg, cfg: dict, dte: int, texit: dt.date) -> dict | None:
    """Wheel entry: a cash-secured put at the spread's short strike, if the cash fits the account."""
    acct = cfg.get("account_size", 25000)
    notional = short.strike * 100
    if notional > cfg.get("csp_max_notional_pct", 0.30) * acct or short.mid <= 0:
        return None
    c = short.mid
    m, tp = stop_multiple(cfg), cfg["take_profit_pct"]
    return {
        "setup": "cash_secured_put", "summary": f"SELL {short.strike:g}P {expiry} @ {c:.2f} credit (cash-secured)",
        "cash_required": round(notional - c * 100), "credit": round(c * 100),
        "yield_pct": round(c / short.strike * 100, 2), "annualised_pct": round(c / short.strike * 365 / max(dte, 1) * 100, 1),
        "take_profit": f"Buy back at {c * (1 - tp):.2f}", "stop": f"Buy back at {c * m:.2f} ({m:g}x credit; loss = {m - 1:g}x credit). No rolling.",
        "time_exit": texit.isoformat(),
        "note": "Only if you want to own 100 shares at this price (wheel). Assignment is possible any time.",
    }


def build_trade(chain: Chain, lv: Levels, sc: Score, cfg: dict, earnings: dt.date | None,
                today: dt.date | None = None) -> tuple[Trade | None, str | None]:
    today = today or dt.date.today()
    if sc.setup == "skip":
        return None, "no setup"
    expiry, why = _pick_expiry(chain, cfg, earnings, today)
    if expiry is None:
        return None, why
    dte = (expiry - today).days
    S = chain.spot
    texit = time_exit_date(today, expiry, cfg)

    if sc.setup in ("bull_put", "bear_call"):
        cp = "P" if sc.setup == "bull_put" else "C"
        res, why = _credit_spread(chain, lv, expiry, cp, cfg)
        if res is None:
            return None, why
        sh, lg, width, cm, cn, iv = res
        max_p, max_l = cm * 100, (width - cm) * 100
        be = sh.strike - cm if cp == "P" else sh.strike + cm
        tr = Trade(chain.symbol, sc.setup, expiry.isoformat(), dte, [sh, lg], round(cm, 2), round(cn, 2),
                   round(max_p), round(max_l), [round(be, 2)], round(1 - abs(sh.delta), 2),
                   _touch(S, sh.strike, dte, iv, cp), contracts=_size(cfg, max_l))
        if cp == "P":
            tr.stop_below = sh.strike
        else:
            tr.stop_above = sh.strike
        _credit_exits(tr, cm, width, cfg, texit)
        if cp == "P":
            alt = _csp_alternative(chain, expiry.isoformat(), sh, cfg, dte, texit)
            if alt:
                tr.alternatives.append(alt)
        return tr, None

    if sc.setup == "iron_condor":
        half = cfg["min_credit_to_width"] / 2          # each side of a condor needs ~half the credit
        p, why_p = _credit_spread(chain, lv, expiry, "P", cfg, credit_ratio=half)
        c, why_c = _credit_spread(chain, lv, expiry, "C", cfg, credit_ratio=half)
        if p is None or c is None:
            return None, why_p or why_c
        cm, cn = p[3] + c[3], p[4] + c[4]
        width = max(p[2], c[2])
        max_l = (width - cm) * 100
        tr = Trade(chain.symbol, "iron_condor", expiry.isoformat(), dte, [p[0], p[1], c[0], c[1]], round(cm, 2),
                   round(cn, 2), round(cm * 100), round(max_l), [round(p[0].strike - cm, 2), round(c[0].strike + cm, 2)],
                   round(max(0.0, 1 - abs(p[0].delta) - abs(c[0].delta)), 2),
                   max(_touch(S, p[0].strike, dte, p[5], "P") or 0, _touch(S, c[0].strike, dte, c[5], "C") or 0),
                   contracts=_size(cfg, max_l))
        tr.stop_below, tr.stop_above = p[0].strike, c[0].strike
        _credit_exits(tr, cm, width, cfg, texit)
        tr.notes.append("If one side's short strike is breached on a close, close the WHOLE condor (no rolling).")
        return tr, None

    if sc.setup == "call_debit":
        t = _leg_table(chain, expiry, "C")
        if t.empty:
            return None, "no call quotes"
        long_row = t.iloc[(t["strike"] - S).abs().argsort()].iloc[0]
        tgt = lv.call_wall if (lv.call_wall and lv.call_wall > long_row["strike"]) else long_row["strike"] * 1.06
        shorts = t[t["strike"] > long_row["strike"]]
        if shorts.empty:
            return None, "no short call strike"
        short_row = shorts.iloc[(shorts["strike"] - tgt).abs().argsort()].iloc[0]
        lg, sh = _mk_leg(long_row, "BUY"), _mk_leg(short_row, "SELL")
        debit_mid, debit_nat = lg.mid - sh.mid, lg.ask - sh.bid
        width = sh.strike - lg.strike
        if debit_mid <= 0 or debit_mid >= width:
            return None, "debit spread mispriced"
        max_p = width - debit_mid
        tpp = cfg.get("debit_take_profit_pct", 0.75)
        tr = Trade(chain.symbol, "call_debit", expiry.isoformat(), dte, [lg, sh], round(-debit_mid, 2), round(-debit_nat, 2),
                   round(max_p * 100), round(debit_mid * 100), [round(lg.strike + debit_mid, 2)],
                   round(float(abs(bs.delta(S, lg.strike + debit_mid, dte / 365, lv.atm_iv or 0.5, "C"))), 2),
                   contracts=_size(cfg, debit_mid * 100), width=width)
        tr.tp_value = round(debit_mid + tpp * max_p, 2)
        inval = lv.put_wall if (lv.put_wall and lv.put_wall < S) else None
        tr.stop_below = inval if inval else round(min(lg.strike, S) * 0.97, 2)
        tr.time_exit = texit.isoformat()
        tr.take_profit = f"Sell the spread at {tr.tp_value:.2f} ({int(tpp * 100)}% of max profit) or when price tags {sh.strike:g}"
        tr.stop = (f"Max loss = the {debit_mid:.2f} debit (${debit_mid * 100:,.0f}/contract). "
                   f"Invalidation: close if {chain.symbol} closes below {tr.stop_below:g}"
                   f"{' (put wall)' if inval else ''}.")
        tr.time_stop = f"Close on {tr.time_exit} if the target hasn't hit (time decay works against you)"
        tr.notes.append("Spread only: never a naked long call.")
        return tr, None
    return None, "unsupported setup"


def runaway_risk(lv: Levels, tech: Technicals, earnings: dt.date | None, today: dt.date, cfg: dict) -> list[str]:
    """Reasons NOT to sell a covered call right now (the stock may run through the strike)."""
    why = []
    if tech.trend_label == "uptrend" and tech.breakout.startswith("20d high"):
        why.append("breaking out to a 20d high on volume in an uptrend")
    if (tech.rsi14 or 0) > 70 and tech.trend_label == "uptrend":
        why.append(f"strong momentum (RSI {tech.rsi14:.0f})")
    if lv.call_wall and lv.call_wall < lv.spot:
        why.append(f"spot above the call wall {lv.call_wall:g}: no overhead dealer resistance")
    if earnings and 0 <= (earnings - today).days < cfg.get("cc_min_days_to_earnings", 5):
        why.append(f"earnings {earnings} within {cfg.get('cc_min_days_to_earnings', 5)} days")
    return why


def build_covered_call(chain: Chain, lv: Levels, tech: Technicals, shares: int, cfg: dict,
                       earnings: dt.date | None, today: dt.date) -> tuple[Trade | None, str | None]:
    if shares < 100:
        return None, "fewer than 100 shares"
    veto = runaway_risk(lv, tech, earnings, today, cfg)
    if veto:
        return None, "runaway-risk veto: " + "; ".join(veto)
    expiry, why = _pick_expiry(chain, cfg, earnings, today)
    if expiry is None:
        return None, why
    dte = (expiry - today).days
    S = chain.spot
    t = _leg_table(chain, expiry, "C")
    floor_k = max(lv.call_wall or 0, S * 1.02)
    cands = t[(t["strike"] >= floor_k) & (t["delta_use"].abs().between(cfg["short_delta_min"], cfg["short_delta_max"]))
              & (t["bid"] > 0) & (t["spread_pct"] <= cfg["max_bid_ask_pct"])]
    if cands.empty:
        return None, "no call at/above the call wall meets delta/liquidity rules"
    row = cands.iloc[(cands["delta_use"].abs() - 0.20).abs().argsort()].iloc[0]
    sh = _mk_leg(row, "SELL")
    c = sh.mid
    n = shares // 100
    tr = Trade(chain.symbol, "covered_call", expiry.isoformat(), dte, [sh], round(c, 2), round(sh.bid, 2),
               round(c * 100), 0, [], round(1 - abs(sh.delta), 2), _touch(S, sh.strike, dte, float(row["iv_use"]), "C"),
               contracts=n)
    tr.max_loss = round(c * (stop_multiple(cfg) - 1) * 100)   # planned loss on the option at the stop
    tr.stop_above = sh.strike
    _credit_exits(tr, c, sh.strike, cfg, time_exit_date(today, expiry, cfg))
    tr.notes += [f"Covers {n * 100} of your {shares} shares. Upside above {sh.strike:g} is capped until you close.",
                 "Stop protects against the stock running away: buy the call back, keep the shares. No rolling."]
    return tr, None
