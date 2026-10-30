"""Weekly NIFTY premium-selling paper books. Paper only, never Kite orders.

Rules, backtested on NSE end-of-day prices Feb 2019 – Oct 2026
(scripts/bt_iron_condor_eod.py, doc/weekly-option-selling.md):

- Entry at 15:25, three trading sessions before the weekly expiry (normally Thursday
  for a Tuesday expiry).
- Forward F from put-call parity at the ATM strike; ATM IV from Black-76.
- Sell the put at or below F * exp(-1.5 * IV * sqrt(3/252)) and the call at or above
  F * exp(+1.5 * IV * sqrt(3/252)), using only strikes with a live quote.
- weekly_condor5 also buys wings 5% of F beyond each short strike;
  weekly_strangle has no wings.
- Stop: once a day at 15:25, before expiry day, close everything if the position
  costs >= 3x the entry credit to buy back (both at last traded prices).
- Otherwise hold to expiry and close at 15:29 on expiry day.

Fills are paper: sells at bid, buys at ask when quotes have them, else last price
-/+ SLIP_PTS. Kite charges at current rates. Positions carry overnight and are
restored from the ledger after a restart.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from atlas_lite.agent_gates import _is_nse_trading_day
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger
from atlas_lite.metrics import atm_iv_from_prices, quote_ask, quote_bid, quote_ltp
from atlas_lite.minute_bars import hm_ge

IST = ZoneInfo("Asia/Kolkata")
SD = 1.5
SESSIONS = 3
STOP_MULT = 3.0
ENTRY_AT = (15, 25)
ENTRY_CLOSED = (15, 30)
EXPIRY_CLOSE = (15, 29)
SLIP_PTS = 1.0
DEFAULT_LOT_SIZE = 65

BOOKS: dict[str, dict[str, Any]] = {
    "weekly_strangle": {"ledger": "paper_weekly_strangle.jsonl", "wing_pct": None, "name": "Weekly strangle"},
    "weekly_condor5": {"ledger": "paper_weekly_condor5.jsonl", "wing_pct": 0.05, "name": "Weekly condor (5% wings)"},
}


def paper_weekly_sell_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_WEEKLY_SELL", "1").strip().lower()
    return raw in ("1", "true", "yes")


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


# ---- shared with the backtest ----


def sessions_until(day: date, expiry: date, is_trading_day: Callable[[date], bool] = _is_nse_trading_day) -> int:
    """Trading sessions after ``day`` up to and including ``expiry`` (3 => enter today)."""
    n, cur = 0, day
    while cur < expiry:
        cur += timedelta(days=1)
        if is_trading_day(cur):
            n += 1
    return n


def select_strikes(
    F: float,
    iv: float,
    put_strikes: list[int],
    call_strikes: list[int],
    *,
    sd: float = SD,
    sessions: int = SESSIONS,
    wing_pct: float | None = None,
) -> tuple[int, int, int | None, int | None] | None:
    """Short put/call at +-sd standard deviations over ``sessions``; wings wing_pct*F beyond.

    Strikes come from the lists given (strikes that can actually be traded). Returns
    (short_put, short_call, long_put, long_call); wings are None when wing_pct is None.
    """
    move = sd * iv * math.sqrt(sessions / 252)
    puts = [k for k in put_strikes if k <= F * math.exp(-move)]
    calls = [k for k in call_strikes if k >= F * math.exp(move)]
    if not puts or not calls:
        return None
    sp, sc = max(puts), min(calls)
    if wing_pct is None:
        return sp, sc, None, None
    lps = [k for k in put_strikes if k <= sp - wing_pct * F]
    lcs = [k for k in call_strikes if k >= sc + wing_pct * F]
    if not lps or not lcs:
        return None
    return sp, sc, max(lps), min(lcs)


def iv_decimal(iv: float) -> float:
    return iv / 100.0 if iv > 1 else iv


# ---- the book ----


@dataclass
class Leg:
    strike: int
    opt: str  # "CE" | "PE"
    sign: int  # -1 sold, +1 bought
    symbol: str
    entry: float  # last traded price at entry (for the stop)
    fill: float  # paper fill price
    mark: float | None = None


@dataclass
class WeeklyPosition:
    day: str
    expiry: str
    opened_at: str
    legs: list[Leg]
    credit_ltp: float
    credit_fill: float
    charges_open: float
    forward: float
    iv: float
    qty: int = DEFAULT_LOT_SIZE
    stop_checked: str = ""


@dataclass
class PaperWeeklySell:
    path: Path
    strategy: str
    wing_pct: float | None = None
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = 1
    sd: float = SD
    sessions: int = SESSIONS
    stop_mult: float = STOP_MULT
    position: WeeklyPosition | None = None
    entered_for: str = ""  # expiry already entered (one trade per weekly expiry)
    last_reject: str | None = None
    last_event: dict[str, Any] | None = None
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_weekly_sell"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._restore()

    @property
    def qty(self) -> int:
        return int(self.lots) * int(self.lot_size)

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
            if ev.get("strategy") != self.strategy:
                continue
            if ev.get("event") == "open":
                # The entry day counts as checked (first check is the next session), as in _try_open.
                open_ev = {**ev, "stop_checked": ev.get("day")}
                self.entered_for = str(ev.get("expiry") or "")
            elif ev.get("event") == "close":
                open_ev = None
            elif ev.get("event") == "stop_check" and open_ev is not None:
                open_ev = {**open_ev, "stop_checked": ev.get("day")}
        if open_ev is not None:
            self.position = WeeklyPosition(
                day=str(open_ev["day"]),
                expiry=str(open_ev["expiry"]),
                opened_at=str(open_ev["ts"]),
                legs=[Leg(**{k: leg[k] for k in ("strike", "opt", "sign", "symbol", "entry", "fill")}) for leg in open_ev["legs"]],
                credit_ltp=float(open_ev["credit_ltp"]),
                credit_fill=float(open_ev["credit_fill"]),
                charges_open=float(open_ev["charges_open"]),
                forward=float(open_ev["forward"]),
                iv=float(open_ev["iv"]),
                qty=int(open_ev.get("qty") or self.qty),
                stop_checked=str(open_ev.get("stop_checked") or ""),
            )

    # ---- pricing ----

    @staticmethod
    def _fill(row: dict[str, Any] | None, sign: int, opening: bool) -> float | None:
        """Sell at bid / buy at ask when present, else last price -/+ SLIP_PTS."""
        ltp = quote_ltp(row)
        if ltp is None or ltp <= 0:
            return None
        selling = (sign < 0) == opening
        if selling:
            bid = quote_bid(row)
            return bid if bid and bid > 0 else max(0.05, ltp - SLIP_PTS)
        ask = quote_ask(row)
        return ask if ask and ask > 0 else ltp + SLIP_PTS

    # ---- open / close ----

    def _try_open(
        self,
        now: datetime,
        book: QuoteSource,
        expiry: date,
        strikes: list[int],
        option_symbol: Callable[[int, str], str],
        atm: int,
    ) -> dict[str, Any] | None:
        ce = quote_ltp(book.get(option_symbol(atm, "CE")))
        pe = quote_ltp(book.get(option_symbol(atm, "PE")))
        if not ce or not pe:
            self.last_reject = "no_atm_quote"
            return None
        F = atm + ce - pe
        iv_raw = atm_iv_from_prices(ce, pe, F, float(atm), expiry, now=now)
        if not iv_raw:
            self.last_reject = "no_iv"
            return None
        iv = iv_decimal(iv_raw)

        def live(opt: str) -> list[int]:
            return [k for k in strikes if (quote_ltp(book.get(option_symbol(k, opt))) or 0) > 0]

        picked = select_strikes(F, iv, live("PE"), live("CE"), sd=self.sd, sessions=self.sessions, wing_pct=self.wing_pct)
        if picked is None:
            self.last_reject = "no_strike_far_enough"
            return None
        sp, sc, lp, lc = picked
        spec = [(sp, "PE", -1), (sc, "CE", -1)]
        if lp is not None and lc is not None:
            spec += [(lp, "PE", +1), (lc, "CE", +1)]
        legs: list[Leg] = []
        for k, opt, sign in spec:
            sym = option_symbol(k, opt)
            row = book.get(sym)
            ltp, fill = quote_ltp(row), self._fill(row, sign, opening=True)
            if not ltp or fill is None:
                self.last_reject = f"no_quote_{k}{opt}"
                return None
            legs.append(Leg(k, opt, sign, sym, round(ltp, 2), round(fill, 2), round(ltp, 2)))
        credit_ltp = round(sum(-leg.sign * leg.entry for leg in legs), 2)
        credit_fill = round(sum(-leg.sign * leg.fill for leg in legs), 2)
        if credit_fill <= 0:
            self.last_reject = "no_credit"
            return None
        fills = [(leg.fill, self.qty, "sell" if leg.sign < 0 else "buy") for leg in legs]
        charges_open = round(float(kite_nfo_charges(fills)["total"]), 2)
        pos = WeeklyPosition(
            day=now.strftime("%Y-%m-%d"),
            expiry=expiry.isoformat(),
            opened_at=now.isoformat(),
            legs=legs,
            credit_ltp=credit_ltp,
            credit_fill=credit_fill,
            charges_open=charges_open,
            forward=round(F, 2),
            iv=round(iv, 4),
            qty=self.qty,
            stop_checked=now.strftime("%Y-%m-%d"),  # first stop check is the next session, as backtested
        )
        ev = self._append(
            {
                "event": "open",
                "ts": now.isoformat(),
                "day": pos.day,
                "expiry": pos.expiry,
                "side": "short",
                "style": "short",
                "symbol": ",".join(leg.symbol for leg in legs),
                "legs": [
                    {"strike": leg.strike, "opt": leg.opt, "sign": leg.sign, "symbol": leg.symbol, "entry": leg.entry, "fill": leg.fill}
                    for leg in legs
                ],
                "qty": self.qty,
                "credit_ltp": credit_ltp,
                "credit_fill": credit_fill,
                "entry": credit_fill,
                "stop_level": round(self.stop_mult * credit_ltp, 2),
                "max_loss": self._max_loss(legs, credit_fill),
                "charges_open": charges_open,
                "forward": pos.forward,
                "iv": pos.iv,
                "atm": atm,
                "reason": f"{self.sd:g} SD, {self.sessions} sessions to expiry",
            }
        )
        if ev is None:
            return None
        self.position = pos
        self.entered_for = pos.expiry
        self.last_reject = None
        return ev

    def _max_loss(self, legs: list[Leg], credit: float) -> float | None:
        widths = []
        for opt in ("PE", "CE"):
            short = [leg for leg in legs if leg.opt == opt and leg.sign < 0]
            long = [leg for leg in legs if leg.opt == opt and leg.sign > 0]
            if short and not long:
                return None  # naked side: loss is not capped
            if short and long:
                widths.append(abs(short[0].strike - long[0].strike))
        return round((max(widths) - credit) * self.qty, 2) if widths else None

    def _close(self, now: datetime, book: QuoteSource, reason: str) -> dict[str, Any] | None:
        """Stop: trade out at bid/ask. Expiry: held to settlement, so last price with no slippage
        (the backtest settles at intrinsic). Missing quotes or a missed expiry: P&L unknown."""
        pos = self.position
        if pos is None:
            return None
        fills = []
        debit = 0.0
        known = reason in ("stop", "expiry")
        exits = []
        for leg in pos.legs:
            row = book.get(leg.symbol)
            if reason == "expiry":
                px = quote_ltp(row)
            else:
                px = self._fill(row, leg.sign, opening=False)
            if px is None:
                px = leg.mark  # last price seen this run; None after a restart
                known = False
            exits.append({"strike": leg.strike, "opt": leg.opt, "exit": None if px is None else round(px, 2)})
            if px is None:
                continue
            debit += -leg.sign * px
            if px > 0:
                fills.append((px, pos.qty, "buy" if leg.sign < 0 else "sell"))
        charges = round(pos.charges_open + float(kite_nfo_charges(fills)["total"]), 2)
        gross = round((pos.credit_fill - debit) * pos.qty, 2) if known else None
        pnl = round(gross - charges, 2) if gross is not None else None
        ev = self._append(
            {
                "event": "close",
                "ts": now.isoformat(),
                "day": now.strftime("%Y-%m-%d"),
                "expiry": pos.expiry,
                "side": "short",
                "style": "short",
                "symbol": ",".join(leg.symbol for leg in pos.legs),
                "opened_at": pos.opened_at,
                "qty": pos.qty,
                "entry": pos.credit_fill,
                "exit": round(debit, 2) if known else None,
                "legs": exits,
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

    # ---- frame ----

    def on_frame(
        self,
        *,
        now: datetime,
        book: QuoteSource,
        expiry: date | None,
        strikes: list[int],
        option_symbol: Callable[[int, str], str] | None,
        atm: int | None,
        allow_entry: bool = True,
    ) -> dict[str, Any] | None:
        today = now.date()
        pos = self.position
        if pos is not None:
            for leg in pos.legs:
                ltp = quote_ltp(book.get(leg.symbol))
                if ltp is not None and ltp >= 0:
                    leg.mark = ltp
            exp = date.fromisoformat(pos.expiry)
            if today > exp:  # missed the expiry close (engine down): book at last marks
                return self._close(now, book, "expiry_missed")
            if not _is_nse_trading_day(today):
                return None
            if today == exp:
                return self._close(now, book, "expiry") if hm_ge(now, EXPIRY_CLOSE) else None
            if hm_ge(now, ENTRY_AT) and pos.stop_checked != pos_day(now):
                marks = [leg.mark for leg in pos.legs]
                if any(m is None for m in marks):
                    return None
                cost = sum(-leg.sign * leg.mark for leg in pos.legs)  # type: ignore[operator]
                pos.stop_checked = pos_day(now)
                if cost >= self.stop_mult * pos.credit_ltp:
                    return self._close(now, book, "stop")
                self._append({"event": "stop_check", "ts": now.isoformat(), "day": pos_day(now),
                              "cost_ltp": round(cost, 2), "stop_level": round(self.stop_mult * pos.credit_ltp, 2)})
            return None
        if expiry is None or option_symbol is None or atm is None or not _is_nse_trading_day(today):
            return None
        if self.entered_for == expiry.isoformat():
            return None
        if not hm_ge(now, ENTRY_AT) or hm_ge(now, ENTRY_CLOSED):
            return None
        if sessions_until(today, expiry) != self.sessions:
            return None
        if not allow_entry:
            self.last_reject = "gate_paused"
            return None
        return self._try_open(now, book, expiry, strikes, option_symbol, atm)

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        pos = self.position
        body: dict[str, Any] = {
            "ok": True,
            "mode": "paper",
            "live_orders": False,
            "book": self.strategy,
            "name": BOOKS.get(self.strategy, {}).get("name", self.strategy),
            "enabled": True,
            "wing_pct": self.wing_pct,
            "last_reject": self.last_reject,
            "position": None,
        }
        if pos is not None:
            marks = []
            for leg in pos.legs:
                m = quote_ltp(book.get(leg.symbol)) if book is not None else leg.mark
                marks.append(m if m is not None else leg.mark)
            cost = None if any(m is None for m in marks) else sum(-leg.sign * m for leg, m in zip(pos.legs, marks))
            close_ch = 0.0
            if cost is not None:
                fills = [(m, pos.qty, "buy" if leg.sign < 0 else "sell") for leg, m in zip(pos.legs, marks) if m]
                close_ch = float(kite_nfo_charges(fills)["total"]) if fills else 0.0
            body["position"] = {
                "expiry": pos.expiry,
                "legs": [f"{'sell' if leg.sign < 0 else 'buy'} {leg.strike} {leg.opt}" for leg in pos.legs],
                "credit": pos.credit_fill,
                "cost_now": None if cost is None else round(cost, 2),
                "stop_level": round(self.stop_mult * pos.credit_ltp, 2),
                # Net of entry charges and estimated exit charges, like the ledger's pnl.
                "open_pnl": None if cost is None else round((pos.credit_fill - cost) * pos.qty - pos.charges_open - close_ch, 2),
            }
        return body


def pos_day(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")
