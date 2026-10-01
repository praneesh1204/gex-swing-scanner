# CLAUDE.md: gexscan

Read-only options opportunity scanner ("follow the money"). Python package in `src/gexscan/`.

## Hard rules (never violate)
1. **No order code.** Never place, submit, modify or cancel orders. No broker order endpoints, not even behind a flag. The IBKR adapter uses an allow-list of read-only paths, with a test. Never call IBKR MCP order tools.
2. **Every idea has an exit plan.** Short premium: stop when the loss reaches 2× the credit (cost to close = 3× credit) or the underlying closes beyond the short strike, profit target (default 50 %), time exit. **No rolling** anywhere, including in text. Debit spreads: max loss = debit, a time stop and an invalidation price.
3. **No secrets in the repo.** Keys only via env / `.env` (git-ignored; see `.env.example`). Never log keys; redact URLs.
4. **Show freshness.** Provider + timestamp on every data point. OI is prior-day. CBOE timestamps are UTC.
5. **Model outputs are estimates.** The README carries a "not financial advice" disclaimer.
6. Never mix volume sources in one ratio (IBKR ≈ 60–70 % of consolidated). No lookahead in backtests or features.
7. Don't commit raw CBOE chains, personal picks or reports (the repo is public).

## Docs
- `docs/AUDIT.md`: v0.1 audit and gaps
- `docs/strategy_rules.md`: rules extracted from the course slides, with open decisions (❓)
- `docs/DATA_SOURCES.md`: provider capabilities and probes

## Dev
- Setup: `uv sync --extra dev`. Tests: `uv run pytest -q`
- Provider probe: `python scripts/probe_providers.py [--only cboe,...]`
- Offline mode: `uv run gexscan -s UPCO,DNCO,PINCO,EARN --fixtures tests/fixtures --today 2026-09-25 --out /tmp/demo --state /tmp/demo-state`

## Status
- v2.0.0 built (2026-09-30): Typer CLI (scan/report/review/journal), SQLite journal with exit-plan review, risk gate + cooling-off, flow, volume profile, vol stack, news (+optional Claude summary), charts, HTML/MD/JSON/CSV outputs.
- Not built yet: IBKR read-only adapter, Unusual Whales, Bigdata.com, alerts, backtests, Parquet export.
- Defaults chosen without user input (see README 'Exit rules'); user may revise.
