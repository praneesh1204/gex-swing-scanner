"""analytics.simulate (spec v2.2 §6.8): unbiased against BSM, plain GBM when vol is flat, exit-plan
accounting, determinism."""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from gexscan.analytics import bs, pricing as P, simulate as SIM

TODAY = dt.date(2026, 10, 2)
EXP = "2026-11-20"


def _paths(sigma=0.4, n=40_000, seed=7, event=None, term=None, spot=100.0, r=0.04, q=0.01):
    dates = SIM.step_dates(TODAY, EXP)
    var = SIM.diffusion_variance(TODAY, dates, term, sigma, event)
    return dates, var, SIM.simulate_paths(spot, TODAY, dates, var, r, q, event, n, np.random.default_rng(seed))


def test_discounted_mean_payoff_matches_bsm():
    dates, _, S = _paths()
    T = (dt.date.fromisoformat(EXP) - TODAY).days / 365
    pay = np.maximum(S[-1] - 105, 0) * math.exp(-0.04 * T)
    ref = float(bs.price(100, 105, T, 0.4, "C", 0.04, 0.01))
    assert abs(pay.mean() - ref) < 3 * pay.std() / math.sqrt(len(pay))


def test_probability_above_strike_matches_n_d2():
    _, _, S = _paths()
    T = (dt.date.fromisoformat(EXP) - TODAY).days / 365
    d2 = (math.log(100 / 105) + (0.04 - 0.01 - 0.08) * T) / (0.4 * math.sqrt(T))
    p = float(np.mean(S[-1] > 105))
    assert abs(p - float(bs._ncdf(d2))) < 3 * math.sqrt(p * (1 - p) / S.shape[1])


def test_event_keeps_total_variance_and_adds_the_jump():
    ev = P.Event("2026-10-23", 0.09)
    dates, var, S = _paths(event=ev)
    T = (dt.date.fromisoformat(EXP) - TODAY).days / 365
    assert math.isclose(var.sum() + 0.09 ** 2, 0.4 ** 2 * T, rel_tol=1e-9)
    k = next(i for i, d in enumerate(dates) if d >= ev.date)
    jump = np.log(S[k] / S[k - 1])
    assert jump.std() > 2 * np.log(S[k - 1] / S[k - 2]).std()


def test_flat_term_structure_is_plain_gbm():
    dates, _, S = _paths(term=[("2026-10-30", 0.4), (EXP, 0.4), ("2026-12-18", 0.4)], seed=11, n=2000)
    dts = np.diff(np.concatenate([[0.0], [(d - TODAY).days / 365 for d in dates]]))
    Z = np.random.default_rng(11).standard_normal((len(dates), 2000))
    ref = 100 * np.exp(np.cumsum(((0.04 - 0.01 - 0.08) * dts)[:, None] + 0.4 * np.sqrt(dts)[:, None] * Z, axis=0))
    assert np.allclose(S, ref, rtol=1e-12)


def test_steps_are_weekdays_and_end_on_the_horizon():
    d = SIM.step_dates(TODAY, "2026-10-17")      # a Saturday expiry still gets its own step
    assert d[-1] == dt.date(2026, 10, 17) and all(x.weekday() < 5 for x in d[:-1]) and d[0] > TODAY


def _bear_call():
    return [P.Leg(-1, "C", 105, EXP, 1, 2.40, 0.42), P.Leg(1, "C", 110, EXP, 1, 1.00, 0.40)]


def test_credit_stop_loses_at_least_one_credit_and_never_more_than_max_loss():
    legs = _bear_call()
    out = SIM.simulate(legs, 100.0, TODAY, 0.04, 0.0, n=8000, seed=3)
    credit = -P.net_basis(legs)
    assert out["plan"]["kind"] == "credit" and out["plan"]["stop"] == 2.0
    st = out["stats"]["plan"]
    assert st["p_stop"] > 0 and st["p_target"] > 0
    res = SIM.simulate(legs, 100.0, TODAY, 0.04, 0.0, n=8000, seed=3)
    assert res["stats"] == out["stats"]                      # same seed, same answer
    lo = min(res["histogram"]["edges"])
    assert lo >= out["max_loss"] - 1e-6                      # nothing worse than the defined max loss
    assert st["cvar5"] <= -credit + 1e-6                     # the bad tail is at least a 1x-credit loss


def test_stop_path_accounting_by_hand():
    legs = _bear_call()
    credit = -P.net_basis(legs)
    dates = SIM.step_dates(TODAY, EXP)
    var = SIM.diffusion_variance(TODAY, dates, None, 0.41, None)
    S = SIM.simulate_paths(100.0, TODAY, dates, var, 0.04, 0.0, None, 4000, np.random.default_rng(5))
    V = np.stack([P.position_value(legs, S[k], d, TODAY, 0.04, 0.0) for k, d in enumerate(dates)])
    first = np.argmax(-V >= 2 * credit, axis=0)
    hit = (-V >= 2 * credit).any(axis=0)
    pl = V[first[hit], np.where(hit)[0]] - P.net_basis(legs)
    rs = P.risk_stats(legs, TODAY, 0.04, 0.0, spot=100.0)
    assert hit.any() and (pl <= -credit + 1e-9).all() and (pl >= rs["max_loss"] - 1e-9).all()


def test_debit_plan_and_outputs_shape():
    legs = [P.Leg(1, "C", 100, EXP, 1, 4.6, 0.4), P.Leg(-1, "C", 110, EXP, 1, 1.6, 0.38)]
    out = SIM.simulate(legs, 100.0, TODAY, 0.04, 0.0, n=3000)
    assert out["plan"]["kind"] == "debit" and out["plan"]["texit"] < EXP
    assert len(out["fan"]["plan"]["p50"]) == out["steps"] == len(out["dates"])
    assert sum(out["histogram"]["plan"]) == out["n"] == sum(out["histogram"]["hold"])
    assert math.isclose(sum(out["stats"]["plan"]["exit_mix"].values()), 1.0)
    assert out["stats"]["plan"]["exit_mix"]["expiry"] == 0.0          # the time exit comes first
    assert SIM.LABELS[0] in out["labels"]
    again = SIM.simulate(legs, 100.0, TODAY, 0.04, 0.0, n=3000)
    assert again["seed"] == out["seed"] and again["stats"] == out["stats"]


def test_path_budget_cuts_n_and_says_so():
    legs = [P.Leg(1, "C", 100 + i, "2027-06-18", 1, 1.0, 0.4) for i in range(8)]
    out = SIM.simulate(legs, 100.0, TODAY, n=50_000, seed=1)
    assert out["n"] < 50_000 and any("cut" in x for x in out["labels"])


def test_bad_inputs():
    with pytest.raises(ValueError):
        SIM.simulate([P.Leg(1, "S", 0, None, 100, 100.0)], 100.0, TODAY)
    with pytest.raises(ValueError):
        SIM.simulate([P.Leg(1, "C", 100, "2026-09-30", 1, 1.0, 0.4)], 100.0, TODAY)
