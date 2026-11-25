"""Coin-flip buyer: the LLM buyer's engine and rules with a coin in place of the LLM (paper only, never live).

Operator's call, 25 Nov 2026: a control for the LLM buyer. Everything except the decision is the LLM buyer's own code
(LlmBuyer): the day-direction rule picks the side, the stretch limit, the 10-minute pause after a loss, the NIFTY stop,
lot 1's trailing stop, the 40% giveback, the :30 check while red, the 15:08 exit, 2 lots, paper fills at bid/ask and
Kite charges. Where the LLM would be asked, the coin answers:
- flat: buy the allowed side with probability P_ENTRY (else WAIT);
- holding: EXIT with probability P_EXIT (else HOLD); at one question a minute that is a ~10-minute average hold,
  close to the LLM's own holds.
No LLM calls, no cost. If the LLM buyer cannot beat this book over the same days, the LLM adds nothing.
Exits differ on purpose (operator's call): the profit stops arm at +5% here (ARM_PCT), +10% in the LLM buyer.
Off with ATLAS_LITE_PAPER_COIN_BUYER=0.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.llm_buyer import LlmBuyer, QuoteSource

STRATEGY = "coin_buyer"
LEDGER_NAME = "paper_coin_buyer.jsonl"
NAME = "Coin buyer · LLM control"
P_ENTRY = 0.10
P_EXIT = 0.10


def paper_coin_buyer_enabled() -> bool:
    return os.environ.get("ATLAS_LITE_PAPER_COIN_BUYER", "1").strip() != "0"


class CoinDecider:
    """Answers like LlmDecider.decide, from a coin. `rng` is injectable for tests."""

    def __init__(self, rng: random.Random | None = None, p_entry: float = P_ENTRY, p_exit: float = P_EXIT) -> None:
        self.rng = rng or random.Random()
        self.p_entry, self.p_exit = float(p_entry), float(p_exit)

    def decide(self, state: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"provider": "coin", "model": "coin", "factors": [], "inputs": state}
        if state.get("position"):
            exit_now = self.rng.random() < self.p_exit
            return {**out, "action": "EXIT" if exit_now else "HOLD", "reason": "coin: exit" if exit_now else "coin: hold"}
        allowed = str(state.get("allowed_entry") or "")
        if allowed == "calls only":
            side = "BUY_CE"
        elif allowed == "puts only":
            side = "BUY_PE"
        elif allowed == "calls or puts":
            side = "BUY_CE" if self.rng.random() < 0.5 else "BUY_PE"
        else:  # "no new trades" (or unknown): the book would not ask; wait if it does
            return {**out, "action": "WAIT", "reason": "coin: no side allowed"}
        if self.rng.random() < self.p_entry:
            return {**out, "action": side, "reason": "coin: buy"}
        return {**out, "action": "WAIT", "reason": "coin: wait"}


# ---- the book ----
# Earlier profit protection (operator's call, 25 Nov 2026, this book only; the LLM buyer keeps +10%): lot 1's trailing
# stop and the giveback stop on the rest arm once the bid is ARM_PCT above the price paid. Prompted by Sol's 25 Nov
# 09:29 trade: best +9.2% (+Rs 2,958), never armed at +10%, closed -Rs 1,064. Not backtested yet.
ARM_PCT = 0.05
TRAIL_PCT = 0.02  # lot 1: sold this far below its highest bid since armed (as the LLM buyer)
GIVEBACK = 0.40  # the rest: sold after giving back this share of its best gain since armed (as the LLM buyer)


@dataclass
class CoinBuyer(LlmBuyer):
    """The LLM buyer's book with a coin deciding and the profit stops armed at ARM_PCT. Paper only: build it without
    an order client or live flag."""

    def live_enabled(self) -> bool:  # never live, whatever is passed in
        return False

    def _lot2_giveback(self, now: datetime, book: QuoteSource) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        bid = self._sell_px(book, pos.symbol)
        if bid is None:
            return None
        if not pos.lot2_high:
            if bid >= pos.fill * (1 + ARM_PCT):
                pos.lot2_high = bid  # armed
            return None
        pos.lot2_high = max(pos.lot2_high, bid)
        if bid > pos.fill + (pos.lot2_high - pos.fill) * (1 - GIVEBACK):
            return None
        return self._exit(now, book, "lot2_giveback")

    def _partial_take(self, now: datetime, book: QuoteSource) -> dict[str, Any] | None:
        pos = self.position
        if pos is None or pos.banked_gross or not pos.lot_size or pos.qty <= pos.lot_size:
            return None
        bid = self._sell_px(book, pos.symbol)
        if bid is None:
            return None
        if not pos.lot1_high:
            if bid >= pos.fill * (1 + ARM_PCT):
                pos.lot1_high = bid  # armed
            return None
        pos.lot1_high = max(pos.lot1_high, bid)
        if bid > pos.lot1_high * (1 - TRAIL_PCT):
            return None
        lot = pos.lot_size
        charges = round(float(kite_nfo_charges([(pos.fill, lot, "buy"), (bid, lot, "sell")])["total"]), 2)
        gross = round((bid - pos.fill) * lot, 2)
        ev = self._append({"event": "partial_close", "ts": now.isoformat(), "day": pos.day, "leg": pos.leg,
                           "symbol": pos.symbol, "qty": lot, "entry": pos.fill, "exit": round(bid, 2), "pnl_gross": gross,
                           "charges": charges, "pnl": round(gross - charges, 2), "reason": "lot_trailing_stop",
                           "lot_high": round(pos.lot1_high, 2)})
        if ev is None:
            return None
        pos.qty -= lot
        pos.banked_gross += gross
        pos.banked_charges += charges
        return ev
