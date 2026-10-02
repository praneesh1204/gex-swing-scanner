"""Per-name volatility signals from the option chain + daily bars.

  * IV rank / percentile over 52 weeks (own IV snapshots; else a labelled RV-range proxy)
  * VRP: 30-day ATM IV minus 20-day realized vol, close-to-close AND Parkinson (high-low)
  * ATM IV and straddle expected move for every expiry (the "tent"), front/back ratio, backwardation flag
  * forward factor between a front/back pair (idea credit: kdubb-labs/forward-factor-backtest, MIT)
  * 25-delta risk reversal at ~30 DTE
  * earnings: implied move from the first expiry holding the print vs past |moves|, and the front "pump"

Everything here uses only data as of the chain/bar date. Outputs are estimates.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from ..analytics.volatility import ANN, _iv_at_delta, iv_rank_pct, parkinson
from ..data.events import EventInfo


@dataclass
class TermPoint:
    expiry: str
    dte: int
    atm_iv: float | None
    strike: float | None
    straddle: float | None      # ATM call mid + put mid ($/share): the market's expected |move| to expiry
    em_pct: float | None
    lower: float | None         # tent edges: spot -/+ straddle
    upper: float | None


@dataclass
class NameVol:
    symbol: str
    spot: float
    atm_iv: float | None = None            # ~30 DTE, decimal
    iv_rank: float | None = None
    iv_percentile: float | None = None
    iv_rank_source: str = ""
    rv20: float | None = None              # close-to-close
    park20: float | None = None            # Parkinson high-low
    vrp_c2c: float | None = None           # vol points (IV - RV) x 100
    vrp_park: float | None = None
    term: list = field(default_factory=list)          # [TermPoint]
    front_expiry: str | None = None
    back_expiry: str | None = None
    front_iv: float | None = None
    back_iv: float | None = None
    ts_ratio: float | None = None          # front / back
    ff: float | None = None                # forward factor for the calendar pair below
    ff_front: str | None = None
    ff_back: str | None = None
    ff_fwd_iv: float | None = None
    ff_note: str = ""
    put25: float | None = None
    call25: float | None = None
    rr25: float | None = None              # vol points: put25 - call25
    put_skew_ratio: float | None = None    # put25 / ATM
    earn_expiry: str | None = None         # first expiry that holds the print
    earn_event_day: str | None = None      # the session whose move the print lands on (AMC -> next day)
    implied_move_pct: float | None = None  # straddle / spot at earn_expiry (includes the normal days to expiry)
    hist_move_pct: float | None = None     # mean |past earnings move|
    move_ratio: float | None = None        # implied / historical
    front_pump: float | None = None        # IV(earn_expiry) / IV(next expiry)
    as_of: str = ""

    def to_dict(self):
        return asdict(self)

    def tent(self, expiry: str) -> TermPoint | None:
        return next((t for t in self.term if t.expiry == expiry), None)


def _atm(e: pd.DataFrame, S: float):
    x = e[(e["iv"] > 0.02) & (e["iv"] < 5)]
    if x.empty:
        return None, None, None
    ks = x["strike"].unique()
    k = float(ks[np.argmin(np.abs(ks - S))])
    c = x[(x["strike"] == k) & (x["type"] == "C")]
    p = x[(x["strike"] == k) & (x["type"] == "P")]
    ivs = [float(r["iv"].iloc[0]) for r in (c, p) if len(r)]
    mids = [float(r["mid"].iloc[0]) for r in (c, p) if len(r) and r["mid"].iloc[0] > 0]
    straddle = sum(mids) if len(mids) == 2 else None
    return (float(np.mean(ivs)) if ivs else None), k, straddle


def term_curve(opts: pd.DataFrame, S: float) -> list[TermPoint]:
    out = []
    for exp, e in opts.groupby("expiry"):
        dte = int(e["dte"].iloc[0])
        if dte < 1:
            continue
        iv, k, st = _atm(e, S)
        out.append(TermPoint(str(exp), dte, round(iv, 4) if iv else None, k, round(st, 2) if st else None,
                             round(st / S * 100, 2) if st else None,
                             round(S - st, 2) if st else None, round(S + st, 2) if st else None))
    return sorted(out, key=lambda t: t.dte)


def forward_factor(iv1: float, t1: float, iv2: float, t2: float) -> tuple[float | None, float | None]:
    """FF = (front IV - forward IV) / forward IV, forward variance = (T2 s2^2 - T1 s1^2) / (T2 - T1).
    Returns (None, None) when forward variance is <= 0 (the pair is not usable)."""
    if not (iv1 and iv2) or t2 <= t1:
        return None, None
    fv = (t2 * iv2 ** 2 - t1 * iv1 ** 2) / (t2 - t1)
    if fv <= 0:
        return None, None
    fwd = math.sqrt(fv)
    return round((iv1 - fwd) / fwd, 3), round(fwd, 4)


def _nearest(term: list[TermPoint], target: int, lo: int | None = None, hi: int | None = None) -> TermPoint | None:
    c = [t for t in term if t.atm_iv and (lo is None or t.dte >= lo) and (hi is None or t.dte <= hi)]
    return min(c, key=lambda t: abs(t.dte - target)) if c else None


def event_day(info: EventInfo | None) -> dt.date | None:
    """The session whose close first reflects the report: AMC/unknown -> next day (timps0n: AMC +1)."""
    if info is None or info.next_earnings is None:
        return None
    d = info.next_earnings
    return d if info.timing == "BMO" else d + dt.timedelta(days=1)


def name_vol(chain, hist: pd.DataFrame | None, iv_history: pd.Series | None, info: EventInfo | None,
             sheet: dict, today: dt.date) -> NameVol:
    S, opts = chain.spot, chain.options
    sig = sheet.get("signals", {})
    nv = NameVol(chain.symbol, S, as_of=str(chain.as_of))
    term = term_curve(opts, S)
    nv.term = term
    p30 = _nearest(term, 30, 14, 60)
    nv.atm_iv = p30.atm_iv if p30 else None

    # realized vol + VRP
    if hist is not None and len(hist) >= 21:
        h = hist.astype(float)
        h = h[h.index <= pd.Timestamp(today)]
        r = np.log(h["close"] / h["close"].shift(1)).dropna()
        nv.rv20 = round(float(r.tail(20).std() * math.sqrt(ANN)), 4) if len(r) >= 20 else None
        nv.park20 = round(parkinson(h, 20), 4) if parkinson(h, 20) else None
        rv_hist = r.rolling(20).std() * math.sqrt(ANN)
        nv.iv_rank, nv.iv_percentile, nv.iv_rank_source = iv_rank_pct(nv.atm_iv, iv_history, rv_hist)
    if nv.atm_iv and nv.rv20:
        nv.vrp_c2c = round((nv.atm_iv - nv.rv20) * 100, 1)
    if nv.atm_iv and nv.park20:
        nv.vrp_park = round((nv.atm_iv - nv.park20) * 100, 1)

    # term structure: front ~30 vs back ~60
    ts = sig.get("term_structure", {})
    fp = _nearest(term, ts.get("front_dte", 30), 14, 45)
    bp = _nearest(term, ts.get("back_dte", 60), (fp.dte + 14) if fp else 30, 120) if fp else None
    if fp and bp:
        nv.front_expiry, nv.back_expiry, nv.front_iv, nv.back_iv = fp.expiry, bp.expiry, fp.atm_iv, bp.atm_iv
        nv.ts_ratio = round(fp.atm_iv / bp.atm_iv, 3)

    # forward factor on the calendar pair (front 21-45 DTE, back 21-45 days later)
    eday = event_day(info)
    f1 = _nearest(term, 30, 21, 45)
    f2 = _nearest(term, (f1.dte + 30) if f1 else 60, (f1.dte + 21) if f1 else 42, (f1.dte + 45) if f1 else 90) if f1 else None
    if f1 and f2:
        nv.ff_front, nv.ff_back = f1.expiry, f2.expiry
        if eday and dt.date.fromisoformat(f1.expiry) < eday <= dt.date.fromisoformat(f2.expiry):
            nv.ff_note = f"earnings ({info.next_earnings}) fall between {f1.expiry} and {f2.expiry}: FF not used"
        else:
            nv.ff, nv.ff_fwd_iv = forward_factor(f1.atm_iv, f1.dte / 365, f2.atm_iv, f2.dte / 365)
            if nv.ff is None:
                nv.ff_note = "forward variance <= 0 for this pair: not usable"

    # 25-delta risk reversal at ~30 DTE
    if p30:
        e = opts[opts["expiry"].astype(str) == p30.expiry]
        T = max(p30.dte, 1) / 365
        put, call = _iv_at_delta(e, S, T, "P", 0.25), _iv_at_delta(e, S, T, "C", 0.25)
        if put and call:
            nv.put25, nv.call25 = round(put, 4), round(call, 4)
            nv.rr25 = round((put - call) * 100, 2)
            nv.put_skew_ratio = round(put / nv.atm_iv, 3) if nv.atm_iv else None

    # earnings: implied vs historical move, front pump
    if eday:
        after = [t for t in term if dt.date.fromisoformat(t.expiry) >= eday and t.straddle]
        if after:
            ep = after[0]
            nv.earn_expiry, nv.earn_event_day = ep.expiry, eday.isoformat()
            nv.implied_move_pct = ep.em_pct
            nxt = [t for t in term if t.dte > ep.dte and t.atm_iv]
            if nxt and ep.atm_iv:
                nv.front_pump = round(ep.atm_iv / nxt[0].atm_iv, 3)
    if info is not None and info.mean_abs_move_pct:
        nv.hist_move_pct = info.mean_abs_move_pct
        if nv.implied_move_pct and info.n_moves >= sig.get("earnings_move", {}).get("min_history", 4):
            nv.move_ratio = round(nv.implied_move_pct / info.mean_abs_move_pct, 2)
    return nv
