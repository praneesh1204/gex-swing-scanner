"""Cash-secured put: sell a 16-30 delta put at/below support, only if you'd own 100 shares at the strike.

Score helper from the alpaca wheel example: (1 - |delta|) x (250 / (DTE + 5)) x (bid / strike).
"""
from __future__ import annotations

from .base import Candidate, Strategy, credit_exits, leg_table, mk_leg, pick_expiry, size


def wheel_score(delta: float, dte: int, bid: float, strike: float) -> float:
    return round((1 - abs(delta)) * (250 / (dte + 5)) * (bid / strike) * 100, 3)


class CashSecuredPut(Strategy):
    name = "csp"

    def build(self, ctx, gates, book) -> list[Candidate]:
        b = self.build_cfg
        ex = pick_expiry(ctx, b["dte"], b.get("target_dte"))
        if not ex:
            book.log(ctx.symbol, self.name, f"no expiry with {b['dte'][0]}-{b['dte'][1]} DTE")
            return []
        expiry, dte = ex
        t = leg_table(ctx, expiry, "P")
        lo, hi = b["delta"]
        band = t[t["delta_use"].abs().between(lo, hi) & (t["bid"] > 0)]
        if band.empty:
            book.log(ctx.symbol, self.name, f"no put with |delta| {lo}-{hi}")
            return []
        sup = book.names[ctx.symbol]["support"].value
        below = band[band["strike"] <= sup] if sup else band.iloc[0:0]
        pool = below if len(below) else band
        row = pool.iloc[(pool["delta_use"].abs() - b.get("target_delta", 0.22)).abs().argsort()].iloc[0]
        leg = mk_leg(row, "SELL", expiry)
        c = self.new(ctx, expiry, dte, [leg], gates)
        if sup and not len(below):
            c.notes.append(f"no put in the delta band sits at/below support {sup:g}; strike is above it")
        c.max_loss = round((leg.strike - c.net) * 100, 2)     # stock to zero, cash-secured
        c.max_profit = round(c.net * 100, 2)
        c.stop_below = leg.strike
        c.metrics.update(yield_pct=round(c.net / leg.strike * 100, 2),
                         annualised_pct=round(c.net / leg.strike * 365 / max(dte, 1) * 100, 1),
                         wheel_score=wheel_score(leg.delta, dte, leg.bid, leg.strike),
                         short_delta=round(abs(leg.delta), 2))
        self.finish(ctx, c)
        credit_exits(c, self.tcfg, ctx.today)
        c.notes.append("Only if you want to own 100 shares at this strike (wheel). Assignment can happen any time.")
        acct = self.tcfg.get("account_size", 25000)
        cap = self.tcfg.get("csp_max_notional_pct", 0.30) * acct
        cash = leg.strike * 100 - c.net * 100
        if not self.trade_gate(c, cash <= cap, "cash_required",
                               f"cash to secure ${cash:,.0f} vs cap ${cap:,.0f} "
                               f"({self.tcfg.get('csp_max_notional_pct', 0.30):.0%} of account)", "liquidity"):
            return [c]
        if not self.common_checks(ctx, c):
            return [c]
        size(c, self.tcfg)
        return [c]
