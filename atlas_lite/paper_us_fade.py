"""US-fade watch book: fade a big S&P 500 move after India opens. Paper only, never live, no LLM. 1 lot ATM CE or PE.

Operator's call, 28 Nov 2026: a watch book for the one weak buying lead of the 28 Nov push (scratchpad us_cues.py;
S&P 500 daily from Yahoo vs NIFTY 09:16 -> 15:14, 2019-2026): after a US session that moved 1% or more, NIFTY's rest
of the day leaned the other way: +5.1 NIFTY pts a day in 2019-23 (t 0.6, 357 days, won 50%) and +16.8 in 2024-26
(t 1.3, 143 days, won 57%). Same sign in both periods but NOT significant and, in 2019-23, below an option buy's
costs. Built to collect real evidence, not as a candidate for money.

Rules:
- Each session from 09:00 a background thread fetches the S&P 500's daily closes from Yahoo Finance (public data;
  no account) and keeps the last completed US session dated before today.
- At 09:16 (from second 3): US move >= +1% -> buy the ATM PE at the ask; <= -1% -> buy the ATM CE. Otherwise no trade.
  If the US figure is not in by 09:20 the day is skipped (recorded).
- Exit at 15:14 at the bid. No stop (as tested). One trade a day.
- Guards (28 Nov double-check): only on a day NIFTY has traded (today's candles exist: never on an NSE holiday at
  stale quotes); the US session must be newer than India's previous session (after a US holiday the old move was
  already traded; fresh-only backtest +5.3 / +17.2 pts, unchanged); no entry after 09:20, even if the US figure
  arrives late.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from atlas_lite.agent_gates import _is_nse_trading_day
from atlas_lite.paper_g2t10 import PaperG2T10, QuoteSource

STRATEGY = "us_fade"
LEDGER_NAME = "paper_us_fade.jsonl"
NAME = "US-fade watch · no LLM"
ENTRY_AT, ENTRY_UNTIL, EXIT_AT = "09:16", "09:20", "15:14"
MOVE_PCT = 1.0
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=15d&interval=1d"
US_TZ = timezone(timedelta(hours=-5))  # a Yahoo daily bar's timestamp is the US open; -5 h keeps the US date


def paper_us_fade_enabled() -> bool:
    return os.environ.get("ATLAS_LITE_PAPER_US_FADE", "1").strip().lower() in ("1", "true", "yes")


def last_us_move(payload: dict[str, Any], india_day: str) -> tuple[str, float] | None:
    """(US date, % move) of the last completed S&P 500 session dated before `india_day`, from Yahoo's chart JSON."""
    try:
        res = payload["chart"]["result"][0]
        ts, closes = res["timestamp"], res["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return None
    rows = [(datetime.fromtimestamp(t, US_TZ).date().isoformat(), c) for t, c in zip(ts, closes) if c]
    rows = [r for r in rows if r[0] < india_day]
    if len(rows) < 2:
        return None
    (_, prev), (d, last) = rows[-2], rows[-1]
    return d, round((last / prev - 1) * 100, 3)


def previous_nse_session(india_day: str) -> str:
    d = date.fromisoformat(india_day) - timedelta(days=1)
    for _ in range(15):
        if _is_nse_trading_day(d):
            return d.isoformat()
        d -= timedelta(days=1)
    return d.isoformat()


def fetch_yahoo() -> dict[str, Any]:
    import httpx

    with httpx.Client(timeout=10.0, headers={"User-Agent": "Mozilla/5.0"}) as c:
        r = c.get(YAHOO_URL)
        r.raise_for_status()
        return r.json()


@dataclass
class PaperUsFade(PaperG2T10):
    """G2T10's ledger, restart, fills and square-off; the US move decides. Paper only (never Kite orders)."""

    strategy: str = STRATEGY
    name: str = NAME
    fetch: Callable[[], dict[str, Any]] = field(default=fetch_yahoo, repr=False)
    us: dict[str, Any] | None = None  # {"day": india day, "us_date", "move_pct"} or {"day", "error"}
    _fetching: str = ""
    decided: str = ""

    def _start_fetch(self, today: str) -> None:
        if self._fetching == today:
            return
        self._fetching = today

        def work() -> None:
            try:
                got = last_us_move(self.fetch(), today)
                self.us = ({"day": today, "us_date": got[0], "move_pct": got[1]} if got
                           else {"day": today, "error": "no US session found"})
            except Exception as exc:  # noqa: BLE001 - a failed fetch only skips the day
                self.us = {"day": today, "error": str(exc)[:120]}

        threading.Thread(target=work, name="us-fade-fetch", daemon=True).start()

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
        return hm != self.done_minute and now.second >= 3 and ENTRY_AT <= hm < EXIT_AT

    def on_frame(self, *, now: datetime, feed: dict[str, Any], book: QuoteSource) -> dict[str, Any] | None:
        today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        self._new_day(today)
        if hm >= "09:00" and hm < EXIT_AT:
            self._start_fetch(today)
        if self.position is not None and hm >= EXIT_AT:
            return self._close(now, book, "square_off")
        if self.position is not None or self.decided == today or self.trades:
            return None
        if not self.decision_due(now) or not feed or feed.get("error"):
            return None
        self.done_minute = hm
        if hm > ENTRY_UNTIL and self.decided != today:  # a restart after 09:20, or a US figure that came late
            self.decided = today
            self._append({"event": "skip", "ts": now.isoformat(), "day": today, "reason": "entry window 09:16-09:20 missed"})
            return None
        if not any(str(b.get("t") or "").startswith(today) for b in (feed.get("bars_today") or [])):
            return None  # NIFTY has not traded today (holiday / no candles yet): never trade at stale quotes
        us = self.us if self.us and self.us.get("day") == today else None
        if us is None:
            if hm == ENTRY_UNTIL:
                self.decided = today
                self._append({"event": "skip", "ts": now.isoformat(), "day": today, "reason": "US figure not in by 09:20"})
            return None
        self.decided = today
        move = us.get("move_pct")
        if us.get("us_date") and us["us_date"] < previous_nse_session(today):
            self._append({"event": "skip", "ts": now.isoformat(), "day": today, "us_date": us["us_date"],
                          "us_move_pct": move, "reason": "US session already traded by India (US holiday)"})
            return None
        if move is None or abs(move) < MOVE_PCT:
            self._append({"event": "skip", "ts": now.isoformat(), "day": today, "us_date": us.get("us_date"),
                          "us_move_pct": move, "reason": us.get("error") or f"US move under {MOVE_PCT}%"})
            return None
        leg = "pe" if move > 0 else "ce"
        symbol, strike = feed.get(f"{leg}_symbol"), int(feed.get("atm") or 0)
        px = self._buy_px(book, str(symbol)) if symbol else None
        if not symbol or not strike or not px:
            self.last_reject = "no_quote"
            self.decided = ""  # try again next minute until 09:20
            return None
        qty = int(self.lot_size)
        ev = self._append({"event": "open", "ts": now.isoformat(), "day": today, "leg": leg, "symbol": symbol,
                           "strike": strike, "qty": qty, "fill": round(px, 2), "entry": round(px, 2), "side": "long",
                           "us_date": us["us_date"], "us_move_pct": move,
                           "reason": f"S&P 500 {move:+.2f}% on {us['us_date']}: fade it ({leg.upper()})"})
        if ev is None:
            return None
        self.trades += 1
        self.position = {"leg": leg, "symbol": symbol, "strike": strike, "qty": qty, "fill": round(px, 2),
                         "opened_at": now.isoformat()}
        return ev

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        snap = super().snapshot(book)
        snap.pop("entries_stopped", None)
        snap.pop("losses_today", None)
        snap["us"] = self.us
        snap["window"] = (f"{ENTRY_AT}: S&P 500 last session >= +{MOVE_PCT}% -> buy ATM PE, <= -{MOVE_PCT}% -> ATM CE · "
                          f"exit {EXIT_AT} · no stop · paper watch")
        return snap
