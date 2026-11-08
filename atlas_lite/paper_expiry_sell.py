"""Expiry-week NIFTY and SENSEX selling paper books, with VIX deciding whether the straddle trades; 1 lot each.
Paper only, never Kite orders. The research below is NIFTY's; the SENSEX books run the same rules untested.

Research (6 Nov 2026, real NSE option prices Apr 2019 – Nov 2026, rules picked on 2019–23 and
judged on 2024–26; scratchpad fresh_search.py / sell_dynamic.py / sell_combo.py):

- expiry_eve_strangle*: at the open one session before expiry, sell the call nearest F*(1+0.5%)
  and the put nearest F*(1-0.5%). Per-leg stop: buy back a leg once it costs 2x its sale price
  (+100%). Hold the rest to expiry day, close 15:14. Per week, 1 lot: +Rs 2,151 / +Rs 1,732
  (t 4.3 / 4.1); with 5% wings +Rs 1,403 / +Rs 1,296.
- expiry_day_straddle*: at the open on expiry day, only if VIX >= 13, sell the ATM straddle.
  Per-leg stop at +50%. Close 15:14. +Rs 1,595 / +Rs 1,527 (t 4.6 / 4.0); 5% wings
  +Rs 1,092 / +Rs 1,309. VIX < 13 days earned ~nothing.
- Both together with 5% wings (fits Rs 2.5L margin): +Rs 2,659 / +Rs 2,510 a week, worst week
  -Rs 31k / -Rs 15k, no losing year. Doubling lots at VIX >= 17 adds profit but more drawdown (worst week
  -Rs 87k in Mar 2020); 1 lot always has the best profit per unit of drawdown in both periods (8 Nov
  re-test, scratchpad rich_premium.py), so the books trade 1 lot. ``size_up_vix`` can switch doubling back on.

Wings, when set, are bought first at the strikes nearest F*(1 +- 5%), as backtested, and
held to the close. Fills: sells at bid, buys at ask, else last price -/+ SLIP_PTS. Kite charges.
Positions carry overnight and are restored from the ledger after a restart.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from atlas_lite.agent_gates import _is_nse_trading_day
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger
from atlas_lite.metrics import quote_ask, quote_bid, quote_ltp
from atlas_lite.minute_bars import hm_ge, hm_lt
from atlas_lite.paper_weekly_sell import sessions_until

IST = ZoneInfo("Asia/Kolkata")
ENTRY_AT = (9, 16)
ENTRY_CLOSED = (9, 30)
EXIT_AT = (15, 14)
MARKET_OPEN, MARKET_CLOSE = (9, 15), (15, 30)  # stops are checked only while the market trades
SLIP_PTS = 1.0
DEFAULT_LOT_SIZE = 65
SIZE_UP_VIX: float | None = None  # 1 lot always; 17.0 doubled lots at VIX >= 17 until 8 Nov 2026

BOOKS: dict[str, dict[str, Any]] = {
    "expiry_eve_strangle": {"index": "nifty", "ledger": "paper_expiry_eve_strangle.jsonl", "sessions": 1, "dist": 0.005,
                            "wing_pct": 0.05, "stop": 1.0, "min_vix": None, "name": "Expiry-eve strangle ±0.5% (5% wings)"},
    "expiry_day_straddle": {"index": "nifty", "ledger": "paper_expiry_day_straddle.jsonl", "sessions": 0, "dist": 0.0,
                            "wing_pct": 0.05, "stop": 0.5, "min_vix": 13.0, "name": "Expiry-day straddle (5% wings)"},
    # Same rules on SENSEX (BSE, Thursday expiry). Not backtested on SENSEX: this paper run is the test.
    # India VIX (NIFTY options) stands in for SENSEX volatility; charges use the NSE schedule (BSE's
    # option transaction charge is within ~0.003% of NSE's).
    "sensex_expiry_eve_strangle": {"index": "sensex", "ledger": "paper_sensex_expiry_eve_strangle.jsonl", "sessions": 1,
                                   "dist": 0.005, "wing_pct": 0.05, "stop": 1.0, "min_vix": None,
                                   "name": "SENSEX expiry-eve strangle ±0.5% (5% wings)"},
    "sensex_expiry_day_straddle": {"index": "sensex", "ledger": "paper_sensex_expiry_day_straddle.jsonl", "sessions": 0,
                                   "dist": 0.0, "wing_pct": 0.05, "stop": 0.5, "min_vix": 13.0,
                                   "name": "SENSEX expiry-day straddle (5% wings)"},
}


def paper_expiry_sell_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_EXPIRY_SELL", "1").strip().lower()
    return raw in ("1", "true", "yes")


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def nearest(strikes: list[int], target: float) -> int | None:
    return min(strikes, key=lambda k: (abs(k - target), k)) if strikes else None


def lots_for_vix(vix: float | None, size_up_vix: float | None = SIZE_UP_VIX) -> int:
    return 2 if (size_up_vix is not None and vix is not None and vix >= size_up_vix) else 1


@dataclass
class Leg:
    strike: int
    opt: str  # "CE" | "PE"
    sign: int  # -1 sold, +1 bought
    symbol: str
    entry: float  # last traded price at entry (stops are measured from it)
    fill: float  # paper fill price
    mark: float | None = None
    exit: float | None = None  # set once the leg is closed (stop)


@dataclass
class ExpiryPosition:
    day: str
    expiry: str
    opened_at: str
    legs: list[Leg]
    qty: int
    charges_open: float
    vix: float | None
    credit: float = 0.0  # net credit per unit at entry (fills)
    realized: float = 0.0  # points x qty already booked by leg stops (before charges)
    charges_stops: float = 0.0


@dataclass
class PaperExpirySell:
    path: Path
    strategy: str
    sessions: int = 1
    dist: float = 0.005
    wing_pct: float | None = 0.05
    stop: float = 1.0
    min_vix: float | None = None
    size_up_vix: float | None = SIZE_UP_VIX
    lot_size: int = DEFAULT_LOT_SIZE
    name: str = ""
    position: ExpiryPosition | None = None
    entered_for: str = ""  # expiry already entered (one trade per weekly expiry)
    last_reject: str | None = None
    last_event: dict[str, Any] | None = None
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_expiry_sell"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._restore()

    def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event.setdefault("strategy", self.strategy)
        event.setdefault("live_orders", False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError as exc:
            self._log.warning("%s ledger write failed: %s", self.strategy, exc)
            return None
        self.last_event = event
        return event

    def _restore(self) -> None:
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        pos: ExpiryPosition | None = None
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("strategy") != self.strategy:
                continue
            if ev.get("event") == "skip":
                self.entered_for = str(ev.get("expiry") or "")
            elif ev.get("event") == "open":
                self.entered_for = str(ev.get("expiry") or "")
                pos = ExpiryPosition(
                    day=str(ev["day"]), expiry=str(ev["expiry"]), opened_at=str(ev["ts"]),
                    legs=[Leg(**{k: leg[k] for k in ("strike", "opt", "sign", "symbol", "entry", "fill")}) for leg in ev["legs"]],
                    qty=int(ev["qty"]), charges_open=float(ev["charges_open"]), vix=ev.get("vix"),
                    credit=float(ev.get("credit_fill") or 0.0),
                )
            elif ev.get("event") == "leg_stop" and pos is not None:
                for leg in pos.legs:
                    if leg.symbol == ev.get("leg") and leg.exit is None:
                        leg.exit = float(ev["exit"])
                pos.realized += float(ev.get("pnl_pts_qty") or 0.0)
                pos.charges_stops += float(ev.get("charges") or 0.0)
            elif ev.get("event") == "close":
                pos = None
        self.position = pos

    # ---- pricing ----

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

    # ---- open ----

    def _try_open(self, now: datetime, book: QuoteSource, expiry: date, strikes: list[int],
                  option_symbol: Callable[[int, str], str], atm: int, vix: float | None) -> dict[str, Any] | None:
        if self.min_vix is not None and vix is None:
            self.last_reject = "no_vix"  # retried until the entry window closes
            return None
        if self.min_vix is not None and vix is not None and vix < self.min_vix:
            self.last_reject = f"vix_{vix:.1f}_below_{self.min_vix:g}"
            self.entered_for = expiry.isoformat()  # decided for this expiry: skip
            self._append({"event": "skip", "ts": now.isoformat(), "day": now.strftime("%Y-%m-%d"),
                          "expiry": expiry.isoformat(), "vix": vix, "reason": self.last_reject})
            return None
        ce = quote_ltp(book.get(option_symbol(atm, "CE")))
        pe = quote_ltp(book.get(option_symbol(atm, "PE")))
        if not ce or not pe:
            self.last_reject = "no_atm_quote"
            return None
        F = atm + ce - pe

        def live(opt: str) -> list[int]:
            return [k for k in strikes if (quote_ltp(book.get(option_symbol(k, opt))) or 0) > 0]

        pes, ces = live("PE"), live("CE")
        sc, sp = nearest(ces, F * (1 + self.dist)), nearest(pes, F * (1 - self.dist))
        if sc is None or sp is None:
            self.last_reject = "no_short_strike"
            return None
        spec = []
        if self.wing_pct:
            lc, lp = nearest(ces, F * (1 + self.wing_pct)), nearest(pes, F * (1 - self.wing_pct))
            if lc is None or lp is None or lc <= sc or lp >= sp:
                self.last_reject = "no_wing_strike"
                return None
            spec += [(lc, "CE", +1), (lp, "PE", +1)]  # wings first, as live orders would be
        spec += [(sc, "CE", -1), (sp, "PE", -1)]
        lots = lots_for_vix(vix, self.size_up_vix)
        qty = lots * int(self.lot_size)
        legs: list[Leg] = []
        for k, opt, sign in spec:
            sym = option_symbol(k, opt)
            row = book.get(sym)
            ltp, fill = quote_ltp(row), self._fill(row, selling=sign < 0)
            if not ltp or fill is None:
                self.last_reject = f"no_quote_{k}{opt}"
                return None
            legs.append(Leg(k, opt, sign, sym, round(ltp, 2), round(fill, 2), round(ltp, 2)))
        credit = round(sum(-leg.sign * leg.fill for leg in legs), 2)
        if credit <= 0:
            self.last_reject = "no_credit"
            return None
        charges_open = round(float(kite_nfo_charges([(leg.fill, qty, "sell" if leg.sign < 0 else "buy") for leg in legs])["total"]), 2)
        pos = ExpiryPosition(day=now.strftime("%Y-%m-%d"), expiry=expiry.isoformat(), opened_at=now.isoformat(),
                             legs=legs, qty=qty, charges_open=charges_open, vix=vix, credit=credit)
        ev = self._append({
            "event": "open", "ts": now.isoformat(), "day": pos.day, "expiry": pos.expiry, "side": "short", "style": "short",
            "symbol": ",".join(leg.symbol for leg in legs),
            "legs": [{"strike": leg.strike, "opt": leg.opt, "sign": leg.sign, "symbol": leg.symbol, "entry": leg.entry,
                      "fill": leg.fill} for leg in legs],
            "qty": qty, "lots": lots, "vix": vix, "forward": round(F, 2), "atm": atm,
            "credit_fill": credit, "entry": credit, "charges_open": charges_open,
            "stops": {leg.symbol: round(leg.entry * (1 + self.stop), 2) for leg in legs if leg.sign < 0},
            "reason": f"{self.sessions} session(s) to expiry, short ±{self.dist:.1%}"
            + (f", wings {self.wing_pct:.0%}" if self.wing_pct else ", no wings") + f", {lots} lot(s) at VIX {vix}",
        })
        if ev is None:
            return None
        self.position = pos
        self.entered_for = pos.expiry
        self.last_reject = None
        return ev

    # ---- stops / close ----

    def _leg_stop(self, now: datetime, book: QuoteSource, leg: Leg) -> dict[str, Any] | None:
        pos = self.position
        assert pos is not None
        px = self._fill(book.get(leg.symbol), selling=False)
        if px is None:
            return None
        pts_qty = round((leg.fill - px) * pos.qty, 2)
        charges = round(float(kite_nfo_charges([(px, pos.qty, "buy")])["total"]), 2)
        ev = self._append({"event": "leg_stop", "ts": now.isoformat(), "day": now.strftime("%Y-%m-%d"), "expiry": pos.expiry,
                           "leg": leg.symbol, "entry": leg.fill, "exit": round(px, 2), "mark": leg.mark,
                           "stop_level": round(leg.entry * (1 + self.stop), 2), "pnl_pts_qty": pts_qty, "charges": charges})
        if ev is None:
            return None
        leg.exit = round(px, 2)
        pos.realized += pts_qty
        pos.charges_stops += charges
        return ev

    def _close(self, now: datetime, book: QuoteSource, reason: str) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        known = reason != "expiry_missed"
        gross = pos.realized
        fills = []
        exits = []
        for leg in pos.legs:
            if leg.exit is not None:
                exits.append({"strike": leg.strike, "opt": leg.opt, "exit": leg.exit, "stopped": True})
                continue
            px = self._fill(book.get(leg.symbol), selling=leg.sign > 0)
            if px is None:
                px = leg.mark
                known = False
            exits.append({"strike": leg.strike, "opt": leg.opt, "exit": None if px is None else round(px, 2)})
            if px is None:
                continue
            gross += leg.sign * (px - leg.fill) * pos.qty
            if px > 0:
                fills.append((px, pos.qty, "buy" if leg.sign < 0 else "sell"))
        charges = round(pos.charges_open + pos.charges_stops + float(kite_nfo_charges(fills)["total"]), 2)
        pnl = round(gross - charges, 2) if known else None
        ev = self._append({
            "event": "close", "ts": now.isoformat(), "day": now.strftime("%Y-%m-%d"), "expiry": pos.expiry,
            "side": "short", "style": "short", "symbol": ",".join(leg.symbol for leg in pos.legs),
            "opened_at": pos.opened_at, "qty": pos.qty, "vix": pos.vix, "legs": exits,
            "entry": pos.credit, "exit": round(pos.credit - gross / pos.qty, 2) if known else None,
            "pnl_gross": round(gross, 2) if known else None, "charges": charges, "pnl": pnl, "pnl_known": known,
            "reason": reason,
        })
        if ev is None:
            return None
        self.position = None
        return ev

    # ---- frame ----

    def on_frame(self, *, now: datetime, book: QuoteSource, expiry: date | None, strikes: list[int],
                 option_symbol: Callable[[int, str], str] | None, atm: int | None, vix: float | None,
                 allow_entry: bool = True) -> dict[str, Any] | None:
        today = now.date()
        pos = self.position
        if pos is not None:
            for leg in pos.legs:
                ltp = quote_ltp(book.get(leg.symbol))
                if ltp is not None and ltp >= 0:
                    leg.mark = ltp
            exp = date.fromisoformat(pos.expiry)
            if today > exp:  # engine was down at the expiry close: book at last marks, P&L unknown
                return self._close(now, book, "expiry_missed")
            if not _is_nse_trading_day(today) or not hm_ge(now, MARKET_OPEN) or not hm_lt(now, MARKET_CLOSE):
                return None
            if today == exp and hm_ge(now, EXIT_AT):
                return self._close(now, book, "expiry")
            out = None
            for leg in pos.legs:
                if leg.sign < 0 and leg.exit is None and leg.mark is not None and leg.mark >= leg.entry * (1 + self.stop):
                    out = self._leg_stop(now, book, leg) or out
            if all(leg.exit is not None for leg in pos.legs if leg.sign < 0):
                return self._close(now, book, "all_legs_stopped")
            return out
        if not _is_nse_trading_day(today):
            return None
        if expiry is None or option_symbol is None or atm is None:
            self.last_reject = "no_chain"  # shown on the dashboard: the index's option chain is not loaded
            return None
        if self.last_reject == "no_chain":
            self.last_reject = None
        if self.entered_for == expiry.isoformat():
            return None
        if not hm_ge(now, ENTRY_AT) or hm_ge(now, ENTRY_CLOSED):
            return None
        if sessions_until(today, expiry) != self.sessions:
            return None
        if not allow_entry:
            self.last_reject = "gate_paused"
            return None
        return self._try_open(now, book, expiry, strikes, option_symbol, atm, vix)

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        pos = self.position
        body: dict[str, Any] = {
            "ok": True, "mode": "paper", "live_orders": False, "enabled": True, "book": self.strategy,
            "name": self.name or BOOKS.get(self.strategy, {}).get("name", self.strategy),
            "sessions": self.sessions, "min_vix": self.min_vix, "size_up_vix": self.size_up_vix,
            "last_reject": self.last_reject, "position": None,
        }
        if pos is not None:
            open_pts = 0.0
            known = True
            for leg in pos.legs:
                if leg.exit is not None:
                    continue
                m = quote_ltp(book.get(leg.symbol)) if book is not None else leg.mark
                m = m if m is not None else leg.mark
                if m is None:
                    known = False
                    continue
                open_pts += leg.sign * (m - leg.fill)
            body["position"] = {
                "expiry": pos.expiry, "qty": pos.qty, "vix": pos.vix,
                "legs": [f"{'sell' if leg.sign < 0 else 'buy'} {leg.strike} {leg.opt}" + (" (stopped)" if leg.exit is not None else "")
                         for leg in pos.legs],
                "open_pnl": round(pos.realized + open_pts * pos.qty - pos.charges_open - pos.charges_stops, 2) if known else None,
            }
        return body
