from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

# Defaults for every section, so an older or minimal config.yaml still runs.
DEFAULTS: dict[str, Any] = {
    "watchlist": [],
    "holdings": {},                      # {SYMBOL: shares} -> covered-call ideas (read-only, you maintain it)
    "data": {
        "cboe_url": "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{symbol}.json",
        "cboe_history_url": "https://cdn-api.cboe.com/api/global/delayed_quotes/charts/historical/{symbol}.json",
        "max_dte_for_levels": 60,
        "flashalpha_symbols": [],
        "request_timeout_s": 20,
        "pause_between_symbols_s": 1.0,
        "history_days": 400,
        "intraday_interval": "30m",
        "intraday_days": 30,
        "benchmark": "SPY",
    },
    "gex": {"flip_search_range_pct": 0.30, "flip_grid_points": 241},
    "volume_profile": {"bins": 60, "value_area_pct": 0.70},
    "flow": {"unusual_min_volume": 500, "unusual_vol_oi_ratio": 1.0},
    "news": {"enabled": True, "max_items": 8, "lookback_days": 5,
             "llm": True, "llm_model": "claude-opus-5-5", "llm_max_symbols": 10},
    "scoring": {
        "weights": {"regime": 25, "location": 20, "vol_edge": 20, "trend": 15, "liquidity": 10, "flow_bonus": 10},
        "min_score_to_trade": 55,
        "news_penalty": 10,              # points removed when news carries a red-flag event
    },
    "trades": {
        "target_dte_min": 14,
        "target_dte_max": 45,
        "target_dte_ideal": 35,
        "earnings_buffer_days": 5,
        "credit_spread_width_pct": 0.04,
        "short_delta_max": 0.30,
        "short_delta_min": 0.12,
        "min_credit_to_width": 0.25,
        "max_bid_ask_pct": 0.25,
        "take_profit_pct": 0.50,
        "stop_loss_credit_multiple": 2.0,   # close when LOSS = 2x credit received (buy back at 3x credit)
        "time_exit_dte": 21,                # or halfway to expiry, whichever comes first
        "debit_take_profit_pct": 0.75,      # of max profit
        "account_size": 25000,
        "max_risk_per_trade_pct": 0.02,
        "csp_max_notional_pct": 0.30,       # show a cash-secured put only if strike*100 <= this share of account
        "cc_min_days_to_earnings": 5,
    },
    "risk": {
        "max_new_ideas_per_day": 3,
        "max_ideas_per_symbol": 1,
        "cooling_off_after_stop_days": 1,   # no new ideas the day after a stop was hit
        "max_total_risk_pct": 0.10,         # cap on max-loss summed over taken + new ideas
    },
    "review": {"big_move_pct": 5.0, "gex_weaken_pct": 30.0},
    "output": {"report_dir": "reports", "state_dir": "state", "top_n": 5, "charts": True},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, never overrides variables already set."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(k.strip(), v)


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, raw)
    cfg["watchlist"] = [s.strip().upper() for s in cfg["watchlist"] if s and str(s).strip()]
    cfg["holdings"] = {str(k).upper(): int(v) for k, v in (cfg.get("holdings") or {}).items()}
    return cfg
