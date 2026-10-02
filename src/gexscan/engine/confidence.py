"""Confidence score (spec §5): 0-100, weights from config.yaml engine.weights.

Each category scores 0..1 as a 50/50 blend of
  * gate credit: mean credit of that category's gate decisions (pass 1, fail 0, unknown 0.5), and
  * a metric that grades HOW strongly the edge is there (IV rank / VRP size, credit/width, tent placement, ...).
A category with neither scores 0.5 (no information). Points = weight x score. Penalties are then subtracted:
flow against the trade, a binary event in the window, stale data. Buckets: Low < 40 <= Medium <= 70 < High.
Proxy (free) flow is shrunk toward neutral before it is scored, so it can't move the total much.
Win rate is never an input: ranking is confidence, then liquidity, then credit/width.
"""
from __future__ import annotations

import datetime as dt

from ..strategies.base import Candidate, Confidence

CATEGORIES = ("regime", "iv_richness", "structure", "positioning", "flow", "liquidity", "event")
DIR_SIGN = {"bullish": 1.0, "neutral_bullish": 0.5, "bearish": -1.0, "neutral_bearish": -0.5, "neutral": 0.0}


def _c(x: float | None) -> float | None:
    return None if x is None else round(min(max(float(x), 0.0), 1.0), 3)


def bucket(score: float, sheet: dict) -> str:
    b = sheet.get("confidence", {}).get("buckets", {})
    return "Low" if score < b.get("low_below", 40) else "High" if score > b.get("high_above", 70) else "Medium"


def _metrics(c: Candidate, book, ctx) -> dict[str, tuple[float | None, str]]:
    nv, m = ctx.nv, c.metrics
    out: dict[str, tuple[float | None, str]] = {}
    crush = c.strategy == "earnings_crush"

    # iv richness
    if crush and nv and nv.move_ratio:
        out["iv_richness"] = (_c((nv.move_ratio - 1) / 0.6), f"implied/historical move {nv.move_ratio:.2f}x")
    elif nv and nv.iv_rank is not None:
        if c.vol_view == "long":
            out["iv_richness"] = (_c((70 - nv.iv_rank) / 60), f"IVR {nv.iv_rank:.0f}: long vega wants a cheaper back month")
        else:
            parts = [_c((nv.iv_rank - 20) / 60)]
            vrp = [v for v in (nv.vrp_c2c, nv.vrp_park) if v is not None]
            if vrp:
                parts.append(_c(min(vrp) / 10))
            s = sum(parts) / len(parts)
            out["iv_richness"] = (_c(s), f"IVR {nv.iv_rank:.0f}" + (f", VRP {min(vrp):+.1f} pts" if vrp else ""))

    # structure
    if m.get("debit_to_width") is not None:   # debit verticals: a cheaper spread pays more per $ risked
        out["structure"] = (_c((0.70 - m["debit_to_width"]) / 0.40),
                            f"debit/width {m['debit_to_width']:.0%} (reward/risk {m.get('reward_to_risk')})")
    elif c.kind == "debit" and nv:
        xs = [x for x in (_c(nv.ff / 0.4) if nv.ff is not None else None,
                          _c((nv.ts_ratio - 0.97) / 0.10) if nv.ts_ratio else None,
                          _c((nv.front_pump - 1) / 0.30) if nv.front_pump else None) if x is not None]
        if xs:
            out["structure"] = (max(xs), f"FF {nv.ff}, front/back {nv.ts_ratio}, earnings pump {nv.front_pump}")
    elif m.get("credit_to_width") is not None:
        mn = m.get("min_credit_to_width") or 0.25
        tgt = m.get("target_credit_to_width")
        if tgt:   # floor -> 0.4, the ~1/3 target -> ~0.75, 1.2x target -> 1.0
            s = 0.4 + 0.6 * _c((m["credit_to_width"] - mn) / max(1.2 * tgt - mn, 1e-6))
            out["structure"] = (s, f"credit/width {m['credit_to_width']:.0%} vs target {tgt:.0%} (floor {mn:.0%})")
        else:
            out["structure"] = (_c(m["credit_to_width"] / (1.5 * mn)), f"credit/width {m['credit_to_width']:.0%} vs min {mn:.0%}")
    elif c.strategy == "csp" and nv and nv.rr25 is not None:
        out["structure"] = (_c(nv.rr25 / 6), f"25d risk reversal {nv.rr25:+.1f} pts (put skew pays the put seller)")

    # positioning: short strikes outside the expected-move tent
    em = m.get("expected_move")
    if em and c.kind == "credit" and not (crush and m.get("vehicle") == "iron_fly"):
        shorts = [l for l in c.legs if l.action == "SELL"]
        ok = [(l.strike <= em["lower"]) if l.type == "P" else (l.strike >= em["upper"]) for l in shorts]
        if ok:
            out["positioning"] = (1.0 if all(ok) else 0.5 if any(ok) else 0.2,
                                  f"{sum(ok)}/{len(ok)} short strikes outside the {em['lower']:g}-{em['upper']:g} tent")

    # liquidity
    if m.get("liquidity_score") is not None:
        out["liquidity"] = (_c(m["liquidity_score"]), f"leg liquidity {m['liquidity_score']:.2f}")

    # flow alignment
    fs = ctx.fs
    if fs is not None:
        sgn = DIR_SIGN.get(c.direction, 0.0)
        s = 0.5 + 0.5 * sgn * fs.bias if sgn else 1 - min(abs(fs.bias), 1.0)
        if fs.approx:
            s = 0.5 + (s - 0.5) * 0.6
        out["flow"] = (_c(s), f"flow bias {fs.bias:+.2f}" + (" (PROXY, shrunk to neutral)" if fs.approx else ""))

    # event cleanliness (news + macro)
    st = book.states(c.symbol)
    ev = []
    if "news" in st:
        ev.append({"clean": 1.0, "unknown": 0.5, "red_flag": 0.2, "binary": 0.0}.get(st["news"].state, 0.5))
    if "macro" in st:
        ev.append({"clear": 1.0, "unknown": 0.5, "event_near": 0.3}.get(st["macro"].state, 0.5))
    if ev:
        out["event"] = (_c(sum(ev) / len(ev)), f"news {st.get('news').state if 'news' in st else '-'}, "
                                                f"macro {st.get('macro').state if 'macro' in st else '-'}")
    return out


def _penalties(c: Candidate, book, ctx, cfg: dict, sheet: dict) -> list[dict]:
    pen = cfg["engine"].get("penalties", {})
    out = []
    fs = ctx.fs
    th = sheet["signals"]["flow"].get("bias_at", 0.20)
    if fs is not None:
        sgn = DIR_SIGN.get(c.direction, 0.0)
        against = (sgn and sgn * fs.bias <= -th) or (not sgn and abs(fs.bias) >= 2 * th)
        if against:
            p = pen.get("flow_against", 8) * (0.6 if fs.approx else 1.0)
            out.append({"reason": f"flow {fs.label} ({fs.bias:+.2f}) against a {c.direction or 'neutral'} trade"
                                  + (" [PROXY]" if fs.approx else ""), "points": round(p, 1)})
    st = book.states(c.symbol)
    binary = []
    if st.get("macro") and st["macro"].state == "event_near":
        binary.append(st["macro"].detail)
    if st.get("news") and st["news"].state == "binary":
        binary.append(st["news"].detail)
    reg = book.market_regime
    if reg is not None and c.time_exit:
        hold_end = dt.date.fromisoformat(c.time_exit)
        if (hold_end - book.today).days <= 10:
            short = [e for e in reg.events if dt.date.fromisoformat(str(e["date"])) <= hold_end]
            if short:
                binary.append("short hold with " + ", ".join(f"{e['event']} {e['date']}" for e in short))
    if binary:
        out.append({"reason": "binary event in the trade window: " + "; ".join(binary), "points": pen.get("binary_event", 10)})
    if not ctx.fresh_ok:
        out.append({"reason": f"stale chain (as of {ctx.as_of})", "points": pen.get("stale_data", 5)})
    rp = c.metrics.get("roundtrip_penalty")
    if rp is not None:  # F3: wide markets relative to the credit
        out.append({"reason": f"round-trip bid-ask {c.metrics.get('roundtrip_pct_of_net', 0):.0%} of the credit",
                    "points": round(pen.get("wide_roundtrip", 6) * (0.5 + 0.5 * rp), 1)})
    return out


def score(c: Candidate, book, cfg: dict, sheet: dict) -> Confidence:
    ctx = book.contexts[c.symbol]
    w = cfg["engine"].get("weights", {})
    credits = c.gates.credits() if c.gates else {}
    mets = _metrics(c, book, ctx)
    sub, total = {}, 0.0
    for cat in CATEGORIES:
        wt = float(w.get(cat, 0))
        g = credits.get(cat)
        gs = round(sum(g) / len(g), 3) if g else None
        ms, note = mets.get(cat, (None, ""))
        parts = [x for x in (gs, ms) if x is not None]
        s = sum(parts) / len(parts) if parts else 0.5
        pts = round(wt * s, 1)
        total += pts
        sub[cat] = {"points": pts, "max": wt, "score": round(s, 3), "gate": gs, "metric": ms,
                    "gates": len(g or []), "notes": note or ("no gates or metric: scored neutral" if not parts else "")}
    pens = _penalties(c, book, ctx, cfg, sheet)
    total -= sum(p["points"] for p in pens)
    total = round(min(max(total, 0.0), 100.0), 1)
    return Confidence(total, bucket(total, sheet), sub, pens)
