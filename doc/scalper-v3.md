# Scalper v3 — daily afternoon straddle sale (paper)

Status: **paper book built 2026-10-29**, on by default (`ATLAS_LITE_PAPER_SCALPER_V3=1`). Paper only, never Kite orders. Module `atlas_lite/paper_scalper_v3.py`, ledger `data/paper_scalper_v3.jsonl`, strategy `scalper_v3`.

Replaces the idea of Scalper v2 (buying ATM options on a trend), which lost on every test: −₹202 to −₹226 per trade over the Kite year, still −₹75 with a move-size filter.

## Rules

- **Daily, intraday.** 13:00 on every trading day that is not the weekly expiry (and not an NSE holiday): sell the ATM call and put, 1 lot. One trade a day; a first frame after 13:05 skips the day.
- **Take profit:** when buying back costs ≤ 70% of the entry credit (30% of the credit earned).
- **Stop:** when buying back costs ≥ 130% of the credit (a loss of 30% of the credit). Checked on every frame.
- **Exit:** otherwise close at 15:14. Flat every night.
- **Fills:** sell at bid, buy at ask; last price ∓ 1 pt with no book. If a quote blinks at the close, the last seen price + 1 pt. A position never squared off (engine down) is closed next session with P&L unknown.

## Evidence

| Test | Result |
|---|---|
| Kite year, walk-forward test days (160, estimated premiums) | +₹241/trade, PF 1.71, 79% win, worst day −₹5,945 |
| Real recorded premiums (16 days) | +₹256/trade, PF 1.68 |
| Existing 14:30 short straddle paper book, non-expiry days only | about +₹2,300 over 10 trades |
| Live class replayed over the backtest days | Kite year: 194/194 same exit, P&L identical; real days: 16/16 same exit |

Scripts: `scripts/bt_short_premium.py --only "straddle 13:00 tp30/sl30 not_expiry"`.

## Limits and what was ruled out

- **Expiry days skipped:** on expiry days real fills lost far more than the premium model predicted (two expiry losses of −₹3,857 and −₹4,291 in the 14:30 book).
- **Far wings do not pay intraday:** 2–5% wings cost ₹255–558 per trade (real NSE prices: slippage and charges on two extra legs), about the whole edge, and the 30% stop closes the trade long before a wing would help.
- **Calm-day ML filter:** raised profit per trade on the estimated year but halved the trades and hurt the 16 real days; not used.
- Research: the option-selling premium is earned mostly overnight, so the intraday edge may be thinner than the estimate.

## Running next to the 14:30 short straddle

The older `short_atm_straddle` paper book (sell at 14:30, hold to 15:14, no stop) can hold a position at the same time. On paper that is intended: two ledgers, no orders, and a direct comparison of the two rules. Only one naked straddle can go live on ₹2.5 lakh.

## Margin

Kite basket margin on 2026-10-29, 1 lot: this straddle (MIS) ₹1,93,794; weekly strangle ₹1,75,264; weekly condor (5% wings) ₹1,33,113; condor + this straddle together ₹3,26,907. With ₹2.5 lakh only one selling book can run live. Margins move with NIFTY and VIX: a live version must check Kite's basket margin and available funds before every entry and skip the trade if they don't cover it.

## Rollout

1. Paper for 2–3 months (~40–60 trades); compare each day with the backtest rule.
   `scripts/paper_vs_backtest.py --fetch` replays each paper day on that day's real recorded premiums and prints the gap.
2. Live, 1 lot, only after an explicit decision and a margin check.
