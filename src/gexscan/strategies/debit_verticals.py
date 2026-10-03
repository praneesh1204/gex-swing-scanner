"""Bull call / bear put debit spreads (spec v2.2 §6.5): a directional view at a known, capped cost.

Long leg near `long_delta` (default 0.55, slightly in the money), short leg ~width_pct of spot further out,
pulled in to the dealer wall when the wall sits inside that width (price tends to stall there).
Debit must be <= max_debit_to_width of the width so the payoff is worth the risk.
Exit plan: max loss = the debit; TP +50% of the debit; stop when the spread is worth <= 50% of the debit;
invalidation = the support (bull call) / resistance (bear put) level; time exit before expiry. No rolling.
"""
from __future__ import annotations

from .base import (Candidate, Strategy, debit_exits, leg_table, mk_leg, nearest_strike, pick_expiry, size,
                   time_exit_date)


class _DebitVertical(Strategy):
    cp = "C"

    def build(self, ctx, gates, book) -> list[Candidate]:
        b = self.build_cfg
        ex = pick_expiry(ctx, b["dte"], b.get("target_dte"))
        if not ex:
            book.log(ctx.symbol, self.name, f"no expiry with {b['dte'][0]}-{b['dte'][1]} DTE")
            return []
        expiry, dte = ex
        t = leg_table(ctx, expiry, self.cp)
        S = ctx.chain.spot
        mx = self.liq["max_leg_spread_pct"]
        pool = t[(t["ask"] > 0) & (t["spread_pct"] <= mx)]
        if pool.empty:
            book.log(ctx.symbol, self.name, f"no {self.cp} strike with bid-ask <= {mx:.0%}")
            return []
        target = b.get("long_delta", 0.55)
        lg = pool.iloc[(pool["delta_use"].abs() - target).abs().argsort()].iloc[0]
        st = book.names[ctx.symbol]
        wall = (st["resistance"] if self.cp == "C" else st["support"]).value
        width = S * b.get("width_pct", 0.06)
        sgn = 1 if self.cp == "C" else -1
        k_tgt, k_min = lg["strike"] + sgn * width, lg["strike"] + sgn * width * 0.5
        wall_inside = bool(wall) and (lg["strike"] < wall < k_tgt if self.cp == "C" else k_tgt < wall < lg["strike"])
        if wall_inside and sgn * (wall - k_min) >= 0:
            k_tgt = wall          # pull the short strike in to the wall, but never under half the target width
        side = t[(t["strike"] > lg["strike"]) if self.cp == "C" else (t["strike"] < lg["strike"])]
        side = side[(side["bid"] > 0) & (side["spread_pct"] <= mx * 2)]
        sh = nearest_strike(side, k_tgt)
        if sh is None:
            book.log(ctx.symbol, self.name, f"no short {self.cp} strike near {k_tgt:.2f}")
            return []
        legs = [mk_leg(lg, "BUY", expiry), mk_leg(sh, "SELL", expiry)]
        w = abs(legs[1].strike - legs[0].strike)
        c = self.new(ctx, expiry, dte, legs, gates, width=w)
        d = -c.net
        c.max_loss = round(d * 100, 2)
        c.max_profit = round((w - d) * 100, 2)
        c.metrics.update(debit_to_width=round(d / w, 3) if w else None, max_debit_to_width=b["max_debit_to_width"],
                         long_delta=round(abs(legs[0].delta), 2), reward_to_risk=round((w - d) / d, 2) if d > 0 else None)
        lvl = "resistance" if self.cp == "C" else "support"
        if wall_inside and k_tgt == wall:
            c.notes.append(f"Short strike at the {wall:g} {lvl}: price often stalls at the wall, so profit is capped "
                           "where it is likely to stop anyway.")
        elif wall_inside:
            c.warnings.append(f"{lvl.capitalize()} at {wall:g} sits inside the spread, close to the long strike: "
                              "the move has to clear it to reach max profit.")
        self.finish(ctx, c)
        sup, res = st["support"].value, st["resistance"].value
        lo, hi = (sup if sup and sup < S else None, None) if self.cp == "C" else (None, res if res and res > S else None)
        texit = time_exit_date(ctx.today, expiry, self.tcfg.get("time_exit_dte", 21))
        debit_exits(c, b, ctx.today, lo, hi, texit)
        c.time_stop = f"Close on {c.time_exit} at the latest (the thesis has had its time; theta speeds up after)"
        if not self.trade_gate(c, d > 0, "debit", f"net {c.net:+.2f} must be a debit", "structure"):
            return [c]
        if not self.trade_gate(c, d <= b["max_debit_to_width"] * w, "debit_to_width",
                               f"debit {d:.2f} = {d / w:.0%} of the {w:g} width (max {b['max_debit_to_width']:.0%})",
                               "structure"):
            return [c]
        if not self.common_checks(ctx, c):
            return [c]
        size(c, self.tcfg)
        return [c]


class BullCall(_DebitVertical):
    name = "bull_call"
    cp = "C"


class BearPut(_DebitVertical):
    name = "bear_put"
    cp = "P"
