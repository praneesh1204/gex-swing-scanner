# CLAUDE.md: gexscan

Read-only options opportunity scanner ("follow the money") and strategy engine. Python package in `src/gexscan/`. It recommends and alerts; the user acts.

## Hard rules (never violate)
1. **No order code.** Never place, submit, modify or cancel orders, in any broker, live or paper. No broker order endpoints, not even behind a flag. The IBKR adapter is GET-only with an allow-list of read-only paths, with a test. Never call IBKR MCP order tools. `tests/test_v2.py::test_no_order_placement_code` greps `src/` for order patterns (avoid "/order" in strings); `tests/test_web.py::test_front_end_has_no_order_code` does the same for `web/static` js/html/css. The web app stays 127.0.0.1-only and GET/HEAD-only, with no forms and safe DOM only (spec v2.2 W1–W8, all tested).
2. **Every idea has an exit plan.** Short premium: stop when the cost to close reaches 2× the credit received (loss = 1× credit; `stop_credit_multiple: 2.0`) or the underlying closes beyond the short strike, profit target (default 50 %), time exit. The stop is computed at recommendation time and `gexscan alerts` watches it: alert only, never an action. **No rolling** anywhere, including in text. Debit spreads/calendars: max loss = debit, a time stop and an invalidation price.
3. **No secrets in the repo.** Keys only via env / `.env` (git-ignored; see `.env.example`). Never log keys; `logutil.redact` masks them in every log line.
4. **Show freshness.** Provider + timestamp on every data point. OI is prior-day. CBOE timestamps are UTC.
5. **Model outputs are estimates.** The README carries a "not financial advice" disclaimer. Every recommendation has a confidence score and a three-level rationale. The backtest is labelled SYNTHETIC.
6. Never mix volume sources in one ratio (IBKR ≈ 60–70 % of consolidated). No lookahead in backtests or features (there is a test).
7. Don't commit raw CBOE chains, personal picks, `positions.yaml` or reports (the repo is public).
8. **Defined risk by default.** Undefined-risk structures (short strangle) only with `engine.allow_undefined_risk` / `--allow-undefined`, always flagged `UNDEFINED RISK`.
9. **Licences.** Borrow ideas, not code. Never vendor or import AGPL/GPL code (optopsy, optionlab, OpenBB): optional adapters only, behind guarded imports. Credit sources in the README.

## Docs
- `docs/AUDIT.md`: v0.1 audit and gaps
- `docs/strategy_rules.md`: rules extracted from the course slides, with open decisions (❓)
- `docs/DATA_SOURCES.md`: provider capabilities and probes
- `src/gexscan/config/signal_sheet.yaml`: the engine's gating ruleset (signals, strategies, confidence)
- `docs/specs/v2.2-visualizer-spec.md` + `docs/specs/architecture.html`: tech universe, web app, visualizer, simulation (Rev 3, as built)
- `docs/PROCESS.md`: how v2.2 was specified, built, tested and reviewed
- Docs and examples use synthetic tickers only (DEMO, fixtures). The user's real positions never go in the repo; `options-webapp-design-doc.md` is the user's private doc (git-ignored)

## Dev
- Setup: `uv sync --extra dev`. Tests: `uv run pytest -q` (offline; `tests/conftest.py` regenerates missing fixtures)
- Fixtures: `uv run python tests/make_fixtures.py tests/fixtures [--engine-only]` (TODAY = 2026-09-25)
- Provider probe: `python scripts/probe_providers.py [--only cboe,...]`
- Offline mode: add `--fixtures tests/fixtures --today 2026-09-25 --state /tmp/demo-state` to `gexscan`, `recommend`, `explain`, `alerts`, `backtest` or `web`. Fixture names: UPCO, DNCO, PINCO, EARN, RICH, TERM.
- Web app: `uv run gexscan web --fixtures tests/fixtures --today 2026-09-25 --state /tmp/gexweb-state --no-news --port 8765`. Static JS/CSS is re-read per request; `app.html` is read once at start-up (restart after editing it).
- Golden pricing vectors: `uv run --with scipy python scripts/make_golden.py` (independent of `analytics/pricing.py`). JS parity: `node tests/js/parity.mjs` (run by `test_pricing.py`).
- `gexscan.engine` re-exports `recommend` under the module's name: monkeypatch via `importlib.import_module("gexscan.engine.recommend")`.

## Status
- v2.0.0 built (2026-09-30): Typer CLI (scan/report/review/journal), SQLite journal with exit-plan review, risk gate + cooling-off, flow, volume profile, vol stack, news (+optional Claude summary), charts, HTML/MD/JSON/CSV outputs.
- v2.1.0 built (2026-10-02): strategy engine (`recommend`, `explain`): signal book, YAML signal sheet, 9 strategy plugins, confidence, rationale, selector. Also: `alerts` (2×-credit stop, VIX term structure, VVIX, IV-rank crossings, earnings; console/log/webhook/telegram/email sinks), synthetic `backtest` + optional optopsy adapter, IBKR read-only positions, provider registry with cache and rate limits (`capabilities`), structured logging with redaction. HTML pages for `recommend`/`explain`/`alerts`/`backtest` (`reports/html.py`, `templates/`; `--open`).
- v2.2.0 built (2026-10-02): tech universe (12 themes, outside bar ≥ 85 / 2 a day, theme caps and the outside bar on the default run only, correlated-risk warning, `recommend --theme`); Phase 0 fixes F1–F4; `bull_call`/`bear_put` (11 plugins); `gexscan web` local site (Home → Theme → Ticker → Options scan → Scan & visualize; Strategy Builder with 22 templates; About + live pricing self-check); BSM-with-q pricing in Python and `engine.js` with golden parity; seeded Monte Carlo exit-plan simulation. Moon-silver matte UI, green/red glossy heatmap with a colour-blind (blue) toggle. 218 tests. Next (P6, needs approval): positions & alerts page, American approximation, dark mode.
- Not built yet: Bigdata.com, Tradier/MarketData/Tiingo/Marketaux/SEC EDGAR providers (catalogued only), real-chain backtests without optopsy, Parquet export.
- Defaults chosen without user input (see README 'Exit rules' and 'Decisions taken'); user may revise.
