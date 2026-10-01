"""Optional FlashAlpha cross-check (free plan: 5 requests/day, ~250 symbols).

Set the FLASHALPHA_API_KEY environment variable to enable.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)
BASE = "https://lab.flashalpha.com/v1"


def get_levels(symbol: str, timeout: int = 20) -> dict | None:
    key = os.environ.get("FLASHALPHA_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(f"{BASE}/exposure/levels/{symbol.upper()}", headers={"X-Api-Key": key}, timeout=timeout)
        if r.status_code != 200:
            log.warning("FlashAlpha %s -> HTTP %s: %s", symbol, r.status_code, r.text[:200])
            return None
        body = r.json()
        lv = body.get("levels", {})
        return {
            "source": "flashalpha",
            "as_of": body.get("as_of"),
            "spot": body.get("underlying_price"),
            "gamma_flip": lv.get("gamma_flip"),
            "call_wall": lv.get("call_wall"),
            "put_wall": lv.get("put_wall"),
            "max_pain": lv.get("max_pain"),
        }
    except Exception as e:
        log.warning("FlashAlpha request failed for %s: %s", symbol, e)
        return None
