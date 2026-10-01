"""Price history (daily + intraday) and earnings dates, with graceful fallbacks.

Daily bars: yfinance (includes today's session) -> CBOE historical (lags a session).
Intraday bars: yfinance (30m bars cover ~60 days). All volume here is Yahoo consolidated volume;
never mix it with IBKR volume in one ratio.
"""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

log = logging.getLogger(__name__)


def _yf_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    return df.astype(float)


def fetch_history(symbol: str, days: int = 400, cboe_url: str | None = None) -> pd.DataFrame:
    """Daily OHLCV (naive DatetimeIndex). Returns an empty DataFrame when every source fails."""
    df = pd.DataFrame()
    try:
        import yfinance as yf

        df = _yf_frame(yf.Ticker(symbol).history(period=f"{days}d", interval="1d", auto_adjust=False))
        if not df.empty:
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            df.attrs["source"] = "yfinance"
    except Exception as e:  # network / API changes
        log.warning("yfinance history failed for %s: %s", symbol, e)
    if df.empty and cboe_url:
        from .cboe import fetch_daily_history

        df = fetch_daily_history(symbol, cboe_url)
        if not df.empty:
            df = df[df.index >= df.index[-1] - pd.Timedelta(days=days)]
            df.attrs["source"] = "cboe (lags one session)"
    return df


def fetch_intraday(symbol: str, interval: str = "30m", days: int = 30) -> pd.DataFrame:
    """Intraday OHLCV bars in America/New_York time (tz-naive). Empty DataFrame on failure."""
    try:
        import yfinance as yf

        df = _yf_frame(yf.Ticker(symbol).history(period=f"{min(days, 59)}d", interval=interval, prepost=False))
        if df.empty:
            return df
        idx = pd.to_datetime(df.index)
        if idx.tz is not None:
            idx = idx.tz_convert("America/New_York").tz_localize(None)
        df.index = idx
        df.attrs["source"] = f"yfinance {interval}"
        return df
    except Exception as e:
        log.warning("intraday fetch failed for %s: %s", symbol, e)
        return pd.DataFrame()


def fetch_next_earnings(symbol: str, today: dt.date | None = None) -> dt.date | None:
    """Next earnings date if yfinance knows it, else None (treated as 'unknown')."""
    today = today or dt.date.today()
    try:
        import yfinance as yf

        t = yf.Ticker(symbol)
        dates: list[dt.date] = []
        cal = getattr(t, "calendar", None)
        if isinstance(cal, dict):
            for v in cal.get("Earnings Date", []) or []:
                dates.append(pd.Timestamp(v).date())
        try:
            ed = t.get_earnings_dates(limit=8)
            if ed is not None and not ed.empty:
                dates += [pd.Timestamp(i).date() for i in ed.index]
        except Exception:
            pass
        future = sorted(d for d in dates if d >= today)
        return future[0] if future else None
    except Exception as e:
        log.warning("earnings lookup failed for %s: %s", symbol, e)
        return None
