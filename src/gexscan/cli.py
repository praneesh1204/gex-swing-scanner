"""gexscan command line.

  gexscan                    same as `gexscan scan`
  gexscan scan               full daily run: review open ideas, scan the watchlist, write the report
  gexscan report NVDA        deep-dive on one ticker (nothing is logged)
  gexscan review             mark and check open ideas only
  gexscan journal list|take|close|stats
  gexscan recommend          strategy engine: gated, confidence-scored ideas with rationale + exit plan
  gexscan explain NVDA       every signal state and gate decision for one ticker
  gexscan alerts             2x-credit stop alerts on open short premium + regime alerts (alert only)
  gexscan backtest           synthetic replay of the strategy rules and exits (labelled SYNTHETIC)
  gexscan capabilities       which data providers / alert sinks are usable with your .env
  gexscan web                local website: themes -> ticker -> options scan -> visualizer, plus a strategy builder

Read-only: this tool never places, changes or cancels orders. It recommends and alerts; you act.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import webbrowser
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .config import load_config, load_dotenv, stop_multiple
from .logutil import redact, setup_logging
from .pipeline import DataSource, review_journal, run, scan_symbol
from .review import weekly_stats
from .store import Store

app = typer.Typer(add_completion=False, no_args_is_help=False, help="Follow-the-money options scanner (read-only, ideas only).")
journal_app = typer.Typer(help="Your idea journal: list, mark taken, close, stats.")
app.add_typer(journal_app, name="journal")
con = Console()

CONFIG = typer.Option("config.yaml", "--config", "-c", help="config file")
STATE = typer.Option(None, "--state", help="state dir (journal database)")
SYMBOLS = typer.Option(None, "--symbols", "-s", help="comma list overriding the watchlist")
FIXTURES = typer.Option(None, "--fixtures", help="offline fixtures dir (tests/demos)")
TODAY = typer.Option(None, "--today", help="override run date YYYY-MM-DD")
VERBOSE = typer.Option(False, "--verbose", "-v")
JSON_LOGS = typer.Option(False, "--json-logs", help="structured JSON log lines (secrets redacted)")
LOG_FILE = typer.Option(None, "--log-file", help="also write logs here")
OPEN = typer.Option(False, "--open", help="open the HTML page in your browser")


def _setup(config: str, verbose: bool = False, symbols: str | None = None, json_logs: bool = False,
           log_file: str | None = None) -> dict:
    load_dotenv()
    setup_logging(verbose, json_logs, log_file)
    cfg = load_config(config)
    if symbols:
        cfg["watchlist"] = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    return cfg


def _money(x) -> str:
    return "–" if x is None else f"{'-' if x < 0 else '+'}${abs(x):,.0f}"


def _print_reviews(reviews: list[dict]) -> None:
    if not reviews:
        con.print("[dim]No open ideas from earlier runs.[/dim]")
        return
    t = Table(title="Review of open ideas", show_lines=False)
    for c in ("#", "Ticker", "Idea", "Spot", "P/L est.", "Setup", "Action"):
        t.add_column(c)
    for r in reviews:
        style = {"stop": "red", "tp": "green", "time": "yellow"}.get(r["status"], "")
        lab = {"setup intact": "green", "watch/reversal": "yellow", "key level broken": "red"}.get(r["label"], "")
        t.add_row(str(r["id"]) + (" ✓" if r["taken"] else ""), r["symbol"], r["summary"],
                  f"{r['prev_spot'] or 0:.2f} → {r['spot'] or 0:.2f}", _money(r["pnl"]),
                  f"[{lab}]{r['label']}[/{lab}]" if lab else r["label"],
                  f"[{style}]{r['action']}[/{style}]" if style else r["action"])
    con.print(t)
    for r in reviews:
        if r["exit_reason"]:
            con.print(f"  #{r['id']} {r['symbol']}: {r['exit_reason']}")


def _print_ideas(ctx: dict) -> None:
    for n in ctx["risk_notes"]:
        con.print(f"[yellow]{n}[/yellow]")
    if not ctx["top"]:
        con.print("[bold]No trade idea passed the rules today.[/bold] Sitting out is a position.")
    for i, item in enumerate(ctx["top"], 1):
        t, s = item["trade"], item["score"]
        legs = " / ".join(f"{g['action']} {g['strike']:g}{g['type']}" for g in t["legs"])
        kind = "credit" if t["net_mid"] > 0 else "debit"
        con.print(f"\n[bold cyan]{i}. {t['symbol']} {t['setup'].replace('_', ' ')}[/bold cyan]  score {s.get('total')}  "
                  f"{t['expiry']} ({t['dte']} DTE)  {legs}  @ {abs(t['net_mid']):.2f} {kind}  x{t['contracts']}")
        con.print(f"   max profit ${t['max_profit']:,.0f} / max loss ${t['max_loss']:,.0f} per contract · "
                  f"POP≈{(t['pop_est'] or 0) * 100:.0f}% (est.)")
        con.print(f"   [green]TP[/green]   {t['take_profit']}")
        con.print(f"   [red]STOP[/red] {t['stop']}")
        con.print(f"   [yellow]TIME[/yellow] {t['time_stop']}")
        for w in s.get("warnings", []) + t["notes"]:
            con.print(f"   [dim]! {w}[/dim]")
        if item.get("news") and item["news"].get("summary"):
            con.print(f"   [dim]news: {item['news']['summary'][:300]}[/dim]")
        con.print(f"   [dim]{item['status']}[/dim]")
    if ctx["watch"]:
        con.print("\n[bold]Watch only:[/bold] " + "; ".join(f"{w['symbol']} ({w['status']})" for w in ctx["watch"]))


def _scan(config, symbols, fixtures, today, out, state, verbose, no_news, open_report):
    cfg = _setup(config, verbose, symbols)
    d = dt.date.fromisoformat(today) if today else None
    with con.status(f"Scanning {len(cfg['watchlist'])} tickers (chains, levels, volume profile, technicals, news)..."):
        ctx = run(cfg, fixtures=Path(fixtures) if fixtures else None, today=d,
                  out_dir=Path(out) if out else None, state_dir=Path(state) if state else None, use_news=not no_news)
    _print_reviews(ctx["reviews"])
    tb = Table(title=f"Scoreboard {ctx['run_date']}")
    for c in ("Ticker", "Spot", "Setup", "Score", "Put wall", "Call wall", "Flip", "IV/RV", "Trend", "Value area", "Flow", "News", "Status"):
        tb.add_column(c)
    for r in ctx["rows"]:
        tb.add_row(r["symbol"], f"{r['spot']:.2f}" if r["spot"] else "–", r["setup"], str(r["score"]), r["put_wall_txt"],
                   r["call_wall_txt"], r["flip_txt"], r["ivrv_txt"], r["trend"] or "–", r["vp_location"] or "–",
                   r["flow_bias"] or "–", (r["news_sentiment"] or "–") + (" ⚠" if r["news_flags"] else ""), r["status"])
    con.print(tb)
    _print_ideas(ctx)
    report = Path(out or cfg["output"]["report_dir"]) / "index.html"
    con.print(f"\nReport: [link=file://{report.resolve()}]{report}[/link]   (journal: {Path(state or cfg['output']['state_dir']) / 'gexscan.db'})")
    for e in ctx["errors"]:
        con.print(f"[dim]data issue: {e}[/dim]")
    con.print(f"[dim]{ctx['disclaimer']}[/dim]")
    if open_report:
        webbrowser.open(report.resolve().as_uri())


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context,
         config: str = CONFIG,
         symbols: Optional[str] = typer.Option(None, "--symbols", "-s", help="comma list overriding the watchlist"),
         fixtures: Optional[str] = typer.Option(None, help="offline fixtures dir (tests/demos)"),
         today: Optional[str] = typer.Option(None, help="override run date YYYY-MM-DD"),
         out: Optional[str] = typer.Option(None, help="report dir"),
         state: Optional[str] = STATE,
         verbose: bool = typer.Option(False, "--verbose", "-v"),
         no_news: bool = typer.Option(False, "--no-news", help="skip news research"),
         open_report: bool = typer.Option(False, "--open", help="open the HTML report when done")):
    """Run the daily scan when no sub-command is given."""
    if ctx.invoked_subcommand is None:
        _scan(config, symbols, fixtures, today, out, state, verbose, no_news, open_report)


@app.command()
def scan(config: str = CONFIG,
         symbols: Optional[str] = typer.Option(None, "--symbols", "-s"),
         fixtures: Optional[str] = typer.Option(None),
         today: Optional[str] = typer.Option(None),
         out: Optional[str] = typer.Option(None),
         state: Optional[str] = STATE,
         verbose: bool = typer.Option(False, "--verbose", "-v"),
         no_news: bool = typer.Option(False, "--no-news"),
         open_report: bool = typer.Option(False, "--open")):
    """Review open ideas, scan the watchlist, rank ideas, write reports/index.html."""
    _scan(config, symbols, fixtures, today, out, state, verbose, no_news, open_report)


@app.command()
def report(ticker: str, config: str = CONFIG, verbose: bool = typer.Option(False, "--verbose", "-v"),
           no_news: bool = typer.Option(False, "--no-news")):
    """Deep-dive on one ticker in the terminal (nothing is logged)."""
    cfg = _setup(config, verbose)
    st = Store(cfg["output"]["state_dir"])
    try:
        with con.status(f"Analysing {ticker.upper()}..."):
            r = scan_symbol(ticker.upper(), DataSource(cfg, use_news=not no_news), cfg, st)
    finally:
        st.close()
    if r.levels is None:
        con.print(f"[red]{'; '.join(r.errors)}[/red]")
        raise typer.Exit(1)
    lv, t, v, vp, f, n = r.levels, r.tech, r.vol, r.vp, r.flow, r.news
    con.rule(f"{r.symbol} {lv.spot:.2f}  ·  {lv.regime.replace('_', ' ')}  ·  {r.status}")
    con.print(f"[dim]{' · '.join(f'{k}: {x}' for k, x in r.fresh.items())}[/dim]")
    con.print(f"GEX {lv.net_gex / 1e6:,.1f}M $/1% · call wall {lv.call_wall} · put wall {lv.put_wall} · flip "
              f"{f'{lv.gamma_flip:.2f}' if lv.gamma_flip else lv.flip_status} · max pain {lv.max_pain_near} ({lv.near_expiry})")
    con.print(f"Call/put volume {lv.call_volume:,.0f}/{lv.put_volume:,.0f} · OI {lv.total_call_oi:,.0f}/{lv.total_put_oi:,.0f} · "
              f"flow bias {f.bias_label} ({f.bias:+.2f}, proxy)")
    if f.unusual:
        con.print("Unusual: " + ", ".join(f"{u['contract']} {u['volume']:,}/{u['oi']:,}" for u in f.unusual))
    if t.close:
        con.print(f"Trend {t.trend_label} · RSI {t.rsi14:.0f} · ATR {t.atr_pct:.1f}% · {t.pct_vs_sma20:+.1f}% vs SMA20 · "
                  f"breakout {t.breakout or '–'} · squeeze {'yes' if t.squeeze else 'no'}")
    if v.atm_iv:
        con.print(f"IV {v.atm_iv * 100:.0f}% · RV20 {(v.rv20 or 0) * 100:.0f}% · forecast {(v.garch_forecast or 0) * 100:.0f}% "
                  f"({v.garch_model}) · IV rank {v.iv_rank if v.iv_rank is not None else '–'} ({v.iv_rank_source})")
    if vp.poc:
        con.print(f"Volume profile ({vp.source}): POC {vp.poc:.2f} · VAH {vp.vah:.2f} · VAL {vp.val:.2f} · {vp.location} · "
                  f"HVN {vp.hvn} · LVN {vp.lvn}")
    if n:
        con.print(f"News ({', '.join(n.providers)}): {n.summary}")
        for i in n.items[:6]:
            con.print(f"  [dim]{i.published[:10]} {i.title} ({i.source})[/dim]")
    if r.score:
        con.print(f"Setup {r.score.setup} · score {r.score.total} · " + "; ".join(r.score.reasons))
        for w in r.score.warnings:
            con.print(f"  [yellow]! {w}[/yellow]")
    if r.trade:
        tr = r.trade
        con.print(f"\n[bold cyan]Idea:[/bold cyan] {tr.summary()} x{tr.contracts} · max P/L ${tr.max_profit:,.0f}/${tr.max_loss:,.0f}")
        con.print(f"  [green]TP[/green] {tr.take_profit}\n  [red]STOP[/red] {tr.stop}\n  [yellow]TIME[/yellow] {tr.time_stop}")
    if r.cc_status:
        con.print(f"Holdings: {r.cc_status}" + (f": {r.cc.summary()} · stop {r.cc.stop}" if r.cc else ""))


@app.command()
def review(config: str = CONFIG, state: Optional[str] = STATE, verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Mark open ideas on today's chains and check every exit rule (updates the journal)."""
    cfg = _setup(config, verbose)
    st = Store(state or cfg["output"]["state_dir"])
    try:
        with con.status("Marking open ideas..."):
            rv = review_journal(st, DataSource(cfg, use_news=False), cfg, {})
    finally:
        st.close()
    _print_reviews(rv)


@journal_app.command("list")
def journal_list(status: Optional[str] = typer.Option("open", help="open / tp / stop / time / expired / all"),
                 config: str = CONFIG, state: Optional[str] = STATE):
    """List ideas in the journal."""
    cfg = _setup(config)
    st = Store(state or cfg["output"]["state_dir"])
    rows = st.ideas(None if status == "all" else status)
    st.close()
    t = Table(title=f"Journal ({status})")
    for c in ("#", "Date", "Ticker", "Setup", "Idea", "Taken", "Status", "Last mark", "P/L est.", "Note"):
        t.add_column(c)
    for i in rows:
        t.add_row(str(i["id"]), i["run_date"], i["symbol"], i["setup"], i["summary"] or "", "✓" if i["taken"] else "",
                  i["status"], f"{i['last_mark']:.2f}" if i["last_mark"] is not None else "–", _money(i["pnl"]), i["note"] or "")
    con.print(t)


@journal_app.command("take")
def journal_take(idea_id: int, fill: Optional[float] = typer.Option(None, help="your actual fill per share (the net credit or debit you got)"),
                 contracts: Optional[int] = typer.Option(None), config: str = CONFIG, state: Optional[str] = STATE):
    """Mark an idea as taken (you placed it yourself, paper or real). Optionally record your fill."""
    cfg = _setup(config)
    st = Store(state or cfg["output"]["state_dir"])
    idea = next((i for i in st.ideas() if i["id"] == idea_id), None)
    if idea is None:
        st.close()
        con.print(f"[red]No idea #{idea_id}[/red]")
        raise typer.Exit(1)
    upd = {"taken": 1}
    if fill is not None:
        # re-anchor the exit plan on your actual fill (same rules: TP %, 2x-credit stop)
        t = cfg["trades"]
        fill = abs(fill) if idea["entry_net"] > 0 else -abs(fill)
        upd["entry_net"] = fill
        upd["summary"] = re.sub(r"@ [0-9.]+ (credit|debit)", lambda m: f"@ {abs(fill):.2f} {m.group(1)} (your fill)", idea["summary"] or "")
        if fill > 0:
            upd["tp_value"] = round(fill * (1 - t["take_profit_pct"]), 2)
            upd["stop_value"] = round(fill * stop_multiple(t), 2)
            upd["max_profit"] = round(fill * 100, 2)
            if idea.get("width"):
                upd["max_loss"] = round((idea["width"] - fill) * 100, 2)
        else:
            upd["max_loss"] = round(-fill * 100, 2)
            if idea.get("width"):
                upd["tp_value"] = round(-fill + t["debit_take_profit_pct"] * (idea["width"] + fill), 2)
                upd["max_profit"] = round((idea["width"] + fill) * 100, 2)
    if contracts:
        upd["contracts"] = contracts
    st.update_idea(idea_id, **upd)
    st.close()
    msg = f"Idea #{idea_id} marked as taken. Its exit plan is reviewed on every scan."
    if "tp_value" in upd:
        msg += f" Exit plan re-based on your fill: TP {upd['tp_value']:.2f}" + (f", stop {upd['stop_value']:.2f}" if "stop_value" in upd else "")
    con.print(msg)


@journal_app.command("close")
def journal_close(idea_id: int, price: Optional[float] = typer.Option(None, help="your exit price per share"),
                  reason: str = typer.Option("manual", help="tp / stop / time / manual"),
                  config: str = CONFIG, state: Optional[str] = STATE):
    """Record that you closed an idea (so it stops being reviewed)."""
    cfg = _setup(config)
    st = Store(state or cfg["output"]["state_dir"])
    idea = next((i for i in st.ideas() if i["id"] == idea_id), None)
    if idea is None:
        st.close()
        con.print(f"[red]No idea #{idea_id}[/red]")
        raise typer.Exit(1)
    upd = {"status": reason if reason in ("tp", "stop", "time") else "closed", "closed_date": dt.date.today().isoformat(),
           "note": f"closed by you ({reason})"}
    if price is not None:
        cur = price if idea["entry_net"] > 0 else -price
        upd["pnl"] = round((idea["entry_net"] - cur) * 100 * (idea["contracts"] or 1), 2)
        upd["last_mark"] = price
    st.update_idea(idea_id, **upd)
    st.close()
    con.print(f"Idea #{idea_id} closed ({reason}){' · P/L ' + _money(upd.get('pnl')) if 'pnl' in upd else ''}.")


@journal_app.command("stats")
def journal_stats(days: int = typer.Option(7, help="look-back window"), config: str = CONFIG, state: Optional[str] = STATE):
    """Weekly review: closed ideas, win rate, P/L by setup (estimates from delayed marks)."""
    cfg = _setup(config)
    st = Store(state or cfg["output"]["state_dir"])
    ideas = st.ideas()
    st.close()
    s = weekly_stats(ideas, dt.date.today() - dt.timedelta(days=days))
    if not s["n"]:
        con.print(f"No ideas closed in the last {days} days.")
        return
    con.print(f"Last {days} days: {s['n']} closed · {s['wins']} wins ({s['win_rate']:.0f}%) · P/L {_money(s['total_pnl'])} "
              f"(taken only: {_money(s['taken_pnl'])}) · exits {s['by_status']}")
    t = Table(title="By setup")
    for c in ("Setup", "Count", "Total P/L", "Avg P/L"):
        t.add_column(c)
    for b in s["by_setup"]:
        t.add_row(b["setup"], str(int(b["count"])), _money(b["sum"]), _money(b["mean"]))
    con.print(t)
    stops = s["by_status"].get("stop", 0)
    if stops >= 2:
        con.print("[yellow]Two or more stops this week. Consider trading smaller or sitting out a few days.[/yellow]")


# ---------------------------------------------------------------------------------------------------
# strategy engine

def _html_done(path: Path, open_page: bool, out: Console | None = None) -> None:
    """Print where the HTML page went (stderr under --json, so stdout stays clean) and optionally open it."""
    (out or con).print(f"HTML: [link={path.resolve().as_uri()}]{escape(str(path))}[/link]")
    if open_page:
        webbrowser.open(path.resolve().as_uri())


def _date(s: str | None) -> dt.date | None:
    return dt.date.fromisoformat(s) if s else None


def _csv(s: str | None) -> list[str] | None:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def _engine(cfg, symbols, strategy, min_confidence, top, max_per_ticker, allow_undefined, direction, vol_view,
            fixtures, today, state, no_news, theme=None, dte_min=None, dte_max=None):
    from .engine.recommend import Filters, recommend as run_engine
    from .engine.universe import load_universe

    f = Filters(symbols=[s.upper() for s in _csv(symbols)] if symbols else None, strategies=_csv(strategy),
                min_confidence=min_confidence, top_n=top, max_per_ticker=max_per_ticker,
                allow_undefined_risk=allow_undefined, directions=_csv(direction), vol_view=vol_view,
                themes=_csv(theme), dte_min=dte_min, dte_max=dte_max)
    uni = load_universe(cfg, fixtures=bool(fixtures))
    if f.themes:
        bad = [t for t in f.themes if t not in uni.themes and t != "outside"]
        if bad:
            raise typer.BadParameter(f"unknown theme(s) {', '.join(bad)}; choose from {', '.join(uni.themes)}, outside")
    n = len(f.symbols or (uni.symbols(f.themes) if f.themes else uni.scan_list(cfg["watchlist"])))
    with con.status(f"Strategy engine: {n} tickers (chains, vol, events, regime, gates)..."):
        return run_engine(None, f, cfg, Path(fixtures) if fixtures else None, _date(today),
                          Path(state) if state else None, use_news=not no_news)


@app.command()
def recommend(config: str = CONFIG, symbols: Optional[str] = SYMBOLS,
              strategy: Optional[str] = typer.Option(None, "--strategy", help="comma list, e.g. csp,iron_condor"),
              min_confidence: Optional[float] = typer.Option(None, "--min-confidence", help="0-100 floor (default 40)"),
              top: Optional[int] = typer.Option(None, "--top", help="max ideas"),
              max_per_ticker: Optional[int] = typer.Option(None, "--max-per-ticker"),
              allow_undefined: Optional[bool] = typer.Option(None, "--allow-undefined/--defined-only",
                                                             help="opt in to UNDEFINED-RISK ideas (short strangles); flagged"),
              direction: Optional[str] = typer.Option(None, "--direction", help="comma list: bullish,bearish,neutral"),
              vol_view: Optional[str] = typer.Option(None, "--vol", help="short / long"),
              theme: Optional[str] = typer.Option(None, "--theme", help="comma list of universe themes, e.g. optics,memory"),
              dte_min: Optional[int] = typer.Option(None, "--dte-min", help="only ideas with at least this many DTE"),
              dte_max: Optional[int] = typer.Option(None, "--dte-max", help="only ideas with at most this many DTE"),
              as_json: bool = typer.Option(False, "--json", help="print the JSON to stdout"),
              explain: bool = typer.Option(False, "--explain", help="every gate decision under each idea"),
              fixtures: Optional[str] = FIXTURES, today: Optional[str] = TODAY, state: Optional[str] = STATE,
              out: Optional[str] = typer.Option(None, "--out", help="dir for recommendations_<date>.json/.md/.html"),
              no_news: bool = typer.Option(False, "--no-news"), open_page: bool = OPEN, verbose: bool = VERBOSE,
              json_logs: bool = JSON_LOGS, log_file: Optional[str] = LOG_FILE):
    """Recommend option strategies: gates -> confidence -> rationale -> exit plan. Nothing is executed."""
    from .engine.recommend import write_recommendations
    from .reports.brief import markdown, print_brief, to_json
    from .reports.html import render_recommend, write_html

    cfg = _setup(config, verbose, None, json_logs, log_file)
    r = _engine(cfg, symbols, strategy, min_confidence, top, max_per_ticker, allow_undefined, direction, vol_view,
                fixtures, today, state, no_news, theme, dte_min, dte_max)
    od = Path(out or cfg["output"]["report_dir"])
    j, m = write_recommendations(r, od, markdown(r, explain))
    h = write_html(render_recommend(r, stop_mult=stop_multiple(cfg["trades"])), od / f"recommendations_{r.today}.html")
    if as_json:
        typer.echo(to_json(r))
        _html_done(h, open_page, Console(stderr=True))
        return
    print_brief(r, con, explain)
    con.print(f"\n[dim]Wrote {escape(str(j))} and {escape(str(m))}[/dim]")
    _html_done(h, open_page)


@app.command()
def explain(ticker: str, config: str = CONFIG, strategy: Optional[str] = typer.Option(None, "--strategy"),
            fixtures: Optional[str] = FIXTURES, today: Optional[str] = TODAY, state: Optional[str] = STATE,
            out: Optional[str] = typer.Option(None, "--out", help="dir for explain_<TICKER>_<date>.html"),
            no_news: bool = typer.Option(False, "--no-news"), open_page: bool = OPEN, verbose: bool = VERBOSE):
    """Every signal state, gate decision and build note for one ticker (why an idea did or didn't make it)."""
    from .reports.brief import print_explain
    from .reports.html import render_explain, write_html

    cfg = _setup(config, verbose)
    sym = ticker.upper()
    r = _engine(cfg, sym, strategy, 0, 50, 50, None, None, None, fixtures, today, state, no_news)
    print_explain(con, r, sym)              # signal states, the ideas that passed, strategy log, gates
    if sym in r.names and not any(c.symbol == sym for c in r.recommendations):
        con.print(f"\n[bold]No {escape(sym)} idea passed the gates.[/bold] The gate lines above say why.")
    h = write_html(render_explain(r, sym), Path(out or cfg["output"]["report_dir"]) / f"explain_{sym}_{r.today}.html")
    _html_done(h, open_page)


@app.command()
def alerts(config: str = CONFIG, symbols: Optional[str] = SYMBOLS,
           positions: Optional[str] = typer.Option(None, "--positions", help="positions YAML (default alerts.positions_file)"),
           ibkr: Optional[bool] = typer.Option(None, "--ibkr/--no-ibkr", help="read positions from the IBKR gateway (GET only)"),
           no_names: bool = typer.Option(False, "--no-names", help="skip the watchlist IV-rank / earnings checks"),
           sink: Optional[list[str]] = typer.Option(None, "--sink", help="console / log / webhook / telegram / email (repeat)"),
           dry_run: bool = typer.Option(False, "--dry-run", help="console only; don't remember alert state"),
           as_json: bool = typer.Option(False, "--json", help="print the run as JSON to stdout"),
           fixtures: Optional[str] = FIXTURES, today: Optional[str] = TODAY, state: Optional[str] = STATE,
           out_dir: Optional[str] = typer.Option(None, "--out", help="dir for alerts_<date>.html"), open_page: bool = OPEN,
           verbose: bool = VERBOSE, json_logs: bool = JSON_LOGS, log_file: Optional[str] = LOG_FILE):
    """Stop alerts (cost to close >= 2x credit) on open short premium + regime alerts. Alert only: no auto-action."""
    from .alerts import run_alerts
    from .alerts.sinks import FOOTER
    from .reports.html import render_alerts, write_html

    cfg = _setup(config, verbose, None, json_logs, log_file)
    out = Console(stderr=True) if as_json else con
    with out.status("Checking positions and regime..."):
        r = run_alerts(cfg, Path(fixtures) if fixtures else None, _date(today), Path(state) if state else None,
                       positions, ibkr, [s.upper() for s in _csv(symbols)] if symbols else None, not no_names,
                       sink or None, dry_run, out)
    h = write_html(render_alerts(r), Path(out_dir or cfg["output"]["report_dir"]) / f"alerts_{r.today}.html")
    if as_json:
        typer.echo(redact(json.dumps(r.to_dict(), indent=2, default=str)))
        _html_done(h, open_page, out)
        return
    if r.positions:
        t = Table(title=f"Open short premium ({len(r.positions)})")
        for c in ("Ticker", "Strategy", "Expiry", "Legs", "Credit", "Cost to close", "x credit", "Stop at", "P/L", "State", "Source"):
            t.add_column(c)
        for p in r.positions:
            st = p.get("state", "")
            sty = {"stop": "bold red", "warn": "yellow", "ok": "green"}.get(st, "dim")
            t.add_row(p["symbol"], p.get("strategy") or "", p.get("expiry") or "", escape(p.get("legs", "")),
                      f"{p['credit']:.2f}", f"{p['cost_to_close']:.2f}" if "cost_to_close" in p else "–",
                      f"{p['ratio']:.2f}" if "ratio" in p else "–", f"{p['stop_at']:.2f}" if "stop_at" in p else "–",
                      _money(p.get("pnl")), f"[{sty}]{st}[/{sty}]", escape(f"{p['source']} {p['ref']}"))
        con.print(t)
    else:
        con.print("[dim]No open short-premium positions (positions.yaml, journal 'taken' ideas, IBKR if enabled).[/dim]")
    con.print(f"{len(r.alerts)} alert(s) sent, {len(r.suppressed)} unchanged since last run (not re-sent).")
    for k, v in r.delivered.items():
        con.print(f"  [dim]{k}: {escape(v)}[/dim]")
    for e in r.errors:
        con.print(f"[dim]data issue: {escape(redact(e))}[/dim]")
    con.print(f"[dim]{FOOTER}[/dim]")
    _html_done(h, open_page)


@app.command()
def backtest(config: str = CONFIG, symbols: Optional[str] = SYMBOLS,
             strategy: Optional[str] = typer.Option(None, "--strategy", help="comma list (csp,bull_put,bear_call,iron_condor,calendar)"),
             years: Optional[float] = typer.Option(None, "--years"),
             as_json: bool = typer.Option(False, "--json"), trades: bool = typer.Option(False, "--trades", help="list every trade"),
             fixtures: Optional[str] = FIXTURES, today: Optional[str] = TODAY, state: Optional[str] = STATE,
             out: Optional[str] = typer.Option(None, "--out", help="dir for backtest_<date>.json/.md/.html"),
             open_page: bool = OPEN, verbose: bool = VERBOSE):
    """SYNTHETIC backtest: BS repricing on an RV-based IV proxy with the live entry gates and exit plan."""
    from .backtest import markdown, run_backtest
    from .reports.html import render_backtest, write_html

    cfg = _setup(config, verbose)
    d = _date(today) or dt.date.today()
    with con.status("Replaying entries and exits..."):
        r = run_backtest(cfg, [s.upper() for s in _csv(symbols)] if symbols else None, _csv(strategy), years,
                         Path(fixtures) if fixtures else None, d, Path(state) if state else None)
    od = Path(out or cfg["output"]["report_dir"])
    od.mkdir(parents=True, exist_ok=True)
    (od / f"backtest_{d}.json").write_text(json.dumps(r.to_dict(), indent=2, default=str))
    (od / f"backtest_{d}.md").write_text(markdown(r))
    h = write_html(render_backtest(r, d), od / f"backtest_{d}.html")
    if as_json:
        typer.echo(json.dumps(r.to_dict(), indent=2, default=str))
        _html_done(h, open_page, Console(stderr=True))
        return
    con.rule(f"Backtest {r.start} to {r.end} · {', '.join(r.symbols)}")
    con.print(f"[bold yellow]{escape(r.label)}[/bold yellow]")
    t = Table()
    for c in ("Strategy", "Trades", "Win rate", "Avg P/L", "Total P/L", "Max DD", "Avg days", "Exits", "Top skips"):
        t.add_column(c)
    for k, s in r.stats.items():
        ex = ", ".join(f"{a} {b}" for a, b in sorted(s.exits.items())) + (f", open {s.open}" if s.open else "")
        sk = ", ".join(f"{a}: {b}" for a, b in sorted(s.skipped.items(), key=lambda x: -x[1])[:3])
        t.add_row(k, str(s.n), "–" if s.win_rate is None else f"{s.win_rate:.0f}%", _money(s.avg_pnl),
                  _money(s.total_pnl), _money(-s.max_drawdown), "–" if s.avg_days is None else f"{s.avg_days:g}",
                  escape(ex or "–"), escape(sk or "–"))
    con.print(t)
    if trades:
        tt = Table(title="Trades")
        for c in ("Ticker", "Strategy", "Entry", "Exit", "Legs", "Net", "Exit value", "P/L", "Reason"):
            tt.add_column(c)
        for x in r.trades:
            tt.add_row(x.symbol, x.strategy, x.entry, x.exit, escape(x.legs), f"{x.net:+.2f}", f"{x.exit_value:+.2f}",
                       _money(x.pnl), x.reason)
        con.print(tt)
    for a in r.assumptions[1:]:
        con.print(f"[dim]· {escape(a)}[/dim]")
    for e in r.errors:
        con.print(f"[dim]data issue: {escape(e)}[/dim]")
    con.print(f"[dim]Wrote {od / f'backtest_{d}.md'} (+ .json). Synthetic estimates, not financial advice.[/dim]")
    _html_done(h, open_page)


@app.command()
def capabilities(config: str = CONFIG, as_json: bool = typer.Option(False, "--json")):
    """Which providers and alert sinks are usable with your .env. Shows key presence only, never values."""
    from .alerts.sinks import make_sinks
    from .backtest import optopsy_adapter
    from .data.providers import capabilities as caps

    cfg = _setup(config)
    rows = caps()
    sinks = {s.name: s.ready() for s in make_sinks(["console", "log", "webhook", "telegram", "email"], "alerts.log")}
    extra = {"sinks": {k: "ready" if ok else f"off ({why})" for k, (ok, why) in sinks.items()},
             "optopsy": "installed (AGPL-3.0, optional)" if optopsy_adapter.available() else "not installed (optional)",
             "positions_file": cfg["alerts"].get("positions_file"), "ibkr_positions": bool(cfg["alerts"].get("use_ibkr"))}
    if as_json:
        typer.echo(json.dumps({"providers": rows, **extra}, indent=2))
        return
    t = Table(title="Data providers")
    for c in ("Provider", "Category", "Kind", "Keys", "Status", "Notes"):
        t.add_column(c)
    for p in rows:
        sty = {"ready": "green", "key missing": "yellow"}.get(p["status"], "dim")
        t.add_row(p["provider"], p["category"], p["kind"], p["keys"], f"[{sty}]{p['status']}[/{sty}]", escape(p["notes"]))
    con.print(t)
    con.print("Alert sinks: " + ", ".join(f"{k} {v}" for k, v in extra["sinks"].items()))
    con.print(f"optopsy: {extra['optopsy']} · positions file: {extra['positions_file']} · "
              f"IBKR positions: {'on' if extra['ibkr_positions'] else 'off'}")
    con.print("[dim]Live probe of what each key's tier returns: python scripts/probe_providers.py[/dim]")


@app.command()
def web(config: str = CONFIG, port: int = typer.Option(None, "--port", help="local port (default web.port, 8765)"),
        fixtures: Optional[str] = FIXTURES, today: Optional[str] = TODAY, state: Optional[str] = STATE,
        no_news: bool = typer.Option(False, "--no-news"), open_page: bool = OPEN, verbose: bool = VERBOSE,
        json_logs: bool = JSON_LOGS, log_file: Optional[str] = LOG_FILE):
    """Local website on 127.0.0.1: pick a theme, read a ticker, scan it, visualize trades, build your own strategy.

    Read-only, GET-only, bound to this machine. A web scan never writes to the journal.
    """
    from .web.server import serve

    cfg = _setup(config, verbose, None, json_logs, log_file)
    p = int(port or cfg.get("web", {}).get("port", 8765))
    url = f"http://127.0.0.1:{p}/"
    mode = f"FIXTURES ({fixtures})" if fixtures else "LIVE (CBOE delayed)"
    con.print(f"gexscan web · {mode} · [bold]{url}[/bold]  (Ctrl+C to stop)")
    con.print("[dim]Ideas and estimates only, not financial advice. This app never places orders.[/dim]")
    if open_page:
        webbrowser.open(url)
    serve(cfg, p, Path(fixtures) if fixtures else None, _date(today), Path(state) if state else None,
          use_news=not no_news)


if __name__ == "__main__":
    app()
