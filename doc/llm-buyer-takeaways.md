# LLM buyer: takeaways from the "NIFTY 1-minute options buying AI" spec (10 Nov 2026)

Reference read: an external design spec for an XGBoost + LLM + independent risk engine options buyer. This note maps
it onto our `llm_buyer` book and lists what to implement later. **Nothing here is implemented yet.**

## Already matching the spec

| Spec | Our book |
|---|---|
| Buy only; BUY_CE / BUY_PE / WAIT / HOLD / EXIT | Same actions, never sells options |
| Completed candles only, recent context | Closed 1-min candles only, last 15, opening range, 5/15-min moves |
| LLM never places orders or bypasses risk | Code owns all risk; the LLM only answers |
| Fixed, versioned system prompt | Prompt v2 lives in code; changes only by commit |
| Independent risk engine | Day stop −₹2,000, 2 entries (4 while up), profit lock, month stop −₹10,000, funds check, stale-feed block, 15:05 exit, 25 s call limit |
| No look-ahead | Replay fixed 10 Nov (start-of-minute snapshot, closed candles) |
| Backtest on executable prices | Replay fills at recorded ask (buy) / bid (sell) after answer time + 1 s, Kite charges |

## To implement later (in this order)

1. **Mandatory per-trade stop, set by code** (e.g. −30% of premium). Today only the day stop limits a trade.
2. **Spread / liquidity gate before every buy**: no entry when the ATM bid-ask spread is wider than a fixed limit.
3. **Bid/ask in the LLM's inputs**, plus one ITM and one OTM strike, so it sees the real entry cost.
4. **Structured output with reason codes** (e.g. `VWAP_CONFIRMATION`, `CONFLICTING_SIGNALS`) so decisions can be
   counted: which reasons win, which lose.
5. **Experiment log**: one row per model / prompt / rule test, failures included, with the replay days used.
6. **Report by regime**: trend up, trend down, sideways, high / low volatility, expiry day — not only the total.
7. **Futures inputs**: OI change and basis (today only futures OI vs its day high).

How to judge each change: the fixed replay (`scripts/llm_replay_day.py`) on ~10 recorded days, decision rule written
down before running, results by regime, logged as an experiment. One change at a time.

## Where our own evidence says be careful

- **XGBoost direction prediction (spec section 6).** Already tested on 7.5 years of NIFTY 1-min: 5–30 min direction
  was a coin flip walk-forward (AUC 0.48–0.50); move size was predictable but already priced into options. Revisit
  only with the TrueData option history, as a pre-registered test under the spec's own rules (chronological splits,
  purging, limited tuning) — never as a live component first.
- **More indicators (spec 3.2).** RSI, MACD, EMA cross, ORB, gap fade, prev-day break, SMC were all tested on 7.5
  years; none survived costs. Adding them to the prompt mostly adds noise.
- **"Same inputs → same decision."** LLM answers vary run to run even with fixed inputs; reduce it, don't rely on it.
  Another reason the code, not the LLM, owns stops and limits.
- **Replay honesty.** The 9 Nov replays looked profitable only because of a one-minute look-ahead (Opus +₹1,770 →
  −₹1,559 with real fills). Any result must come from the fixed replay at real ask/bid.

## Facts to keep in mind

- 9 Nov at real fills: Opus −₹1,559, Sonnet −₹769, GPT-4.1 live −₹747. No model has shown an edge yet.
- The day's three swings (10:45→11:38 +170 pts, 11:40→13:30 −145, 13:30→14:30 +107) were worth ₹5k–11k each to an
  ATM buyer even entering 5–15 min late; every model exited at the first pause.
- 7.5-year research: option buying has no edge on available data; expiry-week selling is the one that held.
