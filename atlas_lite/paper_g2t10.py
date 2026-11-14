"""G2T10: the coded no-LLM baseline for the LLM buyer. Paper only, never Kite orders; 1 lot ATM NIFTY CE or PE.

Prompt v3 written as rules (pre-registered 12 Nov 2026), plus the two guards picked on 13 Nov (scratchpad
edge_A_chopguard.py): entries only from 10:00, and no new entries after the day's second losing trade. On 29 recorded
days (5 Oct - 13 Nov) it made +Rs 26,923 (t 1.4), worst day -Rs 2,502, chop days about Rs 0; the 7.5-year spot check of
the same rules says the long-run gross edge (~2.3 pts a day) is below option costs. It runs as the yardstick the LLM
book must beat on the same days, not as a candidate for money.

Once a minute, from today's closed one-minute NIFTY candles (the same feed the LLM book sees):
- Levels: the day's high / low before the last candle, and the opening range (09:15-09:29) from 09:31.
- Entry (10:00-15:00): the last candle closes beyond a level the candle before closed inside, and the 5-minute move
  agrees. Within 0.25% of the level: buy the ATM CE (break up) / PE (break down) at the ask. Further away: remember the
  break for 30 minutes and buy the first candle that touches the level and closes back on the break side.
- Exit: a candle closes back through the traded level, else 15:08, sold at the bid.
- One trade at a time; no new entries after 2 losing trades in the day. No day stop or entry cap (paper, no limits).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger
from atlas_lite.metrics import quote_ask, quote_bid, quote_ltp

STRATEGY = "g2t10"
LEDGER_NAME = "paper_g2t10.jsonl"
FIRST_ENTRY, LAST_ENTRY, EXIT_AT = "10:00", "15:00", "15:08"
CHASE = 0.0025  # a break further than this from its level waits for the pullback
PULLBACK_MIN = 30  # how long a too-far break is remembered
MAX_LOSSES = 2
SLIP_PTS = 1.0
DEFAULT_LOT_SIZE = 65


def paper_g2t10_enabled() -> bool:
    return os.environ.get("ATLAS_LITE_PAPER_G2T10", "1").strip().lower() in ("1", "true", "yes")


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def _mins(hm: str) -> int:
    return int(hm[:2]) * 60 + int(hm[3:5])


@dataclass
class PaperG2T10:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    strategy: str = STRATEGY
    name: str = "G2T10 · coded baseline"
    day: str = ""
    position: dict[str, Any] | None = None
    losses: int = 0
    trades: int = 0
    realized: float = 0.0
    pending: dict[tuple[str, str], tuple[float, int]] = field(default_factory=dict)
    done_minute: str = ""
    last_reject: str | None = None
    last_event: dict[str, Any] | None = None
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_g2t10"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    # ---- ledger ----

    def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event.setdefault("strategy", self.strategy)
        event.setdefault("live_orders", False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError as exc:
            self._log.warning("g2t10 ledger write failed: %s", exc)
            return None
        self.last_event = event
        return event

    def _new_day(self, today: str) -> None:
        """Reset the day, then restore today's open trade, losses and P&L from the ledger (restart-safe). A trade
        left open from an earlier day (never squared off) is closed with an unknown P&L, never priced today."""
        if self.day == today:
            return
        self.day, self.position, self.losses, self.trades, self.realized = today, None, 0, 0, 0.0
        self.pending, self.done_minute = {}, ""
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        stale: dict[str, Any] | None = None
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("day") != today:
                if ev.get("event") == "open":
                    stale = ev
                elif ev.get("event") == "close":
                    stale = None
                continue
            if ev.get("event") == "open":
                self.trades += 1
                self.position = {k: ev.get(k) for k in ("leg", "symbol", "strike", "qty", "fill", "entry_level",
                                                        "entry_level_kind")} | {"opened_at": ev.get("ts")}
            elif ev.get("event") == "close":
                pnl = float(ev.get("pnl") or 0.0)
                self.realized += pnl
                self.losses += 1 if pnl <= 0 else 0
                self.position = None
        if stale is not None and str(stale.get("day") or "") < today:
            self._append({"event": "close", "ts": f"{today}T00:00:00+05:30", "day": stale.get("day"),
                          "leg": stale.get("leg"), "symbol": stale.get("symbol"), "strike": stale.get("strike"),
                          "qty": stale.get("qty"), "entry": stale.get("fill"), "exit": None, "pnl": None,
                          "pnl_known": False, "reason": "missed_square_off", "side": "long"})

    # ---- prices ----

    @staticmethod
    def _buy_px(book: QuoteSource, symbol: str) -> float | None:
        row = book.get(symbol)
        ask, ltp = quote_ask(row), quote_ltp(row)
        return float(ask) if ask else (float(ltp) + SLIP_PTS if ltp else None)

    @staticmethod
    def _sell_px(book: QuoteSource, symbol: str) -> float | None:
        row = book.get(symbol)
        bid, ltp = quote_bid(row), quote_ltp(row)
        return float(bid) if bid else (max(0.05, float(ltp) - SLIP_PTS) if ltp else None)

    # ---- trades ----

    def _close(self, now: datetime, book: QuoteSource, why: str) -> dict[str, Any] | None:
        pos = self.position
        assert pos is not None
        px = self._sell_px(book, str(pos["symbol"]))
        if px is None:
            self.last_reject = "no_quote_for_exit"
            return None
        qty, fill = int(pos["qty"]), float(pos["fill"])
        charges = round(float(kite_nfo_charges([(fill, qty, "buy"), (px, qty, "sell")])["total"]), 2)
        gross = round((px - fill) * qty, 2)
        ev = self._append({"event": "close", "ts": now.isoformat(), "day": self.day, "leg": pos["leg"],
                           "symbol": pos["symbol"], "strike": pos["strike"], "qty": qty, "entry": fill,
                           "exit": round(px, 2), "pnl_gross": gross, "charges": charges,
                           "pnl": round(gross - charges, 2), "reason": why, "side": "long"})
        if ev is None:
            return None
        self.position = None
        self.realized = round(self.realized + ev["pnl"], 2)
        self.losses += 1 if ev["pnl"] <= 0 else 0
        return ev

    def _open(self, now: datetime, feed: dict[str, Any], book: QuoteSource, kind: str, level: float,
              leg: str) -> dict[str, Any] | None:
        symbol, strike = feed.get(f"{leg}_symbol"), int(feed.get("atm") or 0)
        if not symbol or not strike:
            self.last_reject = "no_atm_symbol"
            return None
        px = self._buy_px(book, str(symbol))
        if not px:
            self.last_reject = "no_quote"
            return None
        qty = int(self.lot_size)
        ev = self._append({"event": "open", "ts": now.isoformat(), "day": self.day, "leg": leg, "symbol": symbol,
                           "strike": strike, "qty": qty, "fill": round(px, 2), "entry": round(px, 2), "side": "long",
                           "entry_level": round(level, 2), "entry_level_kind": kind,
                           "reason": f"close beyond {kind} {level:.1f}, 5-min move agrees"})
        if ev is None:
            return None
        self.trades += 1
        self.position = {"leg": leg, "symbol": symbol, "strike": strike, "qty": qty, "fill": round(px, 2),
                         "entry_level": round(level, 2), "entry_level_kind": kind, "opened_at": now.isoformat()}
        self.last_reject = None
        return ev

    # ---- every frame ----

    def decision_due(self, now: datetime) -> bool:
        """True when this minute's decision has not run yet (the engine builds the feed only then)."""
        hm = now.strftime("%H:%M")
        return hm != self.done_minute and now.second >= 3 and "09:16" <= hm < EXIT_AT

    def on_frame(self, *, now: datetime, feed: dict[str, Any], book: QuoteSource) -> dict[str, Any] | None:
        """Called every second; decides once a minute (from second 3, like the backtest's fills)."""
        today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        self._new_day(today)
        if self.position is not None and hm >= EXIT_AT:
            return self._close(now, book, "square_off")
        if not self.decision_due(now):
            return None
        if not feed or feed.get("error"):
            return None
        bars = [b for b in (feed.get("bars_today") or [])
                if isinstance(b, dict) and b.get("c") is not None and str(b.get("t") or "").startswith(today)
                and str(b["t"])[11:16] < hm]  # closed candles only
        self.done_minute = hm
        if len(bars) < 8:
            return None
        last, before = bars[-1], bars[-2]
        c, pc = float(last["c"]), float(before["c"])
        if self.position is not None:
            lvl = float(self.position["entry_level"])
            back = c < lvl if self.position["leg"] == "ce" else c > lvl
            return self._close(now, book, "close_back") if back else None
        if hm < FIRST_ENTRY or hm > LAST_ENTRY:
            return None
        if self.losses >= MAX_LOSSES:
            self.last_reject = "two_losses_today"
            return None
        ref = bars[:-1]
        levels = [("day_high", max(float(b["h"]) for b in ref), "ce"), ("day_low", min(float(b["l"]) for b in ref), "pe")]
        orb = [b for b in bars if "09:15" <= str(b["t"])[11:16] <= "09:29"]
        if hm >= "09:31" and orb:
            levels += [("or_high", max(float(b["h"]) for b in orb), "ce"), ("or_low", min(float(b["l"]) for b in orb), "pe")]
        m5 = c - float(bars[-6]["c"])
        now_min = _mins(hm)
        sig = None
        for kind, lvl, leg in levels:
            up = leg == "ce"
            broke = (c > lvl >= pc) if up else (c < lvl <= pc)
            if broke and (m5 > 0 if up else m5 < 0):
                if abs(c - lvl) <= CHASE * c:
                    sig = (kind, lvl, leg)
                    break
                self.pending[(kind, leg)] = (lvl, now_min)  # too far: wait for the first pullback
        if sig is None:
            for (kind, leg), (lvl, m0) in list(self.pending.items()):
                if now_min - m0 > PULLBACK_MIN:
                    self.pending.pop((kind, leg))
                    continue
                up = leg == "ce"
                touch = (float(last["l"]) <= lvl < c) if up else (float(last["h"]) >= lvl > c)
                if touch:
                    sig = (kind, lvl, leg)
                    self.pending.pop((kind, leg))
                    break
        if sig is None:
            return None
        return self._open(now, feed, book, *sig)

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        pos = None
        if self.position is not None:
            pos = dict(self.position)
            px = self._sell_px(book, str(pos["symbol"])) if book is not None else None
            pos["bid"] = px
            pos["open_pnl"] = round((px - float(pos["fill"])) * int(pos["qty"])) if px is not None else None
        return {"ok": True, "mode": "paper", "enabled": True, "live_orders": False, "book": self.strategy,
                "name": self.name, "day": self.day, "position": pos, "trades_today": self.trades,
                "losses_today": self.losses, "realized_today": round(self.realized, 2),
                "entries_stopped": self.losses >= MAX_LOSSES, "last_reject": self.last_reject,
                "window": f"entries {FIRST_ENTRY}-{LAST_ENTRY} · exit on close back through the level or {EXIT_AT}"}
