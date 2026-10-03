# How v2.2 was built: the process

This page explains how the v2.2 release was made: the tech universe, the local website, the strategy visualizer and the Strategy Builder. It covers how the requirements were gathered, how the spec came first, the trade-offs, how it was tested and reviewed, and what is still open.

The **what** is in [specs/v2.2-visualizer-spec.md](specs/v2.2-visualizer-spec.md). The **diagrams** are in [specs/architecture.html](specs/architecture.html).

> gexscan only recommends and alerts. It never places, submits, modifies or cancels an order, in any broker, live or paper. All numbers are model estimates from delayed data. Not financial advice. Every example on this page uses synthetic data (the DEMO ticker or the test fixtures UPCO, DNCO, PINCO, EARN, RICH and TERM).

---

## 1. The steps at a glance

| # | Step | Output |
|---|---|---|
| 1 | Gather requirements and restate the hard rules | §2 of the spec, CLAUDE.md |
| 2 | Write the spec and the visual architecture **before any code** | `v2.2-visualizer-spec.md`, `architecture.html` |
| 3 | Revise the spec with your feedback (green/red heatmap, moon-silver UI, website flow, builder) | Spec Rev 2 |
| 4 | Pick and validate the colours with a script, not by eye | §6.9 of the spec |
| 5 | Build in phases P0 → P5, with tests in every phase | the code and 218 tests |
| 6 | Review security and privacy against the web rules W1–W8 | `tests/test_web.py` |
| 7 | Test in a real browser on the fixtures, then fix what it found | §7 below |
| 8 | Update the spec to match what was built | Spec Rev 3 (*as built* notes) |

---

## 2. Requirements

The input came in four rounds:

1. **The hard rules:**
   - no order code, ever;
   - an exit plan on every idea;
   - no secrets in the repo;
   - show freshness, label estimates and keep the disclaimer;
   - never mix volume sources, no lookahead;
   - defined risk by default;
   - no AGPL/GPL code.
2. **The strategy engine prompt.** This produced v2.1: signals, gates, confidence, rationale, alerts and a synthetic backtest.
3. **The tech universe and the visualizer.** Technology themes (AI, semis, memory, optics, power, cooling, data centres, energy and more). An OptionStrat-style view of P/L $, P/L %, contract value and % of max risk over time, with a simulation, as a website. Write the specs and the diagrams first.
4. **The look and the flow:**
   - a green profit / red loss heatmap with a shiny finish, on a moon-silver matte UI;
   - Home (pick a theme) → Theme → Ticker (overview, performance, catalysts, risks, outlook) → **Options scan** → the visualized ideas;
   - a separate Strategy Builder tab for any ticker and any structure.

Your own design doc was read alongside these. Its requirements were kept: the scanner controls, the expiration tabs and strike ladder, the template list, Table/Graph × 4 metrics, the sliders with a < 50 ms target, BSM with dividends, and golden vectors. Its stack (React/FastAPI, `POST /scan`) was not kept; §3 explains why.

**What stays private.** Screenshots of a real position were used only to understand the look. No real position, pick or account figure appears anywhere in the repo, and every example uses DEMO or the fixtures. The design doc itself stays out of the repo.

---

## 3. Spec first, then code

The spec and `architecture.html` were written and revised before implementation began. They hold:
- the goals and non-goals;
- the rules carried forward, plus eight web rules (W1–W8), each tied to a test;
- the pages, the API, the pricing model, the simulation and the visual design;
- the modules, the test plan, the phases and the risks.

Questions that would normally block the work were answered with stated defaults instead (spec §10), so that each one can be changed later:
- the universe list;
- where HOOD, BMNR and TQQQ go (outside the universe, behind the higher bar);
- the outside bar (≥ 85 confidence, at most 2 a day);
- a local server as the delivery;
- adding debit plugins;
- applying the Phase 0 fixes.

### Trade-offs

| Choice | Alternative | Why this one |
|---|---|---|
| A local server, `gexscan web`, on 127.0.0.1 | A static site | The flow needs live data on demand (a scan when you click), which a static site cannot do |
| The Python standard-library HTTP server | FastAPI | No new dependency to vet or license. GET-only is easy to prove in a test |
| Vanilla ES modules, SVG and CSS. No framework, no build step, no npm | React/TypeScript | No supply chain, no licence risk, works offline, and the whole front end can be checked by grep for order paths or unsafe DOM calls |
| GET only, plus `/api/scan` instead of `POST /scan` | POST endpoints | There are no side effects, so nothing needs a POST. A server that refuses every other method is read-only by construction |
| Pricing in the browser (`engine.js`), with Python as the source of truth | A pricing endpoint | The sliders must re-price within 50 ms. Shared golden vectors keep the two in step (§5) |
| The simulation in Python (numpy, seeded) | In the browser | It is path-dependent, it has statistical tests, and it is reproducible from a seed. The browser only draws the results |

---

## 4. The look and the dataviz method

The charts follow a fixed procedure: pick the form, assign colour by its job, **validate the palette with a script**, apply the mark specs, add the hover layer, run an accessibility pass, then look at the rendered result.

**Form.** Each view gets the chart that suits its job:
- a heatmap table for price × date;
- a line chart with one axis for P/L against price (now, a chosen date, expiry);
- a separate strip for the probability density;
- small multiples for the Greeks;
- a fan of percentiles for the simulation.

No chart has two y-axes.

**Colour.** The heatmap is diverging: red for loss, a silver neutral at zero, and green for profit.
- Each arm is one hue, light to dark, interpolated in OKLab, and scaled to its own extreme (max loss or max profit).
- The shine comes from a gloss gradient and a 1 px highlight on each tile. It is not in the data colour.

**Validation** (`validate_palette.js`):

| Check | Result |
|---|---|
| Each arm is monotone in lightness (adjacent ΔL ≥ 0.06), a single hue, and ≥ 5:1 against the graphite tray at the light end | PASS |
| The red and green arms are matched in lightness | within 0.003 L at every step |
| Series 1–3 (Now / selected date / Expiry) on the card surface | PASS on every check |
| **Red against green for colour-blind readers** | **FAIL** (deutan ΔE 1.8) |

That failure is expected for red/green, which you asked for. Three things make the heatmap readable without colour:
1. Every cell prints its signed value (+12 % / −34 %).
2. Loss cells carry a faint diagonal hatch.
3. A **Colour-blind safe** toggle swaps the profit arm to blue (red against blue: deutan ΔE 22.4, PASS).

**Other rules:**
- text uses ink colours, never series colours;
- status colours are used only for warnings, stops and the self-check, always with an icon and a label;
- every chart has a hover tooltip, and keyboard focus shows the same tooltip;
- hit targets are at least 24 px.

---

## 5. Build phases

| Phase | What | Notes |
|---|---|---|
| **P0** Fixes | F1 iron-fly stop at the breakevens. F2 loss text capped at max loss. F3 round-trip cost gate for credit trades. F4 `risk.enforce_budget` | Small bugs found while writing the spec. Fixed first, so the visualizer would not draw wrong stops |
| **P1** Universe | 12 themes in `config.yaml`, `engine/universe.py`, `recommend --theme`, the outside bar and its own section, theme caps, the correlated-risk warning | Caps and the outside bar apply only to the default run. A ticker you pick yourself is never held back |
| **P2** Pricing core | Dividend yield in `bs.py`. `analytics/pricing.py` (value grids, metrics, Greeks, breakevens, max loss and profit, lognormal chance of profit). `golden.json`. `engine.js` | `reprice()` and `probability()` are kept separate, as your doc asked |
| **P3** Web app | `web/server.py`, `web/api.py`, and the pages: Home, Theme, Ticker, Scan & visualize, Builder, About | 22 builder templates in 6 groups, plus custom legs |
| **P4** Simulation | `analytics/simulate.py`: seeded Monte Carlo with an earnings jump that keeps the total variance. It plays out the exit plan (target, stop, time exit, invalidation) against holding to expiry | 30 M path-step budget. It says when it cuts the number of paths |
| **P5** Debit plugins | `bull_call`, `bear_put`. The engine now has 11 strategy plugins | Debit plan: max loss = the debit, a time stop and an invalidation level |

Each phase ended with its tests passing before the next one started.

---

## 6. Testing

**Result: 218 tests pass, offline, in about 14 seconds** (`uv run pytest -q`).

| File | Tests | What it proves |
|---|---|---|
| `test_pricing.py` | 56 | 40 BSM reference values. Put-call parity with dividends. The earnings-event volatility. Three golden positions (a DEMO bull call, an iron condor with a dividend yield, an earnings calendar with an IV crush). Greeks. Breakevens. Unbounded legs. Expiry equals intrinsic value. The leg codec. Chance of profit equals N(d2) for one call. **JS parity:** `node tests/js/parity.mjs` runs `engine.js` against the same golden file (1e-6 tolerance in JS, 1e-9 in Python) |
| `test_simulate.py` | 10 | The discounted mean payoff matches BSM. P(above K) matches N(d2). An event keeps the total variance and adds the jump. Steps fall on weekdays. A credit stop loses at least 1× the credit and never more than the max loss. Stop accounting checked by hand. The path budget. Bad inputs |
| `test_web.py` | 70 | Server hardening, the API on fixtures, the front-end source rules (§7) |
| `test_universe.py` | 14 | Theme tags. Theme caps on the default run only. Explicit picks are not held back. The outside bar, its section and its daily maximum. Modes `prefer` / `only` / `off`. Unquoted YAML `ON` is refused. The correlated-risk warning. The four Phase 0 fixes |
| `test_engine.py` | 19 | Market classification, gates, strike picking, the selector, undefined risk being opt-in and flagged, the IBKR GET-only allow-list, secret redaction (v2.1) |
| `test_alerts.py` | 16 | Stop states and the 2× text, de-duplication, VIX term-structure flips, VVIX spikes, IV-rank crossings, earnings windows, sinks skipped without keys (v2.1) |
| `test_v2.py` | 15 | The 2×-credit exit plan, the review rules, risk limits and cooling-off, flow, the volume profile, the no-order grep over `src/` (v2.0) |
| `test_scanner.py`, `test_backtest.py`, `test_html.py` | 6 each | The daily scan, the synthetic backtest (with no lookahead), the HTML pages |

The golden values come from `scripts/make_golden.py`, an **independent** implementation (scipy's normal distribution and root finder) that does not import the app's pricing code. So the test compares two separate implementations, rather than the code against a copy of its own output.

The site also runs the parity check live in your browser on every load: it re-prices every golden value, and the About page shows the result ("passed (264 values)"). If `engine.js` ever drifts from Python, a banner on every page says the numbers may be wrong.

---

## 7. Security and privacy review

The website runs on your machine, and any other site you visit could try to reach it. The review asked one question: what could a hostile page, a bad link or a mistake do? Each rule below has a test.

| Threat | Defence | Test |
|---|---|---|
| Another site or a DNS-rebinding page calls the local server | Binds 127.0.0.1 only; there is no option to bind anything else. Host must be `127.0.0.1:<port>` or `localhost:<port>` (421 otherwise). No CORS headers. CSP `frame-ancestors 'none'` | `test_host_allow_list`, `test_binds_loopback_only`, `test_security_headers_everywhere` |
| A request that changes something | GET and HEAD only; every other method gets 405. No endpoint writes anything. A scan runs with `save_snapshots=False` and the journal stays untouched | `test_only_get_and_head`, `test_scan_has_exit_plans_and_never_journals` |
| Script injection | A strict CSP with no inline script or style. The DOM is built with `createElement` and `textContent` only (`innerHTML`, `eval` and similar are banned). No `<form>`. Only `ui.js` calls `fetch`, and only with GET | `test_front_end_builds_dom_safely`, `test_only_ui_js_fetches` |
| Reading files off disk | Static files come from a fixed list built at start-up, with strict names. Traversal, unknown paths and the page shell as a static file all return 404 | `test_unknown_paths_404` |
| Leaking API keys | Responses are built field by field and never from the environment. The test sets every key in `.env.example` to a sentinel and checks every response for it. Errors are short and carry no stack trace | `test_no_secret_in_any_response`, `test_bad_requests_are_400` |
| Order code creeping in | The no-order grep runs over the Python **and** over the static JS, HTML and CSS | `test_v2.py::test_no_order_placement_code`, `test_front_end_has_no_order_code` |
| "Rolling" advice | "No rolling" is the only form of the word allowed in the front end | `test_front_end_says_no_rolling_only` |
| Oversized requests | URLs over 8 KB get 414. At most 4 API requests are worked on at once | `test_url_too_long` |

---

## 8. Browser QA and review: what they found

Every page was opened in a real browser on the fixtures:
- every viz tab, the sliders, the tooltips and keyboard focus;
- the builder templates, including the calendars, diagonals, condor, collar, broken-wing butterfly and the undefined-risk badge;
- the scan → builder round trip;
- the theme scan and its correlated-risk warning;
- the About self-check.

Items 1–8 were found in the browser. Items 9–12 were found in the code and spec review at the end. All are fixed.

| # | Found | Fix |
|---|---|---|
| 1 | The builder's strike ladder cut off far legs | The ladder range always includes every leg's strike |
| 2 | Wall labels overlapped on the price chart, the Graph view and the ladder | Labels are staggered when they would collide |
| 3 | Greek axis ticks repeated (0.1, 0.1), or 2.5 was rounded to $3 | Tick decimals are chosen from the tick step |
| 4 | The 1:2 broken-wing butterfly template was far too wide (a $2.2k debit), because its wings were clamped to the end of the chain | The near wing is half the expected move |
| 5 | Any simulation with an earnings event returned a 500 error (an unhashable cache key) | The cache key uses plain values. Regression test: `test_simulate_with_event` |
| 6 | The About self-check stayed on "running" | It now waits on the check itself; it shows "passed (264 values)" |
| 7 | A scan card said $605 while the visualizer said $606 | Both now use the exact mid ($605.50) |
| 8 | Closed browser connections printed tracebacks in the server log | The server ignores a client hanging up |
| 9 | The page shell could also be fetched as `/static/app.html` | It is served only on the page routes. Regression test in `test_unknown_paths_404` |
| 10 | The spec said responses passed through the log redactor; they don't (they never contain keys to begin with) | Spec W5 corrected. The sentinel test proves the real guarantee |
| 11 | The universe and the Phase 0 fixes were built but had no tests | `tests/test_universe.py` (14 tests) |
| 12 | An unquoted `ON` (the ticker) in YAML becomes the boolean `true` | The loader refuses booleans in ticker lists and says "quote it" |

---

## 9. The spec as built

At the end, the spec was revised (Rev 3) wherever the build refined it. These points are marked *(as built)*:
- the web rules W5, W7 and W8;
- §4.3 rule 7 (caps and the outside bar apply to the default run only);
- the 22 builder templates;
- the derived inputs (event move, dividend yield, exact mids);
- the pricing grid and breakevens;
- the simulation's step schedule and its stop test;
- the module list, the test plan and the phase status.

`architecture.html` was updated to the final module names.

---

## 10. Known limitations

- **European pricing.** BSM with a dividend yield; early exercise is ignored. Deep in-the-money American puts are slightly undervalued.
- **Sticky strike.** Each leg keeps its own IV as price moves. The IV slider shifts all legs together. Back-month IV is held constant through a front expiry, apart from the modelled earnings crush.
- **Fills at the mid.** Every value is at the mid. The round-trip cost is shown next to it, and real fills can be worse.
- **Daily-close stops in the simulation.** The stop and target are checked on each weekday's close, so intraday breaches are missed.
- **The simulation is a model, not a forecast.** It assumes lognormal moves at the implied volatility, plus a jump for earnings. Every simulated number is labelled as an estimate, and the CVaR is shown next to the EV.
- **Delayed data.** CBOE quotes are about 15 minutes old and OI is prior-day. Every page shows its source and age.

## 11. Still open (each needs your approval)

- **P6:** a read-only positions and alerts page (marks and the 2× stop monitor), position history, an American-exercise approximation, accuracy tracking, dark mode.
- **Ticker overview:** it uses only free sources. Catalysts and risks are rule-based (the earnings and dividend calendar, FOMC/CPI dates, headline tags, levels and volatility), not from a research feed.
- **Nothing is committed or pushed** until you ask.
