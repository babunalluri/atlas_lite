"""Same-day live orders for the expiry-day selling books, DRY RUN ONLY: the exact Kite orders each paper trade would
send are built and logged next to it, never sent. This module has no order-sending code; going live is a separate,
later change and the operator's call.

Books (closed the same day, intraday MIS; Zerodha squares off anything left at ~15:20):
  expiry_day_straddle (NIFTY, Tuesday) and sensex_expiry_day_strangle (SENSEX, Thursday).

For each paper event of those books:
  open      -> entry orders in the paper's order (wings bought first, then the short legs sold), LIMIT at the paper
               fill, then one stop-loss (SL) BUY per short leg held at the exchange: trigger = the paper stop level,
               limit 5% above it (an SL buy needs limit >= trigger). Plus a read-only Kite margin check for the
               entry basket against the funds available (/margins/basket, /user/margins), run off the engine loop.
  leg_stop  -> that leg's exchange SL fills (the paper exit price stands in for the fill).
  close     -> cancel the stop orders still open, then buy back the short legs / sell the wings, LIMIT at the paper
               exit price.
Rows go to data/expiry_dry_run.jsonl with the book's name; the dashboard card shows today's count and the margin.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from atlas_lite.log_util import get_logger

IST = ZoneInfo("Asia/Kolkata")
LEDGER_NAME = "expiry_dry_run.jsonl"
DRY_RUN_BOOKS = frozenset({"expiry_day_straddle", "sensex_expiry_day_strangle"})
PRODUCT = "MIS"
TICK = 0.05
SL_LIMIT_ABOVE = 0.05  # SL buy limit 5% above the trigger, so a fast move still fills
MARGIN_HEADROOM = 1.10  # funds must cover the basket margin + 10%


def expiry_dry_run_enabled() -> bool:
    """On by default; ATLAS_LITE_EXPIRY_DRY_RUN=0 turns off the dry-run log and its read-only margin checks."""
    return os.environ.get("ATLAS_LITE_EXPIRY_DRY_RUN", "1").strip().lower() in ("1", "true", "yes")


def _tick(px: float, up: bool) -> float:
    n = px / TICK
    return round((math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * TICK, 2)


def _split(symbol: str) -> tuple[str, str]:
    exch, _, sym = str(symbol).partition(":")
    return (exch, sym) if sym else ("NFO", exch)


def kite_order(symbol: str, side: str, qty: int, *, price: float, order_type: str = "LIMIT",
               trigger: float | None = None, tag: str = "") -> dict[str, Any]:
    exch, sym = _split(symbol)
    o: dict[str, Any] = {"variety": "regular", "exchange": exch, "tradingsymbol": sym, "transaction_type": side,
                         "quantity": int(qty), "product": PRODUCT, "order_type": order_type, "validity": "DAY",
                         "price": price}
    if trigger is not None:
        o["trigger_price"] = trigger
    if tag:
        o["tag"] = tag[:20]
    return o


@dataclass
class ExpiryDryRun:
    path: Path
    margin_fn: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None  # read-only Kite margin check
    open_stops: dict[str, set[str]] = field(default_factory=dict)  # book -> short-leg symbols with a live stop order
    signs: dict[str, dict[str, int]] = field(default_factory=dict)  # book -> symbol -> -1 sold / +1 bought
    last_margin: dict[str, dict[str, Any]] = field(default_factory=dict)
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.expiry_dry_run"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def _write(self, row: dict[str, Any]) -> None:
        row.setdefault("sent", False)  # never sent: dry run
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError as exc:
            self._log.warning("expiry dry-run write failed: %s", exc)

    def on_event(self, book: str, ev: dict[str, Any]) -> list[dict[str, Any]]:
        """Orders a live book would send for this paper event; logged, returned for the margin check."""
        if book not in DRY_RUN_BOOKS or not isinstance(ev, dict):
            return []
        kind, ts, day = ev.get("event"), str(ev.get("ts") or ""), str(ev.get("day") or "")
        tag = f"dry-{book}"[:20]
        base = {"book": book, "ts": ts, "day": day, "paper_event": kind}
        orders: list[dict[str, Any]] = []
        if kind == "open":
            qty = int(ev.get("qty") or 0)
            entry, stops = [], []
            for leg in ev.get("legs") or []:
                side = "SELL" if int(leg.get("sign") or 0) < 0 else "BUY"
                px = float(leg.get("fill") or leg.get("entry") or 0.0)
                entry.append(kite_order(leg["symbol"], side, qty, price=_tick(px, up=side == "BUY"), tag=tag))
            for sym, lvl in (ev.get("stops") or {}).items():
                trig = _tick(float(lvl), up=True)
                stops.append(kite_order(sym, "BUY", qty, order_type="SL", trigger=trig,
                                        price=_tick(trig * (1 + SL_LIMIT_ABOVE), up=True), tag=tag))
            self.open_stops[book] = set((ev.get("stops") or {}).keys())
            self.signs[book] = {str(leg["symbol"]): int(leg.get("sign") or 0) for leg in ev.get("legs") or []}
            for o in entry:
                self._write({**base, "action": "place_entry", "order": o})
            for o in stops:
                self._write({**base, "action": "place_stop", "order": o})
            orders = entry + stops
            if self.margin_fn is not None:
                self._write({**base, "action": "margin_check_queued"})
        elif kind == "leg_stop":
            sym = str(ev.get("leg") or "")
            self.open_stops.get(book, set()).discard(sym)
            self._write({**base, "action": "stop_filled", "symbol": sym, "fill_px": ev.get("exit"),
                         "stop_level": ev.get("stop_level")})
        elif kind == "close":
            syms = str(ev.get("symbol") or "").split(",")
            signs = self.signs.pop(book, None)
            shorts = {s for s, g in signs.items() if g < 0} if signs else self._shorts(ev)  # after a restart: by strike
            stops = self.open_stops.pop(book, None)
            if stops is None:  # restarted mid-trade: every sold leg not stopped still has its stop order
                stops = {s for s, leg in zip(syms, ev.get("legs") or []) if s in shorts and not leg.get("stopped")}
            if ev.get("reason") == "expiry_missed":  # engine down at the close: MIS was squared off by the broker
                self._write({**base, "action": "missed_close", "symbols": sorted(syms)})
                return []
            for sym in sorted(stops):
                self._write({**base, "action": "cancel_stop", "symbol": sym})
            qty = int(ev.get("qty") or 0)
            for sym, leg in zip(syms, ev.get("legs") or []):
                if leg.get("stopped") or leg.get("exit") is None:
                    continue
                side = "BUY" if sym in shorts else "SELL"
                o = kite_order(sym, side, qty, price=_tick(float(leg["exit"]), up=side == "BUY"), tag=tag)
                orders.append(o)
                self._write({**base, "action": "place_exit", "order": o, "reason": ev.get("reason")})
        return orders

    @staticmethod
    def _shorts(ev: dict[str, Any]) -> set[str]:
        """Short legs of a closing trade: the legs that are not the 5% wings (wings are bought far out of the money,
        the sold legs are the two nearest the money)."""
        syms = [s for s in str(ev.get("symbol") or "").split(",") if s]
        legs = ev.get("legs") or []
        strikes = [(s, l.get("strike"), l.get("opt")) for s, l in zip(syms, legs)]
        out = set()
        for opt in ("CE", "PE"):
            same = [(s, k) for s, k, o in strikes if o == opt and k is not None]
            if same:  # the sold call is the lowest call strike, the sold put the highest put strike
                out.add((min if opt == "CE" else max)(same, key=lambda x: x[1])[0])
        return out

    def check_margin(self, book: str, ev: dict[str, Any], orders: list[dict[str, Any]]) -> None:
        """Read-only: would the entry basket fit? Run in a worker thread (blocking HTTP)."""
        if self.margin_fn is None:
            return
        entry = [{k: o[k] for k in ("exchange", "tradingsymbol", "transaction_type", "variety", "product",
                                    "order_type", "quantity", "price")} for o in orders if o["order_type"] == "LIMIT"]
        try:
            res = dict(self.margin_fn(entry))
            need, have = res.get("required"), res.get("available")
            res["fits"] = (need is not None and have is not None and have >= need * MARGIN_HEADROOM)
        except Exception as exc:  # noqa: BLE001 - a failed check is logged, never fatal
            res = {"error": str(exc)[:200], "fits": None}
        self.last_margin[book] = {**res, "ts": ev.get("ts")}
        self._write({"book": book, "ts": datetime.now(IST).isoformat(), "day": ev.get("day"), "paper_event": "open",
                     "action": "margin_check", **res})

    def snapshot(self, book: str, today: str) -> dict[str, Any]:
        n = 0
        if self.path.is_file():
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    if f'"book":"{book}"' in line and f'"day":"{today}"' in line and '"action":"place_' in line:
                        n += 1
            except OSError:
                pass
        return {"mode": "dry_run", "orders_today": n, "margin": self.last_margin.get(book)}
