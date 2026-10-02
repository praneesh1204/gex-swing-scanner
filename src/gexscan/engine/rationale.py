"""Three-level rationale (spec §0/§6) for every recommendation.

  1. why premium is rich or cheap (IV rank/percentile, VRP, implied vs historical earnings move, term structure)
  2. what the market is pricing (expected move to expiry, where the strikes and breakevens sit vs that move,
     skew, the PoP that follows from those prices)
  3. second-order effects (rates/oil, VIX term structure + VVIX, dealer gamma as a vol regime, macro prints
     before expiry, index correlation, flow (labelled PROXY when free), put/call extremes, dividends)
plus the invalidation (what would make the thesis wrong) and the exit plan headline.
"""
from __future__ import annotations

import datetime as dt

from ..signals.market import rate_regime
from ..strategies.base import Candidate


def _pct(x):
    return f"{x:.1%}" if x is not None else "n/a"


def level1(c: Candidate, ctx, st) -> list[str]:
    nv = ctx.nv
    out = []
    if c.strategy == "earnings_crush" and nv and nv.move_ratio:
        out.append(f"The {nv.earn_expiry} straddle implies a {nv.implied_move_pct:.1f}% move vs a mean "
                   f"{nv.hist_move_pct:.1f}% over past prints ({nv.move_ratio:.2f}x): the print is priced rich, and "
                   "the event premium drains out the morning after (IV crush).")
    if "iv_rank" in st and st["iv_rank"].state != "unknown":
        r = st["iv_rank"]
        tag = " (PROXY: realized-vol range, not IV history)" if r.approx else ""
        rich = {"rich": "rich", "sell": "above average", "mid": "middling", "low": "cheap"}.get(r.state, r.state)
        out.append(f"IV rank {r.value:.0f}{tag}: implied vol is {rich} against its own 52-week range "
                   f"(ATM {_pct(nv.atm_iv)}).")
    if "vrp" in st and st["vrp"].state != "unknown":
        v = st["vrp"]
        meaning = {"positive": "options are priced above what the stock has been delivering: sellers are paid a "
                               "premium for bearing the risk",
                   "negative": "the stock is moving more than options price: selling premium here is selling it cheap",
                   "flat": "implied and realized vol are close: no clear volatility risk premium"}[v.state]
        out.append(f"{v.detail}: {meaning}.")
    if c.vol_view == "long" and nv:
        bits = []
        if nv.ts_ratio:
            bits.append(f"front/back IV {nv.ts_ratio:.2f} ({nv.front_expiry} vs {nv.back_expiry})")
        if nv.ff is not None:
            bits.append(f"forward factor {nv.ff:+.2f} (front IV {'above' if nv.ff > 0 else 'below'} the implied forward vol)")
        if nv.front_pump:
            bits.append(f"earnings expiry IV {nv.front_pump:.2f}x the next expiry")
        if bits:
            out.append("Time spread edge: " + "; ".join(bits) + ". You sell the expensive front month and own the "
                       "cheaper back month.")
    return out


def level2(c: Candidate, ctx, st) -> list[str]:
    nv, S = ctx.nv, ctx.chain.spot
    out = []
    em = c.metrics.get("expected_move")
    if em:
        out.append(f"The {em['expiry']} straddle prices a +/-{em['em']:.2f} ({em['em_pct']:.1f}%) move: "
                   f"a {em['lower']:g}-{em['upper']:g} range from {S:g}.")
    shorts = [l for l in c.legs if l.action == "SELL"]
    if shorts and em and c.kind == "credit":
        pos = ", ".join(f"{l.strike:g}{l.type} ({abs(l.delta):.2f} delta, "
                        f"{'outside' if (l.strike <= em['lower'] if l.type == 'P' else l.strike >= em['upper']) else 'inside'} the range)"
                        for l in shorts)
        out.append(f"Short strikes: {pos}.")
    if c.breakevens:
        out.append("Breakevens " + " / ".join(f"{b:g}" for b in c.breakevens) +
                   (f"; PoP {c.pop:.0%} under a lognormal at the market's ATM IV (an estimate, not a forecast)."
                    if c.pop is not None else "."))
    sk = st.get("skew")
    if sk and sk.state != "unknown":
        m = {"put_rich": "puts are bid over calls (crash insurance demand): put sellers collect the skew",
             "call_rich": "calls are bid over puts (squeeze/takeover demand): call sellers are paid, but upside is the risk",
             "neutral": "skew is unremarkable"}[sk.state]
        out.append(f"Skew: {sk.detail}; {m}.")
    if nv and nv.earn_expiry and c.strategy != "earnings_crush" and c.vol_view == "long":
        out.append(f"The {nv.earn_expiry} expiry holds the report (implied {nv.implied_move_pct}%).")
    return out


def level3(c: Candidate, ctx, st, book) -> list[str]:
    m = book.market_regime
    out = []
    if m is not None:
        rr = rate_regime(m)
        if rr:
            out.append(f"Rates/oil: {rr}. Higher-for-longer rates raise the cost of carry in deep ITM puts and "
                       "favour cash-secured selling (cash earns the rate); an oil shock feeds CPI and the next FOMC.")
        if "vix_ts" in st and st["vix_ts"].state != "unknown":
            vt = st["vix_ts"]
            out.append(f"VIX term structure {vt.value:.2f} ({vt.state}): "
                       + ("contango = calm, premium decays on schedule." if vt.state == "contango" else
                          "flat = stress building; sizes should be smaller." if vt.state == "flat" else
                          "backwardation = stress regime, stand down on new short premium."))
        if "vvix" in st and st["vvix"].state != "unknown":
            out.append(f"{st['vvix'].detail} ({st['vvix'].state})" +
                       ("" if st["vvix"].state == "normal" else ": vol-of-vol is bid, so vol can gap; long vega is pricey."))
        ev = [e for e in m.events if dt.date.fromisoformat(str(e["date"])) <= dt.date.fromisoformat(c.expiry)]
        if ev:
            out.append("Macro prints before expiry: " + ", ".join(f"{e['event']} {e['date']}" for e in ev) +
                       ". Each can re-price index vol; single names with high index correlation move with it.")
    g = st.get("gex")
    if g and g.state != "unknown":
        out.append(f"Dealer gamma: {g.detail}. " +
                   ("Positive gamma: dealers sell rallies and buy dips, which damps realized vol (good for short premium)."
                    if g.state == "positive" else
                    "Negative gamma: dealers chase moves, which amplifies realized vol (bad for short premium)."))
    if ctx.corr_spy is not None:
        out.append(f"60-day correlation with SPY {ctx.corr_spy:.2f}: "
                   + ("index-driven, so the VIX regime above matters as much as the name." if ctx.corr_spy >= 0.6 else
                      "mostly idiosyncratic: name-specific news drives it more than the index."))
    fs = ctx.fs
    if fs is not None:
        tag = " [PROXY: free chain volume/OI, trade side unknown]" if fs.approx else f" [{fs.source}]"
        line = f"Flow {fs.label} (bias {fs.bias:+.2f}){tag}"
        if fs.volume_vs_20d:
            line += f"; option volume {fs.volume_vs_20d:.1f}x its 20-day average"
        out.append(line + ".")
        if fs.market_pc_note:
            out.append(fs.market_pc_note + ".")
    info = ctx.info
    if info is not None and info.ex_div_date and ctx.today <= info.ex_div_date <= dt.date.fromisoformat(c.expiry):
        out.append(f"Ex-dividend {info.ex_div_date} (${info.dividend:.2f}) before expiry: short calls with little "
                   "extrinsic value can be assigned early; puts get slightly richer into the date.")
    return out


def build(c: Candidate, book) -> dict:
    ctx = book.contexts[c.symbol]
    st = book.states(c.symbol)
    inval = c.invalidation
    if not inval:
        lv = []
        if c.stop_below:
            lv.append(f"a close below {c.stop_below:g}")
        if c.stop_above:
            lv.append(f"a close above {c.stop_above:g}")
        inval = ("Thesis wrong on " + " or ".join(lv) + "." if lv else "") + \
                (" Short-vol thesis also fails if VIX/VIX3M flips to backwardation or the name's gamma turns negative."
                 if c.vol_view == "short" else " Long-vega thesis fails if the front-month premium collapses into the back month.")
    return {"why_premium": level1(c, ctx, st), "market_pricing": level2(c, ctx, st),
            "second_order": level3(c, ctx, st, book), "invalidation": inval.strip(),
            "exit": f"{c.stop_rule}. TP: {c.take_profit}. Stop: {c.stop} Time: {c.time_stop}."}
