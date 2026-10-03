"""Strategy engine: signal classification, gating, defined-risk checks, vertical search, the selector on the
fixtures (integration), the read-only IBKR allow-list and secret redaction."""
import datetime as dt
import logging
import re
from pathlib import Path

import pandas as pd
import pytest

from gexscan.analytics import bs
from gexscan.backtest.harness import bs_delta
from gexscan.config import load_config, load_signal_sheet
from gexscan.data.positions import IBKRReadOnly
from gexscan.engine.recommend import recommend
from gexscan.engine.selector import Filters, select
from gexscan.logutil import redact, setup_logging
from gexscan.reports.brief import markdown, to_json
from gexscan.signals.market import MarketRegime
from gexscan.signals.ruleset import UNKNOWN, _st, classify_market, evaluate_gates, matches
from gexscan.strategies.base import OptLeg, calls_covered
from gexscan.strategies.verticals import pick_vertical

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
TODAY = dt.date(2026, 9, 25)
NAMES = ["UPCO", "DNCO", "PINCO", "EARN", "RICH", "TERM"]


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture(scope="module")
def sheet(cfg):
    return load_signal_sheet(cfg)


@pytest.fixture(scope="module")
def recs(cfg, tmp_path_factory):
    return recommend(NAMES, Filters(), cfg, FIX, TODAY, tmp_path_factory.mktemp("state"), use_news=False)


# ---------- signals + gates ----------

def test_matches_unknown_is_none():
    assert matches({"in": ["rich", "sell"]}, "rich") is True
    assert matches({"in": ["rich"]}, "low") is False
    assert matches({"not_in": ["spike"]}, "normal") is True
    assert matches({"in": ["rich"]}, UNKNOWN) is None


@pytest.mark.parametrize("vix,vix3m,vvix,d5,want_ts,want_level,want_vvix", [
    (15.5, 17.6, 82, 5, "contango", "normal", "normal"),
    (17.0, 17.4, 112, 2, "flat", "normal", "elevated"),
    (26.0, 24.0, 130, 3, "backwardation", "stressed", "spike"),
    (31.0, 28.0, 100, 20, "backwardation", "extreme", "spike"),     # 5-day jump alone is a spike
    (13.0, 15.0, 90, 0, "contango", "calm", "normal"),
])
def test_classify_market(cfg, sheet, vix, vix3m, vvix, d5, want_ts, want_level, want_vvix):
    m = MarketRegime("2026-09-25", vix=vix, vix3m=vix3m, vvix=vvix, ts_ratio=vix / vix3m, vvix_5d_pct=d5)
    st = classify_market(m, sheet, cfg, TODAY)
    assert (st["vix_ts"].state, st["vix_level"].state, st["vvix"].state) == (want_ts, want_level, want_vvix)


def test_classify_market_missing_data_is_unknown(cfg, sheet):
    st = classify_market(MarketRegime("2026-09-25"), sheet, cfg, TODAY)
    assert st["vix_ts"].state == st["vix_level"].state == st["vvix"].state == UNKNOWN


def _states(**kw):
    return {k: _st(k, v) for k, v in kw.items()}


SPEC = {"gates": {"required": {"vix_ts": {"in": ["contango", "flat"]},
                               "iv_rank": {"in": ["rich", "sell"], "unknown": "fail"},
                               "gex": {"in": ["positive"]}},
                  "any_of": [{"term_structure": {"in": ["backwardation"]}}, {"forward_factor": {"in": ["high"]}}],
                  "warn": {"earnings": {"in": ["upcoming"]}}}}


def test_gates_pass_and_fail():
    ok = evaluate_gates("x", SPEC, _states(vix_ts="contango", iv_rank="rich", gex="positive",
                                           term_structure="flat", forward_factor="high", earnings="none"), "T")
    assert ok.passed and not ok.failed
    bad = evaluate_gates("x", SPEC, _states(vix_ts="backwardation", iv_rank="rich", gex="positive",
                                            term_structure="flat", forward_factor="low", earnings="upcoming"), "T")
    assert not bad.passed
    assert any("vix_ts is backwardation" in f for f in bad.failed)
    assert any(f.startswith("none of:") for f in bad.failed)
    assert any("earnings is upcoming" in w for w in bad.warnings)


def test_unknown_gate_policy():
    # iv_rank says unknown: fail; gex uses the default (warn = pass at half credit)
    r = evaluate_gates("x", SPEC, _states(vix_ts="contango", gex="positive", forward_factor="high"), "T")
    assert not r.passed and any("iv_rank" in f for f in r.failed)
    r = evaluate_gates("x", SPEC, _states(vix_ts="contango", iv_rank="sell", forward_factor="high"), "T")
    assert r.passed and any("gex unknown" in w for w in r.warnings)


# ---------- defined risk ----------

def _leg(action, cp, k, exp):
    return OptLeg(action, cp, k, exp, 30, 1.0, 1.1, 1.05, 0.4, 0.3, 100, 10)


def test_calls_covered():
    cal = [_leg("SELL", "C", 100, "2026-10-23"), _leg("BUY", "C", 100, "2026-11-20")]
    assert calls_covered(cal)
    diag = [_leg("SELL", "C", 105, "2026-10-23"), _leg("BUY", "C", 110, "2026-11-20"),
            _leg("SELL", "P", 95, "2026-10-23"), _leg("BUY", "P", 90, "2026-11-20")]
    assert calls_covered(diag)
    assert not calls_covered([_leg("SELL", "C", 100, "2026-11-20"), _leg("BUY", "C", 105, "2026-10-23")])
    assert not calls_covered([_leg("SELL", "C", 100, "2026-10-23")])
    assert calls_covered([_leg("SELL", "P", 90, "2026-10-23")])        # no short calls: nothing to cover


# ---------- vertical search ----------

def _put_table(v, S=100.0, T=35 / 365):
    rows = []
    for k in range(60, 101):
        p = bs.price(S, k, T, v, "P")
        rows.append(dict(strike=float(k), delta_use=bs_delta(S, k, T, v, "P"), bid=p * 0.98, mid=p, spread_pct=0.04))
    return pd.DataFrame(rows)


def _ctw(r):
    sh, lg, cr = r
    return cr / abs(sh["strike"] - lg["strike"])


def test_pick_vertical_hits_target():
    r, note = pick_vertical(_put_table(0.6), "P", 100, (0.12, 0.30), 0.20, 5, 0.20, None, 0.10, 0.33)
    assert r is not None and _ctw(r) >= 0.33 and note == ""
    assert abs(r[0]["strike"] - r[1]["strike"]) <= 5 * 1.25 and r[1]["strike"] < r[0]["strike"]


def test_pick_vertical_falls_back_to_floor_with_note():
    r, note = pick_vertical(_put_table(0.3), "P", 100, (0.12, 0.30), 0.20, 5, 0.20, None, 0.10, 0.33)
    assert r is not None and 0.20 <= _ctw(r) < 0.33
    assert "under the ~33% target" in note


def test_pick_vertical_none_below_floor():
    r, why = pick_vertical(_put_table(0.3), "P", 100, (0.12, 0.30), 0.20, 5, 0.50, None, 0.10, 0.50)
    assert r is None and "50% of width" in why


# ---------- selector on the fixtures (integration) ----------

ROLL = re.compile(r"\broll", re.I)


def test_recommend_integration(recs, cfg):
    assert recs.recommendations, recs.explain
    per = {}
    for c in recs.recommendations:
        per[c.symbol] = per.get(c.symbol, 0) + 1
        assert 0 <= c.confidence.score <= 100 and c.confidence.bucket in ("Low", "Medium", "High")
        assert c.confidence.score >= cfg["engine"]["min_confidence"]
        assert c.risk == "defined" and c.max_loss is not None and c.max_loss > 0
        assert c.gates is not None and c.gates.passed
        for lvl in ("why_premium", "market_pricing", "second_order"):
            assert c.rationale[lvl], (c.summary(), lvl)
        assert c.rationale["invalidation"] and c.rationale["exit"]
        assert c.tp_value is not None and c.stop_value is not None and c.time_exit
        if c.kind == "credit":
            assert c.stop_value == pytest.approx(round(c.net * 2.0, 2), abs=0.011)
            assert c.stop_rule == "Stop = 2x credit; no rolling"
        else:
            assert c.max_loss == pytest.approx(-c.net * 100, rel=0.02)          # per contract: the debit
            assert "max loss = the" in c.stop_rule.lower()
        text = " ".join([c.stop, c.take_profit, c.time_stop, c.stop_rule, c.rationale["exit"]])
        assert not ROLL.search(re.sub(r"no rolling", "", text, flags=re.I)), text
    assert max(per.values()) <= cfg["engine"]["max_per_ticker"]
    scores = [c.confidence.score for c in recs.recommendations]
    assert scores == sorted(scores, reverse=True)


def test_selector_filters(recs, cfg, sheet):
    uni = list(recs.book.contexts.values())
    out, _, _ = select(uni, recs.book, sheet, cfg, Filters(strategies=["iron_condor"]))
    assert out and {c.strategy for c in out} == {"iron_condor"}
    out, _, _ = select(uni, recs.book, sheet, cfg, Filters(top_n=1))
    assert len(out) == 1
    out, dropped, _ = select(uni, recs.book, sheet, cfg, Filters(directions=["bearish"]))
    assert all(c.direction == "bearish" for c in out)
    assert any("direction" in d["why"] for d in dropped)
    out, _, _ = select(uni, recs.book, sheet, cfg, Filters(min_confidence=85))
    assert all(c.confidence.score >= 85 for c in out)


def test_undefined_risk_is_opt_in_and_flagged(recs, cfg, sheet):
    uni = list(recs.book.contexts.values())
    out, dropped, _ = select(uni, recs.book, sheet, cfg, Filters())
    assert all(c.risk == "defined" for c in out)
    out, _, _ = select(uni, recs.book, sheet, cfg, Filters(allow_undefined_risk=True, top_n=50, max_per_ticker=50))
    for c in out:
        if c.risk == "undefined":
            assert "UNDEFINED RISK" in c.flags


def test_brief_has_exit_rule_and_disclaimer(recs):
    md = markdown(recs, explain=True)
    assert "Stop = 2x credit; no rolling" in md
    assert "never places" in md.lower() or "not financial advice" in md.lower()
    js = to_json(recs)
    assert '"recommendations"' in js and '"disclaimer"' in js


# ---------- read-only broker + secrets ----------

def test_ibkr_allow_list_is_read_only():
    ok = ["/portfolio/accounts", "/portfolio/U1234567/positions/0", "/iserver/accounts",
          "/iserver/marketdata/snapshot?conids=1&fields=31"]
    bad = ["/iserver/account/U1234567/orders", "/iserver/account/orders", "/iserver/account/U1/order/123",
           "/iserver/reply/abc", "/iserver/account/trades", "/portfolio/U1/positions/0/close", "/iserver/account/U1/whatif"]
    assert all(IBKRReadOnly.allowed(p) for p in ok)
    assert not any(IBKRReadOnly.allowed(p) for p in bad)
    client = IBKRReadOnly("https://localhost:5000/v1/api")
    with pytest.raises(PermissionError):
        client.get("/iserver/account/U1/orders")          # refused before any request is made
    assert not any(hasattr(IBKRReadOnly, m) for m in ("post", "put", "delete", "patch"))


def test_redaction(monkeypatch, tmp_path):
    monkeypatch.setenv("FINNHUB_API_KEY", "sk_live_supersecret_123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:telegram-secret-token")
    s = ("GET https://x.io/q?symbol=NVDA&apikey=abc123&token=zzz sk_live_supersecret_123 "
         "https://api.telegram.org/bot999:telegram-secret-token/send Authorization: Bearer eyJ.abc-def")
    out = redact(s)
    for secret in ("abc123", "zzz", "sk_live_supersecret_123", "telegram-secret-token", "eyJ.abc-def"):
        assert secret not in out
    assert "symbol=NVDA" in out

    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    try:
        f = tmp_path / "run.log"
        setup_logging(verbose=True, json_logs=True, log_file=f)
        logging.getLogger("gexscan.test").info("calling %s", "https://x.io/?apikey=sk_live_supersecret_123")
        for h in root.handlers:
            h.flush()
        txt = f.read_text()
        assert "calling" in txt and "sk_live_supersecret_123" not in txt
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in saved:
            root.addHandler(h)
        root.setLevel(level)
