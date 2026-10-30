# Weekly NIFTY option selling (paper)

Status: **paper books built 2026-10-28**, on by default (`ATLAS_LITE_PAPER_WEEKLY_SELL=1`). Paper only, never Kite orders.

Two books, same short strikes, run side by side:

| Book | Structure | Ledger |
|---|---|---|
| `weekly_strangle` | Sell put + call, no wings | `data/paper_weekly_strangle.jsonl` |
| `weekly_condor5` | Same, plus wings bought 5% of F beyond each short strike | `data/paper_weekly_condor5.jsonl` |

Research behind it: [NIFTY Options Evidence Review](https://claude.ai/artifact/AmfvRUobJp5XoUJj6WJBGk) (SEBI studies, academic papers, Quantsapp backtests, all sources by trust tier).

## Rules

- **Entry:** 15:25, three trading sessions before the weekly expiry (normally Thursday for a Tuesday expiry; holidays counted with `NSE_HOLIDAYS`).
- **Forward and IV:** F = ATM strike + ATM call − ATM put; ATM IV from Black-76.
- **Short strikes:** put at or below F·e^(−1.5·IV·√(3/252)), call at or above F·e^(+1.5·IV·√(3/252)), from strikes with a live quote. Same function as the backtest (`select_strikes`).
- **Stop:** once a day at 15:25, from the session after entry until the day before expiry: close everything if buying the position back costs ≥ 3× the entry credit (last traded prices).
- **Exit:** otherwise close at 15:29 on expiry day. If the engine was down through expiry, the next frame closes at the last seen marks (`expiry_missed`, `pnl_known: false`).
- **Fills:** sell at bid, buy at ask; last price ∓ 1 pt when there is no book. Kite charges at current rates. 1 lot.

## Backtest: real NSE prices, Feb 2019 – Oct 2026

Data: NSE F&O bhavcopy for every trading day since NIFTY weeklies began (1,901 days), nearest two expiries, via `scripts/nse_nifty_options_history.py` → `data/nifty_opt_eod.pkl`. Backtest: `scripts/bt_iron_condor_eod.py`. Per 65 units, Kite charges, slippage per leg per side.

| Structure | Slip 1 pt: total | PF | 2019–23 / 2024–26 PF | Worst week | Max DD | Slip 2 pt: total |
|---|---|---|---|---|---|---|
| Strangle (no wings) | +₹209,525 | 1.95 | 2.17 / 1.73 | −₹33,739 | −₹41,683 | +₹155,668 |
| Condor, 5% wings | +₹85,795 | 1.52 | 1.40 / 1.71 | −₹21,746 | −₹27,179 | −₹2,853 |
| Condor, 2% wings (published rule) | +₹28,647 | 1.16 | 1.08 / 1.25 | −₹22,112 | −₹27,656 | — |

- The strangle never had a losing year (worst 2024: +₹5,093).
- March 2020: 9 of 11 expiries traded (two stop-outs); the two skipped weeks had no strike far enough out and would have won.
- A 45-setting grid picked on 2019–2023 did worse on 2024–2026 than the untuned 1.5 SD rule. Entering 5 sessions before expiry lost money. About 60 settings and structures were tried in total.
- Expiry-day trap found while building it: on expiry day NSE writes NIFTY's final settlement level into every option's settlement-price field. The backtest settles at intrinsic against that level.

## Limits

- End-of-day data: entries and stops at the close, so a crash within a day is seen at 15:30. The paper book has the same once-a-day stop on purpose, so paper and backtest compare like for like.
- Closing prices stand in for bid/ask; the condor's result depends on fills (flat at 2 pt slippage).
- Margin (check with Kite basket margin): strangle about ₹2.5–3 lakh per lot, uncapped gap risk; 5%-wing condor caps the loss near ₹58k per lot and needs less.
- Weekly wins average ~₹1.2k, losses ~₹6.9k: it works only at a ~92% win rate.

## Rollout

1. Paper both books for **3 months (~12 expiries)**. Compare each week's paper P&L with what the backtest rules give for that week.
   `scripts/paper_vs_backtest.py --fetch` prints both side by side once the expiry's bhavcopy is downloaded (`scripts/nse_nifty_options_history.py --start <entry day>`).
2. Live, 1 lot, only after an explicit decision. Strangle needs ~₹3 lakh margin; condor fits ₹1–1.5 lakh.
3. Kill rule for live: back to paper after 3 consecutive stop-outs or a loss beyond the backtest's worst week.
