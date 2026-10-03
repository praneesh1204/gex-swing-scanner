"""Market-wide regime numbers: VIX term structure, VIX level, VVIX, rates, oil, put/call, macro calendar.

Pure functions of MarketData (data/macro.py). States are assigned in ruleset.py from the signal sheet.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from ..data.macro import MarketData, next_events


@dataclass
class MarketRegime:
    as_of: str
    vix: float | None = None
    vix3m: float | None = None
    vix9d: float | None = None
    vvix: float | None = None
    skew_index: float | None = None
    ts_ratio: float | None = None          # VIX / VIX3M
    ts_ratio_prev: float | None = None     # previous session, for flip alerts
    vix9d_ratio: float | None = None       # VIX9D / VIX: short-dated stress
    vvix_5d_pct: float | None = None
    vix_pct_1y: float | None = None        # percentile of today's VIX in the past year
    fed_funds: float | None = None
    y2: float | None = None
    y10: float | None = None
    curve_10y2y: float | None = None
    y10_20d_bp: float | None = None        # change over ~20 sessions, basis points
    wti: float | None = None
    wti_20d_pct: float | None = None
    cpi_yoy: float | None = None
    put_call: dict = field(default_factory=dict)
    events: list = field(default_factory=list)     # [{event, date, days}]
    sources: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["events"] = [{**e, "date": str(e["date"])} for e in self.events]
        return d


def _last(s: pd.Series | None, back: int = 0) -> float | None:
    if s is None or len(s) <= back:
        return None
    return float(s.iloc[-1 - back])


def _chg(s: pd.Series | None, n: int) -> float | None:
    if s is None or len(s) <= n:
        return None
    return float(s.iloc[-1] - s.iloc[-1 - n])


def market_regime(md: MarketData, today: dt.date, horizon_days: int = 60) -> MarketRegime:
    m = MarketRegime(as_of=str(md.as_of), sources=dict(md.sources), errors=list(md.errors))
    S = md.series
    m.vix, m.vix3m, m.vix9d = _last(S.get("VIX")), _last(S.get("VIX3M")), _last(S.get("VIX9D"))
    m.vvix, m.skew_index = _last(S.get("VVIX")), _last(S.get("SKEW"))
    if m.vix and m.vix3m:
        m.ts_ratio = round(m.vix / m.vix3m, 3)
        a, b = S["VIX"], S["VIX3M"]
        j = a.to_frame("v").join(b.to_frame("v3"), how="inner")
        if len(j) >= 2:
            m.ts_ratio_prev = round(float(j["v"].iloc[-2] / j["v3"].iloc[-2]), 3)
    if m.vix9d and m.vix:
        m.vix9d_ratio = round(m.vix9d / m.vix, 3)
    vv = S.get("VVIX")
    if vv is not None and len(vv) > 5 and float(vv.iloc[-6]) > 0:
        m.vvix_5d_pct = round((float(vv.iloc[-1]) / float(vv.iloc[-6]) - 1) * 100, 1)
    v = S.get("VIX")
    if v is not None and len(v) >= 60:
        h = v.tail(252)
        m.vix_pct_1y = round(float((h < h.iloc[-1]).mean() * 100), 1)

    F = md.fred
    m.fed_funds, m.y2, m.y10 = _last(F.get("fed_funds")), _last(F.get("y2")), _last(F.get("y10"))
    m.curve_10y2y = _last(F.get("curve_10y2y"))
    if m.curve_10y2y is None and m.y10 is not None and m.y2 is not None:
        m.curve_10y2y = round(m.y10 - m.y2, 2)
    c10 = _chg(F.get("y10"), 20)
    m.y10_20d_bp = round(c10 * 100, 0) if c10 is not None else None
    w = F.get("wti")
    m.wti = _last(w)
    if w is not None and len(w) > 20 and float(w.iloc[-21]) > 0:
        m.wti_20d_pct = round((float(w.iloc[-1]) / float(w.iloc[-21]) - 1) * 100, 1)
    cpi = F.get("cpi_index")
    if cpi is not None and len(cpi) > 12 and float(cpi.iloc[-13]) > 0:
        m.cpi_yoy = round((float(cpi.iloc[-1]) / float(cpi.iloc[-13]) - 1) * 100, 2)
    m.put_call = dict(md.put_call or {})
    m.events = next_events(md, today, horizon_days)
    return m


def rate_regime(m: MarketRegime) -> str:
    """Plain-words rates/oil context used by the second-order rationale."""
    parts = []
    if m.fed_funds is not None:
        parts.append(f"Fed funds {m.fed_funds:.2f}%")
    if m.y2 is not None and m.y10 is not None:
        parts.append(f"2y {m.y2:.2f}% / 10y {m.y10:.2f}%")
    if m.curve_10y2y is not None:
        parts.append(f"curve {m.curve_10y2y:+.2f} ({'inverted' if m.curve_10y2y < 0 else 'positive'})")
    if m.y10_20d_bp is not None and abs(m.y10_20d_bp) >= 15:
        parts.append(f"10y {'up' if m.y10_20d_bp > 0 else 'down'} {abs(m.y10_20d_bp):.0f}bp in 20 sessions")
    if m.wti is not None:
        oil = f"WTI {m.wti:.1f}"
        if m.wti_20d_pct is not None:
            oil += f" ({m.wti_20d_pct:+.1f}% 20d)"
        parts.append(oil)
    if m.cpi_yoy is not None:
        parts.append(f"CPI {m.cpi_yoy:.1f}% y/y")
    return "; ".join(parts)


def correlation(hist: pd.DataFrame | None, bench: pd.DataFrame | None, n: int = 60) -> float | None:
    """60-session correlation of daily returns with the benchmark (index-driven vs idiosyncratic)."""
    if hist is None or bench is None or len(hist) < n + 1 or len(bench) < n + 1:
        return None
    a = np.log(hist["close"].astype(float)).diff()
    b = np.log(bench["close"].astype(float)).diff()
    j = pd.concat([a, b], axis=1, join="inner").dropna().tail(n)
    if len(j) < n // 2:
        return None
    return round(float(j.iloc[:, 0].corr(j.iloc[:, 1])), 2)
