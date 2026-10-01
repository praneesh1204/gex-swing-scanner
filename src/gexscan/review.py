"""Daily review of open ideas in the journal: mark to today's chain, check every exit rule, label the setup.

Each open idea gets:
  * a mark (spread value at today's mids) and P/L, estimated from delayed quotes
  * an exit check: take-profit / stop (2x-credit loss or underlying close beyond the level) / time exit / expired
  * a setup label from the levels it was built on: "setup intact", "watch/reversal", "key level broken"
  * one action: HOLD, or CLOSE (reason). Never "roll".
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

BULLISH = {"bull_put", "call_debit", "cash_secured_put"}
BEARISH = {"bear_call", "covered_call"}
EXIT_NAMES = {"tp": "take profit", "stop": "stop hit", "time": "time exit"}


def _f(x):
    try:
        v = float(x)
        return None if np.isnan(v) else v
    except (TypeError, ValueError):
        return None


def mark_legs(legs: list[dict], opts: pd.DataFrame, expiry: dt.date) -> float | None:
    """Current net at mid, same sign convention as entry: SELL legs +mid, BUY legs -mid."""
    e = opts[opts["expiry"] == expiry]
    net = 0.0
    for l in legs:
        q = e[(e["type"] == l["type"]) & (np.isclose(e["strike"], float(l["strike"])))]
        if q.empty or (q["mid"].iloc[0] <= 0 and q["ask"].iloc[0] <= 0):
            return None
        m = float(q["mid"].iloc[0])
        net += m if l["action"] == "SELL" else -m
    return net


def intrinsic_net(legs: list[dict], S: float) -> float:
    net = 0.0
    for l in legs:
        k = float(l["strike"])
        v = max(S - k, 0) if l["type"] == "C" else max(k - S, 0)
        net += v if l["action"] == "SELL" else -v
    return net


def _level_triggers(idea: dict, lv, tech, cfg: dict) -> tuple[str, list[str]]:
    S = lv.spot
    setup = idea["setup"]
    p_spot, p_pw, p_cw, p_flip, p_gex = (_f(idea.get(c)) for c in ("spot", "put_wall", "call_wall", "gamma_flip", "net_gex"))
    shorts = [float(l["strike"]) for l in idea["legs"] if l["action"] == "SELL"]
    chg = (S / p_spot - 1) * 100 if p_spot else None
    broken, watch = [], []
    if setup in BULLISH or setup == "iron_condor":
        if p_pw and S < p_pw:
            broken.append(f"spot {S:.2f} below prior put wall {p_pw:g}")
        if lv.put_wall and S < lv.put_wall and setup != "iron_condor" and not (p_pw and S < p_pw):
            watch.append(f"put wall moved to {lv.put_wall:g}, above spot (support shifting)")
        if p_flip and S < p_flip and setup != "iron_condor":
            broken.append(f"crossed below prior gamma flip {p_flip:.2f}")
        put_shorts = [float(l["strike"]) for l in idea["legs"] if l["action"] == "SELL" and l["type"] == "P"]
        if put_shorts and S < max(put_shorts):
            broken.append(f"through short put {max(put_shorts):g}")
        if chg is not None and chg < -3 and setup != "iron_condor":
            watch.append(f"price reversal {chg:+.1f}% since entry")
    if setup in BEARISH or setup == "iron_condor":
        if p_cw and S > p_cw:
            broken.append(f"spot above prior call wall {p_cw:g}")
        call_shorts = [float(l["strike"]) for l in idea["legs"] if l["action"] == "SELL" and l["type"] == "C"]
        if call_shorts and S > min(call_shorts):
            broken.append(f"through short call {min(call_shorts):g}")
        if chg is not None and chg > 3 and setup != "iron_condor":
            watch.append(f"price reversal {chg:+.1f}% since entry")
    if p_gex and lv.net_gex is not None:
        if (p_gex > 0) != (lv.net_gex > 0):
            broken.append(f"GEX changed sign ({p_gex / 1e6:,.1f}M -> {lv.net_gex / 1e6:,.1f}M)")
        elif p_gex > 0 and (lv.net_gex / p_gex - 1) * 100 < -cfg["review"]["gex_weaken_pct"]:
            watch.append(f"GEX weakened {(lv.net_gex / p_gex - 1) * 100:.0f}%")
    if tech is not None and tech.last_day_pct is not None and abs(tech.last_day_pct) >= cfg["review"]["big_move_pct"]:
        watch.append(f"unusual 1-day move {tech.last_day_pct:+.1f}%")
    label = "key level broken" if broken else "watch/reversal" if watch else "setup intact"
    return label, broken + watch


def _plan(idea: dict, cfg: dict) -> dict:
    """Exit plan stored with the idea, or derived from config for ideas imported from v0.1."""
    t = cfg["trades"]
    e = float(idea["entry_net"])
    credit = e > 0
    tp, stop = _f(idea.get("tp_value")), _f(idea.get("stop_value"))
    below, above = _f(idea.get("stop_below")), _f(idea.get("stop_above"))
    if tp is None:
        if credit:
            tp = e * (1 - t["take_profit_pct"])
            stop = e * (1 + t.get("stop_loss_credit_multiple", 2.0))
        else:
            w = _f(idea.get("width")) or 0
            tp = -e + t.get("debit_take_profit_pct", 0.75) * (w + e)
    if below is None and above is None:
        for l in idea["legs"]:
            if l["action"] == "SELL" and l["type"] == "P" and credit:
                below = float(l["strike"])
            if l["action"] == "SELL" and l["type"] == "C" and credit:
                above = float(l["strike"])
        if not credit:
            below = _f(idea.get("put_wall"))
    texit = idea.get("time_exit")
    if not texit:
        from .trades import time_exit_date
        texit = time_exit_date(dt.date.fromisoformat(idea["run_date"]), dt.date.fromisoformat(idea["expiry"]), t).isoformat()
    return {"credit": credit, "tp": tp, "stop": stop, "below": below, "above": above, "time_exit": texit}


def review_idea(idea: dict, chain, lv, tech, cfg: dict, today: dt.date) -> dict:
    """Mark and check one open idea. `chain`/`lv` may be None when data is unavailable."""
    plan = _plan(idea, cfg)
    expiry = dt.date.fromisoformat(idea["expiry"])
    e = float(idea["entry_net"])
    n = int(idea.get("contracts") or 1)
    out = {"id": idea["id"], "symbol": idea["symbol"], "setup": idea["setup"], "run_date": idea["run_date"],
           "summary": idea.get("summary", ""), "prev_trade": idea.get("summary", ""), "taken": bool(idea.get("taken")),
           "expiry": idea["expiry"], "entry_net": e, "contracts": n, "prev_spot": _f(idea.get("spot")),
           "plan": plan, "label": "no data", "triggers": [], "status": "open", "mark": None, "pnl": None,
           "pnl_pct": None, "spot": None, "chg_pct": None, "action": "HOLD", "exit_reason": ""}
    if lv is None:
        out["action"] = "CHECK MANUALLY (no data today)"
        return out
    S = lv.spot
    out["spot"] = S
    out["chg_pct"] = (S / out["prev_spot"] - 1) * 100 if out["prev_spot"] else None
    out["label"], out["triggers"] = _level_triggers(idea, lv, tech, cfg)

    if expiry < today:
        cur, status, reason = intrinsic_net(idea["legs"], S), "expired", f"expired {expiry} (P/L at intrinsic vs spot {S:.2f})"
    else:
        cur = mark_legs(idea["legs"], chain.options, expiry) if chain is not None else None
        status, reason = "open", ""
    if cur is not None:
        out["mark"] = round(abs(cur), 2)
        pnl = (e - cur) * 100 * n
        out["pnl"] = round(pnl, 2)
        risk = _f(idea.get("max_loss")) or abs(e) * 100
        out["pnl_pct"] = round(pnl / (risk * n) * 100, 1) if risk else None

    if status == "open":
        close = S   # scan runs after the close on delayed data; spot ~ the day's close
        hits = []
        if cur is not None:
            value = cur if plan["credit"] else -cur        # cost to close (credit) / spread value (debit)
            if plan["credit"] and value <= plan["tp"]:
                hits.append(("tp", f"profit target: cost to close {value:.2f} <= {plan['tp']:.2f}"))
            if not plan["credit"] and value >= plan["tp"]:
                hits.append(("tp", f"profit target: spread worth {value:.2f} >= {plan['tp']:.2f}"))
            if plan["credit"] and plan["stop"] is not None and value >= plan["stop"]:
                hits.append(("stop", f"stop: cost to close {value:.2f} >= {plan['stop']:.2f} (loss >= 2x credit)"))
        if plan["below"] is not None and close < plan["below"]:
            hits.append(("stop", f"stop: {idea['symbol']} {close:.2f} closed below {plan['below']:g}"))
        if plan["above"] is not None and close > plan["above"]:
            hits.append(("stop", f"stop: {idea['symbol']} {close:.2f} closed above {plan['above']:g}"))
        if today.isoformat() >= plan["time_exit"]:
            hits.append(("time", f"time exit reached ({plan['time_exit']})"))
        if hits:
            # stop outranks target outranks time
            order = {"stop": 0, "tp": 1, "time": 2}
            hits.sort(key=lambda h: order[h[0]])
            status, reason = hits[0][0], "; ".join(h[1] for h in hits)
    out["status"], out["exit_reason"] = status, reason
    if status == "open":
        out["action"] = "HOLD" if out["label"] != "key level broken" else "HOLD, but levels broke: be ready to close at the stop"
    elif status == "expired":
        out["action"] = "EXPIRED: record the result"
    else:
        out["action"] = f"CLOSE ({EXIT_NAMES[status]}). No rolling."
    return out


def weekly_stats(ideas: list[dict], since: dt.date | None = None) -> dict:
    """Closed-idea stats for the weekly review (estimates from delayed mids)."""
    rows = [i for i in ideas if i["status"] != "open" and i.get("pnl") is not None
            and (since is None or (i.get("closed_date") or "") >= since.isoformat())]
    if not rows:
        return {"n": 0}
    df = pd.DataFrame(rows)
    by = df.groupby("setup")["pnl"].agg(["count", "sum", "mean"]).round(0).reset_index().to_dict("records")
    return {"n": len(df), "wins": int((df["pnl"] > 0).sum()), "win_rate": round(float((df["pnl"] > 0).mean() * 100), 0),
            "total_pnl": round(float(df["pnl"].sum()), 0), "avg_pnl": round(float(df["pnl"].mean()), 0),
            "by_status": df["status"].value_counts().to_dict(), "by_setup": by,
            "taken_pnl": round(float(df.loc[df["taken"] == 1, "pnl"].sum()), 0) if "taken" in df else 0}
