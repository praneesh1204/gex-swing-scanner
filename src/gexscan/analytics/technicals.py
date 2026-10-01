"""Daily technical indicators: trend, momentum, ATR, RSI, 52-week structure, squeeze, breakout, RS."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass
class Technicals:
    close: float | None = None
    sma10: float | None = None
    sma20: float | None = None
    pct_vs_sma20: float | None = None
    chg5_pct: float | None = None
    chg10_pct: float | None = None
    rv20: float | None = None          # annualised close-to-close, decimal
    high20: float | None = None
    low20: float | None = None
    last_day_pct: float | None = None
    vol_vs_avg: float | None = None    # last volume / 20d avg
    sma50: float | None = None
    sma200: float | None = None
    rsi14: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None       # ATR as % of close
    high52: float | None = None
    low52: float | None = None
    pct_from_high52: float | None = None
    macd_hist: float | None = None
    bb_width_pct: float | None = None  # Bollinger(20,2) width as % of price
    squeeze: bool = False              # BB width in the lowest 20% of the past ~6 months
    breakout: str = ""                 # "20d high on volume" / "20d low on volume" / ""
    rs_vs_bench_20d: float | None = None   # 20d return minus benchmark 20d return (pct points)
    trend_label: str = "n/a"           # uptrend / downtrend / range
    bars: int = 0
    source: str = ""

    def to_dict(self):
        return asdict(self)


def _rsi(c: pd.Series, n: int = 14) -> float | None:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    v = 100 - 100 / (1 + rs.iloc[-1]) if not np.isnan(rs.iloc[-1]) else 100.0
    return float(v)


def _atr(h: pd.DataFrame, n: int = 14) -> float:
    pc = h["close"].shift(1)
    tr = pd.concat([h["high"] - h["low"], (h["high"] - pc).abs(), (h["low"] - pc).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


def compute_technicals(hist: pd.DataFrame, bench: pd.DataFrame | None = None) -> Technicals:
    if hist is None or hist.empty or len(hist) < 21:
        return Technicals()
    h = hist.astype(float)
    c = h["close"]
    r = np.log(c / c.shift(1)).dropna()
    close = float(c.iloc[-1])
    sma10, sma20 = float(c.tail(10).mean()), float(c.tail(20).mean())
    vol = h["volume"]
    t = Technicals(
        close=close, sma10=sma10, sma20=sma20,
        pct_vs_sma20=(close / sma20 - 1) * 100,
        chg5_pct=(close / float(c.iloc[-6]) - 1) * 100,
        chg10_pct=(close / float(c.iloc[-11]) - 1) * 100,
        rv20=float(r.tail(20).std() * np.sqrt(252)),
        high20=float(c.tail(20).max()), low20=float(c.tail(20).min()),
        last_day_pct=(close / float(c.iloc[-2]) - 1) * 100,
        vol_vs_avg=float(vol.iloc[-1] / vol.tail(20).mean()) if vol.tail(20).mean() > 0 else None,
        bars=len(h), source=hist.attrs.get("source", ""),
    )
    t.sma50 = float(c.tail(50).mean()) if len(c) >= 50 else None
    t.sma200 = float(c.tail(200).mean()) if len(c) >= 200 else None
    t.rsi14 = _rsi(c)
    t.atr14 = _atr(h)
    t.atr_pct = t.atr14 / close * 100
    t.high52, t.low52 = float(h["high"].tail(252).max()), float(h["low"].tail(252).min())
    t.pct_from_high52 = (close / t.high52 - 1) * 100
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    t.macd_hist = float((macd - macd.ewm(span=9, adjust=False).mean()).iloc[-1])
    sd20 = c.rolling(20).std()
    bbw = (4 * sd20 / c.rolling(20).mean() * 100).dropna()
    if len(bbw):
        t.bb_width_pct = float(bbw.iloc[-1])
        hist_w = bbw.tail(126)
        t.squeeze = len(hist_w) >= 60 and t.bb_width_pct <= float(hist_w.quantile(0.2))
    prior_hi, prior_lo = float(c.iloc[-21:-1].max()), float(c.iloc[-21:-1].min())
    if (t.vol_vs_avg or 0) >= 1.5:
        if close > prior_hi:
            t.breakout = "20d high on volume"
        elif close < prior_lo:
            t.breakout = "20d low on volume"
    if bench is not None and len(bench) >= 21:
        b = bench["close"].astype(float)
        t.rs_vs_bench_20d = ((close / float(c.iloc[-21]) - 1) - (float(b.iloc[-1]) / float(b.iloc[-21]) - 1)) * 100
    up = close > sma20 and (t.sma50 is None or sma20 > t.sma50)
    dn = close < sma20 and (t.sma50 is None or sma20 < t.sma50)
    t.trend_label = "uptrend" if up else "downtrend" if dn else "range"
    return t

