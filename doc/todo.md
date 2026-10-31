# Trading to-do

Checked every trading day. Tick items as done (`[x]`), add new ones with a date. Paper only until an explicit live decision; ₹2.5 lakh margin fits one selling book live.

## Every trading day (after 15:20)

- [ ] **Scalper v3:** traded at 13:00 on a non-expiry day? Exit reason and P&L on the dashboard.
- [ ] **Gamma reading** logged with today's v3 entry (dashboard: "last entry gamma long/short").
- [ ] **Weekly strangle / condor:** 15:25 stop check logged; open P&L sensible.
- [ ] **ai_agent (shadow):** today's shadow trades and P&L.
- [ ] **Paper vs backtest:** `.venv/bin/python scripts/paper_vs_backtest.py --fetch`. A gap above ~₹300/trade, or an exit that differs from the backtest, gets investigated.
- [ ] **Server health:** dashboard loads; no repeated warnings.
- [ ] **NSE vs BSE gap watcher (research week, 2 – 6 Nov):** dashboard "NSE vs BSE gaps" line says live/final for today. It is the `arb-watch` service (`docker compose ps`; logs: `docker logs atlas-lite-arb`), restarted by Docker like the engine. Turn off with `ATLAS_LITE_ARB_WATCH=0` in the server `.env`, or remove the service when the research is done.

## Dated

### Mon 2 Nov 2026
- [ ] v3 trades at 13:00: **first entry with a gamma reading** (check the open event has `gamma`, not null).
- [ ] 15:40: ai_agent refit with the fixed learner. Expect `promoted: false` (`side_no_better_than_random`).
- [ ] **NSE vs BSE gap watcher** (read-only, `arb-watch` service): after 09:15, `docker logs atlas-lite-arb` should say `connected` (needs Monday's fresh Kite token).
- [ ] After 15:30: read `data/arb/summary-2026-11-02.json`, **small caps first, then mid, then large** (658 stocks: 515 small, 98 mid, 45 large; BSE groups A/B/X only): crossings, how many beat the ~0.083% cost, how long they lasted, shares available. Decide whether NSE–BSE arbitrage is worth anything, starting with small caps. Also `data/arb/paper-2026-11-02.jsonl` and the `paper` section per group: simulated IOC orders at 100/300/600 ms delay, both-filled vs one-leg (unwound at market) vs none, net paper P&L. The 300 ms row is the realistic one for Kite. Paper trades fire only when estimated net after costs is ≥ ₹500 (`--min-net`); with the ₹1 lakh size cap that needs a gap of about 0.58%. If none fire, check the "Est. net ≥ min" column before changing the cap or threshold.

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
- [ ] **Evening: NSE vs BSE research week analysis** (5 days in `data/arb/`). Find the best combination, small caps first: group × paper track (₹1/100/200/300/500/1,000) × order delay (100/300/600 ms) × stock × time of day. Judge on net after costs and failed legs, both-filled rate, and **day-by-day consistency** (a rule that only worked on one day is noise). Also replay the week under a **capital limit** (₹1 lakh one trade at a time, and ₹2.5 lakh up to two at once): first-come-first-served vs **highest estimated net first**; the paper file has every attempt's time and estimate, so no new recording is needed. Then paper-confirm the chosen rule for another week before any real order.
- [ ] **ai_agent decision:** its shadow trades vs Scalper v3 for the week. Retire (`ATLAS_LITE_PAPER_AI_AGENT=0`) unless it clearly beat v3.

### ~Fri 27 Nov 2026 (about 55 real days)
- [ ] Rebuild real prices (`paper_vs_backtest.py --fetch` does it) and recheck v3 on real premiums only: per-trade range should exclude zero before going live.
- [ ] **Gamma filter decision:** split v3's real trades by regime. Adopt only if positive-gamma days beat negative by a clear margin over 20+ trades each way.
- [ ] **Entry time:** 13:00 vs 14:30 on real prices (14:30 earned less, +₹256 vs +₹340, but its worst day was −₹277 vs −₹3,681).
- [ ] **Weekly condor live decision:** after 3–4 weekly paper cycles matching the backtest.

## Open / optional

- [ ] Optional: paper book "v3 at 14:30" (same rules, 14:30 entry) to compare with 13:00 on real fills.
- [ ] Before any live book: build the live order path for that one book: Kite basket-margin and funds check before every entry, partial-fill handling, kill rule (back to paper after 3 stop-outs in a row or a loss beyond the backtest's worst day), and an explicit go-ahead.
- [ ] Add the depth-tape bid/ask builder (`~/px_build/build_quotes.py` on the server) to `scripts/build_px_real.py`, so buying-side backtests price at ask in / bid out by default.
- [ ] Delete `~/.cache/huggingface` (1.1 GB of Kronos weights) if the space is wanted.

## Reference

- **Running:** Scalper v3 (13:00 straddle sale), weekly strangle, weekly condor (5% wings), short iron condor, iron fly, agent (Scalper v2, paper), ai_agent (shadow).
- **Retired (flags off, ledgers kept):** 14:30 short straddle, theta cliff, ICT, combo, impulse fade, skew fade, long iron condor, VWAP long, factor paper.
- **Scalping research summary:** buying-side scalps all lost after costs (ML, Kronos, ICT, engulfing, 2-min EMA, v2, long straddles, the framework doc's compression breakout); intraday selling only pays when held into the last hour (Scalper v3).
- **Pricing lesson (31 Oct):** last-price backtests overstate breakout buying by ₹400–900/trade (exits on prints above the bid). At real bid/ask the compression breakout went from +₹512/+₹802 to −₹13/−₹102 per trade, while Scalper v3 improved (+₹774 → +₹969 on the same 15 days; 30 Oct sim = paper ₹695 exactly). Details in `doc/scalper-v3.md` and `doc/weekly-option-selling.md`.
- **Live switch:** `ATLAS_LITE_AGENT_LIVE_ALLOWED=0` on the server. Nothing goes live without an explicit decision.
