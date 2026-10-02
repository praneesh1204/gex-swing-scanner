"""Bull put / bear call credit spreads: short strike in the delta band at/beyond the dealer wall, long wing up to
~width_pct of spot. Credit target ~1/3 of the width (spec); min_credit_to_width is the hard floor."""
from __future__ import annotations

import pandas as pd

from .base import Candidate, Strategy, credit_exits, leg_table, mk_leg, pick_expiry, size


def pick_vertical(t: pd.DataFrame, cp: str, S: float, delta_band, target_delta: float, width: float,
                  min_ctw: float, anchor: float | None, max_spread: float, target_ctw: float | None = None):
    """-> ((short_row, long_row, credit_mid), note) or (None, reason).
    anchor: put wall / support below (puts) or call wall / resistance above (calls).
    Shorts are tried nearest the target delta first, wings nearest the target width (never wider than 1.25x).
    The first spread at >= target_ctw wins; otherwise the best credit/width at >= min_ctw, with a note."""
    target_ctw = max(target_ctw or min_ctw, min_ctw)
    lo, hi = delta_band if isinstance(delta_band, (list, tuple)) else (delta_band * 0.6, delta_band * 1.5)
    band = t[t["delta_use"].abs().between(lo, hi) & (t["bid"] > 0) & (t["spread_pct"] <= max_spread)]
    if band.empty:
        return None, f"no {cp} strike with |delta| {lo:.2f}-{hi:.2f} and bid-ask <= {max_spread:.0%}"
    note = ""
    if anchor:
        beyond = band[band["strike"] <= anchor] if cp == "P" else band[band["strike"] >= anchor]
        if len(beyond):
            band = beyond
        else:
            note = f"no strike in the delta band sits beyond the {anchor:g} level; short strike is inside it"
    shorts = band.iloc[(band["delta_use"].abs() - target_delta).abs().argsort()]
    best = None
    for _, sh in shorts.iterrows():
        longs = t[t["strike"] < sh["strike"]] if cp == "P" else t[t["strike"] > sh["strike"]]
        longs = longs[((longs["strike"] - sh["strike"]).abs() <= width * 1.25) & (longs["spread_pct"] <= max_spread * 2)]
        if longs.empty:
            continue
        tgt = sh["strike"] - width if cp == "P" else sh["strike"] + width
        for _, lg in longs.iloc[(longs["strike"] - tgt).abs().argsort()].iterrows():
            w = abs(sh["strike"] - lg["strike"])
            cr = float(sh["mid"] - lg["mid"])
            if w <= 0 or cr < min_ctw * w:
                continue
            if cr >= target_ctw * w:
                return (sh, lg, cr), note
            if best is None or cr / w > best[2] / best[3]:
                best = (sh, lg, cr, w)
    if best is not None:
        sh, lg, cr, w = best
        miss = f"credit/width {cr / w:.0%} is under the ~{target_ctw:.0%} target (floor {min_ctw:.0%})"
        return (sh, lg, cr), "; ".join(x for x in (note, miss) if x)
    return None, f"no {cp} spread reaches credit >= {min_ctw:.0%} of width"


class _Vertical(Strategy):
    cp = "P"

    def build(self, ctx, gates, book) -> list[Candidate]:
        b = self.build_cfg
        ex = pick_expiry(ctx, b["dte"], b.get("target_dte"))
        if not ex:
            book.log(ctx.symbol, self.name, f"no expiry with {b['dte'][0]}-{b['dte'][1]} DTE")
            return []
        expiry, dte = ex
        t = leg_table(ctx, expiry, self.cp)
        S = ctx.chain.spot
        st = book.names[ctx.symbol]
        anchor = (st["support"] if self.cp == "P" else st["resistance"]).value
        res, note = pick_vertical(t, self.cp, S, b["delta"], b.get("target_delta", 0.25), S * b["width_pct"],
                                  b["min_credit_to_width"], anchor, self.liq["max_leg_spread_pct"],
                                  b.get("target_credit_to_width"))
        if res is None:
            book.log(ctx.symbol, self.name, note)
            return []
        sh, lg, _ = res
        legs = [mk_leg(sh, "SELL", expiry), mk_leg(lg, "BUY", expiry)]
        width = abs(legs[0].strike - legs[1].strike)
        c = self.new(ctx, expiry, dte, legs, gates, width=width)
        if note:
            (c.warnings if "under the" in note else c.notes).append(note)
        c.max_loss = round((width - c.net) * 100, 2)
        c.max_profit = round(c.net * 100, 2)
        if self.cp == "P":
            c.stop_below = legs[0].strike
        else:
            c.stop_above = legs[0].strike
        c.metrics["credit_to_width"] = round(c.net / width, 3)
        c.metrics["min_credit_to_width"] = b["min_credit_to_width"]
        c.metrics["target_credit_to_width"] = b.get("target_credit_to_width")
        c.metrics["short_delta"] = round(abs(legs[0].delta), 2)
        self.finish(ctx, c)
        credit_exits(c, self.tcfg, ctx.today)
        if not self.common_checks(ctx, c):
            return [c]
        size(c, self.tcfg)
        return [c]


class BullPut(_Vertical):
    name = "bull_put"
    cp = "P"


class BearCall(_Vertical):
    name = "bear_call"
    cp = "C"
