"""The universe handed to strategy plugins (NameContext per ticker) and the SignalBook of states + explain log."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .ruleset import SignalState, classify_market, classify_name


@dataclass
class NameContext:
    symbol: str
    today: dt.date
    chain: Any = None               # data.cboe.Chain
    levels: Any = None              # analytics.gex.Levels
    tech: Any = None                # analytics.technicals.Technicals
    vp: Any = None                  # analytics.volume_profile.VolumeProfile
    flow: Any = None                # analytics.flow.Flow
    fs: Any = None                  # signals.flow.FlowSignal
    nv: Any = None                  # signals.vol.NameVol
    info: Any = None                # data.events.EventInfo
    news: Any = None                # data.news.NewsReport
    hist: pd.DataFrame | None = None
    shares: int = 0                 # holdings (covered calls)
    corr_spy: float | None = None
    fresh: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    @property
    def as_of(self) -> str:
        return str(self.chain.as_of) if self.chain is not None else ""

    @property
    def fresh_ok(self) -> bool:
        """Chain no older than ~1 trading day (weekend-aware)."""
        try:
            d = pd.Timestamp(self.as_of).date()
        except Exception:
            return False
        return (self.today - d).days <= (3 if self.today.weekday() == 0 else 1)


@dataclass
class SignalBook:
    today: dt.date
    market_regime: Any = None                      # signals.market.MarketRegime
    market: dict = field(default_factory=dict)     # name -> SignalState
    names: dict = field(default_factory=dict)      # symbol -> {name -> SignalState}
    contexts: dict = field(default_factory=dict)   # symbol -> NameContext
    gates: dict = field(default_factory=dict)      # symbol -> {strategy -> GateResult}
    explain: dict = field(default_factory=dict)    # symbol -> [lines]

    def states(self, symbol: str) -> dict[str, SignalState]:
        return {**self.market, **self.names.get(symbol, {})}

    def log(self, symbol: str, strategy: str, msg: str) -> None:
        self.explain.setdefault(symbol, []).append(f"{strategy}: {msg}")


def build_book(regime, universe: list[NameContext], sheet: dict, cfg: dict, today: dt.date) -> SignalBook:
    book = SignalBook(today, regime)
    book.market = classify_market(regime, sheet, cfg, today) if regime is not None else {}
    for ctx in universe:
        book.contexts[ctx.symbol] = ctx
        if ctx.chain is None or ctx.nv is None:
            book.names[ctx.symbol] = {}
            book.log(ctx.symbol, "data", "no option chain: nothing evaluated")
            continue
        book.names[ctx.symbol] = classify_name(ctx.symbol, ctx.nv, ctx.levels, ctx.tech, ctx.vp, ctx.fs, ctx.info,
                                               ctx.news, sheet, today)
    return book
