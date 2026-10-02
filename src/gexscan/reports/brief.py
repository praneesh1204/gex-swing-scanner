"""Recommendation brief (spec §6): Rich console, markdown and JSON views of a Recommendations object.

Every idea shows its legs, net (mid and natural), width, max profit / max loss, breakevens, PoP (estimate),
DTE, the exit plan headed by its rule ("Stop = 2x credit; no rolling" for short premium, "max loss = the
debit" for debit trades), the confidence with sub-scores and penalties, the three rationale levels and every
gate line. The brief ends at the idea. There is no execution step: you decide and place every trade yourself.
"""
from __future__ import annotations

import json

from rich.console import Console
from rich.markup import escape as esc
from rich.table import Table

NO_EXEC = ("Ideas only. This tool never places, changes or cancels orders, in any broker or mode: "
           "you decide and place every trade yourself.")
LEVELS = (("why_premium", "1. Why premium is rich or cheap"),
          ("market_pricing", "2. What the market is pricing"),
          ("second_order", "3. Second-order effects"))
MARK = {"pass": "✓", "fail": "✗", "unknown": "?", "warn": "!", "ok": "·"}


def _money(x) -> str:
    return "n/a" if x is None else f"${x:,.0f}"


def _num(x, fmt="{:.2f}") -> str:
    return "n/a" if x is None else fmt.format(x)


# ---------------------------------------------------------------------------------------------------
# shared text building (both renderers use these, so console and markdown never disagree)

def market_lines(r) -> list[str]:
    m = r.market or {}
    if not m:
        return ["Market data disabled or unavailable."]
    st = {k: v.get("state") for k, v in (r.market_states or {}).items()}
    out = [f"VIX {_num(m.get('vix'))} / VIX3M {_num(m.get('vix3m'))} = {_num(m.get('ts_ratio'), '{:.3f}')} "
           f"({st.get('vix_ts', '?')}) · VIX9D {_num(m.get('vix9d'))} · VIX {st.get('vix_level', '?')}"
           + (f", {m['vix_pct_1y']:.0f}th pct of 1y" if m.get("vix_pct_1y") is not None else "")
           + f" · VVIX {_num(m.get('vvix'), '{:.1f}')}"
           + (f" ({m['vvix_5d_pct']:+.1f}% 5d, {st.get('vvix', '?')})" if m.get("vvix_5d_pct") is not None else "")]
    rates = []
    if m.get("fed_funds") is not None:
        rates.append(f"Fed funds {m['fed_funds']:.2f}%")
    if m.get("y2") is not None:
        rates.append(f"2y {m['y2']:.2f}%")
    if m.get("y10") is not None:
        rates.append(f"10y {m['y10']:.2f}%" + (f" ({m['y10_20d_bp']:+.0f}bp 20d)" if m.get("y10_20d_bp") is not None else ""))
    if m.get("curve_10y2y") is not None:
        rates.append(f"10y-2y {m['curve_10y2y'] * 100:+.0f}bp")
    if m.get("wti") is not None:
        rates.append(f"WTI ${m['wti']:.1f}" + (f" ({m['wti_20d_pct']:+.1f}% 20d)" if m.get("wti_20d_pct") is not None else ""))
    if m.get("cpi_yoy") is not None:
        rates.append(f"CPI {m['cpi_yoy']:.1f}% y/y")
    if rates:
        out.append("Rates / oil: " + " · ".join(rates))
    pc = m.get("put_call") or {}
    if pc:
        out.append("CBOE put/call: " + " · ".join(f"{k} {v:.2f}" for k, v in pc.items() if isinstance(v, (int, float)))
                   + (f" ({pc['date']})" if pc.get("date") else ""))
    if m.get("events"):
        out.append("Next events: " + ", ".join(f"{e['event']} {e['date']} ({e['days']}d)" for e in m["events"][:5]))
    out.append(f"Macro state: {st.get('macro', '?')}")
    return out


def filters_line(r) -> str:
    f = r.filters
    return (f"min confidence {f.get('min_confidence', 40):.0f} (floor {f.get('floor', 40):.0f}) · top "
            f"{f.get('top_n')} · max {f.get('max_per_ticker')} per ticker · undefined risk "
            f"{'allowed' if f.get('allow_undefined_risk') else 'excluded'}"
            + (f" · strategies {', '.join(f['strategies'])}" if f.get("strategies") else ""))


def idea_header(i: int, c) -> str:
    conf = c.confidence
    risk = "UNDEFINED RISK" if c.risk == "undefined" else "defined risk"
    return (f"{i}. {c.symbol} {c.label}: {conf.score:.1f} ({conf.bucket})  ·  {c.direction or 'neutral'}, "
            f"{c.vol_view or '?'} vol, {risk}")


def idea_facts(c) -> list[str]:
    exp = c.expiry + (f" / {c.back_expiry}" if c.back_expiry else "")
    be = " / ".join(f"{b:g}" for b in c.breakevens) or "n/a"
    out = [" / ".join(l.text() for l in c.legs),
           f"Net {abs(c.net):.2f} {c.kind} at mid ({abs(c.net_natural):.2f} natural) · "
           + (f"width {c.width:g} · " if c.width else "")
           + f"max profit {_money(c.max_profit)}"
           + (" (est.)" if c.back_expiry else "")
           + f" · max loss {_money(c.max_loss) if c.max_loss is not None else 'UNDEFINED'} per contract · x{c.contracts}",
           f"Breakevens {be} · PoP ≈ {c.pop * 100:.0f}% (model estimate) · {c.dte} DTE ({exp}) · spot {c.spot:g}"
           if c.pop is not None else f"Breakevens {be} · {c.dte} DTE ({exp}) · spot {c.spot:g}"]
    m = c.metrics
    extra = []
    if m.get("credit_to_width") is not None:
        extra.append(f"credit/width {m['credit_to_width']:.0%}")
    if m.get("liquidity_score") is not None:
        extra.append(f"liquidity {m['liquidity_score']:.2f}")
    if m.get("roundtrip_pct_of_net") is not None:
        extra.append(f"round-trip spread {m['roundtrip_pct_of_net']:.0%} of net")
    if m.get("expected_move"):
        em = m["expected_move"]
        extra.append(f"expected move ±{em['em']:g} ({em['em_pct']:.1f}%) to {em['expiry']}: {em['lower']:g}-{em['upper']:g}")
    if extra:
        out.append(" · ".join(extra))
    return out


def exit_lines(c) -> list[tuple[str, str]]:
    out = [("Take profit", c.take_profit), ("Stop", c.stop), ("Time", c.time_stop)]
    inval = c.invalidation or c.rationale.get("invalidation")
    if inval:
        out.append(("Invalidation", inval))
    return [(k, v) for k, v in out if v]


def sub_rows(c) -> list[tuple[str, str, str]]:
    rows = []
    for cat, s in c.confidence.sub.items():
        rows.append((cat, f"{s['points']:.1f}/{s['max']:g}", s.get("notes") or ""))
    return rows


def gate_lines(c) -> list[str]:
    if not c.gates:
        return []
    out = []
    for d in c.gates.decisions:
        line = f"{MARK.get(d.outcome, '?')} {d.kind} {d.gate}: {d.state}, wants {d.want}"
        if d.detail:
            line += f" ({d.detail})"
        out.append(line)
    return out


# ---------------------------------------------------------------------------------------------------
# markdown

def markdown(r, explain: bool = False) -> str:
    L = [f"# Strategy engine brief: {r.today}", "",
         f"_generated {r.generated} · estimates from delayed data; check live quotes before acting_", "",
         f"> {NO_EXEC} Not financial advice.", "", "## Market", ""]
    L += [f"- {x}" for x in market_lines(r)]
    L += ["", f"## Ideas ({len(r.recommendations)})", "", f"_filters: {filters_line(r)}_", ""]
    if not r.recommendations:
        L += ["No idea passed the gates and the confidence floor today. Sitting out is a position.", ""]
    else:
        L += ["| # | Ticker | Strategy | Expiry | Net | Max loss | PoP | Exit rule | Confidence |",
              "|---|---|---|---|---|---|---|---|---|"]
        for i, c in enumerate(r.recommendations, 1):
            L.append(f"| {i} | {c.symbol} | {c.label} | {c.expiry} ({c.dte}d) | {abs(c.net):.2f} {c.kind} | "
                     f"{_money(c.max_loss) if c.max_loss is not None else 'UNDEFINED'} | "
                     f"{'n/a' if c.pop is None else f'{c.pop:.0%}'} | {c.stop_rule} | "
                     f"{c.confidence.score:.1f} {c.confidence.bucket} |")
        L.append("")
    for i, c in enumerate(r.recommendations, 1):
        L += [f"### {idea_header(i, c)}", ""]
        if c.flags:
            L += ["**Flags:** " + ", ".join(f"`{x}`" for x in c.flags), ""]
        facts = idea_facts(c)
        L += [f"**Legs:** {facts[0]}  ", *[f"{x}  " for x in facts[1:]], ""]
        L += [f"**Exit plan: {c.stop_rule}**", ""] + [f"- **{k}:** {v}" for k, v in exit_lines(c)] + [""]
        L += [f"**Confidence {c.confidence.score:.1f}/100 ({c.confidence.bucket})**", "",
              "| Category | Points | Why |", "|---|---|---|"]
        L += [f"| {a} | {b} | {n} |" for a, b, n in sub_rows(c)]
        for p in c.confidence.penalties:
            L.append(f"| penalty | -{p['points']:g} | {p['reason']} |")
        L += ["", "**Rationale**", ""]
        for key, title in LEVELS:
            L.append(f"*{title}*")
            L += [f"- {x}" for x in c.rationale.get(key) or ["(no data)"]]
            L.append("")
        L += ["<details><summary>Gates</summary>", ""] + [f"- {x}" for x in gate_lines(c)] + ["", "</details>", ""]
        notes = [f"⚠ {w}" for w in c.warnings] + c.notes
        if notes:
            L += [f"- {x}" for x in notes] + [""]
        if c.sources:
            L += ["_data: " + " · ".join(f"{k}: {v}" for k, v in c.sources.items()) + "_", ""]
    if r.rejected:
        L += ["## Scored but not shown", "", "| Ticker | Strategy | Confidence | Why |", "|---|---|---|---|"]
        L += [f"| {d['symbol']} | {d['strategy']} | {_num(d['confidence'], '{:.1f}')} | {d['why']} |" for d in r.rejected]
        L.append("")
    if explain:
        L += ["## Explain (why each strategy did or didn't fire)", ""]
        for sym, lines in r.explain.items():
            L += [f"### {sym}", ""] + [f"- {x}" for x in lines] + [""]
    L += ["## Data", ""]
    for sym, n in r.names.items():
        fresh = " · ".join(f"{k}: {v}" for k, v in (n.get("fresh") or {}).items())
        L.append(f"- **{sym}** {fresh}" + (f" · errors: {'; '.join(n['errors'])}" if n.get("errors") else ""))
    caps = [c for c in r.capabilities if c["wired"]]
    L += ["", "Providers: " + ", ".join(f"{c['provider']} ({c['status']})" for c in caps), ""]
    if r.errors:
        L += ["## Data issues", ""] + [f"- {e}" for e in r.errors] + [""]
    L += ["---", f"_{r.disclaimer}_", ""]
    return "\n".join(L)


def to_json(r) -> str:
    return json.dumps(r.to_dict(), indent=2, default=str)


# ---------------------------------------------------------------------------------------------------
# console

def print_brief(r, con: Console | None = None, explain: bool = False, detail: bool = True) -> None:
    con = con or Console()
    con.rule(f"Strategy engine · {r.today}")
    con.print(f"[dim]{NO_EXEC}[/dim]")
    for x in market_lines(r):
        con.print(f"  {esc(x)}")
    if not r.recommendations:
        con.print("\n[bold]No idea passed the gates and the confidence floor today.[/bold] Sitting out is a position.")
    else:
        t = Table(title=f"Ideas ({len(r.recommendations)})", show_lines=False)
        for col in ("#", "Ticker", "Strategy", "Expiry", "Net", "Max loss", "PoP", "Exit rule", "Conf"):
            t.add_column(col)
        for i, c in enumerate(r.recommendations, 1):
            col = {"High": "green", "Medium": "yellow", "Low": "red"}[c.confidence.bucket]
            t.add_row(str(i), c.symbol, esc(c.label), f"{c.expiry} ({c.dte}d)", f"{abs(c.net):.2f} {c.kind}",
                      _money(c.max_loss) if c.max_loss is not None else "[red]UNDEFINED[/red]",
                      "–" if c.pop is None else f"{c.pop:.0%}", esc(c.stop_rule),
                      f"[{col}]{c.confidence.score:.0f} {c.confidence.bucket}[/{col}]")
        con.print(t)
    if detail:
        for i, c in enumerate(r.recommendations, 1):
            print_idea(con, i, c)
    if r.rejected:
        con.print("\n[bold]Scored but not shown[/bold]")
        for d in r.rejected:
            con.print(f"  [dim]{d['symbol']} {d['strategy']} ({_num(d['confidence'], '{:.0f}')}): {esc(d['why'])}[/dim]")
    if explain:
        con.rule("Explain")
        for sym, lines in r.explain.items():
            con.print(f"[bold]{sym}[/bold]")
            for x in lines:
                con.print(f"  [dim]{esc(x)}[/dim]")
    for e in r.errors:
        con.print(f"[dim]data issue: {esc(e)}[/dim]")
    con.print(f"[dim]{esc(r.disclaimer)}[/dim]")


def print_idea(con: Console, i: int, c) -> None:
    col = {"High": "green", "Medium": "yellow", "Low": "red"}[c.confidence.bucket]
    con.print(f"\n[bold cyan]{esc(idea_header(i, c))}[/bold cyan]")
    if c.flags:
        con.print("  " + " ".join(f"[reverse] {esc(x)} [/reverse]" for x in c.flags))
    for x in idea_facts(c):
        con.print(f"  {esc(x)}")
    con.print(f"  [bold red]{esc(c.stop_rule)}[/bold red]")
    for k, v in exit_lines(c):
        tag = {"Take profit": "green", "Stop": "red", "Time": "yellow"}.get(k, "magenta")
        con.print(f"   [{tag}]{k}[/{tag}] {esc(v)}")
    t = Table(show_header=True, box=None, padding=(0, 1), title=f"[{col}]confidence {c.confidence.score:.1f}[/{col}]",
              title_justify="left")
    for h in ("category", "points", "why"):
        t.add_column(h)
    for a, b, n in sub_rows(c):
        t.add_row(a, b, esc(n))
    for p in c.confidence.penalties:
        t.add_row("[red]penalty[/red]", f"[red]-{p['points']:g}[/red]", esc(p["reason"]))
    con.print(t)
    for key, title in LEVELS:
        con.print(f"  [bold]{title}[/bold]")
        for x in c.rationale.get(key) or ["(no data)"]:
            con.print(f"   · {esc(x)}")
    con.print("  [bold]Gates[/bold]")
    for x in gate_lines(c):
        style = "red" if x.startswith("✗") else "yellow" if x[0] in "?!" else "dim"
        con.print(f"   [{style}]{esc(x)}[/{style}]")
    for w in c.warnings:
        con.print(f"  [yellow]! {esc(w)}[/yellow]")
    for n in c.notes:
        con.print(f"  [dim]{esc(n)}[/dim]")
    if c.sources:
        con.print("  [dim]data: " + esc(" · ".join(f"{k}: {v}" for k, v in c.sources.items())) + "[/dim]")


def print_explain(con: Console, r, sym: str) -> None:
    """`gexscan explain TICKER`: every signal state, then each strategy's gate lines and build log."""
    sym = sym.upper()
    n = r.names.get(sym)
    if n is None:
        con.print(f"[red]{sym} not in this run[/red]")
        return
    con.rule(f"{sym} · spot {n.get('spot')} · as of {n.get('as_of')}")
    t = Table(title="Signal states (market + name)", show_lines=False)
    for h in ("signal", "state", "value", "detail"):
        t.add_column(h)
    for k, s in {**(r.market_states or {}), **n["states"]}.items():
        v = s.get("value")
        t.add_row(esc(k + (" [PROXY]" if s.get("approx") else "")), esc(str(s.get("state", ""))),
                  "" if v is None else esc(f"{v:.3g}" if isinstance(v, (int, float)) else str(v)), esc(str(s.get("detail") or "")))
    con.print(t)
    for k, v in (n.get("fresh") or {}).items():
        con.print(f"  [dim]{esc(f'{k}: {v}')}[/dim]")
    shown = [c for c in r.recommendations if c.symbol == sym]
    for i, c in enumerate(shown, 1):
        print_idea(con, i, c)
    con.print("\n[bold]Strategy log[/bold]")
    for x in r.explain.get(sym, []):
        con.print(f"  {esc(x)}")
    if r.book is not None:
        for name, gr in r.book.gates.get(sym, {}).items():
            con.print(f"\n  [bold]{name}[/bold] gates {'passed' if gr.passed else '[red]failed[/red]'}")
            for g in gr.decisions:
                style = "red" if g.outcome == "fail" else "yellow" if g.outcome in ("unknown", "warn") else "dim"
                con.print(f"   [{style}]{esc(g.line())}[/{style}]")
    for e in n.get("errors") or []:
        con.print(f"[dim]data issue: {esc(e)}[/dim]")
