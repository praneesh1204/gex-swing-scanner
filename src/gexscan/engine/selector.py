"""Selector (spec §6): run every strategy plugin, score, attach the rationale, filter, rank, cap per ticker.

Ranking is confidence, then leg liquidity, then credit/width. Never win rate (a high-PoP short-premium
trade can be a bad trade). Required gates are already hard filters inside the plugins; here the extra
filters are the confidence floor, defined-risk-only (unless allowed) and the caller's symbol/strategy picks.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..strategies import Candidate, load_strategies
from . import confidence, rationale
from .universe import Universe, correlated_warning


@dataclass
class Filters:
    symbols: list[str] | None = None
    strategies: list[str] | None = None
    min_confidence: float | None = None
    top_n: int | None = None
    max_per_ticker: int | None = None
    allow_undefined_risk: bool | None = None
    directions: list[str] | None = None        # e.g. ["bullish", "neutral"]
    vol_view: str | None = None                # "short" / "long"
    dte_min: int | None = None                 # v2.2: only ideas whose expiry is inside this DTE window
    dte_max: int | None = None
    themes: list[str] | None = None            # v2.2: limit the run to these universe themes


def resolve(f: Filters, cfg: dict, sheet: dict) -> dict:
    e = cfg["engine"]
    floor = float(sheet.get("confidence", {}).get("floor", 40))
    return {"min_confidence": max(float(f.min_confidence if f.min_confidence is not None else e["min_confidence"]), floor),
            "floor": floor,
            "top_n": int(f.top_n or e["top_n"]),
            "max_per_ticker": int(f.max_per_ticker or e["max_per_ticker"]),
            "allow_undefined_risk": bool(e["allow_undefined_risk"] if f.allow_undefined_risk is None else f.allow_undefined_risk)}


def _rank_key(c: Candidate):
    m = c.metrics
    return (c.confidence.score if c.confidence else 0, m.get("liquidity_score") or 0, m.get("credit_to_width") or 0)


def _drop(c: Candidate, why: str) -> dict:
    return {"symbol": c.symbol, "strategy": c.strategy, "summary": c.summary(), "why": why,
            "confidence": c.confidence.score if c.confidence else None}


def select(universe, book, sheet: dict, cfg: dict, f: Filters, uni: Universe | None = None,
           default_run: bool = False) -> tuple[list[Candidate], list[dict], dict]:
    """`uni` tags every idea with its themes. On the default run (no -s / --theme) it also applies the outside
    bar and the theme caps; an explicit pick is never held back by them."""
    r = resolve(f, cfg, sheet)
    enforce = bool(cfg.get("risk", {}).get("enforce_budget", False))
    syms = {s.upper() for s in f.symbols} if f.symbols else None
    names = [u for u in universe if syms is None or u.symbol in syms]
    cands: list[Candidate] = []
    for strat in load_strategies(sheet, cfg, f.strategies):
        cands += strat.evaluate(names, book)
    kept, dropped = [], []
    for c in cands:
        c.confidence = confidence.score(c, book, cfg, sheet)
        c.rationale = rationale.build(c, book)
        if c.risk == "undefined" and "UNDEFINED RISK" not in c.flags:
            c.flags.insert(0, "UNDEFINED RISK")
        if uni is not None:
            c.metrics["themes"] = uni.themes_of(c.symbol)
            if not c.metrics["themes"]:
                c.metrics["outside"] = True
        if c.risk == "undefined" and not r["allow_undefined_risk"]:
            dropped.append(_drop(c, "undefined risk (opt in with --allow-undefined)"))
        elif f.dte_min is not None and c.dte < f.dte_min:
            dropped.append(_drop(c, f"{c.dte} DTE < the {f.dte_min} DTE minimum"))
        elif f.dte_max is not None and c.dte > f.dte_max:
            dropped.append(_drop(c, f"{c.dte} DTE > the {f.dte_max} DTE maximum"))
        elif enforce and c.metrics.get("over_budget"):
            dropped.append(_drop(c, "1 contract risks more than the per-trade budget (risk.enforce_budget)"))
        elif f.directions and c.direction not in f.directions:
            dropped.append(_drop(c, f"direction {c.direction} not in {f.directions}"))
        elif f.vol_view and c.vol_view != f.vol_view:
            dropped.append(_drop(c, f"vol view {c.vol_view} != {f.vol_view}"))
        elif c.confidence.score < r["min_confidence"]:
            dropped.append(_drop(c, f"confidence {c.confidence.score:.0f} < {r['min_confidence']:.0f}"))
        else:
            kept.append(c)
    kept.sort(key=_rank_key, reverse=True)
    gated = uni is not None and default_run and uni.mode != "off"
    out, outside, per, per_theme = [], [], {}, {}
    for c in kept:
        if per.get(c.symbol, 0) >= r["max_per_ticker"]:
            dropped.append(_drop(c, f"max {r['max_per_ticker']} ideas per ticker (a higher-ranked one was kept)"))
            continue
        if gated and c.metrics.get("outside"):
            if uni.mode == "only" or not uni.outside_allow:
                dropped.append(_drop(c, "outside the universe (universe.mode only)"))
            elif c.confidence.score < uni.outside_min_confidence:
                dropped.append(_drop(c, f"outside the universe: confidence {c.confidence.score:.0f} < the "
                                        f"{uni.outside_min_confidence:g} bar"))
            elif len(outside) >= uni.outside_max_per_day:
                dropped.append(_drop(c, f"outside the universe: max {uni.outside_max_per_day} a day"))
            else:
                if "OUTSIDE UNIVERSE" not in c.flags:
                    c.flags.append("OUTSIDE UNIVERSE")
                per[c.symbol] = per.get(c.symbol, 0) + 1
                outside.append(c)
            continue
        if gated:
            full = [k for k in c.metrics.get("themes", []) if per_theme.get(k, 0) >= uni.max_per_theme]
            if full:
                dropped.append(_drop(c, f"theme cap ({full[0]} {uni.max_per_theme}/{uni.max_per_theme})"))
                continue
        if len(out) >= r["top_n"]:
            dropped.append(_drop(c, f"outside the top {r['top_n']}"))
            continue
        per[c.symbol] = per.get(c.symbol, 0) + 1
        for k in c.metrics.get("themes", []):
            per_theme[k] = per_theme.get(k, 0) + 1
        out.append(c)
    out += outside        # their own section: never in a universe slot
    if uni is not None:
        w = correlated_warning(out, uni)
        if w:
            r["correlated_warning"] = w
    for d in dropped:
        book.log(d["symbol"], d["strategy"], f"dropped after scoring: {d['why']}")
    return out, dropped, r
