"""Paper COMBO confluence scalp (never Kite orders).

Chart overlay is unchanged. This book uses the same 1m votes as
``atlas_lite.combo`` / Confluence B/S, then adds exits the overlay does not:

* Closed 1m only. **B → long ATM CE**, **S → long ATM PE**, **1 lot**.
* Target **+8%**, stop **−6%**, or flatten after **12 minutes**.
* Flatten when confluence is lost (``side`` is None / tooltip Side: —).
* Opposite letter closes the wing and may reverse if a slot remains.
* Entries **09:30–14:45 IST**, max **4**/day, **8 minutes** cooldown after
  a flatten (reverse in the same closed minute skips cooldown).
* Hard flat by **15:14**.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from atlas_lite.combo import last_day_combo
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger, ist_now
from atlas_lite.metrics import quote_ltp
from atlas_lite.minute_bars import hm_ge, hm_le

IST = ZoneInfo("Asia/Kolkata")

STRATEGY = "combo_confluence"
CAPITAL = 200_000.0
LOTS = 1
DEFAULT_LOT_SIZE = 65
MAX_ENTRIES_PER_DAY = 4
ENTRY_AFTER = (9, 30)
ENTRY_UNTIL = (14, 45)
SQUARE_OFF = (15, 14)
HOLD_MINUTES = 12
TARGET_PCT = 0.08
STOP_PCT = 0.06
COOLDOWN_MIN = 8

Side = Literal["ce", "pe"]
ComboLetter = Literal["B", "S"]


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def paper_combo_enabled() -> bool:
    """Off by default. The option tape had no paying variant. Set ATLAS_LITE_PAPER_COMBO=1 to run."""
    raw = os.environ.get("ATLAS_LITE_PAPER_COMBO", "0").strip().lower()
    return raw in ("1", "true", "yes")


def in_combo_entry_window(now: datetime) -> bool:
    return hm_ge(now, ENTRY_AFTER) and hm_le(now, ENTRY_UNTIL) and not hm_ge(now, SQUARE_OFF)


def combo_letter_side(letter: str | None) -> Side | None:
    if letter == "B":
        return "ce"
    if letter == "S":
        return "pe"
    return None


def closed_bars_before(bars: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Drop the forming IST minute so a mid-bar wick cannot fire COMBO."""
    current = now.astimezone(IST).strftime("%Y-%m-%d %H:%M")
    out: list[dict[str, Any]] = []
    for bar in bars:
        t = str(bar.get("t") or "").replace("T", " ")[:16]
        if t and t < current:
            out.append(bar)
    return out


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _weekday(now: datetime) -> bool:
    return now.weekday() < 5


def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _open_leg(px: float, qty: int) -> list[tuple[float, int, str]]:
    return [(float(px), qty, "buy")]


def _close_leg(px: float, qty: int) -> list[tuple[float, int, str]]:
    return [(float(px), qty, "sell")]


@dataclass
class ComboPosition:
    day: str
    atm: int
    side: Side
    letter: ComboLetter
    symbol: str
    ce_symbol: str
    pe_symbol: str
    qty: int
    lots: int
    entry: float
    target: float
    stop: float
    hold_until: str
    opened_at: str
    charges_open: float
    signal_minute: str
    bull: int
    bear: int


@dataclass
class PaperCombo:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = LOTS
    capital: float = CAPITAL
    max_entries_per_day: int = MAX_ENTRIES_PER_DAY
    hold_minutes: int = HOLD_MINUTES
    target_pct: float = TARGET_PCT
    stop_pct: float = STOP_PCT
    cooldown_min: int = COOLDOWN_MIN
    position: ComboPosition | None = None
    traded_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    eod_written: bool = False
    last_event: dict[str, Any] | None = None
    last_exit_at: datetime | None = None
    last_signal_minute: str = ""
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_combo"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restore()

    def _restore(self) -> None:
        if not self.path.is_file():
            return
        last_open: dict[str, Any] | None = None
        last_exit_at: datetime | None = None
        last_signal_minute = ""
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
                last_exit_at = None
                last_signal_minute = ""
                eod_written = False
            if kind == "open":
                last_open = ev
                entries += 1
                last_signal_minute = str(ev.get("signal_minute") or "")
            elif kind == "close":
                last_open = None
                if entries == 0:
                    entries = 1
                if ev.get("day_pnl") is not None:
                    day_pnl = float(ev["day_pnl"])
                last_exit_at = _parse_ts(str(ev.get("ts") or ""))
            elif kind == "day_pnl":
                if ev.get("day_pnl") is not None:
                    day_pnl = float(ev["day_pnl"])
                eod_written = True
        self.last_event = last_any
        self.traded_day = day
        self.entries_today = entries
        self.day_pnl = round(day_pnl, 2)
        self.eod_written = bool(eod_written)
        self.last_exit_at = last_exit_at
        self.last_signal_minute = last_signal_minute
        if last_open:
            entry = float(last_open.get("entry") or 0)
            raw_letter = last_open.get("letter")
            if raw_letter in ("B", "S"):
                letter = raw_letter
            else:
                letter = "S" if last_open.get("side") == "pe" else "B"
            self.position = ComboPosition(
                day=str(last_open["day"]),
                atm=int(last_open["atm"]),
                side="pe" if last_open.get("side") == "pe" else "ce",
                letter=letter,
                symbol=str(last_open.get("symbol") or ""),
                ce_symbol=str(last_open["ce_symbol"]),
                pe_symbol=str(last_open["pe_symbol"]),
                qty=int(last_open.get("qty") or self.lots * self.lot_size),
                lots=int(last_open.get("lots") or self.lots),
                entry=entry,
                target=self._restored_level(last_open, "target", entry, 1.0 + float(self.target_pct)),
                stop=self._restored_level(last_open, "stop", entry, 1.0 - float(self.stop_pct)),
                hold_until=str(last_open.get("hold_until") or ""),
                opened_at=str(last_open.get("ts") or ""),
                charges_open=float(last_open.get("charges") or 0.0),
                signal_minute=str(last_open.get("signal_minute") or ""),
                bull=int(last_open.get("bull") or 0),
                bear=int(last_open.get("bear") or 0),
            )

    def _restored_level(
        self, event: dict[str, Any], key: str, entry: float, mult: float
    ) -> float:
        raw = _f(event.get(key))
        if raw is not None and raw > 0:
            return raw
        if entry > 0:
            return round(entry * mult, 2)
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
            self._log.warning("paper combo ledger write failed: %s", exc)
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

    def _in_cooldown(self, now: datetime) -> bool:
        if self.last_exit_at is None:
            return False
        return now < self.last_exit_at + timedelta(minutes=int(self.cooldown_min))

    def _hold_due(self, now: datetime, pos: ComboPosition) -> bool:
        until = _parse_ts(pos.hold_until)
        if until is None:
            opened = _parse_ts(pos.opened_at)
            if opened is None:
                return False
            until = opened + timedelta(minutes=int(self.hold_minutes))
        return now >= until

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
                "letter": pos.letter,
                "symbol": pos.symbol,
                "ce_symbol": pos.ce_symbol,
                "pe_symbol": pos.pe_symbol,
                "qty": pos.qty,
                "lots": pos.lots,
                "entry": pos.entry,
                "target": pos.target,
                "stop": pos.stop,
                "hold_until": pos.hold_until,
                "opened_at": pos.opened_at,
                "charges_open": pos.charges_open,
                "signal_minute": pos.signal_minute,
                "bull": pos.bull,
                "bear": pos.bear,
            }
            charges = float(pos.charges_open)
            ce, pe = self._book_quotes(book, pos.ce_symbol, pos.pe_symbol)
            px = self._side_px(ce, pe, pos.side)
            if px is not None:
                open_gross = round((px - pos.entry) * pos.qty, 2)
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
            "target_pct": self.target_pct,
            "stop_pct": self.stop_pct,
            "hold_minutes": int(self.hold_minutes),
            "cooldown_min": int(self.cooldown_min),
            "max_entries_per_day": int(self.max_entries_per_day),
            "last_event": self.last_event,
        }

    def _roll_to_day(self, day: str) -> None:
        self.traded_day = day
        self.entries_today = 0
        self.day_pnl = 0.0
        self.eod_written = False
        self.last_exit_at = None
        self.last_signal_minute = ""

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
        bars: list[dict[str, Any]] | None = None,
        combo: dict[str, Any] | None = None,
        signal_minute: str | None = None,
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
        row = combo
        if row is None and bars is not None:
            row = last_day_combo(closed_bars_before(bars, now), day)
        minute = signal_minute or (str(row.get("t") or "") if row else "")
        if self.position is not None:
            closed = self._maybe_exit(now, book, row=row, signal_minute=minute)
            if hm_ge(now, SQUARE_OFF):
                eod = None if self.position is not None else self._write_eod_if_needed(now)
                return closed or eod
            if (
                closed
                and closed.get("reason") == "flip"
                and allow_new_entries
                and row is not None
                and combo_letter_side(str(row.get("signal") or ""))
                and self.entries_today < int(self.max_entries_per_day)
                and in_combo_entry_window(now)
            ):
                opened = self._open(
                    now,
                    feed,
                    book,
                    ce_symbol,
                    pe_symbol,
                    atm,
                    str(row["signal"]),
                    row,
                    minute,
                )
                return opened or closed
            return closed
        if hm_ge(now, SQUARE_OFF):
            return self._write_eod_if_needed(now)
        if not allow_new_entries:
            return None
        if self.entries_today >= int(self.max_entries_per_day):
            return None
        if self._in_cooldown(now):
            return None
        if not in_combo_entry_window(now):
            return None
        if not row:
            return None
        if minute and minute == self.last_signal_minute:
            return None
        letter = str(row.get("signal") or "")
        if combo_letter_side(letter) is None:
            return None
        return self._open(now, feed, book, ce_symbol, pe_symbol, atm, letter, row, minute)

    def _open(
        self,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource | None,
        ce_symbol: str | None,
        pe_symbol: str | None,
        atm: int | None,
        letter: str,
        row: dict[str, Any],
        signal_minute: str | None = None,
    ) -> dict[str, Any] | None:
        side = combo_letter_side(letter)
        if side is None:
            return None
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
        entry = round(ce if side == "ce" else pe, 2)
        target = round(entry * (1.0 + float(self.target_pct)), 2)
        stop = round(entry * (1.0 - float(self.stop_pct)), 2)
        hold_until = (now + timedelta(minutes=int(self.hold_minutes))).isoformat()
        charges_open = float(kite_nfo_charges(_open_leg(entry, qty))["total"])
        pos = ComboPosition(
            day=now.strftime("%Y-%m-%d"),
            atm=int(atm),
            side=side,
            letter="S" if letter == "S" else "B",
            symbol=ce_sym if side == "ce" else pe_sym,
            ce_symbol=ce_sym,
            pe_symbol=pe_sym,
            qty=qty,
            lots=int(self.lots),
            entry=entry,
            target=target,
            stop=stop,
            hold_until=hold_until,
            opened_at=now.isoformat(),
            charges_open=round(charges_open, 2),
            signal_minute=signal_minute or "",
            bull=int(row.get("bull") or 0),
            bear=int(row.get("bear") or 0),
        )
        event = self._append(
            {
                "event": "open",
                "ts": now.isoformat(),
                "day": pos.day,
                "atm": pos.atm,
                "side": pos.side,
                "letter": pos.letter,
                "symbol": pos.symbol,
                "ce_symbol": pos.ce_symbol,
                "pe_symbol": pos.pe_symbol,
                "qty": pos.qty,
                "lots": pos.lots,
                "lot_size": self.lot_size,
                "entry": pos.entry,
                "target": pos.target,
                "stop": pos.stop,
                "signal_minute": pos.signal_minute,
                "hold_until": pos.hold_until,
                "target_pct": self.target_pct,
                "stop_pct": self.stop_pct,
                "bull": pos.bull,
                "bear": pos.bear,
                "charges": pos.charges_open,
            }
        )
        if event is None:
            return None
        self.position = pos
        self.traded_day = pos.day
        self.entries_today += 1
        self.last_signal_minute = pos.signal_minute
        self.last_exit_at = None
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
        book: QuoteSource | None,
        *,
        row: dict[str, Any] | None,
        signal_minute: str,
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
        if pos.target > 0 and px >= pos.target:
            return self._close(now, ce, pe, "target")
        if pos.stop > 0 and px <= pos.stop:
            return self._close(now, ce, pe, "stop")
        if self._hold_due(now, pos) or hm_ge(now, SQUARE_OFF):
            return self._close(now, ce, pe, "time")
        if not row:
            return None
        if signal_minute and signal_minute == pos.signal_minute:
            return None
        nxt = row.get("signal")
        if nxt in ("B", "S") and nxt != pos.letter:
            return self._close(now, ce, pe, "flip")
        if row.get("side") is None:
            return self._close(now, ce, pe, "confluence")
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
                pnl_gross = round((exit_px - pos.entry) * pos.qty, 2)
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
                "letter": pos.letter,
                "symbol": pos.symbol,
                "ce_symbol": pos.ce_symbol,
                "pe_symbol": pos.pe_symbol,
                "qty": pos.qty,
                "entry": pos.entry,
                "exit": exit_px,
                "target": pos.target,
                "stop": pos.stop,
                "pnl_gross": pnl_gross,
                "charges": charges,
                "charges_open": pos.charges_open,
                "charges_close": charges_close,
                "pnl": pnl,
                "pnl_known": marked and pnl is not None,
                "opened_at": pos.opened_at,
                "signal_minute": pos.signal_minute,
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
            self.entries_today = 1
        self.position = None
        self.day_pnl = new_day_pnl
        self.last_exit_at = now
        return event
