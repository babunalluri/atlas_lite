# One trader, one risk manager (design, 6 Nov 2026)

Status: **risk manager built locally in shadow mode** (`atlas_lite/risk_manager.py`, asked before the long
straddle's live entry; not deployed). The rest is design. Each phase is deployed only on your go.

Operator answers (6 Nov): no alerts; account day stop −₹2,000, week stop −₹5,000 (raised the same evening to −₹8,000 / −₹15,000 for the expiry-week selling plan); SEBI rules checked by
me (below); static IP already whitelisted.

## Goal

The system runs only the strategies that have passed real-price backtests and paper trading. It trades
them at fixed times with fixed rules, inside one risk budget for the whole account, and tells you at
once when something needs a human.

What it will not do:
- **No AI picks direction or decides to trade.** ai_agent, the direction ML, Kronos and ICT all scored
  coin-flip on new data. The AI part (below) reads and reports only.
- **No strategy goes live without passing the go-live bar** in `doc/todo.md`, plus your explicit tick.

## What already exists and is reused

| Piece | Where | Reused for |
|---|---|---|
| Live feed, quotes, option chain | `feed_engine.py`, `kite_ws.py` | every book |
| Paper books with bid/ask fills, own ledgers | `paper_*.py` | paper side of every strategy, unchanged |
| Tagged-order live layer: tag written before send, holdings from own fills only, hand trades flagged, stop needs confirmation | `live_straddle.py` | the order layer for every live book (generalised to N legs) |
| Day-only live switch: host env lock + dashboard tick stamped with the day | `day_live_flag.py` | one switch per live book |
| Kite basket margin and available funds | `kite_orders.py` (`basket_margin_required`, `equity_available`) | pre-trade margin check |
| Day loss stop with persisted state | `arb_live.py` (`ArbLiveDayState`) | account-level day stop |
| Per-book pause gates | `agent_gates.py` | pause and kill per book |

## The three parts

### 1. Trader (strategy registry)

One file, `data/live_books.json`, lists the books allowed to trade live, edited only by you:

```json
{"long_straddle": {"lots": 1, "priority": 1},
 "scalper_v3":    {"lots": 1, "priority": 2}}
```

- A book trades live only if all three hold: it is in this file, the host env lock is on, and today's
  dashboard tick is on. Otherwise it stays paper, exactly as today.
- Each live book keeps its paper twin running, so live fills can be compared with paper every day.
- Selling books (v3, weekly strangle) need a new order rule: **buy any wings first, then sell**. Kite
  charges less margin for a hedged position, and the position is never naked if the second order fails.

### 2. Risk manager (account level, checked before every entry)

| Check | Rule (proposal) | Why |
|---|---|---|
| Margin | Kite basket margin for the new orders ≤ funds available − ₹20k buffer | ₹2.5L is shared; a rejected second leg leaves a naked position |
| Priority | When two books want margin, the higher priority wins; the other stays paper that day | Two selling books together exceed ₹2.5L |
| Account day stop | Live closed P&L today across books (arb included) ≤ **−₹8,000** → no new live entries; open trades keep their own exits | One bad day must not become three |
| Account week stop | ≤ **−₹15,000** since Monday → no new live entries until next week | Your limit |
| Book kill rule | 3 stop-outs in a row, or one loss beyond the backtest's worst day → that book back to paper | From `doc/todo.md`; a broken edge shows here first |
| Event days | No new selling entries on RBI policy (4 Dec, 5 Feb), the session after a US FOMC decision (10 Dec, 28 Jan), Union Budget (1 Feb); `data/event_days.json` adds more | Gap risk |
| Cut-off | No new orders after 15:10; MIS square-off 15:14 is Zerodha's | Same as the live straddle |

Every refusal is written to the ledger and shown on the dashboard ("v3 stayed paper: margin").

### 3. Watchdog

Dropped (no alerts wanted). The dashboard stays the place to look; the risk manager writes every check
to `data/risk_checks.jsonl`, and refusals show in the book's ledger (`live_risk_block`).

### AI assistant (reporting only)

After the close, a script reads the day's ledgers and writes a plain summary:
- **What happened:** each book's trades and P&L, live vs paper slippage, and the NIFTY move.
- **What looks wrong:** paper and live disagree, fills worse than usual, or a book drifting outside its backtest range.

It can use Claude for the wording. It has no order permissions and no write access to any switch.

## Rollout

| Phase | What | Gate to the next phase |
|---|---|---|
| 1 (built, not deployed) | Risk manager in **shadow** for the long straddle: logs what it would block, blocks nothing | One week; you agree with every would-block |
| 2 | `ATLAS_LITE_RISK_ENFORCE=1` on the server: a "no" keeps that day's trade on paper | One week clean |
| 3 | Order layer generalised to N legs (wings first), v3 or the weekly strangle on paper through it | Paper fills through the new layer match the old paper book |
| 4 | A second live book, if one passes the go-live bar (~27 Nov review), asking the same risk manager | Your tick |

Each phase is its own commit and deploy, on your go.

## SEBI retail algo rules (checked 6 Nov 2026)

Since 1 Apr 2026 (NSE circular, as applied by Zerodha):
- **Static IP:** orders through Kite Connect are accepted only from the IP whitelisted on the developer
  account (data and order book work from any IP). Ours is whitelisted; live orders on 4–5 Nov went through.
- **Rate:** up to 10 orders a second per client ID needs no exchange registration or algo ID. Above that
  Kite returns 429 and the strategy must be registered. Our books send 2–4 orders a trade, far below.
- **Market orders:** API market and SL-M orders must carry `market_protection` (0 is rejected; −1 = Zerodha's
  automatic limit). `kite_orders.py` already sends it.
- Exchange-assigned algo IDs/tagging apply only to registered (>10 orders/s) strategies.
Nothing to register for our use. Sharing the IP outside immediate family can suspend the API account.

## Still open

- Retire arb-watch and cf-watch: at the next deploy, server `.env` gets `ATLAS_LITE_ARB_WATCH=0`,
  `ATLAS_LITE_ARB_LIVE_ALLOWED=0`, `ATLAS_LITE_CF_WATCH=0` (backup first). cf-watch would stop after
  13 Nov on its own; arb-watch is set to run to 31 Dec.
- `agent_paper` live (off since 3 Nov) does not ask the risk manager yet; add it before it is ever ticked again.
