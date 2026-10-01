"""Options activity from the day's chain: call/put volume, premium traded, unusual contracts, OI change.

This is a *proxy* for flow. The free chain has no trade-side data, so it can't tell buyer-initiated
from seller-initiated prints. The bias score is a hint that is weighted lightly, never a signal on its own.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Flow:
    call_volume: float = 0.0
    put_volume: float = 0.0
    pc_volume_ratio: float | None = None
    call_premium: float = 0.0          # $ traded at mid, all expiries
    put_premium: float = 0.0
    otm_call_premium: float = 0.0
    otm_put_premium: float = 0.0
    volume_vs_oi: float | None = None  # total volume / total OI
    unusual: list = field(default_factory=list)   # [{contract, volume, oi, ratio, premium}]
    oi_change_date: str | None = None
    oi_adds: list = field(default_factory=list)   # biggest OI increases since the last snapshot
    oi_cuts: list = field(default_factory=list)
    net_call_oi_change: float | None = None
    net_put_oi_change: float | None = None
    bias: float = 0.0                  # -1 (bearish activity) .. +1 (bullish activity)
    bias_label: str = "neutral"
    note: str = "proxy: chain volume/OI only, trade side unknown"

    def to_dict(self):
        return asdict(self)


def _fmt(row) -> str:
    return f"{row['expiry']:%m/%d} {row['strike']:g}{row['type']}"


def compute_flow(opts: pd.DataFrame, spot: float, prev_oi: pd.DataFrame | None = None, prev_date: str | None = None,
                 min_volume: int = 500, vol_oi_ratio: float = 1.0) -> Flow:
    f = Flow()
    if opts is None or opts.empty:
        return f
    o = opts.copy()
    o["prem"] = o["mid"].clip(lower=0) * o["volume"] * 100
    c, p = o[o["type"] == "C"], o[o["type"] == "P"]
    f.call_volume, f.put_volume = float(c["volume"].sum()), float(p["volume"].sum())
    f.pc_volume_ratio = f.put_volume / f.call_volume if f.call_volume else None
    f.call_premium, f.put_premium = float(c["prem"].sum()), float(p["prem"].sum())
    f.otm_call_premium = float(c.loc[c["strike"] > spot, "prem"].sum())
    f.otm_put_premium = float(p.loc[p["strike"] < spot, "prem"].sum())
    toi = o["oi"].sum()
    f.volume_vs_oi = float(o["volume"].sum() / toi) if toi else None

    u = o[(o["volume"] >= min_volume) & (o["volume"] >= vol_oi_ratio * o["oi"].clip(lower=1))]
    u = u.sort_values("prem", ascending=False).head(6)
    f.unusual = [{"contract": _fmt(r), "volume": int(r["volume"]), "oi": int(r["oi"]),
                  "ratio": round(r["volume"] / max(r["oi"], 1), 1), "premium": round(r["prem"])}
                 for _, r in u.iterrows()]

    if prev_oi is not None and not prev_oi.empty:
        cur = o[["expiry", "strike", "type", "oi"]].copy()
        cur["expiry"] = cur["expiry"].astype(str)
        m = cur.merge(prev_oi, on=["expiry", "strike", "type"], how="inner", suffixes=("", "_prev"))
        m["d"] = m["oi"] - m["oi_prev"]
        f.oi_change_date = prev_date
        f.net_call_oi_change = float(m.loc[m["type"] == "C", "d"].sum())
        f.net_put_oi_change = float(m.loc[m["type"] == "P", "d"].sum())
        m["expiry"] = pd.to_datetime(m["expiry"])
        f.oi_adds = [f"{_fmt(r)} {int(r['d']):+,}" for _, r in m.nlargest(5, "d").iterrows() if r["d"] > 0]
        f.oi_cuts = [f"{_fmt(r)} {int(r['d']):+,}" for _, r in m.nsmallest(3, "d").iterrows() if r["d"] < 0]

    # Bias: OTM call vs OTM put premium, tilted by OI build when available. Bounded to [-1, 1].
    tot = f.otm_call_premium + f.otm_put_premium
    b = (f.otm_call_premium - f.otm_put_premium) / tot if tot > 0 else 0.0
    if f.net_call_oi_change is not None:
        doi = f.net_call_oi_change - f.net_put_oi_change
        scale = max(abs(f.net_call_oi_change) + abs(f.net_put_oi_change), 1)
        b = 0.6 * b + 0.4 * doi / scale
    f.bias = round(float(np.clip(b, -1, 1)), 2)
    f.bias_label = "bullish" if f.bias > 0.2 else "bearish" if f.bias < -0.2 else "neutral"
    return f
