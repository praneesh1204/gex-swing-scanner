# Strategy rules

Sources:
- **[SS]** `OptionsKit-Options-Selling-System-1.pdf`
- **[SP]** `Option-Spreads.pdf`
- **[DN]** `Delta-Neutral.pdf`
- **[ER]** `Earnings-OptionsKit-Coaching.pdf`
- **[PI]** `OptionsKit-Passive-Investing.pdf`

These are OptionsKit course slides (instructor: Ravish), cited as `[deck pN]`. **[SPEC]** is the gexscan v2 build spec. **[PB]** is a personal playbook file in the same folder (`million-mandate.html`); only its generic rules are used here, and it is not copied into the repo.

Status legend: ✅ adopted as default · ⚙️ configurable parameter · ❓ **conflict, needs your decision**.

---

## A. Regime / volatility filters

| # | Rule | Source | Status |
|---|---|---|---|
| A1 | **High IV:** VIX > 20 **and** IV percentile > 50 → "prime time" for selling | [SS p14] | ⚙️ |
| A2 | **Normal:** VIX 12–20 → steady selling at standard deltas | [SS p14] | ⚙️ |
| A3 | **Low:** VIX < 12 → scale back, be selective, or favour high-IV single names | [SS p14] | ⚙️ |
| A4 | Sell when range-bound/neutral and IV is elevated; buy when conviction is strong and IV is low | [SS p6] | ✅ |
| A5 | IV Percentile = % of past-year readings below today's IV; IV Rank = position between the 1-year high and low (0–100) | [SS p13] | ✅ (both computed) |
| A6 | Spec: prefer IV rank ≥ ~30 **or** IV > GARCH forecast | [SPEC §5A] | ❓ vs A1/A7 |
| A7 | Playbook: sell puts only when IV **rank** > 50; no *new* short puts until VIX > 22 | [PB] | ❓ vs A1/A6 |

> **❓ Q-A. Premium-selling IV gate.** Three different thresholds: IVR ≥ 30 [SPEC], IVP > 50 & VIX > 20 [SS], IVR > 50 & VIX > 22 [PB].
> Proposal: a **tiered** gate. IVR ≥ 30 or IV > GARCH is the minimum to *consider* a trade. VIX/IVP from [SS] set a regime label (low/normal/high) that scales size and max ideas per day. The [PB] VIX > 22 rule is an optional "strict mode" flag.

## B. Entry / structure selection

| # | Rule | Source | Status |
|---|---|---|---|
| B1 | Credit spread: sell OTM/ATM, buy further OTM | [SP p6] | ✅ |
| B2 | Debit spread: buy ITM/ATM, sell further OTM | [SP p6] | ✅ (spec: long Δ 0.55–0.70) |
| B3 | **Credit ≥ 1/3 of width** (e.g. $1.50 on a $5 spread) | [SP p9], [ER p6] | ❓ v0.1 uses 20 %; spec gives no number |
| B4 | Start with **$5-wide** spreads to keep risk manageable | [SP p5] | ⚙️ (v0.1: width ≈ 4 % of spot) |
| B5 | **30–45 DTE** for spreads; 30–45 d is the "sweet spot" for monthly income | [SP p9], [SS p11] | ❓ spec says 7–45; v0.1 uses 10–30 |
| B6 | Liquid underlyings, tight bid/ask | [SP p9] | ✅ |
| B7 | Iron condor: short strikes **10–25 Δ**, wings 5–10 pts; "high win rate but poor risk/reward" | [DN p10] | ⚙️ |
| B8 | Iron fly: wings sized for ~50 % POP | [DN p11] | ⚙️ (later) |
| B9 | Spec: short-put Δ 0.15–0.30 at/below put wall or HVN/VAL | [SPEC §5A] | ✅ (consistent with [ER p5] 20–30 Δ) |
| B10 | Always sell the strike closer to the money (don't invert legs) | [SP p10] | ✅ (validation check) |
| B11 | Delta-neutral short straddles/strangles (naked) | [DN p4–7] | ❌ **excluded:** undefined risk. Could add later as defined-risk variants only |

## C. Exits and management

| # | Rule | Source | Status |
|---|---|---|---|
| C1 | **Stop: exit when loss reaches 2× the credit; no rolling** | [SPEC §2] | ✅ **resolved (v2.1):** the strategy-engine spec defines it as "exit at 2× credit received, i.e. when the position's loss equals the credit". That is interpretation (b): cost to close = 2× credit, everywhere. The underlying-close stop at the short strike stays for verticals |
| C2 | Take profit at **25–50 %** of max profit; "the 50 % rule" for verticals | [SP p9, p12] | ✅ default 50 % |
| C3 | "Exit trades halfway to expiration": if 4 weeks, take profit at 2 weeks; if not in profit at 2 weeks, "consider adjustment or roll" | [SP p11] | ❓ spec time exit is "~21 DTE or 1–2 days before expiry" |
| C4 | Rolling is taught (roll out / up / down, ≥ 14–21 DTE), with the rule "never roll just to avoid taking a loss" | [SP p13–14] | ❌ **overridden by [SPEC]: no rolling** |
| C5 | Straddle: TP 25 %, SL 50–100 % of credit; strangle: TP 50 %, SL **100–200 %** of credit | [DN p5, p7] | Reference for C1 |
| C6 | Set alerts at breakeven, profit target and stop; review positions daily at open and close | [SP p11] | ✅ → `monitor` alerts |
| C7 | Have the exit plan before entry | [SP p9] | ✅ (hard rule 2) |
| C8 | Playbook: no new trade on a day you took a stop | [PB] | ✅ → cooling-off (§6 of spec) |

> **❓ Q-C1. What does "2× credit" mean?** For a credit spread with credit **C** and width **W**:
>
> | Interpretation | Close when spread value hits | Realised loss | Fires before max loss only if |
> |---|---|---|---|
> | **(a) Loss = 2× credit** (spec wording; top of [DN p7] strangle range) | 3C | 2C | C < W/3 |
> | **(b) Value = 2× credit** (v0.1 code; top of [DN p5] straddle range) | 2C | 1C | C < W/2 |
>
> **Conflict with B3.** If we also require credit ≥ W/3, interpretation (a) *never fires before max loss*. Example: $5 wide, $1.67 credit → max loss $3.33 ≈ 2C. So on spreads that meet the course's 1/3 rule, the stop is effectively "hold to max loss" and only the time exit and invalidation level do any work.
> For **cash-secured puts and covered calls** (no wing), (a) is well defined and meaningful.
> Options: (1) (a) for CSP/CC and (b) for spreads; (2) (a) everywhere and accept the above; (3) (b) everywhere; (4) (a), plus an underlying-price stop at the short strike.

> **❓ Q-C3. Time exit.** "Halfway to expiry" [SP] vs "21 DTE or 1–2 days before expiry" [SPEC]. For a 30-DTE entry these are ~15 DTE vs 21 DTE.
> Proposal: default = the earlier of 50 % of the entry DTE elapsed or 21 DTE; configurable. The "not in profit at the halfway point → adjust/roll" branch becomes **"not in profit at halfway → close"**.

## D. Earnings

| # | Rule | Source | Status |
|---|---|---|---|
| D1 | Default: exclude any structure whose expiry spans earnings | [SPEC §4.2] | ✅ (v0.1: 2-day buffer) |
| D2 | Playbook: no new options within **5 days** of a holding's earnings | [PB] | ❓ buffer 2 vs 5 days |
| D3 | Earnings-play mode: trade only if **IV > RV**, a clear consistent post-earnings pattern, and a **historic win rate ≥ 70 %** | [ER p4] | ⚙️ `--earnings-mode` (later phase) |
| D4 | Consistently up after earnings → sell 20–30 Δ put or bull put spread (25–50 Δ); consistently down → bear call spread (25–50 Δ) | [ER p5] | ⚙️ |
| D5 | Stays inside the expected move → IC on the expected move, 2–5 pt wings, credit ≥ 1/3 width; or an iron fly with > 50 % POP | [ER p6] | ⚙️ |
| D6 | Mostly moves outside the expected move → skip, or buy an IC/iron fly with wings inside the expected move | [ER p7] | ⚙️ |
| D7 | Open 3:30–4:00 pm ET; close at next open. If in a big loss, "can hold for some time"; optionally take CSP assignment | [ER p8–9] | ❓ holding a big loser conflicts with the stop rule. Proposal: in earnings mode the stop is the **defined max loss** (spreads only), no discretionary hold |

## E. Wheel, covered calls, LEAPs

| # | Rule | Source | Status |
|---|---|---|---|
| E1 | Wheel: CSP → assignment → covered call → called away → repeat; cost basis nets premium | [SS p9–10] | ✅ wheel helper |
| E2 | Short-put LEAPS: 6–12 mo, 30–50 Δ (cash-secured, ≥ 20 % ROI) or 15–30 Δ (margin, ≥ 50 % return on margin); TP 20–40 %; at long-term support or RSI < 30 | [PI p6] | ⚙️ |
| E3 | LEAPS covered calls: 30–50 Δ, 6–12 mo, ≥ 30 % ROI (≥ 20 % mega-cap); TP 50–80 %; *roll up & out* | [PI p8] | ❓ rolling vs "no rolling" |
| E4 | Super risk reversal: short 15–25 Δ LEAP put + ATM/OTM bull call spread | [PI p7] | ⚙️ (later) |
| E5 | Collars (zero-cost, hybrid) | [PI p17–20] | later |
| E6 | Spec LEAPs = **buying** deep-ITM calls Δ 0.75–0.85, 9–24 mo, extrinsic ≤ 10–15 %; PMCC; long call spreads | [SPEC §5C] | ✅ |

> **❓ Q-E. "LEAPs" means two different things.** The course's "LEAPS" [PI] mostly means **selling** long-dated options (short put LEAPS, LEAPS covered calls). The spec's `gexscan leaps` means **buying** deep-ITM calls / PMCC.
> Proposal: `gexscan leaps` covers both: long-LEAP ideas [SPEC] and short-put-LEAP ideas [PI p6]. LEAP covered calls follow the no-rolling rule, so they get a stop rather than a roll-up.

## F. Sizing and risk

| # | Rule | Source | Status |
|---|---|---|---|
| F1 | Never risk more than **2–5 %** of the account per trade | [SP p9] | Spec is stricter: **1–2 %** ✅ |
| F2 | Leverage ≤ 2× if used | [PI p5] | ✅ (informational; the app never computes margin use) |
| F3 | Single name ≤ 20 %, small cap ≤ 5 %, max 3 new ideas/day | [SPEC §6] | ✅ |
| F4 | Playbook: total CSP assignment notional ≤ 30 % of NLV | [PB] | ⚙️ add to portfolio checks |

## G. Things to note (not rules)

- [SS p5]'s "70–90 % win rate / 80 % expire worthless" and [SS p15]'s "2–12 % monthly" targets are **marketing claims**, not design inputs. The weekly review measures *your* expectancy instead.
- Slide typos, so don't copy numbers blindly: [SP p8] says "above $190 / below $180" for a $260/$270 spread; [SP p13] gives a breakeven of "$438.20 vs $443.50" for SPY at $640.
