"""Market-wide inputs: VIX family, rates/oil (FRED), CBOE put/call statistics, FOMC and CPI dates.

Sources (all keyless):
  * VIX, VIX3M, VIX9D, VVIX, SKEW: CBOE daily history CSVs, newest print from the CBOE delayed quote,
    yfinance (^VIX ...) as a fallback.
  * FRED CSV (fredgraph.csv): DFF, DGS2, DGS10, T10Y2Y, CPIAUCSL, DCOILWTICO.
  * CPI release dates: config list, else FRED's release calendar page (release 10 = CPI), else the
    FRED API when FRED_API_KEY is set.
  * FOMC decision days: config list (the Fed publishes them a year ahead).
Everything is cached and timestamped. Series are cut at `today`, so a past run never sees later data.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .providers import Http, make_http

log = logging.getLogger(__name__)
CBOE_HIST = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
CBOE_QUOTE = "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/_{name}.json"
CBOE_PC = "https://cdn.cboe.com/data/us/options/market_statistics/daily/{date}_daily_options"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_CAL = "https://fred.stlouisfed.org/releases/calendar"
FRED_API_DATES = "https://api.stlouisfed.org/fred/release/dates"
VIX_NAMES = ("VIX", "VIX3M", "VIX9D", "VVIX", "SKEW")
YF_TICKERS = {"VIX": "^VIX", "VIX3M": "^VIX3M", "VIX9D": "^VIX9D", "VVIX": "^VVIX", "SKEW": "^SKEW"}


@dataclass
class MarketData:
    as_of: dt.date
    series: dict = field(default_factory=dict)      # name -> pd.Series (daily closes, date index)
    fred: dict = field(default_factory=dict)        # label -> pd.Series
    put_call: dict = field(default_factory=dict)    # {"date":..., "total":..., "equity":..., "index":...}
    fomc: list = field(default_factory=list)        # [dt.date]
    cpi: list = field(default_factory=list)         # [dt.date]
    sources: dict = field(default_factory=dict)     # name -> freshness text
    errors: list = field(default_factory=list)

    def last(self, name: str) -> float | None:
        s = self.series.get(name)
        return float(s.iloc[-1]) if s is not None and len(s) else None


def _parse_cboe_csv(text: str, name: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().upper() for c in df.columns]
    col = "CLOSE" if "CLOSE" in df.columns else name.upper() if name.upper() in df.columns else df.columns[-1]
    idx = pd.to_datetime(df["DATE"], format="%m/%d/%Y", errors="coerce")
    s = pd.Series(pd.to_numeric(df[col], errors="coerce").to_numpy(), index=idx).dropna()
    return s[~s.index.isna()].sort_index()


def _parse_fred_csv(text: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(text))
    s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").to_numpy(), index=pd.to_datetime(df.iloc[:, 0]))
    return s.dropna().sort_index()


def _cut(s: pd.Series, today: dt.date) -> pd.Series:
    return s[s.index <= pd.Timestamp(today)] if s is not None and len(s) else s


def parse_cpi_calendar(html: str, year: int) -> list[dt.date]:
    out = set()
    for m in re.finditer(rf"([A-Z][a-z]+ \d{{1,2}}, {year})", html):
        try:
            out.add(dt.datetime.strptime(m.group(1), "%B %d, %Y").date())
        except ValueError:
            continue
    return sorted(out)


class MarketSource:
    def __init__(self, cfg: dict, http: Http | None = None, fixtures: Path | None = None, today: dt.date | None = None):
        self.cfg, self.fixtures = cfg, fixtures
        self.today = today or dt.date.today()
        self.http = http or make_http(cfg, offline=bool(fixtures))
        self.mcfg = cfg.get("macro", {})

    # ---- fixtures ---------------------------------------------------------------------------------
    def _fixture(self) -> MarketData:
        md = MarketData(self.today)
        p = self.fixtures / "market.json"
        if not p.exists():
            md.errors.append("no market.json fixture")
            return md
        raw = json.loads(p.read_text())
        for name, rows in raw.get("series", {}).items():
            md.series[name] = _cut(pd.Series({pd.Timestamp(d): float(v) for d, v in rows.items()}).sort_index(), self.today)
            md.sources[name] = "fixture"
        for name, rows in raw.get("fred", {}).items():
            md.fred[name] = _cut(pd.Series({pd.Timestamp(d): float(v) for d, v in rows.items()}).sort_index(), self.today)
            md.sources[f"fred:{name}"] = "fixture"
        md.put_call = raw.get("put_call", {})
        md.fomc = [dt.date.fromisoformat(d) for d in raw.get("fomc", self.mcfg.get("fomc_dates", []))]
        md.cpi = [dt.date.fromisoformat(d) for d in raw.get("cpi", [])]
        md.sources["put_call"] = md.sources["calendar"] = "fixture"
        return md

    # ---- live ---------------------------------------------------------------------------------------
    def load(self) -> MarketData:
        if self.fixtures:
            return self._fixture()
        md = MarketData(self.today)
        for name in VIX_NAMES:
            s, src = self._vix_series(name)
            if s is not None and len(s):
                md.series[name] = _cut(s, self.today)
                md.sources[name] = src
            else:
                md.errors.append(f"{name}: unavailable")
        for label, sid in (self.mcfg.get("fred_series") or {}).items():
            f = self.http.get("fred", FRED_CSV, {"id": sid}, ttl_s=6 * 3600, as_json=False)
            if f is None:
                md.errors.append(f"FRED {sid}: unavailable")
                continue
            try:
                md.fred[label] = _cut(_parse_fred_csv(f.data), self.today)
                md.sources[f"fred:{label}"] = f.freshness()
            except Exception as e:
                md.errors.append(f"FRED {sid}: parse failed ({e})")
        md.put_call, md.sources["put_call"] = self._put_call()
        md.fomc = [dt.date.fromisoformat(d) for d in self.mcfg.get("fomc_dates", [])]
        md.cpi, md.sources["calendar"] = self._cpi_dates()
        return md

    def _vix_series(self, name: str) -> tuple[pd.Series | None, str]:
        f = self.http.get("cboe", CBOE_HIST.format(name=name), ttl_s=6 * 3600, as_json=False)
        s, src = None, ""
        if f is not None:
            try:
                s, src = _parse_cboe_csv(f.data, name), f.freshness()
            except Exception as e:
                log.info("CBOE %s csv parse failed: %s", name, e)
        if s is None or not len(s):
            try:
                import yfinance as yf

                h = yf.Ticker(YF_TICKERS[name]).history(period="2y", interval="1d")
                if h is not None and not h.empty:
                    s = pd.Series(h["Close"].to_numpy(), index=pd.to_datetime(h.index).tz_localize(None).normalize())
                    src = f"yfinance {YF_TICKERS[name]} @ {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z"
            except Exception as e:
                log.info("yfinance %s failed: %s", name, e)
        if s is None or not len(s):
            return None, ""
        q = self.http.get("cboe", CBOE_QUOTE.format(name=name), ttl_s=600)
        try:
            d = (q.data or {}).get("data", {}) if q else {}
            px, ts = d.get("current_price"), str(d.get("last_trade_time", ""))[:10]
            if px and ts:
                day = pd.Timestamp(ts)
                if day.date() <= self.today and (day > s.index[-1]):
                    s = pd.concat([s, pd.Series([float(px)], index=[day])])
                    src += f"; latest {px} from CBOE quote ({d.get('last_trade_time')} ET, delayed)"
        except Exception:
            pass
        return s, src

    def _put_call(self) -> tuple[dict, str]:
        for back in range(self.mcfg.get("pc_lookback_days", 5) + 1):
            day = self.today - dt.timedelta(days=back)
            if day.weekday() >= 5:
                continue
            f = self.http.get("cboe", CBOE_PC.format(date=day.isoformat()), ttl_s=12 * 3600, retries=0)
            if f is None or not isinstance(f.data, dict):
                continue
            out = {"date": day.isoformat()}
            for r in f.data.get("ratios", []):
                nm = str(r.get("name", "")).upper()
                try:
                    v = float(r.get("value"))
                except (TypeError, ValueError):
                    continue
                key = "total" if nm.startswith("TOTAL") else "equity" if nm.startswith("EQUITY") else \
                      "index" if nm.startswith("INDEX") else None
                if key:
                    out[key] = v
            if len(out) > 1:
                return out, f"CBOE daily market statistics {day}"
        return {}, "unavailable"

    def _cpi_dates(self) -> tuple[list[dt.date], str]:
        if self.mcfg.get("cpi_dates"):
            return sorted(dt.date.fromisoformat(str(d)) for d in self.mcfg["cpi_dates"]), "config"
        years = [self.today.year] + ([self.today.year + 1] if self.today.month >= 11 else [])
        dates, src = [], ""
        for y in years:
            f = self.http.get("fred", FRED_CAL, {"rid": 10, "y": y}, ttl_s=7 * 86400, as_json=False)
            if f is not None:
                dates += parse_cpi_calendar(f.data, y)
                src = f"FRED release calendar (BLS CPI) {f.freshness()}"
        if not dates and os.environ.get("FRED_API_KEY"):
            f = self.http.get("fred_api", FRED_API_DATES, {"release_id": 10, "file_type": "json",
                              "include_release_dates_with_no_data": "true", "api_key": os.environ["FRED_API_KEY"],
                              "realtime_start": f"{self.today.year}-01-01"}, ttl_s=7 * 86400)
            if f is not None:
                dates = [dt.date.fromisoformat(r["date"]) for r in f.data.get("release_dates", [])]
                src = "FRED API release dates"
        return sorted(set(dates)), src or "unavailable"


def next_events(md: MarketData, today: dt.date, horizon_days: int = 60) -> list[dict]:
    """Upcoming FOMC / CPI events within the horizon, nearest first."""
    out = [{"event": "FOMC", "date": d, "days": (d - today).days} for d in md.fomc if 0 <= (d - today).days <= horizon_days]
    out += [{"event": "CPI", "date": d, "days": (d - today).days} for d in md.cpi if 0 <= (d - today).days <= horizon_days]
    return sorted(out, key=lambda e: e["days"])
