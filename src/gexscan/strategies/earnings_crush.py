"""Earnings IV crush: sell the implied move when it is rich vs the stock's own history, exit the session after.

Vehicles (sheet build.vehicles), all on the first expiry that holds the print:
  * iron_condor:     shorts at/just beyond spot -/+ the straddle (the expected move), wings EM x wing_em_mult out
  * iron_fly:        short the ATM straddle, wings at -/+ EM x wing_em_mult
  * calendar:        ATM, sell the pumped earnings expiry, buy the next expiry >= 21 days later (debit)
  * double_calendar: put/call calendars at the expected-move edges (debit)
Size is cut by engine.earnings_size_factor (a gap through the wings is the normal failure mode).
"""
from __future__ import annotations

import datetime as dt

from ..signals.vol import event_day
from .base import (Candidate, Strategy, check_calendar_cost, credit_exits, debit_exits, expiries, leg_table, mk_leg,
                   nearest_strike, size)

VEHICLE_LABEL = {"iron_condor": "iron condor", "iron_fly": "iron fly", "calendar": "calendar",
                 "double_calendar": "double calendar"}


def _beyond(t, cp: str, k: float):
    """First strike at/beyond k on the OTM side (puts: <= k, calls: >= k)."""
    pool = t[t["strike"] <= k] if cp == "P" else t[t["strike"] >= k]
    pool = pool[pool["bid"] > 0]
    if pool.empty:
        return None
    return pool.iloc[(pool["strike"] - k).abs().argsort()].iloc[0]


class EarningsCrush(Strategy):
    name = "earnings_crush"

    def _factor(self) -> float:
        return float(self.cfg["engine"].get(self.spec.get("size_factor_key", "earnings_size_factor"), 0.5))

    def build(self, ctx, gates, book) -> list[Candidate]:
        nv = ctx.nv
        eday = event_day(ctx.info)
        if not (nv and nv.earn_expiry and eday):
            book.log(ctx.symbol, self.name, "no expiry holding the report")
            return []
        expiry = nv.earn_expiry
        dte = next(d for e, d in expiries(ctx) if e == expiry)
        tent = nv.tent(expiry)
        if not (tent and tent.straddle):
            book.log(ctx.symbol, self.name, f"no ATM straddle on {expiry}")
            return []
        b = self.build_cfg
        out = []
        for v in b.get("vehicles", ["iron_condor"]):
            fn = getattr(self, f"_{v}", None)
            if fn is None:
                book.log(ctx.symbol, self.name, f"unknown vehicle {v}")
                continue
            c = fn(ctx, gates, book, expiry, dte, tent, eday)
            if c is None:
                continue
            c.flags.append("EARNINGS TRADE")
            c.metrics.update(vehicle=v, implied_move_pct=nv.implied_move_pct, hist_move_pct=nv.hist_move_pct,
                             move_ratio=nv.move_ratio, front_pump=nv.front_pump, event_day=eday.isoformat())
            c.notes.append(f"Report {ctx.info.next_earnings} {ctx.info.timing}: the move lands on {eday}. Close that "
                           "session whatever the P/L; this is a one-night trade. No rolling.")
            out.append(c)
        return out

    # --- credit vehicles ------------------------------------------------------------------------------
    def _credit(self, ctx, gates, expiry, dte, legs, eday, vehicle: str) -> Candidate:
        wmax = max(legs[1].strike - legs[0].strike, legs[3].strike - legs[2].strike)
        c = self.new(ctx, expiry, dte, legs, gates, width=wmax,
                     label=f"{self.spec.get('label', self.name)}: {VEHICLE_LABEL[vehicle]}")
        c.max_loss = round((wmax - c.net) * 100, 2)
        c.max_profit = round(c.net * 100, 2)
        if vehicle == "iron_fly":  # F1: both shorts sit at the same strike, so use the breakevens as the level stop
            k = legs[1].strike
            c.stop_below, c.stop_above = round(k - c.net, 2), round(k + c.net, 2)
        else:
            c.stop_below, c.stop_above = legs[1].strike, legs[2].strike
        c.metrics["credit_to_width"] = round(c.net / wmax, 3) if wmax else None
        self.finish(ctx, c)
        credit_exits(c, self.tcfg, ctx.today, time_exit=eday)
        if not self.trade_gate(c, eday <= dt.date.fromisoformat(expiry), "event_before_expiry",
                               f"move lands {eday}, expiry {expiry}", "event"):
            return c
        if not self.trade_gate(c, c.net > 0, "credit", f"net {c.net:.2f}", "structure"):
            return c
        if self.common_checks(ctx, c, avoid_earnings=False):
            size(c, self.tcfg, self._factor())
        return c

    def _iron_condor(self, ctx, gates, book, expiry, dte, tent, eday):
        S, em = ctx.chain.spot, tent.straddle
        w = em * self.build_cfg.get("wing_em_mult", 1.0)
        tp, tc = leg_table(ctx, expiry, "P"), leg_table(ctx, expiry, "C")
        sp, sc = _beyond(tp, "P", S - em), _beyond(tc, "C", S + em)
        if sp is None or sc is None:
            book.log(ctx.symbol, self.name, "iron_condor: no strikes at the expected-move edges")
            return None
        lp = nearest_strike(tp[tp["strike"] < sp["strike"]], sp["strike"] - w)
        lc = nearest_strike(tc[tc["strike"] > sc["strike"]], sc["strike"] + w)
        if lp is None or lc is None:
            book.log(ctx.symbol, self.name, "iron_condor: no wing strikes")
            return None
        legs = [mk_leg(lp, "BUY", expiry), mk_leg(sp, "SELL", expiry), mk_leg(sc, "SELL", expiry),
                mk_leg(lc, "BUY", expiry)]
        c = self._credit(ctx, gates, expiry, dte, legs, eday, "iron_condor")
        c.metrics["shorts_vs_tent"] = f"shorts {legs[1].strike:g}/{legs[2].strike:g} vs tent {tent.lower:g}-{tent.upper:g}"
        return c

    def _iron_fly(self, ctx, gates, book, expiry, dte, tent, eday):
        S, em = ctx.chain.spot, tent.straddle
        w = em * self.build_cfg.get("wing_em_mult", 1.0)
        tp, tc = leg_table(ctx, expiry, "P"), leg_table(ctx, expiry, "C")
        common = sorted(set(tp["strike"]) & set(tc["strike"]))
        if not common:
            return None
        k = min(common, key=lambda x: abs(x - S))
        sp, sc = tp[tp["strike"] == k].iloc[0], tc[tc["strike"] == k].iloc[0]
        lp = nearest_strike(tp[tp["strike"] < k], k - w)
        lc = nearest_strike(tc[tc["strike"] > k], k + w)
        if lp is None or lc is None:
            book.log(ctx.symbol, self.name, "iron_fly: no wing strikes")
            return None
        legs = [mk_leg(lp, "BUY", expiry), mk_leg(sp, "SELL", expiry), mk_leg(sc, "SELL", expiry),
                mk_leg(lc, "BUY", expiry)]
        c = self._credit(ctx, gates, expiry, dte, legs, eday, "iron_fly")
        c.notes.append(f"Short the {k:g} straddle; profit tent = the breakevens. The fly profits most if {ctx.symbol} "
                       f"opens near {k:g} after the print.")
        return c

    # --- debit vehicles -------------------------------------------------------------------------------
    def _back(self, ctx, expiry):
        ex = expiries(ctx)
        fd = next(d for e, d in ex if e == expiry)
        backs = [(e, d) for e, d in ex if d - fd >= 21] or [(e, d) for e, d in ex if d > fd]
        return backs[0] if backs else None

    def _debit(self, ctx, gates, book, expiry, dte, legs, eday, vehicle, back):
        c = self.new(ctx, expiry, dte, legs, gates,
                     label=f"{self.spec.get('label', self.name)}: {VEHICLE_LABEL[vehicle]}")
        c.back_expiry = back
        self.finish(ctx, c, at_expiry=expiry)
        c.max_loss = round(-c.net * 100, 2)
        c.metrics["max_profit_note"] = "estimate at the front expiry, back-month IV held constant (it drops too after the print)"
        lo = min(c.breakevens) if c.breakevens else None
        hi = max(c.breakevens) if len(c.breakevens) > 1 else None
        debit_exits(c, {"take_profit_pct": 0.25, "stop_value_pct": 0.50}, ctx.today, lo, hi, eday)
        if not self.trade_gate(c, -c.net > 0, "debit", "net must be a debit", "structure"):
            return c
        if not self.common_checks(ctx, c, avoid_earnings=False):
            return c
        ok, msg = check_calendar_cost(c, self.liq)
        if self.trade_gate(c, ok, "calendar_roundtrip",
                           msg or f"round-trip {c.metrics['roundtrip_pct_of_net']:.0%} of debit", "liquidity"):
            size(c, self.tcfg, self._factor())
        return c

    def _cal_legs(self, ctx, expiry, back, cp, k):
        tf, tb = leg_table(ctx, expiry, cp), leg_table(ctx, back, cp)
        common = sorted(set(tf["strike"]) & set(tb["strike"]))
        if not common:
            return None
        kk = min(common, key=lambda x: abs(x - k))
        return [mk_leg(tf[tf["strike"] == kk].iloc[0], "SELL", expiry), mk_leg(tb[tb["strike"] == kk].iloc[0], "BUY", back)]

    def _calendar(self, ctx, gates, book, expiry, dte, tent, eday):
        bk = self._back(ctx, expiry)
        if bk is None:
            return None
        S = ctx.chain.spot
        legs = self._cal_legs(ctx, expiry, bk[0], "C" if (tent.strike or S) >= S else "P", tent.strike or S)
        if not legs:
            return None
        return self._debit(ctx, gates, book, expiry, dte, legs, eday, "calendar", bk[0])

    def _double_calendar(self, ctx, gates, book, expiry, dte, tent, eday):
        bk = self._back(ctx, expiry)
        if bk is None:
            return None
        p = self._cal_legs(ctx, expiry, bk[0], "P", tent.lower)
        c = self._cal_legs(ctx, expiry, bk[0], "C", tent.upper)
        if not p or not c or p[0].strike >= c[0].strike:
            return None
        return self._debit(ctx, gates, book, expiry, dte, p + c, eday, "double_calendar", bk[0])
