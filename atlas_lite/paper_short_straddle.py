"""Paper afternoon short ATM straddle (never Kite orders).

From OCI session recordings (ATM CE+PE, Kite NFO charges; paper size **1 lot**):

* 14:00→15:14 hold beat 13:00 hold on 14–25 Sep (+₹3,631 vs +₹1,264).
* A 6% premium stop turned that tape into +₹243 and stopped 25 Sep live
  at −₹986 while a hold would have been +₹197.

Rules:

* Sell ATM CE+PE, **1 lot**, **14:00–14:15 IST**, max **1**/day.
* Hold to **15:14** (no profit target, no premium stop).
* Skip when ``|NIFTY chg| > 0.75%`` (same day-trend cap as the fly).
* Skip when ATM skew fade already filled today, or the iron fly is still
  open (do not stack short-vol).
* Separate ledger from iron fly / VWAP.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger, ist_now
from atlas_lite.metrics import quote_ltp
from atlas_lite.minute_bars import hm_ge, hm_le
from atlas_lite.paper_straddle import PAPER_INDEX_ABS

IST = ZoneInfo("Asia/Kolkata")

STRATEGY = "short_atm_straddle"
CAPITAL = 200_000.0
LOTS = 1
DEFAULT_LOT_SIZE = 65
MAX_ENTRIES_PER_DAY = 1
ENTRY_AFTER = (14, 0)
ENTRY_UNTIL = (14, 15)
SQUARE_OFF = (15, 14)
# 0 = hold to square-off. A 6% stop lost vs hold on the Sep 14–25 tape.
STOP_PCT = 0.0


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def paper_short_str_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_SHORT_STR", "1").strip().lower()
    return raw in ("1", "true", "yes")


def in_short_str_entry_window(now: datetime) -> bool:
    return hm_ge(now, ENTRY_AFTER) and hm_le(now, ENTRY_UNTIL) and not hm_ge(now, SQUARE_OFF)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _weekday(now: datetime) -> bool:
    return now.weekday() < 5


def _trend_ok(feed: dict[str, Any]) -> bool:
    chg = _f(feed.get("index_nifty_chg"))
    if chg is None:
        return False
    return abs(chg) <= PAPER_INDEX_ABS


def _open_legs(ce: float, pe: float, qty: int) -> list[tuple[float, int, str]]:
    return [(float(ce), qty, "sell"), (float(pe), qty, "sell")]


def _close_legs(ce: float, pe: float, qty: int) -> list[tuple[float, int, str]]:
    return [(float(ce), qty, "buy"), (float(pe), qty, "buy")]


@dataclass
class ShortStrPosition:
    day: str
    atm: int
    ce_symbol: str
    pe_symbol: str
    qty: int
    lots: int
    ce_entry: float
    pe_entry: float
    straddle_entry: float
    stop_straddle: float
    opened_at: str
    charges_open: float


@dataclass
class PaperShortStraddle:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = LOTS
    capital: float = CAPITAL
    max_entries_per_day: int = MAX_ENTRIES_PER_DAY
    stop_pct: float = STOP_PCT
    position: ShortStrPosition | None = None
    traded_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    eod_written: bool = False
    last_event: dict[str, Any] | None = None
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_short_str"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restore()

    def _restore(self) -> None:
        if not self.path.is_file():
            return
        last_open: dict[str, Any] | None = None
        day_pnl = 0.0
        entries = 0
        day = ""
        eod_written = False
        last_any: dict[str, Any] | None = None
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("strategy") != STRATEGY:
                continue
            last_any = ev
            kind = ev.get("event")
            d = str(ev.get("day") or "")
            if d and d != day:
                day = d
                day_pnl = 0.0
                entries = 0
                last_open = None
                eod_written = False
            if kind == "open":
                last_open = ev
                entries += 1
            elif kind == "close":
                last_open = None
                # Leftover flatten books onto a new calendar day — the open row
                # stays on the prior day, so count that close as today's slot.
                if entries == 0:
                    entries = 1
                if ev.get("day_pnl") is not None:
                    day_pnl = float(ev["day_pnl"])
            elif kind == "day_pnl":
                if ev.get("day_pnl") is not None:
                    day_pnl = float(ev["day_pnl"])
                eod_written = True
        self.last_event = last_any
        self.traded_day = day
        self.entries_today = entries
        self.day_pnl = round(day_pnl, 2)
        self.eod_written = bool(eod_written)
        if last_open:
            self.position = ShortStrPosition(
                day=str(last_open["day"]),
                atm=int(last_open["atm"]),
                ce_symbol=str(last_open["ce_symbol"]),
                pe_symbol=str(last_open["pe_symbol"]),
                qty=int(last_open.get("qty") or self.lots * self.lot_size),
                lots=int(last_open.get("lots") or self.lots),
                ce_entry=float(last_open["ce_entry"] if last_open.get("ce_entry") is not None else last_open.get("ce") or 0),
                pe_entry=float(last_open["pe_entry"] if last_open.get("pe_entry") is not None else last_open.get("pe") or 0),
                straddle_entry=float(last_open.get("straddle_entry") or last_open.get("straddle") or 0),
                stop_straddle=self._restored_stop(
                    last_open,
                    float(last_open.get("straddle_entry") or last_open.get("straddle") or 0),
                ),
                opened_at=str(last_open.get("ts") or ""),
                charges_open=float(last_open.get("charges") or 0.0),
            )

    def _restored_stop(self, event: dict[str, Any], entry: float) -> float:
        stop = _f(event.get("stop_straddle"))
        if stop is not None and stop > 0:
            return stop
        if self.stop_pct > 0 and entry > 0:
            return round(entry * (1.0 + float(self.stop_pct)), 2)
        return 0.0

    def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event = dict(event)
        event.setdefault("mode", "paper")
        event.setdefault("strategy", STRATEGY)
        event.setdefault("logged_at", ist_now())
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError as exc:
            self._log.warning("paper short-straddle ledger write failed: %s", exc)
            return None
        self.last_event = event
        return event

    def _book_quotes(
        self, book: QuoteSource | None, ce_sym: str, pe_sym: str
    ) -> tuple[float | None, float | None]:
        if book is None:
            return None, None
        return quote_ltp(book.get(ce_sym)), quote_ltp(book.get(pe_sym))

    def _open_quotes(
        self, book: QuoteSource | None, ce_sym: str, pe_sym: str, feed: dict[str, Any]
    ) -> tuple[float | None, float | None]:
        ce, pe = self._book_quotes(book, ce_sym, pe_sym)
        if ce is None:
            ce = _f(feed.get("ce"))
        if pe is None:
            pe = _f(feed.get("pe"))
        return ce, pe

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        open_pnl = 0.0
        open_gross = 0.0
        charges = 0.0
        pos_body = None
        if self.position is not None:
            pos = self.position
            pos_body = {
                "day": pos.day,
                "strategy": STRATEGY,
                "atm": pos.atm,
                "ce_symbol": pos.ce_symbol,
                "pe_symbol": pos.pe_symbol,
                "qty": pos.qty,
                "lots": pos.lots,
                "ce_entry": pos.ce_entry,
                "pe_entry": pos.pe_entry,
                "straddle_entry": pos.straddle_entry,
                "stop_straddle": pos.stop_straddle,
                "opened_at": pos.opened_at,
                "charges_open": pos.charges_open,
            }
            charges = float(pos.charges_open)
            ce, pe = self._book_quotes(book, pos.ce_symbol, pos.pe_symbol)
            if ce is not None and pe is not None:
                exit_px = ce + pe
                open_gross = round((pos.straddle_entry - exit_px) * pos.qty, 2)
                close_ch = kite_nfo_charges(_close_legs(ce, pe, pos.qty))["total"]
                charges = round(pos.charges_open + close_ch, 2)
                open_pnl = round(open_gross - charges, 2)
        mtm = round(self.day_pnl + open_pnl, 2)
        return {
            "ok": True,
            "mode": "paper",
            "book": STRATEGY,
            "live_orders": False,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "qty_per_leg": self.lots * self.lot_size,
            "capital": self.capital,
            "traded_day": self.traded_day,
            "entries_today": self.entries_today,
            "day_pnl": round(self.day_pnl, 2),
            "day_pnl_pct": round(self.day_pnl / self.capital * 100.0, 4) if self.capital else 0.0,
            "open_pnl": open_pnl,
            "open_pnl_gross": open_gross,
            "charges": charges,
            "mtm_pnl": mtm,
            "equity": round(self.capital + mtm, 2),
            "position": pos_body,
            "eod": self.eod_written,
            "square_off": f"{SQUARE_OFF[0]:02d}:{SQUARE_OFF[1]:02d}",
            "entry_window": (
                f"{ENTRY_AFTER[0]:02d}:{ENTRY_AFTER[1]:02d}"
                f"-{ENTRY_UNTIL[0]:02d}:{ENTRY_UNTIL[1]:02d}"
            ),
            "stop_pct": self.stop_pct,
            "max_entries_per_day": int(self.max_entries_per_day),
            "last_event": self.last_event,
        }

    def _roll_to_day(self, day: str) -> None:
        self.traded_day = day
        self.entries_today = 0
        self.day_pnl = 0.0
        self.eod_written = False

    def _seal_then_roll(self, now: datetime, day: str) -> dict[str, Any] | None:
        if self.position is not None or self.traded_day == day:
            return None
        sealed = None
        if self.traded_day and not self.eod_written and (self.entries_today or self.day_pnl):
            sealed = self._write_eod_if_needed(now)
            if sealed is None:
                return None
        self._roll_to_day(day)
        return sealed

    def _write_eod_if_needed(
        self, now: datetime, *, allow_open: bool = False
    ) -> dict[str, Any] | None:
        if self.eod_written:
            return None
        if self.position is not None and not allow_open:
            return None
        if not self.entries_today and self.day_pnl == 0.0:
            self.eod_written = True
            return None
        event = self._append(
            {
                "event": "day_pnl",
                "ts": now.isoformat(),
                "day": self.traded_day or now.strftime("%Y-%m-%d"),
                "capital": self.capital,
                "trades": self.entries_today,
                "day_pnl": round(self.day_pnl, 2),
                "day_pnl_pct": round(self.day_pnl / self.capital * 100.0, 4) if self.capital else 0.0,
                "equity": round(self.capital + self.day_pnl, 2),
            }
        )
        if event is None:
            return None
        self.eod_written = True
        return event

    def on_frame(
        self,
        *,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource | None,
        ce_symbol: str | None,
        pe_symbol: str | None,
        atm: int | None,
        allow_entry: bool = True,
    ) -> dict[str, Any] | None:
        day = now.strftime("%Y-%m-%d")
        if not _weekday(now):
            if self.position is not None:
                return self._flatten_stale(now, book, "weekend")
            return None
        if self.position is not None and self.position.day != day:
            return self._flatten_stale(now, book, "session_gap")
        if self.traded_day != day and self.position is None:
            sealed = self._seal_then_roll(now, day)
            if sealed is not None:
                return sealed
            if self.traded_day != day:
                return None
        if self.position is not None:
            closed = self._maybe_exit(now, feed, book)
            if hm_ge(now, SQUARE_OFF):
                eod = None if self.position is not None else self._write_eod_if_needed(now)
                return closed or eod
            return closed
        if hm_ge(now, SQUARE_OFF):
            return self._write_eod_if_needed(now)
        if self.entries_today >= int(self.max_entries_per_day):
            return None
        if not allow_entry:
            return None
        if not in_short_str_entry_window(now):
            return None
        if not _trend_ok(feed):
            return None
        return self._open(now, feed, book, ce_symbol, pe_symbol, atm)

    def _open(
        self,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource | None,
        ce_symbol: str | None,
        pe_symbol: str | None,
        atm: int | None,
    ) -> dict[str, Any] | None:
        ce_sym = ce_symbol or str(feed.get("ce_symbol") or "")
        pe_sym = pe_symbol or str(feed.get("pe_symbol") or "")
        if not ce_sym or not pe_sym or atm is None:
            return None
        ce, pe = self._open_quotes(book, ce_sym, pe_sym, feed)
        if ce is None or pe is None or ce <= 0 or pe <= 0:
            return None
        qty = int(self.lots) * int(self.lot_size)
        if qty <= 0:
            return None
        straddle = round(ce + pe, 2)
        stop_px = 0.0
        if self.stop_pct > 0:
            stop_px = round(straddle * (1.0 + float(self.stop_pct)), 2)
        charges_open = float(kite_nfo_charges(_open_legs(ce, pe, qty))["total"])
        pos = ShortStrPosition(
            day=now.strftime("%Y-%m-%d"),
            atm=int(atm),
            ce_symbol=ce_sym,
            pe_symbol=pe_sym,
            qty=qty,
            lots=int(self.lots),
            ce_entry=round(ce, 2),
            pe_entry=round(pe, 2),
            straddle_entry=straddle,
            stop_straddle=stop_px,
            opened_at=now.isoformat(),
            charges_open=round(charges_open, 2),
        )
        event = self._append(
            {
                "event": "open",
                "ts": now.isoformat(),
                "day": pos.day,
                "atm": pos.atm,
                "ce_symbol": pos.ce_symbol,
                "pe_symbol": pos.pe_symbol,
                "qty": pos.qty,
                "lots": pos.lots,
                "lot_size": self.lot_size,
                "ce": pos.ce_entry,
                "pe": pos.pe_entry,
                "ce_entry": pos.ce_entry,
                "pe_entry": pos.pe_entry,
                "straddle": pos.straddle_entry,
                "straddle_entry": pos.straddle_entry,
                "stop_straddle": pos.stop_straddle,
                "stop_pct": self.stop_pct,
                "charges": pos.charges_open,
            }
        )
        if event is None:
            return None
        self.position = pos
        self.traded_day = pos.day
        self.entries_today += 1
        return event

    def _flatten_stale(
        self,
        now: datetime,
        book: QuoteSource | None,
        reason: str,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        calendar_day = now.strftime("%Y-%m-%d")
        # Live quotes are today's — do not stamp that mark onto the open day.
        book_day = calendar_day if pos.day != calendar_day else None
        if (
            book_day
            and self.traded_day
            and self.traded_day != book_day
            and not self.eod_written
            and (self.entries_today or self.day_pnl)
        ):
            if self._write_eod_if_needed(now, allow_open=True) is None:
                return None
        ce, pe = self._book_quotes(book, pos.ce_symbol, pos.pe_symbol)
        if ce is None or pe is None:
            return self._close(now, None, None, reason, marked=False, book_day=book_day)
        return self._close(now, ce, pe, reason, book_day=book_day)

    def _maybe_exit(
        self,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource | None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        ce, pe = self._book_quotes(book, pos.ce_symbol, pos.pe_symbol)
        if ce is None or pe is None:
            if hm_ge(now, SQUARE_OFF):
                return self._close(now, None, None, "time_flat", marked=False)
            return None
        straddle = ce + pe
        if pos.stop_straddle > 0 and straddle >= pos.stop_straddle:
            return self._close(now, ce, pe, "stop")
        if hm_ge(now, SQUARE_OFF):
            return self._close(now, ce, pe, "time")
        return None

    def _close(
        self,
        now: datetime,
        ce: float | None,
        pe: float | None,
        reason: str,
        *,
        marked: bool = True,
        book_day: str | None = None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        pnl = None
        pnl_gross = None
        charges = None
        charges_close = None
        straddle = None
        ce_exit = None
        pe_exit = None
        book_day = book_day or pos.day
        rolling = book_day != (self.traded_day or pos.day)
        new_day_pnl = 0.0 if rolling else self.day_pnl
        if marked and ce is not None and pe is not None:
            straddle = round(float(ce) + float(pe), 2)
            pnl_gross = round((pos.straddle_entry - straddle) * pos.qty, 2)
            charges_close = float(kite_nfo_charges(_close_legs(ce, pe, pos.qty))["total"])
            charges = round(pos.charges_open + charges_close, 2)
            pnl = round(pnl_gross - charges, 2)
            ce_exit = round(float(ce), 2)
            pe_exit = round(float(pe), 2)
            new_day_pnl = round(new_day_pnl + pnl, 2)
        event = self._append(
            {
                "event": "close",
                "ts": now.isoformat(),
                "day": book_day,
                "reason": reason,
                "atm": pos.atm,
                "ce_symbol": pos.ce_symbol,
                "pe_symbol": pos.pe_symbol,
                "qty": pos.qty,
                "ce_entry": pos.ce_entry,
                "pe_entry": pos.pe_entry,
                "ce_exit": ce_exit,
                "pe_exit": pe_exit,
                "straddle_entry": pos.straddle_entry,
                "straddle_exit": straddle,
                "stop_straddle": pos.stop_straddle,
                "pnl_gross": pnl_gross,
                "charges": charges,
                "charges_open": pos.charges_open,
                "charges_close": charges_close,
                "pnl": pnl,
                "pnl_known": marked and pnl is not None,
                "opened_at": pos.opened_at,
                "day_pnl": round(new_day_pnl, 2),
                "day_pnl_pct": round(new_day_pnl / self.capital * 100.0, 4) if self.capital else 0.0,
                "capital": self.capital,
                "equity": round(self.capital + new_day_pnl, 2),
            }
        )
        if event is None:
            return None
        if rolling:
            self._roll_to_day(book_day)
            # Leftover flatten is today's slot — do not allow a second entry.
            self.entries_today = 1
        self.position = None
        self.day_pnl = new_day_pnl
        return event
