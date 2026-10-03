"""Position pricing for the visualizer (spec v2.2 §6.2, §6.6). `web/static/engine.js` is the same code in
JavaScript; both are pinned to `web/static/golden.json` (Python to 1e-9, JS to 1e-6).

reprice side:   Leg, t_years, event_sigma, leg_sigma, leg_price, position_value, net_basis, metrics, grid,
                breakevens_at, risk_stats, greeks
probability:    lognormal_cdf, density, pop

Conventions
  * side +1 = long (bought), -1 = short (sold). Options carry 100 shares per contract; stock legs (type "S")
    count shares, priced at S with no expiry.
  * V = sum(side * qty * mult * price) is what the position is worth now (negative = cost to close).
    E = sum(side * qty * mult * fill) is what it cost (positive = debit paid, negative = credit received).
    P/L $ = V - E. P/L % = P/L / |E|. % of max risk = P/L / |max loss|.
  * T = calendar days / 365, valued at the close. On or after its expiry a leg is worth intrinsic (settled).
  * European BSM with dividend yield q, sticky strike: each leg keeps its own IV; the IV slider scales all.
  * Earnings event (date the move lands, move = 1-sd log move): after it, a leg that expires later loses the
    event variance: sigma_post = sqrt((sigma^2 T0 - move^2) / T0), floored at 0.25 sigma (flagged). Before it,
    sigma_pre = sqrt(sigma_post^2 + move^2 / T), which equals sigma today and rises into the event.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np

from . import bs

MULT = {"C": 100, "P": 100, "S": 1}
GRID_POINTS = 2001
SLOPE_EPS = 0.01        # $ of P/L per $ of underlying at the top of the grid: beyond it the risk is unbounded
EVENT_FLOOR = 0.25


def _date(x) -> dt.date | None:
    if x is None or x == "":
        return None
    return x if isinstance(x, dt.date) else dt.date.fromisoformat(str(x)[:10])


@dataclass
class Leg:
    side: int                    # +1 long, -1 short
    type: str                    # C / P / S
    strike: float = 0.0
    expiry: dt.date | None = None
    qty: float = 1.0
    fill: float = 0.0            # entry price per share (premium, or the stock price)
    iv: float | None = None      # annualised, e.g. 0.45

    def __post_init__(self):
        self.expiry = _date(self.expiry)
        if self.type not in MULT:
            raise ValueError(f"leg type must be C, P or S, not {self.type!r}")
        if self.side not in (1, -1):
            raise ValueError("leg side must be +1 or -1")
        if self.type != "S" and (self.expiry is None or self.strike <= 0):
            raise ValueError("an option leg needs a strike > 0 and an expiry")

    @property
    def mult(self) -> int:
        return MULT[self.type]

    def encode(self) -> str:
        """URL form: B|S : C|P|S : strike : expiry : qty : fill : iv (e.g. B:C:105:2026-12-11:1:6.30:0.61)."""
        iv = "" if self.iv is None else f"{self.iv:g}"
        return ":".join(["B" if self.side > 0 else "S", self.type, f"{self.strike:g}",
                         self.expiry.isoformat() if self.expiry else "", f"{self.qty:g}", f"{self.fill:g}", iv])

    @classmethod
    def decode(cls, code: str) -> "Leg":
        p = code.split(":")
        if len(p) != 7 or p[0] not in ("B", "S"):
            raise ValueError("leg must be action:type:strike:expiry:qty:fill:iv")
        return cls(1 if p[0] == "B" else -1, p[1], float(p[2] or 0), p[3] or None, float(p[4]), float(p[5]),
                   float(p[6]) if p[6] else None)

    @classmethod
    def from_dict(cls, d: dict) -> "Leg":
        return cls(int(d["side"]), d["type"], float(d.get("strike") or 0), d.get("expiry"), float(d.get("qty", 1)),
                   float(d.get("fill", 0)), None if d.get("iv") is None else float(d["iv"]))

    def to_dict(self) -> dict:
        return {"side": self.side, "type": self.type, "strike": self.strike,
                "expiry": self.expiry.isoformat() if self.expiry else None, "qty": self.qty, "fill": self.fill,
                "iv": self.iv}


@dataclass
class Event:
    date: dt.date                # the session the move lands on (after-close reports: the next session)
    move: float                  # 1-sd log move, e.g. 0.08

    def __post_init__(self):
        self.date = _date(self.date)


def t_years(on, expiry) -> float:
    return max((_date(expiry) - _date(on)).days, 0) / 365.0


def event_sigma(sigma: float, T0: float, move: float) -> tuple[float, bool]:
    """-> (post-event sigma, floored?). T0 = years from today to the leg's expiry."""
    if T0 <= 0 or sigma <= 0:
        return sigma, False
    var = (sigma * sigma * T0 - move * move) / T0
    lo = (EVENT_FLOOR * sigma) ** 2
    if var < lo:
        return EVENT_FLOOR * sigma, True
    return math.sqrt(var), False


def leg_sigma(leg: Leg, on, today, ivmult: float = 1.0, event: Event | None = None) -> float:
    s = (leg.iv or 0.0) * ivmult
    if event is None or leg.type == "S" or not (_date(today) < event.date < leg.expiry):
        return s
    post, _ = event_sigma(s, t_years(today, leg.expiry), event.move)
    if _date(on) >= event.date:
        return post
    T = t_years(on, leg.expiry)
    return math.sqrt(post * post + event.move * event.move / T) if T > 0 else post


def leg_price(leg: Leg, S, on, today, r: float = 0.0, q: float = 0.0, ivmult: float = 1.0,
              event: Event | None = None) -> np.ndarray:
    S = np.asarray(S, dtype=float)
    if leg.type == "S":
        return S.copy()
    T = t_years(on, leg.expiry)
    if T <= 0:
        return np.maximum(S - leg.strike, 0.0) if leg.type == "C" else np.maximum(leg.strike - S, 0.0)
    v = leg_sigma(leg, on, today, ivmult, event)
    return np.asarray(bs.price(np.maximum(S, 1e-12), leg.strike, T, v, leg.type, r, q), dtype=float)


def position_value(legs: list[Leg], S, on, today, r: float = 0.0, q: float = 0.0, ivmult: float = 1.0,
                   event: Event | None = None) -> np.ndarray:
    S = np.asarray(S, dtype=float)
    out = np.zeros_like(S)
    for lg in legs:
        out = out + lg.side * lg.qty * lg.mult * leg_price(lg, S, on, today, r, q, ivmult, event)
    return out


def net_basis(legs: list[Leg]) -> float:
    return float(sum(lg.side * lg.qty * lg.mult * lg.fill for lg in legs))


def metrics(V, E: float, max_loss: float | None) -> dict:
    V = np.asarray(V, dtype=float)
    pl = V - E
    return {"value": V, "pl": pl, "pl_pct": pl / abs(E) if E else np.full_like(pl, np.nan),
            "pct_risk": pl / abs(max_loss) if max_loss else np.full_like(pl, np.nan)}


def grid(legs: list[Leg], prices, dates, today, r: float = 0.0, q: float = 0.0, ivmult: float = 1.0,
         event: Event | None = None) -> np.ndarray:
    """V for every (price, date): shape (len(prices), len(dates))."""
    P = np.asarray(prices, dtype=float)
    return np.stack([position_value(legs, P, d, today, r, q, ivmult, event) for d in dates], axis=1)


def front_expiry(legs: list[Leg]) -> dt.date | None:
    ex = [lg.expiry for lg in legs if lg.type != "S"]
    return min(ex) if ex else None


def _price_grid(legs: list[Leg], spot: float | None) -> np.ndarray:
    """2,001 points from 0 to 3x the top strike, plus every strike exactly (a fly's peak sits on one)."""
    ks = [lg.strike for lg in legs if lg.type != "S"]
    top = 3.0 * max(ks + [spot or 0.0, 1.0])
    return np.unique(np.concatenate([np.linspace(0.0, top, GRID_POINTS), np.asarray(ks, dtype=float)]))


def breakevens_at(legs: list[Leg], on, today, r: float = 0.0, q: float = 0.0, ivmult: float = 1.0,
                  event: Event | None = None, spot: float | None = None) -> tuple[np.ndarray, np.ndarray, list[float]]:
    """-> (grid, P/L on the grid, breakevens). A breakeven is where "P/L > 0" flips between two grid points
    (so a flat zero stretch, e.g. a long option bought for nothing, is not one); refined by 60 bisections."""
    E = net_basis(legs)
    xs = _price_grid(legs, spot)
    pl = position_value(legs, xs, on, today, r, q, ivmult, event) - E
    out: list[float] = []

    def up(x: float) -> bool:
        return float(position_value(legs, np.array([x]), on, today, r, q, ivmult, event)[0] - E) > 0

    for i in range(len(xs) - 1):
        a = bool(pl[i] > 0)
        if a != bool(pl[i + 1] > 0):
            lo, hi = float(xs[i]), float(xs[i + 1])
            for _ in range(60):
                mid = 0.5 * (lo + hi)
                if up(mid) == a:
                    lo = mid
                else:
                    hi = mid
            out.append(0.5 * (lo + hi))
    return xs, pl, out


def risk_stats(legs: list[Leg], today, r: float = 0.0, q: float = 0.0, ivmult: float = 1.0,
               event: Event | None = None, spot: float | None = None) -> dict:
    """Max profit / max loss / breakevens of the whole position at the FRONT expiry (back legs at BSM value).
    max_profit / max_loss are None when unbounded (P/L still sloping at 3x the top strike)."""
    at = front_expiry(legs) or _date(today)
    xs, pl, be = breakevens_at(legs, at, today, r, q, ivmult, event, spot)
    slope = (pl[-1] - pl[-2]) / (xs[-1] - xs[-2])
    up_profit, up_loss = slope > SLOPE_EPS, slope < -SLOPE_EPS
    return {"at": at.isoformat(), "max_profit": None if up_profit else float(pl.max()),
            "max_loss": None if up_loss else float(pl.min()), "breakevens": be,
            "unbounded_profit": bool(up_profit), "unbounded_loss": bool(up_loss), "net_basis": net_basis(legs)}


# ---------------------------------------------------------------------------------------------------
# probability (kept apart from repricing: a density is a model of the future, prices are a model of now)

def lognormal_cdf(x, S: float, T: float, sigma: float, r: float = 0.0, q: float = 0.0):
    """P(S_T <= x) with S_T = S exp((r - q - sigma^2/2) T + sigma sqrt(T) Z)."""
    x = np.asarray(x, dtype=float)
    if T <= 0 or sigma <= 0:
        return (x >= S).astype(float)
    sd = sigma * math.sqrt(T)
    with np.errstate(divide="ignore"):
        z = (np.log(np.maximum(x, 1e-300) / S) - (r - q - 0.5 * sigma * sigma) * T) / sd
    return np.where(x <= 0, 0.0, bs._ncdf(z))


def density(xs, S: float, T: float, sigma: float, r: float = 0.0, q: float = 0.0) -> np.ndarray:
    xs = np.asarray(xs, dtype=float)
    if T <= 0 or sigma <= 0:
        return np.zeros_like(xs)
    sd = sigma * math.sqrt(T)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (np.log(np.maximum(xs, 1e-300) / S) - (r - q - 0.5 * sigma * sigma) * T) / sd
        d = np.exp(-0.5 * z * z) / (xs * sd * math.sqrt(2 * math.pi))
    return np.where(xs > 0, d, 0.0)


def pop(legs: list[Leg], S: float, on, today, sigma: float, r: float = 0.0, q: float = 0.0,
        ivmult: float = 1.0, event: Event | None = None) -> float:
    """P(P/L > 0 on date `on`), lognormal at `sigma` (an estimate: real returns have fat tails)."""
    front = front_expiry(legs)
    on = min(_date(on), front) if front else _date(on)
    xs, pl, be = breakevens_at(legs, on, today, r, q, ivmult, event, S)
    T = t_years(today, on)
    cuts = [0.0] + be + [math.inf]
    total = 0.0
    for a, b in zip(cuts[:-1], cuts[1:]):
        m = 0.5 * (a + b) if math.isfinite(b) else max(2.0 * a, xs[-1])
        if float(position_value(legs, np.array([m]), on, today, r, q, ivmult, event)[0]) - net_basis(legs) > 0:
            hi = 1.0 if not math.isfinite(b) else float(lognormal_cdf(b, S, T, sigma, r, q))
            total += hi - float(lognormal_cdf(a, S, T, sigma, r, q))
    return min(max(total, 0.0), 1.0)


def greeks(legs: list[Leg], S: float, on, today, r: float = 0.0, q: float = 0.0, ivmult: float = 1.0,
           event: Event | None = None) -> dict:
    """Position Greeks at (S, on): delta (shares), gamma (per $1), vega ($ per 1 vol point), theta ($ per
    calendar day: the change in value over the next day, so it includes an event or an expiry)."""
    d = g = v = 0.0
    for lg in legs:
        k = lg.side * lg.qty * lg.mult
        if lg.type == "S":
            d += k
            continue
        T = t_years(on, lg.expiry)
        if T <= 0:
            d += k * (1.0 if (lg.type == "C" and S > lg.strike) else -1.0 if (lg.type == "P" and S < lg.strike) else 0.0)
            continue
        sig = max(leg_sigma(lg, on, today, ivmult, event), 1e-4)
        sq = math.sqrt(T)
        d1 = (math.log(S / lg.strike) + (r - q + 0.5 * sig * sig) * T) / (sig * sq)
        dq = math.exp(-q * T)
        n1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
        nd1 = float(bs._ncdf(d1))
        d += k * (dq * nd1 if lg.type == "C" else dq * (nd1 - 1.0))
        g += k * dq * n1 / (S * sig * sq)
        v += k * S * dq * n1 * sq / 100.0
    on_d = _date(on)
    nxt = on_d + dt.timedelta(days=1)
    th = float(position_value(legs, np.array([S]), nxt, today, r, q, ivmult, event)[0]
               - position_value(legs, np.array([S]), on_d, today, r, q, ivmult, event)[0])
    return {"delta": d, "gamma": g, "theta": th, "vega": v}


__all__ = ["Event", "Leg", "MULT", "breakevens_at", "density", "event_sigma", "front_expiry", "greeks", "grid",
           "leg_price", "leg_sigma", "lognormal_cdf", "metrics", "net_basis", "pop", "position_value", "risk_stats",
           "t_years"]
