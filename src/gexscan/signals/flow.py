"""Flow signal (spec §2.5): real trade-side flow when Unusual Whales is configured, else labelled proxies.

Free proxies, each named in the output:
  * OTM call vs OTM put premium traded today, tilted by day-over-day OI change (analytics/flow.py)
  * today's option volume vs the 20-day average from our own snapshots (unusual activity)
  * unusual contracts: volume >= OI
  * CBOE equity put/call ratio extremes (market-wide, contrarian context; not used for the lean)
Proxies can't see trade side, so they can't tell bought from sold. They're weighted 10 points at most.
Volume sources are never mixed in one ratio: every ratio here is CBOE chain volume over CBOE chain volume.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from ..analytics.flow import Flow
from ..data.flowsrc import TradeFlow


@dataclass
class FlowSignal:
    bias: float = 0.0                 # -1 .. +1
    label: str = "neutral"            # bullish / bearish / neutral
    approx: bool = True               # True = free proxy, False = trade-side data
    source: str = "proxy: CBOE chain volume/OI (trade side unknown)"
    volume_vs_20d: float | None = None
    unusual: int = 0
    pc_volume_ratio: float | None = None
    market_pc_equity: float | None = None
    market_pc_note: str = ""
    components: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def flow_signal(flow: Flow | None, trade: TradeFlow | None, volume_hist: pd.Series | None,
                market_pc: dict | None, params: dict) -> FlowSignal:
    th = params.get("bias_at", 0.20)
    fs = FlowSignal()
    if flow is not None:
        fs.components["chain_premium_oi_bias"] = flow.bias
        fs.unusual = len(flow.unusual)
        fs.pc_volume_ratio = round(flow.pc_volume_ratio, 2) if flow.pc_volume_ratio else None
        fs.bias = flow.bias
        today_vol = flow.call_volume + flow.put_volume
        if volume_hist is not None and len(volume_hist.dropna()) >= 5 and today_vol > 0:
            avg = float(volume_hist.dropna().tail(20).mean())
            if avg > 0:
                fs.volume_vs_20d = round(today_vol / avg, 2)
                fs.components["volume_vs_20d_avg"] = fs.volume_vs_20d
        if flow.oi_change_date is None:
            fs.notes.append("no prior OI snapshot yet: OI-change tilt unavailable (builds up as you run daily)")
    if trade is not None and trade.n_trades:
        fs.components["trade_side_bias"] = trade.bias
        fs.bias = round(0.8 * trade.bias + 0.2 * fs.bias, 2) if flow is not None else trade.bias
        fs.approx, fs.source = False, trade.source
    pc = (market_pc or {}).get("equity")
    if pc is not None:
        fs.market_pc_equity = pc
        if pc >= params.get("pc_extreme_high", 1.2):
            fs.market_pc_note = f"CBOE equity put/call {pc:.2f}: fear extreme (contrarian bullish context)"
        elif pc <= params.get("pc_extreme_low", 0.5):
            fs.market_pc_note = f"CBOE equity put/call {pc:.2f}: complacency extreme (contrarian bearish context)"
    fs.label = "bullish" if fs.bias >= th else "bearish" if fs.bias <= -th else "neutral"
    if fs.approx:
        fs.notes.append("flow is a free-data approximation (labelled PROXY), not institutional tape")
    return fs
