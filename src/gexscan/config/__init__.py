from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

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
        "stop_credit_multiple": 2.0,        # buy back when cost to close = 2x the credit (loss = 1x credit). No rolling.
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
        "enforce_budget": False,            # true = drop ideas whose 1 contract risks more than the per-trade budget
    },
    "review": {"big_move_pct": 5.0, "gex_weaken_pct": 30.0},
    "output": {"report_dir": "reports", "state_dir": "state", "top_n": 5, "charts": True},
    # ---- strategy engine (gexscan recommend / explain / alerts / backtest) ----------------------
    "engine": {
        "signal_sheet": None,                # path to your own copy; default = packaged config/signal_sheet.yaml
        "sheet_overrides": {},               # deep-merged into the sheet, e.g. {signals: {iv_rank: {rich: 55}}}
        "strategies": ["csp", "covered_call", "bull_put", "bear_call", "bull_call", "bear_put", "calendar",
                       "double_calendar", "double_diagonal", "iron_condor", "earnings_crush"],
        "min_confidence": 40,                # floor: Low < 40 <= Medium <= 70 < High
        "top_n": 10,
        "max_per_ticker": 2,
        "allow_undefined_risk": False,       # short strangles etc. are opt-in and flagged UNDEFINED RISK
        "history_days": 800,                 # daily bars: IV-rank proxy, VRP, earnings-move history
        "earnings_size_factor": 0.5,         # earnings trades are sized at half the normal risk budget
        "weights": {"regime": 20, "iv_richness": 25, "structure": 15, "positioning": 15,
                    "flow": 10, "liquidity": 10, "event": 5},
        "penalties": {"flow_against": 8, "binary_event": 10, "stale_data": 5, "wide_roundtrip": 6},
    },
    # ---- v2.2: tech universe (edit in config.yaml; a `themes:` block there replaces this one) ----------
    "universe": {
        "mode": "prefer",                    # prefer | only | off (off = watchlist only, as in v2.1)
        "themes": {
            "gpus": {"name": "GPUs & AI accelerators", "tickers": ["NVDA", "AMD", "AVGO", "MRVL"]},
            "cpus": {"name": "CPUs", "tickers": ["AMD", "INTC", "ARM"]},
            "chips_foundry": {"name": "Chips, foundry & equipment",
                              "tickers": ["TSM", "ASML", "AMAT", "LRCX", "KLAC", "TSEM", "INTC"]},
            "memory": {"name": "Memory & storage", "tickers": ["MU", "WDC", "STX", "SNDK"]},
            "networking": {"name": "Networking & interconnect",
                           "tickers": ["ANET", "CSCO", "AVGO", "MRVL", "CIEN", "CRDO", "ALAB"]},
            "optics": {"name": "Optics, lasers & silicon photonics",
                       "tickers": ["LITE", "COHR", "AAOI", "FN", "AXTI", "POET"]},
            "power_semis": {"name": "Power semis", "tickers": ["ON", "MPWR", "NVTS", "VICR", "POWI"]},
            "data_center": {"name": "Data centre", "tickers": ["EQIX", "DLR", "SMCI", "DELL", "VRT", "NBIS"]},
            "cooling": {"name": "Cooling & DC power", "tickers": ["VRT", "NVT", "MOD", "ETN"]},
            "energy": {"name": "Energy & power for AI", "tickers": ["CEG", "VST", "TLN", "GEV", "BE", "OKLO", "CCJ"]},
            "infra": {"name": "AI infra & neoclouds", "tickers": ["NBIS", "CRWV", "IREN", "APLD", "ORCL"]},
            "hyperscalers": {"name": "Hyperscalers", "tickers": ["MSFT", "GOOGL", "META", "AMZN", "ORCL"]},
        },
        "outside": {"allow": True, "symbols": ["HOOD", "BMNR", "TQQQ", "SPY", "QQQ", "IWM", "AAPL", "TSLA",
                                               "COIN", "PLTR"],
                    "min_confidence": 85, "max_per_day": 2},
        "concentration": {"max_ideas_per_theme": 3, "correlated_warn_pct": 0.60},
    },
    # ---- v2.2: pricing, simulation, web app -----------------------------------------------------
    "pricing": {"rate": 0.04},               # risk-free rate for the visualizer (shown on every page)
    "sim": {"paths": 20000, "max_paths": 50000, "fan_percentiles": [5, 25, 50, 75, 95], "min_vol": 0.05},
    "web": {"port": 8765, "cache_ttl_s": 300, "max_legs": 8},
    "macro": {
        "enabled": True,
        "fred_series": {"fed_funds": "DFF", "y2": "DGS2", "y10": "DGS10", "curve_10y2y": "T10Y2Y",
                        "cpi_index": "CPIAUCSL", "wti": "DCOILWTICO"},
        "cpi_dates": [],                     # leave empty to read the BLS CPI schedule from FRED's release calendar
        "fomc_dates": ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
                       "2026-09-16", "2026-10-28", "2026-12-09"],   # decision days; update each year
        "near_days": 3,                      # an FOMC/CPI print this close = binary macro event
        "pc_lookback_days": 5,               # CBOE daily put/call statistics, newest trading day
    },
    "events": {
        "earnings_overrides": {},            # {SYMBOL: YYYY-MM-DD} when the free sources have no date
        "history_quarters": 8,               # past earnings moves used for the implied-vs-historical check
    },
    "providers": {
        "cache_dir": None,                   # default: <state_dir>/cache/http
        "default_ttl_s": 3600,
        "use_openbb": False,                 # OpenBB is AGPL-3.0: optional, never vendored
        "limits": {},                        # per-provider overrides: {finnhub: {min_interval_s: 1.1, daily_cap: 3000}}
    },
    "alerts": {
        "sinks": ["console", "log"],         # + webhook / telegram / email (each needs env vars, see .env.example)
        "log_file": None,                    # JSON lines; default <state_dir>/alerts.log
        "positions_file": "positions.yaml",  # your open short-premium positions (git-ignored)
        "use_ibkr": False,                   # read positions from the IBKR Client Portal gateway (GET only)
        "stop_warn_ratio": 0.875,            # warn when cost to close reaches 87.5% of the stop (1.75x credit)
        "ivr_levels": [30, 50],              # alert when a watchlist name's IV rank crosses these
        "vvix_spike_pct": 15,                # VVIX up this much over 5 sessions
        "earnings_window_days": 10,          # alert when earnings come within this many days
    },
    "backtest": {
        "years": 3,
        "entry_every_days": 5,               # one entry per name per week
        "iv_premium": 1.10,                  # IV proxy = max(RV20, RV60) x this (no free historical chains)
        "slippage_pct_of_credit": 0.05,
        "strategies": ["csp", "bull_put", "bear_call", "iron_condor", "calendar"],
    },
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


def stop_multiple(tcfg: dict) -> float:
    """Short-premium stop: close when the cost to buy the position back reaches this multiple of the credit."""
    return float(tcfg.get("stop_credit_multiple", 2.0))


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    legacy = (raw.get("trades") or {}).pop("stop_loss_credit_multiple", None)
    cfg = _merge(DEFAULTS, raw)
    if (raw.get("universe") or {}).get("themes"):   # your theme list replaces the default one (no merging)
        cfg["universe"]["themes"] = copy.deepcopy(raw["universe"]["themes"])
    if legacy is not None and "stop_credit_multiple" not in (raw.get("trades") or {}):
        # v2.0 key meant "loss multiple" (loss = k x credit, buy back at (k+1)x); keep that meaning, but say so
        cfg["trades"]["stop_credit_multiple"] = float(legacy) + 1.0
        log.warning("config: trades.stop_loss_credit_multiple=%s is deprecated; it means buy back at %sx credit. "
                    "Use trades.stop_credit_multiple (default 2.0 = buy back at 2x credit).", legacy, float(legacy) + 1)
    cfg["watchlist"] = [s.strip().upper() for s in cfg["watchlist"] if s and str(s).strip()]
    cfg["holdings"] = {str(k).upper(): int(v) for k, v in (cfg.get("holdings") or {}).items()}
    return cfg


SHEET_PATH = Path(__file__).with_name("signal_sheet.yaml")


def load_signal_sheet(cfg: dict | None = None) -> dict:
    """The §3 signal sheet: thresholds per signal and required/preferred gates per strategy.
    `engine.signal_sheet` points at your own copy; `engine.sheet_overrides` is merged on top."""
    eng = (cfg or {}).get("engine", {})
    path = Path(eng.get("signal_sheet") or SHEET_PATH)
    with open(path, "r", encoding="utf-8") as f:
        sheet = yaml.safe_load(f) or {}
    return _merge(sheet, eng.get("sheet_overrides") or {})
