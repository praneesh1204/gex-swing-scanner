"""Static PNG charts (base64-embedded in the HTML report). matplotlib is optional."""
from __future__ import annotations

import base64
import io
import logging

import pandas as pd

log = logging.getLogger(__name__)

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:  # pragma: no cover
    plt = None

C_CALL, C_PUT, C_NET, C_SPOT, C_GRID = "#2e9d6a", "#d0574f", "#4a7bd0", "#e0a030", "#8888"


def _b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", transparent=True)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _style(ax):
    ax.grid(True, color=C_GRID, lw=0.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(colors="#888", labelsize=8)


def _vlines(ax, lv, horizontal=False):
    marks = [(lv.spot, C_SPOT, "spot"), (lv.put_wall, C_PUT, "put wall"), (lv.call_wall, C_CALL, "call wall"),
             (lv.gamma_flip, "#999", "flip")]
    for v, c, name in marks:
        if v:
            (ax.axhline if horizontal else ax.axvline)(v, color=c, lw=1.2, ls="--" if name != "spot" else "-", label=f"{name} {v:.2f}")


def gex_chart(sym: str, by_strike: pd.DataFrame, lv) -> str | None:
    if plt is None or by_strike is None or by_strike.empty:
        return None
    try:
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.5, 4.6), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
        k = by_strike.index.to_numpy()
        w = (k[1:] - k[:-1]).min() * 0.8 if len(k) > 1 else 1
        a1.bar(k, by_strike["gex_net"] / 1e6, width=w, color=[C_CALL if v >= 0 else C_PUT for v in by_strike["gex_net"]])
        a1.set_ylabel("net GEX ($M / 1%)", fontsize=8, color="#888")
        _vlines(a1, lv)
        a1.legend(fontsize=7, frameon=False, loc="upper left")
        a1.set_title(f"{sym}: dealer gamma by strike (model estimate, OI is prior-day)", fontsize=9, color="#888")
        a2.bar(k, by_strike["oi_C"], width=w, color=C_CALL, alpha=0.7, label="call OI")
        a2.bar(k, -by_strike["oi_P"], width=w, color=C_PUT, alpha=0.7, label="put OI")
        a2.plot(k, by_strike["volume_C"], color=C_CALL, lw=1, label="call vol")
        a2.plot(k, -by_strike["volume_P"], color=C_PUT, lw=1, label="put vol")
        a2.axvline(lv.spot, color=C_SPOT, lw=1.2)
        a2.set_ylabel("OI / volume", fontsize=8, color="#888")
        a2.legend(fontsize=7, frameon=False, ncol=4, loc="lower left")
        for a in (a1, a2):
            _style(a)
        return _b64(fig)
    except Exception as e:
        log.info("gex chart %s failed: %s", sym, e)
        return None


def price_chart(sym: str, hist: pd.DataFrame, lv, vp) -> str | None:
    if plt is None or hist is None or len(hist) < 20:
        return None
    try:
        h = hist.tail(120).astype(float)
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.5, 3.6), sharey=True, gridspec_kw={"width_ratios": [4, 1], "wspace": 0.03})
        a1.plot(h.index, h["close"], color="#666", lw=1.3, label="close")
        c = hist["close"].astype(float)
        a1.plot(h.index, c.rolling(20).mean().tail(len(h)), color=C_NET, lw=1, label="SMA20")
        if len(c) >= 50:
            a1.plot(h.index, c.rolling(50).mean().tail(len(h)), color="#a070c0", lw=1, label="SMA50")
        _vlines(a1, lv, horizontal=True)
        a1.legend(fontsize=7, frameon=False, loc="upper left", ncol=2)
        a1.set_title(f"{sym}: 120d price, GEX levels and volume profile", fontsize=9, color="#888")
        a1.tick_params(axis="x", rotation=30)
        if vp is not None and vp.bins:
            px, vol = zip(*vp.bins)
            step = (px[1] - px[0]) if len(px) > 1 else 1
            inside = [vp.val <= p <= vp.vah for p in px]
            a2.barh(px, vol, height=step * 0.9, color=["#4a7bd0" if i else "#9ab" for i in inside])
            for v, name in ((vp.poc, "POC"), (vp.vah, "VAH"), (vp.val, "VAL")):
                if v:
                    a2.axhline(v, color="#555", lw=0.8, ls=":")
                    a2.text(max(vol) * 0.98, v, name, fontsize=6, ha="right", va="bottom", color="#888")
            a2.set_xticks([])
        for a in (a1, a2):
            _style(a)
        return _b64(fig)
    except Exception as e:
        log.info("price chart %s failed: %s", sym, e)
        return None


def payoff_chart(c, today) -> str | None:
    """P/L per contract at the first expiry vs the underlying (strategy-engine idea). Later legs (calendars,
    diagonals) are valued with Black-Scholes at their own IV, so that part of the curve is an estimate."""
    if plt is None or not c.legs or not c.spot:
        return None
    try:
        import numpy as np

        from .strategies.base import pnl_at

        first = min(l.expiry for l in c.legs)
        ks = [l.strike for l in c.legs]
        lo, hi = min(ks + [c.spot]), max(ks + [c.spot])
        pad = max((hi - lo) * 0.6, c.spot * 0.08)
        g = np.linspace(max(lo - pad, 0.01), hi + pad, 400)
        p = pnl_at(c.legs, c.net, g, first, today) * 100
        fig, ax = plt.subplots(figsize=(6.2, 2.6))
        ax.plot(g, p, color=C_NET, lw=1.4)
        ax.fill_between(g, p, 0, where=p >= 0, color=C_CALL, alpha=0.18, interpolate=True)
        ax.fill_between(g, p, 0, where=p < 0, color=C_PUT, alpha=0.18, interpolate=True)
        ax.axhline(0, color="#888", lw=0.8)
        ax.axvline(c.spot, color=C_SPOT, lw=1.2, label=f"spot {c.spot:g}")
        for b in c.breakevens:
            ax.axvline(b, color="#999", lw=0.8, ls=":")
        ax.set_ylabel("$ per contract", fontsize=8, color="#888")
        late = any(l.expiry != first for l in c.legs)
        ax.set_title(f"{c.symbol} {c.label}: P/L at {first}" + (" (later legs: BS estimate)" if late else "")
                     + ", before costs", fontsize=9, color="#888")
        ax.legend(fontsize=7, frameon=False, loc="best")
        _style(ax)
        return _b64(fig)
    except Exception as e:
        log.info("payoff chart %s failed: %s", c.symbol, e)
        return None


def equity_chart(trades) -> str | None:
    """Cumulative P/L by exit date, one line per strategy (closed backtest trades; SYNTHETIC)."""
    closed = [t for t in trades if t.reason != "open"]
    if plt is None or not closed:
        return None
    try:
        df = pd.DataFrame([{"strategy": t.strategy, "exit": pd.Timestamp(t.exit), "pnl": t.pnl} for t in closed])
        fig, ax = plt.subplots(figsize=(7.5, 3.2))
        for name, g in df.sort_values("exit").groupby("strategy", sort=False):
            ax.plot(g["exit"], g["pnl"].cumsum(), lw=1.3, label=name)
        ax.axhline(0, color="#888", lw=0.8)
        ax.set_ylabel("cumulative $ (1 contract)", fontsize=8, color="#888")
        ax.set_title("SYNTHETIC backtest: cumulative P/L by exit date", fontsize=9, color="#888")
        ax.legend(fontsize=7, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=5)
        ax.tick_params(axis="x", rotation=30)
        _style(ax)
        return _b64(fig)
    except Exception as e:
        log.info("equity chart failed: %s", e)
        return None
