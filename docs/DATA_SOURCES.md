# Data sources (probed 2026-09-29)

How the probes were run: `scripts/probe_providers.py` (read-only GETs, keys redacted, ≤ 3 FlashAlpha calls), plus reading each provider's public docs.
**Keyed probes are still pending:** no `.env` and no provider environment variables were found on this machine (see §7).

Legend: ✅ verified by a live call · 📄 from docs only · ⏳ pending a key · ❌ not available on this tier.

## 1. Capability matrix (what the data layer can rely on)

| Capability | Primary | Fallback | Notes |
|---|---|---|---|
| Full option chain (bid/ask, IV, greeks, OI, volume) | **CBOE delayed** ✅ | IBKR (per contract, slow) | 15-min delayed; OI is prior-day |
| Own GEX / DEX / walls / flip / max pain | computed from CBOE ✅ | — | |
| GEX cross-check | FlashAlpha `/v1/exposure/levels` 📄⏳ | — | 5 calls/day on Free |
| Daily OHLCV (long history) | **CBOE historical** ✅ (back to 2012) | yfinance, FMP EOD ⏳ | |
| Intraday bars (volume profile, VWAP) | FMP `historical-chart/{1min…4hour}` 📄⏳ | IBKR history ⏳ | Alpha Vantage intraday is premium ❌ |
| VIX level + VIX chain | CBOE `_VIX` ✅ | | |
| SPX/SPY/QQQ/SMH for relative strength | CBOE ✅ | | |
| Earnings dates | FMP `earnings-calendar` 📄⏳ | yfinance, Alpha Vantage `EARNINGS_CALENDAR` 📄⏳ | |
| Macro calendar (CPI/FOMC/NFP) | FMP `economic-calendar` 📄⏳ | hard-coded list | |
| Risk-free rate | Alpha Vantage `TREASURY_YIELD` 📄⏳ | constant in config | |
| IV rank / percentile | **own daily snapshots** (builds over time) | IBKR IV-percentile fields ⏳ | Needs about 1 year of snapshots for our own history; FlashAlpha historical is Alpha tier ❌ |
| Positions (read-only) | IBKR Client Portal ⏳ | manual CSV | never any order endpoint |
| Options flow / sweeps / unusual activity | **none available yet** | Unusual Whales (stub) | see §5 |
| News / sentiment enrichment | Bigdata.com (optional) ⏳ | — | not on the critical path |

## 2. CBOE delayed quotes (keyless) ✅

- **Chain:** `https://cdn-api.cboe.com/api/global/delayed_quotes/options/{SYM}.json`
  - `cdn.cboe.com` now answers **307** to `cdn-api.cboe.com`, so follow redirects and update `config.yaml`.
  - META returned 7,536 contracts, 3.3 MB. Payload `data`: `price, iv30, volume, …, options[]`.
  - Per-contract fields: `option` (OCC symbol), `bid, bid_size, ask, ask_size, last_trade_price, last_trade_time, open, high, low, prev_day_close, change, percent_change, volume, open_interest, iv, delta, gamma, theta, vega, rho, theo, tick`.
  - `timestamp` is **UTC** (e.g. `2026-09-30 03:43:56`). Treat it as UTC and display it in ET.
  - Data quality (META, cached chain): **14 % of contracts have IV = 0** and **34 % have OI = 0**, mostly far OTM/illiquid. Contracts with IV = 0 must have their IV re-solved from mid, or be excluded from greeks-based aggregates.
  - `_SPX` ≈ 13 MB and `_VIX` ≈ 0.7 MB both work. Index roots take a leading underscore.
- **Quote:** `.../quotes/{SYM}.json` ✅ (514 B).
- **Daily history:** `.../charts/historical/{SYM}.json` ✅ OHLCV daily back to 2012. This is enough for RV, GARCH and 52-week structure without yfinance.
- **Freshness:** delayed ~15 min intraday. **OI is as of the prior close** (OCC publishes overnight), so every GEX figure must show "OI as of T-1".
- **Terms:** CBOE's delayed data is for personal, non-display use. **Do not commit raw chains or derived per-contract tables to the public repo.** Aggregated levels in personal reports are OK locally; git-ignore `reports/`.
- **Rate:** no published limit. Use a polite 1 request/second, local caching, and one chain per symbol per run.

## 3. FlashAlpha 📄 (key pending ⏳)

- Base `https://lab.flashalpha.com`, header `X-Api-Key`.
- **Free tier: 5 requests/day, single US stocks only (no ETFs/indexes), 15-minute cache.**
- Other tiers: Basic $79 (250/day), Growth $299 (2,500/day), Alpha $1,499 (unlimited).

| Endpoint | Tier | Use in gexscan |
|---|---|---|
| `/v1/exposure/levels/{sym}` | Free | Cross-check flip / walls / max ±γ. The spec's reference values come from this endpoint. |
| `/v1/exposure/gex/{sym}?expiration=` | Free (one expiry per call) | Spot-check one expiry |
| `/v1/options/{ticker}`, `/stockquote/{ticker}`, `/v1/stock/{sym}/summary` | Free | not needed (CBOE covers them) |
| `/v1/pricing/greeks`, `/v1/pricing/iv` | Free | not needed (local BS) |
| `/v1/surface/{sym}`, `/health` | public | optional IV-surface sanity check |
| dex / vex / chex / maxpain | Basic | computed locally instead |
| exposure summary, narrative, zero-dte, `/v1/volatility`, flow levels/summary | Growth | ❌ |
| vrp, advanced volatility, raw flow, historical | Alpha | ❌ |

**Design consequence:** FlashAlpha is a **validator, not a source**. Budget: at most 3 symbols/day by default (configurable via `flashalpha_symbols`), cache for 24 h, never inside loops. A mismatch beyond tolerance gets flagged in the report instead of breaking the run.
The FlashAlpha flip is labelled `sensitive_root` on META. Our engine will report **all** zero-crossings plus the sticky-strike vs sticky-moneyness sensitivity, so the two can be compared honestly.

## 4. Alpha Vantage 📄 (key pending ⏳)

- From the docs, these are **premium-only:** `TIME_SERIES_INTRADAY`, `TIME_SERIES_DAILY&outputsize=full`, `VWAP`.
- These are free: `TIME_SERIES_DAILY` compact (last 100 bars), `GLOBAL_QUOTE`, `TREASURY_YIELD`, `EARNINGS_CALENDAR` (CSV), fundamentals and news sentiment.
- `HISTORICAL_OPTIONS` / `REALTIME_OPTIONS`: whether they're included on the free tier wasn't legible in the docs. The probe tests both. If `HISTORICAL_OPTIONS` works on free, it's the fastest way to **backfill IV history** for IV rank/percentile instead of waiting a year for our own snapshots.
- The free-tier rate isn't stated on the page. Historically it has been 25/day, so treat it as scarce: cache for 24 h.
- Role: risk-free rate, earnings-calendar fallback, and possibly an IV-history backfill.

## 5. Financial Modeling Prep 📄 (key pending ⏳)

- Docs list 418 `/stable/` paths. **None cover options, options flow, dark pool or unusual activity.**
- Useful endpoints:
  - `historical-chart/{1min,5min,15min,30min,1hour,4hour}` and `historical-price-eod/full`
  - `batch-quote`, `aftermarket-trade`, `aftermarket-quote`
  - `earnings-calendar`, `economic-calendar`
  - insider trading
- The "live order flow" in your plan is most likely FMP's **WebSocket stream of real-time stock trades/quotes** (the stock tape). It is **not** options flow. FMP's WebSocket also requires a one-time *User Declaration Form*.
- Role: **intraday bars for volume profile / VWAP / anchored VWAP**, the earnings and macro calendars, and possibly stock-tape block detection later.
- ⚠️ **Volume-source rule:** FMP (consolidated) volume and IBKR volume (~60–70 % of consolidated) must never be mixed in one ratio. Every volume series carries a `source` tag, and derived ratios assert that both sides share it.

## 6. IBKR (read-only) ⏳

- The probe hit `https://localhost:5000/v1/api/iserver/auth/status` and got **connection refused**: no Client Portal Gateway is running.
- Plan: an adapter against the **Client Portal Web API**, using only these read-only paths:
  - `/portfolio/accounts`, `/portfolio/{acct}/positions`
  - `/iserver/marketdata/snapshot`, `/iserver/marketdata/history`, `/iserver/secdef/*`
- An **allow-list in code** rejects any path containing `order`, `orders`, `reply` or `whatif`, and a test enforces it (hard rule 1).
- The alternative is TWS/Gateway via `ib_async`, restricted to data calls only. That's your choice (§11 Q3).
- The IBKR MCP connector in this Claude session is **not** used by the app. It exposes order-instruction tools, and I won't call them.
- Market-data subscriptions determine whether snapshots are real-time or delayed. The adapter records the `mktDataAvailability` flag as freshness.

## 7. Bigdata.com / Unusual Whales

- **Bigdata.com** (optional, key pending): news, events and sentiment enrichment for the `report TICKER` narrative. It's never a scoring input in v2.
- **Unusual Whales:** interface stub only (`FlowProvider` protocol), no calls. Options-flow scoring stays at weight 0 until a real flow source exists. Until then, the "flow" component is proxied by **day-over-day OI change and volume/OI from our own CBOE snapshots**, labelled as a proxy.

## 8. Keyed probes: to run once `.env` exists

```bash
cp .env.example .env   # fill in keys
python scripts/probe_providers.py --only flashalpha,alphavantage,fmp,ibkr
```

The results are written to `docs/probe_results.json`, which is git-ignored (it records entitlement shapes, not values). This file then gets updated with the actual tier and entitlement results.
