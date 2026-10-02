"""Ticker overview for the web app (spec v2.2 §5.3): profile, performance, options picture, catalysts, risks
and a rules-based outlook. Every number is from data with its source; the outlook is a read of that data,
not a forecast.

    overview(sym, src, cfg, sheet, md, regime, store) -> dict (JSON-ready)
    quote(sym, src) -> dict (the small card on a theme page)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
OUTLOOK_NOTE = "A rules-based read of the data, not a forecast."
WINDOWS = {"1W": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252}


def _f(x, nd=2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


# ---------------------------------------------------------------------------------------------------
# profile

PROFILE_KEYS = {"longName": "name", "sector": "sector", "industry": "industry", "marketCap": "market_cap",
                "fullTimeEmployees": "employees", "longBusinessSummary": "summary", "website": "website"}


def _short(text: str, n: int = 3) -> str:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(parts[:n])


def profile(sym: str, src, state_dir: Path | None) -> dict:
    """Fixtures: profiles.json. Live: yfinance `info`, cached once a day under state/cache/profiles."""
    if src.fixtures:
        p = src.fixtures / "profiles.json"
        d = (json.loads(p.read_text()).get(sym) if p.exists() else None) or {}
        return {"name": sym, **d, "source": "fixture (synthetic)"}
    cache = Path(state_dir or ".") / "cache" / "profiles" / f"{sym}_{src.today.isoformat()}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    out = {"name": sym, "source": "unavailable"}
    try:
        import yfinance as yf
        info = yf.Ticker(sym).info or {}
        out = {v: info.get(k) for k, v in PROFILE_KEYS.items() if info.get(k) is not None}
        out.setdefault("name", info.get("shortName") or sym)
        out["summary"] = _short(out.get("summary", ""))
        out["source"] = f"yfinance, {src.today.isoformat()}"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out))
    except Exception as e:  # profile is decoration: never fail the page for it
        log.warning("profile %s: %s", sym, type(e).__name__)
    return out


# ---------------------------------------------------------------------------------------------------
# performance

def _ret(c: pd.Series, n: int) -> float | None:
    return _f((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 1) if len(c) > n else None


def _ytd(c: pd.Series, today: dt.date) -> float | None:
    prior = c[c.index < pd.Timestamp(dt.date(today.year, 1, 1))]
    return _f((c.iloc[-1] / prior.iloc[-1] - 1) * 100, 1) if len(prior) else None


def performance(hist: pd.DataFrame, bench: pd.DataFrame | None, tech, today: dt.date) -> dict:
    if hist is None or hist.empty:
        return {}
    c = hist["close"].astype(float)
    b = bench["close"].astype(float) if bench is not None and len(bench) else None
    rows = []
    for k, n in WINDOWS.items():
        rows.append({"window": k, "ret": _ret(c, n), "spy": _ret(b, n) if b is not None else None})
    rows.append({"window": "YTD", "ret": _ytd(c, today), "spy": _ytd(b, today) if b is not None else None})
    hi, lo = float(hist["high"].astype(float).tail(252).max()), float(hist["low"].astype(float).tail(252).min())
    beta = None
    if b is not None:
        j = pd.concat([np.log(c).diff(), np.log(b).diff()], axis=1, join="inner").dropna().tail(252)
        if len(j) > 60 and j.iloc[:, 1].var() > 0:
            beta = _f(j.iloc[:, 0].cov(j.iloc[:, 1]) / j.iloc[:, 1].var(), 2)
    peak = c.tail(252).cummax()
    tail = c.tail(252)
    s50, s200 = c.rolling(50).mean().tail(252), c.rolling(200).mean().tail(252)
    return {
        "returns": rows, "high52": _f(hi), "low52": _f(lo),
        "pos52": _f((c.iloc[-1] - lo) / (hi - lo), 3) if hi > lo else None,
        "from_high_pct": _f((c.iloc[-1] / hi - 1) * 100, 1), "max_drawdown_1y_pct": _f(((tail / peak) - 1).min() * 100, 1),
        "rv20": _f(tech.rv20 if tech else None, 3), "atr_pct": _f(tech.atr_pct if tech else None, 2),
        "rsi14": _f(tech.rsi14 if tech else None, 1), "beta": beta,
        "series": {"dates": [str(x)[:10] for x in tail.index], "close": [_f(x) for x in tail],
                   "sma50": [_f(x) for x in s50], "sma200": [_f(x) for x in s200]},
        "source": hist.attrs.get("source", ""), "as_of": str(c.index[-1])[:10],
    }


# ---------------------------------------------------------------------------------------------------
# options picture, catalysts, risks, outlook

def monthly_point(nv):
    """The standard monthly (third Friday) 20-60 DTE, else the expiry nearest 30 DTE."""
    term = [t for t in (nv.term if nv else []) if t.atm_iv]
    if not term:
        return None

    def third_friday(e):
        d = dt.date.fromisoformat(e)
        return d.weekday() == 4 and 15 <= d.day <= 21

    m = [t for t in term if 20 <= t.dte <= 60 and third_friday(t.expiry)]
    return m[0] if m else min(term, key=lambda t: abs(t.dte - 30))


def options_picture(ctx) -> dict:
    nv, lv = ctx.nv, ctx.levels
    mp = monthly_point(nv)
    front = next((t for t in nv.term if t.atm_iv), None) if nv else None
    shape = None
    if nv and nv.ts_ratio:
        shape = "backwardation (front rich)" if nv.ts_ratio > 1.03 else "contango" if nv.ts_ratio < 0.97 else "flat"
    return {
        "atm_iv": _f(nv.atm_iv, 4) if nv else None, "iv_rank": _f(nv.iv_rank, 1) if nv else None,
        "iv_percentile": _f(nv.iv_percentile, 1) if nv else None, "iv_rank_source": nv.iv_rank_source if nv else "",
        "rv20": _f(nv.rv20, 4) if nv else None, "vrp": _f(nv.vrp_c2c, 1) if nv else None,
        "em_front": {"expiry": front.expiry, "dte": front.dte, "pct": _f(front.em_pct, 2), "lower": _f(front.lower),
                     "upper": _f(front.upper)} if front else None,
        "em_monthly": {"expiry": mp.expiry, "dte": mp.dte, "pct": _f(mp.em_pct, 2), "lower": _f(mp.lower),
                       "upper": _f(mp.upper)} if mp else None,
        "term": [{"expiry": t.expiry, "dte": t.dte, "atm_iv": _f(t.atm_iv, 4), "em_pct": _f(t.em_pct, 2)}
                 for t in (nv.term if nv else []) if t.atm_iv],
        "term_shape": shape, "ts_ratio": _f(nv.ts_ratio, 3) if nv else None, "rr25": _f(nv.rr25, 1) if nv else None,
        "gex_regime": lv.regime, "net_gex": _f(lv.net_gex, 0), "call_wall": lv.call_wall, "put_wall": lv.put_wall,
        "gamma_flip": _f(lv.gamma_flip), "pc_oi_ratio": _f(lv.pc_oi_ratio, 2), "max_pain": lv.max_pain_near,
        "atm_spread_pct": _f(lv.atm_spread_pct, 3),
    }


def catalysts(ctx, regime, monthly_expiry: str | None) -> dict:
    info, nv = ctx.info, ctx.nv
    earn = None
    if info and info.next_earnings:
        earn = {"date": info.next_earnings.isoformat(), "timing": info.timing, "days": info.days_to_earnings,
                "implied_move_pct": _f(nv.implied_move_pct, 2) if nv else None,
                "hist_move_pct": _f(info.mean_abs_move_pct, 2), "n_moves": info.n_moves,
                "move_ratio": _f(nv.move_ratio, 2) if nv else None, "source": info.earnings_source,
                "inside_monthly": bool(monthly_expiry and info.next_earnings.isoformat() <= monthly_expiry)}
    macro = []
    for e in (regime.events if regime else []):
        if not monthly_expiry or str(e.get("date", "")) <= monthly_expiry:
            macro.append({"event": e.get("event"), "date": str(e.get("date")), "days": e.get("days")})
    return {"earnings": earn,
            "ex_dividend": {"date": info.ex_div_date.isoformat(), "dividend": info.dividend, "source": info.div_source}
            if info and info.ex_div_date else None,
            "macro": macro, "news": list(ctx.news.catalysts) if ctx.news else []}


def risks(ctx, perf: dict, opt: dict, cat: dict) -> list[dict]:
    t, out = ctx.tech, []

    def add(key, label, value, level="warn"):
        out.append({"key": key, "label": label, "value": value, "level": level})

    e = cat.get("earnings")
    if e and e.get("inside_monthly"):
        add("earnings_gap", f"Earnings on {e['date']} falls inside the monthly: a gap risk",
            f"implied ±{e['implied_move_pct']}%" if e.get("implied_move_pct") else e["date"], "high")
    if t and t.rsi14 and t.rsi14 > 70:
        add("stretched_rsi", "Stretched: RSI above 70", f"RSI {t.rsi14:.0f}")
    if t and t.sma50 and t.close > t.sma50 * 1.15:
        add("stretched_sma", "Stretched: more than 15% above the 50-day average", f"{(t.close / t.sma50 - 1) * 100:+.0f}%")
    if t and t.sma200 and t.close < t.sma200:
        add("downtrend", "In a downtrend: below the 200-day average", f"{(t.close / t.sma200 - 1) * 100:+.0f}%")
    if opt.get("gex_regime") == "negative_gamma":
        add("neg_gamma", "Negative-gamma regime: dealer hedging can extend moves", f"net GEX {opt.get('net_gex'):,.0f}")
    if perf.get("beta") and perf["beta"] > 1.5:
        add("beta", "High beta to SPY (above 1.5)", f"beta {perf['beta']:.2f}")
    ivr = opt.get("iv_rank")
    if ivr is not None and ivr > 80:
        add("iv_rich", "IV rank above 80: big moves are already priced in", f"IV rank {ivr:.0f}")
    elif ivr is not None and ivr < 20:
        add("iv_cheap", "IV rank below 20: premium is thin for sellers", f"IV rank {ivr:.0f}", "info")
    sp = opt.get("atm_spread_pct")
    if sp is not None and sp > 0.10:
        add("thin", "Thin options: wide bid-ask near the money", f"{sp:.0%} of mid")
    if ctx.news and ctx.news.red_flags:
        add("news", "News red flags", ", ".join(ctx.news.red_flags[:4]), "high")
    if perf.get("from_high_pct") is not None and perf["from_high_pct"] < -30:
        add("drawdown", "Deep drawdown: more than 30% off the 52-week high", f"{perf['from_high_pct']:+.0f}%")
    return out


def outlook(ctx, perf: dict, opt: dict) -> dict:
    t, reasons, score = ctx.tech, [], 0.0
    if t and t.sma50 and t.sma200:
        if t.close > t.sma50 > t.sma200:
            score += 1; reasons.append(("+", "Trend: price above the 50-day, which is above the 200-day"))
        elif t.close < t.sma50 < t.sma200:
            score -= 1; reasons.append(("-", "Trend: price below the 50-day, which is below the 200-day"))
        else:
            reasons.append(("=", "Trend: averages mixed"))
    if t and t.rsi14:
        if 50 <= t.rsi14 <= 70:
            score += 0.5; reasons.append(("+", f"Momentum: RSI {t.rsi14:.0f} (firm, not stretched)"))
        elif t.rsi14 > 70:
            reasons.append(("=", f"Momentum: RSI {t.rsi14:.0f} (stretched; pullbacks are common)"))
        elif t.rsi14 < 40:
            score -= 0.5; reasons.append(("-", f"Momentum: RSI {t.rsi14:.0f} (weak)"))
    rel = next((r for r in perf.get("returns", []) if r["window"] == "1M"), None)
    if rel and rel["ret"] is not None and rel["spy"] is not None:
        d = rel["ret"] - rel["spy"]
        if d > 3:
            score += 0.5; reasons.append(("+", f"Relative strength: {d:+.1f} pts vs SPY over 1 month"))
        elif d < -3:
            score -= 0.5; reasons.append(("-", f"Relative strength: {d:+.1f} pts vs SPY over 1 month"))
    spot, flip = ctx.chain.spot, opt.get("gamma_flip")
    if opt.get("gex_regime") == "positive_gamma" and flip and spot > flip:
        score += 0.5; reasons.append(("+", f"Positioning: positive gamma, spot above the {flip:g} flip (dampened moves)"))
    elif opt.get("gex_regime") == "negative_gamma":
        score -= 0.5; reasons.append(("-", "Positioning: negative gamma (moves can extend)"))
    if opt.get("iv_rank") is not None and opt["iv_rank"] > 80:
        score -= 0.5; reasons.append(("-", f"Vol: IV rank {opt['iv_rank']:.0f} (stress priced in)"))
    label = "Constructive" if score >= 1.5 else "Cautious" if score <= -1.0 else "Neutral"
    em = opt.get("em_monthly")
    return {"label": label, "score": score, "reasons": [{"sign": s, "text": x} for s, x in reasons],
            "range": {"expiry": em["expiry"], "lower": em["lower"], "upper": em["upper"], "pct": em["pct"]} if em else None,
            "note": OUTLOOK_NOTE}


def _news(ctx) -> dict | None:
    if ctx.news is None:
        return None
    n = ctx.news
    return {"items": [{"title": i.title, "source": i.source, "provider": i.provider, "published": i.published,
                       "tags": list(i.tags)} for i in n.items[:8]],
            "sentiment": n.sentiment_label, "summary": n.summary, "summary_source": n.summary_source,
            "providers": list(n.providers)}


def overview(sym: str, src, cfg: dict, sheet: dict, md, regime, store, state_dir: Path | None = None) -> dict:
    from ..engine.recommend import build_context
    ctx = build_context(sym, src, cfg, sheet, None, md, iv_store=store)
    if ctx.chain is None:
        return {"symbol": sym, "errors": ctx.errors or [f"{sym}: no data"]}
    perf = performance(ctx.hist, src.benchmark(), ctx.tech, src.today)
    opt = options_picture(ctx)
    mp = monthly_point(ctx.nv)
    cat = catalysts(ctx, regime, mp.expiry if mp else None)
    prof = profile(sym, src, state_dir)
    lv = ctx.levels
    return {
        "symbol": sym, "today": src.today.isoformat(), "spot": _f(ctx.chain.spot), "as_of": ctx.as_of,
        "profile": prof, "performance": perf, "options": opt, "catalysts": cat,
        "risks": risks(ctx, perf, opt, cat), "outlook": outlook(ctx, perf, opt), "news": _news(ctx),
        "levels": {"call_wall": lv.call_wall, "put_wall": lv.put_wall, "gamma_flip": _f(lv.gamma_flip)},
        "fresh": {**ctx.fresh, "profile": prof.get("source", "")}, "errors": ctx.errors,
        "corr_spy": ctx.corr_spy,
    }


def quote(sym: str, src) -> dict:
    """Spot, 1D / 1M change, 3 months of closes, next earnings. Cheap enough to call per theme card."""
    hist = src.daily(sym)
    out: dict = {"symbol": sym, "source": hist.attrs.get("source", "") if hist is not None else ""}
    try:
        ch = src.chain(sym)
        out.update(spot=_f(ch.spot), as_of=ch.as_of)
    except Exception as e:
        out["errors"] = [f"chain unavailable ({type(e).__name__})"]
    if hist is not None and len(hist):
        c = hist["close"].astype(float)
        out.setdefault("spot", _f(c.iloc[-1]))
        out.update(chg1d=_ret(c, 1), chg1m=_ret(c, 21), closes=[_f(x) for x in c.tail(63)],
                   bars_to=str(c.index[-1])[:10])
    try:
        info = src.events.get(sym, hist) if hasattr(src, "events") else None
        if info and info.next_earnings:
            out["next_earnings"] = {"date": info.next_earnings.isoformat(), "days": info.days_to_earnings,
                                    "timing": info.timing}
    except Exception:
        pass
    return out


__all__ = ["OUTLOOK_NOTE", "catalysts", "monthly_point", "options_picture", "outlook", "overview", "performance",
           "profile", "quote", "risks"]
