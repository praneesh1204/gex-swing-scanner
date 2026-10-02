"""Generate synthetic CBOE-format chains + price histories so the scanner runs offline.

python tests/make_fixtures.py tests/fixtures                 # everything
python tests/make_fixtures.py tests/fixtures --engine-only   # only the v2.1 engine files
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gexscan.analytics import bs  # noqa: E402

TODAY = dt.date(2026, 9, 25)

# symbol: spot, iv, trend drift per day, call OI peaks, put OI peaks, earnings
PROFILES = {
    "UPCO": dict(spot=247.6, iv=0.80, drift=0.006, calls={250: 9000, 260: 5000, 300: 3000}, puts={220: 12000, 200: 6000}, earn="2026-11-12"),
    "DNCO": dict(spot=99.3, iv=0.90, drift=-0.012, calls={110: 6000, 120: 4000}, puts={100: 2000, 90: 9000, 95: 7000}, earn="2026-11-05"),
    "PINCO": dict(spot=500.0, iv=0.28, drift=0.0, calls={520: 20000, 540: 8000}, puts={480: 20000, 460: 9000}, earn="2026-10-28"),
    "EARN": dict(spot=1058.0, iv=0.78, drift=0.003, calls={1100: 5000}, puts={1000: 6000}, earn="2026-09-30"),
}


def weekly_fridays(n=10):
    d = TODAY
    out = []
    while len(out) < n:
        d += dt.timedelta(days=1)
        if d.weekday() == 4:
            out.append(d)
    return out


def make_chain(sym, p, rng):
    S = p["spot"]
    strikes = np.round(np.arange(S * 0.6, S * 1.4, max(S * 0.0125, 0.5)) / (2.5 if S > 50 else 0.5)) * (2.5 if S > 50 else 0.5)
    strikes = sorted(set(list(strikes) + list(p["calls"]) + list(p["puts"])))
    a, tau = p.get("term", (0.0, 1.0))           # front-month premium: iv x (1 + a e^(-dte/tau)); 0 = flat
    opts = []
    for exp in weekly_fridays(10):
        dte = (exp - TODAY).days
        T = max(dte, 0.5) / 365
        iv = p["iv"] * (1 + a * np.exp(-dte / tau))
        for K in strikes:
            for cp in "CP":
                skew = iv * (1 + p.get("skew", 0.15) * max(0, (S - K) / S) * (1 if cp == "P" else 0.5))
                px = float(bs.price(S, K, T, skew, cp))
                if px < 0.01:
                    continue
                base_oi = p.get("base_oi", 200) * np.exp(-abs(K - S) / (0.08 * S))
                peaks = p["calls"] if cp == "C" else p["puts"]
                oi = base_oi + sum(v * np.exp(-((K - k) / (0.01 * S)) ** 2) for k, v in peaks.items())
                oi *= (1.5 if dte > 14 else 1.0)
                spr = max(0.02, px * p.get("spread", 0.04))
                opts.append({
                    "option": f"{sym}{exp:%y%m%d}{cp}{int(round(K*1000)):08d}",
                    "bid": round(max(0, px - spr / 2), 2), "ask": round(px + spr / 2, 2),
                    "iv": round(skew, 4), "open_interest": int(oi), "volume": int(oi * rng.uniform(0.05, 0.4)),
                    "delta": round(float(bs.delta(S, K, T, skew, cp)), 4), "gamma": round(float(bs.gamma(S, K, T, skew)), 5),
                })
    return {"timestamp": f"{TODAY} 18:00:00", "data": {"symbol": sym, "current_price": S, "options": opts}}


def make_history(p, rng, n=90):
    vol = p["iv"] * 0.25 / np.sqrt(252)            # low noise so the intended trend shows through
    r = rng.normal(p["drift"], vol, n)
    path = np.exp(np.cumsum(r))
    closes = p["spot"] * path / path[-1]            # path that ends exactly at today's spot
    idx = pd.bdate_range(end=pd.Timestamp(TODAY), periods=n)
    return pd.DataFrame({"open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes,
                         "volume": rng.integers(1e6, 5e6, n)}, index=idx)


# ---------------------------------------------------------------------------------------------------
# Engine fixtures (v2.1): ~2y of daily bars, a year of IV snapshots, past earnings, dividends, VIX family,
# FRED series, put/call and the macro calendar. New files only, own rng, so the v2.0 fixtures above stay
# byte-identical.

ENGINE_PROFILES = {
    # steep put skew, range-bound under positive gamma, next print 76 days out: condor / CSP / bull put
    "RICH": dict(spot=182.4, iv=0.42, drift=0.0, skew=1.2, base_oi=600, rv=0.28, beta=0.9, earn="2026-12-10",
                 calls={190: 18000, 195: 15000, 200: 9000}, puts={170: 15000, 165: 8000}),
    # ETF with a bid front month (VIX-style backwardation in one name): calendars
    "TERM": dict(spot=64.8, iv=0.30, drift=0.0, term=(0.6, 20), spread=0.015, base_oi=600, rv=0.18, beta=0.6,
                 calls={67.5: 25000, 70: 10000}, puts={62.5: 18000, 60: 9000}),
}
IV_RANGE = {"UPCO": (0.55, 1.10), "DNCO": (0.60, 1.10), "PINCO": (0.26, 0.60), "EARN": (0.40, 0.85),
            "RICH": (0.22, 0.48), "TERM": (0.22, 0.52)}
# past reports (after the close) and the next-session move imprinted on the bars, % of spot
PRINTS = {
    "EARN": [("2024-10-30", 4.8), ("2025-01-29", -3.9), ("2025-04-30", 5.6), ("2025-07-30", -4.2),
             ("2025-10-29", 3.5), ("2026-01-28", -5.1), ("2026-04-29", 4.4)],
    "UPCO": [("2025-02-12", 7.5), ("2025-05-14", -6.0), ("2025-08-13", 9.1), ("2025-11-12", -5.4),
             ("2026-02-11", 6.6)],
}
DIVIDENDS = {"PINCO": {"ex_date": "2026-10-09", "amount": 1.25}, "RICH": {"ex_date": "2026-10-16", "amount": 0.85}}
ETFS = ["TERM"]
N2Y = 520
STRESS = (pd.Timestamp("2025-03-31"), pd.Timestamp("2025-04-25"))   # one VIX backwardation episode for the backtest


def _bars(closes, idx, rng, rng_pct=None):
    hl = rng_pct if rng_pct is not None else np.full(len(closes), 0.01)
    return pd.DataFrame({"open": closes, "high": closes * (1 + hl), "low": closes * (1 - hl), "close": closes,
                         "volume": rng.integers(1e6, 5e6, len(closes))}, index=idx)


def make_spy(rng):
    idx = pd.bdate_range(end=pd.Timestamp(TODAY), periods=N2Y)
    stress = (idx >= STRESS[0]) & (idx <= STRESS[1])
    vol = np.where(stress, 0.45, 0.13) / np.sqrt(252)
    vol[-30:] = 0.12 / np.sqrt(252)
    r = rng.normal(0.0004, vol)
    r[stress] -= 0.006                               # ~ -12% over the episode
    closes = 655.0 * np.exp(np.cumsum(r)) / np.exp(np.cumsum(r))[-1]
    return _bars(closes, idx, rng, np.abs(vol) * 0.8), r


def make_daily2y(sym, p, hist90, spy_r, rng):
    """~2y of bars. v2.0 names: the 90 fixture bars are kept and history is prepended so RV-based and
    earnings-history signals have enough data. New names: correlated with SPY, last 30 bars range-bound."""
    idx = pd.bdate_range(end=pd.Timestamp(TODAY), periods=N2Y)
    dvol = p.get("rv", p["iv"] * 0.25) / np.sqrt(252)
    if hist90 is not None:
        n = N2Y - len(hist90)
        pre_idx = idx[:n]
        r = rng.normal(0.0003, dvol, n)
    else:
        n, pre_idx = N2Y, idx
        b, sv = p.get("beta", 0.0), 0.13 / np.sqrt(252)
        idio = np.sqrt(max(dvol ** 2 - (b * sv) ** 2, 1e-10))
        r = b * spy_r + rng.normal(p["drift"], idio, n)
        r[-30:] = (r[-30:] - r[-30:].mean()) * 0.75   # recent: flat and a little calmer than the year
    for d, mv in PRINTS.get(sym, []):
        pos = pre_idx.searchsorted(pd.Timestamp(d))
        if pos + 1 < len(pre_idx):
            r[pos + 1] = np.log(1 + mv / 100)          # AMC report: the move lands the next session
    path = np.exp(np.cumsum(r))
    if hist90 is not None:
        first = float(hist90["close"].iloc[0])
        pre = first * path / path[-1] / np.exp(rng.normal(0, dvol))
        return pd.concat([_bars(pre, pre_idx, rng, np.full(n, min(0.8 * dvol, 0.01))), hist90])
    closes = p["spot"] * path / path[-1]
    # last 60 bars: a 2% drift up then a fade back to spot, so close < SMA20 and SMA20 > SMA50 (= "range"),
    # within ~1% of SMA20 (neutral lean) whatever the seed did. The level carries beta x SPY's *daily* return
    # (not its cumulative path), so daily returns still correlate with SPY (~0.7 x beta share) without a trend.
    i = np.arange(60)
    arc = np.where(i < 55, 0.99 + 0.02 * i / 55, 1.01 - 0.01 * (i - 54) / 5)
    lvl = np.log(arc) + p.get("beta", 0.0) * spy_r[-60:] + rng.normal(0, 0.15 * dvol, 60)
    tail = p["spot"] * np.exp(lvl - lvl[-1])
    closes[:-60] *= tail[0] / closes[-61] * np.exp(-p.get("beta", 0.0) * spy_r[-60])
    closes[-60:] = tail
    return _bars(closes, idx, rng, np.full(n, 0.8 * dvol))


def make_iv_history(syms, rng):
    idx = pd.bdate_range(end=pd.Timestamp(TODAY) - pd.Timedelta(days=1), periods=252)
    out = {}
    for s in syms:
        lo, hi = IV_RANGE[s]
        x = np.zeros(len(idx))
        for i in range(1, len(idx)):
            x[i] = 0.97 * x[i - 1] + rng.normal(0, 1)
        x = (x - x.min()) / (x.max() - x.min())
        out[s] = np.round(lo + (hi - lo) * x, 4)
    return pd.DataFrame(out, index=idx)


def make_market(spy: pd.DataFrame, rng) -> dict:
    """VIX family driven by SPY realized vol; contango except during STRESS (backwardation)."""
    idx = spy.index
    r = np.log(spy["close"]).diff().fillna(0)
    rv = (r.rolling(20, min_periods=5).std() * np.sqrt(252) * 100).bfill().to_numpy()
    noise = np.zeros(len(idx))
    for i in range(1, len(idx)):
        noise[i] = 0.9 * noise[i - 1] + rng.normal(0, 0.4)
    vix = np.clip(7 + rv + noise, 11, None)
    stress = (idx >= STRESS[0]) & (idx <= STRESS[1] + pd.Timedelta(days=7))
    vix3m = vix * np.where(stress, 0.90, 1.14) + rng.normal(0, 0.15, len(idx))
    vix9d = vix * np.where(stress, 1.10, 0.93) + rng.normal(0, 0.15, len(idx))
    vvix = 82 + 1.8 * (vix - 15) + rng.normal(0, 1.5, len(idx))
    skew = 140 + rng.normal(0, 4, len(idx))
    ser = lambda a: {f"{d:%Y-%m-%d}": round(float(v), 2) for d, v in zip(idx, a)}  # noqa: E731

    fed = np.select([idx < "2025-09-18", idx < "2025-10-30", idx < "2025-12-11"], [4.33, 4.08, 3.83], 3.63)
    y2 = 3.55 + np.cumsum(rng.normal(0, 0.02, len(idx)))
    y2 = y2 - y2[-1] + 3.52
    y10 = 4.10 + np.cumsum(rng.normal(0, 0.02, len(idx)))
    y10[-20:] += np.linspace(0, 0.18, 20)          # 10y up ~18bp in a month
    y10 = y10 - y10[-1] + 4.31
    wti = 66 + np.cumsum(rng.normal(0, 0.6, len(idx)))
    wti[-20:] += np.linspace(0, 5.5, 20)            # an oil pop into the next CPI
    months = pd.date_range("2024-09-01", "2026-08-01", freq="MS")
    cpi = 314.0 * np.cumprod(np.full(len(months), 1.0024))
    return {
        "series": {"VIX": ser(vix), "VIX3M": ser(vix3m), "VIX9D": ser(vix9d), "VVIX": ser(vvix), "SKEW": ser(skew)},
        "fred": {"fed_funds": ser(fed), "y2": ser(y2), "y10": ser(y10), "curve_10y2y": ser(y10 - y2), "wti": ser(wti),
                 "cpi_index": {f"{d:%Y-%m-%d}": round(float(v), 3) for d, v in zip(months, cpi)}},
        "put_call": {"date": TODAY.isoformat(), "total": 0.88, "equity": 0.58, "index": 1.21},
        "fomc": ["2026-10-28", "2026-12-09"],
        "cpi": ["2026-10-14", "2026-11-13", "2026-12-10"],
    }


def engine_fixtures(outp: Path) -> None:
    """Reads the v2.0 {sym}_history.csv already on disk, so the 2y bars join the committed 90 exactly."""
    hists = {s: pd.read_csv(outp / f"{s}_history.csv", index_col=0, parse_dates=True) for s in PROFILES}
    rng = np.random.default_rng(11)
    spy, spy_r = make_spy(rng)
    spy.to_csv(outp / "SPY_daily2y.csv")
    for sym, p in ENGINE_PROFILES.items():
        (outp / f"{sym}.json").write_text(json.dumps(make_chain(sym, p, rng)))
    for sym, p in {**PROFILES, **ENGINE_PROFILES}.items():
        make_daily2y(sym, p, hists.get(sym), spy_r, rng).to_csv(outp / f"{sym}_daily2y.csv")
    make_iv_history(list(PROFILES) + list(ENGINE_PROFILES), rng).to_csv(outp / "iv_history.csv")
    (outp / "market.json").write_text(json.dumps(make_market(spy, rng)))
    (outp / "earnings_history.json").write_text(json.dumps(
        {s: [{"date": d, "timing": "AMC"} for d, _ in v] for s, v in PRINTS.items()}, indent=2))
    (outp / "dividends.json").write_text(json.dumps(DIVIDENDS, indent=2))
    (outp / "etfs.json").write_text(json.dumps(ETFS))
    ep = outp / "earnings.json"
    earn = json.loads(ep.read_text()) if ep.exists() else {}
    earn.update({s: p["earn"] for s, p in ENGINE_PROFILES.items() if p.get("earn")})
    ep.write_text(json.dumps(earn, indent=2))
    write_profiles(outp)


# company cards for the web app's ticker page (made up: these companies do not exist)
COMPANIES = {
    "UPCO": ("Upco Photonics (synthetic)", "Technology", "Optical components", 61e9, 9400,
             "A made-up maker of optical transceivers for AI data centres. Used to test an uptrend into earnings."),
    "DNCO": ("Downco Memory (synthetic)", "Technology", "Memory chips", 18e9, 5200,
             "A made-up memory supplier in a downtrend. Used to test bearish set-ups and negative gamma."),
    "PINCO": ("Pinco Power Semis (synthetic)", "Technology", "Power semiconductors", 140e9, 21000,
              "A made-up power-semiconductor name pinned between large call and put walls."),
    "EARN": ("Earnco Compute (synthetic)", "Technology", "GPUs and accelerators", 410e9, 30000,
             "A made-up accelerator designer with earnings days away. Used to test the earnings gate."),
    "RICH": ("Richvol Cooling (synthetic)", "Industrials", "Data-centre cooling", 33e9, 12000,
             "A made-up liquid-cooling supplier with rich implied volatility."),
    "TERM": ("Termco Energy (synthetic)", "Utilities", "Power for data centres", 52e9, 7600,
             "A made-up power producer whose volatility term structure is inverted."),
}


def write_profiles(outp: Path) -> None:
    (outp / "profiles.json").write_text(json.dumps({
        s: {"name": n, "sector": sec, "industry": ind, "market_cap": cap, "employees": emp,
            "summary": f"Synthetic test company. {txt}"}
        for s, (n, sec, ind, cap, emp, txt) in COMPANIES.items()}, indent=1))


def main(out: str):
    outp = Path(out)
    outp.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    earn = {}
    for sym, p in PROFILES.items():
        (outp / f"{sym}.json").write_text(json.dumps(make_chain(sym, p, rng)))
        make_history(p, rng).to_csv(outp / f"{sym}_history.csv")
        earn[sym] = p["earn"]
    (outp / "earnings.json").write_text(json.dumps(earn, indent=2))
    engine_fixtures(outp)
    print("fixtures written to", outp)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = args[0] if args else "tests/fixtures"
    if "--engine-only" in sys.argv:          # keep the committed v2.0 files, (re)write only the engine set
        engine_fixtures(Path(out))
        print("engine fixtures written to", out)
    else:
        main(out)
