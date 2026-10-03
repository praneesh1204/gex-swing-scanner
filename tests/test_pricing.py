"""analytics.pricing vs the independent scipy reference in web/static/golden.json (spec v2.2 §6.7), plus the
JS engine parity check (tests/js/parity.mjs) when node is installed."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from gexscan.analytics import bs, pricing as P

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "src/gexscan/web/static/golden.json").read_text())
TOL = GOLDEN["tolerance"]["python"]


def close(a, b, tol=TOL):
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol * max(1.0, abs(b))


def legs_of(p):
    return [P.Leg.from_dict(d) for d in p["legs"]]


def ev_of(p):
    return P.Event(**p["event"]) if p.get("event") else None


@pytest.mark.parametrize("c", GOLDEN["bsm"], ids=lambda c: f"{c['cp']}{c['K']}@{c['S']}")
def test_bsm_matches_reference(c):
    px = float(bs.price(c["S"], c["K"], c["T"], c["sigma"], c["cp"], c["r"], c["q"]))
    assert close(px, c["price"]), (px, c["price"])
    assert close(float(bs.delta(c["S"], c["K"], c["T"], c["sigma"], c["cp"], c["r"], c["q"])), c["delta"])
    assert close(float(bs.gamma(c["S"], c["K"], c["T"], c["sigma"], c["r"], c["q"])), c["gamma"])


def test_put_call_parity_with_dividends():
    S, K, T, v, r, q = 120.0, 110.0, 0.4, 0.35, 0.04, 0.02
    c, p = (float(bs.price(S, K, T, v, x, r, q)) for x in ("C", "P"))
    assert math.isclose(c - p, S * math.exp(-q * T) - K * math.exp(-r * T), rel_tol=1e-12)


@pytest.mark.parametrize("e", GOLDEN["event_sigma"])
def test_event_sigma(e):
    post, floored = P.event_sigma(e["sigma"], e["T0"], e["move"])
    assert close(post, e["post"]) and floored == e["floored"]


@pytest.mark.parametrize("p", GOLDEN["positions"], ids=lambda p: p["name"])
def test_position_values(p):
    legs, ev = legs_of(p), ev_of(p)
    for v in p["values"]:
        got = float(P.position_value(legs, np.array([v["S"]]), v["on"], p["today"], p["r"], p["q"], v["ivmult"], ev)[0])
        assert close(got, v["V"]), (v, got)
    assert close(P.net_basis(legs), p["risk"]["net_basis"])


@pytest.mark.parametrize("p", GOLDEN["positions"], ids=lambda p: p["name"])
def test_risk_stats_pop_greeks(p):
    legs, ev, ref = legs_of(p), ev_of(p), p["risk"]
    rs = P.risk_stats(legs, p["today"], p["r"], p["q"], 1.0, ev, p["spot"])
    assert rs["at"] == ref["at"]
    assert close(rs["max_profit"], ref["max_profit"]) and close(rs["max_loss"], ref["max_loss"])
    assert rs["unbounded_profit"] == ref["unbounded_profit"] and rs["unbounded_loss"] == ref["unbounded_loss"]
    assert len(rs["breakevens"]) == len(ref["breakevens"])
    assert all(close(a, b) for a, b in zip(rs["breakevens"], ref["breakevens"]))
    pp = p["pop"]
    assert close(P.pop(legs, p["spot"], pp["on"], p["today"], pp["sigma"], p["r"], p["q"], 1.0, ev), pp["value"])
    g = P.greeks(legs, p["greeks"]["S"], p["greeks"]["on"], p["today"], p["r"], p["q"], 1.0, ev)
    for k in ("delta", "gamma", "vega", "theta"):
        assert close(g[k], p["greeks"][k]), (k, g[k], p["greeks"][k])


def test_unbounded_short_call_and_stock_legs():
    legs = [P.Leg(-1, "C", 100, "2026-11-20", 1, 3.0, 0.4)]
    rs = P.risk_stats(legs, "2026-10-02", 0.04, 0.0, spot=100)
    assert rs["max_loss"] is None and rs["unbounded_loss"] and math.isclose(rs["max_profit"], 300.0)
    cc = [P.Leg(1, "S", 0, None, 100, 98.0), P.Leg(-1, "C", 105, "2026-11-20", 1, 2.0, 0.4)]   # covered call
    rs = P.risk_stats(cc, "2026-10-02", 0.04, 0.0, spot=98)
    assert math.isclose(rs["max_profit"], (105 - 98 + 2) * 100) and math.isclose(rs["max_loss"], -(98 - 2) * 100)
    assert rs["breakevens"] == pytest.approx([96.0])


def test_expiry_is_intrinsic_and_iv_slider_scales():
    lg = P.Leg(1, "P", 50, "2026-10-16", 1, 1.0, 0.5)
    assert float(P.leg_price(lg, 45.0, "2026-10-16", "2026-10-02")) == 5.0
    lo = float(P.leg_price(lg, 50.0, "2026-10-02", "2026-10-02", ivmult=1.0))
    hi = float(P.leg_price(lg, 50.0, "2026-10-02", "2026-10-02", ivmult=2.0))
    assert hi > lo * 1.8


def test_event_crush_lowers_value_after_the_print():
    lg = P.Leg(1, "C", 80, "2026-10-30", 1, 4.0, 0.72)
    ev = P.Event("2026-10-23", 0.09)
    pre = float(P.leg_price(lg, 80.0, "2026-10-22", "2026-10-02", event=ev))
    post = float(P.leg_price(lg, 80.0, "2026-10-23", "2026-10-02", event=ev))
    flat = float(P.leg_price(lg, 80.0, "2026-10-23", "2026-10-02"))
    assert post < flat and pre > post * 1.3
    assert math.isclose(P.leg_sigma(lg, "2026-10-02", "2026-10-02", 1.0, ev), 0.72, rel_tol=1e-12)


def test_leg_codec_roundtrip_and_validation():
    lg = P.Leg(-1, "P", 237.5, "2026-11-13", 2, 3.15, 0.44)
    assert P.Leg.decode(lg.encode()) == lg
    assert P.Leg.decode("B:S:0::100:250:").type == "S"
    for bad in ("X:C:1:2026-11-13:1:1:", "B:Z:1:2026-11-13:1:1:", "B:C:0:2026-11-13:1:1:", "B:C:100::1:1:"):
        with pytest.raises(ValueError):
            P.Leg.decode(bad)


def test_pop_matches_n_d2_for_a_single_call():
    S, K, T, v, r = 100.0, 105.0, 60 / 365, 0.4, 0.04
    lg = P.Leg(1, "C", K, "2026-12-01", 1, 0.0, v)        # zero fill: profit iff S_T > K
    d2 = (math.log(S / K) + (r - v * v / 2) * T) / (v * math.sqrt(T))
    assert math.isclose(P.pop([lg], S, "2026-12-01", "2026-10-02", v, r), float(bs._ncdf(d2)), rel_tol=1e-9)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_engine_parity():
    r = subprocess.run(["node", str(ROOT / "tests/js/parity.mjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout
