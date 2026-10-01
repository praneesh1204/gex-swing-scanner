"""gexscan command line.

  gexscan                    same as `gexscan scan`
  gexscan scan               full daily run: review open ideas, scan the watchlist, write the report
  gexscan report NVDA        deep-dive on one ticker (nothing is logged)
  gexscan review             mark and check open ideas only
  gexscan journal list|take|close|stats

Read-only: this tool never places orders.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import webbrowser
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config, load_dotenv
from .pipeline import DataSource, review_journal, run, scan_symbol
from .review import weekly_stats
from .store import Store

app = typer.Typer(add_completion=False, no_args_is_help=False, help="Follow-the-money options scanner (read-only, ideas only).")
journal_app = typer.Typer(help="Your idea journal: list, mark taken, close, stats.")
app.add_typer(journal_app, name="journal")
con = Console()

CONFIG = typer.Option("config.yaml", "--config", "-c", help="config file")
STATE = typer.Option(None, "--state", help="state dir (journal database)")


def _setup(config: str, verbose: bool = False, symbols: str | None = None) -> dict:
    load_dotenv()
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("yfinance", "urllib3", "matplotlib", "peewee"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
    cfg = load_config(config)
    if symbols:
        cfg["watchlist"] = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    return cfg


def _money(x) -> str:
    return "–" if x is None else f"${x:+,.0f}"


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
            upd["stop_value"] = round(fill * (1 + t["stop_loss_credit_multiple"]), 2)
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


if __name__ == "__main__":
    app()
