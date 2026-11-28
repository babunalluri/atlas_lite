"""14:00 stretch watch book: follow a strong morning into the last hour. Paper only, never live, no LLM. 1 lot ITM option.

Operator's call, 28 Nov 2026: a watch book for the best buying lead of the 28 Nov push (scratchpad stretch_spread.py,
stretch_real.py). NIFTY >= +-0.75% from the 09:15 open at 14:00 kept going to 15:00 by +13.9 / +14.6 NIFTY pts
(2019-23 / 2024-26, t 3.6 / 2.1, won 58% / 61%). With a 100-pt ITM option priced from VIX (buyer-kind model):
+Rs 360 / +422 a trade (t 1.9 / 1.3). On 20 real days (TrueData + recorder) about break-even: those days followed
through only ~+6 pts. The 0.75% level was chosen after an earlier 0.5% search, so this is a watch book to collect real
evidence, not a candidate for money.

Rules:
- At 14:01 (the 14:00 candle closed): NIFTY >= +0.75% above today's 09:15 open -> buy the CE 100 pts in the money
  (ATM - 100) at the ask; <= -0.75% -> buy the PE 100 pts in the money (ATM + 100). Otherwise no trade.
- Exit at 15:00 at the bid. No stop. One trade a day.
- Guards: only on a day NIFTY has traded (today's candles exist); entries 14:01-14:05 only (a missing quote retries
  until 14:05); a restart after 14:05 never enters late.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from atlas_lite.paper_g2t10 import PaperG2T10, QuoteSource

STRATEGY = "stretch_1400"
LEDGER_NAME = "paper_stretch_1400.jsonl"
NAME = "14:00 stretch watch · no LLM"
SIGNAL_BAR, ENTRY_FROM, ENTRY_UNTIL, EXIT_AT = "14:00", "14:01", "14:05", "15:00"
STRETCH_PCT = 0.75
ITM_PTS = 100


def paper_stretch_enabled() -> bool:
    return os.environ.get("ATLAS_LITE_PAPER_STRETCH", "1").strip().lower() in ("1", "true", "yes")


def itm_symbol(atm_symbol: str, atm: int, leg: str) -> tuple[str, int] | None:
    """The same expiry's option ITM_PTS in the money, from the ATM symbol (e.g. ...D0125600CE -> ...D0125500CE)."""
    suffix = f"{atm}{leg.upper()}"
    if not atm_symbol or not atm_symbol.endswith(suffix):
        return None
    strike = atm - ITM_PTS if leg == "ce" else atm + ITM_PTS
    return atm_symbol[: -len(suffix)] + f"{strike}{leg.upper()}", strike


@dataclass
class PaperStretch(PaperG2T10):
    """G2T10's ledger, restart, fills and square-off; the 14:00 stretch decides. Paper only (never Kite orders)."""

    strategy: str = STRATEGY
    name: str = NAME
    decided: str = ""
    last_signal: dict[str, Any] | None = None

    def _new_day(self, today: str) -> None:
        fresh = self.day != today
        super()._new_day(today)
        if fresh and self.path.is_file():  # a restart remembers today's skip (opens are restored by G2T10)
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    if f'"day":"{today}"' in line and '"event":"skip"' in line:
                        self.decided = today
            except OSError:
                pass

    def decision_due(self, now: datetime) -> bool:
        hm = now.strftime("%H:%M")
        return hm != self.done_minute and now.second >= 3 and ENTRY_FROM <= hm < EXIT_AT

    def on_frame(self, *, now: datetime, feed: dict[str, Any], book: QuoteSource) -> dict[str, Any] | None:
        today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        self._new_day(today)
        if self.position is not None and hm >= EXIT_AT:
            return self._close(now, book, "exit_1500")
        if self.position is not None or self.decided == today or self.trades:
            return None
        if not self.decision_due(now) or not feed or feed.get("error"):
            return None
        self.done_minute = hm
        bars = [b for b in (feed.get("bars_today") or []) if str(b.get("t") or "").startswith(today)]
        if not bars:
            return None  # NIFTY has not traded today (holiday): never trade at stale quotes, log nothing
        if hm > ENTRY_UNTIL:  # a restart after the entry window: no late entry
            self.decided = today
            self._append({"event": "skip", "ts": now.isoformat(), "day": today, "reason": "entry window 14:01-14:05 missed"})
            return None
        first = next((b for b in bars if str(b["t"])[11:16] == "09:15"), None)
        sig = next((b for b in bars if str(b["t"])[11:16] == SIGNAL_BAR), None)
        if first is None or sig is None:
            self.decided = today
            self._append({"event": "skip", "ts": now.isoformat(), "day": today, "reason": "missing the 09:15 or 14:00 candle"})
            return None
        move = round((float(sig["c"]) / float(first["o"]) - 1) * 100, 3)
        self.last_signal = {"day": today, "move_pct": move, "open": first["o"], "at_1400": sig["c"]}
        if abs(move) < STRETCH_PCT:
            self.decided = today
            self._append({"event": "skip", "ts": now.isoformat(), "day": today, "move_pct": move,
                          "reason": f"NIFTY {move:+.2f}% from the open at 14:00 (needs +-{STRETCH_PCT}%)"})
            return None
        leg = "ce" if move > 0 else "pe"
        atm = int(feed.get("atm") or 0)
        got = itm_symbol(str(feed.get(f"{leg}_symbol") or ""), atm, leg)
        px = self._buy_px(book, got[0]) if got else None
        if not got or not px:
            self.last_reject = "no_itm_quote"
            if hm >= ENTRY_UNTIL:
                self.decided = today
                self._append({"event": "skip", "ts": now.isoformat(), "day": today, "move_pct": move,
                              "reason": "no ITM quote by 14:05"})
            return None
        symbol, strike = got
        qty = int(self.lot_size)
        ev = self._append({"event": "open", "ts": now.isoformat(), "day": today, "leg": leg, "symbol": symbol,
                           "strike": strike, "qty": qty, "fill": round(px, 2), "entry": round(px, 2), "side": "long",
                           "move_pct": move,
                           "reason": f"NIFTY {move:+.2f}% from the open at 14:00: follow it ({leg.upper()} {ITM_PTS} ITM)"})
        if ev is None:
            return None
        self.decided = today
        self.trades += 1
        self.position = {"leg": leg, "symbol": symbol, "strike": strike, "qty": qty, "fill": round(px, 2),
                         "opened_at": now.isoformat()}
        return ev

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        snap = super().snapshot(book)
        snap.pop("entries_stopped", None)
        snap.pop("losses_today", None)
        snap["last_signal"] = self.last_signal
        snap["window"] = (f"{ENTRY_FROM}: NIFTY >= +-{STRETCH_PCT}% from the 09:15 open at 14:00 -> buy the "
                          f"{ITM_PTS}-pt ITM option that way · exit {EXIT_AT} · no stop · paper watch")
        return snap
