"""Portfolio guardrails applied to the day's ranked ideas before anything is logged.

  * cooling-off: if any journal idea hit its stop today (or within `cooling_off_after_stop_days`),
    today's ideas are shown as WATCH ONLY (anti-revenge-trading)
  * at most `max_new_ideas_per_day` new ideas, `max_ideas_per_symbol` per ticker (counting open ones)
  * total open risk (taken ideas + new) capped at `max_total_risk_pct` of the account
"""
from __future__ import annotations

import datetime as dt


def gate(candidates: list, open_ideas: list[dict], stops_recent: int, cfg: dict) -> tuple[list, list, list[str]]:
    """candidates: SymbolResults with .trade, best first. Returns (actionable, watch_only, notes)."""
    r, t = cfg["risk"], cfg["trades"]
    notes: list[str] = []
    if stops_recent:
        notes.append(f"Cooling-off: {stops_recent} stop(s) hit in the last {r.get('cooling_off_after_stop_days', 1)} day(s). "
                     "No new trades today, ideas are watch-only. Take a breath; don't chase losses.")
        return [], list(candidates), notes

    acct = t.get("account_size", 25000)
    cap = r.get("max_total_risk_pct", 0.10) * acct
    open_risk = sum((i.get("max_loss") or 0) * (i.get("contracts") or 1) for i in open_ideas if i.get("taken"))
    per_sym: dict[str, int] = {}
    for i in open_ideas:
        per_sym[i["symbol"]] = per_sym.get(i["symbol"], 0) + 1

    ok, watch = [], []
    for c in candidates:
        tr = c.trade
        risk = tr.max_loss * tr.contracts
        why = None
        if len(ok) >= r.get("max_new_ideas_per_day", 3):
            why = f"daily limit of {r.get('max_new_ideas_per_day', 3)} new ideas"
        elif per_sym.get(c.symbol, 0) >= r.get("max_ideas_per_symbol", 1):
            why = f"already {per_sym[c.symbol]} open idea(s) on {c.symbol}"
        elif tr.setup != "covered_call" and open_risk + risk > cap:
            # try a smaller size before giving up
            room = cap - open_risk
            if tr.max_loss > 0 and room >= tr.max_loss:
                tr.contracts = int(room // tr.max_loss)
                risk = tr.max_loss * tr.contracts
                tr.notes.append(f"size cut to {tr.contracts} to stay under the {cap:,.0f} total-risk cap")
            else:
                why = f"total open risk would exceed {cap:,.0f} ({r.get('max_total_risk_pct', 0.10):.0%} of account)"
        if why:
            c.status = f"watch only: {why}"
            watch.append(c)
            continue
        ok.append(c)
        per_sym[c.symbol] = per_sym.get(c.symbol, 0) + 1
        if tr.setup != "covered_call":
            open_risk += risk
    if open_ideas:
        notes.append(f"Open taken risk before today: ${sum((i.get('max_loss') or 0) * (i.get('contracts') or 1) for i in open_ideas if i.get('taken')):,.0f} "
                     f"of a ${cap:,.0f} cap.")
    return ok, watch, notes


def recent_stops(store, today: dt.date, days: int) -> int:
    return sum(store.stops_on(today - dt.timedelta(days=d)) for d in range(0, max(days, 1)))
