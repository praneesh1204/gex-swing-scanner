"""Setup classification and v2 scoring.

Every name gets a setup type and a 0-100 score built from:
  regime (gamma sign / flip distance), location (distance to the relevant wall, confirmed by volume profile),
  vol edge (IV vs realized), trend, liquidity, and a flow bonus (options activity aligned with the setup).
Severe news red flags (offering, guidance cut, ...) subtract `news_penalty` points.
Earnings inside the trade window is a hard veto (handled in trades/pipeline).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .analytics.gex import Levels
from .analytics.technicals import Technicals


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 100.0


@dataclass
class Score:
    symbol: str
    setup: str                      # bull_put / bear_call / call_debit / iron_condor / skip
    total: float
    components: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    dist_put_wall_pct: float | None = None
    dist_call_wall_pct: float | None = None
    dist_flip_pct: float | None = None
    iv_rv_ratio: float | None = None

    def to_dict(self):
        return asdict(self)


def flow_alignment(setup: str, bias: float | None) -> float:
    """Map flow bias (-1..1) to a 0..1 bonus aligned with the setup's direction."""
    if bias is None:
        return 0.0
    if setup in ("bull_put", "call_debit", "covered_call"):
        return _clip(bias)
    if setup == "bear_call":
        return _clip(-bias)
    if setup == "iron_condor":
        return _clip(1 - abs(bias) * 2)
    return 0.0


def classify_and_score(lv: Levels, tech: Technicals, weights: dict, flow_bias: float | None = None,
                       vp=None, news=None, news_penalty: float = 10.0) -> Score:
    S = lv.spot
    d_pw = _pct(S, lv.put_wall)          # + means spot above put wall
    d_cw = _pct(lv.call_wall, S)         # + means call wall above spot
    d_flip = _pct(S, lv.gamma_flip)      # + means spot above flip
    rv = tech.rv20
    iv = lv.atm_iv
    ratio = (iv / rv) if (iv and rv) else None
    pos = lv.regime == "positive_gamma"
    above_flip = (d_flip is None and lv.flip_status == "no_boundary_positive") or (d_flip is not None and d_flip > 0)
    has_trend = tech.close is not None and tech.sma20 is not None
    flat = has_trend and abs(tech.pct_vs_sma20 or 0) < 3.0
    trend_up = has_trend and tech.close > tech.sma20
    trend_dn = has_trend and tech.close < tech.sma20

    reasons, warnings = [], []
    if lv.flip_status == "no_data":
        return Score(lv.symbol, "skip", 0.0, reasons=["no options data"])

    # --- choose setup ---------------------------------------------------------
    cheap_iv = ratio is not None and ratio < 0.9
    if pos and above_flip and flat and ratio is not None and ratio >= 1.0 \
            and d_pw is not None and d_pw > 3 and d_cw is not None and d_cw > 3:
        setup = "iron_condor"                       # range-bound, rich premium, walls on both sides
    elif pos and above_flip and d_pw is not None and d_pw > 0 and trend_up and not cheap_iv:
        setup = "bull_put"                          # sell premium under the put wall, with the trend
    elif pos and above_flip and cheap_iv and d_pw is not None and d_pw >= 0 and \
            (d_pw <= 3 or (trend_up and d_cw is not None and d_cw >= 3)):
        setup = "call_debit"                        # options cheap: buy the bounce / the room to the call wall
    elif (not pos or not above_flip) and trend_dn and d_cw is not None and d_cw > 0 and not cheap_iv:
        setup = "bear_call"                         # negative gamma + downtrend: sell calls above the call wall
    else:
        setup = "skip"

    # --- components (0..1) ----------------------------------------------------
    comp = {}
    if setup in ("bull_put", "call_debit", "iron_condor"):
        comp["regime"] = (1.0 if pos else 0.0) * _clip(0.5 + (d_flip or 10.0) / 20.0)
        d = d_pw if d_pw is not None else -1
        if setup == "call_debit":
            bounce = _clip(1 - abs(d - 1.0) / 3.0)                     # right on / just above the put wall
            room = _clip((d_cw or 0) / 6.0)                            # upside room to the call wall
            comp["location"] = max(bounce, room)
        else:
            comp["location"] = 1.0 if 2 <= d <= 8 else (_clip(d / 2) if d < 2 else _clip(1 - (d - 8) / 10))
    elif setup == "bear_call":
        comp["regime"] = 1.0 if not pos else _clip(-(d_flip or 0) / 5.0)
        d = d_cw if d_cw is not None else -1
        comp["location"] = 1.0 if 2 <= d <= 8 else (_clip(d / 2) if d < 2 else _clip(1 - (d - 8) / 10))
    else:
        comp["regime"] = 0.0
        comp["location"] = 0.0

    if ratio is None:
        comp["vol_edge"] = 0.3
        warnings.append("IV or realized vol unavailable")
    elif setup == "call_debit":
        comp["vol_edge"] = _clip((1.1 - ratio) / 0.4)
    else:
        comp["vol_edge"] = _clip((ratio - 0.85) / 0.4)

    t = 0.0
    if tech.close is not None:
        up = setup in ("bull_put", "call_debit")
        if setup == "iron_condor":
            t = _clip(1 - abs(tech.pct_vs_sma20 or 0) / 8)
        else:
            s = 1 if up else -1
            t += 0.5 if s * (tech.close - tech.sma20) > 0 else 0
            t += 0.25 if s * (tech.close - tech.sma10) > 0 else 0
            t += 0.25 if s * (tech.chg10_pct or 0) > 0 else 0
            if up and (tech.pct_vs_sma20 or 0) > 20:
                t -= 0.25
                warnings.append(f"extended {tech.pct_vs_sma20:.0f}% above 20d MA")
    comp["trend"] = _clip(t)

    sp = lv.atm_spread_pct
    oi = lv.total_call_oi + lv.total_put_oi
    comp["liquidity"] = (_clip(1 - (sp if sp is not None else 0.3) / 0.3)) * _clip((oi / 20000) ** 0.5)
    comp["flow_bonus"] = flow_alignment(setup, flow_bias)

    # Volume-profile confirmation: the short strike region sits on real traded volume.
    if vp is not None and vp.poc and setup != "skip":
        if setup in ("bull_put", "iron_condor") and lv.put_wall and vp.val and lv.put_wall <= vp.val * 1.01:
            comp["location"] = _clip(comp["location"] + 0.15)
            reasons.append(f"put wall {lv.put_wall:g} at/below value-area low {vp.val:.2f}")
        elif setup == "bear_call" and lv.call_wall and vp.vah and lv.call_wall >= vp.vah * 0.99:
            comp["location"] = _clip(comp["location"] + 0.15)
            reasons.append(f"call wall {lv.call_wall:g} at/above value-area high {vp.vah:.2f}")
        if setup == "bull_put" and vp.location == "below value":
            warnings.append(f"price below the value area ({vp.val:.2f}); sellers in control")

    total = sum(weights.get(k, 0) * v for k, v in comp.items())

    if news is not None and news.severe_flags and setup != "skip":
        total -= news_penalty
        warnings.append("news red flag: " + ", ".join(news.severe_flags) + f" (-{news_penalty:g} pts)")
    elif news is not None and news.red_flags:
        warnings.append("news: " + ", ".join(news.red_flags[:3]))

    if setup == "skip":
        total = min(total, 20.0)
        reasons.append("no clean setup: regime/trend/location disagree")
    else:
        reasons.append(f"{'positive' if pos else 'negative'} gamma, net GEX {lv.net_gex/1e6:,.1f}M $/1%")
        if d_pw is not None:
            reasons.append(f"{d_pw:+.1f}% vs put wall {lv.put_wall:g}")
        if d_cw is not None:
            reasons.append(f"call wall {lv.call_wall:g} is {d_cw:+.1f}% away")
        if ratio is not None:
            reasons.append(f"IV/RV {ratio:.2f} ({iv*100:.0f}% vs {rv*100:.0f}%)")
    if lv.flip_status.startswith("no_boundary"):
        warnings.append(f"gamma flip: {lv.flip_status}")

    comp = {k: round(v, 2) for k, v in comp.items()}
    return Score(lv.symbol, setup, round(max(total, 0.0), 1), comp, reasons, warnings, d_pw, d_cw, d_flip, ratio)
