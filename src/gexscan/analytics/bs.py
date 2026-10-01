"""Black-Scholes helpers (vectorised with numpy). r and q default to 0 for simplicity."""
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


def _d1(S, K, T, v, r=0.0):
    S, K, T, v = (np.asarray(a, dtype=float) for a in (S, K, T, v))
    T = np.maximum(T, 1e-6)
    v = np.maximum(v, 1e-4)
    return (np.log(S / K) + (r + 0.5 * v * v) * T) / (v * np.sqrt(T))


def gamma(S, K, T, v, r=0.0):
    d1 = _d1(S, K, T, v, r)
    T = np.maximum(np.asarray(T, dtype=float), 1e-6)
    v = np.maximum(np.asarray(v, dtype=float), 1e-4)
    return _npdf(d1) / (np.asarray(S, dtype=float) * v * np.sqrt(T))


def delta(S, K, T, v, cp, r=0.0):
    d1 = _d1(S, K, T, v, r)
    call = _ncdf(d1)
    return np.where(np.asarray(cp) == "C", call, call - 1.0)


def price(S, K, T, v, cp, r=0.0):
    d1 = _d1(S, K, T, v, r)
    T_ = np.maximum(np.asarray(T, dtype=float), 1e-6)
    v_ = np.maximum(np.asarray(v, dtype=float), 1e-4)
    d2 = d1 - v_ * np.sqrt(T_)
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    disc = np.exp(-r * T_)
    c = S * _ncdf(d1) - K * disc * _ncdf(d2)
    p = K * disc * _ncdf(-d2) - S * _ncdf(-d1)
    return np.where(np.asarray(cp) == "C", c, p)
