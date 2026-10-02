"""Iron condor: ~16-delta shorts nudged to the expected-move tent edges, wings at ~width_pct of spot.

The short strangle (same shorts, no wings) is UNDEFINED RISK and only built with engine.allow_undefined_risk.
"""
from __future__ import annotations

import pandas as pd

from .base import Candidate, Strategy, credit_exits, leg_table, mk_leg, nearest_strike, pick_expiry, size


def _short_at_edge(t: pd.DataFrame, cp: str, edge: float | None, target_delta: float, max_spread: float):
    """Short strike nearest the tent edge, kept inside a sane delta band (0.6x-1.6x target)."""
    lo, hi = target_delta * 0.6, target_delta * 1.6
    band = t[t["delta_use"].abs().between(lo, hi) & (t["bid"] > 0) & (t["spread_pct"] <= max_spread)]
    if band.empty:
        return None
    if edge:
        beyond = band[band["strike"] <= edge] if cp == "P" else band[band["strike"] >= edge]
        pool = beyond if len(beyond) else band
        return pool.iloc[(pool["strike"] - edge).abs().argsort()].iloc[0]
    return band.iloc[(band["delta_use"].abs() - target_delta).abs().argsort()].iloc[0]


def _wing(t: pd.DataFrame, cp: str, short_k: float, width: float):
    w = t[t["strike"] < short_k] if cp == "P" else t[t["strike"] > short_k]
    return nearest_strike(w, short_k - width if cp == "P" else short_k + width)


class IronCondor(Strategy):
    name = "iron_condor"

    def build(self, ctx, gates, book) -> list[Candidate]:
        b = self.build_cfg
        ex = pick_expiry(ctx, b["dte"], b.get("target_dte"))
        if not ex:
            book.log(ctx.symbol, self.name, f"no expiry with {b['dte'][0]}-{b['dte'][1]} DTE")
            return []
        expiry, dte = ex
        S = ctx.chain.spot
        tent = ctx.nv.tent(expiry) if ctx.nv else None
        lo_edge, hi_edge = (tent.lower, tent.upper) if (tent and b.get("place") == "em_edges") else (None, None)
        tp, tc = leg_table(ctx, expiry, "P"), leg_table(ctx, expiry, "C")
        mx = self.liq["max_leg_spread_pct"]
        sp = _short_at_edge(tp, "P", lo_edge, b["delta"], mx)
        sc = _short_at_edge(tc, "C", hi_edge, b["delta"], mx)
        if sp is None or sc is None:
            book.log(ctx.symbol, self.name, "no ~16-delta short strike on one side")
            return []
        width = S * b["width_pct"]
        lp, lc = _wing(tp, "P", sp["strike"], width), _wing(tc, "C", sc["strike"], width)
        out = []
        if lp is not None and lc is not None:
            legs = [mk_leg(lp, "BUY", expiry), mk_leg(sp, "SELL", expiry), mk_leg(sc, "SELL", expiry),
                    mk_leg(lc, "BUY", expiry)]
            wmax = max(legs[1].strike - legs[0].strike, legs[3].strike - legs[2].strike)
            c = self.new(ctx, expiry, dte, legs, gates, width=wmax)
            c.max_loss = round((wmax - c.net) * 100, 2)
            c.max_profit = round(c.net * 100, 2)
            c.stop_below, c.stop_above = legs[1].strike, legs[2].strike
            c.metrics.update(credit_to_width=round(c.net / wmax, 3), min_credit_to_width=b["min_credit_to_width"],
                             short_deltas=[round(abs(legs[1].delta), 2), round(abs(legs[2].delta), 2)])
            if tent:
                c.metrics["shorts_vs_tent"] = (f"shorts {legs[1].strike:g}/{legs[2].strike:g} vs expected-move tent "
                                               f"{tent.lower:g}-{tent.upper:g}")
            self.finish(ctx, c)
            credit_exits(c, self.tcfg, ctx.today)
            c.notes.append("If either short strike is breached on a close, close the WHOLE condor (no rolling).")
            if self.trade_gate(c, c.net >= b["min_credit_to_width"] * wmax, "credit_to_width",
                               f"credit {c.net:.2f} = {c.net / wmax:.0%} of the {wmax:g} width "
                               f"(min {b['min_credit_to_width']:.0%})", "structure") and self.common_checks(ctx, c):
                size(c, self.tcfg)
            out.append(c)
        else:
            book.log(ctx.symbol, self.name, "no wing strikes")

        if self.spec.get("strangle_opt_in") and self.cfg["engine"].get("allow_undefined_risk"):
            legs = [mk_leg(sp, "SELL", expiry), mk_leg(sc, "SELL", expiry)]
            s = self.new(ctx, expiry, dte, legs, gates, strategy="short_strangle",
                         label="Short strangle (UNDEFINED RISK)", risk="undefined")
            s.flags.append("UNDEFINED RISK")
            s.max_profit = round(s.net * 100, 2)
            s.stop_below, s.stop_above = legs[0].strike, legs[1].strike
            self.finish(ctx, s)
            s.max_loss = None
            credit_exits(s, self.tcfg, ctx.today)
            s.warnings.append("UNDEFINED RISK: no wings. A gap through either strike can lose far more than the credit. "
                              "Opt-in only (engine.allow_undefined_risk). Needs margin approval.")
            if self.common_checks(ctx, s):
                size(s, self.tcfg)
            out.append(s)
        return out
