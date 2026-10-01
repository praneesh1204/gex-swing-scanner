"""Free CBOE delayed option chains.

Endpoint returns every listed contract for a symbol with bid/ask, IV, greeks,
volume and open interest (OI is from the prior session, quotes ~15 min delayed).
Check CBOE's terms of use before redistributing data; this tool is for personal use.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

OCC_RE = re.compile(r"^(?P<root>[A-Z0-9.]+?)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$")

HEADERS = {"User-Agent": "Mozilla/5.0 (gexscan; personal research)"}


@dataclass
class Chain:
    symbol: str
    spot: float
    as_of: str
    options: pd.DataFrame  # columns: expiry, dte, strike, type, bid, ask, mid, iv, delta, gamma, oi, volume
    raw_meta: dict

    @property
    def expiries(self) -> list[dt.date]:
        return sorted(self.options["expiry"].unique())


def parse_occ(sym: str):
    m = OCC_RE.match(sym.strip())
    if not m:
        return None
    exp = dt.date(2000 + int(m["yy"]), int(m["mm"]), int(m["dd"]))
    return m["root"], exp, "C" if m["cp"] == "C" else "P", int(m["strike"]) / 1000.0


def parse_cboe_json(payload: dict, symbol: str, today: dt.date | None = None) -> Chain:
    today = today or dt.date.today()
    data = payload.get("data", payload)
    spot = None
    for key in ("current_price", "close", "prev_day_close", "last_trade_price"):
        v = data.get(key)
        if isinstance(v, (int, float)) and v > 0:
            spot = float(v)
            break
    rows = []
    for o in data.get("options", []):
        parsed = parse_occ(o.get("option", ""))
        if not parsed:
            continue
        _, exp, cp, strike = parsed
        dte = (exp - today).days
        if dte < 0:
            continue
        bid = float(o.get("bid") or 0.0)
        ask = float(o.get("ask") or 0.0)
        rows.append(
            {
                "expiry": exp,
                "dte": dte,
                "strike": strike,
                "type": cp,
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2 if ask > 0 else bid,
                "iv": float(o.get("iv") or 0.0),
                "delta": float(o.get("delta") or 0.0),
                "gamma": float(o.get("gamma") or 0.0),
                "oi": float(o.get("open_interest") or 0.0),
                "volume": float(o.get("volume") or 0.0),
            }
        )
    df = pd.DataFrame(rows)
    if spot is None and not df.empty:
        spot = _implied_spot_from_parity(df)
    meta = {k: v for k, v in data.items() if k != "options"}
    meta["timestamp_tz"] = "UTC"   # CBOE timestamps are UTC
    return Chain(symbol=symbol.upper(), spot=float(spot or 0.0), as_of=str(payload.get("timestamp", "")), options=df, raw_meta=meta)


def _implied_spot_from_parity(df: pd.DataFrame) -> float | None:
    """Fallback spot estimate: strike where call mid - put mid is closest to zero (nearest expiry)."""
    near = df[df["dte"] == df["dte"].min()]
    piv = near.pivot_table(index="strike", columns="type", values="mid", aggfunc="first").dropna()
    if piv.empty or "C" not in piv or "P" not in piv:
        return None
    diff = (piv["C"] - piv["P"])
    k = (diff.abs()).idxmin()
    return float(k + diff.loc[k])


def fetch_chain(symbol: str, url_template: str, timeout: int = 20, cache_dir: Path | None = None,
                today: dt.date | None = None) -> Chain:
    """Download and parse a chain. If cache_dir is given, raw JSON is saved (and reused if fetch fails)."""
    symbol = symbol.upper()
    # CBOE moved the feed from cdn.cboe.com to cdn-api.cboe.com (the old host 307-redirects)
    url = url_template.replace("//cdn.cboe.com", "//cdn-api.cboe.com").format(symbol=_cboe_symbol(symbol))
    cache_file = cache_dir / f"{symbol}.json" if cache_dir else None
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        payload = r.json()
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(payload))
    except Exception:
        if cache_file and cache_file.exists():
            payload = json.loads(cache_file.read_text())
        else:
            raise
    return parse_cboe_json(payload, symbol, today=today)


INDEX_ROOTS = {"SPX", "VIX", "NDX", "RUT", "XSP"}


def _cboe_symbol(symbol: str) -> str:
    """CBOE prefixes index roots with an underscore (_SPX, _VIX)."""
    return f"_{symbol}" if symbol in INDEX_ROOTS else symbol


def fetch_daily_history(symbol: str, url_template: str, timeout: int = 20) -> pd.DataFrame:
    """Daily OHLCV from CBOE (back to ~2012). Lags one session; returns empty DataFrame on failure."""
    url = url_template.format(symbol=_cboe_symbol(symbol.upper()))
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        df = pd.DataFrame(r.json().get("data", []))
        if df.empty:
            return df
        df.index = pd.to_datetime(df.pop("date"))
        return df[["open", "high", "low", "close", "volume"]].astype(float)
    except Exception:
        return pd.DataFrame()


def load_chain_file(path: str | Path, symbol: str, today: dt.date | None = None) -> Chain:
    payload = json.loads(Path(path).read_text())
    return parse_cboe_json(payload, symbol, today=today)
