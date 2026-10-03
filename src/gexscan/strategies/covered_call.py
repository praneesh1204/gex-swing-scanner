"""Covered call on shares you hold (config.yaml `holdings`): 20-30 delta call at/above resistance, with the
v2 runaway-risk veto and an ex-dividend early-assignment check."""
from __future__ import annotations

import datetime as dt

import numpy as np

from ..config import stop_multiple
from ..trades import runaway_risk
from .base import Candidate, Strategy, credit_exits, leg_table, lognormal_cdf, mk_leg, pick_expiry


class CoveredCall(Strategy):
    name = "covered_call"

    def eligible(self, ctx):
        return (ctx.shares >= 100), f"holdings {ctx.shares} < 100 shares"

    def build(self, ctx, gates, book) -> list[Candidate]:
        b = self.build_cfg
        ex = pick_expiry(ctx, b["dte"], b.get("target_dte"))
        if not ex:
            book.log(ctx.symbol, self.name, f"no expiry with {b['dte'][0]}-{b['dte'][1]} DTE")
            return []
        expiry, dte = ex
        S = ctx.chain.spot
        t = leg_table(ctx, expiry, "C")
        lo, hi = b["delta"]
        res = book.names[ctx.symbol]["resistance"].value
        floor_k = max(res or 0, S * 1.02)
        band = t[t["delta_use"].abs().between(lo, hi) & (t["bid"] > 0) & (t["strike"] >= floor_k)]
        if band.empty:
            book.log(ctx.symbol, self.name, f"no call >= {floor_k:g} with |delta| {lo}-{hi}")
            return []
        row = band.iloc[(band["delta_use"].abs() - b.get("target_delta", 0.25)).abs().argsort()].iloc[0]
        leg = mk_leg(row, "SELL", expiry, qty=1)
        n = ctx.shares // 100
        c = self.new(ctx, expiry, dte, [leg], gates, risk="defined")
        c.contracts = n
        m = stop_multiple(self.tcfg)
        c.max_profit = round(c.net * 100, 2)
        c.max_loss = round(c.net * (m - 1) * 100, 2)          # planned loss on the call at the stop
        c.stop_above = leg.strike
        T = max(dte, 1) / 365
        tp = ctx.nv.tent(expiry) if ctx.nv else None
        iv = tp.atm_iv if tp and tp.atm_iv else leg.iv
        c.pop = round(float(lognormal_cdf(np.array([leg.strike + c.net]), S, iv, T)[0]), 3)
        c.breakevens = [round(leg.strike + c.net, 2)]
        c.metrics.update(pop_model=f"P(close < {leg.strike + c.net:.2f}) lognormal at {iv:.1%} (option P/L only)",
                         yield_pct=round(c.net / S * 100, 2), short_delta=round(abs(leg.delta), 2))
        credit_exits(c, self.tcfg, ctx.today)
        c.notes += [f"Covers {n * 100} of your {ctx.shares} shares; upside above {leg.strike:g} is capped until you close.",
                    "The stock risk is yours already; max loss shown is the call's planned loss at the stop.",
                    "Stop protects against the stock running away: buy the call back, keep the shares. No rolling."]
        veto = runaway_risk(ctx.levels, ctx.tech, ctx.info.next_earnings if ctx.info else None, ctx.today, self.tcfg)
        if not self.trade_gate(c, not veto, "runaway_risk", "; ".join(veto) or "no breakout/momentum veto", "positioning"):
            return [c]
        self.common_checks(ctx, c)
        return [c]
