"""Volatility stack: realized estimators, GARCH-family forecast, IV rank/percentile, probabilities.

All outputs are model estimates. GARCH uses the `arch` package (GJR-GARCH(1,1) with Student-t innovations)
and falls back to plain GARCH, then to EWMA (RiskMetrics lambda=0.94) if fitting fails or `arch` is absent.
Every estimator uses only bars up to the as-of date, so there's no lookahead.
"""
from __future__ import annotations

import logging
import warnings
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from . import bs

log = logging.getLogger(__name__)
ANN = 252


@dataclass
class VolStack:
    rv10: float | None = None
    rv20: float | None = None
    rv60: float | None = None
    parkinson20: float | None = None
    garman_klass20: float | None = None
    ewma: float | None = None
    garch_forecast: float | None = None     # annualised mean vol over the horizon
    garch_model: str = ""
    horizon_days: int = 30
    atm_iv: float | None = None
    iv_minus_garch: float | None = None     # vol points (decimal); > 0 = options rich vs forecast
    iv_rv_ratio: float | None = None
    iv_rank: float | None = None            # 0-100
    iv_percentile: float | None = None      # 0-100
    iv_rank_source: str = ""                # "snapshots (n=..)" or "proxy: RV20 1y range"
    term_slope: float | None = None         # IV(~60d) - IV(~7-14d); < 0 = backwardation (event/stress)
    put_skew_25d: float | None = None       # IV(25d put) - IV(25d call), ~30 DTE

    def to_dict(self):
        return asdict(self)


def _rv(r: pd.Series, n: int) -> float | None:
    return float(r.tail(n).std() * np.sqrt(ANN)) if len(r) >= n else None


def parkinson(h: pd.DataFrame, n: int = 20) -> float | None:
    x = np.log(h["high"] / h["low"]).tail(n)
    if len(x) < n or (x <= 0).all():
        return None
    return float(np.sqrt((x ** 2).mean() / (4 * np.log(2)) * ANN))


def garman_klass(h: pd.DataFrame, n: int = 20) -> float | None:
    t = h.tail(n)
    if len(t) < n:
        return None
    hl = np.log(t["high"] / t["low"]) ** 2
    co = np.log(t["close"] / t["open"]) ** 2
    v = (0.5 * hl - (2 * np.log(2) - 1) * co).mean()
    return float(np.sqrt(max(v, 0) * ANN))


def ewma_vol(r: pd.Series, lam: float = 0.94) -> float | None:
    if len(r) < 30:
        return None
    var = float(r.iloc[:30].var())
    for x in r.iloc[30:]:
        var = lam * var + (1 - lam) * x * x
    return float(np.sqrt(var * ANN))


def garch_forecast(r: pd.Series, horizon: int) -> tuple[float | None, str]:
    """Mean annualised vol over the next `horizon` trading days."""
    if len(r) < 150:
        return ewma_vol(r), "EWMA (short history)"
    try:
        from arch import arch_model
    except ImportError:
        return ewma_vol(r), "EWMA (arch not installed)"
    y = r.tail(1000) * 100
    for o, name in ((1, "GJR-GARCH(1,1)-t"), (0, "GARCH(1,1)-t")):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                res = arch_model(y, mean="Constant", vol="GARCH", p=1, o=o, q=1, dist="t").fit(disp="off")
            if res.convergence_flag != 0:
                continue
            var = res.forecast(horizon=max(horizon, 1), reindex=False).variance.values[-1]
            v = float(np.sqrt(np.mean(var) * ANN) / 100)
            if 0.03 < v < 5:
                return v, name
        except Exception as e:  # numerical failures
            log.debug("garch %s failed: %s", name, e)
    return ewma_vol(r), "EWMA (GARCH did not converge)"


def iv_rank_pct(iv: float | None, history: pd.Series, rv_hist: pd.Series | None) -> tuple[float | None, float | None, str]:
    """IV rank and percentile from our own IV snapshots. Needs >= 20 days of history,
    otherwise uses a labelled proxy: where today's IV sits within the past year's RV20 range."""
    if iv is None:
        return None, None, ""
    if history is not None and len(history) >= 20:
        lo, hi = float(history.min()), float(history.max())
        rank = (iv - lo) / (hi - lo) * 100 if hi > lo else 50.0
        pct = float((history < iv).mean() * 100)
        return round(float(np.clip(rank, 0, 100)), 1), round(pct, 1), f"snapshots (n={len(history)})"
    if rv_hist is not None and len(rv_hist.dropna()) >= 120:
        h = rv_hist.dropna().tail(252)
        lo, hi = float(h.min()), float(h.max())
        rank = (iv - lo) / (hi - lo) * 100 if hi > lo else 50.0
        pct = float((h < iv).mean() * 100)
        return round(float(np.clip(rank, 0, 100)), 1), round(pct, 1), "proxy: IV vs 1y RV20 range"
    return None, None, ""


def _iv_at_delta(e: pd.DataFrame, S: float, T: float, cp: str, target: float) -> float | None:
    x = e[(e["type"] == cp) & (e["iv"] > 0.02) & (e["iv"] < 5)].copy()
    if x.empty:
        return None
    d = bs.delta(S, x["strike"].to_numpy(), T, x["iv"].to_numpy(), np.array([cp] * len(x)))
    i = int(np.argmin(np.abs(np.abs(d) - target)))
    return float(x["iv"].iloc[i])


def surface_stats(opts: pd.DataFrame, S: float) -> tuple[float | None, float | None]:
    """Term-structure slope (IV ~60d minus IV ~10d) and 25-delta put-call skew at ~30 DTE."""
    if opts is None or opts.empty:
        return None, None
    exps = opts.groupby("expiry")["dte"].first()

    def atm(dte_target):
        e = (exps - dte_target).abs().idxmin()
        x = opts[(opts["expiry"] == e) & (opts["iv"] > 0.02) & (opts["iv"] < 5)]
        if x.empty:
            return None, None
        k = x.iloc[(x["strike"] - S).abs().argsort()]["strike"].iloc[0]
        return float(x[x["strike"] == k]["iv"].mean()), e

    near, _ = atm(10)
    far, _ = atm(60)
    slope = (far - near) if (near and far) else None
    _, e30 = atm(30)
    skew = None
    if e30 is not None:
        e = opts[opts["expiry"] == e30]
        T = max(int(e["dte"].iloc[0]), 1) / 365
        p, c = _iv_at_delta(e, S, T, "P", 0.25), _iv_at_delta(e, S, T, "C", 0.25)
        skew = (p - c) if (p and c) else None
    return slope, skew


def compute_vol_stack(hist: pd.DataFrame, atm_iv: float | None, horizon_days: int = 30,
                      iv_history: pd.Series | None = None, opts: pd.DataFrame | None = None,
                      spot: float | None = None) -> VolStack:
    v = VolStack(horizon_days=horizon_days, atm_iv=atm_iv)
    if hist is not None and len(hist) >= 21:
        h = hist.astype(float)
        r = np.log(h["close"] / h["close"].shift(1)).dropna()
        v.rv10, v.rv20, v.rv60 = _rv(r, 10), _rv(r, 20), _rv(r, 60)
        v.parkinson20, v.garman_klass20 = parkinson(h), garman_klass(h)
        v.ewma = ewma_vol(r)
        trading_days = max(int(round(horizon_days * 252 / 365)), 1)
        v.garch_forecast, v.garch_model = garch_forecast(r, trading_days)
        rv_hist = r.rolling(20).std() * np.sqrt(ANN)
        v.iv_rank, v.iv_percentile, v.iv_rank_source = iv_rank_pct(atm_iv, iv_history, rv_hist)
    if atm_iv and v.garch_forecast:
        v.iv_minus_garch = atm_iv - v.garch_forecast
    if atm_iv and v.rv20:
        v.iv_rv_ratio = atm_iv / v.rv20
    if opts is not None and spot:
        v.term_slope, v.put_skew_25d = surface_stats(opts, spot)
    return v


def prob_itm(S: float, K: float, T_years: float, iv: float, cp: str) -> float:
    """Risk-neutral probability of finishing in the money (N(d2)); an estimate, not a forecast."""
    d1 = bs._d1(S, K, T_years, iv)
    d2 = d1 - max(iv, 1e-4) * np.sqrt(max(T_years, 1e-6))
    p = float(bs._ncdf(d2)) if cp == "C" else float(bs._ncdf(-d2))
    return p


def prob_touch(S: float, K: float, T_years: float, iv: float, cp: str) -> float:
    """Reflection-principle approximation: P(touch) ~= 2 x P(ITM at expiry), capped at 1."""
    return float(min(1.0, 2 * prob_itm(S, K, T_years, iv, cp)))


def expected_move(S: float, iv: float, dte: int) -> float:
    return float(S * iv * np.sqrt(max(dte, 0) / 365))
