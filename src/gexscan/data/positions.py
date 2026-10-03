"""Open short-premium positions for stop alerts. Read-only, always.

Sources:
  * positions.yaml (git-ignored, you maintain it): see positions.example.yaml
  * the journal: ideas you marked as taken (`gexscan journal take`)
  * IBKR Client Portal gateway (optional): GET requests to an allow-list of read-only paths. The client
    has no POST/PUT/DELETE method at all, and any path outside the allow-list raises before a request
    is made. Nothing here can place, change or cancel anything at a broker.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

log = logging.getLogger(__name__)

# The only gateway paths this app may call (GET). Market data + portfolio views, nothing else.
IBKR_ALLOWED = [
    re.compile(r"^/portfolio/accounts$"),
    re.compile(r"^/portfolio/[A-Za-z0-9]+/positions/\d+$"),
    re.compile(r"^/iserver/accounts$"),               # required once before market-data snapshots
    re.compile(r"^/iserver/marketdata/snapshot$"),
]


@dataclass
class Position:
    symbol: str
    strategy: str
    expiry: str
    legs: list                       # [{action, type, strike, avg_price?, mark?}]
    credit: float                    # per share, > 0 for short premium
    contracts: int = 1
    source: str = ""
    ref: str = ""                    # journal id / IBKR conids / yaml index
    opened: str | None = None
    marks: dict = field(default_factory=dict)   # filled by IBKR: {"cost_to_close": x, "as_of": ...}

    def to_dict(self):
        return asdict(self)


def from_yaml(path: str | Path) -> list[Position]:
    p = Path(path)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text()) or {}
    out = []
    for i, r in enumerate(raw.get("positions") or []):
        try:
            legs = [{"action": str(l["action"]).upper(), "type": str(l["type"]).upper()[:1], "strike": float(l["strike"]),
                     **({"expiry": str(l["expiry"])} if l.get("expiry") else {})}       # per-leg expiry: calendars
                    for l in r["legs"]]
            out.append(Position(str(r["symbol"]).upper(), r.get("strategy", "short_premium"), str(r["expiry"]), legs,
                                abs(float(r["credit"])), int(r.get("contracts", 1)), "positions.yaml", f"#{i + 1}",
                                str(r.get("opened")) if r.get("opened") else None))
        except (KeyError, TypeError, ValueError) as e:
            log.warning("positions.yaml entry %d skipped: %s", i + 1, e)
    return out


def from_journal(store) -> list[Position]:
    out = []
    for i in store.ideas(status="open"):
        if i.get("taken") and (i.get("entry_net") or 0) > 0:
            out.append(Position(i["symbol"], i["setup"], i["expiry"], i["legs"], float(i["entry_net"]),
                                int(i.get("contracts") or 1), "journal", f"idea #{i['id']}", i["run_date"]))
    return out


class IBKRReadOnly:
    """Minimal GET-only Client Portal client. See IBKR_ALLOWED."""

    def __init__(self, base_url: str | None = None, timeout: float = 10):
        self.base = (base_url or os.environ.get("IBKR_CP_BASE_URL") or "").rstrip("/")
        if not self.base:
            raise RuntimeError("IBKR_CP_BASE_URL is not set")
        host = urlparse(self.base).hostname or ""
        # the local gateway uses a self-signed certificate; only skip verification for localhost
        self.verify = host not in ("localhost", "127.0.0.1")
        self.timeout = timeout
        if not self.verify:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @staticmethod
    def allowed(path: str) -> bool:
        p = path.split("?", 1)[0]
        if "order" in p.lower() or "trade" in p.lower():
            return False
        return any(r.match(p) for r in IBKR_ALLOWED)

    def get(self, path: str, params: dict | None = None):
        if not self.allowed(path):
            raise PermissionError(f"IBKR path not on the read-only allow-list: {path}")
        r = requests.get(self.base + path, params=params, timeout=self.timeout, verify=self.verify)
        r.raise_for_status()
        return r.json()

    def positions(self) -> list[Position]:
        accts = self.get("/portfolio/accounts") or []
        out: list[Position] = []
        for a in accts:
            aid = a.get("accountId") or a.get("id")
            if not aid:
                continue
            rows, page = [], 0
            while page < 20:
                chunk = self.get(f"/portfolio/{aid}/positions/{page}") or []
                rows += chunk
                if len(chunk) < 100:
                    break
                page += 1
            out += group_ibkr_positions(rows)
        return out


def group_ibkr_positions(rows: list[dict]) -> list[Position]:
    """Group option legs by underlying + expiry. Keeps groups whose net is a credit (short premium)."""
    groups: dict[tuple, list] = {}
    for r in rows:
        if str(r.get("assetClass", "")).upper() != "OPT" or not r.get("position"):
            continue
        und = r.get("undSym") or r.get("ticker") or str(r.get("contractDesc", "")).split(" ")[0]
        exp = str(r.get("expiry") or "")
        if len(exp) == 8 and exp.isdigit():
            exp = f"{exp[:4]}-{exp[4:6]}-{exp[6:]}"
        groups.setdefault((und, exp), []).append(r)
    out = []
    for (und, exp), legs in groups.items():
        mult = float(legs[0].get("multiplier") or 100) or 100
        qty = min(abs(float(l["position"])) for l in legs)
        credit, cost, out_legs = 0.0, 0.0, []
        for l in legs:
            short = float(l["position"]) < 0
            avg = float(l.get("avgCost") or 0) / mult
            mark = float(l.get("mktPrice") or 0)
            credit += avg if short else -avg
            cost += mark if short else -mark
            out_legs.append({"action": "SELL" if short else "BUY", "type": str(l.get("putOrCall", "?"))[:1].upper(),
                             "strike": float(l.get("strike") or 0), "avg_price": round(avg, 4), "mark": mark})
        if credit <= 0:
            continue
        out.append(Position(und, "short_premium", exp, out_legs, round(credit, 4), int(qty), "ibkr",
                            ",".join(str(l.get("conid")) for l in legs),
                            marks={"cost_to_close": round(cost, 4), "as_of": dt.datetime.now().isoformat(timespec="minutes"),
                                   "source": "IBKR portfolio mktPrice"}))
    return out
