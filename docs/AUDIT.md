# gexscan v0.1 — Audit (2026-09-29)

Scope: everything under `gex-swing-scanner/` as found on 2026-09-29. ~1,400 LOC Python, 6 passing tests.
The directory is **not yet a git repository**.

## 1. What exists

| Module | LOC | What it does |
|---|---|---|
| `cli.py` | 41 | `argparse` entry point `gexscan [--symbols --fixtures --today --out --state -v]`. One command: full scan. |
| `config.py` | 14 | Loads `config.yaml` (watchlist + all thresholds). No validation. |
| `pipeline.py` | 231 | Orchestration: per-symbol chain → levels → technicals → earnings → score → trade; reviews prior picks; writes HTML/JSON/Markdown reports. Holds a `DataSource` class that switches between live and fixture data. |
| `data/cboe.py` | 121 | **CBOE free delayed chain** (`cdn.cboe.com/.../options/{SYM}.json`): every listed contract with bid/ask, IV, delta, gamma, OI, volume. OCC symbol parser. Raw JSON cache + fallback to cache on fetch failure. |
| `data/prices.py` | 50 | yfinance daily OHLCV (90 days) and next earnings date. |
| `data/flashalpha.py` | 38 | Optional `GET /v1/exposure/levels/{sym}` cross-check (reads `FLASHALPHA_API_KEY`). |
| `analytics/bs.py` | 50 | Vectorised Black-Scholes gamma/delta/price, r = q = 0. |
| `analytics/gex.py` | 164 | Own GEX engine: `gamma × OI × 100 × S² × 0.01`, calls +, puts −. Call wall = max call GEX strike, put wall = min put GEX strike, flip = zero-crossing of total GEX re-priced over a ±30 % spot grid (**returns only the crossing nearest spot**), max pain (nearest expiry), straddle expected move, ~30 DTE ATM IV, P/C OI, ATM spread %. |
| `analytics/technicals.py` | 48 | SMA10/20, 5/10-day change, RV20 (close-to-close), 20-day high/low, last-day %, volume vs 20-day avg. |
| `scoring.py` | 140 | Classifies `bull_put / bear_call / iron_condor / call_debit / skip`; 0–100 score from regime, location, vol edge (IV/RV20), trend, liquidity, reserved flow bonus. |
| `trades.py` | 213 | Builds concrete spreads from real quotes: expiry in 10–30 DTE avoiding earnings (+2-day buffer), short-delta band 0.12–0.30, width ≈ 4 % of spot, credit ≥ 20 % of width, bid/ask filter, sizing from `account_size × 2 %`. Exit text: TP, stop, time stop. |
| `review.py` | 107 | Pick log (`state/picks.csv`) + next-day review label (intact / watch / broken). |
| `templates/report.html` | — | Jinja2 light/dark report. |
| `tests/` | 181 | Synthetic CBOE-format fixtures (UPCO/DNCO/PINCO/EARN) + 6 tests: OCC parsing, walls/regime, max pain, earnings veto, bull-put rules, end-to-end + next-day review. |
| `.github/workflows/` | — | `tests.yml` (pytest on push). `daily-scan.yml` (cron 18:30 UTC, commits reports + picks, deploys GitHub Pages). |

**Validated on the reference case (META, cached CBOE chain 2026-09-29 13:37):**

| Item | gexscan v0.1 | Reference (§9 of spec) |
|---|---|---|
| Oct-16 call OI 700 / 825 / 800 / 750 / 900 | 21,112 / 20,768 / 16,869 / 15,864 / 15,131 | ~21.1k / 20.8k / 16.9k / 15.9k / 15.1k ✅ |
| Oct-16 put OI 700 / 600 / 550 | 6,941 / 6,901 / 5,763 | ~6.9k / 550 / 600 ✅ |
| Call wall / put wall (≤60 DTE) | 750 / 700 | 750 / 700 ✅ |
| Regime | positive (net ≈ $0.61B per 1 %) | positive ✅ |
| Gamma flip | 635.1 (≤60 DTE), 549.1 (≤400 DTE) | 676.9 `sensitive_root` ⚠️ |
| Highest-OI strike | 800 (≤60 DTE, all expiries summed) | 750 ⚠️ definition differs |
| ATM IV (~30 DTE) | 45.2 % | 43.0 % (IBKR) ≈ |

The flip mismatch is expected: the flip depends on the IV assumption for each re-priced spot (sticky strike vs. sticky moneyness), the DTE filter, and which root you report. This is exactly why v2 must return **all** roots.

## 2. Strengths worth keeping

- **CBOE chain is the key asset.** It is free and keyless and gives a full-chain OI/IV/greeks snapshot. That removes the need for FlashAlpha Growth to compute full-chain GEX ourselves.
- Clean data → analytics → scoring → trades separation; dataclasses everywhere.
- Fixture-driven offline mode (`--fixtures`, `--today`) already makes runs reproducible and gives a pattern for recorded-API tests.
- Trades come from **real bid/ask**, with mid and natural credit and liquidity filters.
- Earnings veto, pick log and next-day review already exist.
- **No order-placement code anywhere.** ✅ (Hard rule 1 already holds.)

## 3. Gaps vs. the v2 spec

### Correctness / rule violations (fix first)
1. **Stop rule differs from the spec.** `trades.py` sets `stop = spread value >= 2 × credit`, i.e. **loss = 1 × credit**. Spec: "exit when loss reaches 2× credit" = spread value 3× credit. **Needs your decision:** see `strategy_rules.md` §C1, including the math showing that a 2×-loss stop never fires before max loss on a spread that collected ≥ 1/3 of its width.
2. **Rolling is suggested.** `review.py` action text says *"close, roll, or hedge"*, which violates "no rolling".
3. **Covered calls / CSPs / wheel don't exist.** There's no runaway-risk veto on short calls.
4. **Iron-condor stop is vague** ("close the tested side if price closes beyond a wall"), with no credit multiple.
5. **Debit-spread stop is "spread loses 50 %"**, not the spec's "max loss = debit + time stop + invalidation price".
6. **Gamma flip hides ambiguity.** It returns the root nearest spot and silently drops the others.
7. **CBOE moved hosts.** `cdn.cboe.com` now 307-redirects to `cdn-api.cboe.com`. `requests` follows it today, but the config URL should be updated.
8. CBOE `timestamp` is **UTC**. It's stored as a naive string and isn't shown as freshness.
9. `bs.py` assumes r = q = 0. That's fine for gamma ranking, but it biases deltas and prices on long-dated LEAPs. Pass r (T-bill) and q.
10. `_ncdf` uses `np.vectorize(erf)`, which is slow. Swap to `scipy.special.ndtr`.

### Missing capability (spec §3–§7)
- No provider abstraction, capability detection, retry/backoff or rate limiting. The cache is "last raw JSON per symbol", not a history store.
- **No snapshot history.** Nothing accumulates chains, GEX or IV per day, so IV rank/percentile and day-over-day OI change are impossible.
- No expiry buckets, OPEX flags, DEX/vanna/charm, or ATR-unit distances.
- Volatility: only RV20 and 30-day ATM IV. Missing: constant-maturity IV, skew, term structure, IVR/IVP, Parkinson/GK, GARCH family, IV − GARCH edge, probability of touch/ITM, Monte Carlo.
- **No volume profile, VWAP or volume indicators.** yfinance gives 90 daily bars only, with no intraday.
- No flow ingestion (the `flow_bonus` weight is a placeholder).
- No structure/breakout module (52w, squeeze, RS vs SPY/QQQ/SMH).
- No portfolio/concentration checks, cooling-off, journal, weekly review or alerts.
- There's no Typer/Rich CLI, and no `report TICKER`, `monitor`, `journal`, `snapshot` or `leaps` subcommands.
- No charts; no Parquet/CSV export for Tableau.
- Tooling: setuptools + pip; no ruff/mypy; `requires-python >=3.10`, while the spec asks for 3.11+. The local `.venv` is Python 3.14.

### Repo hygiene before going public
- `state/picks.csv`, `reports/*.html|json` and `state/cache/*.json` hold **personal picks and data snapshots**. `state/cache/` is git-ignored; `reports/` and `picks.csv` are not, and the daily Action commits them. Also, redistributing CBOE data in a public repo may conflict with CBOE's terms.
- `config.yaml` embeds the personal watchlist (acceptable, but decide).
- `.env.example` is missing. `.gitignore` already has `.env`.
- No `CLAUDE.md`, no `docs/` methodology.

## 4. Recommendation

Extend in place. Keep `analytics/gex.py`, `data/cboe.py`, `trades.py` and the fixture pattern as the core. Reorganise into the spec's `providers/ analytics/ strategies/ scoring/ risk/ journal/ reports/ cli/` layout incrementally, keeping the existing tests green at every step.
