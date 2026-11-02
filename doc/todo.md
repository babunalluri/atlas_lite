# Trading to-do

Checked every trading day. Tick items as done (`[x]`), add new ones with a date. Paper only until an explicit live decision; ₹2.5 lakh margin fits one selling book live.

## Every trading day (after 15:20)

- [ ] **Scalper v3:** traded at 13:00 on a non-expiry day? Exit reason and P&L on the dashboard.
- [ ] **Long straddle (from its deploy day):** bought at 09:16, sold at 13:00? P&L, and the NIFTY move 09:16→13:00 (break-even ~97 pts in the backtest). Expiry days are traded and marked `expiry_day`; `time_late` exits (engine down at 13:00) are left out of the analysis.
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
- [ ] **Evening: NSE vs BSE research week analysis** (5 days in `data/arb/`). Find the best combination, small caps first: group × paper track (₹1/100/200/300/500/1,000) × order delay (100/300/600 ms) × stock × time of day. Compare **first sighting vs confirmed** (summary `paper` vs `paper_confirmed`; from the day the confirmation build was deployed). True first sightings are rows with `crossing_ticks` 1; about a third of Monday's ₹100+ "first" attempts fired on a later tick (gap grew past the threshold) and filled both legs far more often (20% vs 3.6%). Monday rows have no `crossing_ticks`: treat `seen` within 50 ms of `crossing_root` as tick 1. **Key test: the 09:15–09:20 window.** On 2 Nov it was the only window that paid (₹100+ tracks: 256 attempts, 52 both, +₹4,354; 09:20–10:00 −₹13,969; after 10:00 −₹8,168). Check it is positive on every day before considering a narrowed rule (opening window, confirmed, ₹100+). Judge on net after costs and failed legs, both-filled rate, and **day-by-day consistency** (a rule that only worked on one day is noise). Also replay the week under a **capital limit** (₹1 lakh one trade at a time, and ₹2.5 lakh up to two at once): first-come-first-served vs **highest estimated net first**; the paper file has every attempt's time and estimate, so no new recording is needed. Then paper-confirm the chosen rule for another week before any real order.
- [ ] **Evening: pre-open basis week** (`data/pob/`, from the deploy day through 6 Nov). Confirmed both-fill rate vs CF’s midday book; net after one-leg unwinds; whether 09:15–09:30 alone pays. Keep only if both% and day-consistency beat the all-day CF watcher.
- [x] **ai_agent decision:** retired 2 Nov (shadow −₹2,450 over 42 trades, all from morning shorts; side choice never beat random). `ATLAS_LITE_PAPER_AI_AGENT=0`; ledger and model kept.

### ~Fri 27 Nov 2026 (about 55 real days)
- [ ] Rebuild real prices (`paper_vs_backtest.py --fetch` does it) and recheck v3 on real premiums only: per-trade range should exclude zero before going live.
- [ ] **Gamma filter decision:** split v3's real trades by regime. Adopt only if positive-gamma days beat negative by a clear margin over 20+ trades each way.
- [ ] **Entry time:** 13:00 vs 14:30 on real prices (14:30 earned less, +₹256 vs +₹340, but its worst day was −₹277 vs −₹3,681).
- [ ] **Weekly condor live decision:** after 3–4 weekly paper cycles matching the backtest.
- [ ] **Agent (scalper) decision, with the long straddle:** from 2 Nov its paper fills are at bid/ask (the close logs `trigger_px` next to the fill); it trades longs only (`ATLAS_LITE_AGENT_ALLOW_SHORT=0`, off since 19 Oct). Before that, re-priced at bid/ask, its longs made +₹11.1k over 5–30 Oct (t 1.5, two days carry it, mostly before 11:00). If the long straddle (no direction call) earns as much, agent's side-picking adds nothing: retire it.
- [ ] **Long straddle decision:** ~20 paper days, including a calm stretch. Backtest (real prices, 3 Sep–30 Oct, a volatile window): +₹1,767/day, t 2.8. Keep only if paper stays positive when the median morning move is near the yearly ~68 pts; it should lose in quiet weeks and win in busy ones.

## Open / optional

- [ ] Optional: paper book "v3 at 14:30" (same rules, 14:30 entry) to compare with 13:00 on real fills.
- [ ] Before any live book: build the live order path for that one book: Kite basket-margin and funds check before every entry, partial-fill handling, kill rule (back to paper after 3 stop-outs in a row or a loss beyond the backtest's worst day), and an explicit go-ahead.
- [ ] Add the depth-tape bid/ask builder (`~/px_build/build_quotes.py` on the server) to `scripts/build_px_real.py`, so buying-side backtests price at ask in / bid out by default.
- [ ] Delete `~/.cache/huggingface` (1.1 GB of Kronos weights) if the space is wanted.

## Reference

- **Retired 2 Nov:** ai_agent.
- **Running:** Long straddle (09:16 buy, 13:00 sell; from its deploy day), Scalper v3 (13:00 straddle sale), weekly strangle, weekly condor (5% wings), short iron condor, iron fly, agent (Scalper v2, paper, longs only).
- **Retired (flags off, ledgers kept):** 14:30 short straddle, theta cliff, ICT, combo, impulse fade, skew fade, long iron condor, VWAP long, factor paper.
- **Scalping research summary:** buying-side scalps all lost after costs (ML, Kronos, ICT, engulfing, 2-min EMA, v2, long straddles, the framework doc's compression breakout); intraday selling only pays when held into the last hour (Scalper v3).
- **Pricing lesson (31 Oct):** last-price backtests overstate breakout buying by ₹400–900/trade (exits on prints above the bid). At real bid/ask the compression breakout went from +₹512/+₹802 to −₹13/−₹102 per trade, while Scalper v3 improved (+₹774 → +₹969 on the same 15 days; 30 Oct sim = paper ₹695 exactly). Details in `doc/scalper-v3.md` and `doc/weekly-option-selling.md`.
- **Live switch:** `ATLAS_LITE_AGENT_LIVE_ALLOWED=0` on the server. Nothing goes live without an explicit decision.
