"""factor_paper book — trend-following ATM CE/PE buys. Paper only, no Kite orders.

Forward test of the one setup that held out of sample in scripts/bt_setup_search.py
(see doc/factor-paper-book.md):

- Entry: on a closed minute, spot is >= TREND_PTS from Kite's session open ->
  buy ATM CE (above) or PE (below). Bars 09:30-14:44.
- Filter: skip while India VIX is >= 5% above its session open.
- Exits: +50% target / -25% stop / 90 min hold / flat 15:14. No trail.
- Book rules as the tape engine: 1 lot, max 3 opens/day, -2000 day stop,
  one position, 5 min after any exit, side blocks (stop 15m, loss 10m, win 5m),
  side locked after a stop <= -400 or a second stop.

Futures OI change since the open is logged on each entry (the covering
hypothesis) but never gates a trade. Independent of agent_paper: own ledger,
own switch, own position.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from atlas_lite.agent_gates import _is_nse_trading_day
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger
from atlas_lite.metrics import quote_ltp
from atlas_lite.minute_bars import hm_ge, hm_le
from atlas_lite.paper_agent import (
    DEFAULT_LOT_SIZE,
    SQUARE_OFF,
    _env_float,
    _env_hhmm,
    _env_int,
)

IST = ZoneInfo("Asia/Kolkata")
STRATEGY = "factor_paper"
LEDGER_NAME = "paper_factor.jsonl"

EXIT_GAP_MIN = 5
BLOCK_AFTER_STOP_MIN = 15
BLOCK_AFTER_LOSS_MIN = 10
BLOCK_AFTER_WIN_MIN = 5
LOCK_STOP_LOSS = -400.0
LOCK_STOPS = 2
# A base first seen after this is a mid-session restart, not the open.
BASE_BY = (9, 20)


def paper_factor_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_FACTOR", "0").strip().lower()
    return raw in ("1", "true", "yes")


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def _parse_ist(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


@dataclass
class FactorPosition:
    day: str
    atm: int
    side: str
    symbol: str
    qty: int
    lots: int
    entry: float
    target: float
    stop: float
    hold_until: str
    opened_at: str
    charges_open: float
    reason: str
    last_px: float | None = None


@dataclass
class PaperFactor:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = 1
    trend_pts: float = field(default_factory=lambda: _env_float("ATLAS_LITE_FACTOR_TREND_PTS", 50.0))
    entry_after: tuple[int, int] = field(
        default_factory=lambda: _env_hhmm("ATLAS_LITE_FACTOR_ENTRY_AFTER", (9, 30))
    )
    entry_until: tuple[int, int] = field(
        default_factory=lambda: _env_hhmm("ATLAS_LITE_FACTOR_ENTRY_UNTIL", (14, 44))
    )
    target_pct: float = field(default_factory=lambda: _env_float("ATLAS_LITE_FACTOR_TARGET_PCT", 0.50))
    stop_pct: float = field(default_factory=lambda: _env_float("ATLAS_LITE_FACTOR_STOP_PCT", 0.25))
    hold_minutes: int = field(default_factory=lambda: _env_int("ATLAS_LITE_FACTOR_HOLD_MIN", 90))
    vix_rise_max: float = field(default_factory=lambda: _env_float("ATLAS_LITE_FACTOR_VIX_RISE_MAX", 0.05))
    max_entries_per_day: int = field(default_factory=lambda: _env_int("ATLAS_LITE_FACTOR_MAX_TRADES_DAY", 3))
    # 0 disables the day stop.
    daily_loss_stop: float = field(
        default_factory=lambda: -abs(_env_float("ATLAS_LITE_FACTOR_DAILY_LOSS_STOP", 2000.0))
    )
    position: FactorPosition | None = None
    traded_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    stops_today: int = 0
    free_at: datetime | None = None
    side_block: dict[str, datetime] = field(default_factory=dict)
    side_locked: set[str] = field(default_factory=set)
    last_reject: str | None = None
    last_signal: dict[str, Any] | None = None
    _vix_base: tuple[str, float] | None = None
    _oi_base: tuple[str, float] | None = None
    _open_minute_key: str = ""
    _open_minute: dict[str, Any] | None = None
    _decided_minute: str = ""
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_factor"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._restore()

    # ---- ledger ----

    def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event.setdefault("strategy", STRATEGY)
        event.setdefault("live_orders", False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError as exc:
            self._log.warning("factor ledger write failed: %s", exc)
            return None
        return event

    def _restore(self, now: datetime | None = None) -> None:
        """Rebuild today's counters, blocks and any open position from the ledger."""
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        events: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("strategy") in (None, STRATEGY):
                events.append(ev)
        open_ev: dict[str, Any] | None = None
        for ev in events:
            kind = ev.get("event")
            if kind == "open":
                open_ev = ev
            elif kind == "close":
                open_ev = None
        today = (now or datetime.now(IST)).strftime("%Y-%m-%d")
        if open_ev is not None:
            today = str(open_ev.get("day") or today)
        self.traded_day = today
        for ev in events:
            if str(ev.get("day") or "") != today:
                continue
            if ev.get("event") == "open":
                self.entries_today += 1
            elif ev.get("event") == "close":
                closed_at = _parse_ist(str(ev.get("ts") or ""))
                pnl = ev.get("pnl")
                if pnl is not None:
                    self.day_pnl = round(self.day_pnl + float(pnl), 2)
                if closed_at is not None:
                    self._after_close(
                        closed_at,
                        str(ev.get("side") or ""),
                        str(ev.get("reason") or ""),
                        float(pnl) if pnl is not None else None,
                    )
        if open_ev is not None:
            self.position = FactorPosition(
                day=str(open_ev.get("day") or ""),
                atm=int(open_ev.get("atm") or 0),
                side=str(open_ev.get("side") or "ce"),
                symbol=str(open_ev.get("symbol") or ""),
                qty=int(open_ev.get("qty") or self.lot_size),
                lots=int(open_ev.get("lots") or self.lots),
                entry=float(open_ev.get("entry") or 0.0),
                target=float(open_ev.get("target") or 0.0),
                stop=float(open_ev.get("stop") or 0.0),
                hold_until=str(open_ev.get("hold_until") or ""),
                opened_at=str(open_ev.get("ts") or ""),
                charges_open=float(open_ev.get("charges") or 0.0),
                reason=str(open_ev.get("reason") or ""),
                last_px=float(open_ev.get("entry") or 0.0) or None,
            )

    # ---- book rules ----

    def _after_close(self, now: datetime, side: str, reason: str, pnl: float | None) -> None:
        # Whole minutes, as the tape engine: exit minute + N vs the signal bar's minute.
        now = now.replace(second=0, microsecond=0)
        self.free_at = now + timedelta(minutes=EXIT_GAP_MIN)
        if reason == "stop":
            self.stops_today += 1
            self.side_block[side] = now + timedelta(minutes=BLOCK_AFTER_STOP_MIN)
            if (pnl is not None and pnl <= LOCK_STOP_LOSS) or self.stops_today >= LOCK_STOPS:
                self.side_locked.add(side)
        elif pnl is not None and pnl < 0:
            self.side_block[side] = now + timedelta(minutes=BLOCK_AFTER_LOSS_MIN)
        else:
            self.side_block[side] = now + timedelta(minutes=BLOCK_AFTER_WIN_MIN)

    def _new_day(self, day: str) -> None:
        self.traded_day = day
        self.entries_today = 0
        self.day_pnl = 0.0
        self.stops_today = 0
        self.free_at = None
        self.side_block = {}
        self.side_locked = set()
        self.last_signal = None
        self.last_reject = None

    def _book_reject(self, now: datetime, side: str) -> str | None:
        if self.daily_loss_stop and self.day_pnl <= float(self.daily_loss_stop):
            return "daily_loss_stop"
        if self.entries_today >= int(self.max_entries_per_day):
            return "max_trades_day"
        if self.free_at is not None and now < self.free_at:
            return "exit_gap"
        if side in self.side_locked:
            return f"{side}_locked"
        until = self.side_block.get(side)
        if until is not None and now < until:
            return f"{side}_blocked"
        return None

    def _note_bases(self, now: datetime, vix: float | None, vix_open: float | None, fut_oi: float | None) -> None:
        day = now.strftime("%Y-%m-%d")
        if not hm_ge(now, (9, 15)):
            return
        opening = hm_le(now, BASE_BY)
        if vix_open is not None and vix_open > 0:
            self._vix_base = (day, float(vix_open))
        elif opening and vix is not None and vix > 0 and (self._vix_base is None or self._vix_base[0] != day):
            self._vix_base = (day, float(vix))
        # Kite has no OI open; a restart after BASE_BY logs no OI change rather than a wrong one.
        if opening and fut_oi is not None and fut_oi > 0 and (self._oi_base is None or self._oi_base[0] != day):
            self._oi_base = (day, float(fut_oi))

    def _vix_rise(self, day: str, vix: float | None) -> float | None:
        if vix is None or self._vix_base is None or self._vix_base[0] != day:
            return None
        return vix / self._vix_base[1] - 1.0

    def _oi_chg(self, day: str, fut_oi: float | None) -> float | None:
        if fut_oi is None or self._oi_base is None or self._oi_base[0] != day:
            return None
        return fut_oi / self._oi_base[1] - 1.0

    # ---- open / close ----

    def _open(
        self,
        now: datetime,
        book: QuoteSource | None,
        *,
        side: str,
        symbol: str,
        atm: int,
        bar_at: datetime,
        signal: dict[str, Any],
    ) -> dict[str, Any] | None:
        entry = quote_ltp(book.get(symbol)) if book is not None else None
        if entry is None or entry <= 0:
            self.last_reject = "no_quote"
            return None
        qty = int(self.lots) * int(self.lot_size)
        charges_open = float(kite_nfo_charges([(entry, qty, "buy")])["total"])
        pos = FactorPosition(
            day=now.strftime("%Y-%m-%d"),
            atm=int(atm),
            side=side,
            symbol=symbol,
            qty=qty,
            lots=int(self.lots),
            entry=round(entry, 2),
            target=round(entry * (1.0 + self.target_pct), 2),
            stop=round(entry * (1.0 - self.stop_pct), 2),
            # Tape: entry at the signal bar's close, time exit on the close of bar + hold.
            hold_until=(
                bar_at.replace(second=0, microsecond=0)
                + timedelta(minutes=int(self.hold_minutes), seconds=59)
            ).isoformat(),
            opened_at=now.isoformat(),
            charges_open=round(charges_open, 2),
            reason=f"trend {signal['move']:+.0f}pt vs open",
            last_px=round(entry, 2),
        )
        event = self._append(
            {
                "event": "open",
                "ts": now.isoformat(),
                "day": pos.day,
                "atm": pos.atm,
                "side": pos.side,
                "style": "long",
                "symbol": pos.symbol,
                "qty": pos.qty,
                "lots": pos.lots,
                "lot_size": self.lot_size,
                "entry": pos.entry,
                "target": pos.target,
                "stop": pos.stop,
                "hold_until": pos.hold_until,
                "charges": pos.charges_open,
                "reason": pos.reason,
                "gates": f"trend>={self.trend_pts:g}pt vix_rise<{self.vix_rise_max:.0%}",
                **signal,
            }
        )
        if event is None:
            return None
        self.position = pos
        self.entries_today += 1
        self.traded_day = pos.day
        self.last_reject = None
        return event

    def _close(
        self,
        now: datetime,
        book: QuoteSource | None,
        reason: str,
        *,
        exit_px: float | None = None,
        spot: float | None = None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        if exit_px is None and book is not None:
            exit_px = quote_ltp(book.get(pos.symbol)) or pos.last_px
        # Closed after its own session ended: the real exit is unknown, so no P&L is booked.
        overnight = reason in ("session_gap", "no_session")
        marked = exit_px is not None and exit_px > 0 and not overnight
        last_seen = round(float(exit_px), 2) if exit_px else None
        exit_px = round(float(exit_px), 2) if marked else pos.entry
        charges_close = float(kite_nfo_charges([(exit_px, pos.qty, "sell")])["total"]) if marked else 0.0
        charges = round(pos.charges_open + charges_close, 2)
        gross = round((exit_px - pos.entry) * pos.qty, 2) if marked else None
        pnl = round(float(gross) - charges, 2) if gross is not None else None
        if pnl is not None and pos.day == now.strftime("%Y-%m-%d"):
            self.day_pnl = round(self.day_pnl + pnl, 2)
        event = self._append(
            {
                "event": "close",
                "ts": now.isoformat(),
                "day": pos.day,
                "atm": pos.atm,
                "side": pos.side,
                "style": "long",
                "symbol": pos.symbol,
                "qty": pos.qty,
                "entry": pos.entry,
                "exit": exit_px,
                "target": pos.target,
                "stop": pos.stop,
                "opened_at": pos.opened_at,
                "pnl_gross": gross,
                "charges": charges,
                "pnl": pnl,
                "pnl_known": pnl is not None,
                "exit_last_seen": last_seen if overnight else None,
                "reason": reason,
                "day_pnl": round(self.day_pnl, 2),
                "spot": spot,
            }
        )
        if event is None:
            return None
        self.position = None
        self._after_close(now, pos.side, reason, pnl)
        return event

    def _maybe_exit(self, now: datetime, book: QuoteSource | None, *, spot: float | None) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        session_due = hm_ge(now, SQUARE_OFF)
        hold = _parse_ist(pos.hold_until) or (
            (_parse_ist(pos.opened_at) or now) + timedelta(minutes=int(self.hold_minutes))
        )
        px = quote_ltp(book.get(pos.symbol)) if book is not None else None
        if px is None or px <= 0:
            # No quote now (strike left the subscribed chain): leave on time at the
            # last seen price, as the tape engine does. Target/stop need a live print.
            if session_due:
                return self._close(now, book, "square_off", exit_px=pos.last_px, spot=spot)
            if now >= hold:
                return self._close(now, book, "time", exit_px=pos.last_px, spot=spot)
            return None
        pos.last_px = px
        if session_due:
            return self._close(now, book, "square_off", exit_px=px, spot=spot)
        if px >= pos.target:
            return self._close(now, book, "target", exit_px=px, spot=spot)
        if px <= pos.stop:
            return self._close(now, book, "stop", exit_px=px, spot=spot)
        if now >= hold:
            return self._close(now, book, "time", exit_px=px, spot=spot)
        return None

    # ---- frame ----

    def _finish_minute(self, now: datetime, ctx: dict[str, Any]) -> dict[str, Any] | None:
        """Keep the latest values of this minute. Return the previous minute once it has closed."""
        key = now.strftime("%Y-%m-%dT%H:%M")
        closed = self._open_minute if self._open_minute_key and key != self._open_minute_key else None
        self._open_minute_key = key
        self._open_minute = {"key": key, "as_of": now, **ctx}
        return closed

    def on_frame(
        self,
        *,
        now: datetime,
        book: QuoteSource | None,
        spot: float | None,
        spot_open: float | None,
        vix: float | None = None,
        vix_open: float | None = None,
        fut_oi: float | None = None,
        ce_symbol: str | None,
        pe_symbol: str | None,
        atm: int | None,
        allow_new_entries: bool = True,
    ) -> dict[str, Any] | None:
        day = now.strftime("%Y-%m-%d")
        # Off-session quotes are the last session's, so nothing here is a signal.
        # A carried position leaves at its last seen price, not today's gap.
        if not _is_nse_trading_day(now.date()):
            if self.position is None:
                return None
            return self._close(now, book, "no_session", exit_px=self.position.last_px, spot=spot)
        if self.position is not None and self.position.day != day:
            return self._close(now, book, "session_gap", exit_px=self.position.last_px, spot=spot)
        if self.traded_day != day and self.position is None:
            self._new_day(day)
        self._note_bases(now, vix, vix_open, fut_oi)
        bar = self._finish_minute(
            now,
            {
                "spot": spot,
                "spot_open": spot_open,
                "vix": vix,
                "fut_oi": fut_oi,
                "ce_symbol": ce_symbol,
                "pe_symbol": pe_symbol,
                "atm": atm,
            },
        )
        if self.position is not None:
            return self._maybe_exit(now, book, spot=spot)
        if bar is None or bar["key"] == self._decided_minute:
            return None
        self._decided_minute = bar["key"]
        bar_at: datetime = bar["as_of"]
        # Only the minute that just closed; a stalled loop must not trade an old bar.
        if (bar_at + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M") != now.strftime("%Y-%m-%dT%H:%M"):
            return None
        if not hm_ge(bar_at, self.entry_after) or not hm_le(bar_at, self.entry_until) or hm_ge(now, SQUARE_OFF):
            return None
        close, open_px = bar["spot"], bar["spot_open"]
        if close is None or not open_px:
            self.last_reject = "no_spot_open"
            return None
        move = float(close) - float(open_px)
        if abs(move) < float(self.trend_pts):
            self.last_reject = None
            return None
        side = "ce" if move > 0 else "pe"
        vix_rise = self._vix_rise(day, bar["vix"])
        oi_chg = self._oi_chg(day, bar["fut_oi"])
        self.last_signal = {
            "bar": bar["key"][11:],
            "side": side,
            "move": round(move, 2),
            "vix_rise": None if vix_rise is None else round(vix_rise, 4),
            "oi_chg_open": None if oi_chg is None else round(oi_chg, 4),
        }
        if not allow_new_entries:
            self.last_reject = "gate_paused"
            return None
        if vix_rise is not None and vix_rise >= float(self.vix_rise_max):
            self.last_reject = "vix_rising"
            return None
        reject = self._book_reject(bar_at.replace(second=0, microsecond=0), side)
        if reject is not None:
            self.last_reject = reject
            return None
        symbol = bar["ce_symbol"] if side == "ce" else bar["pe_symbol"]
        if not symbol or bar["atm"] is None:
            self.last_reject = "no_atm"
            return None
        signal = {
            "bar": bar["key"][11:],
            "move": round(move, 2),
            "spot": close,
            "spot_open": open_px,
            "vix": bar["vix"],
            "vix_rise": self.last_signal["vix_rise"],
            "fut_oi": bar["fut_oi"],
            "oi_chg_open": self.last_signal["oi_chg_open"],
        }
        return self._open(
            now, book, side=side, symbol=str(symbol), atm=int(bar["atm"]), bar_at=bar_at, signal=signal
        )

    def snapshot(self, book: QuoteSource | None = None, *, spot: float | None = None) -> dict[str, Any]:
        pos = self.position
        pos_body = None
        if pos is not None:
            pos_body = {
                "side": pos.side,
                "style": "long",
                "symbol": pos.symbol,
                "atm": pos.atm,
                "entry": pos.entry,
                "target": pos.target,
                "stop": pos.stop,
                "mark": quote_ltp(book.get(pos.symbol)) if book is not None else None,
                "hold_until": pos.hold_until,
                "reason": pos.reason,
            }
        return {
            "ok": True,
            "mode": "paper",
            "live_orders": False,
            "book": STRATEGY,
            "name": "Factor paper",
            "enabled": True,
            "rules": {
                "trend_pts": self.trend_pts,
                "entry": f"{self.entry_after[0]:02d}:{self.entry_after[1]:02d}-"
                f"{self.entry_until[0]:02d}:{self.entry_until[1]:02d}",
                "target_pct": self.target_pct,
                "stop_pct": self.stop_pct,
                "hold_minutes": self.hold_minutes,
                "vix_rise_max": self.vix_rise_max,
            },
            "position": pos_body,
            "entries_today": self.entries_today,
            "max_entries_per_day": self.max_entries_per_day,
            "day_pnl": self.day_pnl,
            "daily_loss_stop": self.daily_loss_stop,
            "side_locked": sorted(self.side_locked),
            "last_signal": self.last_signal,
            "last_reject": self.last_reject,
            "spot": spot,
        }
