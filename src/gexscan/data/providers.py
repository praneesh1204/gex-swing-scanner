"""Provider registry, capability report and a cached, rate-limited HTTP client.

Every provider is optional except the keyless core (CBOE, yfinance, FRED CSV). Missing keys degrade
gracefully: the signal that needed the provider becomes "unknown" and says so. Every fetch returns
a `Fetched` with its timestamp and whether it came from cache, so outputs can show freshness.

Keys are read from the environment only. They're never put in cache keys, file names or logs.
OpenBB (AGPL-3.0) is supported as an optional import and is never vendored.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

from ..logutil import redact

log = logging.getLogger(__name__)
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
SECRET_PARAMS = {"apikey", "api_key", "token", "key", "access_token", "api_token", "secret"}


@dataclass
class ProviderSpec:
    name: str
    category: str               # options / prices / macro / events / flow / news / broker
    env: list = field(default_factory=list)   # env vars needed (empty = keyless)
    kind: str = "free"          # free / free-key / paid
    min_interval_s: float = 0.0
    daily_cap: int | None = None
    wired: bool = False         # True if gexscan calls it today; False = listed, not integrated yet
    license: str = ""           # data terms / code licence note
    notes: str = ""


REGISTRY: dict[str, ProviderSpec] = {p.name: p for p in [
    # keyless core
    ProviderSpec("cboe", "options", [], "free", 0.5, None, True, "CBOE delayed data: personal use",
                 "chains + greeks + OI (prior day), VIX family history CSVs, daily put/call statistics"),
    ProviderSpec("yfinance", "prices", [], "free", 0.3, None, True, "Yahoo terms: personal use",
                 "daily/intraday bars, ^VIX ^VIX3M ^VVIX, earnings dates, dividends"),
    ProviderSpec("fred", "macro", [], "free", 0.5, None, True, "public domain (most series)",
                 "Fed funds, 2y/10y, curve, CPI index, WTI; CPI release calendar"),
    ProviderSpec("sec_edgar", "events", ["SEC_USER_AGENT"], "free", 0.15, None, False, "public",
                 "8-K/10-Q filings feed; needs a 'App contact@email' user agent"),
    ProviderSpec("google_news", "news", [], "free", 1.0, None, True, "headlines only", "RSS headlines"),
    ProviderSpec("gdelt", "news", [], "free", 6.0, None, False, "open", "global news tone; 429s fast"),
    ProviderSpec("stocktwits", "news", [], "free", 2.0, None, False, "terms: personal use", "retail sentiment"),
    ProviderSpec("stooq", "prices", [], "free", 1.0, None, False, "personal use", "EOD bars fallback"),
    # free with key
    ProviderSpec("flashalpha", "flow", ["FLASHALPHA_API_KEY"], "free-key", 1.0, 5, True, "terms of service",
                 "GEX/walls cross-check (free plan = 5 calls/day)"),
    ProviderSpec("finnhub", "events", ["FINNHUB_API_KEY"], "free-key", 1.1, None, True, "terms of service",
                 "earnings calendar, company news"),
    ProviderSpec("fmp", "events", ["FMP_API_KEY"], "free-key", 1.0, 250, True, "terms of service",
                 "earnings calendar, dividends"),
    ProviderSpec("alphavantage", "news", ["ALPHAVANTAGE_API_KEY"], "free-key", 12.5, 25, True, "terms of service",
                 "news sentiment"),
    ProviderSpec("fred_api", "macro", ["FRED_API_KEY"], "free-key", 0.5, None, True, "public domain",
                 "release dates via API (CSV + calendar page work without it)"),
    ProviderSpec("tradier", "options", ["TRADIER_TOKEN"], "free-key", 0.5, None, False, "sandbox: delayed",
                 "chains with greeks (sandbox)"),
    ProviderSpec("marketdata", "options", ["MARKETDATA_TOKEN"], "free-key", 1.0, 100, False, "terms of service",
                 "chains; keyless returns 203 for a few symbols"),
    ProviderSpec("alpaca", "options", ["ALPACA_API_KEY", "ALPACA_API_SECRET"], "free-key", 0.4, None, False,
                 "terms of service", "indicative option quotes (market data only; no trading endpoints used)"),
    ProviderSpec("twelvedata", "prices", ["TWELVEDATA_API_KEY"], "free-key", 8.0, 800, False, "terms of service", "bars"),
    ProviderSpec("tiingo", "prices", ["TIINGO_API_KEY"], "free-key", 1.0, 1000, False, "terms of service", "EOD bars, news"),
    ProviderSpec("eodhd", "prices", ["EODHD_API_KEY"], "free-key", 1.0, 20, False, "terms of service", "EOD bars"),
    ProviderSpec("nasdaq_data_link", "macro", ["NASDAQ_DATA_LINK_API_KEY"], "free-key", 1.0, None, False,
                 "dataset-specific", "macro datasets"),
    ProviderSpec("marketaux", "news", ["MARKETAUX_API_KEY"], "free-key", 1.0, 100, False, "terms of service", "news + entity sentiment"),
    ProviderSpec("newsapi", "news", ["NEWSAPI_KEY"], "free-key", 1.0, 100, False, "terms of service", "headlines"),
    ProviderSpec("eulerpool", "events", ["EULERPOOL_API_KEY"], "free-key", 1.0, None, False, "terms of service", "fundamentals"),
    ProviderSpec("massive", "prices", ["MASSIVE_API_KEY"], "free-key", 12.0, None, False, "terms of service",
                 "Massive (ex-Polygon) aggregates"),
    # paid / account
    ProviderSpec("unusual_whales", "flow", ["UNUSUAL_WHALES_API_KEY"], "paid", 1.0, None, True, "subscription",
                 "real trade-side flow (at-ask/at-bid), replaces the free proxies when present"),
    ProviderSpec("bigdata", "news", ["BIGDATA_API_KEY"], "paid", 1.0, None, False, "subscription", "news/event enrichment"),
    ProviderSpec("ibkr_cp", "broker", ["IBKR_CP_BASE_URL"], "account", 0.2, None, True, "your account",
                 "READ-ONLY positions + snapshots via the local Client Portal gateway (GET allow-list)"),
]}


def has_keys(name: str) -> bool:
    spec = REGISTRY[name]
    return all(os.environ.get(e) for e in spec.env)


def capabilities() -> list[dict]:
    """Which providers are usable right now. Reports key *presence* only, never values."""
    rows = []
    for p in REGISTRY.values():
        ready = has_keys(p.name)
        if p.wired:
            status = "ready" if ready else "key missing"
        elif not p.env:
            status = "keyless (not integrated)"
        else:
            status = "key present (not integrated)" if ready else "listed (not integrated)"
        rows.append({"provider": p.name, "category": p.category, "kind": p.kind, "keys": ", ".join(p.env) or "none",
                     "key_present": ready, "wired": p.wired, "status": status, "notes": p.notes})
    return rows


@dataclass
class Fetched:
    data: Any
    fetched_at: str             # ISO UTC
    source: str
    from_cache: bool = False
    stale: bool = False         # served from an expired cache entry because the live call failed
    status: int | None = None

    def freshness(self) -> str:
        tag = " (stale cache)" if self.stale else " (cached)" if self.from_cache else ""
        return f"{self.source} @ {self.fetched_at[:16]}Z{tag}"

    def to_dict(self):
        d = asdict(self)
        d.pop("data", None)
        return d


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class Http:
    """GET-only client with a disk cache, per-provider spacing and a daily cap.

    The cache key hashes the URL plus the non-secret query parameters, so a key never lands on disk.
    A failed live call falls back to the newest cached copy and marks it `stale`."""

    def __init__(self, cache_dir: str | Path = "state/cache/http", limits: dict | None = None, offline: bool = False):
        self.cache_dir = Path(cache_dir)
        self.limits = limits or {}
        self.offline = offline
        self._last: dict[str, float] = {}
        self._quota_path = self.cache_dir / "_quota.json"

    # ---- rate limiting ----------------------------------------------------------------------------
    def _limit(self, provider: str) -> tuple[float, int | None]:
        spec = REGISTRY.get(provider)
        o = self.limits.get(provider, {})
        return (float(o.get("min_interval_s", spec.min_interval_s if spec else 0.5)),
                o.get("daily_cap", spec.daily_cap if spec else None))

    def _quota(self) -> dict:
        try:
            q = json.loads(self._quota_path.read_text())
        except Exception:
            q = {}
        today = dt.date.today().isoformat()
        return q if q.get("date") == today else {"date": today, "used": {}}

    def _take(self, provider: str) -> bool:
        gap, cap = self._limit(provider)
        q = self._quota()
        used = q["used"].get(provider, 0)
        if cap is not None and used >= cap:
            log.warning("%s: daily cap %s reached, using cache only", provider, cap)
            return False
        wait = gap - (time.monotonic() - self._last.get(provider, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last[provider] = time.monotonic()
        q["used"][provider] = used + 1
        self._quota_path.parent.mkdir(parents=True, exist_ok=True)
        self._quota_path.write_text(json.dumps(q))
        return True

    # ---- cache --------------------------------------------------------------------------------------
    def _key(self, url: str, params: dict | None) -> str:
        safe = {k: v for k, v in sorted((params or {}).items()) if k.lower() not in SECRET_PARAMS}
        return hashlib.sha1((url + "?" + json.dumps(safe, sort_keys=True, default=str)).encode()).hexdigest()

    def _paths(self, provider: str, key: str) -> tuple[Path, Path]:
        d = self.cache_dir / provider
        return d / f"{key}.body", d / f"{key}.meta.json"

    def _read_cache(self, provider: str, key: str):
        body, meta = self._paths(provider, key)
        if not body.exists() or not meta.exists():
            return None, None
        try:
            return body.read_bytes(), json.loads(meta.read_text())
        except Exception:
            return None, None

    def _write_cache(self, provider: str, key: str, content: bytes, status: int) -> str:
        body, meta = self._paths(provider, key)
        body.parent.mkdir(parents=True, exist_ok=True)
        ts = _now()
        body.write_bytes(content)
        meta.write_text(json.dumps({"fetched_at": ts, "status": status}))
        return ts

    # ---- fetch -------------------------------------------------------------------------------------
    def headers_for(self, provider: str) -> dict:
        if provider in ("fred", "fred_api"):
            return {}                                   # FRED rejects browser-like agents on the CSV endpoint
        if provider == "sec_edgar":
            return {"User-Agent": os.environ.get("SEC_USER_AGENT", "gexscan research contact@example.com")}
        if provider in ("stocktwits", "gdelt", "google_news"):
            return {"User-Agent": BROWSER_UA}
        return {"User-Agent": "Mozilla/5.0 (gexscan; personal research)"}

    def get(self, provider: str, url: str, params: dict | None = None, ttl_s: float = 3600, as_json: bool = True,
            headers: dict | None = None, timeout: float = 20, retries: int = 1, backoff_s: float = 5.0) -> Fetched | None:
        key = self._key(url, params)
        content, meta = self._read_cache(provider, key)
        if content is not None:
            age = (dt.datetime.now(dt.timezone.utc) -
                   dt.datetime.fromisoformat(meta["fetched_at"]).replace(tzinfo=dt.timezone.utc)).total_seconds()
            if age <= ttl_s or self.offline:
                return Fetched(self._decode(content, as_json), meta["fetched_at"], provider, True, age > ttl_s, meta.get("status"))
        if self.offline:
            return None
        h = {**self.headers_for(provider), **(headers or {})}
        last_err = None
        for attempt in range(retries + 1):
            if not self._take(provider):
                break
            try:
                r = requests.get(url, params=params, headers=h, timeout=timeout)
                if 200 <= r.status_code < 300:
                    data = self._decode(r.content, as_json)
                    ts = self._write_cache(provider, key, r.content, r.status_code)
                    return Fetched(data, ts, provider, False, False, r.status_code)
                last_err = f"HTTP {r.status_code}"
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(backoff_s * (attempt + 1))
                    continue
                break
            except (requests.RequestException, ValueError) as e:
                last_err = str(e)
                if attempt < retries:
                    time.sleep(backoff_s)
        log.info("%s: %s (%s)", provider, redact(url), redact(str(last_err)))
        if content is not None:
            return Fetched(self._decode(content, as_json), meta["fetched_at"], provider, True, True, meta.get("status"))
        return None

    @staticmethod
    def _decode(content: bytes, as_json: bool):
        if not as_json:
            return content.decode("utf-8", errors="replace")
        return json.loads(content)


def make_http(cfg: dict, state_dir: str | Path | None = None, offline: bool = False) -> Http:
    pc = cfg.get("providers", {})
    cache = pc.get("cache_dir") or Path(state_dir or cfg["output"]["state_dir"]) / "cache" / "http"
    return Http(cache, pc.get("limits"), offline=offline)


def openbb():
    """Optional OpenBB Platform handle (AGPL-3.0, installed separately). None if absent or disabled."""
    try:
        from openbb import obb  # type: ignore
        return obb
    except Exception:
        return None
