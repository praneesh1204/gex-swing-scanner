"""Trade-side options flow from Unusual Whales (needs UNUSUAL_WHALES_API_KEY; paid).

When the key is present this replaces the free chain-volume proxies with real at-ask / at-bid premium.
The response schema is parsed defensively (field names vary by endpoint and plan); anything unexpected
returns None and the free proxies are used instead, labelled as such.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass

from .providers import Http

log = logging.getLogger(__name__)
UW_FLOW = "https://api.unusualwhales.com/api/stock/{ticker}/flow-recent"


@dataclass
class TradeFlow:
    symbol: str
    call_ask_premium: float = 0.0     # bought calls (lifted the offer)
    call_bid_premium: float = 0.0     # sold calls (hit the bid)
    put_ask_premium: float = 0.0
    put_bid_premium: float = 0.0
    n_trades: int = 0
    source: str = ""

    @property
    def bias(self) -> float:
        bull = self.call_ask_premium + self.put_bid_premium
        bear = self.put_ask_premium + self.call_bid_premium
        tot = bull + bear
        return round((bull - bear) / tot, 2) if tot > 0 else 0.0

    def to_dict(self):
        d = asdict(self)
        d["bias"] = self.bias
        return d


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def unusual_whales(sym: str, http: Http) -> TradeFlow | None:
    key = os.environ.get("UNUSUAL_WHALES_API_KEY")
    if not key:
        return None
    f = http.get("unusual_whales", UW_FLOW.format(ticker=sym), headers={"Authorization": f"Bearer {key}",
                 "Accept": "application/json"}, ttl_s=900)
    if f is None:
        return None
    rows = f.data.get("data", f.data) if isinstance(f.data, dict) else f.data
    if not isinstance(rows, list):
        return None
    tf = TradeFlow(sym, source=f"unusual_whales {f.freshness()}")
    for r in rows:
        if not isinstance(r, dict):
            continue
        cp = str(r.get("option_type") or r.get("type") or r.get("put_call") or "").lower()[:1]
        prem = _f(r.get("premium") or r.get("total_premium"))
        ask = _f(r.get("ask_side_premium") or r.get("total_ask_side_prem"))
        bid = _f(r.get("bid_side_premium") or r.get("total_bid_side_prem"))
        side = str(r.get("side") or r.get("aggressor") or "").lower()
        if not (ask or bid) and prem:
            ask, bid = (prem, 0.0) if "ask" in side or side == "buy" else (0.0, prem) if "bid" in side or side == "sell" else (0.0, 0.0)
        if cp == "c":
            tf.call_ask_premium += ask
            tf.call_bid_premium += bid
        elif cp == "p":
            tf.put_ask_premium += ask
            tf.put_bid_premium += bid
        else:
            continue
        tf.n_trades += 1
    return tf if tf.n_trades else None
