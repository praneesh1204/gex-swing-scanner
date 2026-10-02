"""Optional bridge to optopsy for backtests on *real* historical chains you supply.

optopsy is AGPL-3.0. gexscan does not vendor, copy or depend on it: this module imports it only if you have
installed it yourself (`uv pip install optopsy`), and you take on its licence terms for that use. Without
it, `available()` is False and the synthetic harness (harness.py) is the only backtest.

Input: a DataFrame of end-of-day option quotes in optopsy's expected columns (underlying_symbol, quote_date,
expiration, strike, option_type, bid, ask, underlying_price, ...). Output: optopsy's own result frame.
Exit rules here are optopsy's (hold to expiry / its own params), not gexscan's 2x-credit stop; the synthetic
harness is the one that mirrors the live exit plan.
"""
from __future__ import annotations

import importlib

NOTICE = ("optopsy is AGPL-3.0 and is not bundled with gexscan; it is used only if you installed it. "
          "Its exits are its own, not gexscan's 2x-credit stop.")
STRATEGIES = {"csp": "short_puts", "bull_put": "short_put_spread", "bear_call": "short_call_spread",
              "iron_condor": "iron_condor", "calendar": "long_call_calendar"}


def _mod():
    try:
        return importlib.import_module("optopsy")
    except Exception:
        return None


def available() -> bool:
    return _mod() is not None


def run(strategy: str, chains, **params):
    """Run optopsy's implementation of `strategy` on `chains` (a pandas DataFrame you provide)."""
    op = _mod()
    if op is None:
        raise RuntimeError("optopsy is not installed. " + NOTICE)
    name = STRATEGIES.get(strategy)
    fn = getattr(op, name, None) if name else None
    if fn is None:
        raise ValueError(f"no optopsy mapping for {strategy!r} in this optopsy version (known: {', '.join(STRATEGIES)})")
    return fn(chains, **params)
