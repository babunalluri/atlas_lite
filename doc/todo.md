# Trading to-do

Checked every trading day. Tick items as done (`[x]`), add new ones with a date. Paper only until an explicit live decision; ₹2.5 lakh margin fits one selling book live.

## Every trading day (after 15:20)

- [ ] **Scalper v3:** traded at 13:00 on a non-expiry day? Exit reason and P&L on the dashboard.
- [ ] **Live straddle (once deployed, your choice, 1 lot):** tick "Live straddle" on the dashboard before 09:16 on days you want real orders (good for that day only; off at 15:30). Watch the dashboard line for LIVE, the stop, and any "EXIT FAILED — close manually in Kite". Live rules: balanced strike, sell 13:00, emergency stop −30%, month limit −₹15,000, funds check, failed second leg sold back at once, failed exits retried 5× then manual.
- [ ] **Long straddle + its +20% take-profit variant (from their deploy days):** both bought at 09:16; main sold at 13:00, variant at +20% (bids) or 13:00? Compare the two P&Ls. Each book has its own gate (pausing one does not pause the other). P&L, and the NIFTY move 09:16→13:00 (break-even ~97 pts in the backtest). Expiry days are traded and marked `expiry_day`; `time_late` exits (engine down at 13:00) are left out of the analysis.
- [ ] **Gamma reading** logged with today's v3 entry (dashboard: "last entry gamma long/short").
- [ ] **Weekly strangle / condor:** 15:25 stop check logged; open P&L sensible.
- [ ] **Paper vs backtest:** `.venv/bin/python scripts/paper_vs_backtest.py --fetch`. A gap above ~₹300/trade, or an exit that differs from the backtest, gets investigated.
- [ ] **Server health:** dashboard loads; no repeated warnings.
- [ ] **NSE vs BSE gap watcher (research week, 2 – 6 Nov):** dashboard "NSE vs BSE gaps" line says live/final for today. It is the `arb-watch` service (`docker compose ps`; logs: `docker logs atlas-lite-arb`), restarted by Docker like the engine. Turn off with `ATLAS_LITE_ARB_WATCH=0` in the server `.env`, or remove the service when the research is done.
- [ ] **Pre-open basis (09:15–10:00):** dashboard "Pre-open basis" line / `/api/pob`. Hosted inside `cf-watch` (same Kite WS; `data/pob/`). Short futures that are rich vs the stock's own previous-day closing basis (aged by calendar-day carry); full-lot pricing; confirmed + 300 ms probes; force exit 10:00; one capital pool with the cash–futures book. Off with `ATLAS_LITE_POB_WATCH=0`. Cash–futures has its own dashboard line too (`/api/cf`).

## Dated

### Mon 2 Nov 2026
- [ ] v3 trades at 13:00: **first entry with a gamma reading** (check the open event has `gamma`, not null).
- [ ] **NSE vs BSE gap watcher** (read-only, `arb-watch` service): after 09:15, `docker logs atlas-lite-arb` should say `connected` (needs Monday's fresh Kite token).
- [ ] **Waiting for your deploy go (after 15:30), not committed — server runs an older, unreviewed version since 12:25:** cash–futures full-lot pricing on entry *and* exit (top-of-book signals were phantoms: −₹16.5k paper on 2 Nov, mostly futures-only unwinds); pre-open basis book referenced to each stock's previous-day closing basis (a flat 6.5% carry made 33 stocks look rich every morning), isolated so its failures never touch the cash–futures feed (`preopen_book_errors` in the report). After deploy: `docker logs atlas-lite-cf` says `preopen-basis research on`; tomorrow 09:15 is the first clean day for both books. Treat cash–futures 2 Nov as void.
- [ ] After 15:30: read `data/arb/summary-2026-11-02.json`, **small caps first, then mid, then large** (658 stocks: 515 small, 98 mid, 45 large; BSE groups A/B/X only): crossings, how many beat the ~0.083% cost, how long they lasted, shares available. Decide whether NSE–BSE arbitrage is worth anything, starting with small caps. Also `data/arb/paper-2026-11-02.jsonl` and the `paper` section per group: simulated IOC orders at 100/300/600 ms delay, both-filled vs one-leg (unwound at market) vs none, net paper P&L. The 300 ms row is the realistic one for Kite. Paper trades fire only when estimated net after costs is ≥ ₹500 (`--min-net`); with the ₹1 lakh size cap that needs a gap of about 0.58%. If none fire, check the "Est. net ≥ min" column before changing the cap or threshold.

- [ ] **Waiting for your deploy go (after 15:30 only), not committed:** confirmation rule for both watchers. Retro of the morning: the gap watcher's CPU was ~4% (not the cause); a crossing seen on one quote update never filled both legs (2 of 1,576 attempts, all tracks, 300 ms) vs 11–20% for ones seen twice. Gap watcher: new **confirmed** paper attempts beside every track and delay (dashboard "Fires on" selector); existing tracks unchanged, so the week stays comparable. Cash–futures: entry now needs the wide spread on two updates in a row (≤5 s apart); first sightings are logged as unconfirmed, and every sighting gets a no-position entry-fill **probe** (`entry_probes` in the report); also fixed: the current minute no longer leaks into the 60-min baseline (a spike raised its own baseline and could trigger an early "reverted" exit).

### Tue 3 Nov 2026 (weekly expiry)
- [ ] 15:29: weekly strangle and condor close at expiry. v3 skips the day.
- [ ] Evening: download the NSE files, then compare:
  ```
  .venv/bin/python scripts/nse_nifty_options_history.py --start 2026-10-29
  .venv/bin/python scripts/paper_vs_backtest.py --fetch
  ```
  First weekly paper-vs-backtest: same strikes? credit within ~2 pts? P&L gap?

### Thu 5 Nov 2026
- [ ] 15:25: weekly books open the next week (expiry 10 Nov). No stop check on the entry evening.

### Fri 6 Nov 2026
- [ ] **Go-live review: the bar, fixed on 3 Nov before seeing the week** (paper only until then). A book goes live only if it meets **all** of these on real bid/ask paper fills:
  1. Net positive for the week after all charges, and positive on at least **4 of 5 days** (or, for books that trade rarely, at least ~10 trades with most of the profit not from one trade or one stock).
  2. Its paper result is within the range its backtest predicted (no sign the paper fills are flattering it).
  3. A live order path already exists and is tested for it, with a margin check against ₹2.5L, partial-fill handling and a kill rule. Today only the agent has one; the NSE–BSE arb and cash–futures books have none (two-exchange orders with an unwind would need building and its own paper week first).
  4. Only one selling book live at a time (any two exceed ₹2.5L margin).
  Books with less than a week of data (cash–futures from 3 Nov, long straddle from 3 Nov, weekly condor after one cycle) stay paper and are judged on 13 Nov / ~27 Nov as planned. The NSE–BSE arb is judged on 09:15–09:20, ₹500+ and ₹1,000+, everything from 15:15 excluded (the 2 Nov 15:20 burst of 2–3% "gaps" was untradable closing-session quotes, +₹2 lakh of fake paper profit).
- [x] **NSE vs BSE research week, done 6 Nov** (`scratchpad/arb_week.py`, run on the server; 2 Nov has first sightings only, confirmation came later that day; everything from 15:15 excluded).
  - **Only 09:15–09:20 on F&O stocks is ever positive.** Every bar after 09:20, and every non-F&O row at any time, lost money (non-F&O filled both legs 0.4–5% of the time).
  - 09:15–09:20, F&O, confirmed, 300 ms: ₹1 −₹37.6k, ₹100 −₹6.0k, ₹200 −₹1.1k, ₹300 +₹993, ₹500 **+₹1,693** (27 tries, 26% both, up 2 of 4 days), ₹1,000 +₹2,586 (5 tries, none filled both: lucky one-leg unwinds).
  - First sightings did better than confirmed at ₹200–500 (₹500: +₹3,567 over 5 days, up 3 of 5), mostly from 2 Nov. 100 ms orders beat 300 ms (₹500: +₹3,049), 600 ms is worse. By group, mid caps did best (₹300: +₹1,511); small and large were near zero.
  - Capital replay (₹500 bar, filled pairs hold their notional all day): ₹1 lakh +₹224–673 over 4 days; ₹2.5 lakh **+₹1,406** (about ₹350 a day on ₹2.5L). Ordering by estimate barely matters.
  - **Verdict:** a real but tiny edge, about ₹300–400 a day, not every day, with live fills worse than paper (5 Nov live −₹172). **Recommend: keep the live arb off and retire the arb-watch service** (frees one of Kite's 3 websockets). Your call.
- [x] **Review done 6 Nov (evening).** The week was a strong trend (NIFTY ~22,970 → ~24,680, about +7% in 6 sessions): good for buying, bad for selling.
  - **Long straddle:** live −₹2,229 (4 Nov) and −₹324 (5 Nov, the book's estimate; both legs were sold by hand at 09:33, logged `manual_needed`). Live and arb checkboxes were switched off 5 Nov 09:47. Paper +₹3,907 (3 Nov) and +₹5,541 (6 Nov, live was off). tp20: −₹2,233, −₹380, +₹4,350.
  - **Arb:** live 1 trade (5 Nov CGPOWER, −₹172). Window 09:15–09:20, F&O, confirmed, 300 ms, 3–6 Nov: ₹300 bar +₹2,611 / −₹75 / −₹634 / −₹908 = **+₹994**; ₹500 bar +₹1,934 / 0 / −₹796 / +₹554 = **+₹1,692**. ₹300 is not better; keep ₹500 or stop. Positive on 2 of 4 days only, so it fails the "4 of 5 days" bar.
  - **Scalper v3:** −₹3,064 (5 Nov) and −₹6,400 (6 Nov, stop at 130% of a 315-pt credit). Paper record now 30 Oct–6 Nov: +695, +1,236, +586, −3,064, −6,400 = **−₹6,947**. A stop is now ~₹6k at these premiums, larger than the backtest's worst hold day.
  - **Weekly strangle / condor 5%:** −₹15,396 / −₹15,652 on 3 Nov (NIFTY ran through the 23,550 call on expiry day; the once-a-day stop never saw it).
  - **Condor wing re-test (NSE EOD 2019–Oct 2026, 1.5 SD shorts, slip 1):** 1% / 1.5% / 2% wings: −₹31k / +₹4k / +₹29k total (3 sessions), worse at 2 sessions and all negative at slip 2. **Closer wings lose; not a candidate.** Only the strangle and the 5% condor stay.
  - **Fixed-width strangle (same data):** 1,000 pts wide, sold **2 sessions** before expiry: +₹426/wk, PF 2.44 / 2.39 (2019–23 / 2024–26), no losing year, worst week −₹20.6k; at 3 sessions fixed widths lost in 2024. → new paper book `weekly_strangle1000`.
  - **short_iron_condor:** −₹28,036 over 14 paper days (7 losing), −₹33.7k on 2–6 Nov. **Retired** in code (off by default from the next deploy; its open 10 Nov set will not be settled in the ledger).
- [ ] **Waiting for your deploy go (local, tests pass 942).** At deploy also: back up the server `.env`, then set `ATLAS_LITE_ARB_WATCH=0`, `ATLAS_LITE_ARB_LIVE_ALLOWED=0`, `ATLAS_LITE_CF_WATCH=0` (retire arb-watch and cf-watch; see the 6 Nov review). Risk manager ships in shadow mode. New paper books: new paper books, all paper only, own ledgers and dashboard lines:
  - `weekly_strangle1000`: 15:25, 2 sessions before expiry (Friday), put/call 1,000 pts apart, 3× stop, no wings.
  - `long_straddle_trail`: 09:16 straddle; once bids +10%, sell 15% below the peak; −30% stop; else 13:00 (backtest +₹1,248/day, 64% positive).
  - `long_straddle_leg50`: 09:16 straddle; sell a leg once its bid is −50% vs its own cost; other leg to 13:00 (backtest +₹2,033/day, worst −₹3,219).
  - `scalper_v3_1415`: 14:15 ATM straddle sale, hold to 15:14, no target/stop (backtest +₹450/day, t 4.1, best of 6 times tried).
  - `scalper_v3_wings`: v3 plus wings 1,000 pts out (iron fly); same target/stop on the net credit.
  - tp20 keeps running alongside (its ~27 Nov comparison stays).
  - **SENSEX option recording** (for a SENSEX morning-straddle test): `recordings/sensex-structure-DAY.jsonl` once a minute (ATM, ±100/200/500, next expiry), plus SENSEX ATM±1 bid/ask in the depth tape (rows carry `"index": "sensex"`). NIFTY files unchanged; the NIFTY builders never read the SENSEX file. ~8 MB/day more.
- [x] **Pre-open basis week, done 6 Nov:** 3 recorded days (no `pob` file for 5 Nov: the watcher did not record that morning). Trades: 3 Nov TATACONSUM +₹3,463 and GODREJCP −₹2,053; 4 Nov ASIANPAINT −₹718; 6 Nov POWERGRID −₹1,745. Net **−₹815** (daily summaries, one-leg unwinds included; the 4 trades alone −₹1,053); three of four "rich" signals never reverted and were closed at 10:00. Confirmed probes filled both legs ~33–38% of the time (vs CF's midday book 13–48%). Cash–futures (all day): 3–6 Nov **−₹22,058** (−₹9,175, −₹2,897, −₹2,878, −₹7,108), losing every day, mostly from one-leg unwinds. **Recommend: retire both at the 13 Nov CF decision** (or now, your call): neither shows an edge.
- [x] **ai_agent decision:** retired 2 Nov (shadow −₹2,450 over 42 trades, all from morning shorts; side choice never beat random). `ATLAS_LITE_PAPER_AI_AGENT=0`; ledger and model kept.

### ~Mon 7 Dec 2026
- [ ] **SENSEX morning straddle, first real-price test** (~4 weeks of `sensex-structure` + depth from the deploy day): same rules as NIFTY (09:16 balanced strike, ask in, bid out at 13:00, Kite BFO charges). Needs a builder for the SENSEX files (copy of `scripts/build_px_real.py` reading `sensex-structure-*`). Only if it is clearly positive does a paper book follow.

### ~Fri 27 Nov 2026 (about 55 real days)
- [ ] Rebuild real prices (`paper_vs_backtest.py --fetch` does it) and recheck v3 on real premiums only: per-trade range should exclude zero before going live.
- [ ] **Gamma filter decision:** split v3's real trades by regime. Adopt only if positive-gamma days beat negative by a clear margin over 20+ trades each way.
- [ ] **Entry time:** 13:00 vs 14:15 (`scalper_v3_1415`, hold to 15:14) on paper fills. Backtest on 27 real non-expiry days: 13:00 hold +₹533 (worst −₹3,585, t 2.2), 14:15 +₹450 (worst −₹936, t 4.1), 14:30 +₹261 (worst −₹2,908). Also v3 vs `scalper_v3_wings` (cost of ±1,000 wings ~₹55/day; check Kite basket margin for the fly).
- [ ] **Weekly strangle 1,000 pts (2 sessions) vs 1.5 SD (3 sessions):** compare each expiry's paper P&L; naked strangle margin ~₹1.75L, so only one selling book can go live.
- [ ] **Long straddle exit variants:** main vs tp20 vs `long_straddle_trail` vs `long_straddle_leg50`, same entries. Pick on average and worst day over ~20 days, calm days included.
- [ ] **Weekly condor live decision:** after 3–4 weekly paper cycles matching the backtest.
- [ ] **Agent (scalper) decision, with the long straddle:** from 2 Nov its paper fills are at bid/ask (the close logs `trigger_px` next to the fill); it trades longs only (`ATLAS_LITE_AGENT_ALLOW_SHORT=0`, off since 19 Oct). Before that, re-priced at bid/ask, its longs made +₹11.1k over 5–30 Oct (t 1.5, two days carry it, mostly before 11:00). If the long straddle (no direction call) earns as much, agent's side-picking adds nothing: retire it.
- [ ] **Long straddle vs its +20% take-profit variant (`long_straddle_tp20`):** same 09:16 entry; the variant sells at exactly +20% of the price paid once the bids reach it, else 13:00. Backtest (37 real days): main +₹1,768/day, 56% positive, t 2.78; variant +₹1,065/day, 65% positive, t 2.92. Keep whichever holds up through a calm stretch (bigger average vs steadier wins).
- [ ] **Long straddle entry-filter re-test (study of 4 Nov):** over the past year, VIX, yesterday's range and the distance between the big call-OI and put-OI strikes predicted the size of the 09:16→13:00 move (about 2× between low and high fifths), but none predicted a move bigger than VIX already prices in (signs flip between halves of the year). On 36 real-price days, cheaper straddles (low VIX/IVP, straddle % of spot) and a less balanced CE/PE did better (about 70% vs 40% wins), too few days to trust. From the next deploy, each open logs `context` (VIX, VIX change, gap, straddle % of spot, CE/PE imbalance). Re-test with ~50 days of real prices: the only candidate is "skip when the straddle is expensive". Do not add a "trade only on high VIX" filter: it has the least support. Method: Spearman rank correlation of each feature against the 13:00 P&L, low half vs high half, checked separately on the first and second halves of the days; count every feature tried.
- [ ] **Long straddle sizing (study 6 Nov, 41 real days, per lot of 65):** cash in the trade ~₹16k/lot (the premium; margin is not the limit, risk is). Hold to 13:00: avg +₹1,667/day, worst day −₹4,141, worst week −₹10,771, max drawdown −₹12,860, longest losing run 6 days, 7 of 41 days ≤ −₹2,000. Leg −50%: +₹1,949/day, worst day −₹3,219, worst week −₹10,925. The −₹5,000 week stop (no new entry after it) *helped* on these days: 1 lot +₹72.8k vs +₹68.3k without it, worst week −₹6.3k vs −₹10.8k. The −₹2,000 day stop does nothing for a once-a-day book. The window was volatile (median morning move 111 pts vs ~68 over the year), so expect much less than ₹35k/month in calm months. **Proposal:** stay at 1 lot until ~20 more days including a calm stretch, live fills within ~₹300/day of paper, and cumulative live P&L positive; then 2 lots at most for 3 months. At 2 lots one day can lose ~₹8k (more than the week stop, which only blocks later entries). Before going to 2 lots: scale the risk manager's per-book worst-day kill threshold by lots (now a fixed ₹4,141).
- [ ] **Long straddle decision:** ~20 paper days, including a calm stretch. Backtest (real prices, 3 Sep–30 Oct, a volatile window): +₹1,767/day, t 2.8. Keep only if paper stays positive when the median morning move is near the yearly ~68 pts; it should lose in quiet weeks and win in busy ones.

## Open / optional

- [x] **Real prices rebuilt to 6 Nov (42 days; 6 Nov evening).** Local `data/px_real_merged.pkl`, `spot_real_merged.pkl`, `quotes_real.pkl` updated (backups in the scratchpad `px_new/`).
  - **Short straddle (v3 family) lost its edge on the new week.** Non-expiry days, hold to 15:14: 13:00 **+₹87/day, t 0.2** (was +₹533, t 2.2 to 30 Oct; 6 Nov alone −₹10,331), 13:30 +₹104, 14:00 +₹248 (t 1.55, worst −₹2,402), 14:15 +₹273 (t 1.75, worst −₹3,311), 14:30 +₹95. Every entry time lost on 6 Nov. v3's own rule (tp30/sl30) at 13:00: −₹24/day. **No afternoon-selling book is near a live decision**; keep all on paper and re-judge ~27 Nov.
  - **Long straddle held up:** 41 days, hold to 13:00 +₹1,667/day, 54% positive, worst −₹4,141, t 2.83 (halves +₹1,937 / +₹1,410). Leg −50%: +₹1,949, worst −₹3,219, t 3.02 (best risk-adjusted). tp20 +₹1,122; trail +10%/15% +₹1,094.
  - **Weekly strangles with the 3 Nov expiry added** (NSE files to 6 Nov): that week lost ~₹16k in every variant. 1,000 pts / 2 sessions now +₹383/wk, PF 2.44 / 1.96 (2019–23 / 2024–26), worst week −₹20.6k, still no losing year; 1.5 SD / 3 sessions (current book) +₹487/wk, PF 2.17 / 1.52.
- [ ] **Risk manager (`doc/trader-design.md`):** built locally in shadow mode (day stop −₹2,000, week stop −₹5,000, kill rule, event days; no alerts, by your choice). After deploy: a week of `data/risk_checks.jsonl`, then decide `ATLAS_LITE_RISK_ENFORCE=1`. SEBI: nothing to register (static IP whitelisted, <10 orders/s, market protection sent).

- [ ] Before any live book: build the live order path for that one book: Kite basket-margin and funds check before every entry, partial-fill handling, kill rule (back to paper after 3 stop-outs in a row or a loss beyond the backtest's worst day), and an explicit go-ahead.
- [ ] Add the depth-tape bid/ask builder (`~/px_build/build_quotes.py` on the server) to `scripts/build_px_real.py`, so buying-side backtests price at ask in / bid out by default.
- [ ] Delete `~/.cache/huggingface` (1.1 GB of Kronos weights) if the space is wanted.

## Reference

- **Retired 2 Nov:** ai_agent.
- **Running:** Long straddle (09:16 buy, 13:00 sell) and its +20% take-profit variant (from its deploy day), Scalper v3 (13:00 straddle sale), weekly strangle, weekly condor (5% wings), short iron condor, iron fly, agent (Scalper v2, paper, longs only).
- **Retired (flags off, ledgers kept):** 14:30 short straddle, theta cliff, ICT, combo, impulse fade, skew fade, long iron condor, VWAP long, factor paper.
- **Scalping research summary:** buying-side scalps all lost after costs (ML, Kronos, ICT, engulfing, 2-min EMA, v2, long straddles, the framework doc's compression breakout); intraday selling only pays when held into the last hour (Scalper v3).
- **Pricing lesson (31 Oct):** last-price backtests overstate breakout buying by ₹400–900/trade (exits on prints above the bid). At real bid/ask the compression breakout went from +₹512/+₹802 to −₹13/−₹102 per trade, while Scalper v3 improved (+₹774 → +₹969 on the same 15 days; 30 Oct sim = paper ₹695 exactly). Details in `doc/scalper-v3.md` and `doc/weekly-option-selling.md`.
- **Live switch:** `ATLAS_LITE_AGENT_LIVE_ALLOWED=0` on the server. Nothing goes live without an explicit decision.
