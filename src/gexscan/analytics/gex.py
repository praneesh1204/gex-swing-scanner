"""Dealer gamma exposure (GEX), walls, gamma flip, max pain and expected move from a CBOE chain.

Convention (public 'naive' model): dealers are long calls (+gamma) and short puts (-gamma).
GEX is expressed in $ of delta change per 1% move in the underlying.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from . import bs
from ..data.cboe import Chain


@dataclass
class Levels:
    symbol: str
    spot: float
    net_gex: float                 # $ per 1% move
    regime: str                    # positive_gamma / negative_gamma
    gamma_flip: float | None
    flip_status: str               # crossing / no_boundary_positive / no_boundary_negative
    call_wall: float | None
    put_wall: float | None
    max_pain_near: float | None
    near_expiry: str | None
    atm_iv: float | None           # ~30 DTE ATM IV (decimal)
    exp_move_near: float | None    # $ straddle-implied move, nearest expiry
    exp_move_near_pct: float | None
    total_call_oi: float
    total_put_oi: float
    pc_oi_ratio: float | None
    atm_spread_pct: float | None   # (ask-bid)/mid near ATM, liquidity proxy
    flip_roots: list = field(default_factory=list)   # every zero crossing of modeled GEX (flip = nearest spot)
    max_pos_gamma_strike: float | None = None        # strike with the largest net +GEX
    max_neg_gamma_strike: float | None = None        # strike with the most negative net GEX
    top_oi_strikes: list = field(default_factory=list)  # [(strike, type, oi)] top 5 by OI, <= max_dte
    call_volume: float = 0.0                          # today's option volume (all expiries)
    put_volume: float = 0.0
    as_of: str = ""                                   # CBOE timestamp (UTC)

    def to_dict(self):
        return asdict(self)


def _clean_iv(df: pd.DataFrame) -> pd.Series:
    """Use contract IV where sane; otherwise fall back to the median sane IV of that expiry."""
    iv = df["iv"].where((df["iv"] > 0.02) & (df["iv"] < 5.0))
    med = iv.groupby(df["expiry"]).transform("median")
    overall = iv.median() if iv.notna().any() else 0.5
    return iv.fillna(med).fillna(overall)


def _gex_at(S: float, K, T, iv, oi, sign) -> np.ndarray:
    g = bs.gamma(S, K, T, iv)
    return sign * g * oi * 100.0 * S * S * 0.01


def compute_levels(chain: Chain, max_dte: int = 60, flip_range: float = 0.30, grid_points: int = 121) -> Levels:
    S = chain.spot
    df = chain.options.copy()
    df = df[(df["dte"] <= max_dte) & (df["oi"] > 0)].copy()
    if df.empty or S <= 0:
        return Levels(chain.symbol, S, 0.0, "unknown", None, "no_data", None, None, None, None, None, None, None, 0, 0, None, None)

    df["iv_c"] = _clean_iv(df)
    df["T"] = np.maximum(df["dte"], 0.5) / 365.0
    df["sign"] = np.where(df["type"] == "C", 1.0, -1.0)
    K, T, iv, oi, sign = (df[c].to_numpy(dtype=float) for c in ("strike", "T", "iv_c", "oi", "sign"))

    df["gex"] = _gex_at(S, K, T, iv, oi, sign)
    net = float(df["gex"].sum())

    # walls: strike with largest positive call GEX / most negative put GEX
    by_k = df.groupby(["strike", "type"])["gex"].sum().unstack(fill_value=0.0)
    call_wall = float(by_k["C"].idxmax()) if "C" in by_k and by_k["C"].max() > 0 else None
    put_wall = float(by_k["P"].idxmin()) if "P" in by_k and by_k["P"].min() < 0 else None

    # gamma flip: re-price total GEX across a spot grid, find zero crossing nearest spot
    grid = np.linspace(S * (1 - flip_range), S * (1 + flip_range), grid_points)
    totals = np.array([_gex_at(x, K, T, iv, oi, sign).sum() for x in grid])
    flip, status, roots = None, "", []
    crossings = np.where(np.sign(totals[:-1]) != np.sign(totals[1:]))[0]
    if len(crossings):
        cands = []
        for i in crossings:
            x0, x1, y0, y1 = grid[i], grid[i + 1], totals[i], totals[i + 1]
            cands.append(x0 - y0 * (x1 - x0) / (y1 - y0))
        flip = float(min(cands, key=lambda x: abs(x - S)))
        roots = sorted(round(float(x), 2) for x in cands)
        status = "crossing" if len(cands) == 1 else f"crossing ({len(cands)} roots)"
    else:
        status = "no_boundary_positive" if totals.mean() > 0 else "no_boundary_negative"

    # nearest expiry stats
    all_opts = chain.options
    near_exp = min(all_opts["expiry"]) if not all_opts.empty else None
    mp = _max_pain(all_opts[all_opts["expiry"] == near_exp]) if near_exp is not None else None
    em, em_pct, spread_pct = _expected_move(all_opts, near_exp, S)
    atm_iv = _atm_iv(all_opts, S, target_dte=30)

    net_k = by_k.sum(axis=1)
    top_oi = df.groupby(["strike", "type"])["oi"].sum().sort_values(ascending=False).head(5)
    tc = float(df.loc[df["type"] == "C", "oi"].sum())
    tp = float(df.loc[df["type"] == "P", "oi"].sum())
    return Levels(
        symbol=chain.symbol,
        spot=S,
        net_gex=net,
        regime="positive_gamma" if net > 0 else "negative_gamma",
        gamma_flip=flip,
        flip_status=status,
        call_wall=call_wall,
        put_wall=put_wall,
        max_pain_near=mp,
        near_expiry=str(near_exp) if near_exp is not None else None,
        atm_iv=atm_iv,
        exp_move_near=em,
        exp_move_near_pct=em_pct,
        total_call_oi=tc,
        total_put_oi=tp,
        pc_oi_ratio=(tp / tc) if tc else None,
        atm_spread_pct=spread_pct,
        flip_roots=roots,
        max_pos_gamma_strike=float(net_k.idxmax()) if len(net_k) and net_k.max() > 0 else None,
        max_neg_gamma_strike=float(net_k.idxmin()) if len(net_k) and net_k.min() < 0 else None,
        top_oi_strikes=[(float(k), t, float(v)) for (k, t), v in top_oi.items()],
        call_volume=float(all_opts.loc[all_opts["type"] == "C", "volume"].sum()),
        put_volume=float(all_opts.loc[all_opts["type"] == "P", "volume"].sum()),
        as_of=chain.as_of,
    )


def gex_by_strike(chain: Chain, max_dte: int = 60, window_pct: float = 0.25) -> pd.DataFrame:
    """Per-strike call GEX, put GEX, OI and volume within +/- window of spot (for charts and the report)."""
    S = chain.spot
    df = chain.options[(chain.options["dte"] <= max_dte)].copy()
    if df.empty:
        return pd.DataFrame()
    df["iv_c"] = _clean_iv(df)
    T = np.maximum(df["dte"].to_numpy(float), 0.5) / 365.0
    sign = np.where(df["type"] == "C", 1.0, -1.0)
    df["gex"] = _gex_at(S, df["strike"].to_numpy(float), T, df["iv_c"].to_numpy(float), df["oi"].to_numpy(float), sign)
    g = df.pivot_table(index="strike", columns="type", values=["gex", "oi", "volume"], aggfunc="sum", fill_value=0.0)
    g.columns = [f"{a}_{b}" for a, b in g.columns]
    for c in ("gex_C", "gex_P", "oi_C", "oi_P", "volume_C", "volume_P"):
        if c not in g:
            g[c] = 0.0
    g["gex_net"] = g["gex_C"] + g["gex_P"]
    return g[(g.index >= S * (1 - window_pct)) & (g.index <= S * (1 + window_pct))]


def _max_pain(exp_df: pd.DataFrame) -> float | None:
    if exp_df.empty:
        return None
    strikes = np.sort(exp_df["strike"].unique())
    calls = exp_df[exp_df["type"] == "C"][["strike", "oi"]].to_numpy()
    puts = exp_df[exp_df["type"] == "P"][["strike", "oi"]].to_numpy()
    best, best_pay = None, None
    for s in strikes:
        pay = (np.maximum(0, s - calls[:, 0]) * calls[:, 1]).sum() + (np.maximum(0, puts[:, 0] - s) * puts[:, 1]).sum()
        if best_pay is None or pay < best_pay:
            best, best_pay = float(s), pay
    return best


def _atm_pair(opts: pd.DataFrame, expiry, S: float):
    e = opts[opts["expiry"] == expiry]
    if e.empty:
        return None, None
    k = e.iloc[(e["strike"] - S).abs().argsort()]["strike"].iloc[0]
    c = e[(e["strike"] == k) & (e["type"] == "C")]
    p = e[(e["strike"] == k) & (e["type"] == "P")]
    return (c.iloc[0] if len(c) else None), (p.iloc[0] if len(p) else None)


def _expected_move(opts: pd.DataFrame, expiry, S: float):
    if expiry is None:
        return None, None, None
    c, p = _atm_pair(opts, expiry, S)
    if c is None or p is None or c["mid"] <= 0 or p["mid"] <= 0:
        return None, None, None
    straddle = float(c["mid"] + p["mid"])
    spreads = []
    for leg in (c, p):
        if leg["mid"] > 0 and leg["ask"] > 0:
            spreads.append((leg["ask"] - leg["bid"]) / leg["mid"])
    return straddle, straddle / S * 100.0, (float(np.mean(spreads)) if spreads else None)


def _atm_iv(opts: pd.DataFrame, S: float, target_dte: int = 30) -> float | None:
    if opts.empty:
        return None
    exps = opts.groupby("expiry")["dte"].first()
    exp = (exps - target_dte).abs().idxmin()
    c, p = _atm_pair(opts, exp, S)
    ivs = [float(x["iv"]) for x in (c, p) if x is not None and 0.02 < x["iv"] < 5]
    return float(np.mean(ivs)) if ivs else None
