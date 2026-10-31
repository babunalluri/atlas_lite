"""Scalper v3 — afternoon ATM straddle sale on NIFTY. Paper only, never Kite orders.

Rules, as tested in scripts/bt_short_premium.py ("straddle 13:00 tp30/sl30 not_expiry"):

- 13:00 on a trading day that is not the weekly expiry: sell the ATM call and put, 1 lot.
- Take profit when buying back costs <= 70% of the credit (30% of the credit earned).
- Stop when buying back costs >= 130% of the credit (a loss of 30% of the credit).
- Otherwise close at 15:14. Flat every night. A first frame after 15:20 (engine down at
  15:14) closes with P&L unknown, never at stale post-close quotes.

Walk-forward test on the Kite year (estimated premiums): +₹241/trade, PF 1.71, 160 days;
real recorded premiums: +₹256/trade over 16 days. Expiry days are skipped: real fills
lost far more there than the premium model predicted.

Each entry logs the dealer-gamma regime at that moment (atlas_lite/gamma_regime.py), for
splitting paper results by regime later. Logging only: the regime never blocks a trade.

Fills are paper: sell at bid, buy at ask when quotes have them, else last price -/+ SLIP_PTS.
Kite charges at current rates. A position left open past 15:14 (engine down) is closed on
the next frame with P&L unknown.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from atlas_lite.agent_gates import _is_nse_trading_day
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger
from atlas_lite.metrics import quote_ask, quote_bid, quote_ltp
from atlas_lite.minute_bars import hm_ge

STRATEGY = "scalper_v3"
LEDGER_NAME = "paper_scalper_v3.jsonl"
ENTRY_AT = (13, 0)
ENTRY_LATEST = (13, 5)  # a late first frame may still enter; later than this skips the day
SQUARE_OFF = (15, 14)
SQUARE_OFF_LATE = (15, 20)  # first frame after this (engine was down at 15:14): exit unknown
TAKE_PROFIT = 0.30
STOP_LOSS = 0.30
SLIP_PTS = 1.0
DEFAULT_LOT_SIZE = 65


def paper_scalper_v3_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_SCALPER_V3", "1").strip().lower()
    return raw in ("1", "true", "yes")


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


@dataclass
class V3Position:
    day: str
    opened_at: str
    atm: int
    ce_symbol: str
    pe_symbol: str
    qty: int
    credit_ltp: float  # ce + pe last prices at entry: TP/stop are measured against this
    credit_fill: float
    charges_open: float
    ce_mark: float | None = None
    pe_mark: float | None = None


@dataclass
class PaperScalperV3:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = 1
    take_profit: float = TAKE_PROFIT
    stop_loss: float = STOP_LOSS
    position: V3Position | None = None
    traded_day: str = ""  # one entry per day
    last_reject: str | None = None
    last_gamma: dict[str, Any] | None = None
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_scalper_v3"), repr=False)

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
            self._log.warning("scalper_v3 ledger write failed: %s", exc)
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
            if ev.get("strategy") != STRATEGY:
                continue
            if ev.get("event") == "open":
                open_ev = ev
                self.traded_day = str(ev.get("day") or "")
                self.last_gamma = ev.get("gamma")
            elif ev.get("event") == "close":
                open_ev = None
        if open_ev is not None:
            self.position = V3Position(
                day=str(open_ev["day"]),
                opened_at=str(open_ev["ts"]),
                atm=int(open_ev["atm"]),
                ce_symbol=str(open_ev["ce_symbol"]),
                pe_symbol=str(open_ev["pe_symbol"]),
                qty=int(open_ev.get("qty") or self.qty),
                credit_ltp=float(open_ev["credit_ltp"]),
                credit_fill=float(open_ev["credit_fill"]),
                charges_open=float(open_ev["charges_open"]),
            )

    @staticmethod
    def _fill(row: dict[str, Any] | None, selling: bool) -> float | None:
        ltp = quote_ltp(row)
        if ltp is None or ltp <= 0:
            return None
        if selling:
            bid = quote_bid(row)
            return bid if bid and bid > 0 else max(0.05, ltp - SLIP_PTS)
        ask = quote_ask(row)
        return ask if ask and ask > 0 else ltp + SLIP_PTS

    def _open(
        self,
        now: datetime,
        book: QuoteSource,
        atm: int,
        ce_symbol: str,
        pe_symbol: str,
        gamma: Callable[[], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any] | None:
        ce_row, pe_row = book.get(ce_symbol), book.get(pe_symbol)
        ce, pe = quote_ltp(ce_row), quote_ltp(pe_row)
        ce_fill, pe_fill = self._fill(ce_row, True), self._fill(pe_row, True)
        if not ce or not pe or ce_fill is None or pe_fill is None:
            self.last_reject = "no_atm_quote"
            return None
        charges_open = round(float(kite_nfo_charges([(ce_fill, self.qty, "sell"), (pe_fill, self.qty, "sell")])["total"]), 2)
        try:
            regime = gamma() if gamma is not None else None
        except Exception as exc:  # noqa: BLE001 - a logging field must never block the entry
            self._log.warning("scalper_v3 gamma reading failed: %s", exc)
            regime = None
        pos = V3Position(
            day=now.strftime("%Y-%m-%d"),
            opened_at=now.isoformat(),
            atm=int(atm),
            ce_symbol=ce_symbol,
            pe_symbol=pe_symbol,
            qty=self.qty,
            credit_ltp=round(ce + pe, 2),
            credit_fill=round(ce_fill + pe_fill, 2),
            charges_open=charges_open,
            ce_mark=ce,
            pe_mark=pe,
        )
        ev = self._append(
            {
                "event": "open",
                "ts": now.isoformat(),
                "day": pos.day,
                "side": "short",
                "style": "short",
                "atm": pos.atm,
                "symbol": f"{ce_symbol},{pe_symbol}",
                "ce_symbol": ce_symbol,
                "pe_symbol": pe_symbol,
                "qty": pos.qty,
                "ce": ce,
                "pe": pe,
                "credit_ltp": pos.credit_ltp,
                "credit_fill": pos.credit_fill,
                "entry": pos.credit_fill,
                "target": round(pos.credit_ltp * (1 - self.take_profit), 2),
                "stop": round(pos.credit_ltp * (1 + self.stop_loss), 2),
                "charges_open": charges_open,
                "gamma": regime,
                "reason": "13:00 ATM straddle sale, non-expiry day",
            }
        )
        if ev is None:
            return None
        self.position = pos
        self.traded_day = pos.day
        self.last_reject = None
        self.last_gamma = regime
        return ev

    def _close(self, now: datetime, book: QuoteSource, reason: str) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        known = reason in ("target", "stop", "square_off")
        prices = []
        for sym, mark in ((pos.ce_symbol, pos.ce_mark), (pos.pe_symbol, pos.pe_mark)):
            px = self._fill(book.get(sym), selling=False) if reason in ("target", "stop", "square_off") else None
            if px is None and known and mark is not None:
                px = mark + SLIP_PTS  # quote blinked this frame: today's last price, bought at a worse fill
            elif px is None:
                px = mark
                known = False
            prices.append(px)
        fills = [(px, pos.qty, "buy") for px in prices if px]
        charges = round(pos.charges_open + float(kite_nfo_charges(fills)["total"]), 2)
        debit = sum(px for px in prices if px is not None)
        gross = round((pos.credit_fill - debit) * pos.qty, 2) if known else None
        pnl = round(gross - charges, 2) if gross is not None else None
        ev = self._append(
            {
                "event": "close",
                "ts": now.isoformat(),
                "day": pos.day,
                "side": "short",
                "style": "short",
                "atm": pos.atm,
                "symbol": f"{pos.ce_symbol},{pos.pe_symbol}",
                "opened_at": pos.opened_at,
                "qty": pos.qty,
                "entry": pos.credit_fill,
                "exit": round(debit, 2) if known else None,
                "pnl_gross": gross,
                "charges": charges,
                "pnl": pnl,
                "pnl_known": known,
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
        gamma: Callable[[], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any] | None:
        """``gamma`` returns the dealer-gamma reading; called once, at entry, for the log."""
        today = now.strftime("%Y-%m-%d")
        pos = self.position
        if pos is not None:
            if pos.day != today:  # never squared off (engine down): exit unknown
                return self._close(now, book, "session_gap")
            ce, pe = quote_ltp(book.get(pos.ce_symbol)), quote_ltp(book.get(pos.pe_symbol))
            if ce is not None:
                pos.ce_mark = ce
            if pe is not None:
                pos.pe_mark = pe
            if hm_ge(now, SQUARE_OFF_LATE):
                return self._close(now, book, "square_off_late")
            if hm_ge(now, SQUARE_OFF):
                return self._close(now, book, "square_off")
            # 1: a leg whose quote blinked keeps its last price, as the backtest carries it forward.
            if pos.ce_mark is None or pos.pe_mark is None:
                return None
            cost = pos.ce_mark + pos.pe_mark
            if cost <= pos.credit_ltp * (1 - self.take_profit):
                return self._close(now, book, "target")
            if cost >= pos.credit_ltp * (1 + self.stop_loss):
                return self._close(now, book, "stop")
            return None
        if self.traded_day == today or not _is_nse_trading_day(now.date()):
            return None
        if not hm_ge(now, ENTRY_AT) or hm_ge(now, (ENTRY_LATEST[0], ENTRY_LATEST[1] + 1)):
            return None
        if expiry is None or atm is None or not ce_symbol or not pe_symbol:
            self.last_reject = "no_atm"
            return None
        if expiry == now.date():
            self.last_reject = "expiry_day"  # re-checked each frame; never locks the day
            return None
        if not allow_entry:
            self.last_reject = "gate_paused"
            return None
        return self._open(now, book, atm, ce_symbol, pe_symbol, gamma)

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        pos = self.position
        body: dict[str, Any] = {
            "ok": True,
            "mode": "paper",
            "live_orders": False,
            "book": STRATEGY,
            "name": "Scalper v3",
            "enabled": True,
            "last_reject": self.last_reject,
            "gamma": self.last_gamma,
            "position": None,
        }
        if pos is not None:
            ce = (quote_ltp(book.get(pos.ce_symbol)) if book is not None else None) or pos.ce_mark
            pe = (quote_ltp(book.get(pos.pe_symbol)) if book is not None else None) or pos.pe_mark
            cost = None if ce is None or pe is None else ce + pe
            close_ch = (
                float(kite_nfo_charges([(ce, pos.qty, "buy"), (pe, pos.qty, "buy")])["total"]) if cost is not None else 0.0
            )
            body["position"] = {
                "atm": pos.atm,
                "credit": pos.credit_fill,
                "cost_now": None if cost is None else round(cost, 2),
                "target": round(pos.credit_ltp * (1 - self.take_profit), 2),
                "stop": round(pos.credit_ltp * (1 + self.stop_loss), 2),
                # Net of entry charges and estimated exit charges, like the ledger's pnl.
                "open_pnl": None if cost is None else round((pos.credit_fill - cost) * pos.qty - pos.charges_open - close_ch, 2),
            }
        return body
