"""JSON endpoints for the local website (spec v2.2 §5.6). All GET, all read-only.

Every handler takes the parsed query (dict of str -> str) and returns a JSON-ready dict, or raises BadRequest.
Data access goes through one lock (the engine's data source keeps caches that are not thread-safe); the
simulation runs outside it. Scans run one at a time and never write to the journal.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import tempfile
import threading
import time
from pathlib import Path

from .. import __version__
from ..analytics import overview as OV, pricing as P, simulate as SIM
from ..config import load_signal_sheet
from ..engine.recommend import EngineSource, Filters, build_context, recommend
from ..engine.universe import correlated_warning, load_universe
from ..pipeline import DISCLAIMER
from ..signals.market import market_regime
from ..store import Store
from ..strategies import REGISTRY

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
THEME = re.compile(r"^[a-z0-9_\-]{1,32}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NOT_SAVED = "Not saved. To track it, use `gexscan journal take` after a CLI run."
ALERT_ONLY = "Alert only. You place and manage every order yourself."
STRATEGY_LABELS = {
    "csp": "Cash-secured put", "covered_call": "Covered call", "bull_put": "Bull put spread",
    "bear_call": "Bear call spread", "bull_call": "Bull call spread", "bear_put": "Bear put spread",
    "iron_condor": "Iron condor", "calendar": "Calendar", "double_calendar": "Double calendar",
    "double_diagonal": "Double diagonal", "earnings_crush": "Earnings crush"}


class BadRequest(ValueError):
    """-> HTTP 400 with this message."""


# ---------------------------------------------------------------------------------------------------
# input parsing (unknown parameters are ignored)

def p_symbol(q: dict, key: str = "symbol", required: bool = True) -> str | None:
    s = (q.get(key) or "").strip().upper()
    if not s:
        if required:
            raise BadRequest(f"{key} is required")
        return None
    if not SYMBOL.match(s):
        raise BadRequest(f"{key} must look like a ticker (letters, digits, . or -; up to 10)")
    return s


def p_num(q: dict, key: str, lo: float, hi: float, default=None, integer: bool = False):
    raw = q.get(key)
    if raw is None or raw == "":
        return default
    try:
        x = float(raw)
    except ValueError:
        raise BadRequest(f"{key} must be a number") from None
    if not math.isfinite(x):
        raise BadRequest(f"{key} must be a finite number")
    x = min(max(x, lo), hi)
    return int(round(x)) if integer else x


def p_date(q: dict, key: str) -> str | None:
    raw = (q.get(key) or "").strip()
    if not raw:
        return None
    if not DATE.match(raw):
        raise BadRequest(f"{key} must be YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(raw).isoformat()
    except ValueError:
        raise BadRequest(f"{key} is not a real date") from None


def p_bool(q: dict, key: str, default: bool) -> bool:
    raw = (q.get(key) or "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def p_legs(q: dict, max_legs: int) -> list[P.Leg]:
    raw = (q.get("legs") or "").strip()
    if not raw:
        raise BadRequest("legs is required")
    codes = [c for c in raw.split(",") if c]
    if len(codes) > max_legs:
        raise BadRequest(f"at most {max_legs} legs")
    legs = []
    for c in codes:
        try:
            lg = P.Leg.decode(c)
        except (ValueError, TypeError):
            raise BadRequest("each leg is action:type:strike:expiry:qty:fill:iv, e.g. B:C:105:2026-12-11:1:6.30:0.61") from None
        if not (0 < lg.qty <= 1000) or not (0 <= lg.fill <= 1e6) or (lg.type != "S" and not (0 < lg.strike <= 1e6)):
            raise BadRequest("leg qty, fill or strike out of range")
        if lg.iv is not None and not (0 < lg.iv <= 10):
            raise BadRequest("leg iv out of range (0-10)")
        if lg.type != "S" and not lg.iv:
            raise BadRequest("an option leg needs an IV")
        legs.append(lg)
    return legs


def p_event(q: dict) -> P.Event | None:
    raw = (q.get("event") or "").strip()
    if not raw:
        return None
    d, _, m = raw.partition(":")
    try:
        move = float(m)
        date = dt.date.fromisoformat(d)
    except ValueError:
        raise BadRequest("event is date:move, e.g. 2026-10-23:0.09") from None
    if not (0 < move < 1.5):
        raise BadRequest("event move must be between 0 and 1.5")
    return P.Event(date.isoformat(), move)


def _clean(x):
    """JSON-safe: NaN/inf -> None, numpy scalars -> python, dates -> ISO."""
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):
        try:
            return _clean(x.item())
        except (ValueError, TypeError):
            return str(x)
    return x


class TTLCache:
    def __init__(self, ttl: float | None):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def get(self, key):
        with self._lock:
            v = self._d.get(key)
            if v is None or (self.ttl is not None and time.monotonic() - v[0] > self.ttl):
                return None
            return v[1]

    def put(self, key, val):
        with self._lock:
            if len(self._d) > 512:
                self._d.clear()
            self._d[key] = (time.monotonic(), val)
        return val


# ---------------------------------------------------------------------------------------------------

class Api:
    def __init__(self, cfg: dict, fixtures: Path | None, today: dt.date | None, state: Path | None,
                 use_news: bool = True):
        self.cfg, self.fixtures, self.use_news = cfg, fixtures, use_news
        self.today = today or dt.date.today()
        # a fixtures demo never touches the real state dir
        self.state = Path(state) if state else (Path(tempfile.mkdtemp(prefix="gexweb-")) if fixtures
                                                else Path(cfg["output"]["state_dir"]))
        self.src = EngineSource(cfg, fixtures, self.today, self.state, use_news=use_news)
        self.uni = load_universe(cfg, fixtures=bool(fixtures))
        self.sheet = load_signal_sheet(cfg)
        web = cfg.get("web") or {}
        self.max_legs = int(web.get("max_legs", 8))
        self.cache = TTLCache(None if fixtures else float(web.get("cache_ttl_s", 300)))
        self.data_lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.r = float((cfg.get("pricing") or {}).get("rate", 0.04))
        self.sim = cfg.get("sim") or {}
        self.routes = {"/api/meta": self.meta, "/api/themes": self.themes, "/api/quote": self.quote,
                       "/api/ticker": self.ticker, "/api/scan": self.scan, "/api/chain": self.chain,
                       "/api/simulate": self.simulate, "/api/golden": self.golden}

    @property
    def mode(self) -> str:
        return "fixtures" if self.fixtures else "live"

    def _cached(self, key, fn):
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        return self.cache.put(key, fn())

    # ---- shared data --------------------------------------------------------------------------------
    def _market(self):
        def build():
            with self.data_lock:
                md = self.src.market.load() if self.cfg["macro"].get("enabled", True) else None
                return md, (market_regime(md, self.today) if md is not None else None)
        return self._cached(("market",), build)

    def _store(self):
        return Store(self.state)

    def _ctx(self, sym: str):
        def build():
            md, _ = self._market()
            with self.data_lock:
                store = None if self.fixtures else self._store()
                try:
                    return build_context(sym, self.src, self.cfg, self.sheet, None, md, iv_store=store)
                finally:
                    if store is not None:
                        store.close()
        return self._cached(("ctx", sym), build)

    def _event(self, ctx) -> dict | None:
        """The next earnings as a pricing event: date = the session the move lands on; move = the jump's
        standard deviation, from the IV of the expiry holding the print against the next one (else from the
        average past move)."""
        nv, info = ctx.nv, ctx.info
        if not nv or not nv.earn_event_day or not info or not info.next_earnings:
            return None
        term = [t for t in nv.term if t.atm_iv]
        e = next((t for t in term if t.expiry == nv.earn_expiry), None)
        b = next((t for t in term if e and t.expiry > e.expiry), None)
        move, how = None, ""
        if e and b and e.atm_iv > b.atm_iv:
            T = max(e.dte, 1) / 365
            move, how = math.sqrt(e.atm_iv ** 2 * T - b.atm_iv ** 2 * T), f"IV {e.expiry} vs {b.expiry}"
        elif info.mean_abs_move_pct:
            move, how = info.mean_abs_move_pct / 100 * math.sqrt(math.pi / 2), f"mean of {info.n_moves} past moves"
        if not move or move < 0.005:
            return None
        return {"date": nv.earn_event_day, "move": round(min(move, 1.0), 4), "earnings": info.next_earnings.isoformat(),
                "timing": info.timing, "source": how}

    def _q(self, ctx) -> float:
        info, spot = ctx.info, ctx.chain.spot if ctx.chain else 0
        if info and info.dividend and spot > 0:
            return round(float(info.dividend) * 4 / spot, 5)      # assumes a quarterly payer
        return 0.0

    # ---- endpoints ----------------------------------------------------------------------------------
    def meta(self, q: dict) -> dict:
        _, reg = self._market()
        m = reg.to_dict() if reg is not None else {}
        return {"version": __version__, "mode": self.mode, "today": self.today.isoformat(),
                "mode_label": "FIXTURES · synthetic" if self.fixtures else "LIVE · CBOE delayed",
                "disclaimer": DISCLAIMER, "never_orders": "This app never places, submits, modifies or cancels orders.",
                "r": self.r, "themes": len(self.uni.theme_list()), "max_legs": self.max_legs,
                "max_paths": int(self.sim.get("max_paths", 50_000)), "paths": int(self.sim.get("paths", 20_000)),
                "allow_undefined_risk": bool(self.cfg["engine"].get("allow_undefined_risk", False)),
                "strategies": [{"key": k, "label": STRATEGY_LABELS.get(k, k)} for k in
                               (self.cfg["engine"].get("strategies") or REGISTRY)],
                "market": {k: m.get(k) for k in ("as_of", "vix", "vix3m", "vix9d", "vvix", "ts_ratio", "vix_pct_1y",
                                                 "events", "sources")},
                "symbols": sorted({s for t in self.uni.theme_list() for s in t["tickers"]})}

    def themes(self, q: dict) -> dict:
        rows = self.uni.theme_list()
        count: dict[str, list[str]] = {}
        for t in rows:
            if t["kind"] != "outside":
                for s in t["tickers"]:
                    count.setdefault(s, []).append(t["key"])
        for t in rows:
            t["overlaps"] = sorted({s for s in t["tickers"] if len(count.get(s, [])) > 1})
        return {"themes": rows, "mode": self.mode}

    def _theme_or_400(self, key: str) -> dict:
        if not THEME.match(key or ""):
            raise BadRequest("theme must be a theme key")
        t = self.uni.theme(key)
        if t is None:
            raise BadRequest("unknown theme")
        return t

    def quote(self, q: dict) -> dict:
        sym = p_symbol(q)

        def build():
            with self.data_lock:
                return OV.quote(sym, self.src)
        out = dict(self._cached(("quote", sym), build))
        out["themes"] = self.uni.themes_of(sym)
        return out

    def ticker(self, q: dict) -> dict:
        sym = p_symbol(q)

        def build():
            md, reg = self._market()
            with self.data_lock:
                store = None if self.fixtures else self._store()
                try:
                    return OV.overview(sym, self.src, self.cfg, self.sheet, md, reg, store, self.state)
                finally:
                    if store is not None:
                        store.close()
        out = dict(self._cached(("ticker", sym), build))
        out["themes"] = [{"key": k, "name": self.uni.themes[k].name} for k in self.uni.themes_of(sym)]
        out["in_universe"] = self.uni.in_universe(sym)
        out["mode"] = self.mode
        return out

    def chain(self, q: dict) -> dict:
        sym = p_symbol(q)
        want = p_date(q, "expiry")
        ctx = self._ctx(sym)
        if ctx.chain is None:
            raise BadRequest(f"no option chain for {sym}")
        opts = ctx.chain.options.assign(expiry=lambda d: d["expiry"].astype(str).str[:10])
        term = {t.expiry: t for t in (ctx.nv.term if ctx.nv else [])}
        expiries = []
        for e in sorted(opts["expiry"].unique()):
            dte = (dt.date.fromisoformat(e) - self.today).days
            if dte < 0:
                continue
            t = term.get(e)
            expiries.append({"expiry": e, "dte": dte, "atm_iv": t.atm_iv if t else None,
                             "em_pct": t.em_pct if t else None, "lower": t.lower if t else None,
                             "upper": t.upper if t else None})
        if not expiries:
            raise BadRequest(f"no live expiries for {sym}")
        mp = OV.monthly_point(ctx.nv)
        default = mp.expiry if mp else expiries[0]["expiry"]
        sel = want if want in {x["expiry"] for x in expiries} else default
        rows: dict[float, dict] = {}
        for r in opts[opts["expiry"] == sel].itertuples(index=False):
            d = rows.setdefault(float(r.strike), {"strike": float(r.strike)})
            d[r.type] = {"bid": r.bid, "ask": r.ask, "mid": r.mid, "iv": r.iv, "delta": r.delta, "oi": r.oi,
                         "volume": r.volume}
        lv = ctx.levels
        return _clean({
            "symbol": sym, "spot": ctx.chain.spot, "as_of": ctx.chain.as_of, "today": self.today.isoformat(),
            "r": self.r, "q": self._q(ctx), "expiries": expiries, "expiry": sel,
            "strikes": [rows[k] for k in sorted(rows)],
            "term": [[t.expiry, t.atm_iv] for t in (ctx.nv.term if ctx.nv else []) if t.atm_iv],
            "levels": {"call_wall": lv.call_wall, "put_wall": lv.put_wall, "gamma_flip": lv.gamma_flip,
                       "regime": lv.regime, "max_pain": lv.max_pain_near},
            "event": self._event(ctx), "fresh": ctx.fresh.get("chain", ""), "mode": self.mode,
            "themes": self.uni.themes_of(sym)})

    def _viz(self, c, ctx) -> dict:
        """What the visualizer needs for one idea: legs at mid, the exit plan, r, q, the event, the levels."""
        legs = [P.Leg(l.sign, l.type, float(l.strike), l.expiry, float(l.qty), float(l.mid), float(l.iv or 0) or None)
                for l in c.legs]
        if c.strategy == "covered_call":
            legs.insert(0, P.Leg(1, "S", 0.0, None, 100.0 * max(1, c.legs[0].qty), float(c.spot), None))
        tc = self.cfg.get("trades") or {}
        # the idea's own exit plan, as fractions of the net (c.net: + credit, - debit)
        n = abs(c.net or 0)
        if c.kind == "credit":
            tp = 1 - c.tp_value / n if n and c.tp_value is not None else float(tc.get("take_profit_pct", 0.5))
            stop = c.stop_value / n if n and c.stop_value else float(tc.get("stop_credit_multiple", 2.0))
        else:
            tp = c.tp_value / n - 1 if n and c.tp_value else 0.5
            stop = c.stop_value / n if n and c.stop_value is not None else 0.5
        plan = {"kind": c.kind, "tp": round(tp, 4), "stop": round(stop, 4),
                "below": c.stop_below, "above": c.stop_above, "texit": c.time_exit}
        lv = ctx.levels if ctx else None
        return _clean({
            "symbol": c.symbol, "spot": c.spot, "today": self.today.isoformat(), "r": self.r,
            "q": self._q(ctx) if ctx else 0.0, "event": self._event(ctx) if ctx else None,
            "legs": [lg.encode() for lg in legs], "plan": plan,
            "term": [[t.expiry, t.atm_iv] for t in (ctx.nv.term if ctx and ctx.nv else []) if t.atm_iv],
            "levels": {"call_wall": lv.call_wall, "put_wall": lv.put_wall, "gamma_flip": lv.gamma_flip} if lv else {},
            "per": "1 lot", "suggested_contracts": c.contracts})

    def scan(self, q: dict) -> dict:
        sym = p_symbol(q, required=False)
        theme = (q.get("theme") or "").strip().lower() or None
        if not sym and not theme:
            raise BadRequest("symbol or theme is required")
        if theme:
            self._theme_or_400(theme)
        known = set(self.cfg["engine"].get("strategies") or REGISTRY)
        strategies = [s for s in (q.get("strategies") or "").split(",") if s] or None
        if strategies and any(s not in known for s in strategies):
            raise BadRequest("unknown strategy")
        dte_min = p_num(q, "dte_min", 0, 400, None, True)
        dte_max = p_num(q, "dte_max", 0, 400, None, True)
        if dte_min is not None and dte_max is not None and dte_min > dte_max:
            raise BadRequest("dte_min is above dte_max")
        minc = p_num(q, "min_confidence", 0, 100)
        top = p_num(q, "top", 1, 30, None, True)
        allow_cfg = bool(self.cfg["engine"].get("allow_undefined_risk", False))
        defined_only = p_bool(q, "defined_only", True) or not allow_cfg      # locked on unless configured
        f = Filters(symbols=[sym] if sym else None, themes=[theme] if theme and not sym else None,
                    strategies=strategies, min_confidence=minc, top_n=top,
                    max_per_ticker=(top or 6) if sym else None, allow_undefined_risk=not defined_only,
                    dte_min=dte_min, dte_max=dte_max)
        key = ("scan", sym, theme, tuple(strategies or ()), dte_min, dte_max, minc, top, defined_only)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        with self.scan_lock:
            hit = self.cache.get(key)
            if hit is not None:
                return hit
            with self.data_lock:
                rec = recommend(None, f, self.cfg, self.fixtures, self.today, self.state, use_news=self.use_news,
                                save_snapshots=False)
            ideas = []
            for c in rec.recommendations:
                d = c.to_dict()
                ctx = rec.book.contexts.get(c.symbol) if rec.book else None
                d["themes"] = [{"key": k, "name": self.uni.themes[k].name} for k in self.uni.themes_of(c.symbol)]
                d["outside"] = not self.uni.in_universe(c.symbol)
                d["strategy_label"] = STRATEGY_LABELS.get(c.strategy, c.strategy)
                d["viz"] = self._viz(c, ctx)
                ideas.append(d)
            out = _clean({
                "symbol": sym, "theme": self.uni.theme(theme) if theme else None, "today": rec.today,
                "generated": rec.generated, "ideas": ideas, "rejected": rec.rejected, "explain": rec.explain,
                "names": rec.names, "market": rec.market, "filters": rec.filters, "errors": rec.errors,
                "warnings": [w for w in [correlated_warning(rec.recommendations, self.uni)] if w],
                "disclaimer": rec.disclaimer, "not_saved": NOT_SAVED, "alert_only": ALERT_ONLY, "mode": self.mode,
                "defined_only": defined_only, "defined_locked": not allow_cfg})
            return self.cache.put(key, out)

    def simulate(self, q: dict) -> dict:
        legs = p_legs(q, self.max_legs)
        spot = p_num(q, "spot", 0.01, 1e6)
        if spot is None:
            raise BadRequest("spot is required")
        sym = p_symbol(q, required=False)
        ev = p_event(q)
        n = p_num(q, "n", 500, float(self.sim.get("max_paths", 50_000)), int(self.sim.get("paths", 20_000)), True)
        seed = p_num(q, "seed", 0, 2 ** 63 - 1, None, True)
        if P.front_expiry(legs) is None:
            raise BadRequest("the simulation needs at least one option leg")
        if P.front_expiry(legs) <= self.today:
            raise BadRequest("the front expiry has passed")
        base = SIM.default_plan(legs, self.today, self.cfg.get("trades") or {})
        texit = p_date(q, "texit")
        plan = SIM.Plan(base.kind,
                        p_num(q, "tp", 0.05, 5.0, base.tp), p_num(q, "stop", 0.0, 10.0, base.stop),
                        p_num(q, "below", 0.0, 1e6), p_num(q, "above", 0.0, 1e6), texit or base.texit)
        if plan.kind == "debit" and plan.stop >= 1:
            raise BadRequest("a debit stop is a fraction of the debit (below 1)")
        if plan.kind == "credit" and plan.stop <= 1:
            raise BadRequest("a credit stop is a multiple of the credit (above 1)")
        term = None
        if sym:
            ctx = self._ctx(sym)
            if ctx.nv:
                term = [(t.expiry, t.atm_iv) for t in ctx.nv.term if t.atm_iv]
        r = p_num(q, "r", 0.0, 0.2, self.r)
        qq = p_num(q, "q", 0.0, 0.2, 0.0)
        evk = (ev.date.isoformat(), ev.move) if ev else None   # Event is a mutable dataclass: not hashable
        key = ("sim", tuple(lg.encode() for lg in legs), spot, sym, evk, n, seed, repr(plan), r, qq)

        def build():
            res = SIM.simulate(legs, spot, self.today, r, qq, term, ev, plan, n=n, seed=seed,
                               min_vol=float(self.sim.get("min_vol", 0.05)), max_paths=int(self.sim.get("max_paths", 50_000)),
                               fan_pct=tuple(self.sim.get("fan_percentiles", (5, 25, 50, 75, 95))))
            res["term_source"] = f"{sym} ATM term structure" if term else "leg IVs (no term structure)"
            return _clean(res)
        return self._cached(key, build)

    def golden(self, q: dict) -> dict:
        return self._cached(("golden",), lambda: json.loads((STATIC / "golden.json").read_text()))

    def handle(self, path: str, q: dict) -> dict:
        fn = self.routes.get(path)
        if fn is None:
            raise KeyError(path)
        return fn(q)


__all__ = ["ALERT_ONLY", "Api", "BadRequest", "NOT_SAVED", "STATIC", "STRATEGY_LABELS", "SYMBOL"]
