"""Alerts: 2x-credit stop thresholds, de-duplication, regime flips, IV-rank crossings, earnings window, sinks.
Alert only: nothing here (or in the code under test) closes or changes a position."""
import datetime as dt
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from gexscan.alerts import run_alerts
from gexscan.alerts.rules import (Alert, earnings_alert, ivr_alerts, should_fire, stop_alert, stop_state,
                                  vix_ts_alert, vvix_alert)
from gexscan.alerts.sinks import deliver, make_sinks
from gexscan.config import load_config, load_signal_sheet
from gexscan.data.positions import Position, from_yaml
from gexscan.signals.market import MarketRegime
from gexscan.signals.ruleset import classify_market
from gexscan.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
TODAY = dt.date(2026, 9, 25)

POSITIONS = """
positions:
  - {symbol: RICH, strategy: bull_put, expiry: 2026-10-30, credit: 1.00,
     legs: [{action: SELL, type: P, strike: 190}, {action: BUY, type: P, strike: 185}]}
  - {symbol: UPCO, strategy: bull_put, expiry: 2026-10-30, credit: 0.86,
     legs: [{action: SELL, type: P, strike: 217.5}, {action: BUY, type: P, strike: 212.5}]}
"""


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config.yaml")


def _quiet():
    return Console(file=io.StringIO(), width=200)


# ---------- stop thresholds ----------

@pytest.mark.parametrize("cost,state", [(2.00, "stop"), (2.40, "stop"), (1.75, "warn"), (1.90, "warn"),
                                        (1.74, "ok"), (0.40, "ok")])
def test_stop_state(cost, state):
    assert stop_state(cost, 1.00, 2.0, 0.875)[0] == state


def test_stop_alert_text():
    p = Position("RICH", "bull_put", "2026-10-30", [{"action": "SELL", "type": "P", "strike": 190.0},
                                                    {"action": "BUY", "type": "P", "strike": 185.0}], 1.00,
                 source="positions.yaml", ref="#1")
    a, row = stop_alert(p, 2.10, "chain mid", 2.0, 0.875, "2026-09-25")
    assert a.level == "STOP" and row["state"] == "stop" and row["stop_at"] == 2.0
    assert "No rolling" in a.message and "yourself" in a.message
    assert row["pnl"] == pytest.approx(-110)
    a, row = stop_alert(p, 1.20, "chain mid", 2.0, 0.875, "2026-09-25")
    assert a is None and row["state"] == "ok"


def test_should_fire_dedupe():
    a = Alert("stop:x:#1", "STOP", "stop", "X", "t", "m", "stop")
    assert should_fire(a, None, TODAY)
    assert not should_fire(a, {"state": "stop", "updated": TODAY.isoformat()}, TODAY)
    assert should_fire(a, {"state": "stop", "updated": "2026-09-24"}, TODAY)            # STOP repeats daily
    w = Alert("stop:x:#1", "WARN", "stop", "X", "t", "m", "warn")
    assert not should_fire(w, {"state": "warn", "updated": "2026-09-24"}, TODAY)        # WARN only on change
    assert should_fire(w, {"state": "ok", "updated": TODAY.isoformat()}, TODAY)


# ---------- regime ----------

def _regime(ts, prev, vvix=90.0, d5=1.0):
    return MarketRegime("2026-09-25", vix=ts * 20, vix3m=20.0, vvix=vvix, ts_ratio=ts, ts_ratio_prev=prev,
                        vvix_5d_pct=d5)


def test_vix_ts_flip(cfg):
    sheet = load_signal_sheet(cfg)
    m = _regime(1.04, 0.97)
    cur, a = vix_ts_alert(m, classify_market(m, sheet, cfg, TODAY), None, sheet)
    assert cur == "backwardation" and a.level == "REGIME" and "backwardation" in a.title
    cur, a = vix_ts_alert(m, classify_market(m, sheet, cfg, TODAY), "backwardation", sheet)
    assert a is None                                                                    # no flip, no alert
    m = _regime(0.92, 1.02)
    cur, a = vix_ts_alert(m, classify_market(m, sheet, cfg, TODAY), "backwardation", sheet)
    assert cur == "contango" and a.level == "INFO"


def test_vvix_spike(cfg):
    sheet = load_signal_sheet(cfg)
    m = _regime(0.9, 0.9, vvix=104, d5=18)
    cur, a = vvix_alert(m, classify_market(m, sheet, cfg, TODAY), "normal", 15)
    assert cur == "spike" and a is not None
    assert vvix_alert(m, classify_market(m, sheet, cfg, TODAY), "spike", 15)[1] is None


def test_ivr_crossing():
    out = {k: a for k, _, a in ivr_alerts("X", 45, 55, [30, 50], {}, "", "snapshots")}
    assert out["ivr:X:50"] is not None and "above 50" in out["ivr:X:50"].title
    assert out["ivr:X:30"] is None
    out = {k: a for k, _, a in ivr_alerts("X", 55, 28, [30, 50], {}, "", "snapshots")}
    assert all(a is not None and "below" in a.title for a in out.values())
    # a stored state wins over the snapshot: already alerted 'above' -> no repeat
    assert ivr_alerts("X", 45, 55, [50], {50.0: "above"}, "", "")[0][2] is None


def test_earnings_window():
    info = SimpleNamespace(next_earnings=dt.date(2026, 9, 30), days_to_earnings=5, earnings_source="fixture")
    a = earnings_alert("EARN", info, 10, TODAY, held=False)
    assert a.level == "INFO" and "5d" in a.title
    assert earnings_alert("EARN", info, 10, TODAY, held=True).level == "WARN"
    info.days_to_earnings = 20
    assert earnings_alert("EARN", info, 10, TODAY, held=False) is None


# ---------- sinks ----------

def test_optional_sinks_skip_without_env(monkeypatch, tmp_path):
    for k in ("ALERT_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_HOST", "ALERT_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    sinks = make_sinks(["webhook", "telegram", "email", "log"], tmp_path / "alerts.log")
    st = deliver([Alert("k", "INFO", "x", None, "t", "m", "s")], sinks)
    assert all(st[n].startswith("skipped") for n in ("webhook", "telegram", "email"))
    assert st["log"] == "sent" and (tmp_path / "alerts.log").read_text().strip()


# ---------- end to end on the fixtures ----------

def test_positions_yaml_per_leg_expiry(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("positions:\n  - {symbol: term, strategy: calendar, expiry: 2026-10-23, credit: 0.5, legs: "
                 "[{action: SELL, type: C, strike: 65}, {action: BUY, type: C, strike: 65, expiry: 2026-11-20}]}\n")
    (p,) = from_yaml(f)
    assert p.symbol == "TERM" and p.legs[1]["expiry"] == "2026-11-20" and "expiry" not in p.legs[0]


def test_run_alerts_stop_warn_and_dedupe(cfg, tmp_path):
    pos = tmp_path / "positions.yaml"
    pos.write_text(POSITIONS)
    state = tmp_path / "state"
    kw = dict(fixtures=FIX, today=TODAY, state_dir=state, positions_file=pos, use_ibkr=False, names=False,
              sinks=["log"], con=_quiet())
    r = run_alerts(cfg, **kw)
    lv = {(a.symbol, a.level) for a in r.alerts}
    assert ("RICH", "STOP") in lv and ("UPCO", "WARN") in lv
    rows = {p["symbol"]: p for p in r.positions}
    assert rows["RICH"]["ratio"] >= 2.0 and rows["UPCO"]["state"] == "warn"
    assert r.delivered == {"log": "sent"}
    r2 = run_alerts(cfg, **kw)
    assert not r2.alerts and len(r2.suppressed) >= 2                                   # same day: nothing re-sent


def test_run_alerts_ivr_crossing_from_seeded_snapshot(cfg, tmp_path):
    state = tmp_path / "state"
    s = Store(state)
    nv = lambda r: SimpleNamespace(atm_iv=0.5, iv_rank=r, iv_percentile=r, iv_rank_source="snapshots",  # noqa: E731
                                   ts_ratio=None, rr25=None, ff=None, front_pump=None, vrp_c2c=None)
    s.save_vol(TODAY - dt.timedelta(days=1), "RICH", nv(40.0))       # fixture IVR today ~77: crosses 50 up
    s.save_vol(TODAY - dt.timedelta(days=1), "UPCO", nv(55.0))       # fixture IVR today ~45: crosses 50 down
    s.close()
    r = run_alerts(cfg, FIX, TODAY, state, tmp_path / "none.yaml", False, ["RICH", "UPCO"], True, ["log"],
                   False, _quiet())
    ivr = {a.key: a for a in r.alerts if a.kind == "ivr"}
    assert "ivr:RICH:50" in ivr and "above" in ivr["ivr:RICH:50"].title
    assert "ivr:UPCO:50" in ivr and "below" in ivr["ivr:UPCO:50"].title
    assert "ivr:RICH:30" not in ivr                                   # was above 30 already
