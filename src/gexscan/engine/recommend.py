"""`recommend(watchlist, filters)` (spec §6): data -> signals -> SignalBook -> plugins -> score -> rank.

Returns a Recommendations object (JSON-able) and, via reports.brief, a console / markdown brief. Every
recommendation carries its legs, net, max loss, breakevens, PoP, DTE, the exit plan (short premium: stop at
2x the credit received, i.e. loss = the credit; no rolling), the confidence with sub-scores, a three-level
rationale and the gate states. There is no execution step: you decide and place every trade yourself.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..analytics.flow import compute_flow
from ..analytics.technicals import compute_technicals
from ..analytics.volume_profile import compute_volume_profile
from ..config import load_signal_sheet
from ..data import prices
from ..data.events import EventSource
from ..data.flowsrc import unusual_whales
from ..data.macro import MarketSource
from ..data.providers import capabilities, has_keys, make_http
from ..pipeline import DISCLAIMER, DataSource, _levels
from ..signals.book import NameContext, SignalBook, build_book
from ..signals.flow import flow_signal
from ..signals.market import correlation, market_regime
from ..signals.vol import name_vol
from ..store import Store
from ..strategies import Candidate
from .selector import Filters, select
from .universe import load_universe

log = logging.getLogger(__name__)


class EngineSource(DataSource):
    """DataSource plus what the engine needs: ~2y of daily bars, IV history, events, market data, real flow."""

    def __init__(self, cfg: dict, fixtures: Path | None = None, today: dt.date | None = None,
                 state_dir: Path | None = None, use_news: bool = True):
        state = Path(state_dir or cfg["output"]["state_dir"])
        super().__init__(cfg, fixtures, cache_dir=state / "cache" / "cboe", today=today, use_news=use_news)
        self.http = make_http(cfg, state, offline=bool(fixtures))
        self.events = EventSource(cfg, self.http, fixtures, self.today)
        self.market = MarketSource(cfg, self.http, fixtures, self.today)
        self._ivh: pd.DataFrame | None = None

    def daily(self, sym: str) -> pd.DataFrame:
        if self.fixtures:
            d = self._csv(f"{sym}_daily2y.csv")
            return d if len(d) else self._csv(f"{sym}_history.csv")
        dcfg = self.cfg["data"]
        return prices.fetch_history(sym, self.cfg["engine"].get("history_days", 800), dcfg.get("cboe_history_url"))

    def benchmark(self) -> pd.DataFrame:
        """~2y of benchmark bars (60-day correlation, relative strength). Fixtures: {bench}_daily2y.csv."""
        if self._bench is None:
            b = self.cfg["data"].get("benchmark", "SPY")
            self._bench = (self._csv(f"{b}_daily2y.csv") if self.fixtures else
                           prices.fetch_history(b, self.cfg["engine"].get("history_days", 800),
                                                self.cfg["data"].get("cboe_history_url")))
        return self._bench

    def iv_history(self, sym: str, store: Store | None) -> pd.Series:
        """Our own daily ATM-IV snapshots. Fixtures: iv_history.csv (date index, one column per symbol)."""
        if self.fixtures:
            if self._ivh is None:
                p = self.fixtures / "iv_history.csv"
                self._ivh = pd.read_csv(p, index_col=0, parse_dates=True) if p.exists() else pd.DataFrame()
            if sym in self._ivh.columns:
                s = self._ivh[sym].dropna()
                return s[s.index < pd.Timestamp(self.today)]
        return store.iv_history(sym, self.today, 365) if store else pd.Series(dtype=float)

    def trade_flow(self, sym: str):
        if self.fixtures or not has_keys("unusual_whales"):
            return None
        try:
            return unusual_whales(sym, self.http)
        except Exception as e:  # a vendor outage falls back to the labelled proxy
            log.warning("unusual whales %s: %s", sym, e)
            return None


def build_context(sym: str, src: EngineSource, cfg: dict, sheet: dict, store: Store | None, md=None,
                  iv_store: Store | None = None) -> NameContext:
    """store: read history and write today's snapshot. iv_store: read-only history when store is None."""
    today = src.today
    hist_store = store or iv_store
    ctx = NameContext(sym, today, shares=int((cfg.get("holdings") or {}).get(sym, 0) or 0))
    try:
        chain = src.chain(sym)
    except Exception as e:
        ctx.errors.append(f"{sym}: option chain unavailable ({e})")
        return ctx
    if chain.options.empty or chain.spot <= 0:
        ctx.errors.append(f"{sym}: empty chain / no spot")
        return ctx
    ctx.chain = chain
    ctx.levels = lv = _levels(chain, cfg)
    ctx.hist = hist = src.daily(sym)
    bench = src.benchmark()
    ctx.tech = compute_technicals(hist, bench)
    ctx.corr_spy = correlation(hist, bench)
    ctx.info = src.events.get(sym, hist)
    ctx.nv = name_vol(chain, hist, src.iv_history(sym, hist_store), ctx.info, sheet, today)
    vpc = cfg["volume_profile"]
    try:
        intra = src.intraday(sym)
    except Exception as e:
        intra = pd.DataFrame()
        ctx.errors.append(f"{sym}: intraday bars unavailable ({e})")
    ctx.vp = compute_volume_profile(intra, hist, chain.spot, vpc["bins"], vpc["value_area_pct"])
    prev_date, prev_oi = hist_store.previous_contract_oi(sym, today) if hist_store else (None, None)
    fc = cfg["flow"]
    ctx.flow = compute_flow(chain.options, chain.spot, prev_oi, prev_date, fc["unusual_min_volume"], fc["unusual_vol_oi_ratio"])
    vh = hist_store.volume_history(sym, today, 40) if hist_store else None
    ctx.fs = flow_signal(ctx.flow, src.trade_flow(sym), vh, md.put_call if md else None, sheet["signals"]["flow"])
    ctx.news = src.news(sym)
    ctx.fresh = {
        "chain": f"{'fixture' if src.fixtures else 'CBOE delayed ~15m'}, as of {chain.as_of} UTC; OI is prior-day",
        "daily": f"{hist.attrs.get('source', '?')}, {len(hist)} bars to {str(hist.index[-1])[:10]}" if len(hist) else "unavailable",
        "iv_rank": ctx.nv.iv_rank_source or "unavailable",
        "flow": ctx.fs.source,
        "earnings": ctx.info.earnings_source or "unknown",
    }
    if ctx.news is not None:
        newest = max((i.published for i in ctx.news.items if i.published), default="")
        ctx.fresh["news"] = f"{', '.join(ctx.news.providers) or 'none'}; newest {newest[:16]}"
    if store is not None:
        store.save_snapshot(today, {
            "symbol": sym, "as_of": lv.as_of, "spot": lv.spot, "atm_iv": ctx.nv.atm_iv or lv.atm_iv, "iv30": ctx.nv.atm_iv,
            "net_gex": lv.net_gex, "call_wall": lv.call_wall, "put_wall": lv.put_wall, "gamma_flip": lv.gamma_flip,
            "call_volume": lv.call_volume, "put_volume": lv.put_volume, "call_oi": lv.total_call_oi,
            "put_oi": lv.total_put_oi, "rv20": ctx.nv.rv20}, chain.options)
        store.save_vol(today, sym, ctx.nv)
    return ctx


@dataclass
class Recommendations:
    today: str
    generated: str
    market: dict
    market_states: dict
    recommendations: list            # [Candidate]
    rejected: list                   # [{symbol, strategy, summary, why, confidence}]
    explain: dict                    # symbol -> [lines] (gate failures, build rejections, drops)
    names: dict                      # symbol -> {states, fresh, errors}
    filters: dict
    capabilities: list
    errors: list = field(default_factory=list)
    disclaimer: str = DISCLAIMER
    book: SignalBook | None = None   # not serialised; used by `explain`

    def to_dict(self) -> dict:
        return {"today": self.today, "generated": self.generated, "market": self.market,
                "market_states": self.market_states, "filters": self.filters,
                "recommendations": [c.to_dict() for c in self.recommendations], "rejected": self.rejected,
                "explain": self.explain, "names": self.names, "capabilities": self.capabilities,
                "errors": self.errors, "disclaimer": self.disclaimer}


def recommend(watchlist: list[str] | None, filters: Filters | None = None, cfg: dict | None = None,
              fixtures: Path | None = None, today: dt.date | None = None, state_dir: Path | None = None,
              use_news: bool = True, save_snapshots: bool = True) -> Recommendations:
    """watchlist None = the default run (universe + outside + watchlist, spec v2.2 §4); a list = explicit pick.
    save_snapshots False skips the market-data snapshot (nothing is ever written to the journal here)."""
    from ..config import DEFAULTS

    cfg = cfg or DEFAULTS
    filters = filters or Filters()
    today = today or dt.date.today()
    sheet = load_signal_sheet(cfg)
    state = Path(state_dir or cfg["output"]["state_dir"])
    src = EngineSource(cfg, fixtures, today, state, use_news=use_news)
    uni = load_universe(cfg, fixtures=bool(fixtures))
    if filters.symbols:
        syms, default_run = [s.upper() for s in filters.symbols], False
    elif filters.themes:
        syms, default_run = uni.symbols(filters.themes), False
    elif watchlist is not None:
        syms, default_run = [s.upper() for s in watchlist], False
    else:
        syms, default_run = uni.scan_list(cfg.get("watchlist") or []), True
    store = Store(state)
    errors: list[str] = []
    try:
        md = src.market.load() if cfg["macro"].get("enabled", True) else None
        regime = market_regime(md, today) if md is not None else None
        if md is not None:
            errors += md.errors
        universe = []
        for s in syms:
            try:
                ctx = build_context(s, src, cfg, sheet, store if save_snapshots else None, md, iv_store=store)
            except Exception as e:  # one bad ticker must not kill the run
                log.exception("context %s", s)
                ctx = NameContext(s, today, errors=[f"{s}: {type(e).__name__}: {e}"])
            errors += ctx.errors
            universe.append(ctx)
        book = build_book(regime, universe, sheet, cfg, today)
        recs, dropped, resolved = select(universe, book, sheet, cfg, filters, uni, default_run)
    finally:
        store.close()
    names = {sym: {"states": {k: v.to_dict() for k, v in st.items()}, "fresh": book.contexts[sym].fresh,
                   "errors": book.contexts[sym].errors, "spot": book.contexts[sym].chain.spot if book.contexts[sym].chain else None,
                   "as_of": book.contexts[sym].as_of}
             for sym, st in book.names.items()}
    return Recommendations(
        today=today.isoformat(), generated=dt.datetime.now().isoformat(timespec="seconds"),
        market=regime.to_dict() if regime is not None else {},
        market_states={k: v.to_dict() for k, v in book.market.items()},
        recommendations=recs, rejected=dropped, explain=book.explain, names=names,
        filters={**{k: v for k, v in vars(filters).items() if v is not None}, **resolved,
                 "default_run": default_run, "universe_mode": uni.mode},
        capabilities=capabilities(), errors=errors, book=book)


def write_recommendations(r: Recommendations, out_dir: Path, md_text: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    j = out_dir / f"recommendations_{r.today}.json"
    m = out_dir / f"recommendations_{r.today}.md"
    j.write_text(json.dumps(r.to_dict(), indent=2, default=str))
    m.write_text(md_text)
    return j, m


__all__ = ["Candidate", "EngineSource", "Filters", "Recommendations", "build_context", "recommend",
           "write_recommendations"]
