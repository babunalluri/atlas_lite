"""Long straddle — morning ATM straddle purchase on NIFTY. Paper only, never Kite orders.

Rules, as tested on real recorded prices (scratch study, 2 Nov 2026):

- 09:16 on every trading day, expiry days included: buy the ATM call and put, 1 lot.
- Sell both at 13:00. No target, no stop: the tested rule, untuned.

Real recorded prices, 3 Sep - 30 Oct 2026 (37 days, ask in / bid out, Kite charges):
+₹1,767 a day at the 13:00 exit, 56% of days positive, worst day -₹4,141, t 2.8. The
window was unusually volatile (median 09:16-13:00 NIFTY move 111 pts vs 68 over the past
year; break-even is about 97), so this book exists to see whether it survives calmer months.
Rationale: intraday volatility is U-shaped; a straddle priced on a whole-day volatility is
cheap for the busy morning. Scalper v3 is the mirror image (sells at 13:00 into the quiet
afternoon).

Fills are paper: buy at ask, sell at bid when quotes have them, else last price +/- SLIP_PTS.
An exit that only happens after EXIT_LATE (engine was down at 13:00) is still priced, but
marked late, so the analysis can leave it out; one first seen after SQUARE_OFF_LATE, or on a
later day, closes with P&L unknown, never at stale post-close quotes.

The strategy name is the retired iron-fly overlay's ("long_straddle"), which never traded;
its two empty day rows stay in paper_trades.jsonl. This book has its own ledger.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from atlas_lite.agent_gates import _is_nse_trading_day
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger
from atlas_lite.metrics import quote_ask, quote_bid, quote_ltp
from atlas_lite.minute_bars import hm_ge

STRATEGY = "long_straddle"
LEDGER_NAME = "paper_long_straddle.jsonl"
ENTRY_AT = (9, 16)
ENTRY_LATEST = (9, 21)  # a late first frame may still enter; later than this skips the day
EXIT_AT = (13, 0)
EXIT_LATE = (13, 6)  # an exit first seen after this is priced but marked late
SQUARE_OFF_LATE = (15, 20)  # first frame after this (engine down all afternoon): exit unknown
SLIP_PTS = 1.0
DEFAULT_LOT_SIZE = 65


def paper_long_straddle_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_LONG_STRADDLE", "1").strip().lower()
    return raw in ("1", "true", "yes")


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


@dataclass
class LongStraddlePosition:
    day: str
    opened_at: str
    atm: int
    ce_symbol: str
    pe_symbol: str
    qty: int
    debit_fill: float  # ce + pe paid (ask), per unit
    charges_open: float
    expiry_day: bool
    ce_mark: float | None = None
    pe_mark: float | None = None


@dataclass
class PaperLongStraddle:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = 1
    position: LongStraddlePosition | None = None
    traded_day: str = ""  # one entry per day
    last_reject: str | None = None
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_long_straddle"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._restore()

    @property
    def qty(self) -> int:
        return int(self.lots) * int(self.lot_size)

    def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event.setdefault("strategy", STRATEGY)
        event.setdefault("live_orders", False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError as exc:
            self._log.warning("long_straddle ledger write failed: %s", exc)
            return None
        return event

    def _restore(self) -> None:
        if not self.path.is_file():
            return
        open_ev: dict[str, Any] | None = None
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict) or ev.get("strategy") != STRATEGY:
                continue
            if ev.get("event") == "open":
                open_ev = ev
                self.traded_day = str(ev.get("day") or "")
            elif ev.get("event") == "close":
                open_ev = None
        if open_ev is not None:
            self.position = LongStraddlePosition(
                day=str(open_ev["day"]),
                opened_at=str(open_ev["ts"]),
                atm=int(open_ev["atm"]),
                ce_symbol=str(open_ev["ce_symbol"]),
                pe_symbol=str(open_ev["pe_symbol"]),
                qty=int(open_ev.get("qty") or self.qty),
                debit_fill=float(open_ev["debit_fill"]),
                charges_open=float(open_ev["charges_open"]),
                expiry_day=bool(open_ev.get("expiry_day")),
            )

    @staticmethod
    def _fill(row: dict[str, Any] | None, buying: bool) -> float | None:
        ltp = quote_ltp(row)
        if ltp is None or ltp <= 0:
            return None
        if buying:
            ask = quote_ask(row)
            return ask if ask and ask > 0 else ltp + SLIP_PTS
        bid = quote_bid(row)
        return bid if bid and bid > 0 else max(0.05, ltp - SLIP_PTS)

    def _open(self, now: datetime, book: QuoteSource, atm: int, ce_symbol: str, pe_symbol: str,
              expiry_day: bool) -> dict[str, Any] | None:
        ce_row, pe_row = book.get(ce_symbol), book.get(pe_symbol)
        ce, pe = quote_ltp(ce_row), quote_ltp(pe_row)
        ce_fill, pe_fill = self._fill(ce_row, True), self._fill(pe_row, True)
        if not ce or not pe or ce_fill is None or pe_fill is None:
            self.last_reject = "no_atm_quote"
            return None
        charges_open = round(float(kite_nfo_charges([(ce_fill, self.qty, "buy"), (pe_fill, self.qty, "buy")])["total"]), 2)
        pos = LongStraddlePosition(
            day=now.strftime("%Y-%m-%d"),
            opened_at=now.isoformat(),
            atm=int(atm),
            ce_symbol=ce_symbol,
            pe_symbol=pe_symbol,
            qty=self.qty,
            debit_fill=round(ce_fill + pe_fill, 2),
            charges_open=charges_open,
            expiry_day=expiry_day,
            ce_mark=ce,
            pe_mark=pe,
        )
        ev = self._append(
            {
                "event": "open",
                "ts": now.isoformat(),
                "day": pos.day,
                "side": "long",
                "style": "long",
                "atm": pos.atm,
                "symbol": f"{ce_symbol},{pe_symbol}",
                "ce_symbol": ce_symbol,
                "pe_symbol": pe_symbol,
                "qty": pos.qty,
                "ce": ce,
                "pe": pe,
                "debit_fill": pos.debit_fill,
                "entry": pos.debit_fill,
                "charges_open": charges_open,
                "expiry_day": expiry_day,
                "reason": "09:16 ATM straddle purchase" + (" (expiry day)" if expiry_day else ""),
            }
        )
        if ev is None:
            return None
        self.position = pos
        self.traded_day = pos.day
        self.last_reject = None
        return ev

    def _close(self, now: datetime, book: QuoteSource, reason: str) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        known = reason in ("time", "time_late")
        prices = []
        for sym, mark in ((pos.ce_symbol, pos.ce_mark), (pos.pe_symbol, pos.pe_mark)):
            px = self._fill(book.get(sym), buying=False) if known else None
            if px is None and known and mark is not None:
                px = max(0.05, mark - SLIP_PTS)  # quote blinked this frame: today's last price, sold at a worse fill
            elif px is None:
                px = mark
                known = False
            prices.append(px)
        fills = [(px, pos.qty, "sell") for px in prices if px]
        charges = round(pos.charges_open + float(kite_nfo_charges(fills)["total"]), 2)
        credit = sum(px for px in prices if px is not None)
        gross = round((credit - pos.debit_fill) * pos.qty, 2) if known else None
        pnl = round(gross - charges, 2) if gross is not None else None
        ev = self._append(
            {
                "event": "close",
                "ts": now.isoformat(),
                "day": pos.day,
                "side": "long",
                "style": "long",
                "atm": pos.atm,
                "symbol": f"{pos.ce_symbol},{pos.pe_symbol}",
                "opened_at": pos.opened_at,
                "qty": pos.qty,
                "entry": pos.debit_fill,
                "exit": round(credit, 2) if known else None,
                "pnl_gross": gross,
                "charges": charges,
                "pnl": pnl,
                "pnl_known": known,
                "expiry_day": pos.expiry_day,
                "reason": reason,
            }
        )
        if ev is None:
            return None
        self.position = None
        return ev

    def on_frame(
        self,
        *,
        now: datetime,
        book: QuoteSource,
        expiry: date | None,
        atm: int | None,
        ce_symbol: str | None,
        pe_symbol: str | None,
        allow_entry: bool = True,
    ) -> dict[str, Any] | None:
        today = now.strftime("%Y-%m-%d")
        pos = self.position
        if pos is not None:
            if pos.day != today:  # never sold (engine down): exit unknown
                return self._close(now, book, "session_gap")
            ce, pe = quote_ltp(book.get(pos.ce_symbol)), quote_ltp(book.get(pos.pe_symbol))
            if ce is not None:
                pos.ce_mark = ce
            if pe is not None:
                pos.pe_mark = pe
            if hm_ge(now, SQUARE_OFF_LATE):
                return self._close(now, book, "square_off_late")
            if hm_ge(now, EXIT_LATE):
                return self._close(now, book, "time_late")
            if hm_ge(now, EXIT_AT):
                return self._close(now, book, "time")
            return None
        if self.traded_day == today or not _is_nse_trading_day(now.date()):
            return None
        if not hm_ge(now, ENTRY_AT) or hm_ge(now, (ENTRY_LATEST[0], ENTRY_LATEST[1] + 1)):
            return None
        if expiry is None or atm is None or not ce_symbol or not pe_symbol:
            self.last_reject = "no_atm"
            return None
        if not allow_entry:
            self.last_reject = "gate_paused"
            return None
        return self._open(now, book, atm, ce_symbol, pe_symbol, expiry == now.date())

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        pos = self.position
        body: dict[str, Any] = {
            "ok": True,
            "mode": "paper",
            "live_orders": False,
            "book": STRATEGY,
            "name": "Long straddle",
            "enabled": True,
            "last_reject": self.last_reject,
            "position": None,
        }
        if pos is not None:
            ce = (quote_ltp(book.get(pos.ce_symbol)) if book is not None else None) or pos.ce_mark
            pe = (quote_ltp(book.get(pos.pe_symbol)) if book is not None else None) or pos.pe_mark
            value = None if ce is None or pe is None else ce + pe
            close_ch = (
                float(kite_nfo_charges([(ce, pos.qty, "sell"), (pe, pos.qty, "sell")])["total"]) if value is not None else 0.0
            )
            body["position"] = {
                "atm": pos.atm,
                "debit": pos.debit_fill,
                "value_now": None if value is None else round(value, 2),
                "expiry_day": pos.expiry_day,
                # Net of entry charges and estimated exit charges, like the ledger's pnl.
                "open_pnl": None if value is None else round((value - pos.debit_fill) * pos.qty - pos.charges_open - close_ch, 2),
            }
        return body
