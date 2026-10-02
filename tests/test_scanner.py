import datetime as dt
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from gexscan.analytics.gex import compute_levels, _max_pain
from gexscan.config import load_config
from gexscan.data.cboe import load_chain_file, parse_occ
from gexscan.pipeline import DataSource, run, scan_symbol

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
TODAY = dt.date(2026, 9, 25)


@pytest.fixture(scope="session", autouse=True)
def fixtures():
    if not (FIX / "UPCO.json").exists():
        import subprocess, sys
        subprocess.check_call([sys.executable, str(ROOT / "tests" / "make_fixtures.py"), str(FIX)])


@pytest.fixture
def cfg():
    c = load_config(ROOT / "config.yaml")
    c["watchlist"] = ["UPCO", "DNCO", "PINCO", "EARN"]
    return c


def test_parse_occ():
    root, exp, cp, k = parse_occ("CRDO260925C00172500")
    assert (root, exp, cp, k) == ("CRDO", dt.date(2026, 9, 25), "C", 172.5)
    assert parse_occ("garbage") is None


def test_levels_find_walls_and_positive_gamma():
    ch = load_chain_file(FIX / "UPCO.json", "UPCO", today=TODAY)
    lv = compute_levels(ch)
    assert lv.put_wall == 220 and lv.call_wall == 250
    assert lv.regime == "positive_gamma" and lv.net_gex > 0
    assert lv.gamma_flip is not None and lv.gamma_flip < lv.spot
    assert lv.exp_move_near and lv.exp_move_near > 0


def test_max_pain_simple():
    df = pd.DataFrame({"strike": [90, 100, 110, 90, 100, 110], "type": ["C", "C", "C", "P", "P", "P"],
                       "oi": [0, 100, 0, 0, 100, 0]})
    assert _max_pain(df) == 100


def test_earnings_inside_window_vetoes_trade(cfg):
    src = DataSource(cfg, fixtures=FIX, today=TODAY)
    r = scan_symbol("EARN", src, cfg)
    assert r.trade is None and "earnings" in r.status


def test_bull_put_respects_put_wall_and_rules(cfg):
    src = DataSource(cfg, fixtures=FIX, today=TODAY)
    r = scan_symbol("UPCO", src, cfg)
    assert r.trade is not None and r.trade.setup == "bull_put"
    short = next(l for l in r.trade.legs if l.action == "SELL")
    long = next(l for l in r.trade.legs if l.action == "BUY")
    assert short.strike <= r.levels.put_wall and long.strike < short.strike
    width = short.strike - long.strike
    assert r.trade.net_mid >= cfg["trades"]["min_credit_to_width"] * width
    assert cfg["trades"]["short_delta_min"] <= abs(short.delta) <= cfg["trades"]["short_delta_max"]
    assert r.trade.max_loss == round((width - r.trade.net_mid) * 100)


def test_end_to_end_and_next_day_review(cfg, tmp_path):
    out, state = tmp_path / "reports", tmp_path / "state"
    ctx = run(cfg, fixtures=FIX, today=TODAY, out_dir=out, state_dir=state)
    assert (out / "index.html").exists() and (out / "summary.md").exists()
    assert "reaches 2× the credit (loss = 1× the credit)" in (out / "index.html").read_text()
    assert len(ctx["top"]) >= 1
    assert (state / "gexscan.db").exists()

    # Day 2: UPCO gaps below its put wall -> prior pick must be flagged "key level broken"
    fix2 = tmp_path / "fix2"
    shutil.copytree(FIX, fix2)
    p = json.loads((fix2 / "UPCO.json").read_text())
    p["data"]["current_price"] = 212.0
    (fix2 / "UPCO.json").write_text(json.dumps(p))
    ctx2 = run(cfg, fixtures=fix2, today=TODAY + dt.timedelta(days=3), out_dir=out, state_dir=state)
    rev = {r["symbol"]: r for r in ctx2["reviews"]}
    assert rev["UPCO"]["label"] == "key level broken"
    assert any("put wall" in t for t in rev["UPCO"]["triggers"])
    assert ctx2["review_date"] == TODAY.isoformat()
