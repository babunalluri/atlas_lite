# factor_paper — separate book (follow institutional flow)

Status: planned. **Not implemented.**

- **Tape result (2026-10-24):** nothing passed on our 23 days. The sweep setup as specified loses, and no filter passes on its own.
- **Direction (2026-10-25):** the book now **follows institutional flow**. Most institutional money is executed by algos that work large orders over hours, and that creates the intraday trends we can ride. The main setup is trend-following, with futures-OI and regime checks as confirmation. **The sweep is dropped as an entry,** because fading a move bets against an institution that has not finished its order.
- **Decision:** don't build it yet. Re-test on 12 months of history (Kite spot/VIX now, TrueData options later) before any code.

Book: **new, separate paper book `factor_paper`**:
- its own module, ledger and env flags;
- **agent_paper is not changed**: Scalper v2 stays live exactly as it is;
- if this book fails, it is switched off without touching v2.

Related: [agent-paper-liquidity-sweep.md](agent-paper-liquidity-sweep.md) (2026-10-20). That note planned *defensive* gates inside agent_paper: no chasing a running premium, plus a quiet period after a shock. This document plans a *trading setup* in its own book.

## Why

An option premium does not move with NIFTY alone. Over a 5–30 minute hold:

| Factor | What it does | Weight for a 5–30 min long |
|---|---|---|
| Delta (NIFTY move) | ATM moves ~0.5 pt per NIFTY pt | Most of the move |
| Gamma | Delta grows as the option goes in the money | Helps buyers on strong moves |
| Vega (IV / VIX) | Premium rises/falls with IV | Medium. IV often falls after a big move ("spot +100 but CE flat") |
| Theta | Premium bleeds every minute | Small over 30 min; large on expiry day and after 13:00 |
| Liquidity (spread, depth) | Cost to get in and out | ~1.25 pt (~₹80) every round trip |

Scalper v2 only uses spot vs the open (delta). This book tests:

1. **Trend-following as the main setup**: ride the footprint of institutional orders.
2. **Flow confirmation**:
   - futures OI (new positions vs covering);
   - previous day's FII/DII figures (not recorded yet).
3. **Regime check**: CE trends in up regimes, PE trends in down regimes, no trade when the market is range-bound.
4. **Factor filters**: IV, theta/expiry, VIX, ADX, depth. A filter is kept only if it improves results on data.
5. ~~Liquidity-sweep reversal~~: tested and dropped as an entry (see Tape results).

### Institutional algos and what they mean for this book

| Type | What it does | Effect on us |
|---|---|---|
| Execution algos (FIIs, mutual funds) | Slice a huge buy or sell order over hours | Create the intraday trend; **this is what we follow** |
| Market makers / HFT | Quote bid/ask, earn the spread | Take ~1.25 pt per round trip from us |
| Option desks | Sell options priced above the move that happens | Take money from option buyers via time decay |
| Prop / directional | Push price through obvious levels, often on expiry | The "retail hunting" part; smaller than assumed. Our sweep test found no tradeable reversal |

We never see an institution's order, only its footprint. Stops and time exits stay.

### Where we stand (2026-10-24)

- **Scalper v2 is live in agent_paper**, 1 lot, ₹20k margin:
  - buys ATM CE/PE when spot is ≥100 pt from the open;
  - entries 09:30–12:00;
  - exits +20% / −10% / 30 min; max 3 trades/day.
- **Live, 2026-10-23:** 2 rule trades, +₹3,085. A third trade was closed manually.
- **Constraints:**
  - buying only (no naked shorts on ₹20k), MIS, 1 lot of 65, Kite market orders;
  - a new rule must earn more than ~1.25 pt per trade after costs.
- **Main risk is overfitting.** Each parameter added to 23 days of data makes the past look better without making live better.

## Tape results (23 days, 2026-09-03 → 2026-10-12)

**How these numbers were made:**
- Script: `scripts/bt_sweep_factors.py`, with the cache built by `scripts/build_spot_cache.py`.
- Book rules as live v2, applied to every setup (v2 and sweep alike):
  - 1 lot; +20% / −10% / 30 min;
  - max 3 trades/day, −₹2,000 day stop;
  - one position at a time; side blocks (stop 15 min, loss 10 min, win 5 min).
- Costs: 0.5 pt slippage per leg plus Kite charges.
- Halves split at 24 Sep.
- PASS uses the bars in Validation.

### Liquidity sweep: fails

| Variant (X pt beyond level, N min to reclaim) | Trades | ₹/trade | Total |
|---|---|---|---|
| X=5, N=3 | 11 | −406 | −₹4,463 |
| X=5, N=5 | 13 | −516 | −₹6,708 |
| X=5, N=10 | 15 | −550 | −₹8,250 |
| X=10, N=3 | 5 | −438 | −₹2,190 |
| X=10, N=5 (base) | 6 | −558 | −₹3,351 |
| X=10, N=10 | 7 | −603 | −₹4,220 |
| X=20, any N | 0–1 | — | — |

- **Every variant loses.** Win rate is 2–3 trades in 5–15.
- **By level (base variant):**
  - opening range: 4 trades, −₹1,064;
  - previous day: 2 trades, −₹2,287.
- **Allowing trades against the v2 trend** makes it worse: 7 trades, −₹3,969.
- **Too few events.** A sweep-and-reclaim of these levels happened only 5–15 times in 23 days, so even a good rule could not be proven on this data.

### v2 by time band (real spot vs open, full day)

| Band | Trades | ₹/trade | Total | First half / second half |
|---|---|---|---|---|
| 09:30–12:00 | 21 | +90 | +₹1,890 | −₹830 / +₹2,719 |
| 12:00–13:30 | 7 | −235 | −₹1,642 | −₹1,275 / −₹367 |
| 13:30–14:44 | 7 | −323 | −₹2,262 | −₹1,045 / −₹1,217 |

- **The afternoon loses in both bands.** v2's 12:00 cut stays.
- **Correction to the earlier v2 test (+₹179/trade over 25 trades):**
  - that test used the ATM *strike* moving 100 pt as the trigger. Live v2 uses *spot* vs Kite's open;
  - on real spot, the morning is **+₹90/trade, and negative in the first half (−₹830)**, so it **does not meet the pass bar**;
  - re-running the strike version in the new script gave +₹87/trade with the first half negative as well, so the earlier +₹179 does not reproduce.

  This is the main reason to **keep v2 at 1 lot and keep the kill rule** (revert to paper if net < −₹1,500 after 20 live trades) until TrueData confirms it.

### Factor filters (each alone, full day 09:30–14:44)

| Filter | v2 result | Morning (09:30–12:00) | Afternoon | Verdict |
|---|---|---|---|---|
| None (baseline) | 35 trades, −₹58/tr | 21, +₹90/tr | 14 trades, −₹3,904 | — |
| IV rise < 10% from day's low | 29, +₹248/tr | 20, **+₹515/tr**, H1 −₹603 | 12:00–13:30 +₹574 (5); 13:30–14:44 −₹3,671 (4) | **Best candidate**; fails the both-halves bar. Re-test on TrueData |
| VIX rise < 5% from the open | 31, +₹1/tr | 21, +₹185/tr, H1 −₹830 | 13:30–14:44 −₹3,671 | Fails |
| ADX ≥ 25 | 32, −₹3/tr | 18, +₹103/tr, H1 −₹146 | 13:30–14:44 −₹2,230 | Fails. As expected, ADX adds little |
| IVP ≤ 70 | 21, −₹305/tr | 8, −₹393/tr | — | Fails. It removes the good morning trades |
| No entries after 13:00 on expiry day | Same as baseline | — | — | No effect; no v2 entries hit it |

- **On the sweep:** every filter leaves it negative. Its 6 trades are too few to judge filters.
- **No filter makes either afternoon band pass** in both halves.
- **The IV-rise filter result is fragile.** Dropping 1 trade changed later trades the same day and added about ₹8,400. It needs hundreds of trades before it can be trusted.
- **Not tested yet:**
  - **spread/depth:** only 6 days of depth data;
  - **OI/PCR:** low prior; order-book tests found no edge earlier.

### Setup search with cross-validation (1,080 combos)

**How it was run:**
- Script: `scripts/bt_setup_search.py`.
- Five buy-only setup families × 6 exit profiles × 3 filters (none, IV rise < 10%, VIX rise < 5%).
- Best params were picked on one half of the days and scored on the other half, then the reverse.
- Only the unseen half counts.

| Family | What it is | Out-of-sample result | Verdict |
|---|---|---|---|
| **Trend** | Spot ≥ T pt from the open → buy that side | **+₹25,205 over 46 unseen trades**; both folds picked the same rule | Only family that holds out of sample; **put-side only** (below) |
| Pullback | Trend ≥ T, pull back ≥ P, resume → buy | +₹4,111 over 12 trades | Too few trades |
| ORB | 1-min close beyond the opening range + X → follow | −₹6,714 over 17 trades | Fails |
| Gap | Gap ≥ G vs previous close → follow / fade at open | −₹1,180 over 3 trades | Fails |
| Sweep | Sweep and reclaim of OR/PD high/low → reverse | −₹1,837 over 4 trades | Fails |

**The trend winner:** T = 50 pt, 09:30–14:44, exits +50% / −25% / 90 min, skip if VIX is up ≥ 5% from its first print.

| Measure | Value |
|---|---|
| Trades / total | 46 / +₹25,205 (+₹548 per trade) |
| CE side | 20 trades, **−₹8,336** |
| PE side | 26 trades, **+₹33,540** |
| Worst trade / worst day | −₹2,577 / −₹2,577 (above the −₹2,000 cap) |
| Max drawdown | −₹4,548 |
| Without its 2 best days | +₹10,240 |

**Why it is not ready:**

- **NIFTY fell 6.8% over the sample** (23,998 → 22,363; 13 of 23 days down). Every trend version makes money on puts and loses on calls, including live v2 (CE −₹450, PE +₹2,339). Cross-validation does not protect against this, because both halves are in the same falling market. The "edge" may simply be "buy puts in a falling market".
- **One −25% stop costs more than the −₹2,000 day cap** on a ~₹150 premium.
- **It is not a scalper.** It holds for up to 90 minutes.
- **Profit is concentrated.** Live v2's rules (100 pt, 20/10/30, morning) are −₹3,248 without their 2 best days.

**Next step:** forward-test it on paper in `factor_paper` (no money), next to live v2, through a rising or sideways market. Validate on TrueData months where NIFTY rose. It needs a CE side that doesn't lose before any live money.

### Futures-OI confirmation (2026-10-25)

**How it was run:**
- Script: `scripts/bt_oi_confirm.py`.
- Trend entries, taken only when NIFTY futures OI confirms the move:
  - **build-up:** OI up since the open, or over the last 15 min = new positions in the move's direction;
  - **covering:** OI down since the open = positions being closed.
- Book rules and costs as above.

| Setup | No OI check | OI build-up (best threshold) | OI covering (down since open) |
|---|---|---|---|
| v2 live (T100, morning, 20/10/30) | 21 tr, +₹90/tr; H1 −₹830 | ≥ 0.5%: 5 tr, +₹151/tr; H1 −₹588 | **13 tr, +₹503/tr; both halves +; CE +₹2,154, PE +₹4,379** |
| T50 full day, 20/10/30 | 42 tr, +₹25/tr | ≥ 2%: 8 tr, +₹561/tr | 28 tr, −₹39/tr |
| T50 full day, 50/25/90 | 48 tr, +₹341/tr | ≥ 0%: 27 tr, +₹387/tr | 30 tr, +₹118/tr; H1 −₹836 |
| T100 full day, 20/10/30 | 35 tr, −₹58/tr | ≥ 2%: 7 tr, +₹134/tr | 27 tr, +₹54/tr; H1 −₹2,403 |

**Findings:**

- **The OI build-up check does not help consistently.** Results flip sign between thresholds: on v2, 0% loses, 0.5% wins, 1% loses. That is noise, not a signal, on this sample.
- **The OI covering check on v2's rules is the only variant that is positive in both halves *and* on both sides.** It fits the idea that forced exits (short covering, long unwinding) are urgent flow that follows through within 30 minutes. But:
  - it has only 13 trades;
  - it does not repeat on the other trend setups.

  Treat it as a hypothesis for the 12-month test, not a rule.
- **Live v2's tape profit comes from the expiry/rollover week** (24–29 Sep). Without those 4 days, v2's rules are 16 trades, −₹68/tr, negative in both halves. That is another reason to keep v2 at 1 lot under the kill rule.
- **Data caveats:**
  - OI is distorted by monthly rollover: it falls into expiry and resets the next day;
  - OI was frozen on 14 Sep and 2 Oct (holiday);
  - FII/DII daily figures are not recorded yet.

## Setup 1 — liquidity sweep (reversal), as tested — DROPPED as an entry

**Idea:** stops cluster just beyond obvious levels. A fast move through a level triggers them, then price snaps back inside. The snap-back is the trade.

### Levels (spot)

| Level | Definition | Available from |
|---|---|---|
| ORH / ORL | Opening-range high / low, 09:15–09:30 spot | 09:30 |
| PDH / PDL | Previous day's high / low | Open |

### Detection

1. **Sweep:** the previous 1-min close is inside the level, then the minute's high goes above level + X (or its low below level − X).
2. **Reclaim:** within N minutes, a 1-min close is back inside the level.
3. Each level fires at most once per day.

### Entry, exits, risk

- **Swept the high:** buy ATM **PE** on the minute after the reclaim close.
- **Swept the low:** buy ATM **CE** on the minute after the reclaim close.
- **Window:** full day 09:30–14:44, reported by band. The last entry is 14:44, so a 30-minute hold ends by the 15:14 MIS square-off.
- **Against an active v2 trend** (spot ≥100 pt from the open the other way): not taken.
- **Exits:** +20% / −10% / 30 min.
- **Risk:** 1 lot, max 3 trades/day, −₹2,000 day cap.

### Next versions to try (on TrueData only)

- **Spot invalidation stop:** stop when spot goes back beyond the sweep extreme.
- **More levels:** round numbers (every 100 pt) and swing highs/lows.
- **Depth confirmation on the sweep minute:** depth pulled on the swept side, or a volume spike.

## Factor filters (candidate rules)

All inputs are already recorded every minute in `data/recordings/*.jsonl.gz` → `feed`:

- `iv`, `iv_day_high`, `iv_day_low`, `ivp`, `iv_chg_5d`
- `vix`
- `pcr`, `ce_oi`, `pe_oi`, `fut_oi`, `oi_pct_chg`
- `adx`, `atr`, `max_pain`, `expiry`

Data issue: `vix_chg` is wrong on several days (it equals `vix`), so the test computes VIX change from the day's first print.

Black-76 IV and greeks already exist in `atlas_lite/metrics.py` (`black76_greeks`, `atm_iv_from_prices`).

| Filter | Question it answers | Rule tested |
|---|---|---|
| IV spike | Is the premium already pumped? | Skip if ATM IV ≥ 10% above the day's low so far |
| IV rank | Are options expensive today? | Skip if `ivp` > 70 |
| Theta / expiry | Will decay eat the hold? | Skip expiry day after 13:00 |
| VIX move | Is the regime shifting? | Skip if VIX ≥ 5% above its first print |
| Trend strength | Does ADX help? | Require `adx` ≥ 25 |
| Spread / depth | Is cost above normal? | Not tested (needs more depth data) |
| OI / PCR | Do positioning shifts predict direction? | Not tested |

**Rules for filters:**
- Test one at a time, over the full day, and report by time band.
- Keep a filter only if it improves net ₹/trade in **both** halves and on **both** sides (CE and PE).
- At most **2 filters** per setup.
- v2's 12:00 cut is lifted only if a filtered afternoon passes validation.

## Data

| Source | What | Status |
|---|---|---|
| `data/recordings/<day>_*.jsonl.gz` | Sub-second spot, ATM CE/PE, IV, VIX, OI, PCR, ADX | 23 days locally (to 12 Oct); recording continues on OCI |
| `data/recordings/structure-*` | ±50/100/250 wings | From 24 Sep |
| `data/recordings/depth-*` | ~1 s, 5-level depth, ATM ±1 strikes | From 5 Oct |
| TrueData (to buy) | 12 months of 1-min expired NIFTY weekly options, spot, VIX, futures; optional tick bid/ask | Pending quote |

**What to ask TrueData:**

- Are expired weekly contracts included?
- How many months of history?
- One-time download or subscription?
- CSV or API?
- Bid/ask, or last traded price only?
- Price for history only? (We don't need their live feed; Kite covers live.)

Store their files in `data/truedata/` (not in git). A loader converts them into the same per-minute format as `px_cache.pkl` / `data/spot_cache.pkl`, so `scripts/bt_sweep_factors.py` runs unchanged.

## Validation (pass / fail)

**Method:**

1. Tape replay with book rules, 1 lot. Costs: 0.5 pt slippage per leg plus Kite charges (conservative).
2. Split the data:
   - **23 days:** first half / second half.
   - **TrueData:** fit on the first 8 months, test on the last 4. The test period is used once.
3. Count every variant tried, and report it. Picking the best of many variants inflates results. The 23-day run tried 9 sweep variants and 5 filters.

**A setup or filter passes only if all of these hold:**

| Check | Bar |
|---|---|
| Net ₹/trade after costs | > 0 in **both** halves (or the fit and the test period) |
| By side | > 0 for CE and for PE, or the setup trades one side only by design |
| Sample | ≥ 30 trades on 23 days; ≥ 150 on 12 months |
| Worst day | ≥ −₹2,000 (the daily cap) |
| Profit factor | ≥ 1.2 on the test period |
| Parameters | ≤ 3 tuned numbers per setup |
| Time band | Each enabled band (09:30–12:00, 12:00–13:30, 13:30–14:44) must pass on its own |

## Rollout

1. ~~**Tape test on 23 days**~~ Done 2026-10-24. Nothing passed (see Tape results).
2. **Regime test on ~1 year of Kite spot + VIX** (free; needs a valid Kite token):
   - estimate premiums with Black-76 from spot, VIX and time to expiry;
   - test the regime switch (CE trends in up regimes, PE in down, none in range);
   - test the OI-covering hypothesis, if Kite futures OI history allows.
3. **TrueData validation** (when bought):
   - re-test v2, trend + flow confirmation and the IV-rise filter on 12 months of real option prices;
   - break out results by month, trend vs range, and expiry vs non-expiry days.
4. **Build `factor_paper` as a separate book**, off by default (`FACTOR_PAPER=0`), only for what passed steps 2–3.
5. **Paper:** run it next to live v2 for **≥ 20 trades**. Compare with the tape expectation.
6. **Live, 1 lot:** only if paper net > 0 and it matches the tape within reason. Margin rule first (see Implementation notes).
7. **Kill rules:**
   - after 20 live trades, revert to paper if net < −₹1,500;
   - the −₹2,000 daily cap and the one-hard-reject block apply as in agent_paper.

## Implementation notes (for when it is built)

| Area | Change |
|---|---|
| Book | New module (e.g. `atlas_lite/paper_factor.py`) and its own ledger. **No edits to `paper_agent.py` or `agent_scorecard.py`** |
| Env | `FACTOR_PAPER` (default `0`), `FACTOR_LIVE` (default `0`), `SWEEP_PTS`, `SWEEP_RECLAIM_MIN`, one flag per filter |
| Levels | ORH/ORL from 09:15–09:30 spot; PDH/PDL from previous day (Kite historical or stored EOD) |
| Detection | Must use 1-min closes, not the ~120 s advise-cycle snapshot |
| Exits | Same profile as v2 (+20% / −10% / 30 min); a shared helper is fine if agent_paper's behaviour does not change |
| Margin | ₹20k covers one lot at a time. Before both books go live together, add a rule: a book may not open while the other holds a live position |
| UI | Separate card/tab; its PnL is not mixed into agent_paper |
| Tests | Own test file: sweep + reclaim → entry; no reclaim → no entry; against v2 trend → no entry; 3-trade and ₹2k caps; filters reject with logged reason |

## Changes vs the 2026-10-20 note

- **PDH/PDL:** the earlier note said not to add them to agent_paper. They are used here only as sweep levels, in a separate book. Other ICT concepts stay out (order blocks, PO3, OTE, premium/discount).
- **Gates still valid:** the earlier note's "no chasing a running premium" gate can be tested as one more filter, under the same pass/fail rules.

## Decision log

| Date | Note |
|---|---|
| 2026-10-23 | Scalper v2 went live; day 1 +₹3,085 on 2 rule trades. |
| 2026-10-24 | User: build a book that uses option factors (IV, theta, liquidity) plus a liquidity-sweep setup; document it in `doc/`. |
| 2026-10-24 | User: new setups and filters are not capped at 12:00. Test 09:30–14:44 by time band; enable only the bands that pass. |
| 2026-10-24 | User: a separate book, `factor_paper`; **no change to agent_paper**. |
| 2026-10-24 | 23-day tape test: the sweep fails (all 9 variants negative); no filter passes; the v2 morning on real spot is +₹90/tr with the first half negative. Decision: don't build; wait for TrueData. v2 stays live at 1 lot under the kill rule. |
| 2026-10-24 | Setup search, 1,080 combos with cross-validation: only trend-following holds out of sample (T=50, +50%/−25%/90 min, VIX filter: +₹25,205 over 46 unseen trades), but all profit is on puts in a 6.8% falling market and CE loses. Next: paper forward test in `factor_paper`, plus TrueData up-months. No live money. |
| 2026-10-25 | User: the book should follow institutional (algo) flow. Direction changed: trend-following + futures-OI/FII/regime confirmation; sweep dropped as an entry. OI test: build-up is noise; covering on v2's rules is the only both-halves, both-sides variant (13 trades), a hypothesis for the 12-month test. v2's tape profit depends on the rollover week. |

## Status checklist

- [x] Plan written under `doc/`
- [x] Sweep tape test on 23 days: failed
- [x] Filter tests (IV spike, IVP, expiry theta, VIX, ADX): none passed; IV rise is the candidate to re-test
- [x] Setup search (trend, ORB, pullback, gap, sweep × exits × filters; cross-validated): trend T=50 wide-exit is the candidate, put-side only
- [x] Futures-OI confirmation (build-up / covering): build-up noise; covering = hypothesis
- [ ] Record FII/DII daily figures (NSE) and test as a regime input
- [ ] Download ~1 year NIFTY 5m + VIX from Kite (`scripts/kite_regime_history.py`); test regime switch (CE in up, PE in down, none in range)
- [ ] Spread/depth and PCR filters (need more data)
- [ ] TrueData history bought and loaded
- [ ] 12-month validation of v2, sweep and IV-rise filter
- [ ] `factor_paper` built as a separate book (`FACTOR_PAPER=0`)
- [ ] ≥ 20 paper trades
- [ ] Explicit user decision to go live
