"""Paper long iron butterfly — ATM longs + fitted hedge (never Kite orders).

Sensibull “Long Iron Butterfly” (width 0 / hedge ~250): buy ATM CE+PE,
sell ATM±hedge. V-shaped (profit outside, loss if spot sits). Same
red-first management as the condor, but ATM longs show the open faster.

Hedge is scanned 150–300 for POP near ~55% (screenshot 57%). Long
butterflies are debit-heavy (R/R often ~0.4) — we do not require R/R ≥ 1.2.
Skip when expected move cannot clear the debit (expiry-morning crush).

Expected move = ATM IV × spot × √T (252-day), else ATM straddle.

NFO cannot fill before 09:15 IST. First four-leg print **09:15**, watch
**2 minutes**, then close the red vertical first. Flat or red leftover
must be out in **30 minutes**. A green vertical that is actually in
profit can run until giveback / turn-red / **15:14**.

* **1 lot**, max **1**/day, separate ledger.
* No 0.75% short-vol cap — this book wants expansion.
* Retired (off by default): 25 Sep live day was -₹630. Re-enable with
  ``ATLAS_LITE_PAPER_LONG_IC=1``.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from atlas_lite.instruments import NIFTY_STRIKE_STEP
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger, ist_now
from atlas_lite.metrics import quote_ltp, trading_years_to_expiry
from atlas_lite.minute_bars import hm_ge, hm_le

STRATEGY = "long_iron_condor"
CAPITAL = 200_000.0
LOTS = 1
DEFAULT_LOT_SIZE = 65
MAX_ENTRIES_PER_DAY = 1
# First NFO print. Cash pre-open cannot fill options.
ENTRY_AFTER = (9, 15)
ENTRY_UNTIL = (9, 25)
SQUARE_OFF = (15, 14)
MAX_HOLD_MINUTES = 30
VALIDATE_MINUTES = 2
# Live fit is ATM longs (width 0). Fallback hedge if the scan has no quote.
LONG_OTM_PTS = 0
WING_PTS = 250
DEBIT_MIN = 15.0
DEBIT_MAX_SLACK = 20.0
MIN_PROFIT_PTS = 25.0
RED_PTS = 2.0
MIN_GREEN_PTS = 8.0
GIVEBACK = 0.40
POP_TARGET = 0.55
POP_MIN = 0.45
POP_MAX = 0.68
HEDGE_CHOICES = (150, 200, 250, 300)
IV_FALLBACK = 12.0

OptionSymbolFn = Callable[[int, str], str]

Side = Literal["call", "put"]


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


@dataclass
class LicQuotes:
    ce_long: float
    ce_short: float
    pe_long: float
    pe_short: float


def paper_long_ic_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_LONG_IC", "0").strip().lower()
    return raw in ("1", "true", "yes")


def in_long_ic_entry_window(now: datetime) -> bool:
    return hm_ge(now, ENTRY_AFTER) and hm_le(now, ENTRY_UNTIL) and not hm_ge(now, SQUARE_OFF)


def long_iron_condor_strikes(
    atm: int,
    *,
    long_otm: int = LONG_OTM_PTS,
    wing: int = WING_PTS,
) -> tuple[int, int, int, int]:
    """PE short, PE long, CE long, CE short.

    Default is the long iron butterfly (ATM longs, ``wing`` shorts).
    """
    a = int(atm)
    lo = int(long_otm)
    w = int(wing)
    return a - lo - w, a - lo, a + lo, a + lo + w


def condor_debit(
    ce_long: float,
    ce_short: float,
    pe_long: float,
    pe_short: float,
) -> float:
    return round(
        (float(ce_long) + float(pe_long)) - (float(ce_short) + float(pe_short)),
        2,
    )


def _cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def expected_move_pts(
    spot: float | None,
    iv_pct: float | None,
    expiry: date | None,
    now: datetime,
    straddle: float | None = None,
) -> float | None:
    """Remaining-life expected move (pts). IV first, then ATM straddle."""
    if spot is not None and spot > 0 and expiry is not None:
        tte = trading_years_to_expiry(expiry, now=now)
        vol = float(iv_pct) if iv_pct is not None and iv_pct > 0 else IV_FALLBACK
        if tte > 0 and vol > 0:
            return round(float(spot) * (vol / 100.0) * math.sqrt(tte), 2)
    if straddle is not None and straddle > 0:
        return round(float(straddle), 2)
    return None


def long_ic_pop(long_otm: float, debit: float, sigma: float) -> float:
    """P(|move| clears the debit breakeven), two-tailed normal.

    Butterfly (longs at ATM): breakeven ≈ debit. Condor: long_otm + debit.
    """
    if sigma <= 0:
        return 0.0
    be = max(float(long_otm) + max(float(debit), 0.0), 1.0)
    pop = 2.0 * (1.0 - _cdf(be / sigma))
    return round(max(0.0, min(1.0, pop)), 4)


def debit_ok(debit: float, hedge: float, *, debit_min: float = DEBIT_MIN) -> bool:
    return debit_min <= float(debit) <= (float(hedge) - DEBIT_MAX_SLACK)


def score_long_ic(pop: float, max_profit_pts: float) -> float:
    """Higher is better. Target POP ~55%; keep some room to the wings."""
    room = min(max(float(max_profit_pts), 0.0), 80.0) / 80.0
    return round(-abs(pop - POP_TARGET) * 2.0 + 0.08 * room, 6)


@dataclass
class LicFit:
    long_otm: int
    wing_pts: int
    pe_short_strike: int
    pe_long_strike: int
    ce_long_strike: int
    ce_short_strike: int
    symbols: dict[str, str]
    quotes: LicQuotes
    debit: float
    pop: float
    rr: float
    expected_move: float
    score: float


def fit_long_iron_condor(
    atm: int,
    *,
    book: QuoteSource,
    option_symbol: OptionSymbolFn,
    now: datetime,
    spot: float | None = None,
    iv_pct: float | None = None,
    expiry: date | None = None,
    straddle: float | None = None,
    step: int = NIFTY_STRIKE_STEP,
    debit_min: float = DEBIT_MIN,
) -> LicFit | None:
    """Pick ATM-long hedge on the live chain for POP near 55%."""
    del step
    sigma = expected_move_pts(spot, iv_pct, expiry, now, straddle)
    if sigma is None or sigma <= 0:
        return None
    best: LicFit | None = None
    long_otm = 0
    for hedge in HEDGE_CHOICES:
        pe_s, pe_l, ce_l, ce_s = long_iron_condor_strikes(
            int(atm), long_otm=long_otm, wing=int(hedge)
        )
        if pe_s <= 0:
            continue
        symbols = {
            "pe_short": option_symbol(pe_s, "PE"),
            "pe_long": option_symbol(pe_l, "PE"),
            "ce_long": option_symbol(ce_l, "CE"),
            "ce_short": option_symbol(ce_s, "CE"),
        }
        px = {k: quote_ltp(book.get(sym)) for k, sym in symbols.items()}
        if any(v is None or v <= 0 for v in px.values()):
            continue
        q = LicQuotes(
            ce_long=float(px["ce_long"]),
            ce_short=float(px["ce_short"]),
            pe_long=float(px["pe_long"]),
            pe_short=float(px["pe_short"]),
        )
        debit = condor_debit(q.ce_long, q.ce_short, q.pe_long, q.pe_short)
        if not debit_ok(debit, hedge, debit_min=debit_min):
            continue
        max_profit = float(hedge) - debit
        if max_profit < MIN_PROFIT_PTS:
            continue
        rr = max_profit / debit
        pop = long_ic_pop(long_otm, debit, sigma)
        if pop < POP_MIN or pop > POP_MAX:
            continue
        scored = score_long_ic(pop, max_profit)
        cand = LicFit(
            long_otm=int(long_otm),
            wing_pts=int(hedge),
            pe_short_strike=pe_s,
            pe_long_strike=pe_l,
            ce_long_strike=ce_l,
            ce_short_strike=ce_s,
            symbols=symbols,
            quotes=q,
            debit=debit,
            pop=pop,
            rr=round(rr, 3),
            expected_move=float(sigma),
            score=scored,
        )
        if best is None or cand.score > best.score:
            best = cand
    return best


def vertical_mtm_pts(
    long_now: float,
    short_now: float,
    long_entry: float,
    short_entry: float,
) -> float:
    """Debit-spread mark: change in (long − short)."""
    return round(
        (float(long_now) - float(short_now)) - (float(long_entry) - float(short_entry)),
        2,
    )


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _weekday(now: datetime) -> bool:
    return now.weekday() < 5


def _close_vertical_legs(long_px: float, short_px: float, qty: int) -> list[tuple[float, int, str]]:
    return [(float(long_px), qty, "sell"), (float(short_px), qty, "buy")]


def _side_open_legs(long_px: float, short_px: float, qty: int) -> list[tuple[float, int, str]]:
    return [(float(long_px), qty, "buy"), (float(short_px), qty, "sell")]


@dataclass
class LicPosition:
    day: str
    atm: int
    qty: int
    lots: int
    long_otm: int
    wing_pts: int
    pe_short_strike: int
    pe_long_strike: int
    ce_long_strike: int
    ce_short_strike: int
    pe_short_symbol: str
    pe_long_symbol: str
    ce_long_symbol: str
    ce_short_symbol: str
    pe_short_entry: float
    pe_long_entry: float
    ce_long_entry: float
    ce_short_entry: float
    debit: float
    opened_at: str
    charges_open: float
    call_open_charges: float
    put_open_charges: float
    pop: float = 0.0
    rr: float = 0.0
    expected_move: float = 0.0
    call_open: bool = True
    put_open: bool = True
    validated: bool = False
    call_peak_pts: float = 0.0
    put_peak_pts: float = 0.0
    charges_closed: float = 0.0


@dataclass
class PaperLongIronCondor:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = LOTS
    capital: float = CAPITAL
    max_entries_per_day: int = MAX_ENTRIES_PER_DAY
    long_otm: int = LONG_OTM_PTS
    wing_pts: int = WING_PTS
    debit_min: float = DEBIT_MIN
    validate_minutes: int = VALIDATE_MINUTES
    max_hold_minutes: int = MAX_HOLD_MINUTES
    red_pts: float = RED_PTS
    min_green_pts: float = MIN_GREEN_PTS
    giveback: float = GIVEBACK
    position: LicPosition | None = None
    traded_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    eod_written: bool = False
    last_event: dict[str, Any] | None = None
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_long_ic"), repr=False)

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
        eod_written = False
        last_any: dict[str, Any] | None = None
        call_open = True
        put_open = True
        validated = False
        call_peak = 0.0
        put_peak = 0.0
        charges_closed = 0.0
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
                call_open = True
                put_open = True
                validated = False
                call_peak = 0.0
                put_peak = 0.0
                charges_closed = 0.0
            if kind == "open":
                last_open = ev
                entries += 1
                call_open = True
                put_open = True
                validated = bool(ev.get("validated"))
                call_peak = float(ev.get("call_peak_pts") or 0)
                put_peak = float(ev.get("put_peak_pts") or 0)
                charges_closed = 0.0
            elif kind == "close_vertical":
                side = str(ev.get("side") or "")
                if side == "call":
                    call_open = False
                elif side == "put":
                    put_open = False
                validated = True
                if ev.get("call_peak_pts") is not None:
                    call_peak = float(ev["call_peak_pts"])
                if ev.get("put_peak_pts") is not None:
                    put_peak = float(ev["put_peak_pts"])
                if ev.get("charges_closed") is not None:
                    charges_closed = float(ev["charges_closed"])
                elif ev.get("charges_close") is not None:
                    charges_closed = round(charges_closed + float(ev["charges_close"]), 2)
                if ev.get("day_pnl") is not None:
                    day_pnl = float(ev["day_pnl"])
                if not call_open and not put_open:
                    last_open = None
            elif kind == "close":
                last_open = None
                call_open = False
                put_open = False
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
        self.entries_today = entries
        self.day_pnl = round(day_pnl, 2)
        self.eod_written = bool(eod_written)
        if last_open and (call_open or put_open):
            self.position = self._position_from_open(
                last_open,
                call_open=call_open,
                put_open=put_open,
                validated=validated,
                call_peak_pts=call_peak,
                put_peak_pts=put_peak,
                charges_closed=charges_closed,
            )

    def _position_from_open(
        self,
        event: dict[str, Any],
        *,
        call_open: bool,
        put_open: bool,
        validated: bool,
        call_peak_pts: float,
        put_peak_pts: float,
        charges_closed: float,
    ) -> LicPosition:
        qty = int(event.get("qty") or self.lots * self.lot_size)
        lots = int(event.get("lots") or self.lots)
        wing = int(event["wing_pts"]) if event.get("wing_pts") is not None else self.wing_pts
        long_otm = int(event["long_otm"]) if event.get("long_otm") is not None else self.long_otm
        atm = int(event.get("atm") or 0)
        pe_s, pe_l, ce_l, ce_s = long_iron_condor_strikes(atm, long_otm=long_otm, wing=wing)
        call_ch = _f(event.get("call_open_charges"))
        put_ch = _f(event.get("put_open_charges"))
        open_ch = float(event.get("charges") or 0.0)
        if call_ch is None or put_ch is None:
            half = round(open_ch / 2.0, 2)
            call_ch = half if call_ch is None else call_ch
            put_ch = round(open_ch - call_ch, 2) if put_ch is None else put_ch
        return LicPosition(
            day=str(event["day"]),
            atm=atm,
            qty=qty,
            lots=lots,
            long_otm=long_otm,
            wing_pts=wing,
            pe_short_strike=int(event.get("pe_short_strike") or pe_s),
            pe_long_strike=int(event.get("pe_long_strike") or pe_l),
            ce_long_strike=int(event.get("ce_long_strike") or ce_l),
            ce_short_strike=int(event.get("ce_short_strike") or ce_s),
            pe_short_symbol=str(event.get("pe_short_symbol") or ""),
            pe_long_symbol=str(event.get("pe_long_symbol") or ""),
            ce_long_symbol=str(event.get("ce_long_symbol") or ""),
            ce_short_symbol=str(event.get("ce_short_symbol") or ""),
            pe_short_entry=float(event.get("pe_short_entry") or 0),
            pe_long_entry=float(event.get("pe_long_entry") or 0),
            ce_long_entry=float(event.get("ce_long_entry") or 0),
            ce_short_entry=float(event.get("ce_short_entry") or 0),
            debit=float(event.get("debit") or 0),
            opened_at=str(event.get("ts") or event.get("opened_at") or ""),
            charges_open=open_ch,
            call_open_charges=float(call_ch),
            put_open_charges=float(put_ch),
            pop=float(event.get("pop") or 0),
            rr=float(event.get("rr") or 0),
            expected_move=float(event.get("expected_move") or 0),
            call_open=call_open,
            put_open=put_open,
            validated=validated,
            call_peak_pts=float(call_peak_pts),
            put_peak_pts=float(put_peak_pts),
            charges_closed=float(charges_closed),
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
            self._log.warning("paper long-IC ledger write failed: %s", exc)
            return None
        self.last_event = event
        return event

    def _held_quotes(self, book: QuoteSource | None, pos: LicPosition) -> LicQuotes | None:
        """Quotes for still-open legs only. Closed-side prices are placeholders."""
        if book is None:
            return None
        if pos.call_open:
            ce_l = quote_ltp(book.get(pos.ce_long_symbol))
            ce_s = quote_ltp(book.get(pos.ce_short_symbol))
            if ce_l is None or ce_s is None:
                return None
        else:
            ce_l, ce_s = pos.ce_long_entry, pos.ce_short_entry
        if pos.put_open:
            pe_l = quote_ltp(book.get(pos.pe_long_symbol))
            pe_s = quote_ltp(book.get(pos.pe_short_symbol))
            if pe_l is None or pe_s is None:
                return None
        else:
            pe_l, pe_s = pos.pe_long_entry, pos.pe_short_entry
        return LicQuotes(ce_long=ce_l, ce_short=ce_s, pe_long=pe_l, pe_short=pe_s)

    def _remaining_open_charges(self, pos: LicPosition) -> float:
        alloc = 0.0
        if pos.call_open:
            alloc += pos.call_open_charges
        if pos.put_open:
            alloc += pos.put_open_charges
        return round(alloc, 2)

    def _open_quotes(
        self,
        book: QuoteSource | None,
        symbols: dict[str, str],
    ) -> LicQuotes | None:
        if book is None:
            return None
        needed = ("ce_long", "ce_short", "pe_long", "pe_short")
        if any(not symbols.get(k) for k in needed):
            return None
        px = {k: quote_ltp(book.get(symbols[k])) for k in needed}
        if any(v is None or v <= 0 for v in px.values()):
            return None
        return LicQuotes(
            ce_long=float(px["ce_long"]),
            ce_short=float(px["ce_short"]),
            pe_long=float(px["pe_long"]),
            pe_short=float(px["pe_short"]),
        )

    def _debit_ok(self, debit: float, hedge: float | None = None) -> bool:
        return debit_ok(debit, hedge if hedge is not None else self.wing_pts, debit_min=self.debit_min)

    def _opened_at(self, pos: LicPosition) -> datetime | None:
        if not pos.opened_at:
            return None
        try:
            return datetime.fromisoformat(pos.opened_at)
        except ValueError:
            return None

    def _max_hold_due(self, pos: LicPosition, now: datetime) -> bool:
        opened = self._opened_at(pos)
        if opened is None:
            return hm_ge(now, SQUARE_OFF)
        return now >= opened + timedelta(minutes=int(self.max_hold_minutes))

    def _validate_ready(self, pos: LicPosition, now: datetime) -> bool:
        if pos.validated:
            return True
        if not pos.opened_at:
            return hm_ge(now, (ENTRY_AFTER[0], ENTRY_AFTER[1] + self.validate_minutes))
        try:
            opened = datetime.fromisoformat(pos.opened_at)
        except ValueError:
            return hm_ge(now, (ENTRY_AFTER[0], ENTRY_AFTER[1] + self.validate_minutes))
        return now >= opened + timedelta(minutes=int(self.validate_minutes))

    def _side_mtm(self, pos: LicPosition, q: LicQuotes, side: Side) -> float:
        if side == "call":
            return vertical_mtm_pts(q.ce_long, q.ce_short, pos.ce_long_entry, pos.ce_short_entry)
        return vertical_mtm_pts(q.pe_long, q.pe_short, pos.pe_long_entry, pos.pe_short_entry)

    def _touch_peaks(self, pos: LicPosition, q: LicQuotes) -> None:
        if pos.call_open:
            pos.call_peak_pts = max(pos.call_peak_pts, self._side_mtm(pos, q, "call"))
        if pos.put_open:
            pos.put_peak_pts = max(pos.put_peak_pts, self._side_mtm(pos, q, "put"))

    def _has_green_profit(self, pos: LicPosition, q: LicQuotes) -> bool:
        """True if a still-open vertical is actually making money."""
        if pos.call_open and self._side_mtm(pos, q, "call") >= self.min_green_pts:
            return True
        if pos.put_open and self._side_mtm(pos, q, "put") >= self.min_green_pts:
            return True
        return False

    def _open_pnl(self, book: QuoteSource | None) -> tuple[float, float, float]:
        pos = self.position
        if pos is None:
            return 0.0, 0.0, 0.0
        q = self._held_quotes(book, pos)
        alloc = self._remaining_open_charges(pos)
        if q is None:
            return 0.0, 0.0, alloc
        gross = 0.0
        close_legs: list[tuple[float, int, str]] = []
        if pos.call_open:
            gross += self._side_mtm(pos, q, "call") * pos.qty
            close_legs.extend(_close_vertical_legs(q.ce_long, q.ce_short, pos.qty))
        if pos.put_open:
            gross += self._side_mtm(pos, q, "put") * pos.qty
            close_legs.extend(_close_vertical_legs(q.pe_long, q.pe_short, pos.qty))
        close_ch = float(kite_nfo_charges(close_legs)["total"]) if close_legs else 0.0
        charges = round(alloc + close_ch, 2)
        return round(gross - charges, 2), round(gross, 2), charges

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        open_pnl, open_gross, charges = self._open_pnl(book)
        pos_body = None
        if self.position is not None:
            pos = self.position
            pos_body = {
                "day": pos.day,
                "strategy": STRATEGY,
                "atm": pos.atm,
                "qty": pos.qty,
                "lots": pos.lots,
                "long_otm": pos.long_otm,
                "wing_pts": pos.wing_pts,
                "pe_short_strike": pos.pe_short_strike,
                "pe_long_strike": pos.pe_long_strike,
                "ce_long_strike": pos.ce_long_strike,
                "ce_short_strike": pos.ce_short_strike,
                "pe_short_symbol": pos.pe_short_symbol,
                "pe_long_symbol": pos.pe_long_symbol,
                "ce_long_symbol": pos.ce_long_symbol,
                "ce_short_symbol": pos.ce_short_symbol,
                "pe_short_entry": pos.pe_short_entry,
                "pe_long_entry": pos.pe_long_entry,
                "ce_long_entry": pos.ce_long_entry,
                "ce_short_entry": pos.ce_short_entry,
                "debit": pos.debit,
                "pop": pos.pop,
                "rr": pos.rr,
                "expected_move": pos.expected_move,
                "opened_at": pos.opened_at,
                "charges_open": pos.charges_open,
                "call_open": pos.call_open,
                "put_open": pos.put_open,
                "validated": pos.validated,
                "call_peak_pts": pos.call_peak_pts,
                "put_peak_pts": pos.put_peak_pts,
            }
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
            "max_hold_minutes": int(self.max_hold_minutes),
            "entry_window": (
                f"{ENTRY_AFTER[0]:02d}:{ENTRY_AFTER[1]:02d}"
                f"-{ENTRY_UNTIL[0]:02d}:{ENTRY_UNTIL[1]:02d}"
            ),
            "validate_minutes": self.validate_minutes,
            "long_otm": self.long_otm,
            "wing_pts": self.wing_pts,
            "pop_target": POP_TARGET,
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
        atm: int | None,
        symbols: dict[str, str] | None = None,
        option_symbol: OptionSymbolFn | None = None,
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
            closed = self._maybe_exit(now, book)
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
        if not in_long_ic_entry_window(now):
            return None
        return self._open(now, feed, book, atm, symbols or {}, option_symbol)

    def _parse_expiry(self, feed: dict[str, Any]) -> date | None:
        raw = feed.get("expiry")
        if isinstance(raw, date) and not isinstance(raw, datetime):
            return raw
        if isinstance(raw, datetime):
            return raw.date()
        if isinstance(raw, str) and raw:
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                return None
        return None

    def _open(
        self,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource | None,
        atm: int | None,
        symbols: dict[str, str],
        option_symbol: OptionSymbolFn | None,
    ) -> dict[str, Any] | None:
        if atm is None:
            return None
        qty = int(self.lots) * int(self.lot_size)
        if qty <= 0:
            return None
        fit: LicFit | None = None
        if book is not None and option_symbol is not None:
            ce = _f(feed.get("ce"))
            pe = _f(feed.get("pe"))
            straddle = (ce + pe) if ce and pe and ce > 0 and pe > 0 else None
            fit = fit_long_iron_condor(
                int(atm),
                book=book,
                option_symbol=option_symbol,
                now=now,
                spot=_f(feed.get("spot")) or float(atm),
                iv_pct=_f(feed.get("iv")),
                expiry=self._parse_expiry(feed),
                straddle=straddle,
                debit_min=self.debit_min,
            )
        if fit is not None:
            symbols = fit.symbols
            q = fit.quotes
            debit = fit.debit
            long_otm = fit.long_otm
            wing_pts = fit.wing_pts
            pe_s, pe_l, ce_l, ce_s = (
                fit.pe_short_strike,
                fit.pe_long_strike,
                fit.ce_long_strike,
                fit.ce_short_strike,
            )
            pop, rr, expected_move = fit.pop, fit.rr, fit.expected_move
        elif option_symbol is not None:
            # Live scan ran and rejected (POP / debit / quotes). Do not
            # sneak in the static 250 fallback.
            return None
        else:
            q = self._open_quotes(book, symbols)
            if q is None:
                return None
            debit = condor_debit(q.ce_long, q.ce_short, q.pe_long, q.pe_short)
            if not self._debit_ok(debit):
                return None
            long_otm = int(self.long_otm)
            wing_pts = int(self.wing_pts)
            pe_s, pe_l, ce_l, ce_s = long_iron_condor_strikes(
                int(atm), long_otm=long_otm, wing=wing_pts
            )
            pop = rr = expected_move = 0.0
        if not symbols.get("pe_short"):
            return None
        call_open_ch = float(
            kite_nfo_charges(_side_open_legs(q.ce_long, q.ce_short, qty))["total"]
        )
        put_open_ch = float(
            kite_nfo_charges(_side_open_legs(q.pe_long, q.pe_short, qty))["total"]
        )
        charges_open = round(call_open_ch + put_open_ch, 2)
        pos = LicPosition(
            day=now.strftime("%Y-%m-%d"),
            atm=int(atm),
            qty=qty,
            lots=int(self.lots),
            long_otm=int(long_otm),
            wing_pts=int(wing_pts),
            pe_short_strike=pe_s,
            pe_long_strike=pe_l,
            ce_long_strike=ce_l,
            ce_short_strike=ce_s,
            pe_short_symbol=str(symbols["pe_short"]),
            pe_long_symbol=str(symbols["pe_long"]),
            ce_long_symbol=str(symbols["ce_long"]),
            ce_short_symbol=str(symbols["ce_short"]),
            pe_short_entry=round(q.pe_short, 2),
            pe_long_entry=round(q.pe_long, 2),
            ce_long_entry=round(q.ce_long, 2),
            ce_short_entry=round(q.ce_short, 2),
            debit=debit,
            opened_at=now.isoformat(),
            charges_open=charges_open,
            call_open_charges=round(call_open_ch, 2),
            put_open_charges=round(put_open_ch, 2),
            pop=float(pop),
            rr=float(rr),
            expected_move=float(expected_move),
        )
        event = self._append(
            {
                "event": "open",
                "ts": now.isoformat(),
                "day": pos.day,
                "atm": pos.atm,
                "qty": pos.qty,
                "lots": pos.lots,
                "lot_size": self.lot_size,
                "long_otm": pos.long_otm,
                "wing_pts": pos.wing_pts,
                "pe_short_strike": pos.pe_short_strike,
                "pe_long_strike": pos.pe_long_strike,
                "ce_long_strike": pos.ce_long_strike,
                "ce_short_strike": pos.ce_short_strike,
                "pe_short_symbol": pos.pe_short_symbol,
                "pe_long_symbol": pos.pe_long_symbol,
                "ce_long_symbol": pos.ce_long_symbol,
                "ce_short_symbol": pos.ce_short_symbol,
                "pe_short_entry": pos.pe_short_entry,
                "pe_long_entry": pos.pe_long_entry,
                "ce_long_entry": pos.ce_long_entry,
                "ce_short_entry": pos.ce_short_entry,
                "debit": pos.debit,
                "pop": pos.pop,
                "rr": pos.rr,
                "expected_move": pos.expected_move,
                "charges": pos.charges_open,
                "call_open_charges": pos.call_open_charges,
                "put_open_charges": pos.put_open_charges,
                "validate_minutes": self.validate_minutes,
            }
        )
        if event is None:
            return None
        self.position = pos
        self.traded_day = pos.day
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
        q = self._held_quotes(book, pos)
        if q is None:
            return self._close_all(now, None, reason, marked=False, book_day=book_day)
        return self._close_all(now, q, reason, book_day=book_day)

    def _maybe_exit(self, now: datetime, book: QuoteSource | None) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        q = self._held_quotes(book, pos)
        session_due = hm_ge(now, SQUARE_OFF)
        hold_due = self._max_hold_due(pos, now)
        if q is None:
            # Mid-session stale wing: wait. Unmarked flatten only at 15:14.
            if session_due:
                return self._close_all(now, None, "time_flat", marked=False)
            return None
        self._touch_peaks(pos, q)
        if session_due:
            return self._close_all(now, q, "time")
        if not self._validate_ready(pos, now) and not hold_due:
            return None
        first_validate = not pos.validated
        pos.validated = True
        call_mtm = self._side_mtm(pos, q, "call") if pos.call_open else None
        put_mtm = self._side_mtm(pos, q, "put") if pos.put_open else None
        if pos.call_open and pos.put_open:
            call_red = call_mtm is not None and call_mtm <= -self.red_pts
            put_red = put_mtm is not None and put_mtm <= -self.red_pts
            if call_red and put_red:
                reason = "validate_fail" if first_validate else "both_red"
                return self._close_all(now, q, reason)
            if call_red:
                return self._close_vertical(now, q, "call", "red")
            if put_red:
                return self._close_vertical(now, q, "put", "red")
            if hold_due:
                call_green = call_mtm is not None and call_mtm >= self.min_green_pts
                put_green = put_mtm is not None and put_mtm >= self.min_green_pts
                if not call_green and not put_green:
                    return self._close_all(now, q, "max_hold")
                if call_green and not put_green:
                    return self._close_vertical(now, q, "put", "max_hold")
                if put_green and not call_green:
                    return self._close_vertical(now, q, "call", "max_hold")
            return None
        side: Side = "call" if pos.call_open else "put"
        mtm = call_mtm if side == "call" else put_mtm
        peak = pos.call_peak_pts if side == "call" else pos.put_peak_pts
        if mtm is None:
            return None
        if mtm <= 0:
            return self._close_vertical(now, q, side, "turn_red")
        if peak >= self.min_green_pts and (peak - mtm) >= self.giveback * peak:
            return self._close_vertical(now, q, side, "giveback")
        if hold_due and not self._has_green_profit(pos, q):
            return self._close_all(now, q, "max_hold")
        return None

    def _close_vertical(
        self,
        now: datetime,
        q: LicQuotes,
        side: Side,
        reason: str,
        *,
        book_day: str | None = None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        if side == "call" and not pos.call_open:
            return None
        if side == "put" and not pos.put_open:
            return None
        if side == "call":
            long_px, short_px = q.ce_long, q.ce_short
            long_e, short_e = pos.ce_long_entry, pos.ce_short_entry
            open_ch = pos.call_open_charges
        else:
            long_px, short_px = q.pe_long, q.pe_short
            long_e, short_e = pos.pe_long_entry, pos.pe_short_entry
            open_ch = pos.put_open_charges
        last_side = (side == "call" and not pos.put_open) or (side == "put" and not pos.call_open)
        mtm_pts = vertical_mtm_pts(long_px, short_px, long_e, short_e)
        pnl_gross = round(mtm_pts * pos.qty, 2)
        charges_close = float(kite_nfo_charges(_close_vertical_legs(long_px, short_px, pos.qty))["total"])
        charges = round(open_ch + charges_close, 2)
        pnl = round(pnl_gross - charges, 2)
        book_day = book_day or pos.day
        rolling = book_day != (self.traded_day or pos.day)
        new_day_pnl = 0.0 if rolling else self.day_pnl
        new_day_pnl = round(new_day_pnl + pnl, 2)
        new_closed = round(pos.charges_closed + charges_close, 2)
        event_kind = "close" if last_side else "close_vertical"
        event = self._append(
            {
                "event": event_kind,
                "ts": now.isoformat(),
                "day": book_day,
                "reason": reason,
                "side": side,
                "atm": pos.atm,
                "qty": pos.qty,
                "long_exit": round(float(long_px), 2),
                "short_exit": round(float(short_px), 2),
                "mtm_pts": mtm_pts,
                "call_peak_pts": pos.call_peak_pts,
                "put_peak_pts": pos.put_peak_pts,
                "pnl_gross": pnl_gross,
                "charges": charges,
                "charges_open": open_ch,
                "charges_close": charges_close,
                "charges_closed": new_closed,
                "pnl": pnl,
                "pnl_known": True,
                "opened_at": pos.opened_at,
                "day_pnl": new_day_pnl,
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
        pos.charges_closed = new_closed
        if side == "call":
            pos.call_open = False
        else:
            pos.put_open = False
        self.day_pnl = new_day_pnl
        if last_side:
            self.position = None
        return event

    def _close_all(
        self,
        now: datetime,
        q: LicQuotes | None,
        reason: str,
        *,
        marked: bool = True,
        book_day: str | None = None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        book_day = book_day or pos.day
        rolling = book_day != (self.traded_day or pos.day)
        new_day_pnl = 0.0 if rolling else self.day_pnl
        pnl = None
        pnl_gross = None
        charges = None
        charges_close = None
        sides: list[str] = []
        if marked and q is not None:
            close_legs: list[tuple[float, int, str]] = []
            gross = 0.0
            alloc = 0.0
            if pos.call_open:
                sides.append("call")
                gross += self._side_mtm(pos, q, "call") * pos.qty
                close_legs.extend(_close_vertical_legs(q.ce_long, q.ce_short, pos.qty))
                alloc += pos.call_open_charges
            if pos.put_open:
                sides.append("put")
                gross += self._side_mtm(pos, q, "put") * pos.qty
                close_legs.extend(_close_vertical_legs(q.pe_long, q.pe_short, pos.qty))
                alloc += pos.put_open_charges
            pnl_gross = round(gross, 2)
            charges_close = float(kite_nfo_charges(close_legs)["total"]) if close_legs else 0.0
            charges = round(alloc + charges_close, 2)
            pnl = round(pnl_gross - charges, 2)
            new_day_pnl = round(new_day_pnl + pnl, 2)
        event = self._append(
            {
                "event": "close",
                "ts": now.isoformat(),
                "day": book_day,
                "reason": reason,
                "side": "+".join(sides) if sides else None,
                "atm": pos.atm,
                "qty": pos.qty,
                "ce_long_exit": None if q is None else round(q.ce_long, 2),
                "ce_short_exit": None if q is None else round(q.ce_short, 2),
                "pe_long_exit": None if q is None else round(q.pe_long, 2),
                "pe_short_exit": None if q is None else round(q.pe_short, 2),
                "call_peak_pts": pos.call_peak_pts,
                "put_peak_pts": pos.put_peak_pts,
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
            self.entries_today = 1
        self.position = None
        self.day_pnl = new_day_pnl
        return event
