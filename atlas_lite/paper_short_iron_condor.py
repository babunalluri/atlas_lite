"""Paper short iron condor — credit wings 4–5 pts/side (never Kite orders).

Customer book (Sensibull-style short IC):

* Fit CE and PE credit verticals so **sell − buy ∈ [4, 5]** each side.
* Wings follow Sensibull-style widths (**100–400 pts**, prefer **300–400** when in band).
* Fills use **bid/ask** when present (sell@bid, buy@ask); else LTP ± light slip.
* Stops use **mid/LTP** (not ask/bid) and must hold **3s**; no stop checks before 09:20.
* Overall take-profit when **fillable** (ask/bid) MTM ≥ **1% of fixed capital**.
* **Hold to weekly expiry** (overnight carry). No daily 15:14 flatten.
* Per-set stop: when mid close-debit reaches **4× entry credit** (fill still @ ask/bid).
* After a set **stops** (not after a profit-take), try **re-entry** on that
  side if a new 4–5 credit vertical exists at the **current** ATM; that
  re-entered set targets **0.5% of capital**. No re-entry after 12:00 on expiry day.
* Past expiry with missing quotes: settle at **expiry-day spot** intrinsic
  (``pnl_known=false``); never use entry spot — unknown if no expiry print.
  Expiry spot is heartbeated to the ledger so a crash still has a settle print.

Ledger: ``paper_short_iron_condor.jsonl``.
On by default; disable with ``ATLAS_LITE_PAPER_SHORT_IC=0``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from atlas_lite.instruments import NIFTY_STRIKE_STEP
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger, ist_now
from atlas_lite.metrics import quote_ask, quote_bid, quote_ltp
from atlas_lite.minute_bars import hm_ge, hm_le

STRATEGY = "short_iron_condor"
CAPITAL = 200_000.0
DEFAULT_LOT_SIZE = 65
# Sensibull sample: qty 390 → 6 lots.
LOTS = 6
MAX_ENTRIES_PER_DAY = 1
MAX_REENTRIES_PER_SIDE = 2
ENTRY_AFTER = (9, 20)
ENTRY_UNTIL = (14, 30)
# Seal flat-day PnL after cash close; open books hold past this to expiry.
SESSION_EOD = (15, 20)
# Flatten remaining legs on the option expiry day (weekly settle).
EXPIRY_FLAT = (15, 20)
# If quotes still missing after this on expiry day, intrinsic / force-clear.
FORCE_EXPIRY_FLAT = (15, 25)
# Same cutoff as new entries — no late re-entries into settle.
EXPIRY_REENTRY_UNTIL = (12, 0)
# Skip stop checks through the open auction (wide asks false-trigger 4×).
STOP_AFTER = (9, 20)
# Mid/LTP stop must stay breached this long before a fill @ ask/bid.
STOP_CONFIRM_S = 3.0
CREDIT_MIN = 4.0
CREDIT_MAX = 5.0
STOP_MULT = 4.0
TARGET_PCT = 0.01  # 1% of fixed CAPITAL
REENTRY_TARGET_PCT = 0.005  # 0.5% of fixed CAPITAL
# Persist expiry-day spot so a crash still settles from a real expiry print.
EXPIRY_SPOT_LOG_MIN = 5.0
EXPIRY_SPOT_LOG_PTS = 25.0  # coarser heartbeat — fewer ledger rows on expiry day
# Sensibull screenshot wings (300/400 preferred); keep narrower as fallback fits.
WING_CHOICES = (100, 150, 200, 250, 300, 350, 400)
# Short strike offsets from ATM (pts).
SHORT_OFFSETS = tuple(range(150, 851, 50))
# Light LTP slip when bid/ask missing (far OTM 4–5 credit books).
SLIP_PCT = 0.02
SLIP_MIN = 0.05
PX_FLOOR = 0.05

OptionSymbolFn = Callable[[int, str], str]
Side = Literal["ce", "pe"]


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def paper_short_ic_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_SHORT_IC", "1").strip().lower()
    return raw in ("1", "true", "yes")


def in_short_ic_entry_window(now: datetime) -> bool:
    return hm_ge(now, ENTRY_AFTER) and hm_le(now, ENTRY_UNTIL)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _weekday(now: datetime) -> bool:
    return now.weekday() < 5


def _parse_expiry(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def set_credit(short_px: float, long_px: float) -> float:
    return round(float(short_px) - float(long_px), 2)


def credit_in_band(credit: float, *, lo: float = CREDIT_MIN, hi: float = CREDIT_MAX) -> bool:
    return lo - 1e-9 <= float(credit) <= hi + 1e-9


@dataclass
class VerticalFit:
    side: Side
    short_strike: int
    long_strike: int
    short_symbol: str
    long_symbol: str
    short_px: float
    long_px: float
    credit: float


@dataclass
class SetState:
    side: Side
    short_strike: int
    long_strike: int
    short_symbol: str
    long_symbol: str
    short_entry: float
    long_entry: float
    credit: float
    open: bool = True
    is_reentry: bool = False
    reentries: int = 0
    target_rupees: float = 0.0
    # True only after a hard set stop — profit-takes must not re-enter.
    awaiting_reentry: bool = False
    # ISO ts when mid/LTP first breached 4× — cleared when mark recovers.
    stop_breach_since: str = ""


@dataclass
class ShortIcPosition:
    day: str
    atm: int
    spot: float
    qty: int
    lots: int
    ce: SetState
    pe: SetState
    opened_at: str
    charges_open: float
    realized_pnl: float = 0.0
    expiry: str = ""  # ISO date of the option week held to settle
    # Last spot seen on expiry day — used for intrinsic settle (never entry spot).
    expiry_spot: float = 0.0
    expiry_spot_ts: str = ""
    expiry_spot_logged_ts: str = ""


def slip_pts(premium: float) -> float:
    return max(SLIP_MIN, SLIP_PCT * max(float(premium), 0.0))


def fill_sell(premium: float, *, floor: float = PX_FLOOR) -> float:
    """Worse fill when selling (open short / close long wing)."""
    return round(max(float(floor), float(premium) - slip_pts(premium)), 2)


def fill_buy(premium: float) -> float:
    """Worse fill when buying (open wing / buy back short)."""
    return round(float(premium) + slip_pts(premium), 2)


def _row(book: QuoteSource | None, symbol: str) -> dict[str, Any] | None:
    if book is None or not symbol:
        return None
    row = book.get(symbol)
    return row if isinstance(row, dict) else None


def _sell_px(book: QuoteSource | None, symbol: str) -> float | None:
    """Price we receive when selling — prefer bid, else slipped LTP."""
    row = _row(book, symbol)
    bid = quote_bid(row)
    if bid is not None and bid > 0:
        return round(float(bid), 2)
    ltp = quote_ltp(row)
    if ltp is None or ltp < 0:
        return None
    return fill_sell(ltp)


def _buy_px(book: QuoteSource | None, symbol: str) -> float | None:
    """Price we pay when buying — prefer ask, else slipped LTP."""
    row = _row(book, symbol)
    ask = quote_ask(row)
    if ask is not None and ask > 0:
        return round(float(ask), 2)
    ltp = quote_ltp(row)
    if ltp is None or ltp < 0:
        return None
    return fill_buy(ltp)


def _px(book: QuoteSource | None, symbol: str) -> float | None:
    """Legacy LTP helper (tests / display). Prefer ``_sell_px`` / ``_buy_px`` for fills."""
    return quote_ltp(_row(book, symbol))


def _fair_px(book: QuoteSource | None, symbol: str) -> float | None:
    """Mid (bid/ask) or LTP for stop / soft MTM — ignores one-sided auction spikes."""
    row = _row(book, symbol)
    bid = quote_bid(row)
    ask = quote_ask(row)
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return round(0.5 * (float(bid) + float(ask)), 2)
    return quote_ltp(row)


def _parse_ts(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def intrinsic_leg(side: Side, strike: int, spot: float) -> float:
    spot = float(spot)
    k = int(strike)
    if side == "ce":
        return round(max(0.0, spot - k), 2)
    return round(max(0.0, k - spot), 2)


def intrinsic_set_mark(st: SetState, spot: float) -> tuple[float, float, float]:
    """Expiry settlement marks from spot (short / long / close debit)."""
    s = intrinsic_leg(st.side, st.short_strike, spot)
    l = intrinsic_leg(st.side, st.long_strike, spot)
    return s, l, set_credit(s, l)


def fit_credit_vertical(
    book: QuoteSource,
    option_symbol: OptionSymbolFn,
    *,
    side: Side,
    atm: int,
    step: int = NIFTY_STRIKE_STEP,
    credit_min: float = CREDIT_MIN,
    credit_max: float = CREDIT_MAX,
    short_offsets: tuple[int, ...] = SHORT_OFFSETS,
    wing_choices: tuple[int, ...] = WING_CHOICES,
) -> VerticalFit | None:
    """Best OTM credit vertical with sell@bid − buy@ask in [credit_min, credit_max]."""
    atm = int(atm)
    step = max(int(step), 1)
    best: VerticalFit | None = None
    best_score = 1e9
    mid = 0.5 * (credit_min + credit_max)
    for off in short_offsets:
        if side == "ce":
            short_k = atm + int(off)
        else:
            short_k = atm - int(off)
        if short_k <= 0:
            continue
        for wing in wing_choices:
            if side == "ce":
                long_k = short_k + int(wing)
                short_sym = option_symbol(short_k, "CE")
                long_sym = option_symbol(long_k, "CE")
            else:
                long_k = short_k - int(wing)
                if long_k <= 0:
                    continue
                short_sym = option_symbol(short_k, "PE")
                long_sym = option_symbol(long_k, "PE")
            # Open: sell short @ bid, buy wing @ ask.
            s_px = _sell_px(book, short_sym)
            l_px = _buy_px(book, long_sym)
            if s_px is None or l_px is None or s_px <= 0 or l_px < 0:
                continue
            credit = set_credit(s_px, l_px)
            if not credit_in_band(credit, lo=credit_min, hi=credit_max):
                continue
            # Prefer mid-band credit, Sensibull-wide wings, then farther OTM.
            score = abs(credit - mid) * 1000 - float(wing) - float(off) * 0.01
            if score < best_score:
                best_score = score
                best = VerticalFit(
                    side=side,
                    short_strike=short_k,
                    long_strike=long_k,
                    short_symbol=short_sym,
                    long_symbol=long_sym,
                    short_px=float(s_px),
                    long_px=float(l_px),
                    credit=credit,
                )
    return best


def fit_short_iron_condor(
    book: QuoteSource,
    option_symbol: OptionSymbolFn,
    *,
    atm: int,
    step: int = NIFTY_STRIKE_STEP,
    credit_min: float = CREDIT_MIN,
    credit_max: float = CREDIT_MAX,
) -> tuple[VerticalFit, VerticalFit] | None:
    ce = fit_credit_vertical(
        book,
        option_symbol,
        side="ce",
        atm=atm,
        step=step,
        credit_min=credit_min,
        credit_max=credit_max,
    )
    pe = fit_credit_vertical(
        book,
        option_symbol,
        side="pe",
        atm=atm,
        step=step,
        credit_min=credit_min,
        credit_max=credit_max,
    )
    if ce is None or pe is None:
        return None
    return ce, pe


def _open_set_legs(st: SetState, qty: int) -> list[tuple[float, int, str]]:
    return [(st.short_entry, qty, "sell"), (st.long_entry, qty, "buy")]


def _close_set_legs(short_px: float, long_px: float, qty: int) -> list[tuple[float, int, str]]:
    return [(float(short_px), qty, "buy"), (float(long_px), qty, "sell")]


def set_from_fit(
    fit: VerticalFit,
    *,
    is_reentry: bool = False,
    reentries: int = 0,
    target_rupees: float = 0.0,
) -> SetState:
    return SetState(
        side=fit.side,
        short_strike=fit.short_strike,
        long_strike=fit.long_strike,
        short_symbol=fit.short_symbol,
        long_symbol=fit.long_symbol,
        short_entry=fit.short_px,
        long_entry=fit.long_px,
        credit=fit.credit,
        open=True,
        is_reentry=is_reentry,
        reentries=reentries,
        target_rupees=float(target_rupees),
        awaiting_reentry=False,
        stop_breach_since="",
    )


@dataclass
class PaperShortIronCondor:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = LOTS
    capital: float = CAPITAL
    credit_min: float = CREDIT_MIN
    credit_max: float = CREDIT_MAX
    stop_mult: float = STOP_MULT
    target_pct: float = TARGET_PCT
    reentry_target_pct: float = REENTRY_TARGET_PCT
    max_entries_per_day: int = MAX_ENTRIES_PER_DAY
    max_reentries_per_side: int = MAX_REENTRIES_PER_SIDE
    position: ShortIcPosition | None = None
    traded_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    eod_written: bool = False
    last_event: dict[str, Any] | None = None
    last_reject: str = ""
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_short_ic"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restore()

    def _restore(self) -> None:
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        last_open: dict[str, Any] | None = None
        day = ""
        entries = 0
        day_pnl = 0.0
        eod = False
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("strategy") not in (None, STRATEGY, "short_ic"):
                continue
            d = str(ev.get("day") or "")
            if d and d != day:
                day = d
                entries = 0
                day_pnl = 0.0
                eod = False
                # Keep last_open — this book holds overnight to expiry.
            kind = ev.get("event")
            if kind == "open":
                last_open = ev
                entries += 1
            elif kind == "reentry":
                last_open = ev
            elif kind == "expiry_spot":
                if last_open is not None:
                    last_open = dict(last_open)
                    spot_v = _f(ev.get("expiry_spot"))
                    if spot_v is None:
                        spot_v = _f(ev.get("spot"))
                    if spot_v is not None and spot_v > 0:
                        last_open["expiry_spot"] = spot_v
                        last_open["expiry_spot_ts"] = str(
                            ev.get("expiry_spot_ts") or ev.get("ts") or ""
                        )
                        last_open["expiry_spot_logged_ts"] = str(ev.get("ts") or "")
            elif kind == "close_set":
                # Book PnL only here — the flatten ``close`` event repeats realized total.
                pnl = _f(ev.get("pnl"))
                if pnl is not None:
                    day_pnl += pnl
                if last_open is not None:
                    side = str(ev.get("side") or "")
                    last_open = dict(last_open)
                    if side == "ce":
                        last_open["ce_open"] = False
                        last_open["ce_awaiting_reentry"] = str(ev.get("reason") or "") == "set_stop_4x"
                    elif side == "pe":
                        last_open["pe_open"] = False
                        last_open["pe_awaiting_reentry"] = str(ev.get("reason") or "") == "set_stop_4x"
                    last_open["realized_pnl"] = float(last_open.get("realized_pnl") or 0) + (
                        pnl or 0
                    )
                    last_open["charges_open"] = float(
                        ev.get("charges_open")
                        if ev.get("charges_open") is not None
                        else last_open.get("charges_open") or 0
                    )
                    # Merge set fields from the close_set body (ce_*/pe_* snapshot).
                    for key, val in ev.items():
                        if key.startswith(("ce_", "pe_")):
                            last_open[key] = val
                    if not last_open.get("ce_open", True) and not last_open.get("pe_open", True):
                        last_open = None
            elif kind == "close":
                last_open = None
            elif kind == "day_pnl":
                eod = True
                # Overnight: day_pnl seal must not wipe a still-open book.
                if last_open is None or (
                    not last_open.get("ce_open", True) and not last_open.get("pe_open", True)
                ):
                    last_open = None
        self.traded_day = day
        self.entries_today = entries
        self.day_pnl = round(day_pnl, 2)
        self.eod_written = eod
        if last_open:
            self.position = self._pos_from_event(last_open)

    def _pos_from_event(self, ev: dict[str, Any]) -> ShortIcPosition | None:
        try:
            ce = SetState(
                side="ce",
                short_strike=int(ev["ce_short_strike"]),
                long_strike=int(ev["ce_long_strike"]),
                short_symbol=str(ev["ce_short_symbol"]),
                long_symbol=str(ev["ce_long_symbol"]),
                short_entry=float(ev["ce_short_entry"]),
                long_entry=float(ev["ce_long_entry"]),
                credit=float(ev["ce_credit"]),
                open=bool(ev.get("ce_open", True)),
                is_reentry=bool(ev.get("ce_is_reentry", False)),
                reentries=int(ev.get("ce_reentries") or 0),
                target_rupees=float(ev.get("ce_target_rupees") or 0),
                awaiting_reentry=bool(ev.get("ce_awaiting_reentry", False)),
                stop_breach_since=str(ev.get("ce_stop_breach_since") or ""),
            )
            pe = SetState(
                side="pe",
                short_strike=int(ev["pe_short_strike"]),
                long_strike=int(ev["pe_long_strike"]),
                short_symbol=str(ev["pe_short_symbol"]),
                long_symbol=str(ev["pe_long_symbol"]),
                short_entry=float(ev["pe_short_entry"]),
                long_entry=float(ev["pe_long_entry"]),
                credit=float(ev["pe_credit"]),
                open=bool(ev.get("pe_open", True)),
                is_reentry=bool(ev.get("pe_is_reentry", False)),
                reentries=int(ev.get("pe_reentries") or 0),
                target_rupees=float(ev.get("pe_target_rupees") or 0),
                awaiting_reentry=bool(ev.get("pe_awaiting_reentry", False)),
                stop_breach_since=str(ev.get("pe_stop_breach_since") or ""),
            )
            # Prefer remaining open-charge alloc after a one-sided stop (not full `charges`).
            if ev.get("charges_open") is not None:
                ch_open = float(ev["charges_open"])
            else:
                ch_open = float(ev.get("charges") or 0)
            return ShortIcPosition(
                day=str(ev["day"]),
                atm=int(ev["atm"]),
                spot=float(ev.get("spot") or 0),
                qty=int(ev.get("qty") or self.lots * self.lot_size),
                lots=int(ev.get("lots") or self.lots),
                ce=ce,
                pe=pe,
                # Prefer original open time — reentry rows carry a later ``ts``.
                opened_at=str(ev.get("opened_at") or ev.get("ts") or ""),
                charges_open=ch_open,
                realized_pnl=float(ev.get("realized_pnl") or 0),
                expiry=str(ev.get("expiry") or ""),
                expiry_spot=float(ev.get("expiry_spot") or 0),
                expiry_spot_ts=str(ev.get("expiry_spot_ts") or ""),
                expiry_spot_logged_ts=str(ev.get("expiry_spot_logged_ts") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event = dict(event)
        event.setdefault("mode", "paper")
        event.setdefault("strategy", STRATEGY)
        event.setdefault("logged_at", ist_now())
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError as exc:
            self._log.warning("paper short-IC ledger write failed: %s", exc)
            return None
        self.last_event = event
        return event

    def _target_rupees(self) -> float:
        return round(float(self.capital) * float(self.target_pct), 2)

    def _reentry_target_rupees(self) -> float:
        return round(float(self.capital) * float(self.reentry_target_pct), 2)

    def _set_mark(self, st: SetState, book: QuoteSource | None) -> tuple[float | None, float | None, float | None]:
        # Fill mark: buy back short @ ask, sell wing @ bid.
        s = _buy_px(book, st.short_symbol)
        l = _sell_px(book, st.long_symbol)
        if s is None or l is None:
            return None, None, None
        return float(s), float(l), set_credit(s, l)

    def _stop_mark(self, st: SetState, book: QuoteSource | None) -> tuple[float | None, float | None, float | None]:
        """Soft mark for 4× stop — mid/LTP so 09:15 ask spikes cannot trip alone."""
        s = _fair_px(book, st.short_symbol)
        l = _fair_px(book, st.long_symbol)
        if s is None or l is None:
            return None, None, None
        return float(s), float(l), set_credit(s, l)

    def _soft_mtm(self, st: SetState, qty: int, book: QuoteSource | None) -> float | None:
        """Display / soft mark (mid/LTP) — not used to fire take-profit."""
        s, l, cur = self._stop_mark(st, book)
        if s is None or l is None or cur is None:
            return None
        return round((st.credit - cur) * qty, 2)

    def _fill_mtm(self, st: SetState, qty: int, book: QuoteSource | None) -> float | None:
        """Executable MTM (buy@ask / sell@bid) — gates 1% / 0.5% targets."""
        s, l, cur = self._set_mark(st, book)
        if s is None or l is None or cur is None:
            return None
        return round((st.credit - cur) * qty, 2)

    def _set_mtm(self, st: SetState, qty: int, book: QuoteSource | None) -> float | None:
        # Back-compat alias: snapshot / callers that want live executable mark.
        return self._fill_mtm(st, qty, book)

    def _stop_confirmed(self, st: SetState, now: datetime, cur: float) -> bool:
        stop_level = st.credit * self.stop_mult
        if cur + 1e-9 < stop_level:
            st.stop_breach_since = ""
            return False
        if not st.stop_breach_since:
            st.stop_breach_since = now.isoformat(timespec="seconds")
            return False
        since = _parse_ts(st.stop_breach_since)
        if since is None:
            st.stop_breach_since = now.isoformat(timespec="seconds")
            return False
        held = (now - since).total_seconds()
        return held + 1e-9 >= float(STOP_CONFIRM_S)

    def _settle_spot(self, pos: ShortIcPosition, *, now: datetime, spot_now: float | None) -> float | None:
        """Spot for intrinsic settle: expiry-day print only (never entry ``pos.spot``)."""
        if pos.expiry_spot and float(pos.expiry_spot) > 0:
            return float(pos.expiry_spot)
        exp = _parse_expiry(pos.expiry)
        if exp is not None and now.date() == exp and spot_now is not None and float(spot_now) > 0:
            return float(spot_now)
        return None

    def _note_expiry_spot(self, pos: ShortIcPosition, now: datetime, spot: float | None) -> None:
        exp = _parse_expiry(pos.expiry)
        if exp is None or now.date() != exp:
            return
        if spot is None or float(spot) <= 0:
            return
        prev = float(pos.expiry_spot or 0)
        pos.expiry_spot = float(spot)
        pos.expiry_spot_ts = now.isoformat(timespec="seconds")
        # Heartbeat to ledger so a crash still has an expiry-day print to settle.
        should_log = prev <= 0
        if not should_log and abs(pos.expiry_spot - prev) + 1e-9 >= EXPIRY_SPOT_LOG_PTS:
            should_log = True
        if not should_log:
            logged = _parse_ts(pos.expiry_spot_logged_ts)
            if logged is None:
                should_log = True
            else:
                should_log = (now - logged).total_seconds() >= EXPIRY_SPOT_LOG_MIN * 60.0
        if not should_log:
            return
        ev = self._append(
            {
                "event": "expiry_spot",
                "ts": now.isoformat(timespec="seconds"),
                "day": now.strftime("%Y-%m-%d"),
                "expiry": pos.expiry,
                "expiry_spot": pos.expiry_spot,
                "expiry_spot_ts": pos.expiry_spot_ts,
                "spot": pos.expiry_spot,
                "opened_at": pos.opened_at,
                "atm": pos.atm,
            }
        )
        if ev is not None:
            pos.expiry_spot_logged_ts = str(ev.get("ts") or pos.expiry_spot_ts)

    def _open_event_body(self, pos: ShortIcPosition) -> dict[str, Any]:
        return {
            "day": pos.day,
            "atm": pos.atm,
            "spot": pos.spot,
            "expiry": pos.expiry,
            "expiry_spot": pos.expiry_spot,
            "expiry_spot_ts": pos.expiry_spot_ts,
            "expiry_spot_logged_ts": pos.expiry_spot_logged_ts,
            "opened_at": pos.opened_at,
            "qty": pos.qty,
            "lots": pos.lots,
            "ce_short_strike": pos.ce.short_strike,
            "ce_long_strike": pos.ce.long_strike,
            "ce_short_symbol": pos.ce.short_symbol,
            "ce_long_symbol": pos.ce.long_symbol,
            "ce_short_entry": pos.ce.short_entry,
            "ce_long_entry": pos.ce.long_entry,
            "ce_credit": pos.ce.credit,
            "ce_open": pos.ce.open,
            "ce_is_reentry": pos.ce.is_reentry,
            "ce_reentries": pos.ce.reentries,
            "ce_target_rupees": pos.ce.target_rupees,
            "ce_awaiting_reentry": pos.ce.awaiting_reentry,
            "ce_stop_breach_since": pos.ce.stop_breach_since,
            "pe_short_strike": pos.pe.short_strike,
            "pe_long_strike": pos.pe.long_strike,
            "pe_short_symbol": pos.pe.short_symbol,
            "pe_long_symbol": pos.pe.long_symbol,
            "pe_short_entry": pos.pe.short_entry,
            "pe_long_entry": pos.pe.long_entry,
            "pe_credit": pos.pe.credit,
            "pe_open": pos.pe.open,
            "pe_is_reentry": pos.pe.is_reentry,
            "pe_reentries": pos.pe.reentries,
            "pe_target_rupees": pos.pe.target_rupees,
            "pe_awaiting_reentry": pos.pe.awaiting_reentry,
            "pe_stop_breach_since": pos.pe.stop_breach_since,
            "credit": round(
                (pos.ce.credit if pos.ce.open else 0.0)
                + (pos.pe.credit if pos.pe.open else 0.0),
                2,
            ),
            "realized_pnl": pos.realized_pnl,
            "charges": pos.charges_open,
            "charges_open": pos.charges_open,
            "target": self._target_rupees(),
            "target_pct_of": "capital",
            "hold": "expiry",
        }

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        open_pnl = 0.0
        charges = 0.0
        pos_body = None
        if self.position is not None:
            pos = self.position
            pos_body = self._open_event_body(pos)
            pos_body["opened_at"] = pos.opened_at
            charges = float(pos.charges_open)
            for st in (pos.ce, pos.pe):
                if not st.open:
                    continue
                mtm = self._set_mtm(st, pos.qty, book)
                if mtm is not None:
                    open_pnl += mtm
                s, l, _ = self._set_mark(st, book)
                if s is not None and l is not None:
                    charges += kite_nfo_charges(_close_set_legs(s, l, pos.qty))["total"]
            open_pnl = round(open_pnl - (charges - pos.charges_open), 2)
            charges = round(charges, 2)
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
            "charges": charges,
            "mtm_pnl": mtm,
            "equity": round(self.capital + mtm, 2),
            "position": pos_body,
            "eod": self.eod_written,
            "hold": "expiry",
            "expiry_flat": f"{EXPIRY_FLAT[0]:02d}:{EXPIRY_FLAT[1]:02d}",
            "force_expiry_flat": f"{FORCE_EXPIRY_FLAT[0]:02d}:{FORCE_EXPIRY_FLAT[1]:02d}",
            "session_eod": f"{SESSION_EOD[0]:02d}:{SESSION_EOD[1]:02d}",
            "entry_window": (
                f"{ENTRY_AFTER[0]:02d}:{ENTRY_AFTER[1]:02d}"
                f"-{ENTRY_UNTIL[0]:02d}:{ENTRY_UNTIL[1]:02d}"
            ),
            "wing_choices": list(WING_CHOICES),
            "credit_band": [self.credit_min, self.credit_max],
            "stop_mult": self.stop_mult,
            "target_pct": self.target_pct,
            "target_pct_of": "capital",
            "reentry_target_pct": self.reentry_target_pct,
            "max_entries_per_day": int(self.max_entries_per_day),
            "last_reject": self.last_reject,
            "last_event": self.last_event,
        }

    def _roll_to_day(self, day: str) -> None:
        self.traded_day = day
        self.entries_today = 0
        self.day_pnl = 0.0
        self.eod_written = False
        self.last_reject = ""

    def _write_eod_if_needed(
        self, now: datetime, *, allow_open: bool = False
    ) -> dict[str, Any] | None:
        if self.eod_written:
            return None
        if self.position is not None and not allow_open:
            return None
        if not (self.entries_today or self.day_pnl or self.position is not None):
            self.eod_written = True
            return None
        ev = self._append(
            {
                "event": "day_pnl",
                "ts": now.isoformat(timespec="seconds"),
                "day": self.traded_day,
                "day_pnl": round(self.day_pnl, 2),
                "day_pnl_pct": round(self.day_pnl / self.capital * 100.0, 4),
                "trades": self.entries_today,
                "open_overnight": bool(self.position is not None),
            }
        )
        if ev is not None:
            self.eod_written = True
        return ev

    def _close_set(
        self,
        now: datetime,
        pos: ShortIcPosition,
        side: Side,
        book: QuoteSource | None,
        reason: str,
        *,
        force: bool = False,
        spot: float | None = None,
    ) -> dict[str, Any] | None:
        st = pos.ce if side == "ce" else pos.pe
        if not st.open:
            return None
        s, l, cur = self._set_mark(st, book)
        settle = "quotes"
        pnl_known = True
        if s is None or l is None or cur is None:
            if not force:
                self.last_reject = f"no_quote_{side}"
                return None
            if spot is not None and float(spot) > 0:
                s, l, cur = intrinsic_set_mark(st, float(spot))
                settle = "intrinsic"
                # Intrinsic is an estimate — keep the booked figure but flag unknown-quality.
                pnl_known = False
                reason = "expiry_settle" if reason in ("expiry", "expiry_force") else reason
            else:
                return self._force_close_unknown(now, pos, side, reason)
        qty = pos.qty
        gross = round((st.credit - cur) * qty, 2)
        close_ch = kite_nfo_charges(_close_set_legs(s, l, qty))["total"]
        # Capture before flipping ``st.open`` — remaining set keeps its share of open charges.
        both_open = pos.ce.open and pos.pe.open
        open_alloc = round(pos.charges_open * 0.5, 2) if both_open else float(pos.charges_open)
        pnl = round(gross - open_alloc - close_ch, 2)
        st.open = False
        st.awaiting_reentry = reason == "set_stop_4x"
        st.stop_breach_since = ""
        pos.realized_pnl = round(pos.realized_pnl + pnl, 2)
        self.day_pnl = round(self.day_pnl + pnl, 2)
        pos.charges_open = round(max(0.0, float(pos.charges_open) - open_alloc), 2)
        body = self._open_event_body(pos)
        ev = self._append(
            {
                "event": "close_set",
                "ts": now.isoformat(timespec="seconds"),
                "day": now.strftime("%Y-%m-%d"),
                "side": side,
                "reason": reason,
                "settle": settle,
                "settle_spot": float(spot) if settle == "intrinsic" and spot is not None else None,
                "short_strike": st.short_strike,
                "long_strike": st.long_strike,
                "short_symbol": st.short_symbol,
                "long_symbol": st.long_symbol,
                "credit_entry": st.credit,
                "credit_exit": cur,
                "short_exit": s,
                "long_exit": l,
                "qty": qty,
                "pnl": pnl,
                "gross": gross,
                "pnl_known": pnl_known,
                "charges": round(open_alloc + close_ch, 2),
                "charges_open": pos.charges_open,
                "stop_level": round(st.credit * self.stop_mult, 2),
                **{
                    k: v
                    for k, v in body.items()
                    if k.startswith(
                        (
                            "ce_",
                            "pe_",
                            "atm",
                            "spot",
                            "qty",
                            "lots",
                            "credit",
                            "realized",
                            "opened",
                            "expiry",
                        )
                    )
                },
            }
        )
        if not pos.ce.open and not pos.pe.open:
            self.position = None
        return ev

    def _force_close_unknown(
        self,
        now: datetime,
        pos: ShortIcPosition,
        side: Side,
        reason: str,
    ) -> dict[str, Any] | None:
        """Clear a side with no quotes and no spot — book PnL unknown for that set."""
        st = pos.ce if side == "ce" else pos.pe
        if not st.open:
            return None
        both_open = pos.ce.open and pos.pe.open
        open_alloc = round(pos.charges_open * 0.5, 2) if both_open else float(pos.charges_open)
        # Charge the open alloc against day PnL; premium PnL unknown.
        self.day_pnl = round(self.day_pnl - open_alloc, 2)
        pos.charges_open = round(max(0.0, float(pos.charges_open) - open_alloc), 2)
        st.open = False
        st.awaiting_reentry = False
        body = self._open_event_body(pos)
        ev = self._append(
            {
                "event": "close_set",
                "ts": now.isoformat(timespec="seconds"),
                "day": now.strftime("%Y-%m-%d"),
                "side": side,
                "reason": reason,
                "settle": "unknown",
                "short_strike": st.short_strike,
                "long_strike": st.long_strike,
                "short_symbol": st.short_symbol,
                "long_symbol": st.long_symbol,
                "credit_entry": st.credit,
                "credit_exit": None,
                "qty": pos.qty,
                "pnl": None,
                "gross": None,
                "pnl_known": False,
                "charges": open_alloc,
                "charges_open": pos.charges_open,
                **{
                    k: v
                    for k, v in body.items()
                    if k.startswith(
                        ("ce_", "pe_", "atm", "spot", "qty", "lots", "credit", "realized", "opened")
                    )
                },
            }
        )
        if not pos.ce.open and not pos.pe.open:
            self.position = None
        return ev

    def _close_all(
        self,
        now: datetime,
        book: QuoteSource | None,
        reason: str,
        *,
        force: bool = False,
        spot: float | None = None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        last = None
        pnl_known = True
        for side in ("ce", "pe"):
            st = pos.ce if side == "ce" else pos.pe
            if not st.open:
                continue
            ev = self._close_set(
                now, pos, side, book, reason, force=force, spot=spot
            )
            if ev is None and force:
                ev = self._force_close_unknown(now, pos, side, reason)
            if ev is not None:
                last = ev
                if ev.get("pnl_known") is False:
                    pnl_known = False
        if self.position is not None and not (self.position.ce.open or self.position.pe.open):
            self.position = None
        if last is not None and self.position is None:
            # Flatten marker for trades API — PnL already booked on each close_set.
            self._append(
                {
                    "event": "close",
                    "ts": now.isoformat(timespec="seconds"),
                    "day": now.strftime("%Y-%m-%d"),
                    "reason": reason,
                    "pnl_known": pnl_known,
                    "realized_pnl": round(pos.realized_pnl, 2) if pnl_known else None,
                    "atm": pos.atm,
                    "qty": pos.qty,
                    "expiry": pos.expiry,
                    "opened_at": pos.opened_at,
                }
            )
        return last

    def _reentry_allowed(self, now: datetime, pos: ShortIcPosition) -> bool:
        exp = _parse_expiry(pos.expiry)
        if exp is not None and now.date() == exp and hm_ge(now, EXPIRY_REENTRY_UNTIL):
            return False
        return True

    def _try_reentry(
        self,
        now: datetime,
        pos: ShortIcPosition,
        side: Side,
        book: QuoteSource,
        option_symbol: OptionSymbolFn,
        *,
        atm: int | None = None,
    ) -> dict[str, Any] | None:
        st = pos.ce if side == "ce" else pos.pe
        if st.open or not st.awaiting_reentry or st.reentries >= self.max_reentries_per_side:
            return None
        if not self._reentry_allowed(now, pos):
            st.awaiting_reentry = False
            self.last_reject = f"reentry_expiry_cutoff_{side}"
            return None
        use_atm = int(atm) if atm is not None else int(pos.atm)
        fit = fit_credit_vertical(
            book,
            option_symbol,
            side=side,
            atm=use_atm,
            credit_min=self.credit_min,
            credit_max=self.credit_max,
        )
        if fit is None:
            self.last_reject = f"reentry_no_fit_{side}"
            return None
        # "0.5% of capital" gate: new set credit×qty must clear 0.5% of CAPITAL.
        max_credit_inr = round(fit.credit * pos.qty, 2)
        need = self._reentry_target_rupees()
        if max_credit_inr + 1e-9 < need:
            self.last_reject = f"reentry_lt_0.5pct_{side}"
            return None
        # Take profit on the re-entered set at min(0.5% capital, full credit).
        set_tp = min(need, max_credit_inr)
        new = set_from_fit(
            fit,
            is_reentry=True,
            reentries=st.reentries + 1,
            target_rupees=set_tp,
        )
        open_ch = kite_nfo_charges(_open_set_legs(new, pos.qty))["total"]
        if side == "ce":
            pos.ce = new
        else:
            pos.pe = new
        pos.atm = use_atm
        pos.charges_open = round(pos.charges_open + open_ch, 2)
        body = self._open_event_body(pos)
        # Side credit first — body.credit is open-set sum and must not clobber.
        return self._append(
            {
                **body,
                "event": "reentry",
                "ts": now.isoformat(timespec="seconds"),
                "day": now.strftime("%Y-%m-%d"),
                "side": side,
                "reason": "reentry_after_stop",
                "opened_at": pos.opened_at,
                "credit": fit.credit,
                "short_strike": fit.short_strike,
                "long_strike": fit.long_strike,
                "short_symbol": fit.short_symbol,
                "long_symbol": fit.long_symbol,
                "short_entry": fit.short_px,
                "long_entry": fit.long_px,
                "qty": pos.qty,
                "charges": open_ch,
                "charges_open": pos.charges_open,
                "target": new.target_rupees,
            }
        )

    def on_frame(
        self,
        *,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource,
        atm: int | None,
        option_symbol: OptionSymbolFn | None,
        allow_entry: bool = True,
        block_reason: str | None = None,
    ) -> dict[str, Any] | None:
        if not _weekday(now) and self.position is None:
            return None
        day = now.strftime("%Y-%m-%d")
        if self.traded_day and self.traded_day != day:
            # Seal prior day (even with overnight open); keep position to expiry.
            self._write_eod_if_needed(now, allow_open=self.position is not None)
            self._roll_to_day(day)
        elif not self.traded_day:
            self._roll_to_day(day)

        # Manage open book (overnight carry until expiry / 1% capital TP / set stops).
        if self.position is not None:
            pos = self.position
            exp = _parse_expiry(pos.expiry) or _parse_expiry(feed.get("expiry"))
            if exp is not None and not pos.expiry:
                pos.expiry = exp.isoformat()
            spot_now = _f(feed.get("spot")) or _f(feed.get("nifty_ltp"))
            self._note_expiry_spot(pos, now, spot_now)
            past_expiry = exp is not None and (
                now.date() > exp or (now.date() == exp and hm_ge(now, EXPIRY_FLAT))
            )
            if past_expiry:
                # Quotes first; force uses expiry-day spot only (never entry spot).
                must_force = now.date() > exp or hm_ge(now, FORCE_EXPIRY_FLAT)
                settle_spot = self._settle_spot(pos, now=now, spot_now=spot_now)
                last = self._close_all(
                    now,
                    book,
                    "expiry",
                    force=must_force,
                    spot=settle_spot,
                )
                if self.position is not None and must_force:
                    return self._close_all(
                        now,
                        book,
                        "expiry_force",
                        force=True,
                        spot=self._settle_spot(pos, now=now, spot_now=spot_now),
                    )
                return last

            # Per-set stop / re-entry TP / try re-entry after stop only
            for side in ("ce", "pe"):
                st = pos.ce if side == "ce" else pos.pe
                if st.open:
                    # Soft mid/LTP stop — fills still go through ask/bid in ``_close_set``.
                    if hm_ge(now, STOP_AFTER):
                        _s, _l, soft = self._stop_mark(st, book)
                        if soft is not None and self._stop_confirmed(st, now, soft):
                            ev = self._close_set(now, pos, side, book, "set_stop_4x")
                            if ev is None:
                                # Wide TOB may still refuse the fill; keep breach clock.
                                continue
                            if self.position is not None and not self._reentry_allowed(
                                now, self.position
                            ):
                                closed = self.position.ce if side == "ce" else self.position.pe
                                closed.awaiting_reentry = False
                                self.last_reject = f"reentry_expiry_cutoff_{side}"
                                return ev
                            if option_symbol is not None and self.position is not None:
                                re_ev = self._try_reentry(
                                    now,
                                    self.position,
                                    side,
                                    book,
                                    option_symbol,
                                    atm=atm,
                                )
                                return re_ev or ev
                            return ev
                    # Take-profit on executable ask/bid MTM (not mid — avoids ₹150–200 shortfall).
                    mtm = self._fill_mtm(st, pos.qty, book)
                    tgt = st.target_rupees or (
                        self._reentry_target_rupees() if st.is_reentry else self._target_rupees()
                    )
                    # Per-set TP only for re-entries (0.5% capital); book uses combined 1%.
                    if st.is_reentry and mtm is not None and mtm >= tgt:
                        return self._close_set(now, pos, side, book, "reentry_target")
                elif st.awaiting_reentry and option_symbol is not None:
                    if not self._reentry_allowed(now, pos):
                        st.awaiting_reentry = False
                        continue
                    re_ev = self._try_reentry(
                        now, pos, side, book, option_symbol, atm=atm
                    )
                    if re_ev is not None:
                        return re_ev

            # Combined 1% of fixed capital on fillable marks + realized in this position
            open_mtm = 0.0
            known = True
            for st in (pos.ce, pos.pe):
                if not st.open:
                    continue
                m = self._fill_mtm(st, pos.qty, book)
                if m is None:
                    known = False
                    break
                open_mtm += m
            if known:
                total = pos.realized_pnl + open_mtm
                if total >= self._target_rupees():
                    return self._close_all(now, book, "target_1pct")
            return None

        # Flat — seal day after session eod
        if hm_ge(now, SESSION_EOD):
            return self._write_eod_if_needed(now)

        if not allow_entry:
            self.last_reject = block_reason or "entry_blocked"
            return None
        if not in_short_ic_entry_window(now):
            return None
        if self.entries_today >= self.max_entries_per_day:
            self.last_reject = "max_entries"
            return None
        if atm is None or option_symbol is None:
            self.last_reject = "no_atm"
            return None
        expiry = _parse_expiry(feed.get("expiry"))
        if expiry is None:
            self.last_reject = "no_expiry"
            return None
        # Do not open on expiry afternoon — not enough time to hold the thesis.
        if now.date() == expiry and hm_ge(now, (12, 0)):
            self.last_reject = "expiry_day_late"
            return None

        fitted = fit_short_iron_condor(
            book,
            option_symbol,
            atm=int(atm),
            credit_min=self.credit_min,
            credit_max=self.credit_max,
        )
        if fitted is None:
            self.last_reject = "no_credit_band_fit"
            return None
        ce_fit, pe_fit = fitted
        qty = int(self.lots * self.lot_size)
        ce = set_from_fit(ce_fit, target_rupees=self._target_rupees())
        pe = set_from_fit(pe_fit, target_rupees=self._target_rupees())
        legs = _open_set_legs(ce, qty) + _open_set_legs(pe, qty)
        charges = kite_nfo_charges(legs)["total"]
        spot = _f(feed.get("spot")) or _f(feed.get("nifty_ltp")) or 0.0
        pos = ShortIcPosition(
            day=day,
            atm=int(atm),
            spot=float(spot),
            qty=qty,
            lots=int(self.lots),
            ce=ce,
            pe=pe,
            opened_at=now.isoformat(timespec="seconds"),
            charges_open=float(charges),
            expiry=expiry.isoformat(),
        )
        self.position = pos
        self.entries_today += 1
        self.last_reject = ""
        return self._append(
            {
                "event": "open",
                "ts": pos.opened_at,
                **self._open_event_body(pos),
            }
        )
