"""The §3 ruleset: numbers -> named states (from config/signal_sheet.yaml) -> gate decisions per strategy.

`classify_market` / `classify_name` turn raw signals into SignalState objects. `evaluate_gates` reads a
strategy's gate block from the sheet and returns a GateResult that records every decision, so
`gexscan explain` can print exactly why an idea was kept, scored down, or dropped.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from typing import Any

UNKNOWN = "unknown"


@dataclass
class SignalState:
    name: str
    state: str
    value: Any = None
    detail: str = ""
    source: str = ""
    as_of: str = ""
    approx: bool = False
    category: str = ""

    def to_dict(self):
        return asdict(self)

    def line(self) -> str:
        v = "" if self.value is None else f" ({self.value})"
        tag = " [PROXY]" if self.approx else ""
        return f"{self.name}: {self.state}{v}{tag} - {self.detail}" if self.detail else f"{self.name}: {self.state}{v}{tag}"


@dataclass
class GateDecision:
    gate: str
    kind: str                 # required / any_of / preferred / warn / trade
    outcome: str              # pass / fail / unknown / warn / ok
    state: str
    want: str
    category: str = ""
    credit: float | None = None   # 1 pass, 0 fail, unknown_credit when unknown (None for warn gates)
    detail: str = ""

    def to_dict(self):
        return asdict(self)

    def line(self) -> str:
        mark = {"pass": "PASS", "fail": "FAIL", "unknown": "UNKN", "warn": "WARN", "ok": " ok "}[self.outcome]
        return f"[{mark}] {self.kind:<9} {self.gate:<15} is {self.state:<14} want {self.want}" + \
               (f"  ({self.detail})" if self.detail else "")


@dataclass
class GateResult:
    strategy: str
    symbol: str
    passed: bool = True
    decisions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    failed: list = field(default_factory=list)

    def add(self, d: GateDecision) -> None:
        self.decisions.append(d)

    def reject(self, gate: str, why: str, category: str = "") -> None:
        """Candidate-level hard gate (earnings inside the trade, thin legs, ...)."""
        self.passed = False
        self.failed.append(f"{gate}: {why}")
        self.add(GateDecision(gate, "trade", "fail", "fail", "pass", category, 0.0, why))

    def credits(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for d in self.decisions:
            if d.credit is not None and d.category:
                out.setdefault(d.category, []).append(d.credit)
        return out

    def states_used(self) -> dict[str, str]:
        return {d.gate: d.state for d in self.decisions if d.kind != "trade"}

    def to_dict(self):
        return {"strategy": self.strategy, "symbol": self.symbol, "passed": self.passed,
                "failed": self.failed, "warnings": self.warnings, "decisions": [d.to_dict() for d in self.decisions]}


# ---------------------------------------------------------------------------------------------------
# classification

def _st(name, state, value=None, detail="", source="", as_of="", approx=False, sheet=None) -> SignalState:
    cat = ((sheet or {}).get("signals", {}).get(name, {}) or {}).get("category", "")
    return SignalState(name, state, value, detail, source, as_of, approx, cat)


def classify_market(m, sheet: dict, cfg: dict, today: dt.date) -> dict[str, SignalState]:
    """m: signals.market.MarketRegime"""
    s = sheet["signals"]
    out: dict[str, SignalState] = {}
    src = lambda k: m.sources.get(k, "")  # noqa: E731

    p = s["vix_ts"]
    if m.ts_ratio is None:
        out["vix_ts"] = _st("vix_ts", UNKNOWN, None, "VIX or VIX3M unavailable", sheet=sheet)
    else:
        st = "backwardation" if m.ts_ratio >= p["backwardation_at"] else "flat" if m.ts_ratio >= p["flat_from"] else "contango"
        out["vix_ts"] = _st("vix_ts", st, m.ts_ratio, f"VIX {m.vix:.2f} / VIX3M {m.vix3m:.2f}",
                            f"{src('VIX')} | {src('VIX3M')}", m.as_of, sheet=sheet)

    p = s["vix_level"]
    if m.vix is None:
        out["vix_level"] = _st("vix_level", UNKNOWN, None, "VIX unavailable", sheet=sheet)
    else:
        st = ("extreme" if m.vix > p["extreme_above"] else "stressed" if m.vix > p["stressed_above"]
              else "calm" if m.vix < p["calm_below"] else "normal")
        pct = f", {m.vix_pct_1y:.0f}th pct of 1y" if m.vix_pct_1y is not None else ""
        out["vix_level"] = _st("vix_level", st, round(m.vix, 2), f"VIX {m.vix:.2f}{pct}", src("VIX"), m.as_of, sheet=sheet)

    p = s["vvix"]
    if m.vvix is None:
        out["vvix"] = _st("vvix", UNKNOWN, None, "VVIX unavailable", sheet=sheet)
    else:
        spike = m.vvix >= p["spike_at"] or (m.vvix_5d_pct is not None and m.vvix_5d_pct >= p["spike_5d_pct"])
        st = "spike" if spike else "elevated" if m.vvix >= p["elevated_at"] else "normal"
        chg = f", {m.vvix_5d_pct:+.1f}% 5d" if m.vvix_5d_pct is not None else ""
        out["vvix"] = _st("vvix", st, round(m.vvix, 2), f"VVIX {m.vvix:.1f}{chg}", src("VVIX"), m.as_of, sheet=sheet)

    near = int(cfg.get("macro", {}).get("near_days", 3))
    if not m.events and not cfg.get("macro", {}).get("fomc_dates") and m.sources.get("calendar") in (None, "", "unavailable"):
        out["macro"] = _st("macro", UNKNOWN, None, "no FOMC/CPI calendar", sheet=sheet)
    else:
        close = [e for e in m.events if e["days"] <= near]
        if close:
            txt = ", ".join(f"{e['event']} {e['date']} ({e['days']}d)" for e in close)
            out["macro"] = _st("macro", "event_near", None, f"binary macro print within {near}d: {txt}",
                               m.sources.get("calendar", ""), m.as_of, sheet=sheet)
        else:
            nxt = ", ".join(f"{e['event']} {e['date']}" for e in m.events[:3]) or "none in 60d"
            out["macro"] = _st("macro", "clear", None, f"next: {nxt}", m.sources.get("calendar", ""), m.as_of, sheet=sheet)
    return out


def _in_window(level, spot, max_pct, above: bool) -> bool:
    if not level or not spot:
        return False
    d = (level / spot - 1) * 100 if above else (1 - level / spot) * 100
    return 0 < d <= max_pct


def classify_name(sym: str, nv, lv, tech, vp, fs, info, news, sheet: dict, today: dt.date) -> dict[str, SignalState]:
    """nv: NameVol, lv: Levels, tech: Technicals, vp: VolumeProfile|None, fs: FlowSignal, info: EventInfo,
    news: NewsReport|None"""
    s = sheet["signals"]
    out: dict[str, SignalState] = {}
    S = nv.spot
    as_of = nv.as_of
    chain_src = f"CBOE chain {as_of}"

    # IV rank / percentile
    p = s["iv_rank"]
    proxy = (nv.iv_rank_source or "").startswith("proxy")
    if nv.iv_rank is None:
        out["iv_rank"] = _st("iv_rank", UNKNOWN, None, "not enough IV or price history", sheet=sheet)
    else:
        r = nv.iv_rank
        st = "rich" if r > p["rich_above"] else "sell" if r > p["sell_above"] else "low" if r < p["low_below"] else "mid"
        out["iv_rank"] = _st("iv_rank", st, r, f"IVR {r:.0f} ({nv.iv_rank_source}), ATM IV {nv.atm_iv:.1%}",
                             chain_src, as_of, proxy, sheet=sheet)
    p = s["iv_percentile"]
    if nv.iv_percentile is None:
        out["iv_percentile"] = _st("iv_percentile", UNKNOWN, None, "not enough history", sheet=sheet)
    else:
        st = "elevated" if nv.iv_percentile >= p["elevated_at"] else "normal"
        out["iv_percentile"] = _st("iv_percentile", st, nv.iv_percentile, f"IVP {nv.iv_percentile:.0f} ({nv.iv_rank_source})",
                                   chain_src, as_of, proxy, sheet=sheet)

    # VRP (both estimators must agree for 'positive')
    p = s["vrp"]
    vals = [v for v in (nv.vrp_c2c, nv.vrp_park) if v is not None]
    if not vals:
        out["vrp"] = _st("vrp", UNKNOWN, None, "realized vol unavailable", sheet=sheet)
    else:
        if all(v >= p["positive_min_pts"] for v in vals):
            st = "positive"
        elif (nv.vrp_c2c is not None and nv.vrp_c2c <= p["negative_below_pts"]) or max(vals) <= p["negative_below_pts"]:
            st = "negative"
        else:
            st = "flat"
        det = f"IV {nv.atm_iv:.1%} vs RV20 c2c {nv.rv20:.1%}" if nv.rv20 else ""
        det += f" / Parkinson {nv.park20:.1%}" if nv.park20 else ""
        out["vrp"] = _st("vrp", st, round(min(vals), 1), det + f" -> VRP {', '.join(f'{v:+.1f}' for v in vals)} vol pts",
                         f"{chain_src}; daily bars", as_of, sheet=sheet)

    # term structure
    p = s["term_structure"]
    if nv.ts_ratio is None:
        out["term_structure"] = _st("term_structure", UNKNOWN, None, "need ~30 and ~60 DTE expiries", sheet=sheet)
    else:
        st = "backwardation" if nv.ts_ratio >= p["backwardation_at"] else "contango" if nv.ts_ratio <= p["contango_below"] else "flat"
        out["term_structure"] = _st("term_structure", st, nv.ts_ratio,
                                    f"{nv.front_expiry} IV {nv.front_iv:.1%} / {nv.back_expiry} IV {nv.back_iv:.1%}",
                                    chain_src, as_of, sheet=sheet)
    p = s["forward_factor"]
    if nv.ff is None:
        out["forward_factor"] = _st("forward_factor", UNKNOWN, None, nv.ff_note or "no usable front/back pair", sheet=sheet)
    else:
        st = "high" if nv.ff >= p["high_at"] else "normal"
        out["forward_factor"] = _st("forward_factor", st, nv.ff,
                                    f"{nv.ff_front}->{nv.ff_back}, forward IV {nv.ff_fwd_iv:.1%}", chain_src, as_of, sheet=sheet)

    # skew
    p = s["skew"]
    if nv.rr25 is None:
        out["skew"] = _st("skew", UNKNOWN, None, "25-delta IVs unavailable", sheet=sheet)
    else:
        rich_put = nv.rr25 >= p["put_rich_pts"] or (nv.put_skew_ratio or 0) >= p["put_rich_ratio"]
        st = "put_rich" if rich_put else "call_rich" if nv.rr25 <= p["call_rich_pts"] else "neutral"
        out["skew"] = _st("skew", st, nv.rr25, f"25d put {nv.put25:.1%} vs call {nv.call25:.1%} (RR {nv.rr25:+.1f} pts, "
                          f"put/ATM {nv.put_skew_ratio})", chain_src, as_of, sheet=sheet)

    # earnings
    p = s["earnings"]
    if info is not None and info.is_etf:
        out["earnings"] = _st("earnings", "none", None, "fund/index: no earnings", info.earnings_source, sheet=sheet)
    elif info is None or info.next_earnings is None or info.days_to_earnings is None:
        out["earnings"] = _st("earnings", UNKNOWN, None, "next earnings date unknown: verify before trading", sheet=sheet)
    else:
        d = info.days_to_earnings
        st = "clear" if d < 0 or d > p["horizon_days"] else "imminent" if d <= p["imminent_days"] else "upcoming"
        out["earnings"] = _st("earnings", st, d, f"{info.next_earnings} {info.timing} ({d}d)", info.earnings_source, sheet=sheet)

    p = s["earnings_front"]
    if nv.front_pump is None:
        st = "none" if out["earnings"].state in ("none", "clear") or nv.earn_expiry is None and out["earnings"].state != UNKNOWN else UNKNOWN
        out["earnings_front"] = _st("earnings_front", st, None, "no expiry holding a print" if st == "none" else "", sheet=sheet)
    else:
        st = "pumped" if nv.front_pump >= p["pumped_at"] else "normal"
        out["earnings_front"] = _st("earnings_front", st, nv.front_pump,
                                    f"IV of {nv.earn_expiry} (holds the print) / next expiry = {nv.front_pump:.2f}",
                                    chain_src, as_of, sheet=sheet)

    p = s["earnings_move"]
    if nv.move_ratio is None:
        why = ("no expiry holding a print" if nv.implied_move_pct is None else
               f"only {info.n_moves if info else 0} past prints (need {p.get('min_history', 4)})")
        out["earnings_move"] = _st("earnings_move", UNKNOWN if out["earnings"].state in ("imminent", "upcoming") else "none",
                                   None, why, sheet=sheet)
    else:
        st = "rich" if nv.move_ratio >= p["rich_at"] else "cheap" if nv.move_ratio <= p["cheap_at"] else "fair"
        out["earnings_move"] = _st("earnings_move", st, nv.move_ratio,
                                   f"implied {nv.implied_move_pct:.1f}% ({nv.earn_expiry} straddle) vs mean |move| "
                                   f"{nv.hist_move_pct:.1f}% over {info.n_moves} prints", chain_src, as_of, sheet=sheet)

    # positioning
    if lv is None:
        for k in ("gex", "range", "support", "resistance"):
            out[k] = _st(k, UNKNOWN, None, "no GEX levels", sheet=sheet)
    else:
        above_flip = (lv.gamma_flip is None and lv.flip_status == "no_boundary_positive") or \
                     (lv.gamma_flip is not None and S > lv.gamma_flip)
        pos = lv.regime == "positive_gamma" and above_flip
        flip = f"flip {lv.gamma_flip:g}" if lv.gamma_flip else lv.flip_status
        out["gex"] = _st("gex", "positive" if pos else "negative", round(lv.net_gex / 1e6, 1),
                         f"net GEX {lv.net_gex / 1e6:+.1f}M/1%, spot {S:g} vs {flip} (vol regime, not direction)",
                         chain_src, as_of, sheet=sheet)
        pr = s["range"]
        inside = (lv.put_wall is None or lv.put_wall <= S) and (lv.call_wall is None or S <= lv.call_wall)
        flat = tech is not None and tech.pct_vs_sma20 is not None and abs(tech.pct_vs_sma20) <= pr["max_abs_pct_vs_sma20"]
        if tech is None or tech.close is None:
            out["range"] = _st("range", UNKNOWN, None, "price history unavailable", sheet=sheet)
        else:
            rb = pos and inside and flat
            out["range"] = _st("range", "range_bound" if rb else "trending", None,
                               f"gamma {'+' if pos else '-'}, {'inside' if inside else 'outside'} walls "
                               f"{lv.put_wall}-{lv.call_wall}, {tech.pct_vs_sma20:+.1f}% vs SMA20", sheet=sheet)
        ps, pr2 = s["support"], s["resistance"]
        sup = [(n, v) for n, v in (("put wall", lv.put_wall), ("value-area low", getattr(vp, "val", None)))
               if _in_window(v, S, ps["max_dist_pct"], above=False)]
        res = [(n, v) for n, v in (("call wall", lv.call_wall), ("value-area high", getattr(vp, "vah", None)))
               if _in_window(v, S, pr2["max_dist_pct"], above=True)]
        out["support"] = _st("support", "below" if sup else "none", sup[0][1] if sup else None,
                             ", ".join(f"{n} {v:g} ({(1 - v / S) * 100:.1f}% below)" for n, v in sup) or "no wall/VAL within range",
                             chain_src, as_of, sheet=sheet)
        out["resistance"] = _st("resistance", "above" if res else "none", res[0][1] if res else None,
                                ", ".join(f"{n} {v:g} ({(v / S - 1) * 100:.1f}% above)" for n, v in res) or "no wall/VAH within range",
                                chain_src, as_of, sheet=sheet)

    # lean (trend + positioning)
    if tech is None or tech.close is None:
        out["lean"] = _st("lean", UNKNOWN, None, "price history unavailable", sheet=sheet)
    else:
        sc = {"uptrend": 1.0, "downtrend": -1.0}.get(tech.trend_label, 0.0)
        sc += 0.5 if (tech.pct_vs_sma20 or 0) > 2 else -0.5 if (tech.pct_vs_sma20 or 0) < -2 else 0
        if out.get("gex") and out["gex"].state == "negative" and lv is not None and lv.gamma_flip and S < lv.gamma_flip:
            sc -= 0.5
        st = "bullish" if sc >= 1 else "bearish" if sc <= -1 else "neutral"
        out["lean"] = _st("lean", st, sc, f"{tech.trend_label}, {tech.pct_vs_sma20:+.1f}% vs SMA20, 10d {tech.chg10_pct:+.1f}%",
                          tech.source, sheet=sheet)

    # flow
    if fs is None:
        out["flow"] = _st("flow", UNKNOWN, None, "no flow data", sheet=sheet)
    else:
        det = f"bias {fs.bias:+.2f}"
        if fs.volume_vs_20d is not None:
            det += f", volume {fs.volume_vs_20d:.1f}x 20d avg"
        if fs.unusual:
            det += f", {fs.unusual} unusual contracts"
        out["flow"] = _st("flow", fs.label, fs.bias, det, fs.source, as_of, fs.approx, sheet=sheet)

    # liquidity (name level)
    p = s["liquidity"]
    if lv is None or lv.atm_spread_pct is None:
        out["liquidity"] = _st("liquidity", UNKNOWN, None, "no ATM quotes", sheet=sheet)
    else:
        oi = (lv.total_call_oi or 0) + (lv.total_put_oi or 0)
        ok = lv.atm_spread_pct <= p["max_atm_spread_pct"] and oi >= p["min_total_oi"]
        out["liquidity"] = _st("liquidity", "ok" if ok else "thin", round(lv.atm_spread_pct, 3),
                               f"ATM bid-ask {lv.atm_spread_pct:.1%} of mid, total OI {oi:,.0f}", chain_src, as_of, sheet=sheet)

    # news
    p = s["news"]
    if news is None:
        out["news"] = _st("news", UNKNOWN, None, "news disabled/unavailable", sheet=sheet)
    else:
        rx = re.compile(r"\b(" + "|".join(re.escape(w) for w in p.get("binary_words", [])) + r")\b", re.I)
        binary = [i.title for i in news.items if rx.search(i.title or "")]
        if binary:
            out["news"] = _st("news", "binary", None, f"binary-event headline: {binary[0][:90]}",
                              ", ".join(news.providers), sheet=sheet)
        elif news.severe_flags:
            out["news"] = _st("news", "red_flag", None, "headline flags: " + ", ".join(news.severe_flags),
                              ", ".join(news.providers), sheet=sheet)
        else:
            out["news"] = _st("news", "clean", None, f"{len(news.items)} headlines, no severe flags",
                              ", ".join(news.providers), sheet=sheet)
    return out


# ---------------------------------------------------------------------------------------------------
# gates

def _want(cond: dict) -> str:
    if "in" in cond:
        return "in [" + ", ".join(cond["in"]) + "]"
    if "not_in" in cond:
        return "not in [" + ", ".join(cond["not_in"]) + "]"
    return "?"


def matches(cond: dict, state: str) -> bool | None:
    """True/False, or None when the state is unknown."""
    if state == UNKNOWN:
        return None
    if "in" in cond:
        return state in cond["in"]
    if "not_in" in cond:
        return state not in cond["not_in"]
    raise ValueError(f"bad gate condition {cond}")


def _decide(gate: str, kind: str, cond: dict, st: SignalState | None, unknown_credit: float) -> GateDecision:
    state = st.state if st else UNKNOWN
    cat = st.category if st else ""
    det = st.detail if st else "signal not computed"
    m = matches(cond, state)
    if m is None:
        pol = cond.get("unknown", "warn")
        if pol == "pass":
            return GateDecision(gate, kind, "pass", state, _want(cond), cat, 1.0, det)
        if pol == "fail":
            return GateDecision(gate, kind, "fail", state, _want(cond), cat, 0.0, det + " (unknown counts as fail)")
        return GateDecision(gate, kind, "unknown", state, _want(cond), cat, unknown_credit, det)
    return GateDecision(gate, kind, "pass" if m else "fail", state, _want(cond), cat, 1.0 if m else 0.0, det)


def evaluate_gates(strategy: str, spec: dict, states: dict[str, SignalState], symbol: str,
                   unknown_credit: float = 0.5) -> GateResult:
    g = spec.get("gates", {})
    res = GateResult(strategy, symbol)
    for name, cond in (g.get("required") or {}).items():
        d = _decide(name, "required", cond, states.get(name), unknown_credit)
        res.add(d)
        if d.outcome == "fail":
            res.passed = False
            res.failed.append(f"{name} is {d.state}, needs {d.want}")
        elif d.outcome == "unknown":
            res.warnings.append(f"{name} unknown ({d.detail}); gate passed at half credit")
    anys = g.get("any_of") or []
    if anys:
        ds = []
        for item in anys:
            (name, cond), = item.items()
            ds.append(_decide(name, "any_of", cond, states.get(name), unknown_credit))
        hit = [d for d in ds if d.outcome == "pass"]
        for d in ds:
            # only the best member of the group feeds the score; the rest are shown for the explain log
            d.credit = None
            res.add(d)
        best = hit[0] if hit else next((d for d in ds if d.outcome == "unknown"), None)
        if best is not None:
            best.credit = 1.0 if best.outcome == "pass" else unknown_credit
        if not hit:
            res.passed = False
            res.failed.append("none of: " + "; ".join(f"{d.gate} {d.want} (is {d.state})" for d in ds))
    for name, cond in (g.get("preferred") or {}).items():
        res.add(_decide(name, "preferred", cond, states.get(name), unknown_credit))
    for name, cond in (g.get("warn") or {}).items():
        st = states.get(name)
        state = st.state if st else UNKNOWN
        m = matches(cond, state)
        if m:
            res.warnings.append(f"{name} is {state}: {st.detail}")
            res.add(GateDecision(name, "warn", "warn", state, _want(cond), st.category, None, st.detail))
        else:
            res.add(GateDecision(name, "warn", "ok", state, "not " + _want(cond), st.category if st else "", None,
                                 st.detail if st else ""))
    return res
