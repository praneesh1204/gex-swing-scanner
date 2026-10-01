"""Probe each data provider with the keys in .env and record what the tier actually returns.

    python scripts/probe_providers.py            # writes docs/probe_results.json
    python scripts/probe_providers.py --symbol NVDA

Read-only GET requests only. Keys are read from the environment / .env and are never
printed or written: every URL is redacted before it is logged. FlashAlpha's free tier
allows 5 requests/day, so this script spends at most 3 of them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UA = "Mozilla/5.0 (gexscan; personal research)"
SECRET_PARAMS = re.compile(r"(apikey|apiKey|token|key)=[^&]+")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def redact(url: str) -> str:
    return SECRET_PARAMS.sub(r"\1=REDACTED", url)


def get(url: str, headers: dict | None = None, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    out: dict = {"url": redact(url)}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            out["status"] = r.status
    except urllib.error.HTTPError as e:
        body, out["status"] = e.read(), e.code
    except Exception as e:  # network, TLS, local gateway down
        out["status"], out["error"] = None, f"{type(e).__name__}: {e}"
        return out
    out["bytes"] = len(body)
    try:
        js = json.loads(body)
        out["shape"] = describe(js)
        if isinstance(js, dict):
            # surface tier / error messages verbatim (they are the point of the probe)
            for k in ("Information", "Note", "Error Message", "error", "message", "detail", "gamma_flip_status"):
                if k in js:
                    out[k] = str(js[k])[:300]
    except ValueError:
        out["text"] = body[:300].decode(errors="replace")
    return out


def describe(js, depth: int = 0):
    """Structure summary: keys and list lengths, no values (values can be large or licensed)."""
    if depth > 2:
        return type(js).__name__
    if isinstance(js, dict):
        return {k: describe(v, depth + 1) for k, v in list(js.items())[:25]}
    if isinstance(js, list):
        return [f"list[{len(js)}]", describe(js[0], depth + 1) if js else None]
    return type(js).__name__


def probe_cboe(sym: str) -> dict:
    base = "https://cdn-api.cboe.com/api/global/delayed_quotes"
    return {
        "chain": get(f"{base}/options/{sym}.json"),
        "quote": get(f"{base}/quotes/{sym}.json"),
        "daily_history": get(f"{base}/charts/historical/{sym}.json"),
        "vix_chain": get(f"{base}/options/_VIX.json"),
    }


def probe_flashalpha(sym: str) -> dict:
    key = os.environ.get("FLASHALPHA_API_KEY")
    if not key:
        return {"skipped": "FLASHALPHA_API_KEY not set"}
    h, b = {"X-Api-Key": key}, "https://lab.flashalpha.com"
    return {
        "account": get(f"{b}/v1/account", h),
        "levels": get(f"{b}/v1/exposure/levels/{sym}", h),
        "exposure_summary_expect_403": get(f"{b}/v1/exposure/summary/{sym}", h),
    }


def probe_alphavantage(sym: str) -> dict:
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not key:
        return {"skipped": "ALPHAVANTAGE_API_KEY not set"}
    b = "https://www.alphavantage.co/query?"
    fns = {
        "daily_compact": f"function=TIME_SERIES_DAILY&symbol={sym}&outputsize=compact",
        "daily_full": f"function=TIME_SERIES_DAILY&symbol={sym}&outputsize=full",
        "intraday_5min": f"function=TIME_SERIES_INTRADAY&symbol={sym}&interval=5min",
        "historical_options": f"function=HISTORICAL_OPTIONS&symbol={sym}&date=2025-09-26",
        "realtime_options": f"function=REALTIME_OPTIONS&symbol={sym}",
        "earnings_calendar": "function=EARNINGS_CALENDAR&horizon=3month",
    }
    return {k: get(f"{b}{q}&apikey={key}") for k, q in fns.items()}


def probe_fmp(sym: str) -> dict:
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return {"skipped": "FMP_API_KEY not set"}
    b = "https://financialmodelingprep.com/stable"
    today = dt.date.today()
    paths = {
        "quote": f"/quote?symbol={sym}",
        "eod_full": f"/historical-price-eod/full?symbol={sym}",
        "intraday_1min": f"/historical-chart/1min?symbol={sym}",
        "intraday_5min": f"/historical-chart/5min?symbol={sym}",
        "aftermarket_trade": f"/aftermarket-trade?symbol={sym}",
        "earnings_calendar": f"/earnings-calendar?from={today}&to={today + dt.timedelta(days=60)}",
        "economic_calendar": f"/economic-calendar?from={today}&to={today + dt.timedelta(days=30)}",
    }
    out = {k: get(f"{b}{p}{'&' if '?' in p else '?'}apikey={key}") for k, p in paths.items()}
    out["note"] = ("FMP public docs list no options/flow endpoints; 'live order flow' is probably the "
                   "WebSocket trade/quote stream (wss), which needs a separate declaration form. Not probed here.")
    return out


def probe_ibkr() -> dict:
    """Client Portal Gateway (local). Only the read-only auth-status call."""
    base = os.environ.get("IBKR_CP_BASE_URL", "https://localhost:5000/v1/api")
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE   # the gateway uses a self-signed cert
    try:
        req = urllib.request.Request(f"{base}/iserver/auth/status", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
            return {"auth_status": {"status": r.status, "shape": describe(json.loads(r.read()))}}
    except Exception as e:
        return {"auth_status": {"status": None, "error": f"{type(e).__name__}: {e}"}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="META")
    ap.add_argument("--out", default=str(ROOT / "docs" / "probe_results.json"))
    ap.add_argument("--only", help="comma list: cboe,flashalpha,alphavantage,fmp,ibkr")
    a = ap.parse_args(argv)
    load_dotenv(ROOT / ".env")
    probes = {"cboe": lambda: probe_cboe(a.symbol), "flashalpha": lambda: probe_flashalpha(a.symbol),
              "alphavantage": lambda: probe_alphavantage(a.symbol), "fmp": lambda: probe_fmp(a.symbol),
              "ibkr": probe_ibkr}
    wanted = a.only.split(",") if a.only else list(probes)
    res = {"run_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "symbol": a.symbol}
    for name in wanted:
        print(f"probing {name} ...", file=sys.stderr)
        res[name] = probes[name]()
    Path(a.out).write_text(json.dumps(res, indent=2))
    for name in wanted:
        for k, v in res[name].items():
            if isinstance(v, dict):
                flag = v.get("Information") or v.get("Error Message") or v.get("error") or v.get("message") or ""
                print(f"{name:13s} {k:28s} {str(v.get('status')):5s} {v.get('bytes', '-'):>9}  {flag[:90]}")
            else:
                print(f"{name:13s} {k:28s} {v}")
    print(f"wrote {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
