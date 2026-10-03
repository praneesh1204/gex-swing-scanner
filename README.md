# gexscan: follow-the-money options scanner and strategy engine

> ⚠️ **Not financial advice.** Educational research tool. Every number here (GEX, walls, probabilities, GARCH forecasts, the flow bias, confidence scores, backtests) is a **model estimate** built on delayed, public data. Open interest is prior-day. **The app never places, submits, modifies or cancels orders, in any broker, live or paper:** there is no order code, not even behind a flag. It recommends and alerts. You decide and execute every trade yourself. Paper trade first.

A read-only toolkit for swing traders who prefer **selling premium with defined risk**. It has three parts: the daily scan, the strategy engine and a local website with an OptionStrat-style strategy visualizer.

**The daily scan** (`gexscan`):

1. **Reviews your earlier ideas**: marks each spread on today's chain, estimates P/L, checks every exit rule (profit target, 2×-credit stop, level stop, time exit) and says **HOLD** or **CLOSE**. No rolling.
2. **Scans the watchlist**, and for each ticker:
   - **Dealer gamma (GEX)**: call wall, put wall, gamma flip, max ±γ strikes, max pain, top OI strikes. Computed locally from the CBOE chain and optionally cross-checked against FlashAlpha.
   - **Options activity**: call/put volume and premium, volume/OI, unusual contracts, and OI change versus the last snapshot. The flow bias is a proxy, because trade side is unknown.
   - **Volume profile** from 30-minute bars: POC, VAH/VAL, HVN/LVN, session and anchored VWAPs, OBV and CMF.
   - **Technicals**: SMA 20/50/200, RSI, ATR, 52-week range, squeeze/breakout, relative strength vs SPY.
   - **Volatility**: ATM IV, IV rank, RV 10/20/60, Parkinson, Garman-Klass, a GJR-GARCH-t forecast, IV − forecast, term slope and skew, expected move.
   - **News**: recent headlines (Google News RSS; Alpha Vantage/FMP if you have keys) tagged for catalysts and red flags. Optionally summarised by Claude if `ANTHROPIC_API_KEY` is set.
3. **Scores and builds trades** from real bid/ask: bull put, bear call, iron condor, call debit spread, plus a cash-secured put alternative (wheel) and covered calls on shares you list. **Each idea ships with an exit plan.**
4. **Risk guardrails**: at most 3 new ideas a day, 1 per ticker, total open risk ≤ 10 % of the account. There is an **anti-revenge cooling-off**: after a stop is hit, the next scan's ideas are watch-only.
5. Writes `reports/index.html` (with charts), `summary.md`, JSON and CSV exports, and a SQLite journal in `state/gexscan.db`.

`recommend`, `explain`, `alerts` and `backtest` also write an HTML page each (see [HTML pages](#html-pages)).

**The strategy engine** (`gexscan recommend`, `alerts`, `backtest`, v2.1):

1. Builds a **signal book** per name: VIX/VIX3M term structure, VIX level, VVIX, IV rank and percentile, VRP (close-to-close and Parkinson), the single-name IV term structure and forward factor, skew, the earnings implied move against the stock's own history, GEX regime and walls, flow, liquidity, earnings, FOMC/CPI and news.
2. **Gates eleven strategy plugins** through a YAML signal sheet. Each gate is pass, fail or unknown, with a reason.
3. Ranks survivors by a **0–100 confidence score** and attaches a **rationale three levels deep**: why the premium exists, what the market is pricing, and the second-order risk. It also gives an invalidation level and the exit plan.
4. **Alerts** (never acts) when a short-premium position's cost to close reaches 2× the credit, or on regime events: VIX term structure flipping to backwardation, a VVIX spike, IV-rank crossings and earnings windows.
5. **Backtests** the same rules on a clearly labelled **synthetic** replay: win rate, average P/L and max drawdown per strategy.

**The website** (`gexscan web`, v2.2; see [Website](#website-gexscan-web)):

1. **Home**: pick a technology theme (GPUs, memory, optics, power semis, data centre, cooling, energy, …).
2. **Theme** → **Ticker**: what the company does, how the stock has performed, catalysts, risks, outlook and the options picture.
3. **Options scan**: runs the strategy engine on that ticker and opens **Scan & visualize**. Every idea is shown with P/L $, P/L %, contract value and % of max risk across price and time, as a heatmap (shiny green = profit, shiny red = loss) and a graph. Date, range and IV sliders re-price in the browser, and a seeded Monte Carlo simulation plays out the exit plan.
4. **Strategy Builder**: any ticker and any structure (calls, puts, verticals, straddles, strangles, calendars, diagonals, iron condors and flies, butterflies, covered calls, cash-secured puts or custom legs), in the same visualizer.

## Install and run (macOS / Linux)

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/) (`brew install uv`).

```bash
git clone https://github.com/praneesh1204/gex-swing-scanner.git
cd gex-swing-scanner
uv sync --extra dev
cp .env.example .env        # optional keys, all free-tier or optional
uv run gexscan              # review earlier ideas + scan the watchlist
uv run gexscan recommend    # gated, confidence-ranked strategy ideas
uv run gexscan web --open   # the website on http://127.0.0.1:8765
open reports/index.html
```

Plain pip works too: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && gexscan`.

### Commands

| Command | What it does |
|---|---|
| `gexscan` / `gexscan scan` | Full daily run: review, scan, rank, write the report. `-s NVDA,MU` overrides the watchlist, `--no-news` skips news, `--open` opens the report |
| `gexscan report NBIS` | Deep-dive on one ticker in the terminal (nothing logged) |
| `gexscan review` | Only mark and check open ideas |
| `gexscan journal list [--status all]` | Your idea journal |
| `gexscan journal take 3 --fill 1.45` | Mark idea #3 as placed, re-basing its TP/stop on your fill |
| `gexscan journal close 3 --price 0.70 --reason tp` | Record that you closed it |
| `gexscan journal stats [--days 7]` | Weekly review: win rate, P/L by setup, stop count |
| `gexscan recommend` | Strategy engine. Filters: `-s`, `--theme optics,memory`, `--strategy csp,iron_condor`, `--min-confidence 60`, `--top 5`, `--max-per-ticker 1`, `--direction bullish,neutral`, `--vol short\|long`, `--dte-min 20`, `--dte-max 60`, `--allow-undefined`. Output: `--json` (stdout), `--explain` (every gate decision), writes `recommendations_<date>.md/.json/.html` |
| `gexscan web` | The local website (127.0.0.1 only, GET only). `--port` (default 8765), `--open`, `--no-news` |
| `gexscan explain NVDA` | Every signal, every gate and every strategy decision for one ticker, including the ones that failed. Writes `explain_<TICKER>_<date>.html` |
| `gexscan alerts` | Checks your open short premium against the 2×-credit stop, plus the regime alerts. `--positions file.yaml`, `--ibkr`, `--sink telegram` (repeatable), `--dry-run`, `--json`. Writes `alerts_<date>.html` |
| `gexscan backtest` | Synthetic backtest of the engine's rules. `--strategy`, `--years 3`, `--trades`, `--json`. Writes `backtest_<date>.md/.json/.html` |
| `gexscan capabilities` | Which data providers and alert sinks are ready, which keys are missing, and what each one adds |

`--out DIR` picks where files go (default `reports/`) and `--open` opens the HTML page in your browser; both work on `recommend`, `explain`, `alerts` and `backtest`. `-v` turns on debug logs. `recommend` and `alerts` also take `--json-logs` and `--log-file run.log`. Secrets are redacted from every log line.

Run it after the close (or late in the session): chains are about 15 minutes delayed and OI is prior-day. Every idea from a scan is logged, so the next scan reviews it. Use `journal take` for the ones you actually trade, so the stats separate paper ideas from real ones.

### Offline demo

```bash
uv run gexscan -s UPCO,DNCO,PINCO,EARN --fixtures tests/fixtures --today 2026-09-25 --state /tmp/demo-state --out /tmp/demo
uv run gexscan recommend -s UPCO,DNCO,PINCO,EARN,RICH,TERM --fixtures tests/fixtures --today 2026-09-25 --state /tmp/demo-state --out /tmp/demo --no-news
uv run gexscan explain DNCO --fixtures tests/fixtures --today 2026-09-25 --state /tmp/demo-state --out /tmp/demo --no-news
uv run gexscan alerts -s UPCO,DNCO,PINCO,EARN,RICH,TERM --positions tests/demo_positions.yaml --fixtures tests/fixtures --today 2026-09-25 --state /tmp/demo-state --out /tmp/demo
uv run gexscan backtest -s UPCO,DNCO,PINCO,EARN,RICH,TERM --fixtures tests/fixtures --today 2026-09-25 --state /tmp/demo-state --out /tmp/demo
uv run gexscan web --fixtures tests/fixtures --today 2026-09-25 --state /tmp/demo-state --no-news --open
uv run pytest -q
```

An example report generated from synthetic data is in [`docs/example-report.html`](docs/example-report.html).

### HTML pages

Each command writes one self-contained page (no scripts, no forms, no broker links; charts are embedded) next to its other output. Add `--open` to open it.

| Command | Page | What's on it |
|---|---|---|
| `gexscan` / `scan` | `index.html` | The daily scan report |
| `recommend` | `recommendations_<date>.html` | Market regime, the ranked ideas table, one card per idea (legs with bid/ask/IV/Δ/OI, max loss, breakevens, PoP, the exit plan, a payoff chart, the confidence breakdown, the three-level rationale, every gate), ideas scored but not shown, the per-ticker explain log, data freshness and a "how this works" section |
| `explain TICKER` | `explain_<TICKER>_<date>.html` | Every signal state with its value and source (proxies flagged), the ideas that passed, the strategy log and each strategy's gate decisions |
| `alerts` | `alerts_<date>.html` | Alerts fired this run, the ones held back as unchanged, open short premium against its stop, the market regime and delivery status. Secrets are redacted |
| `backtest` | `backtest_<date>.html` | Labelled SYNTHETIC: results per strategy, cumulative P/L chart, entries blocked by gates, P/L by ticker, every closed trade and the assumptions |

With `--json`, the JSON goes to stdout and the HTML path to stderr, so pipes stay clean.

## Exit rules (defaults, all in `config.yaml`)

| Trade | Profit target | Stop | Time exit |
|---|---|---|---|
| Credit spread / condor / CSP / covered call / earnings condor or fly | Buy back at 50 % of the credit | Close when the **cost to close reaches 2× the credit received**, i.e. the loss equals the credit. Verticals also stop if the underlying closes beyond the short strike. **No rolling.** | 21 DTE or halfway to expiry, whichever comes first. Earnings trades: the session after the report |
| Call debit spread (daily scan) | 75 % of max profit | Max loss = debit; invalidation on a close below the put wall | Same |
| Bull call / bear put (engine) | +50 % of the debit | Max loss = the debit. Stop when the spread is worth ≤ 50 % of the debit, or the underlying closes through support (bull call) / resistance (bear put). **No rolling.** | 21 DTE |
| Calendar / double calendar / double diagonal | +25 % of the debit | Max loss = the debit. Stop when the spread is worth ≤ 50 % of the debit, or the underlying closes outside the profit tent. **No rolling.** | 5 days before the front expiry |

The stop level is computed when the idea is made and printed with it (`Stop = 2x credit; no rolling`). `gexscan alerts` watches it for you. When a credit is ≥ ½ of the spread width, the 2× stop sits at max loss, and the idea says the level stop is the one that matters.

Entry filters for the scan: 14–45 DTE (ideal 35), short delta 0.12–0.30, credit ≥ 25 % of width, no earnings before expiry + 5 days, and severe news (lawsuit, offering, downgrade, …) costs 10 score points. Covered calls are blocked when a runaway-risk check fires (earnings, breakout, strong trend into the call wall).

## Strategy engine

```
data (chains, bars, VIX/VVIX, FRED, events, news, flow)
  -> signal book: every signal classified into a state (e.g. iv_rank = rich / sell / neutral / low / unknown)
  -> strategy plugins: build legs from real bid/ask, then gate them against the signal sheet
  -> confidence 0-100 + rationale + exit plan
  -> selector: filters, max per ticker, rank by confidence, then liquidity, then credit/width (never win rate)
```

| Strategy | Kind | Direction / vol | Required gates (all must pass) |
|---|---|---|---|
| `csp` cash-secured put | credit | bullish / short | VIX in contango, VIX not extreme, IV rank sell/rich, VRP positive, support below, liquid; no earnings before expiry |
| `covered_call` | credit | neutral-bullish / short | only on shares in `holdings`; IV rank sell/rich, resistance above the strike, liquid |
| `bull_put` / `bear_call` | credit | bullish / bearish | contango, VIX not extreme, IV rank sell/rich, VRP positive, lean not against you, support below / resistance above; credit ≥ 25 % of width |
| `iron_condor` | credit | neutral / short | contango, VIX not stressed (≤ 25), IV rank rich, VRP positive, positive gamma, range-bound |
| `bull_call` / `bear_put` | debit | bullish / bearish, long | VIX not extreme, lean bullish / bearish, IV rank not rich, no earnings in the trade, liquid; support below / resistance above preferred (it sets the invalidation level) |
| `calendar` | debit | neutral / long | **one of** single-name backwardation, a high forward factor or an earnings-pumped front; range-bound, no VVIX spike |
| `double_calendar` | debit | neutral / long | the same, neutral lean, strikes at the expected-move edges |
| `double_diagonal` | debit | neutral / long | the same, moderate IV rank; calls must be covered by later-dated longs |
| `earnings_crush` | credit (condor / fly) or debit (calendars) | neutral / short | report imminent, IV percentile elevated, implied move rich vs the stock's own history; size cut by `earnings_size_factor` |

The gates live in [`src/gexscan/config/signal_sheet.yaml`](src/gexscan/config/signal_sheet.yaml). Point `engine.signal_sheet` at your own copy, or override single values with `engine.sheet_overrides`. A gate group can be:

- `required`: every gate must pass.
- `any_of`: at least one must pass.
- `preferred`: scored, but doesn't block.
- `warn`: shown with the idea.

An unknown required input passes at half credit, with a warning, unless the gate says `unknown: fail`.

**Defined risk by default.** A short strangle is only built with `--allow-undefined` (or `engine.allow_undefined_risk: true`) and is always flagged **UNDEFINED RISK**.

**Confidence** (weights in `engine.weights`): regime 20, IV richness 25, structure 15, positioning 15, flow 10, liquidity 10, event 5. Each category blends gate credit with a graded metric. Penalties: flow against the trade −8, a binary event in the window −10, stale data −5. Buckets: Low < 40 ≤ Medium ≤ 70 < High. The default floor is 40. Free flow is a proxy and is shrunk toward neutral, so it can't move the score much.

**Rationale**: every idea answers *why does this premium exist*, *what is the market pricing* (the implied move, skew, term structure), and *what is the second-order risk* (gap, vol expansion, pin, assignment). It also states its invalidation level and the full exit plan.

### Tech universe (v2.2)

Ideas come mainly from 12 technology themes in `config.yaml` (`universe.themes`): GPUs, CPUs, chips and foundry, memory, networking, optics and photonics, power semis, data centre, cooling, energy, AI infra and hyperscalers. Each idea is tagged with its themes.

- **Outside names** (`universe.outside`) are scanned too, but they must reach **confidence ≥ 85**, at most 2 a day. They are shown in their own section, flagged **OUTSIDE UNIVERSE**.
- **Theme cap**: at most 3 ideas per theme. A **correlated-risk warning** appears when one theme holds more than 60 % of the shown ideas' max loss.
- The cap and the outside bar apply to the default run only. A ticker you pick yourself (`-s`, or a ticker page on the website) is never held back.
- `universe.mode`: `prefer` (the default), `only` (never scan outside names) or `off` (the watchlist only, as in v2.1). Quote tickers that YAML reads as booleans (`"ON"`); the loader refuses them otherwise.

## Website (`gexscan web`)

```bash
uv run gexscan web --open                     # live CBOE delayed data, http://127.0.0.1:8765
uv run gexscan web --fixtures tests/fixtures --today 2026-09-25 --state /tmp/demo-state --no-news --open   # synthetic demo
```

| Page | What's on it |
|---|---|
| Home `/` | Pick an AI theme: one card per theme with its tickers |
| Theme `/theme/<key>` | A card per ticker (price, 1-day and 1-month change, next earnings), plus an **Options scan: whole theme** button |
| Ticker `/ticker/<SYM>` | Profile, performance against SPY, the 52-week range, catalysts (earnings, dividends, FOMC/CPI, headlines), risks, outlook, levels (walls, flip, max pain) and the options picture. **Options scan** opens the next page; **Open in Strategy Builder** starts a trade by hand |
| Scan & visualize `/scan/<SYM>` | The engine's ideas for that ticker, each with its exit plan and the visualizer: a Table (price × date heatmap), a Graph (P/L against price now, on a chosen date and at expiry), Over time, Greeks and Simulation. Switch the metric between P/L $, P/L %, contract value and % of max risk |
| Strategy Builder `/builder` | Any ticker, 22 templates in 6 groups or custom legs, an expiry tab bar and a strike ladder, then the same visualizer below |
| About `/about` | The model, its assumptions and limits, data sources, licences and a live pricing self-check |

- **Pricing**: Black-Scholes-Merton with a dividend yield and an earnings IV crush, run in the browser (`engine.js`) so the sliders respond instantly. It is checked against Python with shared golden vectors, in the tests and live on every page load.
- **Simulation**: a seeded Monte Carlo in Python (`/api/simulate`). It compares following the exit plan (target, stop, time exit, invalidation) with holding to expiry, and gives the chance of profit, EV, the worst 5 % and a percentile fan over time. It is labelled as an estimate everywhere.
- **Colours**: a moon-silver matte UI. The heatmap tiles are glossy green (profit) and red (loss), and each cell prints its signed value. A **Colour-blind safe** toggle swaps green for blue.
- **Read-only by construction**: it binds 127.0.0.1 only and answers GET and HEAD only. The Host header is checked, there is a strict CSP, and there are no forms, order buttons or broker links. The website never writes the journal, positions or snapshots. Tests enforce all of this (`tests/test_web.py`).

The design is in [`docs/specs/v2.2-visualizer-spec.md`](docs/specs/v2.2-visualizer-spec.md), with diagrams in [`docs/specs/architecture.html`](docs/specs/architecture.html). How it was built and tested is in [`docs/PROCESS.md`](docs/PROCESS.md).

## Alerts (`gexscan alerts`)

Alert only: nothing here closes or adjusts a position. No rolling.

- **Stop**: every open short-premium position is marked at mid. **WARN** at 1.75× credit (`stop_warn_ratio`), **STOP** at 2×. A STOP repeats once a day until the position is gone; other alerts fire only when their state changes.
- **Regime**: VIX/VIX3M flips to backwardation (and back), a VVIX spike (level or a 5-day jump ≥ `vvix_spike_pct`), and IV rank crossing `ivr_levels` (30/50) up or down.
- **Earnings**: a report within `earnings_window_days`. It is a WARN if you hold the name.

Positions come from `positions.yaml` (copy [`positions.example.yaml`](positions.example.yaml); it is git-ignored), from journal ideas you marked with `journal take`, or optionally from the IBKR Client Portal gateway (`--ibkr`, `IBKR_CP_BASE_URL`). The IBKR client can only send GET requests to an allow-list of portfolio and market-data paths. It has no POST/PUT/DELETE method, and a test enforces this.

Sinks: `console` and `log` by default. `webhook`, `telegram` and `email` turn on when their env vars are set (see `.env.example`), and are skipped with a reason otherwise. Run it from cron or launchd after the close, e.g. `30 16 * * 1-5 cd ~/gex-swing-scanner && uv run gexscan alerts --sink telegram`.

## Backtest (`gexscan backtest`): synthetic

**Read this before trusting any number.** There are no free historical option chains. The harness replays the engine's entry gates and exit rules on daily bars, and prices options with Black-Scholes on an IV proxy (max(RV20, RV60) × 1.10, flat smile). That makes it useful to compare rules and exits, and useless for forecasting returns. Every output is labelled **SYNTHETIC**.

- Replayed: `csp`, `bull_put`, `bear_call`, `iron_condor`, `calendar`. One entry per name every 5 sessions, 1 contract, 5 % slippage each side. Exits: TP 50 %, stop at 2× credit (plus the underlying beyond the short strike for verticals), time exit. No rolling.
- Not replayed: walls/GEX and support/resistance (they need historical chains), flow, liquidity, news and macro. VRP is positive by construction. The calendar edge is not replayed because the proxy has a flat term structure, so calendar results are low fidelity.
- No lookahead: every decision uses data up to that day only (there is a test). Trades still open at the end are reported separately and kept out of the stats.
- Reports trades, win rate, average and total P/L, max drawdown, days held, exit reasons and the gates that skipped entries.

If you have real end-of-day chains, `gexscan.backtest.optopsy_adapter` runs [optopsy](https://github.com/goldspanlabs/optopsy) on them, **if you install it yourself** (AGPL-3.0; not bundled). Its exit rules are its own, not the 2× stop.

## Scoring (daily scan)

| Factor | Weight | Measures |
|---|---|---|
| Regime | 25 | Net GEX sign and distance from the gamma flip |
| Location | 20 | Distance to the relevant wall, with a bonus when the wall is confirmed by the volume profile (put wall ≤ VAL / call wall ≥ VAH) |
| Vol edge | 20 | IV vs realized vol (sellers want rich IV, buyers cheap) |
| Trend | 15 | Price vs moving averages and momentum |
| Liquidity | 10 | ATM bid/ask width and open interest |
| Flow | 10 | Options-activity bias aligned with the setup (proxy, weighted lightly) |

## Data sources and freshness

| Data | Source | Key | Freshness |
|---|---|---|---|
| Option chains (OI, IV, greeks, bid/ask, volume) | CBOE delayed quotes | none | ~15 min delay, timestamps UTC, OI prior-day |
| VIX, VIX3M, VIX9D, VVIX, SKEW | CBOE index history + delayed quote | none | daily close + delayed spot |
| Daily and 30-min bars, earnings date | yfinance (CBOE daily fallback) | none | end-of-day / intraday |
| Rates, curve, CPI, oil; CPI release dates | FRED CSV and release calendar | none (`FRED_API_KEY` optional) | daily / monthly |
| FOMC dates | `macro.fomc_dates` in `config.yaml` | none | update yearly |
| Earnings dates and timing | overrides → yfinance → Finnhub → FMP | optional | daily |
| News | Google News RSS; Alpha Vantage, FMP optional | optional | 5-day lookback |
| GEX cross-check | FlashAlpha free tier | `FLASHALPHA_API_KEY` | 5 calls/day |
| Trade-side flow | Unusual Whales (paid) | `UNUSUAL_WHALES_API_KEY` | intraday |
| Open positions | IBKR Client Portal gateway (GET only) | `IBKR_CP_BASE_URL` | live |
| Headline summaries | Claude API | `ANTHROPIC_API_KEY` | per run |

`gexscan capabilities` lists every provider in the registry, including the ones that are catalogued but not integrated yet (SEC EDGAR, Tradier, MarketData, Tiingo, Marketaux, …), with key status. HTTP responses are cached under `state/cache/http` (default TTL 1 h) and each provider has a rate limit (`providers.limits`).

**About flow:** without a paid feed, "flow" is chain volume and premium, with no trade side. It is labelled **PROXY FLOW** and carries little weight. With `UNUSUAL_WHALES_API_KEY` the engine uses at-ask / at-bid premium instead. Never mix volume sources in one ratio (IBKR volume is ~60–70 % of consolidated).

Every report section shows its provider and timestamp. Keys go in `.env` (git-ignored) and are never logged. CBOE data is for personal use; don't redistribute it.

## Privacy (the repo is public)

`reports/`, `state/` (your journal and cache), exports, `positions.yaml`, `alerts.log` and `.env` are git-ignored. The `daily-scan` GitHub workflow is manual-only and uploads the report as a 3-day artifact. Artifacts on public repos are visible to signed-in users, so the local run is the intended workflow.

## Decisions taken (defaults; change them in the config)

- **Stop = cost to close at 2× the credit** (loss = 1× credit), per the engine spec. This resolves the open question in `docs/strategy_rules.md` §C1.
- **VIX/VIX3M ≥ 1.0 = backwardation**: no new premium selling. 0.95–1.0 is labelled "flat". The cutoff is our choice; the referenced repos don't use one.
- **Expected move** = the ATM straddle of the first expiry after the event. The implied/historical ratio compares it with the stock's mean absolute earnings move over the last 8 quarters.
- **Calendars**: rejected if crossing every leg's spread would cost more than 25 % of the debit, or if a report falls between the two expiries. The front must be 21–45 DTE. After-close reports count from the next session.
- **CPI dates** come from FRED's release calendar; FOMC dates are a list in `config.yaml`. Either within 3 days counts as a binary event.
- **Ranking never uses win rate**: a high-PoP short-premium idea with a fat left tail would win that contest.

## Credits and licences

The engine borrows **ideas**, not code. Everything was reimplemented. Nothing below is vendored.

| Repo | Licence | What we took |
|---|---|---|
| [goldspanlabs/optopsy](https://github.com/goldspanlabs/optopsy) | **AGPL-3.0** | strategy-as-legs replay design; optional adapter only if you install it |
| [rgaveiga/optionlab](https://github.com/rgaveiga/optionlab) | **GPL-3.0** | PoP as the lognormal probability of the profitable payoff region (math reimplemented; not imported) |
| [kdubb-labs/forward-factor-backtest](https://github.com/kdubb-labs/forward-factor-backtest) | none | forward factor between two expiries |
| [timps0n/calendar-spread-backtest](https://github.com/timps0n/calendar-spread-backtest) | none | its negative result became the calendar guards (round-trip cost, after-close +1 day) |
| [dominickkubica/options-scanner](https://github.com/dominickkubica/options-scanner) | none | liquidity weights: spread .40 / OI .30 / volume .20 / staleness .10 |
| [alpacahq/options-wheel](https://github.com/alpacahq/options-wheel) | Apache-2.0 | CSP score (1 − \|Δ\|) × 250/(DTE + 5) × bid/strike |
| [YichengYang-Ethan/vol-regime-toolkit](https://github.com/YichengYang-Ethan/vol-regime-toolkit) | MIT | the IV percentile / Parkinson RV / VRP signal set (we rank IV against our own IV history, not an HV distribution) |
| [joncovington/MEICAgent](https://github.com/joncovington/MEICAgent) | MIT | gates instead of a ranker: stand down from premium selling at high VIX and in negative gamma |
| [Tapanrajnayak/earnings-strangle-analyzer](https://github.com/Tapanrajnayak/earnings-strangle-analyzer), [michaelpointek/ER_IV_crush](https://github.com/michaelpointek/ER_IV_crush) | none / Apache-2.0 | implied vs historical earnings move; crush setup |

OpenBB (AGPL-3.0) can be used as an optional provider (`providers.use_openbb`) if you install it; it is never bundled. Repos without a licence file are all-rights-reserved, so only their published ideas were used.

## Project layout

```
config.yaml                      watchlist, holdings, every rule and threshold
positions.example.yaml           template for the positions alerts watch (copy to positions.yaml)
src/gexscan/
  cli.py                         Typer + Rich CLI
  pipeline.py                    daily scan: data -> analytics -> score -> trade -> risk gate -> journal -> report
  config/      __init__.py (defaults + loader)   signal_sheet.yaml (the gating ruleset)
  data/        cboe prices news flashalpha | providers (registry, cache, rate limits) macro events flowsrc positions
  analytics/   gex volatility volume_profile flow technicals bs | pricing (visualizer math) simulate (Monte Carlo) overview (ticker page)
  signals/     market vol flow ruleset book  -> per-name signal states
  strategies/  base csp covered_call verticals debit_verticals iron_condor calendars earnings_crush
  engine/      recommend selector confidence rationale universe (themes, outside bar, caps)
  alerts/      rules sinks
  backtest/    harness (synthetic) optopsy_adapter (optional)
  reports/     brief (recommendation markdown/JSON) html (recommend/explain/alerts/backtest pages)
  web/         server (127.0.0.1, GET only) api (JSON) static/ (app.html silver.css ui.js charts.js engine.js viz.js builder.js app.js golden.json)
  scoring.py   trades.py   risk.py   review.py   store.py   charts.py   logutil.py
  templates/   report.html recommend.html explain.html alerts.html backtest.html (+ _base, _macros)
scripts/make_golden.py           regenerates the pricing golden vectors shared by Python and JS
tests/                           synthetic fixtures + pytest (incl. guards that fail if order code appears); js/parity.mjs
docs/                            audit, strategy rules, data-source notes, PROCESS.md (how v2.2 was built)
docs/specs/                      the v2.2 spec and architecture.html (diagrams)
```

## Development

```bash
uv sync --extra dev
uv run pytest -q                                            # 218 tests, offline (the JS parity check needs node)
uv run python tests/make_fixtures.py tests/fixtures         # regenerate the synthetic fixtures
uv run --with scipy python scripts/make_golden.py           # regenerate the pricing golden vectors
python scripts/probe_providers.py [--only cboe,fred,...]   # check which live sources answer
```

The test suite includes:
- a guard that fails if any order-placement code or broker order path appears in `src/` or in the website's JS, HTML and CSS;
- a test that the IBKR client refuses non-allow-listed paths;
- a secret-redaction test, and a test that no API response contains a key;
- the website's hardening (loopback, GET only, the Host check, CSP, safe DOM, no forms);
- golden pricing vectors with a Python/JS parity check;
- statistical checks on the simulation;
- a no-lookahead test for the backtest.

[`docs/PROCESS.md`](docs/PROCESS.md) lists every test file and what it proves.

## Known limitations

- GEX uses the public dealer convention (dealers long calls, short puts). It is an assumption, and vendors disagree.
- No true sweep or dark-pool data without a paid feed. The free flow bias comes from volume and premium, not trade side.
- IV rank needs about 20 daily snapshots of your own history. Until then it uses a labelled RV-based proxy.
- Marks use delayed mid prices, so P/L in reviews and alerts is an estimate. A real fill can be worse than the mid.
- Calendar and diagonal P/L at the front expiry holds back-month IV constant.
- The backtest is synthetic (see above).
- The visualizer prices European options (BSM with a dividend yield), so early exercise is ignored. Each leg keeps its own IV as price moves (sticky strike), and fills are at the mid.
- The simulation checks stops and targets on each weekday's close, so intraday breaches are missed. It is lognormal at the implied vol plus an earnings jump: a model, not a forecast.
- Earnings dates from free sources can be missing; the report says "unknown". Add `events.earnings_overrides` when you know better.

MIT licensed.
