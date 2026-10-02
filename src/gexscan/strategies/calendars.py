"""Long-vega time spreads: calendar, double calendar (expected-move edges), double diagonal.

Thesis (any of): single-name term structure in backwardation, a high forward factor between the two
expiries (idea credit: kdubb-labs/forward-factor-backtest, MIT), or a front expiry pumped by earnings.
Guards (from the negative result in timps0n/calendar-spread-backtest, reimplemented): the round-trip bid-ask must stay under 25% of
the debit, AMC reports count from the next session, and no report may fall between the two expiries (you'd be
buying the event in the back month). P/L at the front expiry holds back-month IV constant: an estimate.
"""
from __future__ import annotations

import datetime as dt

from ..signals.vol import event_day
from .base import (Candidate, Strategy, check_calendar_cost, debit_exits, expiries, leg_table, mk_leg,
                   nearest_strike, size)


def _thesis(gates) -> list[str]:
    return [d.gate for d in gates.decisions if d.kind == "any_of" and d.outcome == "pass"]


def pick_pair(ctx, b: dict, thesis: list[str]) -> tuple[tuple[str, int], tuple[str, int], str] | None:
    """(front, back, mode). mode = 'earnings' when the only thesis is a pumped earnings front."""
    ex = expiries(ctx)
    nv = ctx.nv
    if thesis == ["earnings_front"] and nv and nv.earn_expiry:
        f = next(((e, d) for e, d in ex if e == nv.earn_expiry), None)
        if f:
            lo, hi = b["back_gap_days"]
            backs = [(e, d) for e, d in ex if lo <= d - f[1] <= hi] or [(e, d) for e, d in ex if d > f[1]][:1]
            if backs:
                return f, backs[0], "earnings"
        return None
    flo, fhi = b["front_dte"]
    lo, hi = b["back_gap_days"]
    if nv and nv.ff_front and nv.ff_back and "forward_factor" in thesis:
        f = next(((e, d) for e, d in ex if e == nv.ff_front), None)
        k = next(((e, d) for e, d in ex if e == nv.ff_back), None)
        if f and k and flo <= f[1] <= fhi and lo <= k[1] - f[1] <= hi:
            return f, k, "term"
    fronts = sorted([(e, d) for e, d in ex if flo <= d <= fhi], key=lambda x: abs(x[1] - 30))
    for f in fronts:
        backs = sorted([(e, d) for e, d in ex if lo <= d - f[1] <= hi], key=lambda x: abs(x[1] - f[1] - 30))
        if backs:
            return f, backs[0], "term"
    return None


class _TimeSpread(Strategy):
    def _pair(self, ctx, gates, book):
        b = self.build_cfg
        thesis = _thesis(gates)
        pair = pick_pair(ctx, b, thesis)
        if pair is None:
            book.log(ctx.symbol, self.name, f"no front {b['front_dte']} DTE / back +{b['back_gap_days']} days pair")
            return None
        (fe, fd), (be, bd), mode = pair
        eday = event_day(ctx.info)
        why = ""
        if eday and ctx.today <= eday:
            f, k = dt.date.fromisoformat(fe), dt.date.fromisoformat(be)
            if f < eday <= k:
                why = f"earnings {ctx.info.next_earnings} fall between {fe} and {be}: you'd pay for the event in the back month"
            elif eday <= f and mode != "earnings":
                why = f"earnings {ctx.info.next_earnings} inside the front expiry: that's an earnings trade (see earnings_crush)"
        return fe, fd, be, bd, mode, eday, thesis, why

    def _exit_date(self, ctx, fe: str, mode: str, eday):
        if mode == "earnings" and eday:
            return eday            # close the session after the report (AMC -> next day)
        b = self.build_cfg
        d = dt.date.fromisoformat(fe) - dt.timedelta(days=b.get("exit_days_before_front", 5))
        return max(d, ctx.today + dt.timedelta(days=1))

    def _finish_debit(self, ctx, c: Candidate, fe: str, be: str, mode: str, eday, thesis, why, book, pure: bool):
        nv = ctx.nv
        c.back_expiry = be
        self.finish(ctx, c, at_expiry=fe)
        if pure:
            c.max_loss = round(-c.net * 100, 2)        # calendars: worst case = the debit
        c.metrics.update(thesis=thesis, mode=mode, front_iv=round(c.legs[0].iv, 4),
                         back_iv=round(c.legs[1].iv, 4) if len(c.legs) > 1 else None,
                         ts_ratio=nv.ts_ratio if nv else None, forward_factor=nv.ff if nv else None,
                         front_pump=nv.front_pump if nv else None,
                         max_profit_note="estimate at the front expiry, back-month IV held constant")
        if mode == "earnings":
            c.flags.append("EARNINGS TRADE")
        lo = min(c.breakevens) if c.breakevens else None
        hi = max(c.breakevens) if len(c.breakevens) > 1 else None
        debit_exits(c, self.build_cfg, ctx.today, lo, hi, self._exit_date(ctx, fe, mode, eday))
        if not self.trade_gate(c, not why, "earnings_vs_expiries", why or "no report between the expiries", "event"):
            return c
        if not self.trade_gate(c, -c.net > 0, "debit", "net must be a debit", "structure"):
            return c
        if not self.common_checks(ctx, c, avoid_earnings=False):
            return c
        ok, msg = check_calendar_cost(c, self.liq)
        if self.trade_gate(c, ok, "calendar_roundtrip", msg or f"round-trip {c.metrics['roundtrip_pct_of_net']:.0%} of debit",
                           "liquidity"):
            size(c, self.tcfg)
        return c


class Calendar(_TimeSpread):
    name = "calendar"

    def build(self, ctx, gates, book):
        p = self._pair(ctx, gates, book)
        if p is None:
            return []
        fe, fd, be, bd, mode, eday, thesis, why = p
        S, lv = ctx.chain.spot, ctx.levels
        k = S
        src = "ATM"
        if self.build_cfg.get("strike") == "atm_or_wall" and lv is not None and book.names[ctx.symbol]["gex"].state == "positive":
            cands = [(n, v) for n, v in (("max +gamma strike", lv.max_pos_gamma_strike), ("call wall", lv.call_wall),
                                         ("put wall", lv.put_wall)) if v]
            near = [(n, v) for n, v in cands if abs(v / S - 1) * 100 <= self.build_cfg.get("max_wall_dist_pct", 3)]
            if near:
                src, k = min(near, key=lambda x: abs(x[1] - S))
        cp = "C" if k >= S else "P"
        tf, tb = leg_table(ctx, fe, cp), leg_table(ctx, be, cp)
        common = sorted(set(tf["strike"]) & set(tb["strike"]))
        if not common:
            book.log(ctx.symbol, self.name, "no strike quoted in both expiries")
            return []
        kk = min(common, key=lambda x: abs(x - k))
        rf, rb = tf[tf["strike"] == kk].iloc[0], tb[tb["strike"] == kk].iloc[0]
        legs = [mk_leg(rf, "SELL", fe), mk_leg(rb, "BUY", be)]
        c = self.new(ctx, fe, fd, legs, gates)
        c.notes.append(f"strike {kk:g} = {src}" + ("" if src == "ATM" else " (dealers pin price near it in positive gamma)"))
        return [self._finish_debit(ctx, c, fe, be, mode, eday, thesis, why, book, pure=True)]


class DoubleCalendar(_TimeSpread):
    name = "double_calendar"

    def _edges(self, ctx, fe):
        t = ctx.nv.tent(fe) if ctx.nv else None
        S = ctx.chain.spot
        return (t.lower, t.upper) if t and t.lower else (S * 0.95, S * 1.05)

    def build(self, ctx, gates, book):
        p = self._pair(ctx, gates, book)
        if p is None:
            return []
        fe, fd, be, bd, mode, eday, thesis, why = p
        lo, hi = self._edges(ctx, fe)
        legs = []
        for cp, edge in (("P", lo), ("C", hi)):
            tf, tb = leg_table(ctx, fe, cp), leg_table(ctx, be, cp)
            common = sorted(set(tf["strike"]) & set(tb["strike"]))
            if not common:
                book.log(ctx.symbol, self.name, f"no {cp} strike quoted in both expiries")
                return []
            kk = min(common, key=lambda x: abs(x - edge))
            legs += [mk_leg(tf[tf["strike"] == kk].iloc[0], "SELL", fe), mk_leg(tb[tb["strike"] == kk].iloc[0], "BUY", be)]
        if legs[0].strike >= legs[2].strike:
            book.log(ctx.symbol, self.name, "expected-move edges collapse to one strike")
            return []
        c = self.new(ctx, fe, fd, legs, gates)
        c.notes.append(f"put calendar {legs[0].strike:g} / call calendar {legs[2].strike:g} at the {fe} expected-move edges")
        return [self._finish_debit(ctx, c, fe, be, mode, eday, thesis, why, book, pure=True)]


class DoubleDiagonal(DoubleCalendar):
    name = "double_diagonal"

    def build(self, ctx, gates, book):
        p = self._pair(ctx, gates, book)
        if p is None:
            return []
        fe, fd, be, bd, mode, eday, thesis, why = p
        lo, hi = self._edges(ctx, fe)
        off = int(self.build_cfg.get("long_offset_strikes", 1))
        legs = []
        for cp, edge in (("P", lo), ("C", hi)):
            tf, tb = leg_table(ctx, fe, cp), leg_table(ctx, be, cp)
            sr = nearest_strike(tf, edge)
            if sr is None or tb.empty:
                book.log(ctx.symbol, self.name, f"no {cp} quotes")
                return []
            ks = sorted(tb["strike"])
            further = [k for k in ks if k < sr["strike"]] if cp == "P" else [k for k in ks if k > sr["strike"]]
            if len(further) < off:
                book.log(ctx.symbol, self.name, f"no back-month {cp} strike beyond {sr['strike']:g}")
                return []
            kl = further[-off] if cp == "P" else further[off - 1]
            legs += [mk_leg(sr, "SELL", fe), mk_leg(tb[tb["strike"] == kl].iloc[0], "BUY", be)]
        c = self.new(ctx, fe, fd, legs, gates)
        c.notes.append(f"short {legs[0].strike:g}P/{legs[2].strike:g}C {fe}, long {legs[1].strike:g}P/{legs[3].strike:g}C {be}")
        return [self._finish_debit(ctx, c, fe, be, mode, eday, thesis, why, book, pure=False)]
