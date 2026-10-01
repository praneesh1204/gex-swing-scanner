"""End-to-end daily run: scan -> review open ideas -> risk gate -> journal -> reports.

Nothing here places orders. Outputs are ideas with exit plans, marked on delayed data.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from jinja2 import Environment, PackageLoader, select_autoescape

from . import charts
from .analytics.flow import Flow, compute_flow
from .analytics.gex import Levels, compute_levels, gex_by_strike
from .analytics.technicals import Technicals, compute_technicals
from .analytics.volatility import VolStack, compute_vol_stack
from .analytics.volume_profile import VolumeProfile, compute_volume_profile
from .data import cboe, flashalpha, news, prices
from .review import review_idea, weekly_stats
from .risk import gate, recent_stops
from .scoring import Score, classify_and_score
from .store import Store
from .trades import Trade, build_covered_call, build_trade

log = logging.getLogger(__name__)
DISCLAIMER = ("Educational research tool, not financial advice. Model outputs (GEX, probabilities, GARCH, flow bias) "
              "are estimates on delayed data. This app never places orders; you decide and execute every trade yourself.")


@dataclass
class SymbolResult:
    symbol: str
    chain: cboe.Chain | None = None
    levels: Levels | None = None
    tech: Technicals | None = None
    vol: VolStack | None = None
    vp: VolumeProfile | None = None
    flow: Flow | None = None
    news: news.NewsReport | None = None
    score: Score | None = None
    trade: Trade | None = None
    cc: Trade | None = None
    cc_status: str = ""
    earnings: dt.date | None = None
    status: str = ""
    fa: dict | None = None
    hist: pd.DataFrame | None = None
    by_strike: pd.DataFrame | None = None
    fresh: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


@dataclass
class Candidate:
    symbol: str
    trade: Trade
    res: SymbolResult
    status: str = ""


class DataSource:
    """Live data by default; a fixtures directory makes the whole run offline and reproducible."""

    def __init__(self, cfg: dict, fixtures: Path | None = None, cache_dir: Path | None = None,
                 today: dt.date | None = None, use_news: bool = True):
        self.cfg, self.fixtures, self.cache_dir, self.today = cfg, fixtures, cache_dir, today or dt.date.today()
        self.use_news = use_news and cfg["news"].get("enabled", True)
        self._earn, self._bench = {}, None
        if fixtures and (fixtures / "earnings.json").exists():
            self._earn = json.loads((fixtures / "earnings.json").read_text())

    def _csv(self, name: str) -> pd.DataFrame:
        p = self.fixtures / name
        if not p.exists():
            return pd.DataFrame()
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        df.attrs["source"] = "fixture"
        return df

    def chain(self, sym: str) -> cboe.Chain:
        if self.fixtures:
            return cboe.load_chain_file(self.fixtures / f"{sym}.json", sym, today=self.today)
        d = self.cfg["data"]
        return cboe.fetch_chain(sym, d["cboe_url"], timeout=d.get("request_timeout_s", 20), cache_dir=self.cache_dir, today=self.today)

    def history(self, sym: str) -> pd.DataFrame:
        if self.fixtures:
            return self._csv(f"{sym}_history.csv")
        d = self.cfg["data"]
        return prices.fetch_history(sym, d.get("history_days", 400), d.get("cboe_history_url"))

    def intraday(self, sym: str) -> pd.DataFrame:
        if self.fixtures:
            return self._csv(f"{sym}_intraday.csv")
        d = self.cfg["data"]
        return prices.fetch_intraday(sym, d.get("intraday_interval", "30m"), d.get("intraday_days", 30))

    def benchmark(self) -> pd.DataFrame:
        if self._bench is None:
            b = self.cfg["data"].get("benchmark", "SPY")
            self._bench = self._csv(f"{b}_history.csv") if self.fixtures else prices.fetch_history(b, 120)
        return self._bench

    def earnings(self, sym: str) -> dt.date | None:
        if self.fixtures:
            v = self._earn.get(sym)
            return dt.date.fromisoformat(v) if v else None
        return prices.fetch_next_earnings(sym, today=self.today)

    def flash(self, sym: str) -> dict | None:
        if self.fixtures or sym not in (self.cfg["data"].get("flashalpha_symbols") or []):
            return None
        return flashalpha.get_levels(sym)

    def news(self, sym: str) -> news.NewsReport | None:
        if not self.use_news:
            return None
        if self.fixtures:
            p = self.fixtures / "news.json"
            items = json.loads(p.read_text()).get(sym, []) if p.exists() else []
            rep = news.NewsReport(sym, items=[news.NewsItem(**i) for i in items], providers=["fixture"])
            for i in rep.items:
                i.tags = news._tag(i.title)
            rep.red_flags = sorted({t[2:] for i in rep.items for t in i.tags if t.startswith("⚠")})
            rep.severe_flags = [f for f in rep.red_flags if f in news.SEVERE]
            return rep
        return news.research(sym, self.cfg, use_llm=False)


def _levels(chain: cboe.Chain, cfg: dict) -> Levels:
    return compute_levels(chain, cfg["data"]["max_dte_for_levels"], cfg["gex"]["flip_search_range_pct"],
                          cfg["gex"]["flip_grid_points"])


def scan_symbol(sym: str, src: DataSource, cfg: dict, store: Store | None = None, light: bool = False) -> SymbolResult:
    """Full analysis of one ticker. `light=True` skips news/intraday (used when only marking a journal idea)."""
    res = SymbolResult(sym)
    try:
        chain = src.chain(sym)
    except Exception as e:
        res.errors.append(f"{sym}: option chain unavailable ({e})")
        res.status = "NO DATA"
        return res
    if chain.options.empty or chain.spot <= 0:
        res.errors.append(f"{sym}: empty chain / no spot")
        res.status = "NO DATA"
        return res
    res.chain = chain
    res.levels = lv = _levels(chain, cfg)
    res.hist = hist = src.history(sym)
    res.tech = compute_technicals(hist, None if light else src.benchmark())
    res.fresh["chain"] = f"CBOE delayed ~15m, as of {chain.as_of} UTC; OI is prior-day"
    res.fresh["daily"] = (f"{hist.attrs.get('source', '?')}, last bar {str(hist.index[-1])[:10]}" if len(hist) else "unavailable")
    if light:
        return res

    today = src.today
    res.earnings = src.earnings(sym)
    res.fa = src.flash(sym)
    ivh = store.iv_history(sym, today) if store else None
    res.vol = compute_vol_stack(hist, lv.atm_iv, cfg["trades"].get("target_dte_ideal", 30), ivh, chain.options, chain.spot)
    intra = src.intraday(sym)
    vpc = cfg["volume_profile"]
    res.vp = compute_volume_profile(intra, hist, chain.spot, vpc["bins"], vpc["value_area_pct"])
    res.fresh["intraday"] = f"{res.vp.source}, {res.vp.start} to {res.vp.end}" if res.vp.source else "unavailable"
    prev_date, prev_oi = store.previous_contract_oi(sym, today) if store else (None, None)
    fc = cfg["flow"]
    res.flow = compute_flow(chain.options, chain.spot, prev_oi, prev_date, fc["unusual_min_volume"], fc["unusual_vol_oi_ratio"])
    res.news = src.news(sym)
    if res.news is not None:
        newest = max((i.published for i in res.news.items if i.published), default="")
        res.fresh["news"] = f"{', '.join(res.news.providers) or 'none'}; newest {newest[:16]}"
    res.by_strike = gex_by_strike(chain, cfg["data"]["max_dte_for_levels"])

    res.score = classify_and_score(lv, res.tech, cfg["scoring"]["weights"], res.flow.bias, res.vp, res.news,
                                   cfg["scoring"].get("news_penalty", 10))
    if res.fa:
        _cross_check(res)
    if res.earnings is None:
        res.score.warnings.append("next earnings date unknown: verify before trading")
    if res.tech.close is None:
        res.score.warnings.append("price history unavailable: trend/realized vol not scored")

    shares = int((cfg.get("holdings") or {}).get(sym, 0) or 0)
    if shares:
        res.cc, why = build_covered_call(chain, lv, res.tech, shares, cfg["trades"], res.earnings, today)
        res.cc_status = "covered call idea" if res.cc else f"no covered call: {why}"

    if res.score.setup == "skip":
        res.status = "SKIP"
        return res
    if res.score.total < cfg["scoring"]["min_score_to_trade"]:
        res.status = f"score < {cfg['scoring']['min_score_to_trade']}"
        return res
    trade, why = build_trade(chain, lv, res.score, cfg["trades"], res.earnings, today=today)
    if trade is None:
        res.status = f"no trade: {why}"
    else:
        res.trade, res.status = trade, "TRADE"
    return res


def _cross_check(res: SymbolResult) -> None:
    lv, fa = res.levels, res.fa
    agree = []
    for k in ("put_wall", "call_wall"):
        a, b = getattr(lv, k), fa.get(k)
        if a and b:
            ok = abs(a - b) / lv.spot <= 0.03
            agree.append(ok)
            if not ok:
                res.score.warnings.append(f"FlashAlpha {k.replace('_', ' ')} {b:g} vs ours {a:g}: models disagree")
    if agree and all(agree):
        res.score.reasons.append("walls confirmed by FlashAlpha")


def _fmt_level(v, spot):
    if not v or not spot:
        return "–"
    return f"{v:.2f}".rstrip("0").rstrip(".") + f" ({(v / spot - 1) * 100:+.1f}%)"


def _news_context(r: SymbolResult) -> str:
    lv, t = r.levels, r.trade or r.cc
    parts = [f"spot {lv.spot:.2f}, put wall {lv.put_wall}, call wall {lv.call_wall}, gamma flip {lv.gamma_flip}, "
             f"regime {lv.regime}, ATM IV {((lv.atm_iv or 0) * 100):.0f}%"]
    if r.earnings:
        parts.append(f"next earnings {r.earnings}")
    if t:
        parts.append(f"candidate idea: {t.summary()} ({t.setup})")
    return "; ".join(parts)


def _llm_news(results: list[SymbolResult], cfg: dict) -> None:
    """Upgrade the headline summary with Claude for the names that have an idea (needs ANTHROPIC_API_KEY)."""
    ncfg = cfg["news"]
    if not ncfg.get("llm", True):
        return
    todo = [r for r in results if r.news is not None and r.news.items and (r.trade or r.cc)][: ncfg.get("llm_max_symbols", 10)]
    for r in todo:
        llm = news.claude_summary(r.symbol, r.news.items, _news_context(r), ncfg.get("llm_model", "claude-opus-5-5"))
        if not llm:
            continue
        r.news.summary = (llm.get("summary", "") + " " + llm.get("premium_selling_view", "")).strip()
        if isinstance(llm.get("sentiment"), (int, float)):
            r.news.sentiment = round(float(llm["sentiment"]), 2)
        r.news.red_flags = sorted(set(r.news.red_flags) | {str(x) for x in llm.get("risk_events", [])[:4]})
        r.news.catalysts = sorted(set(r.news.catalysts) | {str(x) for x in llm.get("catalysts", [])[:4]})
        r.news.summary_source = f"Claude ({ncfg.get('llm_model')}), an estimate"


def _detail(r: SymbolResult, cfg: dict, make_charts: bool) -> dict:
    d = {"symbol": r.symbol, "status": r.status, "fresh": r.fresh, "errors": r.errors,
         "earnings": r.earnings.isoformat() if r.earnings else None}
    for k in ("levels", "tech", "vol", "vp", "flow", "news", "score"):
        v = getattr(r, k)
        d[k] = v.to_dict() if v is not None else None
    d["trade"] = r.trade.to_dict() if r.trade else None
    d["cc"] = r.cc.to_dict() if r.cc else None
    d["cc_status"] = r.cc_status
    d["charts"] = {}
    if make_charts and r.levels is not None:
        d["charts"] = {"gex": charts.gex_chart(r.symbol, r.by_strike, r.levels),
                       "price": charts.price_chart(r.symbol, r.hist, r.levels, r.vp)}
    return d


def _row(r: SymbolResult) -> dict:
    lv, t, s, v, vp, f, n = r.levels, r.tech, r.score, r.vol, r.vp, r.flow, r.news
    return {
        "symbol": r.symbol, "spot": lv.spot if lv else None, "setup": s.setup if s else "–",
        "score": s.total if s else "–", "net_gex": lv.net_gex if lv else None,
        "regime": lv.regime if lv else None,
        "put_wall": lv.put_wall if lv else None, "call_wall": lv.call_wall if lv else None,
        "gamma_flip": lv.gamma_flip if lv else None,
        "put_wall_txt": _fmt_level(lv.put_wall, lv.spot) if lv else "–",
        "call_wall_txt": _fmt_level(lv.call_wall, lv.spot) if lv else "–",
        "flip_txt": (_fmt_level(lv.gamma_flip, lv.spot) if lv and lv.gamma_flip else (lv.flip_status if lv else "–")),
        "max_pain": lv.max_pain_near if lv else None,
        "em_txt": (f"±{lv.exp_move_near:.2f} ({lv.exp_move_near_pct:.1f}%) {lv.near_expiry}" if lv and lv.exp_move_near else "–"),
        "atm_iv": lv.atm_iv if lv else None, "rv20": t.rv20 if t else None,
        "ivrv_txt": (f"{lv.atm_iv * 100:.0f}% / {t.rv20 * 100:.0f}%" if lv and lv.atm_iv and t and t.rv20 else "–"),
        "iv_rank": v.iv_rank if v else None, "garch": v.garch_forecast if v else None,
        "trend": t.trend_label if t else None, "rsi14": t.rsi14 if t else None,
        "trend_txt": (f"{t.pct_vs_sma20:+.1f}%" if t and t.pct_vs_sma20 is not None else "–"),
        "vp_location": vp.location if vp else None, "poc": vp.poc if vp else None,
        "call_volume": f.call_volume if f else None, "put_volume": f.put_volume if f else None,
        "pc_volume": f.pc_volume_ratio if f else None, "flow_bias": f.bias_label if f else None,
        "news_sentiment": n.sentiment_label if n else None,
        "news_flags": ", ".join(n.red_flags) if n else "",
        "earnings": r.earnings.isoformat() if r.earnings else None,
        "status": r.status,
    }


def _idea_record(c: Candidate, today: dt.date) -> dict:
    tr, lv, s = c.trade, c.res.levels, c.res.score
    return {
        "run_date": today.isoformat(), "symbol": c.symbol, "setup": tr.setup, "score": s.total if s else None,
        "expiry": tr.expiry, "legs": tr.journal_legs(), "entry_net": tr.net_mid, "width": tr.width,
        "max_profit": tr.max_profit, "max_loss": tr.max_loss, "contracts": tr.contracts, "spot": lv.spot,
        "put_wall": lv.put_wall, "call_wall": lv.call_wall, "gamma_flip": lv.gamma_flip, "net_gex": lv.net_gex,
        "tp_value": tr.tp_value, "stop_value": tr.stop_value, "stop_below": tr.stop_below, "stop_above": tr.stop_above,
        "time_exit": tr.time_exit, "summary": tr.summary(),
    }


def review_journal(store: Store, src: DataSource, cfg: dict, cache: dict[str, SymbolResult]) -> list[dict]:
    """Mark every open idea from earlier runs and apply its exit rules. Updates the journal."""
    today = src.today
    out = []
    for idea in store.ideas(status="open", before=today):
        r = cache.get(idea["symbol"])
        if r is None or r.levels is None:
            r = scan_symbol(idea["symbol"], src, cfg, light=True)
            cache[idea["symbol"]] = r
        rv = review_idea(idea, r.chain, r.levels, r.tech, cfg, today)
        upd = {"last_mark": rv["mark"], "last_mark_date": today.isoformat(), "pnl": rv["pnl"]}
        if rv["status"] != "open":
            upd.update(status=rv["status"], closed_date=today.isoformat(), note=rv["exit_reason"])
        if rv["spot"] is not None:
            store.update_idea(idea["id"], **upd)
        out.append(rv)
    return out


def run(cfg: dict, fixtures: Path | None = None, today: dt.date | None = None, out_dir: Path | None = None,
        state_dir: Path | None = None, use_news: bool = True, log_ideas: bool = True) -> dict:
    today = today or dt.date.today()
    out_dir = Path(out_dir or cfg["output"]["report_dir"])
    state_dir = Path(state_dir or cfg["output"]["state_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    store = Store(state_dir)
    src = DataSource(cfg, fixtures=fixtures, cache_dir=state_dir / "cache", today=today, use_news=use_news)
    try:
        symbols = list(dict.fromkeys(cfg["watchlist"] + [s for s in (cfg.get("holdings") or {})]))
        results: list[SymbolResult] = []
        for i, sym in enumerate(symbols):
            log.info("scanning %s (%d/%d)", sym, i + 1, len(symbols))
            results.append(scan_symbol(sym, src, cfg, store))
            if not fixtures and i < len(symbols) - 1:
                time.sleep(cfg["data"].get("pause_between_symbols_s", 1.0))
        for r in results:
            if r.levels is not None:
                lv = r.levels
                store.save_snapshot(today, {
                    "symbol": r.symbol, "as_of": lv.as_of, "spot": lv.spot, "atm_iv": lv.atm_iv, "iv30": lv.atm_iv,
                    "net_gex": lv.net_gex, "call_wall": lv.call_wall, "put_wall": lv.put_wall, "gamma_flip": lv.gamma_flip,
                    "call_volume": lv.call_volume, "put_volume": lv.put_volume, "call_oi": lv.total_call_oi,
                    "put_oi": lv.total_put_oi, "rv20": r.tech.rv20 if r.tech else None}, r.chain.options)

        # 1) yesterday's (and older) ideas: mark, check exits, update the journal
        cache = {r.symbol: r for r in results}
        reviews = review_journal(store, src, cfg, cache)
        review_date = max((rv["run_date"] for rv in reviews), default=None)

        # 2) today's candidates, gated by the risk rules
        _llm_news(results, cfg)
        cands = [Candidate(r.symbol, r.trade, r) for r in results if r.trade]
        cands.sort(key=lambda c: c.res.score.total, reverse=True)
        cands = cands[: cfg["output"]["top_n"]]
        cands += [Candidate(r.symbol, r.cc, r) for r in results if r.cc]
        open_ideas = store.ideas(status="open")
        stops = recent_stops(store, today, cfg["risk"].get("cooling_off_after_stop_days", 1))
        ok, watch, risk_notes = gate(cands, [i for i in open_ideas if i["run_date"] < today.isoformat()], stops, cfg)
        if log_ideas:
            for c in ok:
                c.status = f"logged as idea #{store.add_idea(_idea_record(c, today))}"

        make_charts = cfg["output"].get("charts", True)
        details = [_detail(r, cfg, make_charts) for r in results]
        journal = store.ideas()
        ctx = {
            "run_date": today.isoformat(),
            "run_ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "top": [{"trade": c.trade.to_dict(), "score": c.res.score.to_dict() if c.res.score else {"symbol": c.symbol, "total": None,
                     "setup": c.trade.setup, "reasons": [], "warnings": []},
                     "levels": c.res.levels.to_dict(), "status": c.status,
                     "news": c.res.news.to_dict() if c.res.news else None} for c in ok],
            "watch": [{"trade": c.trade.to_dict(), "symbol": c.symbol, "status": c.status} for c in watch],
            "risk_notes": risk_notes,
            "rows": [_row(r) for r in sorted(results, key=lambda r: (r.score.total if r.score else -1), reverse=True)],
            "details": details,
            "reviews": reviews,
            "review_date": review_date,
            "open_ideas": [i for i in journal if i["status"] == "open"],
            "stats": weekly_stats(journal, today - dt.timedelta(days=7)),
            "stats_all": weekly_stats(journal),
            "errors": [e for r in results for e in r.errors],
            "fa_used": any(r.fa for r in results),
            "max_dte": cfg["data"]["max_dte_for_levels"],
            "disclaimer": DISCLAIMER,
            "cfg_trades": cfg["trades"],
        }
        write_outputs(ctx, out_dir, today)
        return ctx
    finally:
        store.close()


def write_outputs(ctx: dict, out_dir: Path, today: dt.date) -> None:
    env = Environment(loader=PackageLoader("gexscan", "templates"), autoescape=select_autoescape(["html"]))
    html = env.get_template("report.html").render(**ctx)
    (out_dir / f"{today.isoformat()}.html").write_text(html, encoding="utf-8")
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    slim = {k: v for k, v in ctx.items() if k != "details"}
    slim["details"] = [{k: v for k, v in d.items() if k != "charts"} for d in ctx["details"]]
    (out_dir / f"{today.isoformat()}.json").write_text(json.dumps(slim, default=str, indent=2))
    (out_dir / "summary.md").write_text(render_markdown(ctx), encoding="utf-8")
    ex = out_dir / "exports"
    ex.mkdir(exist_ok=True)
    pd.DataFrame(ctx["rows"]).to_csv(ex / f"scan_{today.isoformat()}.csv", index=False)
    if ctx["reviews"]:
        pd.DataFrame([{k: v for k, v in r.items() if k != "plan"} for r in ctx["reviews"]]).to_csv(
            ex / f"review_{today.isoformat()}.csv", index=False)


def _legs_txt(t: dict) -> str:
    return " / ".join(f"{g['action']} {g['strike']:g}{g['type']}" for g in t["legs"])


def render_markdown(ctx: dict) -> str:
    L = [f"# gexscan {ctx['run_date']}", ""]
    if ctx["reviews"]:
        L += ["## Review of open ideas", "", "| # | Ticker | Idea | Spot then → now | P/L (est.) | Setup | Action |", "|---|---|---|---|---|---|---|"]
        for r in ctx["reviews"]:
            then = f"{r['prev_spot']:.2f}" if r["prev_spot"] else "–"
            now = f"{r['spot']:.2f}" if r["spot"] else "–"
            pnl = f"${r['pnl']:+,.0f}" if r["pnl"] is not None else "–"
            L.append(f"| {r['id']} | {r['symbol']} | {r['summary']} | {then} → {now} | {pnl} | **{r['label']}** "
                     f"{'; '.join(r['triggers'])} | {r['action']} {r['exit_reason']} |")
        L.append("")
    for n in ctx["risk_notes"]:
        L += [f"> {n}", ""]
    L += ["## Trade ideas for today", ""]
    if not ctx["top"]:
        L.append("No idea passed the rules today. That's a valid outcome.")
    for i, item in enumerate(ctx["top"], 1):
        t, s = item["trade"], item["score"]
        kind = "credit" if t["net_mid"] > 0 else "debit"
        L += [f"**{i}. {t['symbol']} {t['setup']}** (score {s.get('total')}): {t['expiry']} {_legs_txt(t)} "
              f"@ {abs(t['net_mid']):.2f} {kind} x{t['contracts']}; max profit ${t['max_profit']:,.0f} / max loss ${t['max_loss']:,.0f}",
              f"   - Take profit: {t['take_profit']}", f"   - Stop: {t['stop']}", f"   - Time: {t['time_stop']}"]
        if item.get("news") and item["news"].get("summary"):
            L.append(f"   - News: {item['news']['summary']}")
        L.append("")
    if ctx["watch"]:
        L += ["## Watch only", ""] + [f"- {w['symbol']}: {_legs_txt(w['trade'])} ({w['status']})" for w in ctx["watch"]] + [""]
    L.append(f"_{ctx['disclaimer']}_")
    return "\n".join(L)
