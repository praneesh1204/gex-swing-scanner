"""`gexscan alerts`: stop alerts on open short premium + regime alerts, delivered to pluggable sinks.

Read-only and alert-only. Positions come from positions.yaml (you maintain it), ideas you marked as taken in
the journal, and optionally the IBKR Client Portal gateway (GET allow-list). Nothing is ever closed, rolled
or placed by this tool: an alert tells you; you act.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config import load_signal_sheet, stop_multiple
from ..data.positions import IBKRReadOnly, Position, from_journal, from_yaml
from ..logutil import redact
from ..signals.market import market_regime
from ..signals.ruleset import classify_market
from ..store import Store
from .rules import (Alert, earnings_alert, ivr_alerts, mark_position, should_fire, stop_alert, vix_ts_alert,
                    vvix_alert)
from .sinks import ConsoleSink, LogSink, deliver, make_sinks

log = logging.getLogger(__name__)


@dataclass
class AlertRun:
    today: str
    alerts: list[Alert]                                   # fired this run (after de-duplication)
    suppressed: list[Alert] = field(default_factory=list)  # same state as last time: not re-sent
    positions: list[dict] = field(default_factory=list)   # every short-premium position checked
    market: dict = field(default_factory=dict)
    delivered: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"today": self.today, "alerts": [a.to_dict() for a in self.alerts],
                "suppressed": [a.to_dict() for a in self.suppressed], "positions": self.positions,
                "market": self.market, "delivered": self.delivered, "errors": self.errors}


def load_positions(cfg: dict, store: Store, positions_file: str | Path | None, use_ibkr: bool,
                   errors: list[str]) -> list[Position]:
    acfg = cfg["alerts"]
    out = from_yaml(positions_file or acfg.get("positions_file") or "positions.yaml")
    out += from_journal(store)
    if use_ibkr:
        try:
            out += IBKRReadOnly().positions()
        except Exception as e:
            errors.append(redact(f"IBKR gateway (read-only positions): {type(e).__name__}: {e}"))
    return out


def run_alerts(cfg: dict, fixtures: Path | None = None, today: dt.date | None = None, state_dir: Path | None = None,
               positions_file: str | Path | None = None, use_ibkr: bool | None = None,
               symbols: list[str] | None = None, names: bool = True, sinks: list[str] | None = None,
               dry_run: bool = False, con=None) -> AlertRun:
    """Evaluate every rule, de-duplicate against the journal DB, deliver. dry_run: console only, no state saved."""
    from ..engine.recommend import EngineSource, build_context

    today = today or dt.date.today()
    acfg = cfg["alerts"]
    state = Path(state_dir or cfg["output"]["state_dir"])
    sheet = load_signal_sheet(cfg)
    src = EngineSource(cfg, fixtures, today, state, use_news=False)
    store = Store(state)
    errors: list[str] = []
    candidates: list[tuple[Alert, None]] = []
    saves: list[tuple[str, str, float | None]] = []      # (key, state, value) written after delivery
    run = AlertRun(today.isoformat(), [])
    try:
        # ---- market regime ---------------------------------------------------------------------
        md = regime = None
        states = {}
        if cfg["macro"].get("enabled", True):
            try:
                md = src.market.load()
                regime = market_regime(md, today)
                states = classify_market(regime, sheet, cfg, today)
                errors += md.errors
            except Exception as e:
                errors.append(f"market data: {type(e).__name__}: {e}")
        if regime is not None:
            run.market = {"vix": regime.vix, "vix3m": regime.vix3m, "ts_ratio": regime.ts_ratio,
                          "ts_ratio_prev": regime.ts_ratio_prev, "vvix": regime.vvix, "vvix_5d_pct": regime.vvix_5d_pct,
                          "states": {k: v.state for k, v in states.items()}, "as_of": regime.as_of}
            for key, fn in (("regime:vix_ts", lambda prev: vix_ts_alert(regime, states, prev, sheet)),
                            ("regime:vvix", lambda prev: vvix_alert(regime, states, prev, acfg.get("vvix_spike_pct", 15)))):
                prev = (store.alert_get(key) or {}).get("state")
                cur, a = fn(prev)
                if cur is not None:
                    saves.append((key, cur, None))
                if a is not None:
                    candidates.append(a)

        # ---- open short premium: 2x-credit stop ----------------------------------------------------
        positions = load_positions(cfg, store, positions_file,
                                   acfg.get("use_ibkr", False) if use_ibkr is None else use_ibkr, errors)
        mult, warn = stop_multiple(cfg["trades"]), float(acfg.get("stop_warn_ratio", 0.875))
        chains: dict[str, object] = {}
        held = set()
        for p in positions:
            held.add(p.symbol)
            opts = None
            if p.marks.get("cost_to_close") is None:
                if p.symbol not in chains:
                    try:
                        chains[p.symbol] = src.chain(p.symbol)
                    except Exception as e:
                        chains[p.symbol] = None
                        errors.append(f"{p.symbol}: chain unavailable for marking ({e})")
                ch = chains[p.symbol]
                opts = ch.options if ch is not None else None
            cost, how = mark_position(p, opts)
            if cost is None:
                legs = " / ".join(f"{l['action']} {float(l['strike']):g}{l['type']}" for l in p.legs)
                run.positions.append({"symbol": p.symbol, "strategy": p.strategy, "expiry": p.expiry, "legs": legs,
                                      "credit": p.credit, "state": "unmarked", "mark": how, "source": p.source,
                                      "ref": p.ref})
                errors.append(f"{p.symbol} {p.expiry} ({p.source} {p.ref}): could not mark ({how})")
                continue
            ch = chains.get(p.symbol)
            as_of = p.marks.get("as_of") or (getattr(ch, "as_of", "") if ch is not None else "")
            a, row = stop_alert(p, cost, how, mult, warn, str(as_of))
            run.positions.append(row)
            saves.append((f"stop:{p.source}:{p.ref}", row["state"], row["ratio"]))
            if a is not None:
                candidates.append(a)

        # ---- watchlist names: IV-rank crossings, earnings entering the window ----------------------
        if names:
            syms = list(dict.fromkeys([s.upper() for s in (symbols or cfg["watchlist"])] + sorted(held)))
            levels = [float(x) for x in acfg.get("ivr_levels", [30, 50])]
            for s in syms:
                try:
                    ctx = build_context(s, src, cfg, sheet, store, md)
                except Exception as e:
                    errors.append(f"{s}: {type(e).__name__}: {e}")
                    continue
                errors += ctx.errors
                if ctx.nv is not None and ctx.nv.iv_rank is not None:
                    prev = store.last_vol(s, today) or {}
                    stored = {lv: (store.alert_get(f"ivr:{s}:{lv:g}") or {}).get("state") for lv in levels}
                    for key, st, a in ivr_alerts(s, prev.get("iv_rank"), ctx.nv.iv_rank, levels, stored,
                                                 ctx.as_of, ctx.nv.iv_rank_source):
                        saves.append((key, st, ctx.nv.iv_rank))
                        if a is not None:
                            candidates.append(a)
                a = earnings_alert(s, ctx.info, int(acfg.get("earnings_window_days", 10)), today, s in held)
                if a is not None:
                    saves.append((a.key, a.state, a.value))
                    candidates.append(a)

        # ---- de-duplicate, deliver, remember ---------------------------------------------------------
        for a in candidates:
            (run.alerts if should_fire(a, store.alert_get(a.key), today) else run.suppressed).append(a)
        order = {lv: i for i, lv in enumerate(("STOP", "WARN", "REGIME", "INFO"))}
        run.alerts.sort(key=lambda a: (order.get(a.level, 9), a.symbol or ""))
        log_file = acfg.get("log_file") or (state / "alerts.log")
        if dry_run:
            run.delivered = deliver(run.alerts, [ConsoleSink(con)])
        else:
            names_ = sinks or acfg.get("sinks") or ["console", "log"]
            run.delivered = deliver(run.alerts, make_sinks(names_, log_file, con))
            fired = {a.key for a in run.alerts}
            for key, st, val in saves:
                prev = store.alert_get(key)
                if key in fired or prev is None or prev.get("state") != st:
                    store.alert_set(key, st, val, today)
    finally:
        store.close()
    run.errors = errors
    return run


__all__ = ["Alert", "AlertRun", "ConsoleSink", "LogSink", "deliver", "load_positions", "make_sinks", "run_alerts"]
