"""Long straddle — morning ATM straddle purchase on NIFTY. Paper by default; the main book can send
real Kite orders on a day the operator ticks "Live straddle" on the dashboard.

Rules, as tested on real recorded prices (scratch study, 2-3 Nov 2026):

- 09:16 on every trading day, expiry days included: buy the call and put at the strike nearest the
  money where their prices are closest (ATM, ATM+50 or ATM-50: the balanced straddle), 1 lot.
- Sell both at 13:00. Emergency stop: sell both if their bids fall to 30% below the price paid.
- Monthly loss limit: no new entries for the rest of the month once the month's closed P&L reaches
  -MONTH_LOSS_LIMIT.

Real recorded prices, 3 Sep - 30 Oct 2026 (37 days, ask in / bid out, Kite charges): nearest strike
+₹1,767 a day, 56% of days positive, worst day -₹4,141, t 2.8; balanced strike +₹1,846, worst -₹2,523,
t 3.0; the -30% stop fired once and did not cut a winner (a -10% stop fired on 16 days and cut the
average by a third). The window was unusually volatile (median 09:16-13:00 NIFTY move 111 pts vs 68
over the past year; break-even about 97), so the book is meant to show whether it survives calm months.
Rationale: intraday volatility is U-shaped; a straddle priced on a whole-day volatility is cheap for
the busy morning. Scalper v3 is the mirror image (sells at 13:00 into the quiet afternoon).

Paper fills: buy at ask, sell at bid when quotes have them, else last price +/- SLIP_PTS. An exit first
seen after EXIT_LATE (engine was down at 13:00) is still priced, but marked late; one first seen after
SQUARE_OFF_LATE, or on a later day, closes with P&L unknown, never at stale post-close quotes.

Live (main book only, intraday MIS, 1 lot): decided at entry, on a day the dashboard checkbox is ticked
and the host allows it (ATLAS_LITE_STRADDLE_LIVE_ALLOWED=1). Before buying, Kite's basket margin for the
two buys (fallback: ask x qty + 10%) must fit the funds Kite shows available. The orders are run by
atlas_lite/live_straddle.py: tagged orders, holdings from the book's own fills read back from Kite's
order book, never from net positions (see that module). Once a live trade has started it is finished on
the broker even if the checkbox is unticked meanwhile.

Variant "long_straddle_tp20" (TP20_*, paper only): the same entry and the same -30% stop, plus a resting
take-profit at +20% of the price paid, filled at exactly that price once the bids reach it, so the two
books differ only in the take-profit. Same 37 days: +₹1,065 a day, 65% of days positive, t 2.92 without
the stop; +₹1,079, t 3.01 with it. Kite has no combo order for NIFTY options, so that two-leg
take-profit would need leg-by-leg handling before it could ever go live.

Variants added 6 Nov 2026 (VARIANTS, paper only, same entry; they differ only in the exit):

- "long_straddle_trail": once the bids reach +10% over the price paid, sell both when they fall 15%
  below their peak since entry; same -30% stop, else 13:00. Same 37 days: +₹1,248 a day, 64% of days
  positive, best day +₹9,423 (the best trade-off of 12 trailing settings; every profit lock lowered
  the average). The peak is kept in memory only: after a restart it starts again from the bids then.
- "long_straddle_leg50": sell a leg on its own once its bid is 50% below what was paid for it (the
  first leg only); the other leg rides to 13:00. No straddle stop (the leg exit is the stop). Same 37
  days: +₹2,033 a day, worst day -₹3,219 (-30% raised the average but had -₹6.6-8.7k days).

The strategy name is the retired iron-fly overlay's ("long_straddle"), which never traded; its two empty
day rows stay in paper_trades.jsonl (day_pnl events, never read as trades). This book has its own ledger.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from atlas_lite.agent_gates import _is_nse_trading_day
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.live_straddle import LiveStraddle
from atlas_lite.log_util import get_logger
from atlas_lite.metrics import pick_float, quote_ask, quote_bid, quote_ltp
from atlas_lite.minute_bars import hm_ge
from atlas_lite.specs import NIFTY_SYMBOL, VIX_SYMBOLS

IST = ZoneInfo("Asia/Kolkata")
STRATEGY = "long_straddle"
LEDGER_NAME = "paper_long_straddle.jsonl"
ENTRY_AT = (9, 16)
ENTRY_LATEST = (9, 21)  # a late first frame may still enter; later than this skips the day
EXIT_AT = (13, 0)
EXIT_LATE = (13, 6)  # an exit first seen after this is priced but marked late
SQUARE_OFF_LATE = (15, 20)  # first frame after this (engine down all afternoon): exit unknown
# Live: no new orders from here (Zerodha squares F&O MIS off at 15:14; an exit retry must never race it)
LIVE_ORDERS_UNTIL = (15, 10)
SLIP_PTS = 1.0
DEFAULT_LOT_SIZE = 65
STRIKE_STEP = 50
STOP_LOSS = 0.30  # emergency stop: bids at 30% below the price paid
MONTH_LOSS_LIMIT = 15_000.0
FUNDS_HEADROOM = 1.10
LIVE_FLAG_NAME = "straddle_live.json"
LIVE_ENV = "ATLAS_LITE_STRADDLE_LIVE_ALLOWED"
LIVE_EVENTS = frozenset({"order_intent", "order_sent", "order_send_error", "order_cancel_sent", "order_final",
                         "live_phase", "manual_needed", "live_mark"})
TP20_STRATEGY = "long_straddle_tp20"
TP20_LEDGER_NAME = "paper_long_straddle_tp20.jsonl"
TP20_TAKE_PROFIT = 0.20
VARIANTS: dict[str, dict[str, Any]] = {
    "long_straddle_trail": {"ledger": "paper_long_straddle_trail.jsonl", "name": "Long straddle trail",
                            "env": "ATLAS_LITE_PAPER_LONG_STRADDLE_TRAIL",
                            "kwargs": {"trail_start": 0.10, "trail_gap": 0.15}},
    "long_straddle_leg50": {"ledger": "paper_long_straddle_leg50.jsonl", "name": "Long straddle leg −50%",
                            "env": "ATLAS_LITE_PAPER_LONG_STRADDLE_LEG50",
                            "kwargs": {"leg_stop": 0.50, "stop_loss": None}},
}


def paper_long_straddle_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_LONG_STRADDLE", "1").strip().lower()
    return raw in ("1", "true", "yes")


def paper_long_straddle_tp20_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_LONG_STRADDLE_TP20", "1").strip().lower()
    return raw in ("1", "true", "yes")


def paper_long_straddle_variant_enabled(name: str) -> bool:
    raw = os.environ.get(VARIANTS[name]["env"], "1").strip().lower()
    return raw in ("1", "true", "yes")


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def hm(t: tuple[int, int]) -> str:
    return f"{t[0]:02d}:{t[1]:02d}"


def entry_context(book: QuoteSource, ce: float, pe: float) -> dict[str, Any]:
    """Conditions at entry, logged for the ~27 Nov re-test of a filter (4 Nov study: VIX, prior range and the
    OI walls predict the size of the morning move, but not a move bigger than the straddle's price; on 36
    real days the cheaper straddles and the less balanced CE/PE did better, too few days to trust).
    Yesterday's range and the OI walls are rebuilt from history at the re-test, so they are not logged.
    Never raises: a logging gap must not stop the trade."""
    try:
        return _entry_context(book, ce, pe)
    except Exception:  # noqa: BLE001
        return {}


def _entry_context(book: QuoteSource, ce: float, pe: float) -> dict[str, Any]:
    out: dict[str, Any] = {"ce_pe_imbalance": round(abs(ce - pe) / (ce + pe), 4)}
    spot_row = book.get(NIFTY_SYMBOL)
    spot = quote_ltp(spot_row)
    if spot:
        out["spot"] = spot
        out["straddle_pct_spot"] = round((ce + pe) / spot * 100, 3)
        prev = pick_float(spot_row.get("ohlc") if isinstance(spot_row.get("ohlc"), dict) else {}, "close")
        if prev:
            out["gap_pct"] = round((spot - prev) / prev * 100, 3)
    vix_row = book.get(VIX_SYMBOLS[0]) if VIX_SYMBOLS else None
    vix = quote_ltp(vix_row)
    if vix:
        out["vix"] = vix
        prev = pick_float(vix_row.get("ohlc") if isinstance(vix_row.get("ohlc"), dict) else {}, "close")
        if prev:
            out["vix_chg"] = round(vix - prev, 2)
    return out


def strike_symbol(symbol: str, atm: int, strike: int) -> str:
    """The same option series at another strike: NFO:NIFTY26N0322950CE -> ...23000CE."""
    head, tail = symbol[: -2], symbol[-2:]
    if not head.endswith(str(atm)):
        return symbol
    return f"{head[: -len(str(atm))]}{strike}{tail}"


def balanced_strike(book: QuoteSource, atm: int, ce_symbol: str, pe_symbol: str) -> tuple[int, str, str]:
    """Among ATM-50, ATM, ATM+50, the strike whose call and put last prices are closest (both quoted).
    Falls back to the given ATM pair."""
    best: tuple[float, int, str, str] | None = None
    for k in (atm - STRIKE_STEP, atm, atm + STRIKE_STEP):
        ce, pe = strike_symbol(ce_symbol, atm, k), strike_symbol(pe_symbol, atm, k)
        c, p = quote_ltp(book.get(ce)), quote_ltp(book.get(pe))
        if c and p and c > 0 and p > 0:
            gap = abs(c - p)
            if best is None or gap < best[0] or (gap == best[0] and k == atm):
                best = (gap, k, ce, pe)
    if best is None:
        return atm, ce_symbol, pe_symbol
    return best[1], best[2], best[3]


@dataclass
class LongStraddlePosition:
    """A paper position (live trades are run by LiveStraddle)."""

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
    ce_fill: float | None = None  # each leg's ask paid (the leg exit compares against it)
    pe_fill: float | None = None
    peak: float = 0.0  # highest bid total since entry (trailing exit; in memory only)
    sold_leg: str | None = None  # "ce" | "pe" once the leg exit sold it
    sold_px: float | None = None


@dataclass
class PaperLongStraddle:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = 1
    strategy: str = STRATEGY
    name: str = "Long straddle"
    take_profit: float | None = None  # resting take-profit as a fraction of the price paid (variant book)
    stop_loss: float | None = STOP_LOSS
    trail_start: float | None = None  # trailing exit arms once the bids are this far above the price paid
    trail_gap: float | None = None  # ... then sells when they fall this far below their peak
    leg_stop: float | None = None  # sell a leg on its own once its bid is this far below its own price paid
    month_loss_limit: float = MONTH_LOSS_LIMIT
    order_client: Any = None  # KiteOrderClient for live; None = paper only
    live_flag: Any = None  # DayLiveFlag; live only while it is enabled for today
    risk: Any = None  # RiskManager: asked before a live entry; logs only unless enforcing
    position: LongStraddlePosition | None = None
    live: LiveStraddle | None = None  # today's live trade while it runs
    pending_close: dict[str, Any] | None = None  # a live close whose ledger line could not be written yet
    traded_day: str = ""  # one entry per day
    last_reject: str | None = None
    month_pnl: dict[str, float] = field(default_factory=dict)
    # Retired (operator's call, 13 Nov 2026): an open paper straddle is sold at the bids on the next frame (reason
    # "retired"), and the book never opens again. A live trade still running is left to finish by its own rules.
    retired: bool = False
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_long_straddle"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._restore()

    @property
    def qty(self) -> int:
        return int(self.lots) * int(self.lot_size)

    def live_enabled(self) -> bool:
        return bool(self.order_client is not None and self.live_flag is not None and self.live_flag.is_enabled())

    # ---- ledger ----
    def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event.setdefault("strategy", self.strategy)
        event.setdefault("live_orders", bool(event.get("live")))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_file() and self.path.stat().st_size:  # a line torn by a crash is closed off first
                with self.path.open("rb") as rf:
                    rf.seek(-1, 2)
                    torn = rf.read(1) != b"\n"
                if torn:
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write("\n")
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
                if event.get("live"):  # real-money lines survive a host crash too
                    fh.flush()
                    os.fsync(fh.fileno())
        except OSError as exc:
            self._log.warning("%s ledger write failed: %s", self.strategy, exc)
            return None
        return event

    def _live_log(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Ledger line for the live order layer (stamped with the live trade's day)."""
        day = self.live.day if self.live is not None else self.traded_day
        return self._append({"ts": datetime.now(IST).isoformat(), "day": day, "live": True, **event})

    def _restore(self) -> None:
        if not self.path.is_file():
            return
        open_ev: dict[str, Any] | None = None
        live_start: dict[str, Any] | None = None
        live_events: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict) or ev.get("strategy") != self.strategy:
                continue
            kind = ev.get("event")
            if kind == "live_start":
                live_start, live_events = ev, []
                self.traded_day = str(ev.get("day") or "")
            elif kind in LIVE_EVENTS and live_start is not None:
                live_events.append(ev)
            elif kind == "live_skip":
                self.traded_day = str(ev.get("day") or self.traded_day)
            elif kind == "open" and not ev.get("live"):
                open_ev = ev
                self.traded_day = str(ev.get("day") or "")
            elif kind == "leg_close" and open_ev is not None:
                open_ev = {**open_ev, "sold_leg": ev.get("leg"), "sold_px": ev.get("px")}
            elif kind == "close":
                if ev.get("live"):
                    live_start = None  # one line ends a live trade and carries its P&L
                else:
                    open_ev = None
                if ev.get("pnl") is not None:
                    m = str(ev.get("day") or "")[:7]
                    self.month_pnl[m] = round(self.month_pnl.get(m, 0.0) + float(ev["pnl"]), 2)
        if live_start is not None:
            self.live = LiveStraddle.restore(live_start, live_events, self._live_log)
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
                ce_fill=pick_float(open_ev, "ce_fill"),
                pe_fill=pick_float(open_ev, "pe_fill"),
                sold_leg=open_ev.get("sold_leg"),
                sold_px=pick_float(open_ev, "sold_px"),
            )

    # ---- prices ----
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

    def _month_blocked(self, now: datetime) -> bool:
        return self.month_pnl.get(now.strftime("%Y-%m"), 0.0) <= -abs(self.month_loss_limit)

    # ---- entry ----
    def _open(self, now: datetime, book: QuoteSource, atm: int, ce_symbol: str, pe_symbol: str,
              expiry_day: bool) -> dict[str, Any] | None:
        strike, ce_symbol, pe_symbol = balanced_strike(book, atm, ce_symbol, pe_symbol)
        ce_row, pe_row = book.get(ce_symbol), book.get(pe_symbol)
        ce, pe = quote_ltp(ce_row), quote_ltp(pe_row)
        ce_ask, pe_ask = self._fill(ce_row, True), self._fill(pe_row, True)
        if not ce or not pe or ce_ask is None or pe_ask is None:
            self.last_reject = "no_atm_quote"
            return None
        day = now.strftime("%Y-%m-%d")
        live = self.live_enabled()
        if live and self.risk is not None:
            verdict = self.risk.check(self.strategy, now, selling=False)
            if verdict.get("blocks"):  # enforced "no": today's trade stays on paper
                live = False
                self._append({"event": "live_risk_block", "ts": now.isoformat(), "day": day, "live": False,
                              "reasons": verdict.get("reasons")})
        if live:
            return self._open_live(now, day, strike, ce_symbol, pe_symbol, ce_ask + pe_ask, expiry_day,
                                   entry_context(book, ce, pe))
        charges_open = round(float(kite_nfo_charges([(ce_ask, self.qty, "buy"), (pe_ask, self.qty, "buy")])["total"]), 2)
        pos = LongStraddlePosition(
            day=day, opened_at=now.isoformat(), atm=int(strike), ce_symbol=ce_symbol, pe_symbol=pe_symbol,
            qty=self.qty, debit_fill=round(ce_ask + pe_ask, 2), charges_open=charges_open,
            expiry_day=expiry_day, ce_mark=ce, pe_mark=pe, ce_fill=ce_ask, pe_fill=pe_ask,
        )
        ev = self._append({
            "event": "open", "ts": now.isoformat(), "day": day, "side": "long", "style": "long",
            "atm": pos.atm, "symbol": f"{ce_symbol},{pe_symbol}", "ce_symbol": ce_symbol, "pe_symbol": pe_symbol,
            "qty": pos.qty, "ce": ce, "pe": pe, "ce_fill": ce_ask, "pe_fill": pe_ask,
            "debit_fill": pos.debit_fill, "entry": pos.debit_fill,
            "charges_open": charges_open, "expiry_day": expiry_day, "live": False,
            "context": entry_context(book, ce, pe),
            "stop": round(pos.debit_fill * (1 - self.stop_loss), 2) if self.stop_loss else None,
            "reason": "09:16 balanced ATM straddle purchase" + (" (expiry day)" if expiry_day else ""),
        })
        if ev is None:
            return None
        self.position = pos
        self.traded_day = day
        self.last_reject = None
        return ev

    def _funds_needed(self, ce_symbol: str, pe_symbol: str, ask_total: float) -> float:
        """Kite basket margin for the two buys; fallback: the asks x qty plus headroom."""
        fallback = ask_total * self.qty * FUNDS_HEADROOM
        orders = []
        for sym in (ce_symbol, pe_symbol):
            exch, ts = sym.split(":", 1) if ":" in sym else ("NFO", sym)
            orders.append({"exchange": exch, "tradingsymbol": ts, "transaction_type": "BUY", "variety": "regular",
                           "product": "MIS", "order_type": "MARKET", "quantity": self.qty, "price": 0})
        try:
            basket = float(self.order_client.basket_margin_required(orders))
        except Exception:  # noqa: BLE001 - margin API unavailable: use the conservative estimate
            return fallback
        return max(basket, fallback) if basket > 0 else fallback

    def _open_live(self, now: datetime, day: str, strike: int, ce_symbol: str, pe_symbol: str, ask_total: float,
                   expiry_day: bool, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Start today's live trade: funds check, a durable start line, then both buys (tagged)."""
        need = self._funds_needed(ce_symbol, pe_symbol, ask_total)
        try:
            available = float(self.order_client.equity_available())
        except Exception as exc:  # noqa: BLE001 - no funds reading, no order
            return self._live_skip(now, day, f"funds_unavailable:{exc}")
        if available < need:
            return self._live_skip(now, day, f"insufficient_funds:avail={available:.0f}:need={need:.0f}")
        self.traded_day = day
        start = self._append({"event": "live_start", "ts": now.isoformat(), "day": day, "live": True, "atm": strike,
                              "ce_symbol": ce_symbol, "pe_symbol": pe_symbol, "qty": self.qty,
                              "expiry_day": expiry_day, "context": context or {}})
        if start is None:  # no durable record: no real orders today
            self.last_reject = "ledger_write_failed"
            return None
        self.live = LiveStraddle(day=day, strike=strike, ce_symbol=ce_symbol, pe_symbol=pe_symbol, qty=self.qty,
                                 expiry_day=expiry_day, log=self._live_log)
        self.last_reject = None
        for leg in ("ce", "pe"):
            if not self.live.place(self.order_client, leg, "BUY", self.qty):
                self.last_reject = "ledger_write_failed"  # whatever was sent is handled by the live layer
                break
        self._log.warning("LIVE long straddle started %s / %s x%d", ce_symbol, pe_symbol, self.qty)
        return start

    def _live_skip(self, now: datetime, day: str, why: str) -> dict[str, Any] | None:
        self.traded_day = day
        self.last_reject = why
        self._log.warning("LIVE long straddle skipped: %s", why)
        return self._append({"event": "live_skip", "ts": now.isoformat(), "day": day, "live": True, "why": why})

    def _live_step(self, now: datetime, book: QuoteSource) -> dict[str, Any] | None:
        ls = self.live
        assert ls is not None
        before = ls.phase
        result = ls.step(self.order_client, bids={"ce": quote_bid(book.get(ls.ce_symbol)),
                                                  "pe": quote_bid(book.get(ls.pe_symbol))},
                         hm=now.strftime("%H:%M"), today=now.strftime("%Y-%m-%d"), stop_loss=self.stop_loss,
                         exit_at=hm(EXIT_AT), square_off_late=hm(LIVE_ORDERS_UNTIL))
        if before == "entering" and ls.phase in ("holding", "exiting"):
            ce_avg, ce_q = ls.cost("ce")
            pe_avg, pe_q = ls.cost("pe")
            # The trades list's "open" (live), with the real fills.
            self._append({"event": "open", "ts": now.isoformat(), "day": ls.day, "side": "long", "style": "long",
                          "atm": ls.strike, "symbol": f"{ls.ce_symbol},{ls.pe_symbol}", "ce_symbol": ls.ce_symbol,
                          "pe_symbol": ls.pe_symbol, "qty": max(ce_q, pe_q), "ce_qty": ce_q, "pe_qty": pe_q,
                          "debit_fill": round(ce_avg + pe_avg, 2), "entry": round(ce_avg + pe_avg, 2),
                          "expiry_day": ls.expiry_day, "live": True,
                          "reason": "09:16 balanced ATM straddle purchase (live)"})
        if result is None:
            if ls.manual_needed:
                self.last_reject = "manual_needed"
            elif ls.book_error:
                self.last_reject = f"order_book_unreadable:{ls.book_error}"
            return None
        self.pending_close = {"event": "close", "ts": now.isoformat(), "day": ls.day, "side": "long", "style": "long",
                              "atm": ls.strike, "symbol": f"{ls.ce_symbol},{ls.pe_symbol}", "qty": ls.qty,
                              "entry": result["debit"], "exit": result["exit"], "pnl_gross": result["pnl_gross"],
                              "charges": result["charges"], "pnl": result["pnl"],
                              "pnl_known": not result["estimated"], "pnl_estimated": result["estimated"],
                              "expiry_day": ls.expiry_day, "reason": result["reason"], "live": True}
        return self._write_pending_close()

    def _write_pending_close(self) -> dict[str, Any] | None:
        """The live close line ends the trade and carries its P&L; until it is written, keep trying (the
        monthly total moves only when the line is on disk, so memory and ledger agree)."""
        ev = self._append(dict(self.pending_close or {}))
        if ev is None:  # the trade stays in memory (finished, sending nothing) until the close is on disk
            self.last_reject = "ledger_write_failed_close_pending"
            return None
        self.live = None
        m = str(ev["day"])[:7]
        self.month_pnl[m] = round(self.month_pnl.get(m, 0.0) + float(ev["pnl"]), 2)  # estimated ones count too
        self.pending_close = None
        return ev

    # ---- paper exit ----
    def _close(self, now: datetime, book: QuoteSource, reason: str,
               leg_prices: tuple[float, float] | None = None) -> dict[str, Any] | None:
        """leg_prices: the fills when already known (a resting take-profit), else priced here."""
        pos = self.position
        if pos is None:
            return None
        known = reason in ("time", "time_late", "target", "stop", "trail", "retired")
        prices = []
        for i, (leg, sym, mark) in enumerate((("ce", pos.ce_symbol, pos.ce_mark), ("pe", pos.pe_symbol, pos.pe_mark))):
            if pos.sold_leg == leg and pos.sold_px is not None:  # sold earlier by the leg exit
                prices.append(pos.sold_px)
                continue
            px = leg_prices[i] if leg_prices is not None else (self._fill(book.get(sym), buying=False) if known else None)
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
        ev = self._append({
            "event": "close", "ts": now.isoformat(), "day": pos.day, "side": "long", "style": "long",
            "atm": pos.atm, "symbol": f"{pos.ce_symbol},{pos.pe_symbol}", "opened_at": pos.opened_at,
            "qty": pos.qty, "entry": pos.debit_fill, "exit": round(credit, 2) if known else None,
            "pnl_gross": gross, "charges": charges, "pnl": pnl, "pnl_known": known,
            "expiry_day": pos.expiry_day, "reason": reason, "live": False,
            **({"sold_leg": pos.sold_leg, "sold_px": pos.sold_px} if pos.sold_leg else {}),
        })
        if ev is None:
            return None
        if pnl is not None:
            m = pos.day[:7]
            self.month_pnl[m] = round(self.month_pnl.get(m, 0.0) + pnl, 2)
        self.position = None
        return ev

    # ---- frame ----
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
        if self.pending_close is not None:  # a finished live trade: only its close line is left to write
            return self._write_pending_close()
        if self.live is not None:  # a live trade runs to its end, checkbox or not
            return self._live_step(now, book)
        pos = self.position
        if self.retired:
            if pos is None:
                return None
            if pos.day != today:  # never sold (engine down): exit unknown
                return self._close(now, book, "session_gap")
            # Only at real bids: right after a restart the quotes are not back yet, and a close then has no price
            # (13 Nov 2026 10:12 closed four books with no exit that way).
            legs = [(leg, sym) for leg, sym in (("ce", pos.ce_symbol), ("pe", pos.pe_symbol)) if pos.sold_leg != leg]
            if any(self._fill(book.get(sym), buying=False) is None for _, sym in legs):
                self.last_reject = "retired_waiting_for_quotes"
                return None
            return self._close(now, book, "retired")
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
            if self.take_profit is not None and not hm_ge(now, EXIT_AT):
                hit = self._take_profit_fill(pos, book)
                if hit is not None:
                    return self._close(now, book, "target", leg_prices=hit)
            if self.trail_start is not None and not hm_ge(now, EXIT_AT):
                hit = self._trail_hit(pos, book)
                if hit is not None:
                    return self._close(now, book, "trail", leg_prices=hit)
            if self.leg_stop is not None and pos.sold_leg is None and not hm_ge(now, EXIT_AT):
                ev = self._leg_exit(now, pos, book)
                if ev is not None:
                    return ev
            if self.stop_loss and pos.sold_leg is None and not hm_ge(now, EXIT_AT) and self._stop_hit(pos, book):
                return self._close(now, book, "stop")
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
        if self._month_blocked(now):
            self.last_reject = "monthly_loss_limit"
            return None
        return self._open(now, book, atm, ce_symbol, pe_symbol, expiry == now.date())

    def _stop_hit(self, pos: LongStraddlePosition, book: QuoteSource) -> bool:
        """Both legs' bids at or below the stop (30% under the price paid)."""
        bids = [quote_bid(book.get(s)) for s in (pos.ce_symbol, pos.pe_symbol)]
        if any(b is None or b <= 0 for b in bids):
            return False
        return bids[0] + bids[1] <= pos.debit_fill * (1 - float(self.stop_loss or 0.0))

    def _trail_hit(self, pos: LongStraddlePosition, book: QuoteSource) -> tuple[float, float] | None:
        """Both bids (the sale prices) once the bid total has been trail_start above the price paid and
        has since fallen trail_gap below its peak; None otherwise."""
        bids = [quote_bid(book.get(s)) for s in (pos.ce_symbol, pos.pe_symbol)]
        if any(b is None or b <= 0 for b in bids):
            return None
        total = bids[0] + bids[1]
        pos.peak = max(pos.peak, total)
        armed = pos.peak >= pos.debit_fill * (1 + float(self.trail_start or 0.0))
        if armed and total <= pos.peak * (1 - float(self.trail_gap or 0.0)):
            return float(bids[0]), float(bids[1])
        return None

    def _leg_exit(self, now: datetime, pos: LongStraddlePosition, book: QuoteSource) -> dict[str, Any] | None:
        """Sell the first leg whose bid is leg_stop below its own ask paid; the other leg rides on."""
        for leg, sym, paid in (("ce", pos.ce_symbol, pos.ce_fill), ("pe", pos.pe_symbol, pos.pe_fill)):
            bid = quote_bid(book.get(sym))
            if paid is None or bid is None or bid <= 0:
                continue
            if bid <= paid * (1 - float(self.leg_stop or 0.0)):
                ev = self._append({
                    "event": "leg_close", "ts": now.isoformat(), "day": pos.day, "leg": leg, "symbol": sym,
                    "paid": paid, "px": round(float(bid), 2), "live": False,
                    "reason": f"leg bid {round(float(bid), 2)} is {self.leg_stop:.0%} or more below {paid}",
                })
                if ev is None:
                    return None
                pos.sold_leg, pos.sold_px = leg, round(float(bid), 2)
                return ev
        return None

    def _take_profit_fill(self, pos: LongStraddlePosition, book: QuoteSource) -> tuple[float, float] | None:
        """Resting limit at debit x (1 + take_profit): fills at exactly that total once the bids reach it.
        Split across the legs in proportion to their bids (for charges); None while not reached.
        Paper model: both legs fill together (see the module docstring)."""
        bids = [quote_bid(book.get(s)) for s in (pos.ce_symbol, pos.pe_symbol)]
        if any(b is None or b <= 0 for b in bids):
            return None
        target = pos.debit_fill * (1 + float(self.take_profit or 0.0))
        total = bids[0] + bids[1]
        if total < target:
            return None
        return round(bids[0] * target / total, 2), round(target - round(bids[0] * target / total, 2), 2)

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        ls, pos = self.live, self.position
        body: dict[str, Any] = {
            "ok": True,
            "mode": "live" if ls is not None else "paper",
            "live_orders": ls is not None,
            "live_capable": self.order_client is not None,
            "book": self.strategy,
            "name": self.name,
            "take_profit": self.take_profit,
            "stop_loss": self.stop_loss,
            "trail_start": self.trail_start,
            "trail_gap": self.trail_gap,
            "leg_stop": self.leg_stop,
            "month_pnl": self.month_pnl.get(datetime.now(IST).strftime("%Y-%m"), 0.0),
            "month_loss_limit": self.month_loss_limit,
            "enabled": True,
            "last_reject": self.last_reject,
            "position": None,
        }
        if ls is not None:
            ce_avg, _ = ls.cost("ce")
            pe_avg, _ = ls.cost("pe")
            held = {leg: ls.held(leg) for leg in ("ce", "pe")}
            bids = {"ce": quote_bid(book.get(ls.ce_symbol)) if book is not None else None,
                    "pe": quote_bid(book.get(ls.pe_symbol)) if book is not None else None}
            value = sum((bids[leg] or 0.0) * held[leg] for leg in held)
            cost = ce_avg * held["ce"] + pe_avg * held["pe"]
            body["position"] = {
                "atm": ls.strike, "live": True, "phase": ls.phase, "held_ce": held["ce"], "held_pe": held["pe"],
                "debit": round(ce_avg + pe_avg, 2), "manual_needed": ls.manual_needed,
                "value_now": round(value / max(held["ce"], held["pe"], 1), 2) if any(held.values()) else None,
                "stop": round((ce_avg + pe_avg) * (1 - self.stop_loss), 2) if self.stop_loss and cost else None,
                "open_pnl": round(value - cost, 2) if any(held.values()) else None,
                "expiry_day": ls.expiry_day, "target": None,
            }
        elif pos is not None:
            # Valued at the bids, as the take-profit, the stop and the 13:00 exit would sell: a last-price
            # value can show "at target" while the bids, and so the resting order, are still below it.
            ce = (self._fill(book.get(pos.ce_symbol), buying=False) if book is not None else None) or pos.ce_mark
            pe = (self._fill(book.get(pos.pe_symbol), buying=False) if book is not None else None) or pos.pe_mark
            if pos.sold_leg == "ce":
                ce = pos.sold_px
            elif pos.sold_leg == "pe":
                pe = pos.sold_px
            value = None if ce is None or pe is None else ce + pe
            close_ch = (
                float(kite_nfo_charges([(ce, pos.qty, "sell"), (pe, pos.qty, "sell")])["total"]) if value is not None else 0.0
            )
            body["position"] = {
                "atm": pos.atm,
                "debit": pos.debit_fill,
                "value_now": None if value is None else round(value, 2),
                "expiry_day": pos.expiry_day,
                "live": False,
                "manual_needed": False,
                "target": round(pos.debit_fill * (1 + self.take_profit), 2) if self.take_profit else None,
                "stop": round(pos.debit_fill * (1 - self.stop_loss), 2) if self.stop_loss and not pos.sold_leg else None,
                "sold_leg": pos.sold_leg,
                "peak": round(pos.peak, 2) if pos.peak else None,
                # Net of entry charges and estimated exit charges, like the ledger's pnl.
                "open_pnl": None if value is None else round((value - pos.debit_fill) * pos.qty - pos.charges_open - close_ch, 2),
            }
        return body
