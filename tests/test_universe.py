"""Tech universe (spec v2.2 §4) and the Phase 0 fixes (§3): theme tags, the outside bar, theme caps, modes,
the correlated-risk warning, the iron-fly stop, capped loss text, the credit round-trip gate, enforce_budget."""
import copy
import datetime as dt
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from gexscan.config import load_config, load_signal_sheet
from gexscan.engine.recommend import recommend
from gexscan.engine.selector import Filters
from gexscan.engine.universe import Theme, Universe, correlated_warning, load_universe
from gexscan.strategies.base import Candidate, credit_exits

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
TODAY = dt.date(2026, 9, 25)
NAMES = ["UPCO", "DNCO", "PINCO", "EARN", "RICH", "TERM"]
WIDE = dict(top_n=30, max_per_ticker=10)
R = importlib.import_module("gexscan.engine.recommend")     # the package re-exports the function under this name


@pytest.fixture(scope="module")
def cfg():
    c = copy.deepcopy(load_config(ROOT / "config.yaml"))
    c["watchlist"] = []
    return c


def uni(mode="prefer", alpha=("TERM", "RICH"), beta=("EARN", "UPCO"), outside=(), **kw):
    themes = {"alpha": Theme("alpha", "Alpha", list(alpha)), "beta": Theme("beta", "Beta", list(beta))}
    return Universe(mode=mode, themes=themes, outside=list(outside), **kw)


def run(monkeypatch, cfg, tmp_path, u, filters=None, sheet=None):
    """recommend() on the fixtures with a test universe in place of config.yaml's (no fixtures theme)."""
    monkeypatch.setattr(R, "load_universe", lambda *a, **k: u)
    if sheet is not None:
        monkeypatch.setattr(R, "load_signal_sheet", lambda *a, **k: sheet)
    return recommend(None, filters or Filters(**WIDE), cfg, FIX, TODAY, tmp_path, use_news=False, save_snapshots=False)


def whys(r):
    return [d["why"] for d in r.rejected]


# ---------- universe ----------

def test_ideas_carry_theme_tags(monkeypatch, cfg, tmp_path):
    r = run(monkeypatch, cfg, tmp_path, uni(max_per_theme=10))
    assert r.recommendations and r.filters["default_run"] is True
    for c in r.recommendations:
        assert c.metrics["themes"] == (["alpha"] if c.symbol in ("TERM", "RICH") else ["beta"])
    assert set(r.names) == {"TERM", "RICH", "EARN", "UPCO"}          # the default run scans the themes only


def test_theme_cap_on_the_default_run(monkeypatch, cfg, tmp_path):
    r = run(monkeypatch, cfg, tmp_path, uni(max_per_theme=1))
    per = {}
    for c in r.recommendations:
        for k in c.metrics["themes"]:
            per[k] = per.get(k, 0) + 1
    assert per and all(v == 1 for v in per.values())
    assert any(w == "theme cap (alpha 1/1)" for w in whys(r))


def test_explicit_pick_is_not_held_back(monkeypatch, cfg, tmp_path):
    u = uni(max_per_theme=1, outside=("PINCO",), outside_min_confidence=101)
    r = run(monkeypatch, cfg, tmp_path, u, Filters(symbols=NAMES, **WIDE))
    assert r.filters["default_run"] is False
    assert not any("theme cap" in w or "outside the universe" in w for w in whys(r))
    assert sum("alpha" in c.metrics["themes"] for c in r.recommendations) > 1
    assert not any("OUTSIDE UNIVERSE" in c.flags for c in r.recommendations)


def test_outside_bar_section_and_daily_max(monkeypatch, cfg, tmp_path):
    u = uni(alpha=("TERM", "RICH"), beta=(), outside=("EARN", "UPCO"), outside_min_confidence=75,
            outside_max_per_day=1, max_per_theme=10)
    r = run(monkeypatch, cfg, tmp_path, u)
    recs = r.recommendations
    out = [c for c in recs if c.metrics.get("outside")]
    assert len(out) == 1 and "OUTSIDE UNIVERSE" in out[0].flags and out[0].confidence.score >= 75
    first_out = next(i for i, c in enumerate(recs) if c.metrics.get("outside"))
    assert all(c.metrics.get("outside") for c in recs[first_out:])            # their own section, after the universe
    assert any(w == "outside the universe: max 1 a day" for w in whys(r))
    assert any(w.startswith("outside the universe: confidence") for w in whys(r))   # the 69-70 EARN ideas


def test_outside_bar_holds_back_lower_confidence(monkeypatch, cfg, tmp_path):
    u = uni(alpha=("TERM",), beta=(), outside=("EARN", "UPCO"), outside_min_confidence=101, max_per_theme=10)
    r = run(monkeypatch, cfg, tmp_path, u)
    assert r.recommendations and not any(c.metrics.get("outside") for c in r.recommendations)
    assert any("< the 101 bar" in w for w in whys(r))


def test_mode_only_never_scans_outside(monkeypatch, cfg, tmp_path):
    r = run(monkeypatch, cfg, tmp_path, uni(mode="only", beta=(), outside=("EARN", "UPCO"), max_per_theme=10))
    assert set(r.names) == {"TERM", "RICH"} and r.filters["universe_mode"] == "only"


def test_scan_lists_by_mode():
    u = uni(outside=("PINCO",))
    assert u.scan_list(["DNCO"]) == ["TERM", "RICH", "EARN", "UPCO", "PINCO", "DNCO"]
    u.mode = "only"
    assert u.scan_list(["DNCO"]) == ["TERM", "RICH", "EARN", "UPCO"]
    u.mode = "off"
    assert u.scan_list(["DNCO"]) == ["DNCO"]                                   # v2.1 behaviour
    assert u.symbols(["outside"]) == ["PINCO"] and u.in_universe("rich") and not u.in_universe("PINCO")


def test_yaml_booleans_are_refused():
    with pytest.raises(ValueError, match="quote it"):
        load_universe({"universe": {"themes": {"x": {"tickers": ["NVDA", True]}}}})      # an unquoted ON


def test_fixtures_theme_only_in_fixtures_mode(cfg):
    assert "fixtures" in load_universe(cfg, fixtures=True).themes
    assert "fixtures" not in load_universe(cfg, fixtures=False).themes


def _idea(ml, themes, n=1):
    return SimpleNamespace(max_loss=ml, contracts=n, metrics={"themes": themes})


def test_correlated_warning():
    u = uni(correlated_warn_pct=0.60)
    w = correlated_warning([_idea(500, ["alpha"]), _idea(300, ["alpha"]), _idea(200, ["beta"])], u)
    assert w and "80%" in w and "Alpha" in w
    assert correlated_warning([_idea(500, ["alpha"]), _idea(500, ["beta"])], u) is None
    assert correlated_warning([_idea(500, ["alpha"])], u) is None                # one idea is not a cluster
    assert correlated_warning([_idea(100, ["alpha"], 9), _idea(500, ["beta"])], u)  # sized by contracts


# ---------- Phase 0 ----------

def test_f1_iron_fly_stop_at_the_breakevens(cfg, tmp_path):
    r = recommend(["EARN"], Filters(**WIDE), cfg, FIX, TODAY, tmp_path, use_news=False, save_snapshots=False)
    flies = [c for c in r.recommendations if c.kind == "credit"
             and len({l.strike for l in c.legs if l.action == "SELL"}) == 1 and len(c.legs) == 4]
    assert flies
    for c in flies:
        k = float(next(l.strike for l in c.legs if l.action == "SELL"))
        assert c.stop_below == pytest.approx(k - c.net, abs=0.01) and c.stop_above == pytest.approx(k + c.net, abs=0.01)


def _cand(net, width, max_loss):
    return Candidate("XYZ", "bull_put", "Bull put", "bullish", "short", "credit", "defined", "2026-11-20", 56,
                     net=net, width=width, max_loss=max_loss, stop_below=95.0)


def test_f2_loss_text_is_capped_at_max_loss():
    c = _cand(net=4.0, width=5.0, max_loss=100.0)          # 1x credit = $400 > the $100 max loss
    credit_exits(c, {"stop_credit_multiple": 2.0, "take_profit_pct": 0.5}, TODAY)
    assert "capped at max loss = $100" in c.stop and c.metrics["stop_loss_usd"] == 100.0
    assert c.stop.endswith("No rolling.")
    c = _cand(net=1.0, width=5.0, max_loss=400.0)
    credit_exits(c, {"stop_credit_multiple": 2.0, "take_profit_pct": 0.5}, TODAY)
    assert "loss = 1x credit = $100" in c.stop and c.stop_value == 2.0 and c.tp_value == 0.5


def test_f3_credit_round_trip_gate(monkeypatch, cfg, tmp_path):
    base = load_signal_sheet(cfg)
    tight = copy.deepcopy(base)
    tight["signals"]["liquidity"]["credit_roundtrip_max"] = 0.30          # RICH bull put is ~55 %
    r = run(monkeypatch, cfg, tmp_path, uni(max_per_theme=10), Filters(symbols=["RICH"], **WIDE), tight)
    assert not any(c.strategy == "bull_put" for c in r.recommendations)
    assert any(e.startswith("bull_put") and "round-trip" in e for e in r.explain["RICH"])
    warn = copy.deepcopy(base)
    warn["signals"]["liquidity"]["credit_roundtrip_warn"] = 0.30
    r = run(monkeypatch, cfg, tmp_path, uni(max_per_theme=10), Filters(symbols=["RICH"], **WIDE), warn)
    bp = next(c for c in r.recommendations if c.strategy == "bull_put")
    assert bp.metrics["roundtrip_penalty"] > 0


def test_f4_enforce_budget(cfg, tmp_path):
    f = Filters(symbols=NAMES, **WIDE)
    loose = recommend(None, f, cfg, FIX, TODAY, tmp_path, use_news=False, save_snapshots=False)
    over = [c for c in loose.recommendations if c.metrics.get("over_budget")]
    assert over                                                              # warned, still shown
    strict = copy.deepcopy(cfg)
    strict["risk"]["enforce_budget"] = True
    r = recommend(None, f, strict, FIX, TODAY, tmp_path, use_news=False, save_snapshots=False)
    assert not any(c.metrics.get("over_budget") for c in r.recommendations)
    assert sum("per-trade budget" in w for w in whys(r)) == len(over)
