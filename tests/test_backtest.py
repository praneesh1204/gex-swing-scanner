"""Synthetic backtest: smoke run, no lookahead, open trades kept out of the stats, drawdown math, gates."""
import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from gexscan.backtest import load_inputs, markdown, run_backtest
from gexscan.backtest.harness import (OPEN, SUPPORTED, StratStats, Trade, backtest, gate_fail, net_value,
                                      pick_vertical, summarize)
from gexscan.config import load_config, load_signal_sheet

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
TODAY = dt.date(2026, 9, 25)
NAMES = ["UPCO", "DNCO", "PINCO", "EARN", "RICH", "TERM"]


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config.yaml")


def test_backtest_smoke(cfg, tmp_path):
    r = run_backtest(cfg, NAMES, None, None, FIX, TODAY, tmp_path)
    assert r.label.startswith("SYNTHETIC") and set(r.stats) == set(SUPPORTED)
    assert r.symbols == sorted(NAMES)
    for k, s in r.stats.items():
        scored = [t for t in r.trades if t.strategy == k and t.reason != OPEN]
        assert s.n == len(scored) and s.open == sum(t.strategy == k and t.reason == OPEN for t in r.trades)
        if s.n:
            assert 0 <= s.win_rate <= 100 and s.max_drawdown >= 0
            assert s.total_pnl == pytest.approx(sum(t.pnl for t in scored), abs=0.05)
    assert sum(s.n for s in r.stats.values()) > 50
    for t in r.trades:
        assert t.entry < t.exit <= TODAY.isoformat() and t.reason in ("tp", "stop", "stop_strike", "time", "expiry", OPEN)
    md = markdown(r)
    assert "SYNTHETIC" in md and "Max DD" in md and "not financial advice" in md and "roll" not in md.replace("No rolling", "")


def test_no_lookahead(cfg, tmp_path):
    """Trades closed before a cut date must be identical whether or not later data exists."""
    errors = []
    bars, series, earn = load_inputs(cfg, NAMES, FIX, TODAY, tmp_path, errors)
    sheet = load_signal_sheet(cfg)
    cut = pd.Timestamp(TODAY - dt.timedelta(days=200))
    tb = {k: v[v.index <= cut] for k, v in bars.items()}
    ts = {k: v[v.index <= cut] for k, v in series.items()}
    full = backtest(bars, series, earn, cfg, sheet, years=10)
    part = backtest(tb, ts, earn, cfg, sheet, years=10)
    closed = [t.to_dict() for t in part.trades if t.reason != OPEN]
    assert closed
    have = {(t.symbol, t.strategy, t.entry): t.to_dict() for t in full.trades}
    for t in closed:
        assert have[(t["symbol"], t["strategy"], t["entry"])] == t


def _t(pnl, exit_, reason="tp"):
    return Trade("X", "csp", "2026-01-01", exit_, "2026-02-20", "", 100, 0.4, 1.0, 0.5, pnl, reason, 10)


def test_summarize_excludes_open_and_drawdown():
    trades = [_t(100, "2026-01-10"), _t(-300, "2026-01-20"), _t(50, "2026-01-30"), _t(-100, "2026-02-05"),
              _t(9999, "2026-02-10", OPEN)]
    s = summarize(StratStats("csp"), trades)
    assert s.n == 4 and s.open == 1 and s.total_pnl == -250
    assert s.max_drawdown == 350                      # peak +100 -> trough -250
    assert s.win_rate == 50.0 and s.exits == {"tp": 4}
    assert summarize(StratStats("csp"), [_t(-50, "2026-01-10")]).max_drawdown == 50   # peak counts from 0


def test_gate_fail():
    spec = {"gates": {"required": {"iv_rank": {"in": ["rich", "sell"]}, "vix_ts": {"in": ["contango"], "unknown": "fail"},
                                   "gex": {"in": ["positive"]}}}}
    assert gate_fail(spec, {"iv_rank": "low", "vix_ts": "contango"}) == "iv_rank (proxy low)"
    assert gate_fail(spec, {"iv_rank": "rich", "vix_ts": "contango"}) is None          # gex: not replayed
    assert gate_fail(spec, {"iv_rank": "unknown", "vix_ts": "contango"}) is None       # unknown passes...
    assert gate_fail(spec, {"iv_rank": "rich"}) == "vix_ts (unknown)"                  # ...unless unknown: fail


def test_synthetic_vertical_floor():
    r = pick_vertical(100, 35 / 365, 0.3, "P", (0.12, 0.30), 0.20, 5, 0.20, 0.33, 1.0)
    assert r is not None
    k, w = r
    assert k < 100 and 0 < w <= 5 * 1.25
    assert pick_vertical(100, 35 / 365, 0.3, "P", (0.12, 0.30), 0.20, 5, 0.60, 0.60, 1.0) is None


def test_net_value_at_expiry():
    exp = dt.date(2026, 10, 30)
    legs = [("SELL", "P", 95.0, exp), ("BUY", "P", 90.0, exp)]
    assert net_value(legs, 80, exp, 0.3) == pytest.approx(5.0)      # cost to close = full width
    assert net_value(legs, 100, exp, 0.3) == pytest.approx(0.0)
