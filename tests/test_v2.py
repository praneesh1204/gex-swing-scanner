"""Exit-plan math, journal review, risk gate, flow, volume profile and the no-order-code guard."""
import datetime as dt
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from gexscan.analytics.flow import compute_flow
from gexscan.analytics.gex import Levels
from gexscan.analytics.volume_profile import compute_volume_profile
from gexscan.config import load_config
from gexscan.pipeline import DataSource, scan_symbol
from gexscan.review import review_idea
from gexscan.risk import gate
from gexscan.trades import time_exit_date

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
TODAY = dt.date(2026, 9, 25)


@pytest.fixture
def cfg():
    return load_config(ROOT / "config.yaml")


# ---------- exit plans ----------

def test_credit_trade_exit_plan_is_2x_credit_stop(cfg):
    r = scan_symbol("UPCO", DataSource(cfg, fixtures=FIX, today=TODAY), cfg)
    tr = r.trade
    c = tr.net_mid
    assert tr.tp_value == pytest.approx(c * 0.5, abs=0.011)
    assert tr.stop_value == pytest.approx(c * 3, abs=0.011)          # buy back at 3x = loss of 2x credit
    short_put = next(l.strike for l in tr.legs if l.action == "SELL")
    assert tr.stop_below == short_put
    assert "No rolling" in tr.stop and "roll" not in tr.take_profit.lower()
    assert TODAY < dt.date.fromisoformat(tr.time_exit) < dt.date.fromisoformat(tr.expiry)


def test_time_exit_date():
    exp = TODAY + dt.timedelta(days=35)
    assert time_exit_date(TODAY, exp, {"time_exit_dte": 21}) == TODAY + dt.timedelta(days=14)   # 21 DTE comes first
    exp = TODAY + dt.timedelta(days=20)
    assert time_exit_date(TODAY, exp, {"time_exit_dte": 21}) == TODAY + dt.timedelta(days=10)   # halfway
    assert time_exit_date(TODAY, TODAY + dt.timedelta(days=1), {}) == TODAY + dt.timedelta(days=1)


# ---------- journal review ----------

def _lv(spot, **kw):
    base = dict(symbol="X", spot=spot, net_gex=1e8, regime="positive_gamma", gamma_flip=None, flip_status="",
                call_wall=None, put_wall=None, max_pain_near=None, near_expiry=None, atm_iv=0.4,
                exp_move_near=None, exp_move_near_pct=None, total_call_oi=0, total_put_oi=0, pc_oi_ratio=None,
                atm_spread_pct=None)
    base.update(kw)
    return Levels(**base)


def _chain(expiry, short_mid, long_mid):
    opts = pd.DataFrame({"expiry": [expiry, expiry], "type": ["P", "P"], "strike": [95.0, 90.0],
                         "mid": [short_mid, long_mid], "ask": [short_mid + 0.05, long_mid + 0.05]})
    return SimpleNamespace(options=opts)


def _idea(expiry):
    return {"id": 1, "symbol": "X", "setup": "bull_put", "run_date": TODAY.isoformat(), "summary": "PCS 95/90",
            "taken": 1, "expiry": expiry.isoformat(), "entry_net": 1.00, "contracts": 1, "spot": 100.0,
            "max_loss": 400, "width": 5, "legs": [{"action": "SELL", "type": "P", "strike": 95},
                                                   {"action": "BUY", "type": "P", "strike": 90}],
            "tp_value": 0.50, "stop_value": 3.00, "stop_below": 95.0, "stop_above": None,
            "time_exit": (expiry - dt.timedelta(days=21)).isoformat()}


def test_review_hits_profit_target(cfg):
    exp = TODAY + dt.timedelta(days=35)
    r = review_idea(_idea(exp), _chain(exp, 0.60, 0.15), _lv(104), None, cfg, TODAY + dt.timedelta(days=3))
    assert r["status"] == "tp" and r["pnl"] == pytest.approx(55.0) and "No rolling" in r["action"]


def test_review_hits_2x_credit_stop(cfg):
    exp = TODAY + dt.timedelta(days=35)
    r = review_idea(_idea(exp), _chain(exp, 4.00, 0.90), _lv(96), None, cfg, TODAY + dt.timedelta(days=3))
    assert r["status"] == "stop" and r["pnl"] == pytest.approx(-210.0)


def test_review_level_stop_on_close_below_short(cfg):
    exp = TODAY + dt.timedelta(days=35)
    r = review_idea(_idea(exp), _chain(exp, 1.8, 0.6), _lv(94.5), None, cfg, TODAY + dt.timedelta(days=3))
    assert r["status"] == "stop" and "closed below 95" in r["exit_reason"]


def test_review_holds_when_nothing_triggered(cfg):
    exp = TODAY + dt.timedelta(days=35)
    r = review_idea(_idea(exp), _chain(exp, 0.9, 0.3), _lv(100), None, cfg, TODAY + dt.timedelta(days=3))
    assert r["status"] == "open" and r["action"] == "HOLD"


def test_review_time_exit(cfg):
    exp = TODAY + dt.timedelta(days=35)
    r = review_idea(_idea(exp), _chain(exp, 0.9, 0.3), _lv(100), None, cfg, exp - dt.timedelta(days=20))
    assert r["status"] == "time"


# ---------- risk gate ----------

def _cand(sym, max_loss=400, contracts=1):
    tr = SimpleNamespace(max_loss=max_loss, contracts=contracts, setup="bull_put", notes=[])
    return SimpleNamespace(symbol=sym, trade=tr, status="")


def test_cooling_off_makes_everything_watch_only(cfg):
    ok, watch, notes = gate([_cand("A"), _cand("B")], [], stops_recent=1, cfg=cfg)
    assert ok == [] and len(watch) == 2 and "Cooling-off" in notes[0]


def test_daily_and_per_symbol_limits(cfg):
    cands = [_cand("A"), _cand("A"), _cand("B"), _cand("C"), _cand("D")]
    ok, watch, _ = gate(cands, [], 0, cfg)
    assert [c.symbol for c in ok] == ["A", "B", "C"]
    assert len(watch) == 2


def test_total_risk_cap_cuts_size(cfg):
    cfg["trades"]["account_size"] = 10000        # cap = 1,000
    ok, watch, _ = gate([_cand("A", max_loss=400, contracts=5)], [], 0, cfg)
    assert ok and ok[0].trade.contracts == 2


# ---------- flow and volume profile ----------

def test_flow_bias_bullish_on_otm_call_premium():
    opts = pd.DataFrame({"type": ["C", "C", "P", "P"], "strike": [110, 120, 90, 80], "mid": [3.0, 1.0, 1.0, 0.5],
                         "volume": [5000, 3000, 500, 200], "oi": [1000, 2000, 3000, 3000],
                         "expiry": [dt.date(2026, 10, 16)] * 4, "dte": [21] * 4, "bid": [0] * 4, "ask": [0] * 4,
                         "iv": [0.5] * 4, "delta": [0.3] * 4, "gamma": [0.01] * 4})
    f = compute_flow(opts, 100.0)
    assert f.bias > 0 and f.pc_volume_ratio == pytest.approx(700 / 8000)
    assert any("110" in u["contract"] for u in f.unusual)


def test_volume_profile_poc_at_heavy_price():
    idx = pd.date_range("2026-09-01 09:30", periods=200, freq="30min")
    price = np.where(np.arange(200) % 4 == 0, 105.0, 100.0)
    bars = pd.DataFrame({"open": price, "high": price + 0.2, "low": price - 0.2, "close": price,
                         "volume": np.where(price == 100.0, 10000, 1000)}, index=idx)
    vp = compute_volume_profile(bars, None, spot=100.0, nbins=40)
    assert abs(vp.poc - 100.0) < 0.5 and vp.val <= 100.0 <= vp.vah


# ---------- hard rule 1: no order code ----------

def test_no_order_placement_code():
    pat = re.compile(r"place_?order|create_order|submit_?order|cancel_?order|modify_?order|/iserver/account/\S*order"
                     r"|/orders?\b|reqPlaceOrder|placeOrder", re.I)
    hits = [f"{p.relative_to(ROOT)}:{i}" for p in (ROOT / "src").rglob("*.py")
            for i, line in enumerate(p.read_text().splitlines(), 1) if pat.search(line)]
    assert hits == [], f"order-related code found: {hits}"
