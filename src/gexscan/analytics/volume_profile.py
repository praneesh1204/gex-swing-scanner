"""Volume profile (POC / VAH / VAL / HVN / LVN), VWAPs and volume indicators.

The profile spreads each bar's volume evenly across its high-low range. Intraday bars are preferred;
it falls back to daily bars (coarser) and labels which one it used.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd


@dataclass
class VolumeProfile:
    source: str = ""
    start: str = ""
    end: str = ""
    poc: float | None = None
    vah: float | None = None
    val: float | None = None
    hvn: list = field(default_factory=list)     # high-volume nodes (support/resistance), nearest spot first
    lvn: list = field(default_factory=list)     # low-volume nodes (price tends to move fast through them)
    vwap_session: float | None = None           # last session VWAP
    vwap_window: float | None = None            # VWAP over the whole profile window
    avwap_from_low: float | None = None         # anchored at the window's lowest low
    avwap_from_high: float | None = None        # anchored at the window's highest high
    anchor_low_date: str = ""
    anchor_high_date: str = ""
    location: str = ""                          # above value / inside value / below value
    obv_slope_20d: float | None = None          # OBV change over 20d / avg volume (accumulation > 0)
    cmf20: float | None = None                  # Chaikin money flow, -1..1
    up_down_vol_ratio_10d: float | None = None  # volume on up days / volume on down days
    bins: list = field(default_factory=list)    # [(price, volume)] for charts

    def to_dict(self):
        d = asdict(self)
        d.pop("bins", None)
        return d


def _profile(bars: pd.DataFrame, nbins: int) -> tuple[np.ndarray, np.ndarray]:
    lo, hi = float(bars["low"].min()), float(bars["high"].max())
    edges = np.linspace(lo, hi, nbins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    vol = np.zeros(nbins)
    for l, h, v in bars[["low", "high", "volume"]].itertuples(index=False):
        if v <= 0:
            continue
        if h <= l:
            vol[min(np.searchsorted(edges, l, side="right") - 1, nbins - 1)] += v
            continue
        overlap = np.clip(np.minimum(edges[1:], h) - np.maximum(edges[:-1], l), 0, None)
        vol += v * overlap / (h - l)
    return centers, vol


def _value_area(centers, vol, pct):
    i = int(np.argmax(vol))
    lo = hi = i
    total, acc = vol.sum(), vol[i]
    while acc < pct * total and (lo > 0 or hi < len(vol) - 1):
        down = vol[lo - 1] if lo > 0 else -1
        up = vol[hi + 1] if hi < len(vol) - 1 else -1
        if up >= down:
            hi += 1
            acc += vol[hi]
        else:
            lo -= 1
            acc += vol[lo]
    return float(centers[i]), float(centers[hi]), float(centers[lo])


def _nodes(centers, vol, spot, k=3):
    s = pd.Series(vol).rolling(3, center=True, min_periods=1).mean().to_numpy()
    peaks = [i for i in range(1, len(s) - 1) if s[i] >= s[i - 1] and s[i] >= s[i + 1] and s[i] > np.median(s)]
    troughs = [i for i in range(1, len(s) - 1) if s[i] <= s[i - 1] and s[i] <= s[i + 1] and s[i] < np.median(s)]
    peaks = sorted(peaks, key=lambda i: -s[i])[: k * 2]
    troughs = sorted(troughs, key=lambda i: s[i])[: k * 2]
    near = lambda idx: sorted((round(float(centers[i]), 2) for i in idx), key=lambda p: abs(p - spot))[:k]
    return near(peaks), near(troughs)


def _vwap(b: pd.DataFrame) -> float | None:
    tp = (b["high"] + b["low"] + b["close"]) / 3
    v = b["volume"].sum()
    return float((tp * b["volume"]).sum() / v) if v > 0 else None


def compute_volume_profile(intraday: pd.DataFrame | None, daily: pd.DataFrame | None, spot: float,
                           nbins: int = 60, va_pct: float = 0.70) -> VolumeProfile:
    vp = VolumeProfile()
    bars = intraday if intraday is not None and len(intraday) >= 50 else None
    if bars is not None:
        vp.source = intraday.attrs.get("source", "intraday")
    elif daily is not None and len(daily) >= 20:
        bars = daily.tail(60)
        vp.source = "daily bars (coarse; intraday unavailable)"
    if bars is None:
        return vp
    bars = bars[bars["volume"] > 0].astype(float)
    vp.start, vp.end = str(bars.index[0])[:16], str(bars.index[-1])[:16]
    centers, vol = _profile(bars, nbins)
    vp.poc, vp.vah, vp.val = _value_area(centers, vol, va_pct)
    vp.hvn, vp.lvn = _nodes(centers, vol, spot)
    vp.bins = [(round(float(c), 2), float(v)) for c, v in zip(centers, vol)]
    vp.vwap_window = _vwap(bars)
    if "daily" not in vp.source:
        last_day = bars.index[-1].date()
        vp.vwap_session = _vwap(bars[bars.index.date == last_day])
    i_lo, i_hi = bars["low"].idxmin(), bars["high"].idxmax()
    vp.avwap_from_low, vp.anchor_low_date = _vwap(bars.loc[i_lo:]), str(i_lo)[:10]
    vp.avwap_from_high, vp.anchor_high_date = _vwap(bars.loc[i_hi:]), str(i_hi)[:10]
    vp.location = "above value" if spot > vp.vah else "below value" if spot < vp.val else "inside value"

    if daily is not None and len(daily) >= 21:
        d = daily.astype(float).tail(60)
        direction = np.sign(d["close"].diff()).fillna(0)
        obv = (direction * d["volume"]).cumsum()
        avg = d["volume"].tail(20).mean()
        vp.obv_slope_20d = float((obv.iloc[-1] - obv.iloc[-21]) / avg) if avg > 0 else None
        rng = (d["high"] - d["low"]).replace(0, np.nan)
        mfm = ((d["close"] - d["low"]) - (d["high"] - d["close"])) / rng
        t = d.tail(20)
        vp.cmf20 = float((mfm.tail(20).fillna(0) * t["volume"]).sum() / t["volume"].sum()) if t["volume"].sum() else None
        l10 = d.tail(10)
        dirs = direction.tail(10)
        up, dn = l10["volume"][dirs > 0].sum(), l10["volume"][dirs < 0].sum()
        vp.up_down_vol_ratio_10d = float(up / dn) if dn > 0 else None
    return vp
