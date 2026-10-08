"""Paper ICT book — ATM option in the spot-signal direction (never Kite orders).

Long bias buys the ATM call. Short bias buys the ATM put. The stop and target
are on **spot** (sweep extreme / opposing liquidity). The option is marked out
when spot trades through those levels, after ``max_holding_bars`` closed 5m
bars, or at 15:14.

Ledger: ``paper_ict.jsonl``. On by default; disable with ``ATLAS_LITE_PAPER_ICT=0``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from atlas_lite.ict import ICTConfig, aggregate_bars, drop_open_bucket, generate_entry
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger, ist_now
from atlas_lite.metrics import quote_ask, quote_bid, quote_ltp
from atlas_lite.minute_bars import hm_ge, hm_le

IST = ZoneInfo("Asia/Kolkata")

STRATEGY = "ict"
CAPITAL = 200_000.0
LOTS = 1
DEFAULT_LOT_SIZE = 65
MAX_ENTRIES_PER_DAY = 0  # 0 = no daily cap
ENTRY_AFTER = (9, 35)
ENTRY_UNTIL = (14, 0)
SQUARE_OFF = (15, 14)
EXEC_MINUTES = 5
HTF_MINUTES = 15
COOLDOWN_MIN = 5
# Same light slip as the short iron condor when the book has no bid/ask.
SLIP_PCT = 0.02
SLIP_MIN = 0.05
PX_FLOOR = 0.05

Side = Literal["ce", "pe"]


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def paper_ict_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_ICT", "1").strip().lower()
    return raw in ("1", "true", "yes")


def in_ict_entry_window(now: datetime) -> bool:
    return hm_ge(now, ENTRY_AFTER) and hm_le(now, ENTRY_UNTIL) and not hm_ge(now, SQUARE_OFF)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quote_row(book: QuoteSource | None, symbol: str) -> dict[str, Any] | None:
    if book is None or not symbol:
        return None
    row = book.get(symbol)
    return row if isinstance(row, dict) else None


def _buy_px(book: QuoteSource | None, symbol: str) -> float | None:
    """Price paid to buy — ask, else LTP plus a small slip."""
    row = _quote_row(book, symbol)
    ask = quote_ask(row)
    if ask is not None and ask > 0:
        return round(float(ask), 2)
    ltp = quote_ltp(row)
    if ltp is None or ltp <= 0:
        return None
    return round(float(ltp) + max(SLIP_MIN, SLIP_PCT * float(ltp)), 2)


def _sell_px(book: QuoteSource | None, symbol: str) -> float | None:
    """Price received to sell — bid, else LTP minus a small slip."""
    row = _quote_row(book, symbol)
    bid = quote_bid(row)
    if bid is not None and bid > 0:
        return round(float(bid), 2)
    ltp = quote_ltp(row)
    if ltp is None or ltp <= 0:
        return None
    return round(max(PX_FLOOR, float(ltp) - max(SLIP_MIN, SLIP_PCT * float(ltp))), 2)


def _live_rr(bias: str, spot: float, stop: float, target: float) -> float | None:
    """Reward/risk from the spot we would actually enter, not the gap-edge limit."""
    if bias == "LONG":
        risk = spot - stop
        reward = target - spot
    elif bias == "SHORT":
        risk = stop - spot
        reward = spot - target
    else:
        return None
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


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


@dataclass
class IctPosition:
    day: str
    atm: int
    side: Side
    bias: str
    symbol: str
    ce_symbol: str
    pe_symbol: str
    qty: int
    lots: int
    entry: float
    spot_entry: float
    spot_stop: float
    spot_target: float
    risk_reward: float
    hold_until: str
    opened_at: str
    signal_minute: str
    charges_open: float


@dataclass
class PaperICT:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = LOTS
    capital: float = CAPITAL
    max_entries_per_day: int = MAX_ENTRIES_PER_DAY
    config: ICTConfig = field(default_factory=ICTConfig)
    position: IctPosition | None = None
    traded_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    eod_written: bool = False
    last_event: dict[str, Any] | None = None
    last_exit_at: datetime | None = None
    last_signal_minute: str = ""
    last_reject: str = ""
    used_setups: set[str] = field(default_factory=set)
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_ict"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restore()

    def _restore(self) -> None:
        if not self.path.is_file():
            return
        last_open: dict[str, Any] | None = None
        day = ""
        day_pnl = 0.0
        entries = 0
        eod_written = False
        last_exit_at: datetime | None = None
        last_signal = ""
        used: set[str] = set()
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
            if ev.get("strategy") not in (None, STRATEGY):
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
                last_signal = ""
                used = set()
                eod_written = False
            if kind == "open":
                last_open = ev
                entries += 1
                last_signal = str(ev.get("signal_minute") or "")
                setup_id = str(ev.get("setup_id") or "")
                if setup_id:
                    used.add(setup_id)
            elif kind == "close":
                last_open = None
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
        self.eod_written = eod_written
        self.last_exit_at = last_exit_at
        self.last_signal_minute = last_signal
        self.used_setups = used
        if not last_open:
            return
        side: Side = "pe" if last_open.get("side") == "pe" else "ce"
        self.position = IctPosition(
            day=str(last_open.get("day") or ""),
            atm=int(last_open.get("atm") or 0),
            side=side,
            bias=str(last_open.get("bias") or ""),
            symbol=str(last_open.get("symbol") or ""),
            ce_symbol=str(last_open.get("ce_symbol") or ""),
            pe_symbol=str(last_open.get("pe_symbol") or ""),
            qty=int(last_open.get("qty") or self.lots * self.lot_size),
            lots=int(last_open.get("lots") or self.lots),
            entry=float(last_open.get("entry") or 0),
            spot_entry=float(last_open.get("spot_entry") or 0),
            spot_stop=float(last_open.get("spot_stop") or last_open.get("stop") or 0),
            spot_target=float(last_open.get("spot_target") or last_open.get("target") or 0),
            risk_reward=float(last_open.get("risk_reward") or 0),
            hold_until=str(last_open.get("hold_until") or ""),
            opened_at=str(last_open.get("ts") or ""),
            signal_minute=str(last_open.get("signal_minute") or ""),
            charges_open=float(last_open.get("charges") or 0),
        )

    def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event = dict(event)
        event.setdefault("mode", "paper")
        event.setdefault("strategy", STRATEGY)
        event.setdefault("logged_at", ist_now())
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError as exc:
            self._log.warning("ict ledger write failed: %s", exc)
            return None
        self.last_event = event
        return event

    def _in_cooldown(self, now: datetime) -> bool:
        if self.last_exit_at is None:
            return False
        return now < self.last_exit_at + timedelta(minutes=COOLDOWN_MIN)

    def _frames(self, bars_1m: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        htf = drop_open_bucket(aggregate_bars(bars_1m, HTF_MINUTES), minutes=HTF_MINUTES, now=now)
        exe = drop_open_bucket(aggregate_bars(bars_1m, EXEC_MINUTES), minutes=EXEC_MINUTES, now=now)
        return htf, exe

    def on_frame(
        self,
        *,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource | None,
        ce_symbol: str | None,
        pe_symbol: str | None,
        atm: int | None,
        bars_1m: list[dict[str, Any]] | None = None,
        spot: float | None = None,
        allow_new_entries: bool = True,
    ) -> dict[str, Any] | None:
        day = now.strftime("%Y-%m-%d")
        if not _weekday(now):
            if self.position is not None:
                return self._flatten(now, book, "weekend", spot)
            return None
        if self.position is not None and self.position.day != day:
            return self._flatten(now, book, "session_gap", spot)
        if self.traded_day != day and self.position is None:
            self._seal(now)
            self.traded_day = day
            self.entries_today = 0
            self.day_pnl = 0.0
            self.eod_written = False
            self.last_signal_minute = ""
            self.used_setups = set()
        if self.position is not None:
            closed = self._maybe_exit(now, book, spot, bars_1m or [])
            if hm_ge(now, SQUARE_OFF):
                flat = self._flatten(now, book, "time", spot) if self.position is not None else None
                return flat or closed or self._seal(now)
            return closed
        if hm_ge(now, SQUARE_OFF):
            return self._seal(now)
        if not allow_new_entries:
            self.last_reject = "policy_gate"
            return None
        if int(self.max_entries_per_day) > 0 and self.entries_today >= int(self.max_entries_per_day):
            self.last_reject = "max_entries"
            return None
        if self._in_cooldown(now):
            self.last_reject = "cooldown"
            return None
        if not in_ict_entry_window(now):
            return None
        htf, exe = self._frames(bars_1m or [], now)
        if not exe:
            return None
        minute = str(exe[-1].get("t") or "")
        if minute and minute == self.last_signal_minute:
            return None
        sig = generate_entry(htf, exe, self.config)
        setup_id = str((sig.metadata or {}).get("setup_id") or "")
        if setup_id and setup_id in self.used_setups:
            self.last_reject = "setup_used"
            self.last_signal_minute = minute
            return None
        if sig.signal not in ("LONG", "SHORT"):
            self.last_reject = sig.reason
            self.last_signal_minute = minute
            return None
        opened = self._open(now, feed, book, ce_symbol, pe_symbol, atm, spot, sig, minute)
        if opened is None and minute:
            self.last_signal_minute = minute
        return opened

    def _open(
        self,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource | None,
        ce_symbol: str | None,
        pe_symbol: str | None,
        atm: int | None,
        spot: float | None,
        sig: Any,
        minute: str,
    ) -> dict[str, Any] | None:
        side: Side = "ce" if sig.signal == "LONG" else "pe"
        ce_sym = ce_symbol or str(feed.get("ce_symbol") or "")
        pe_sym = pe_symbol or str(feed.get("pe_symbol") or "")
        symbol = ce_sym if side == "ce" else pe_sym
        if not symbol or atm is None:
            self.last_reject = "missing_symbol"
            return None
        px = _buy_px(book, symbol)
        if px is None or px <= 0:
            raw = _f(feed.get("ce" if side == "ce" else "pe"))
            px = None if raw is None else round(raw + max(SLIP_MIN, SLIP_PCT * raw), 2)
        if px is None or px <= 0:
            self.last_reject = "no_premium"
            return None
        spot_px = spot if spot is not None else _f(feed.get("spot"))
        if spot_px is None:
            self.last_reject = "missing_spot"
            return None
        stop = float(sig.stop_loss)
        target = float(sig.target)
        live_rr = _live_rr(str(sig.signal), float(spot_px), stop, target)
        if live_rr is None or live_rr < float(self.config.minimum_rr):
            self.last_reject = "risk_reward_at_fill"
            return None
        qty = int(self.lots) * int(self.lot_size)
        charges = float(kite_nfo_charges([(float(px), qty, "buy")])["total"])
        hold_until = (now + timedelta(minutes=EXEC_MINUTES * int(self.config.max_holding_bars))).isoformat(
            timespec="seconds"
        )
        self.position = IctPosition(
            day=now.strftime("%Y-%m-%d"),
            atm=int(atm),
            side=side,
            bias=str(sig.signal),
            symbol=symbol,
            ce_symbol=ce_sym,
            pe_symbol=pe_sym,
            qty=qty,
            lots=int(self.lots),
            entry=round(float(px), 2),
            spot_entry=round(float(spot_px), 2),
            spot_stop=stop,
            spot_target=target,
            risk_reward=round(live_rr, 3),
            hold_until=hold_until,
            opened_at=now.isoformat(timespec="seconds"),
            signal_minute=minute,
            charges_open=charges,
        )
        self.entries_today += 1
        self.last_signal_minute = minute
        self.last_reject = ""
        setup_id = str((sig.metadata or {}).get("setup_id") or "")
        if setup_id:
            self.used_setups.add(setup_id)
        self.traded_day = self.position.day
        return self._append(
            {
                "event": "open",
                "ts": now.isoformat(timespec="seconds"),
                "day": self.position.day,
                "side": side,
                "bias": sig.signal,
                "symbol": symbol,
                "ce_symbol": ce_sym,
                "pe_symbol": pe_sym,
                "atm": int(atm),
                "qty": qty,
                "lots": int(self.lots),
                "entry": self.position.entry,
                "spot": round(float(spot_px), 2),
                "spot_entry": self.position.spot_entry,
                "spot_stop": self.position.spot_stop,
                "spot_target": self.position.spot_target,
                "stop": self.position.spot_stop,
                "target": self.position.spot_target,
                "risk_reward": self.position.risk_reward,
                "reason": sig.reason,
                "signal_minute": minute,
                "setup_id": str((sig.metadata or {}).get("setup_id") or ""),
                "hold_until": hold_until,
                "charges": round(charges, 2),
            }
        )

    def _spot_exit_reason(self, pos: IctPosition, now: datetime, spot: float | None) -> str | None:
        if spot is None:
            return None
        if pos.bias == "LONG":
            if spot <= pos.spot_stop:
                return "stop"
            if spot >= pos.spot_target:
                return "target"
        elif pos.bias == "SHORT":
            if spot >= pos.spot_stop:
                return "stop"
            if spot <= pos.spot_target:
                return "target"
        hold = _parse_ts(pos.hold_until)
        if hold is not None and now >= hold:
            return "time"
        if hm_ge(now, SQUARE_OFF):
            return "time"
        return None

    def _maybe_exit(
        self,
        now: datetime,
        book: QuoteSource | None,
        spot: float | None,
        bars_1m: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        reason = self._spot_exit_reason(pos, now, spot)
        if reason is None and bars_1m:
            _, exe = self._frames(bars_1m, now)
            if exe and str(exe[-1].get("t") or "") > pos.signal_minute:
                hi, lo = _f(exe[-1], "h"), _f(exe[-1], "l")
                if pos.bias == "LONG":
                    if lo is not None and lo <= pos.spot_stop:
                        reason = "stop"
                    elif hi is not None and hi >= pos.spot_target:
                        reason = "target"
                elif pos.bias == "SHORT":
                    if hi is not None and hi >= pos.spot_stop:
                        reason = "stop"
                    elif lo is not None and lo <= pos.spot_target:
                        reason = "target"
        if reason is None:
            return None
        return self._flatten(now, book, reason, spot)

    def _flatten(
        self,
        now: datetime,
        book: QuoteSource | None,
        reason: str,
        spot: float | None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        px = _sell_px(book, pos.symbol)
        if px is None or px <= 0:
            self.last_reject = "exit_no_premium"
            return None
        qty = pos.qty
        gross = round((float(px) - pos.entry) * qty, 2)
        close_ch = float(kite_nfo_charges([(float(px), qty, "sell")])["total"])
        pnl = round(gross - pos.charges_open - close_ch, 2)
        self.day_pnl = round(self.day_pnl + pnl, 2)
        self.position = None
        self.last_exit_at = now
        return self._append(
            {
                "event": "close",
                "ts": now.isoformat(timespec="seconds"),
                "day": pos.day,
                "side": pos.side,
                "bias": pos.bias,
                "symbol": pos.symbol,
                "qty": qty,
                "entry": pos.entry,
                "exit": round(float(px), 2),
                "spot": None if spot is None else round(float(spot), 2),
                "spot_stop": pos.spot_stop,
                "spot_target": pos.spot_target,
                "pnl_gross": gross,
                "charges": round(pos.charges_open + close_ch, 2),
                "pnl": pnl,
                "reason": reason,
                "day_pnl": self.day_pnl,
                "opened_at": pos.opened_at,
            }
        )

    def _seal(self, now: datetime) -> dict[str, Any] | None:
        if self.eod_written or self.position is not None:
            return None
        if self.entries_today == 0 and self.day_pnl == 0.0:
            self.eod_written = True
            return None
        if not hm_ge(now, SQUARE_OFF) and _weekday(now):
            return None
        ev = self._append(
            {
                "event": "day_pnl",
                "ts": now.isoformat(timespec="seconds"),
                "day": self.traded_day or now.strftime("%Y-%m-%d"),
                "trades": self.entries_today,
                "day_pnl": round(self.day_pnl, 2),
                "capital": self.capital,
            }
        )
        self.eod_written = True
        return ev

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        pos = self.position
        mtm = None
        if pos is not None and book is not None:
            px = _sell_px(book, pos.symbol)
            if px is not None:
                mtm = round((float(px) - pos.entry) * pos.qty - pos.charges_open, 2)
        return {
            "ok": True,
            "mode": "paper",
            "live_orders": False,
            "book": STRATEGY,
            "enabled": True,
            "capital": self.capital,
            "lots": self.lots,
            "entries_today": self.entries_today,
            "max_entries_per_day": self.max_entries_per_day,
            "day_pnl": round(self.day_pnl, 2),
            "last_reject": self.last_reject,
            "position": None
            if pos is None
            else {
                "side": pos.side,
                "bias": pos.bias,
                "symbol": pos.symbol,
                "entry": pos.entry,
                "spot_stop": pos.spot_stop,
                "spot_target": pos.spot_target,
                "mtm": mtm,
            },
        }
