"""Black-Scholes-Merton helpers (vectorised with numpy). r (rate) and q (dividend yield) default to 0."""
from __future__ import annotations

import numpy as np

SQRT2 = np.sqrt(2.0)


try:
    from scipy.special import ndtr as _ndtr
except ImportError:  # pragma: no cover
    from math import erf as _erf

    def _ndtr(x):
        return 0.5 * (1.0 + np.vectorize(_erf)(np.asarray(x, dtype=float) / SQRT2))


def _ncdf(x):
    return _ndtr(np.asarray(x, dtype=float))


def _npdf(x):
    x = np.asarray(x, dtype=float)
    return np.exp(-0.5 * x * x) / np.sqrt(2 * np.pi)


def _d1(S, K, T, v, r=0.0, q=0.0):
    S, K, T, v = (np.asarray(a, dtype=float) for a in (S, K, T, v))
    T = np.maximum(T, 1e-6)
    v = np.maximum(v, 1e-4)
    return (np.log(S / K) + (r - q + 0.5 * v * v) * T) / (v * np.sqrt(T))


def gamma(S, K, T, v, r=0.0, q=0.0):
    d1 = _d1(S, K, T, v, r, q)
    T = np.maximum(np.asarray(T, dtype=float), 1e-6)
    v = np.maximum(np.asarray(v, dtype=float), 1e-4)
    return np.exp(-q * T) * _npdf(d1) / (np.asarray(S, dtype=float) * v * np.sqrt(T))


def delta(S, K, T, v, cp, r=0.0, q=0.0):
    d1 = _d1(S, K, T, v, r, q)
    dq = np.exp(-q * np.maximum(np.asarray(T, dtype=float), 1e-6))
    call = dq * _ncdf(d1)
    return np.where(np.asarray(cp) == "C", call, call - dq)


def price(S, K, T, v, cp, r=0.0, q=0.0):
    d1 = _d1(S, K, T, v, r, q)
    T_ = np.maximum(np.asarray(T, dtype=float), 1e-6)
    v_ = np.maximum(np.asarray(v, dtype=float), 1e-4)
    d2 = d1 - v_ * np.sqrt(T_)
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    disc, dq = np.exp(-r * T_), np.exp(-q * T_)
    c = S * dq * _ncdf(d1) - K * disc * _ncdf(d2)
    p = K * disc * _ncdf(-d2) - S * dq * _ncdf(-d1)
    return np.where(np.asarray(cp) == "C", c, p)
