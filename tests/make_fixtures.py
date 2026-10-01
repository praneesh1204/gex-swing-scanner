"""Generate synthetic CBOE-format chains + price histories so the scanner runs offline.

python tests/make_fixtures.py tests/fixtures
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
    S, iv = p["spot"], p["iv"]
    strikes = np.round(np.arange(S * 0.6, S * 1.4, max(S * 0.0125, 0.5)) / (2.5 if S > 50 else 0.5)) * (2.5 if S > 50 else 0.5)
    strikes = sorted(set(list(strikes) + list(p["calls"]) + list(p["puts"])))
    opts = []
    for exp in weekly_fridays(10):
        dte = (exp - TODAY).days
        T = max(dte, 0.5) / 365
        for K in strikes:
            for cp in "CP":
                skew = iv * (1 + 0.15 * max(0, (S - K) / S) * (1 if cp == "P" else 0.5))
                px = float(bs.price(S, K, T, skew, cp))
                if px < 0.01:
                    continue
                base_oi = 200 * np.exp(-abs(K - S) / (0.08 * S))
                peaks = p["calls"] if cp == "C" else p["puts"]
                oi = base_oi + sum(v * np.exp(-((K - k) / (0.01 * S)) ** 2) for k, v in peaks.items())
                oi *= (1.5 if dte > 14 else 1.0)
                spr = max(0.02, px * 0.04)
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
    print("fixtures written to", outp)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures")
