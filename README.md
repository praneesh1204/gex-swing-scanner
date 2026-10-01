# gexscan: follow-the-money options scanner

> ⚠️ **Not financial advice.** Educational research tool. Every number here (GEX, walls, probabilities, GARCH forecasts, the flow bias) is a **model estimate** built on delayed, public data. Open interest is prior-day. **The app never places, modifies or cancels orders:** there is no order code, not even behind a flag. You decide and execute every trade yourself. Paper trade first.

A daily, read-only scanner for swing traders who prefer **selling premium with defined risk**. One command:

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

## Install and run (macOS / Linux)

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/) (`brew install uv`).

```bash
git clone https://github.com/praneesh1204/gex-swing-scanner.git
cd gex-swing-scanner
uv sync --extra dev
cp .env.example .env        # optional keys, all free-tier or optional
uv run gexscan              # review earlier ideas + scan the watchlist
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

Run it after the close (or late in the session): chains are about 15 minutes delayed and OI is prior-day. Every idea from a scan is logged, so the next scan reviews it. Use `journal take` for the ones you actually trade, so the stats separate paper ideas from real ones.

### Offline demo

```bash
uv run gexscan -s UPCO,DNCO,PINCO,EARN --fixtures tests/fixtures --today 2026-09-25 --out /tmp/demo --state /tmp/demo-state
uv run pytest -q
```

An example report generated from synthetic data is in [`docs/example-report.html`](docs/example-report.html).

## Exit rules (defaults, all in `config.yaml`)

| Trade | Profit target | Stop | Time exit |
|---|---|---|---|
| Credit spread / condor / CSP / covered call | Buy back at 50 % of the credit | Close when the **loss reaches 2× the credit** (cost to close = 3× credit), **or** the underlying closes beyond the short strike. **No rolling.** | 21 DTE or halfway to expiry, whichever comes first |
| Call debit spread | 75 % of max profit | Max loss = debit; invalidation on a close below the put wall | Same |

Entry filters: 14–45 DTE (ideal 35), short delta 0.12–0.30, credit ≥ 25 % of width, no earnings before expiry + 5 days, and severe news (lawsuit, offering, downgrade, …) costs 10 score points. Covered calls are blocked when a runaway-risk check fires (earnings, breakout, strong trend into the call wall).

## Scoring

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
| Daily and 30-min bars, earnings date | yfinance (CBOE daily fallback) | none | end-of-day / intraday |
| News | Google News RSS; Alpha Vantage, FMP optional | optional | 5-day lookback |
| GEX cross-check | FlashAlpha free tier | `FLASHALPHA_API_KEY` | 5 calls/day |
| Headline summaries | Claude API | `ANTHROPIC_API_KEY` | per run |

Every report section shows its provider and timestamp. Keys go in `.env` (git-ignored) and are never logged. CBOE data is for personal use; don't redistribute it.

## Privacy (the repo is public)

`reports/`, `state/` (your journal), exports and `.env` are git-ignored. The `daily-scan` GitHub workflow is manual-only and uploads the report as a 3-day artifact. Artifacts on public repos are visible to signed-in users, so the local run is the intended workflow.

## Project layout

```
config.yaml                      watchlist, holdings, every rule and threshold
src/gexscan/
  cli.py                         Typer + Rich CLI
  pipeline.py                    data -> analytics -> score -> trade -> risk gate -> journal -> report
  data/        cboe.py prices.py news.py flashalpha.py
  analytics/   gex.py volatility.py volume_profile.py flow.py technicals.py bs.py
  scoring.py   trades.py   risk.py   review.py   store.py   charts.py
  templates/report.html
tests/                           synthetic fixtures + pytest (incl. a guard that fails if order code appears)
docs/                            audit, strategy rules, data-source notes
```

## Known limitations

- GEX uses the public dealer convention (dealers long calls, short puts). It is an assumption, and vendors disagree.
- No true sweep or dark-pool data. The flow bias comes from volume and premium, not trade side.
- IV rank needs about 20 daily snapshots of your own history. Until then it uses a labelled RV-based proxy.
- Marks use delayed mid prices, so P/L in reviews is an estimate.
- Earnings dates from yfinance can be missing; the report says "unknown".

MIT licensed.
