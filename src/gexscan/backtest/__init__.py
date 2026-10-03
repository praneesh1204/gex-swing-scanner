"""`gexscan backtest`: synthetic replay of the strategy rules + exit plan (see harness.py for every assumption).

Results are labelled SYNTHETIC everywhere they're shown. Win rate, average P/L and max drawdown per strategy.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

from ..config import load_signal_sheet
from .harness import LABEL, SUPPORTED, BacktestResult, StratStats, Trade, backtest

log = logging.getLogger(__name__)


def load_inputs(cfg: dict, symbols: list[str], fixtures: Path | None, today: dt.date, state_dir: Path | None,
                errors: list[str]) -> tuple[dict, dict, dict]:
    """-> (bars, VIX-family series, past earnings dates) via the same EngineSource the live engine uses."""
    from ..engine.recommend import EngineSource

    src = EngineSource(cfg, fixtures, today, state_dir, use_news=False)
    bars, earnings = {}, {}
    for s in symbols:
        try:
            df = src.daily(s)
        except Exception as e:
            errors.append(f"{s}: daily bars unavailable ({type(e).__name__}: {e})")
            continue
        if df is None or df.empty:
            errors.append(f"{s}: no daily bars")
            continue
        df = df[df.index <= pd.Timestamp(today)]
        bars[s] = df
        try:
            info = src.events.get(s, df)
            ds = [dt.date.fromisoformat(str(h["date"])[:10]) for h in (info.history or [])]
            if info.next_earnings:
                ds.append(dt.date.fromisoformat(str(info.next_earnings)[:10]))
            earnings[s] = sorted(set(ds))
        except Exception as e:
            errors.append(f"{s}: earnings history unavailable ({e}); the no-earnings-in-trade rule can't be replayed")
    series = {}
    try:
        md = src.market.load()
        series = {k: v for k, v in md.series.items() if k in ("VIX", "VIX3M", "VVIX")}
        errors += md.errors
    except Exception as e:
        errors.append(f"market data: {type(e).__name__}: {e}; market gates not replayed")
    return bars, series, earnings


def run_backtest(cfg: dict, symbols: list[str] | None = None, strategies: list[str] | None = None,
                 years: float | None = None, fixtures: Path | None = None, today: dt.date | None = None,
                 state_dir: Path | None = None) -> BacktestResult:
    today = today or dt.date.today()
    syms = [s.upper() for s in (symbols or cfg["watchlist"])]
    errors: list[str] = []
    bars, series, earnings = load_inputs(cfg, syms, fixtures, today, state_dir, errors)
    res = backtest(bars, series, earnings, cfg, load_signal_sheet(cfg), strategies, years, today)
    res.errors = errors + res.errors
    return res


def markdown(r: BacktestResult) -> str:
    out = [f"# gexscan backtest {r.start} to {r.end}", "", f"> **{r.label}**", "",
           f"Names: {', '.join(r.symbols) or 'none'}", "",
           "| Strategy | Trades | Win rate | Avg P/L | Total P/L | Max DD | Avg days | Exits | Skipped by gate |",
           "|---|---:|---:|---:|---:|---:|---:|---|---|"]
    for k, s in r.stats.items():
        ex = ", ".join(f"{a} {b}" for a, b in sorted(s.exits.items())) or "-"
        sk = ", ".join(f"{a}: {b}" for a, b in sorted(s.skipped.items(), key=lambda x: -x[1])) or "-"
        ex += f", still open {s.open}" if s.open else ""
        out.append(f"| {k} | {s.n} | {_pct(s.win_rate)} | {_usd(s.avg_pnl)} | {_usd(s.total_pnl)} | "
                   f"{_usd(-s.max_drawdown)} | {s.avg_days if s.avg_days is not None else '-'} | {ex} | {sk} |")
    out += ["", "P/L in $ per 1 contract, after slippage. Max DD = deepest fall of cumulative P/L (trades ordered by "
            "exit). Trades still open when the data ends are counted under Exits but not scored.",
            "", "## Assumptions", ""] + [f"- {a}" for a in r.assumptions]
    if r.errors:
        out += ["", "## Data notes", ""] + [f"- {e}" for e in r.errors]
    out += ["", "_Synthetic estimates, not financial advice. gexscan never places orders._", ""]
    return "\n".join(out)


def _pct(x):
    return "-" if x is None else f"{x:.0f}%"


def _usd(x):
    return "-" if x is None else f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


__all__ = ["LABEL", "SUPPORTED", "BacktestResult", "StratStats", "Trade", "backtest", "markdown", "run_backtest"]
