"""Paper ATM skew fade — sell the rich wing only (never Kite orders).

Historic recordings (local + OCI, 1 lot, Kite NFO charges) beat the
13:00 short *both* ATM wings, and are a different bet: fade the expensive
morning side instead of doubling the iron fly's short-vol.

* Always-short-CE / always-short-PE controls lose or chop.
* |CE−PE| ≥ 12 at 11:00–11:15, sell the rich wing, hold 15:14: 6/6 complete
  days. Sep 16/17 tapes died before 15:14 — those are tape_end, not holds.
* Same tape, 13:00 short straddle: 6W/3L and two 6% stops on cheap expiry
  straddles. Skew skip days were Sep 14 (flat) and Sep 15 (skew 9). Sep 22
  did fill (PE rich by 21).

Rules:

* If |CE−PE| ≥ 12 pts in **11:00–11:15 IST**, sell the richer ATM wing,
  **1 lot**, max **1**/day.
* Hold to **15:14** (no profit target).
* Stop if that wing **rises 15%**.
* Skip when ``|NIFTY chg| > 0.75%``.
* Separate ledger from iron fly / VWAP / 14:00 short ATM straddle.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger, ist_now
from atlas_lite.metrics import quote_ltp
from atlas_lite.minute_bars import hm_ge, hm_le
from atlas_lite.paper_straddle import PAPER_INDEX_ABS

IST = ZoneInfo("Asia/Kolkata")

STRATEGY = "atm_skew_fade"
CAPITAL = 200_000.0
LOTS = 1
DEFAULT_LOT_SIZE = 65
MAX_ENTRIES_PER_DAY = 1
ENTRY_AFTER = (11, 0)
ENTRY_UNTIL = (11, 15)
SQUARE_OFF = (15, 14)
STOP_PCT = 0.15
MIN_SKEW = 12.0

Side = Literal["ce", "pe"]


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def paper_skew_fade_enabled() -> bool:
    """Off by default. Skew faded the wrong wing. Set ATLAS_LITE_PAPER_SKEW_FADE=1 to run."""
    raw = os.environ.get("ATLAS_LITE_PAPER_SKEW_FADE", "0").strip().lower()
    return raw in ("1", "true", "yes")


def in_skew_fade_entry_window(now: datetime) -> bool:
    return hm_ge(now, ENTRY_AFTER) and hm_le(now, ENTRY_UNTIL) and not hm_ge(now, SQUARE_OFF)


def rich_atm_side(ce: float, pe: float, min_skew: float = MIN_SKEW) -> Side | None:
    if ce >= pe + min_skew:
        return "ce"
    if pe >= ce + min_skew:
        return "pe"
    return None


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


def _open_leg(px: float, qty: int) -> list[tuple[float, int, str]]:
    return [(float(px), qty, "sell")]


def _close_leg(px: float, qty: int) -> list[tuple[float, int, str]]:
    return [(float(px), qty, "buy")]


@dataclass
class SkewFadePosition:
    day: str
    atm: int
    side: Side
    symbol: str
    ce_symbol: str
    pe_symbol: str
    qty: int
    lots: int
    entry: float
    stop: float
    ce_entry: float
    pe_entry: float
    skew: float
    opened_at: str
    charges_open: float


@dataclass
class PaperSkewFade:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = LOTS
    capital: float = CAPITAL
    max_entries_per_day: int = MAX_ENTRIES_PER_DAY
    stop_pct: float = STOP_PCT
    min_skew: float = MIN_SKEW
    position: SkewFadePosition | None = None
    traded_day: str = ""
    filled_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    eod_written: bool = False
    last_event: dict[str, Any] | None = None
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_skew_fade"), repr=False)

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
        filled_day = ""
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
                if d:
                    filled_day = d
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
        self.filled_day = filled_day
        self.entries_today = entries
        self.day_pnl = round(day_pnl, 2)
        self.eod_written = bool(eod_written)
        if last_open:
            side = str(last_open.get("side") or "")
            if side not in ("ce", "pe"):
                return
            self.position = SkewFadePosition(
                day=str(last_open["day"]),
                atm=int(last_open["atm"]),
                side=side,  # type: ignore[arg-type]
                symbol=str(last_open.get("symbol") or last_open.get(f"{side}_symbol") or ""),
                ce_symbol=str(last_open["ce_symbol"]),
                pe_symbol=str(last_open["pe_symbol"]),
                qty=int(last_open.get("qty") or self.lots * self.lot_size),
                lots=int(last_open.get("lots") or self.lots),
                entry=float(last_open.get("entry") or last_open.get(side) or 0),
                stop=self._restored_stop(
                    last_open,
                    float(last_open.get("entry") or last_open.get(side) or 0),
                ),
                ce_entry=float(last_open.get("ce_entry") or last_open.get("ce") or 0),
                pe_entry=float(last_open.get("pe_entry") or last_open.get("pe") or 0),
                skew=float(last_open.get("skew") or 0),
                opened_at=str(last_open.get("ts") or ""),
                charges_open=float(last_open.get("charges") or 0.0),
            )

    def _restored_stop(self, event: dict[str, Any], entry: float) -> float:
        stop = _f(event.get("stop"))
        if stop is not None and stop > 0:
            return stop
        if entry > 0:
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
            self._log.warning("paper skew-fade ledger write failed: %s", exc)
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

    def _side_px(self, ce: float | None, pe: float | None, side: Side) -> float | None:
        return ce if side == "ce" else pe

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
                "side": pos.side,
                "symbol": pos.symbol,
                "ce_symbol": pos.ce_symbol,
                "pe_symbol": pos.pe_symbol,
                "qty": pos.qty,
                "lots": pos.lots,
                "entry": pos.entry,
                "stop": pos.stop,
                "ce_entry": pos.ce_entry,
                "pe_entry": pos.pe_entry,
                "skew": pos.skew,
                "opened_at": pos.opened_at,
                "charges_open": pos.charges_open,
            }
            charges = float(pos.charges_open)
            ce, pe = self._book_quotes(book, pos.ce_symbol, pos.pe_symbol)
            px = self._side_px(ce, pe, pos.side)
            if px is not None:
                open_gross = round((pos.entry - px) * pos.qty, 2)
                close_ch = kite_nfo_charges(_close_leg(px, pos.qty))["total"]
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
            "min_skew": self.min_skew,
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
        allow_new_entries: bool = True,
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
        if not allow_new_entries:
            return None
        if self.entries_today >= int(self.max_entries_per_day):
            return None
        if not in_skew_fade_entry_window(now):
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
        side = rich_atm_side(ce, pe, self.min_skew)
        if side is None:
            return None
        qty = int(self.lots) * int(self.lot_size)
        if qty <= 0:
            return None
        entry = round(ce if side == "ce" else pe, 2)
        stop_px = round(entry * (1.0 + float(self.stop_pct)), 2)
        charges_open = float(kite_nfo_charges(_open_leg(entry, qty))["total"])
        pos = SkewFadePosition(
            day=now.strftime("%Y-%m-%d"),
            atm=int(atm),
            side=side,
            symbol=ce_sym if side == "ce" else pe_sym,
            ce_symbol=ce_sym,
            pe_symbol=pe_sym,
            qty=qty,
            lots=int(self.lots),
            entry=entry,
            stop=stop_px,
            ce_entry=round(ce, 2),
            pe_entry=round(pe, 2),
            skew=round(abs(ce - pe), 2),
            opened_at=now.isoformat(),
            charges_open=round(charges_open, 2),
        )
        event = self._append(
            {
                "event": "open",
                "ts": now.isoformat(),
                "day": pos.day,
                "atm": pos.atm,
                "side": pos.side,
                "symbol": pos.symbol,
                "ce_symbol": pos.ce_symbol,
                "pe_symbol": pos.pe_symbol,
                "qty": pos.qty,
                "lots": pos.lots,
                "lot_size": self.lot_size,
                "ce": pos.ce_entry,
                "pe": pos.pe_entry,
                "ce_entry": pos.ce_entry,
                "pe_entry": pos.pe_entry,
                "entry": pos.entry,
                "stop": pos.stop,
                "skew": pos.skew,
                "min_skew": self.min_skew,
                "stop_pct": self.stop_pct,
                "charges": pos.charges_open,
            }
        )
        if event is None:
            return None
        self.position = pos
        self.traded_day = pos.day
        self.filled_day = pos.day
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
        px = self._side_px(ce, pe, pos.side)
        if px is None:
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
        px = self._side_px(ce, pe, pos.side)
        if px is None:
            if hm_ge(now, SQUARE_OFF):
                return self._close(now, None, None, "time_flat", marked=False)
            return None
        if pos.stop > 0 and px >= pos.stop:
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
        exit_px = None
        book_day = book_day or pos.day
        rolling = book_day != (self.traded_day or pos.day)
        new_day_pnl = 0.0 if rolling else self.day_pnl
        if marked:
            px = self._side_px(ce, pe, pos.side)
            if px is not None:
                exit_px = round(float(px), 2)
                pnl_gross = round((pos.entry - exit_px) * pos.qty, 2)
                charges_close = float(kite_nfo_charges(_close_leg(exit_px, pos.qty))["total"])
                charges = round(pos.charges_open + charges_close, 2)
                pnl = round(pnl_gross - charges, 2)
                new_day_pnl = round(new_day_pnl + pnl, 2)
        event = self._append(
            {
                "event": "close",
                "ts": now.isoformat(),
                "day": book_day,
                "reason": reason,
                "atm": pos.atm,
                "side": pos.side,
                "symbol": pos.symbol,
                "ce_symbol": pos.ce_symbol,
                "pe_symbol": pos.pe_symbol,
                "qty": pos.qty,
                "ce_entry": pos.ce_entry,
                "pe_entry": pos.pe_entry,
                "entry": pos.entry,
                "exit": exit_px,
                "stop": pos.stop,
                "skew": pos.skew,
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
