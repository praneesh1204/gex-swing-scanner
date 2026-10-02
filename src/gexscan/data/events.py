"""Per-name event calendar: next earnings (with BMO/AMC timing), past earnings moves, ex-dividend dates.

Order of sources for the next earnings date: config `events.earnings_overrides` (you know best) ->
yfinance -> Finnhub (FINNHUB_API_KEY) -> FMP (FMP_API_KEY). ETFs have no earnings ("none").

Past moves: for an after-close report the move is close(next session) / close(report day); for a
before-open report it is close(report day) / close(previous session). Unknown timing uses the
two-session window. Only bars up to `today` are used.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .providers import Http, make_http

log = logging.getLogger(__name__)
FINNHUB_CAL = "https://finnhub.io/api/v1/calendar/earnings"
FMP_EARN = "https://financialmodelingprep.com/stable/earnings"


@dataclass
class EventInfo:
    symbol: str
    next_earnings: dt.date | None = None
    timing: str = "unknown"                  # BMO / AMC / unknown
    days_to_earnings: int | None = None
    earnings_source: str = ""
    is_etf: bool = False
    history: list = field(default_factory=list)   # [{date, timing, move_pct}]
    mean_abs_move_pct: float | None = None
    median_abs_move_pct: float | None = None
    max_abs_move_pct: float | None = None
    n_moves: int = 0
    ex_div_date: dt.date | None = None
    dividend: float | None = None            # per share, last declared
    div_source: str = ""

    def to_dict(self):
        return asdict(self)


def _timing(ts: pd.Timestamp) -> str:
    if ts.hour == 0 and ts.minute == 0:
        return "unknown"
    return "AMC" if ts.hour >= 16 else "BMO" if ts.hour < 10 else "unknown"


def earnings_moves(hist: pd.DataFrame, events: list[dict], today: dt.date, n: int = 8) -> list[dict]:
    """Close-to-close reaction for each past report, newest first."""
    if hist is None or hist.empty:
        return []
    c = hist["close"].astype(float)
    c = c[c.index <= pd.Timestamp(today)]
    idx = c.index
    out = []
    for e in sorted(events, key=lambda x: x["date"], reverse=True):
        d = pd.Timestamp(e["date"])
        if d.date() >= today:
            continue
        pos = idx.searchsorted(d)              # first bar on/after the report day
        if pos >= len(idx) or pos == 0:
            continue
        on_day = idx[pos].normalize() == d.normalize()
        t = e.get("timing", "unknown")
        try:
            if t == "AMC" and on_day and pos + 1 < len(idx):
                mv = c.iloc[pos + 1] / c.iloc[pos] - 1
            elif t == "BMO":
                mv = c.iloc[pos] / c.iloc[pos - 1] - 1
            elif pos + 1 < len(idx):
                mv = c.iloc[pos + 1] / c.iloc[pos - 1] - 1
            else:
                continue
        except IndexError:
            continue
        out.append({"date": d.date().isoformat(), "timing": t, "move_pct": round(float(mv) * 100, 2)})
        if len(out) >= n:
            break
    return out


def summarize_moves(info: EventInfo) -> None:
    m = np.abs([h["move_pct"] for h in info.history])
    info.n_moves = len(m)
    if len(m):
        info.mean_abs_move_pct = round(float(m.mean()), 2)
        info.median_abs_move_pct = round(float(np.median(m)), 2)
        info.max_abs_move_pct = round(float(m.max()), 2)


class EventSource:
    def __init__(self, cfg: dict, http: Http | None = None, fixtures: Path | None = None, today: dt.date | None = None):
        self.cfg, self.fixtures = cfg, fixtures
        self.today = today or dt.date.today()
        self.http = http or make_http(cfg, offline=bool(fixtures))
        self.ecfg = cfg.get("events", {})
        self._fx: dict = {}
        if fixtures:
            for name in ("earnings", "earnings_history", "dividends", "etfs"):
                p = fixtures / f"{name}.json"
                self._fx[name] = json.loads(p.read_text()) if p.exists() else {}
        self._cache_dir = Path(cfg["output"]["state_dir"]) / "cache" / "events"

    def get(self, sym: str, hist: pd.DataFrame | None) -> EventInfo:
        info = EventInfo(sym)
        n = self.ecfg.get("history_quarters", 8)
        past = self._fixture(sym, info) if self.fixtures else self._live(sym, info)
        override = (self.ecfg.get("earnings_overrides") or {}).get(sym)
        if override:
            info.next_earnings, info.earnings_source = dt.date.fromisoformat(str(override)), "config override"
        if info.next_earnings:
            info.days_to_earnings = (info.next_earnings - self.today).days
        info.history = earnings_moves(hist, past, self.today, n)
        summarize_moves(info)
        return info

    # ---- fixtures -------------------------------------------------------------------------------------
    def _fixture(self, sym: str, info: EventInfo) -> list[dict]:
        v = self._fx.get("earnings", {}).get(sym)
        if v:
            info.next_earnings, info.earnings_source = dt.date.fromisoformat(v), "fixture"
        info.is_etf = sym in (self._fx.get("etfs") or [])
        d = self._fx.get("dividends", {}).get(sym)
        if d:
            info.ex_div_date, info.dividend, info.div_source = dt.date.fromisoformat(d["ex_date"]), float(d["amount"]), "fixture"
        return [{"date": dt.date.fromisoformat(e["date"]), "timing": e.get("timing", "unknown")}
                for e in self._fx.get("earnings_history", {}).get(sym, [])]

    # ---- live ------------------------------------------------------------------------------------------
    def _cached(self, sym: str) -> dict | None:
        p = self._cache_dir / f"{sym}_{self.today.isoformat()}.json"
        try:
            return json.loads(p.read_text()) if p.exists() else None
        except Exception:
            return None

    def _save(self, sym: str, d: dict) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        (self._cache_dir / f"{sym}_{self.today.isoformat()}.json").write_text(json.dumps(d, default=str))

    def _live(self, sym: str, info: EventInfo) -> list[dict]:
        raw = self._cached(sym)
        if raw is None:
            raw = self._yf(sym)
            self._save(sym, raw)
        info.is_etf = raw.get("quote_type") in ("ETF", "INDEX", "MUTUALFUND")
        if raw.get("next"):
            info.next_earnings = dt.date.fromisoformat(raw["next"])
            info.timing = raw.get("next_timing", "unknown")
            info.earnings_source = "yfinance"
        if raw.get("ex_div"):
            info.ex_div_date = dt.date.fromisoformat(raw["ex_div"])
            info.dividend, info.div_source = raw.get("dividend"), "yfinance"
        if info.next_earnings is None and not info.is_etf:
            self._keyed_next(sym, info)
        return [{"date": dt.date.fromisoformat(e["date"]), "timing": e["timing"]} for e in raw.get("past", [])]

    def _yf(self, sym: str) -> dict:
        out: dict = {"past": []}
        try:
            import yfinance as yf

            t = yf.Ticker(sym)
            try:
                inf = t.info or {}
            except Exception:
                inf = {}
            out["quote_type"] = inf.get("quoteType")
            exd = inf.get("exDividendDate")
            if isinstance(exd, (int, float)) and exd > 0:
                out["ex_div"] = dt.datetime.fromtimestamp(exd, dt.timezone.utc).date().isoformat()
                out["dividend"] = inf.get("lastDividendValue") or (
                    (inf.get("dividendRate") or 0) / 4 if inf.get("dividendRate") else None)
            if out["quote_type"] in ("ETF", "INDEX", "MUTUALFUND"):
                return out
            ed = t.get_earnings_dates(limit=12)
            if ed is not None and not ed.empty:
                ed = ed.iloc[:12]
                fut = []
                for ts, row in ed.iterrows():
                    ts = pd.Timestamp(ts)
                    loc = ts.tz_convert("America/New_York") if ts.tzinfo else ts
                    d, tm = loc.date(), _timing(loc)
                    eps = row.get("Reported EPS") if hasattr(row, "get") else None
                    if d >= self.today and (eps is None or pd.isna(eps)):
                        fut.append((d, tm))
                    elif d < self.today:
                        out["past"].append({"date": d.isoformat(), "timing": tm})
                if fut:
                    d, tm = min(fut)
                    out["next"], out["next_timing"] = d.isoformat(), tm
        except Exception as e:
            log.info("yfinance events for %s failed: %s", sym, e)
        return out

    def _keyed_next(self, sym: str, info: EventInfo) -> None:
        to = (self.today + dt.timedelta(days=120)).isoformat()
        if os.environ.get("FINNHUB_API_KEY"):
            f = self.http.get("finnhub", FINNHUB_CAL, {"symbol": sym, "from": self.today.isoformat(), "to": to,
                                                       "token": os.environ["FINNHUB_API_KEY"]}, ttl_s=12 * 3600)
            rows = (f.data or {}).get("earningsCalendar", []) if f and isinstance(f.data, dict) else []
            ds = sorted((r for r in rows if isinstance(r, dict) and r.get("date")), key=lambda r: r["date"])
            if ds:
                info.next_earnings = dt.date.fromisoformat(ds[0]["date"])
                info.timing = {"amc": "AMC", "bmo": "BMO"}.get(str(ds[0].get("hour", "")).lower(), "unknown")
                info.earnings_source = f"finnhub {f.freshness()}"
                return
        if os.environ.get("FMP_API_KEY"):
            f = self.http.get("fmp", FMP_EARN, {"symbol": sym, "apikey": os.environ["FMP_API_KEY"]}, ttl_s=12 * 3600)
            rows = f.data if f and isinstance(f.data, list) else []
            fut = sorted(r["date"] for r in rows if isinstance(r, dict) and r.get("date", "") >= self.today.isoformat())
            if fut:
                info.next_earnings = dt.date.fromisoformat(fut[0][:10])
                info.earnings_source = f"fmp {f.freshness()}"
