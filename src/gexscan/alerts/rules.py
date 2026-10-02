"""Alert rules (spec §7). Alerts only: nothing here closes, changes or places anything, anywhere.

Stop alerts (open short premium, from positions.yaml, the journal or IBKR read-only):
    ratio = cost to close / credit received
    ratio >= 2.0 (stop multiple)            -> STOP: the loss equals the credit. Close it yourself; no rolling.
    ratio >= 2.0 x stop_warn_ratio (1.75)   -> WARN: approaching the stop.
Regime alerts:
    VIX/VIX3M flips into (or out of) backwardation, VVIX spike, a name's IV rank crossing a level
    (sell / stand-down thresholds), earnings entering the window.
De-duplication: alert_state in the journal DB. An alert fires when its state changes; a STOP repeats once
a day until the position is gone.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

import pandas as pd

from ..review import mark_legs

LEVELS = ("STOP", "WARN", "REGIME", "INFO")


@dataclass
class Alert:
    key: str                     # de-duplication key, e.g. "stop:positions.yaml:#1", "ivr:UPCO:50"
    level: str                   # STOP / WARN / REGIME / INFO
    kind: str                    # stop / vix_ts / vvix / ivr / earnings
    symbol: str | None
    title: str
    message: str
    state: str = ""
    value: float | None = None
    as_of: str = ""
    source: str = ""

    def text(self) -> str:
        return f"[{self.level}] {self.title}: {self.message}"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------------------------------
# stop alerts

def mark_position(p, opts: pd.DataFrame | None) -> tuple[float | None, str]:
    """Cost to close per share (> 0 for short premium). IBKR marks win; otherwise mid on today's chain.
    Legs may carry their own expiry (calendars); the rest use the position's expiry."""
    if p.marks.get("cost_to_close") is not None:
        return float(p.marks["cost_to_close"]), p.marks.get("source", "broker mark")
    if opts is None or opts.empty:
        return None, "no chain"
    net = 0.0
    for leg in p.legs:
        try:
            exp = dt.date.fromisoformat(str(leg.get("expiry") or p.expiry)[:10])
        except ValueError:
            return None, f"bad expiry {leg.get('expiry') or p.expiry}"
        v = mark_legs([leg], opts, exp)
        if v is None:
            return None, f"no quote for {leg['action']} {leg['strike']:g}{leg['type']} {exp}"
        net += v
    return round(net, 4), "chain mid"


def stop_state(cost: float, credit: float, mult: float, warn_ratio: float) -> tuple[str, float]:
    ratio = cost / credit if credit > 0 else 0.0
    if ratio >= mult - 1e-9:
        return "stop", ratio
    if ratio >= mult * warn_ratio - 1e-9:
        return "warn", ratio
    return "ok", ratio


def stop_alert(p, cost: float, mark_src: str, mult: float, warn_ratio: float, as_of: str) -> tuple[Alert | None, dict]:
    state, ratio = stop_state(cost, p.credit, mult, warn_ratio)
    stop_px = p.credit * mult
    loss = (cost - p.credit) * 100 * p.contracts
    legs = " / ".join(f"{l['action']} {float(l['strike']):g}{l['type']}" for l in p.legs)
    row = {"symbol": p.symbol, "strategy": p.strategy, "expiry": p.expiry, "legs": legs, "credit": p.credit,
           "cost_to_close": round(cost, 2), "ratio": round(ratio, 2), "stop_at": round(stop_px, 2),
           "pnl": round(-loss, 2), "contracts": p.contracts, "state": state, "source": p.source, "ref": p.ref,
           "mark": mark_src}
    head = f"{p.symbol} {p.strategy} {p.expiry} ({legs}) x{p.contracts}"
    key = f"stop:{p.source}:{p.ref}"
    if state == "stop":
        return Alert(key, "STOP", "stop", p.symbol, f"STOP {p.symbol}",
                     f"{head}: cost to close {cost:.2f} is {ratio:.2f}x the {p.credit:.2f} credit (stop = "
                     f"{mult:g}x = {stop_px:.2f}); loss ≈ ${loss:,.0f} = {ratio - 1:.1f}x the credit (the plan caps it "
                     f"near {mult - 1:g}x). Close the position yourself. No rolling. [{p.source} {p.ref}, mark: {mark_src}]",
                     state, round(ratio, 3), as_of, p.source), row
    if state == "warn":
        return Alert(key, "WARN", "stop", p.symbol, f"near stop {p.symbol}",
                     f"{head}: cost to close {cost:.2f} is {ratio:.2f}x the {p.credit:.2f} credit; the stop is "
                     f"at {stop_px:.2f} ({mult:g}x). Plan the exit now; no rolling. [{p.source} {p.ref}]",
                     state, round(ratio, 3), as_of, p.source), row
    return None, row


# ---------------------------------------------------------------------------------------------------
# regime alerts

def vix_ts_alert(regime, states: dict, prev_state: str | None, sheet: dict) -> tuple[str | None, Alert | None]:
    """-> (current coarse state, alert on a flip). Coarse = backwardation / contango (flat counts as contango)."""
    if regime is None or regime.ts_ratio is None:
        return None, None
    at = sheet["signals"]["vix_ts"].get("backwardation_at", 1.0)
    st = states.get("vix_ts")
    cur = "backwardation" if (st.state == "backwardation" if st else regime.ts_ratio >= at) else "contango"
    if prev_state is None:
        prev_state = ("backwardation" if regime.ts_ratio_prev is not None and regime.ts_ratio_prev >= at
                      else "contango")
    if cur == prev_state:
        return cur, None
    prev_txt = f" (prev {regime.ts_ratio_prev:.3f})" if regime.ts_ratio_prev is not None else ""
    if cur == "backwardation":
        return cur, Alert("regime:vix_ts", "REGIME", "vix_ts", None, "VIX term structure flipped to backwardation",
                          f"VIX/VIX3M {regime.ts_ratio:.3f}{prev_txt}, VIX {regime.vix:.2f} vs VIX3M {regime.vix3m:.2f}. "
                          "Near-term stress is priced above 3-month: the short-premium gates now fail. Review "
                          "open short premium against its stop; no new premium selling until it flips back.",
                          cur, regime.ts_ratio, regime.as_of, regime.sources.get("VIX", ""))
    return cur, Alert("regime:vix_ts", "INFO", "vix_ts", None, "VIX term structure back in contango",
                      f"VIX/VIX3M {regime.ts_ratio:.3f}{prev_txt}. The regime gate for short premium can pass again.",
                      cur, regime.ts_ratio, regime.as_of, regime.sources.get("VIX", ""))


def vvix_alert(regime, states: dict, prev_state: str | None, spike_pct: float) -> tuple[str | None, Alert | None]:
    if regime is None or regime.vvix is None:
        return None, None
    st = states.get("vvix")
    jump = regime.vvix_5d_pct is not None and regime.vvix_5d_pct >= spike_pct
    cur = "spike" if jump or (st is not None and st.state == "spike") else (st.state if st else "normal")
    if cur != "spike" or prev_state == "spike":
        return cur, None
    chg = f", {regime.vvix_5d_pct:+.1f}% in 5 sessions" if regime.vvix_5d_pct is not None else ""
    return cur, Alert("regime:vvix", "REGIME", "vvix", None, "VVIX spike",
                      f"VVIX {regime.vvix:.1f}{chg}. Vol-of-vol is jumping: short vol has fat tails and long vega "
                      "is expensive. Check stops on open short premium; strategies gated on VVIX stand down.",
                      cur, regime.vvix, regime.as_of, regime.sources.get("VVIX", ""))


def ivr_alerts(sym: str, prev_ivr: float | None, cur_ivr: float | None, levels: list[float],
               stored: dict[float, str | None], as_of: str, source: str) -> list[tuple[str, str, Alert | None]]:
    """-> [(key, state, alert)] per level. Crossing up = premium selling turns on; down = stand-down."""
    out = []
    if cur_ivr is None:
        return out
    for lv in levels:
        key = f"ivr:{sym}:{lv:g}"
        cur = "above" if cur_ivr >= lv else "below"
        prev = stored.get(lv) or (None if prev_ivr is None else "above" if prev_ivr >= lv else "below")
        if prev is None or prev == cur:
            out.append((key, cur, None))
            continue
        frm = f"{prev_ivr:.0f} -> " if prev_ivr is not None else ""
        what = ("premium-selling gates on IV rank can pass" if cur == "above"
                else "stand down on new premium selling (IV no longer rich)")
        out.append((key, cur, Alert(key, "REGIME", "ivr", sym, f"{sym} IV rank crossed {'above' if cur == 'above' else 'below'} {lv:g}",
                                    f"IVR {frm}{cur_ivr:.0f} ({source}): {what}.", cur, round(cur_ivr, 1), as_of, source)))
    return out


def earnings_alert(sym: str, info, window: int, today: dt.date, held: bool) -> Alert | None:
    if info is None or info.next_earnings is None or info.days_to_earnings is None:
        return None
    if not 0 <= info.days_to_earnings <= window:
        return None
    d = info.next_earnings
    extra = " You hold short premium here: an earnings gap can blow through the 2x stop in one print." if held else ""
    return Alert(f"earn:{sym}:{d}", "WARN" if held else "INFO", "earnings", sym, f"{sym} earnings in {info.days_to_earnings}d",
                 f"{sym} reports {d} ({info.days_to_earnings} days, {info.earnings_source or 'source?'}). Strategies "
                 f"that need a clean window stand down; earnings-crush setups may open.{extra}",
                 "in_window", float(info.days_to_earnings), today.isoformat(), info.earnings_source or "")


def should_fire(a: Alert, stored: dict | None, today: dt.date) -> bool:
    """New state -> fire. A STOP repeats once per day while the position is still open."""
    if stored is None or stored.get("state") != a.state:
        return True
    return a.level == "STOP" and stored.get("updated") != today.isoformat()
