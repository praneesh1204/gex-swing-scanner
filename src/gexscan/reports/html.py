"""HTML pages for the strategy-engine commands: recommend, explain, alerts and backtest (scan has report.html).

Each page is one self-contained file: charts are inline PNGs, and there are no scripts or external assets. Pages
are rendered with Jinja2 autoescaping. The text comes from the same helpers as the console and markdown views
(reports/brief.py), so the three never disagree. The pages only describe. There are no forms, buttons or broker
links: you decide and place every trade yourself.
"""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .. import charts
from ..logutil import redact
from .brief import LEVELS, NO_EXEC, _money, _num, exit_lines, filters_line, gate_lines, idea_facts, idea_header, market_lines

NOT_ADVICE = "Not financial advice. Estimates from delayed data: check live quotes before acting."
_ENV: Environment | None = None


def env() -> Environment:
    global _ENV
    if _ENV is None:
        _ENV = Environment(loader=PackageLoader("gexscan", "templates"), autoescape=select_autoescape(["html"]))
    return _ENV


def write_html(html: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def _f(x, fmt: str = "{:.2f}") -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    return fmt.format(x)


def _money_signed(x) -> str:
    return "–" if x is None else f"{'-' if x < 0 else '+'}${abs(x):,.0f}"


def _pnl_class(x) -> str:
    return "" if not x else ("good" if x > 0 else "bad")


# ---------------------------------------------------------------------------------------------------
# one idea card (recommend and explain)

def idea_view(i: int, c, today: dt.date | None = None, chart: bool = True) -> dict:
    conf = c.confidence
    gates = [{"outcome": d.outcome, "text": t} for d, t in zip(c.gates.decisions, gate_lines(c))] if c.gates else []
    return {
        "i": i, "symbol": c.symbol, "header": idea_header(i, c), "bucket": conf.bucket, "score": conf.score,
        "flags": [{"text": x, "bad": "UNDEFINED" in x.upper()} for x in c.flags],
        "legs": [{"action": l.action, "qty": l.qty, "contract": f"{l.expiry} {l.strike:g}{l.type}", "dte": l.dte,
                  "bid": _f(l.bid), "ask": _f(l.ask), "mid": _f(l.mid),
                  "iv": _f(None if l.iv is None else l.iv * 100, "{:.0f}%"), "delta": _f(l.delta, "{:+.2f}"),
                  "oi": _f(l.oi, "{:,.0f}"), "volume": _f(l.volume, "{:,.0f}")} for l in c.legs],
        "facts": idea_facts(c)[1:],            # [0] is the legs text, shown as the table instead
        "stop_rule": c.stop_rule, "exits": exit_lines(c),
        "subs": [{"cat": k, "points": s["points"], "max": s["max"], "notes": s.get("notes") or "",
                  "pct": 0 if not s["max"] else max(0.0, min(100.0, 100 * s["points"] / s["max"]))}
                 for k, s in conf.sub.items()],
        "penalties": conf.penalties,
        "levels": [(title, c.rationale.get(key) or ["(no data)"]) for key, title in LEVELS],
        "gates": gates, "warnings": c.warnings, "notes": c.notes,
        "sources": " · ".join(f"{k}: {v}" for k, v in c.sources.items()) if c.sources else "",
        "payoff": charts.payoff_chart(c, today) if chart else None,
    }


def _today(r) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(r.today))
    except ValueError:
        return None


def _names(r) -> list[dict]:
    return [{"symbol": s, "spot": n.get("spot"), "as_of": n.get("as_of"), "fresh": n.get("fresh") or {},
             "errors": n.get("errors") or []} for s, n in r.names.items()]


# ---------------------------------------------------------------------------------------------------
# pages

def render_recommend(r, chart: bool = True, stop_mult: float = 2.0) -> str:
    d = _today(r)
    rows = [{"i": i, "symbol": c.symbol, "label": c.label, "expiry": f"{c.expiry} ({c.dte}d)",
             "net": f"{abs(c.net):.2f} {c.kind}", "undefined": c.max_loss is None,
             "max_loss": _money(c.max_loss) if c.max_loss is not None else "UNDEFINED",
             "pop_txt": "–" if c.pop is None else f"{c.pop:.0%}", "stop_rule": c.stop_rule,
             "score": c.confidence.score, "bucket": c.confidence.bucket} for i, c in enumerate(r.recommendations, 1)]
    return env().get_template("recommend.html").render(
        today=r.today, generated=r.generated, no_exec=NO_EXEC, not_advice=NOT_ADVICE, market=market_lines(r),
        filters=filters_line(r), floor=r.filters.get("floor", 40), rows=rows,
        ideas=[idea_view(i, c, d, chart) for i, c in enumerate(r.recommendations, 1)],
        rejected=[{**x, "conf": _num(x.get("confidence"), "{:.1f}")} for x in r.rejected],
        explain=r.explain, names=_names(r),
        providers=", ".join(f"{c['provider']} ({c['status']})" for c in r.capabilities if c.get("wired")),
        errors=r.errors, disclaimer=r.disclaimer, stop_mult=stop_mult)


def render_explain(r, sym: str, chart: bool = True) -> str:
    sym = sym.upper()
    n = r.names.get(sym)
    states = []
    if n:
        own = n.get("states") or {}
        for k, s in {**(r.market_states or {}), **own}.items():
            v = s.get("value")
            states.append({"signal": k, "scope": "name" if k in own else "market", "proxy": bool(s.get("approx")),
                           "state": str(s.get("state", "")), "detail": str(s.get("detail") or ""),
                           "value": "" if v is None else (f"{v:.3g}" if isinstance(v, (int, float)) else str(v))})
    strategies = []
    if r.book is not None:
        for name, gr in r.book.gates.get(sym, {}).items():
            strategies.append({"name": name, "passed": gr.passed,
                               "decisions": [{"outcome": g.outcome, "text": g.line()} for g in gr.decisions]})
    shown = [c for c in r.recommendations if c.symbol == sym]
    return env().get_template("explain.html").render(
        sym=sym, today=r.today, generated=r.generated, found=n is not None, spot=(n or {}).get("spot"),
        as_of=(n or {}).get("as_of"), fresh=(n or {}).get("fresh") or {}, errors=(n or {}).get("errors") or [],
        states=states, ideas=[idea_view(i, c, _today(r), chart) for i, c in enumerate(shown, 1)],
        log=r.explain.get(sym, []), strategies=strategies, market=market_lines(r),
        no_exec=NO_EXEC, not_advice=NOT_ADVICE, disclaimer=r.disclaimer)


def render_alerts(run) -> str:
    """The whole page goes through logutil.redact, like every alert line and log line."""
    from ..alerts.sinks import FOOTER

    pos = []
    for p in run.positions:
        pos.append({"symbol": p.get("symbol", ""), "strategy": p.get("strategy") or "", "expiry": p.get("expiry") or "",
                    "legs": p.get("legs", ""), "credit": _f(p.get("credit")), "cost": _f(p.get("cost_to_close")),
                    "ratio": _f(p.get("ratio"), "{:.2f}×"), "stop_at": _f(p.get("stop_at")),
                    "pnl": _money_signed(p.get("pnl")),
                    "pnl_class": _pnl_class(p.get("pnl")), "state": p.get("state", ""), "mark": p.get("mark") or "",
                    "source": f"{p.get('source', '')} {p.get('ref', '')}".strip()})
    m = run.market or {}
    market = []
    if m:
        st = m.get("states") or {}
        market.append(f"VIX {_num(m.get('vix'))} / VIX3M {_num(m.get('vix3m'))} = {_num(m.get('ts_ratio'), '{:.3f}')}"
                      + (f" (prev {m['ts_ratio_prev']:.3f})" if m.get("ts_ratio_prev") is not None else "")
                      + f" · VVIX {_num(m.get('vvix'), '{:.1f}')}"
                      + (f" ({m['vvix_5d_pct']:+.1f}% 5d)" if m.get("vvix_5d_pct") is not None else "")
                      + (f" · as of {m['as_of']}" if m.get("as_of") else ""))
        if st:
            market.append("States: " + " · ".join(f"{k} {v}" for k, v in st.items()))
    html = env().get_template("alerts.html").render(
        today=run.today, alerts=run.alerts, suppressed=run.suppressed, positions=pos, market=market,
        delivered=run.delivered, errors=run.errors, footer=FOOTER, not_advice=NOT_ADVICE,
        counts={lv: sum(a.level == lv for a in run.alerts) for lv in ("STOP", "WARN", "REGIME", "INFO")})
    return redact(html)


def render_backtest(r, today=None, chart: bool = True) -> str:
    stats = []
    for k, s in r.stats.items():
        stats.append({"strategy": k, "n": s.n, "wins": s.wins, "win_rate": "–" if s.win_rate is None else f"{s.win_rate:.0f}%",
                      "avg": _money_signed(s.avg_pnl), "total": _money_signed(s.total_pnl), "total_class": _pnl_class(s.total_pnl),
                      "dd": _money_signed(-s.max_drawdown if s.max_drawdown else 0),
                      "days": "–" if s.avg_days is None else f"{s.avg_days:g}",
                      "exits": ", ".join(f"{a} {b}" for a, b in sorted(s.exits.items())) + (f", open {s.open}" if s.open else ""),
                      "skipped": sorted(s.skipped.items(), key=lambda x: -x[1]),
                      "by_symbol": sorted(s.by_symbol.items())})
    trades = [{"symbol": t.symbol, "strategy": t.strategy, "entry": t.entry, "exit": t.exit, "expiry": t.expiry,
               "legs": t.legs, "spot": _f(t.spot), "iv": _f(None if t.iv is None else t.iv * 100, "{:.0f}%"),
               "net": f"{t.net:+.2f}", "exit_value": f"{t.exit_value:+.2f}", "pnl": _money_signed(t.pnl),
               "pnl_class": _pnl_class(t.pnl), "reason": t.reason, "days": t.days} for t in r.trades]
    return env().get_template("backtest.html").render(
        r=r, today=today, label=r.label, stats=stats, trades=trades,
        equity=charts.equity_chart(r.trades) if chart else None,
        assumptions=r.assumptions[1:] if r.assumptions and r.assumptions[0] == r.label else r.assumptions,
        errors=r.errors)
