"""Paper Theta-Cliff Fence — expiry-day short iron condor (never Kite orders).

Edge (from research notes): expiry morning often realises less than same-day
option pricing implies. Rules:

* **Expiry day only**, **1 lot**, enter **12:00–12:10 IST**.
* Skip if morning 5m realised vol > ``0.9 × yesterday's India VIX``.
* Short CE above ``max(spot + 0.75σ, morning high)``, round **up** 50.
* Short PE below ``min(spot − 0.75σ, morning low)``, round **down** 50.
* ``σ`` = yesterday VIX scaled to time left until 15:30 (trading-year √t).
* Buy wings **100 pts** beyond each short (defined-risk fence).
* Spot touches a short strike → close **that vertical only**.
* Flatten remaining at **15:15** (force unknown marks at **15:25** if still missing quotes).
* Fills use the same slippage as ``scripts/tcf_bt.py``: 2% of premium, min 0.5 pt/leg.

Ledger: ``paper_theta_cliff.jsonl``.
On by default; disable with ``ATLAS_LITE_PAPER_THETA_CLIFF=0``.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from atlas_lite.instruments import NIFTY_STRIKE_STEP
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger, ist_now
from atlas_lite.metrics import quote_ltp
from atlas_lite.minute_bars import hm_ge, hm_le
from atlas_lite.paper_vwap_long import aggregate_bars

STRATEGY = "theta_cliff_fence"
CAPITAL = 200_000.0
LOTS = 1
DEFAULT_LOT_SIZE = 65
MAX_ENTRIES_PER_DAY = 1
ENTRY_AFTER = (12, 0)
ENTRY_UNTIL = (12, 10)
# First minute after the entry window — finalize a single skip if still flat.
AFTER_ENTRY = (12, 11)
SQUARE_OFF = (15, 15)
# Retry quote-based flatten from 15:15; only force-mark unknown after this.
FORCE_FLAT = (15, 25)
SESSION_END = (15, 30)
WING_PTS = 100
SIGMA_MULT = 0.75
RV_VIX_MAX = 0.9
# NSE cash session ≈ 375 minutes; 5m bars → 75/day; 252 trading days.
BARS_5M_PER_YEAR = 252.0 * 75.0
MINUTES_PER_TRADING_YEAR = 252.0 * 375.0
VIX_PREV_FILE = "vix_prev.json"
MIN_CREDIT = 5.0
# Reject persisted VIX if older than this many calendar days.
VIX_PREV_MAX_AGE_DAYS = 7
# Match ``scripts/tcf_bt.slip``.
SLIP_PCT = 0.02
SLIP_MIN = 0.5

OptionSymbolFn = Callable[[int, str], str]
Side = Literal["ce", "pe"]


def slip_pts(premium: float) -> float:
    return max(SLIP_MIN, SLIP_PCT * float(premium))


def fill_sell(premium: float, *, floor: float = 0.05) -> float:
    """Worse fill when selling (open short / close long wing)."""
    return round(max(float(floor), float(premium) - slip_pts(premium)), 2)


def fill_buy(premium: float) -> float:
    """Worse fill when buying (open wing / buy back short)."""
    return round(float(premium) + slip_pts(premium), 2)


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def paper_theta_cliff_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_THETA_CLIFF", "1").strip().lower()
    return raw in ("1", "true", "yes")


def in_theta_cliff_entry_window(now: datetime) -> bool:
    return hm_ge(now, ENTRY_AFTER) and hm_le(now, ENTRY_UNTIL) and not hm_ge(now, SQUARE_OFF)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _weekday(now: datetime) -> bool:
    return now.weekday() < 5


def ceil_strike(price: float, step: int = NIFTY_STRIKE_STEP) -> int:
    step = max(int(step), 1)
    return int(math.ceil(float(price) / step - 1e-9) * step)


def floor_strike(price: float, step: int = NIFTY_STRIKE_STEP) -> int:
    step = max(int(step), 1)
    return int(math.floor(float(price) / step + 1e-9) * step)


def remaining_sigma_pts(
    spot: float,
    vix_pct: float,
    now: datetime,
    *,
    session_end: tuple[int, int] = SESSION_END,
) -> float:
    """Spot move (pts) = spot × (VIX/100) × √(minutes_left / trading-year minutes)."""
    end = now.replace(
        hour=int(session_end[0]),
        minute=int(session_end[1]),
        second=0,
        microsecond=0,
    )
    mins = max((end - now).total_seconds() / 60.0, 1.0)
    t_frac = mins / MINUTES_PER_TRADING_YEAR
    return float(spot) * (float(vix_pct) / 100.0) * math.sqrt(t_frac)


def fence_strikes(
    spot: float,
    morning_high: float,
    morning_low: float,
    sigma_pts: float,
    *,
    wing_pts: int = WING_PTS,
    sigma_mult: float = SIGMA_MULT,
    step: int = NIFTY_STRIKE_STEP,
) -> tuple[int, int, int, int]:
    """Return pe_long, pe_short, ce_short, ce_long.

    Rounding matches ``scripts/tcf_bt.py``: ceil/floor of the fence level
    (not an extra step when the level lands on a strike).
    """
    band = float(sigma_mult) * float(sigma_pts)
    ce_short = ceil_strike(max(float(spot) + band, float(morning_high)), step)
    pe_short = floor_strike(min(float(spot) - band, float(morning_low)), step)
    if pe_short >= ce_short:
        mid = int(round(float(spot) / step) * step)
        pe_short = mid - step
        ce_short = mid + step
    w = max(int(wing_pts), step)
    return pe_short - w, pe_short, ce_short, ce_short + w


def morning_session_stats(
    bars_1m: list[dict[str, Any]],
    *,
    day: str,
    until_hm: str = "12:00",
) -> dict[str, float] | None:
    """Morning high/low + annualised RV% from 5m closes (09:15→until_hm)."""
    session: list[dict[str, Any]] = []
    for b in bars_1m:
        t = str(b.get("t") or "")
        if not t.startswith(day):
            continue
        hm = t[11:16] if len(t) >= 16 else ""
        if hm < "09:15" or hm >= until_hm:
            continue
        session.append(b)
    if len(session) < 10:
        return None
    high = max(float(b["h"]) for b in session)
    low = min(float(b["l"]) for b in session)
    bars_5 = aggregate_bars(session, 5)
    closes = [float(b["c"]) for b in bars_5 if float(b.get("c") or 0) > 0]
    if len(closes) < 3:
        return None
    # Skip the first 5m return (opening bar) — same as ``tcf_bt.run``.
    rets = []
    for i in range(2, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            continue
        rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    rv = math.sqrt(max(var, 0.0)) * math.sqrt(BARS_5M_PER_YEAR) * 100.0
    return {
        "morning_high": round(high, 2),
        "morning_low": round(low, 2),
        "rv_pct": round(rv, 4),
        "bars_5m": float(len(closes)),
    }


def rv_filter_ok(rv_pct: float, vix_yesterday: float, *, max_ratio: float = RV_VIX_MAX) -> bool:
    if vix_yesterday <= 0:
        return False
    return float(rv_pct) <= float(max_ratio) * float(vix_yesterday)


def load_vix_prev(
    path: Path,
    *,
    today: str | None = None,
    max_age_days: int = VIX_PREV_MAX_AGE_DAYS,
) -> float | None:
    """Load prior-session VIX. Rejects today's stamp and stale files."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    vix = _f(raw.get("vix"))
    if vix is None or vix <= 0:
        return None
    stored_day = str(raw.get("day") or "").strip()
    if not stored_day:
        return None
    if today and stored_day >= today:
        return None  # must be a prior calendar day
    if today:
        try:
            age = (date.fromisoformat(today) - date.fromisoformat(stored_day[:10])).days
        except ValueError:
            return None
        if age < 1 or age > int(max_age_days):
            return None
    return vix


def save_vix_prev(path: Path, *, day: str, vix: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"day": day, "vix": round(float(vix), 4)}, indent=0) + "\n",
        encoding="utf-8",
    )


def _open_legs(
    ce_short: float,
    pe_short: float,
    ce_long: float,
    pe_long: float,
    qty: int,
) -> list[tuple[float, int, str]]:
    return [
        (float(ce_short), qty, "sell"),
        (float(pe_short), qty, "sell"),
        (float(ce_long), qty, "buy"),
        (float(pe_long), qty, "buy"),
    ]


def _close_vertical_legs(
    short_px: float,
    long_px: float,
    qty: int,
) -> list[tuple[float, int, str]]:
    return [(float(short_px), qty, "buy"), (float(long_px), qty, "sell")]


@dataclass
class FenceQuotes:
    ce_short: float
    pe_short: float
    ce_long: float
    pe_long: float


@dataclass
class ThetaCliffPosition:
    day: str
    pe_long_strike: int
    pe_short_strike: int
    ce_short_strike: int
    ce_long_strike: int
    pe_long_symbol: str
    pe_short_symbol: str
    ce_short_symbol: str
    ce_long_symbol: str
    qty: int
    lots: int
    pe_long_entry: float
    pe_short_entry: float
    ce_short_entry: float
    ce_long_entry: float
    credit: float
    sigma_pts: float
    morning_high: float
    morning_low: float
    rv_pct: float
    vix_yesterday: float
    opened_at: str
    charges_open: float
    ce_open: bool = True
    pe_open: bool = True
    charges_closed: float = 0.0
    # Sticky stop: remember touch even if quotes were missing that tick.
    ce_stop_pending: bool = False
    pe_stop_pending: bool = False
    # Any force-close with unknown marks → final summary must stay pnl_known=False.
    pnl_unknown: bool = False


@dataclass
class PaperThetaCliff:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = LOTS
    capital: float = CAPITAL
    max_entries_per_day: int = MAX_ENTRIES_PER_DAY
    wing_pts: int = WING_PTS
    position: ThetaCliffPosition | None = None
    traded_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    eod_written: bool = False
    last_event: dict[str, Any] | None = None
    last_reject: str | None = None
    skip_day: str = ""  # one ledger ``skip`` per expiry day
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_theta_cliff"), repr=False)

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
        charges_closed = 0.0
        ce_stop_pending = False
        pe_stop_pending = False
        pnl_unknown = False
        skip_day = ""
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
                charges_closed = 0.0
                ce_stop_pending = False
                pe_stop_pending = False
                pnl_unknown = False
            if kind == "open":
                last_open = ev
                entries += 1
                charges_closed = float(ev.get("charges_closed") or 0.0)
                ce_stop_pending = bool(ev.get("ce_stop_pending") or False)
                pe_stop_pending = bool(ev.get("pe_stop_pending") or False)
                pnl_unknown = bool(ev.get("pnl_unknown") or False)
            elif kind == "skip":
                if d:
                    skip_day = d
            elif kind == "stop_pending":
                side = str(ev.get("side") or "").lower()
                if side == "ce":
                    ce_stop_pending = True
                elif side == "pe":
                    pe_stop_pending = True
            elif kind in ("close", "close_vertical"):
                if kind == "close_vertical" and ev.get("pnl_known") is False:
                    pnl_unknown = True
                if kind == "close":
                    last_open = None
                    ce_stop_pending = False
                    pe_stop_pending = False
                    pnl_unknown = False
                elif last_open is not None:
                    side = str(ev.get("side") or "").lower()
                    if side == "ce":
                        last_open = {**last_open, "ce_open": False}
                        ce_stop_pending = False
                    elif side == "pe":
                        last_open = {**last_open, "pe_open": False}
                        pe_stop_pending = False
                    ch_c = _f(ev.get("charges_close"))
                    if ch_c is not None:
                        charges_closed = round(charges_closed + float(ch_c), 2)
                    if last_open.get("ce_open") is False and last_open.get("pe_open") is False:
                        last_open = None
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
        self.skip_day = skip_day
        if last_open:
            self.position = ThetaCliffPosition(
                day=str(last_open["day"]),
                pe_long_strike=int(last_open["pe_long_strike"]),
                pe_short_strike=int(last_open["pe_short_strike"]),
                ce_short_strike=int(last_open["ce_short_strike"]),
                ce_long_strike=int(last_open["ce_long_strike"]),
                pe_long_symbol=str(last_open["pe_long_symbol"]),
                pe_short_symbol=str(last_open["pe_short_symbol"]),
                ce_short_symbol=str(last_open["ce_short_symbol"]),
                ce_long_symbol=str(last_open["ce_long_symbol"]),
                qty=int(last_open.get("qty") or self.lots * self.lot_size),
                lots=int(last_open.get("lots") or self.lots),
                pe_long_entry=float(last_open["pe_long_entry"]),
                pe_short_entry=float(last_open["pe_short_entry"]),
                ce_short_entry=float(last_open["ce_short_entry"]),
                ce_long_entry=float(last_open["ce_long_entry"]),
                credit=float(last_open.get("credit") or 0),
                sigma_pts=float(last_open.get("sigma_pts") or 0),
                morning_high=float(last_open.get("morning_high") or 0),
                morning_low=float(last_open.get("morning_low") or 0),
                rv_pct=float(last_open.get("rv_pct") or 0),
                vix_yesterday=float(last_open.get("vix_yesterday") or 0),
                opened_at=str(last_open.get("ts") or ""),
                charges_open=float(last_open.get("charges") or 0.0),
                ce_open=bool(last_open.get("ce_open", True)),
                pe_open=bool(last_open.get("pe_open", True)),
                charges_closed=float(charges_closed),
                ce_stop_pending=ce_stop_pending,
                pe_stop_pending=pe_stop_pending,
                pnl_unknown=pnl_unknown,
            )

    def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event = dict(event)
        event.setdefault("mode", "paper")
        event.setdefault("strategy", STRATEGY)
        event.setdefault("book", STRATEGY)
        event.setdefault("logged_at", ist_now())
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError as exc:
            self._log.warning("paper theta-cliff ledger write failed: %s", exc)
            return None
        self.last_event = event
        return event

    def _book_quotes(self, book: QuoteSource | None, symbols: dict[str, str]) -> FenceQuotes | None:
        if book is None:
            return None
        vals = {k: quote_ltp(book.get(sym)) for k, sym in symbols.items()}
        # Shorts must trade; wings may mark 0 (far OTM on expiry).
        for short_key in ("ce_short", "pe_short"):
            v = vals.get(short_key)
            if v is None or v <= 0:
                return None
        for wing_key in ("ce_long", "pe_long"):
            v = vals.get(wing_key)
            if v is None or v < 0:
                return None
        return FenceQuotes(
            ce_short=float(vals["ce_short"]),
            pe_short=float(vals["pe_short"]),
            ce_long=float(vals["ce_long"] or 0.0),
            pe_long=float(vals["pe_long"] or 0.0),
        )

    def _pos_symbols(self, pos: ThetaCliffPosition) -> dict[str, str]:
        return {
            "ce_short": pos.ce_short_symbol,
            "pe_short": pos.pe_short_symbol,
            "ce_long": pos.ce_long_symbol,
            "pe_long": pos.pe_long_symbol,
        }

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        open_pnl = 0.0
        open_gross = 0.0
        charges = 0.0
        pos_body = None
        if self.position is not None:
            pos = self.position
            pos_body = {
                "day": pos.day,
                "strategy": STRATEGY,
                "pe_long_strike": pos.pe_long_strike,
                "pe_short_strike": pos.pe_short_strike,
                "ce_short_strike": pos.ce_short_strike,
                "ce_long_strike": pos.ce_long_strike,
                "qty": pos.qty,
                "lots": pos.lots,
                "credit": pos.credit,
                "sigma_pts": pos.sigma_pts,
                "morning_high": pos.morning_high,
                "morning_low": pos.morning_low,
                "rv_pct": pos.rv_pct,
                "vix_yesterday": pos.vix_yesterday,
                "ce_open": pos.ce_open,
                "pe_open": pos.pe_open,
                "ce_stop_pending": pos.ce_stop_pending,
                "pe_stop_pending": pos.pe_stop_pending,
                "opened_at": pos.opened_at,
                "charges_open": pos.charges_open,
                "charges_closed": pos.charges_closed,
            }
            charges = float(pos.charges_open + pos.charges_closed)
            q = self._book_quotes(book, self._pos_symbols(pos))
            if q is not None:
                rem_credit = 0.0
                debit = 0.0
                close_legs: list[tuple[float, int, str]] = []
                if pos.ce_open:
                    rem_credit += pos.ce_short_entry - pos.ce_long_entry
                    ce_s_x = fill_buy(q.ce_short)
                    ce_l_x = fill_sell(q.ce_long, floor=0.0)
                    debit += ce_s_x - ce_l_x
                    close_legs.extend(_close_vertical_legs(ce_s_x, ce_l_x, pos.qty))
                if pos.pe_open:
                    rem_credit += pos.pe_short_entry - pos.pe_long_entry
                    pe_s_x = fill_buy(q.pe_short)
                    pe_l_x = fill_sell(q.pe_long, floor=0.0)
                    debit += pe_s_x - pe_l_x
                    close_legs.extend(_close_vertical_legs(pe_s_x, pe_l_x, pos.qty))
                open_gross = round((rem_credit - debit) * pos.qty, 2)
                close_ch = float(kite_nfo_charges(close_legs)["total"]) if close_legs else 0.0
                open_share = 0.0
                if pos.ce_open and pos.pe_open:
                    open_share = pos.charges_open
                elif pos.ce_open or pos.pe_open:
                    open_share = pos.charges_open * 0.5
                charges = round(pos.charges_open + pos.charges_closed + close_ch, 2)
                open_pnl = round(open_gross - open_share - close_ch, 2)
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
            "entry_window": (
                f"{ENTRY_AFTER[0]:02d}:{ENTRY_AFTER[1]:02d}"
                f"-{ENTRY_UNTIL[0]:02d}:{ENTRY_UNTIL[1]:02d}"
            ),
            "wing_pts": self.wing_pts,
            "max_entries_per_day": int(self.max_entries_per_day),
            "last_reject": self.last_reject,
            "last_event": self.last_event,
        }

    def _roll_to_day(self, day: str) -> None:
        self.traded_day = day
        self.entries_today = 0
        self.day_pnl = 0.0
        self.eod_written = False
        self.last_reject = None

    def _record_skip(
        self,
        now: datetime,
        reason: str,
        *,
        feed: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """One ``skip`` row per expiry day — only after the entry window ends."""
        day = now.strftime("%Y-%m-%d")
        if self.skip_day == day or self.entries_today > 0 or self.position is not None:
            return None
        # Do not log transient rejects inside 12:00–12:10 (quotes may appear later).
        if not hm_ge(now, AFTER_ENTRY):
            return None
        feed = feed or {}
        if not self._is_expiry_day(now, feed):
            return None
        if not reason:
            return None
        event = self._append(
            {
                "event": "skip",
                "ts": now.isoformat(),
                "day": day,
                "reason": reason,
                "spot": _f(feed.get("spot")),
                "vix_yesterday": _f(feed.get("vix_yesterday")),
                "entry_block": feed.get("entry_block"),
            }
        )
        if event is not None:
            self.skip_day = day
            self.traded_day = day
        return event

    def _maybe_finalize_skip(
        self,
        now: datetime,
        feed: dict[str, Any],
    ) -> dict[str, Any] | None:
        """After 12:10, if still flat, persist the last reject as the day's skip."""
        if self.position is not None or self.entries_today > 0:
            return None
        if not hm_ge(now, AFTER_ENTRY) or hm_ge(now, SQUARE_OFF):
            return None
        reason = self.last_reject or str(feed.get("entry_block") or "")
        if not reason:
            return None
        return self._record_skip(now, reason, feed=feed)

    def _seal_close(
        self,
        now: datetime,
        pos: ThetaCliffPosition,
        reason: str,
    ) -> dict[str, Any] | None:
        known = not bool(pos.pnl_unknown)
        return self._append(
            {
                "event": "close",
                "ts": now.isoformat(),
                "day": pos.day,
                "reason": reason,
                "qty": pos.qty,
                "credit": pos.credit,
                "entry": pos.credit,
                "pnl": None,
                "pnl_known": known,
                "day_pnl": round(self.day_pnl, 2),
                "day_pnl_pct": round(self.day_pnl / self.capital * 100.0, 4) if self.capital else 0.0,
                "capital": self.capital,
                "equity": round(self.capital + self.day_pnl, 2),
                "opened_at": pos.opened_at,
                "ce_short_symbol": pos.ce_short_symbol,
                "pe_short_symbol": pos.pe_short_symbol,
            }
        )

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
        bars_1m: list[dict[str, Any]] | None,
        option_symbol: OptionSymbolFn | None,
        allow_entry: bool = True,
    ) -> dict[str, Any] | None:
        day = now.strftime("%Y-%m-%d")
        if not _weekday(now):
            if self.position is not None:
                return self._flatten_all(now, book, "weekend", force=True)
            return None
        if self.position is not None and self.position.day != day:
            return self._flatten_all(now, book, "session_gap", force=True)
        if self.traded_day != day and self.position is None:
            sealed = self._seal_then_roll(now, day)
            if sealed is not None:
                return sealed
            if self.traded_day != day:
                return None
        if self.position is not None:
            closed = self._maybe_manage(now, feed, book)
            if hm_ge(now, SQUARE_OFF):
                eod = None if self.position is not None else self._write_eod_if_needed(now)
                return closed or eod
            return closed
        if hm_ge(now, SQUARE_OFF):
            return self._write_eod_if_needed(now)
        # Past entry window: one skip with the latest reject (if we never opened).
        if not in_theta_cliff_entry_window(now):
            if not allow_entry and feed.get("entry_block"):
                self.last_reject = str(feed.get("entry_block"))
            return self._maybe_finalize_skip(now, feed)
        if self.entries_today >= int(self.max_entries_per_day):
            return None
        if not allow_entry:
            # Remember reason; write skip only after 12:10 if still flat.
            self.last_reject = str(feed.get("entry_block") or "gate_blocked")
            return None
        opened = self._try_open(now, feed, book, bars_1m or [], option_symbol)
        # Transient rejects (no_quotes, etc.) stay in last_reject only until AFTER_ENTRY.
        return opened

    def _is_expiry_day(self, now: datetime, feed: dict[str, Any]) -> bool:
        exp = feed.get("expiry")
        if isinstance(exp, date):
            return exp == now.date()
        if isinstance(exp, str) and exp:
            try:
                return date.fromisoformat(exp[:10]) == now.date()
            except ValueError:
                pass
        dte = _f(feed.get("days_to_expiry"))
        return dte is not None and dte <= 0.5

    def _try_open(
        self,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource | None,
        bars_1m: list[dict[str, Any]],
        option_symbol: OptionSymbolFn | None,
    ) -> dict[str, Any] | None:
        if option_symbol is None:
            self.last_reject = "no_option_symbol"
            return None
        if not self._is_expiry_day(now, feed):
            self.last_reject = "not_expiry_day"
            return None
        spot = _f(feed.get("spot"))
        if spot is None or spot <= 0:
            self.last_reject = "no_spot"
            return None
        vix_y = _f(feed.get("vix_yesterday"))
        if vix_y is None or vix_y <= 0:
            # Do not silently substitute today's VIX as "yesterday".
            self.last_reject = "no_vix_yesterday"
            return None
        day = now.strftime("%Y-%m-%d")
        stats = morning_session_stats(bars_1m, day=day, until_hm="12:00")
        if stats is None:
            self.last_reject = "morning_bars"
            return None
        if not rv_filter_ok(stats["rv_pct"], vix_y):
            self.last_reject = f"rv_filter rv={stats['rv_pct']:.2f}>0.9*vix={vix_y:.2f}"
            return None
        sigma = remaining_sigma_pts(spot, vix_y, now)
        pe_l, pe_s, ce_s, ce_l = fence_strikes(
            spot,
            stats["morning_high"],
            stats["morning_low"],
            sigma,
            wing_pts=self.wing_pts,
        )
        symbols = {
            "pe_long": option_symbol(pe_l, "PE"),
            "pe_short": option_symbol(pe_s, "PE"),
            "ce_short": option_symbol(ce_s, "CE"),
            "ce_long": option_symbol(ce_l, "CE"),
        }
        q = self._book_quotes(book, symbols)
        if q is None:
            self.last_reject = "no_quotes"
            return None
        # Adverse slip vs LTP (same model as tcf_bt).
        ce_s_px = fill_sell(q.ce_short)
        pe_s_px = fill_sell(q.pe_short)
        ce_l_px = fill_buy(q.ce_long)
        pe_l_px = fill_buy(q.pe_long)
        credit = round((ce_s_px + pe_s_px) - (ce_l_px + pe_l_px), 2)
        if credit < MIN_CREDIT:
            self.last_reject = f"credit<{MIN_CREDIT}"
            return None
        qty = int(self.lots) * int(self.lot_size)
        if qty <= 0:
            return None
        charges_open = float(
            kite_nfo_charges(_open_legs(ce_s_px, pe_s_px, ce_l_px, pe_l_px, qty))["total"]
        )
        pos = ThetaCliffPosition(
            day=day,
            pe_long_strike=pe_l,
            pe_short_strike=pe_s,
            ce_short_strike=ce_s,
            ce_long_strike=ce_l,
            pe_long_symbol=symbols["pe_long"],
            pe_short_symbol=symbols["pe_short"],
            ce_short_symbol=symbols["ce_short"],
            ce_long_symbol=symbols["ce_long"],
            qty=qty,
            lots=int(self.lots),
            pe_long_entry=pe_l_px,
            pe_short_entry=pe_s_px,
            ce_short_entry=ce_s_px,
            ce_long_entry=ce_l_px,
            credit=credit,
            sigma_pts=round(sigma, 2),
            morning_high=stats["morning_high"],
            morning_low=stats["morning_low"],
            rv_pct=stats["rv_pct"],
            vix_yesterday=round(float(vix_y), 4),
            opened_at=now.isoformat(),
            charges_open=round(charges_open, 2),
        )
        event = self._append(
            {
                "event": "open",
                "ts": now.isoformat(),
                "day": pos.day,
                "qty": pos.qty,
                "lots": pos.lots,
                "lot_size": self.lot_size,
                "pe_long_strike": pos.pe_long_strike,
                "pe_short_strike": pos.pe_short_strike,
                "ce_short_strike": pos.ce_short_strike,
                "ce_long_strike": pos.ce_long_strike,
                "pe_long_symbol": pos.pe_long_symbol,
                "pe_short_symbol": pos.pe_short_symbol,
                "ce_short_symbol": pos.ce_short_symbol,
                "ce_long_symbol": pos.ce_long_symbol,
                "pe_long_entry": pos.pe_long_entry,
                "pe_short_entry": pos.pe_short_entry,
                "ce_short_entry": pos.ce_short_entry,
                "ce_long_entry": pos.ce_long_entry,
                "ce_short_ltp": round(q.ce_short, 2),
                "pe_short_ltp": round(q.pe_short, 2),
                "ce_long_ltp": round(q.ce_long, 2),
                "pe_long_ltp": round(q.pe_long, 2),
                "slip_pct": SLIP_PCT,
                "slip_min": SLIP_MIN,
                "credit": pos.credit,
                "entry": pos.credit,
                "sigma_pts": pos.sigma_pts,
                "morning_high": pos.morning_high,
                "morning_low": pos.morning_low,
                "rv_pct": pos.rv_pct,
                "vix_yesterday": pos.vix_yesterday,
                "spot": round(spot, 2),
                "ce_open": True,
                "pe_open": True,
                "charges": pos.charges_open,
                "wing_pts": self.wing_pts,
                "opened_at": pos.opened_at,
            }
        )
        if event is None:
            return None
        self.position = pos
        self.traded_day = pos.day
        self.entries_today += 1
        self.last_reject = None
        return event

    def _arm_stop(self, now: datetime, side: Side) -> None:
        """Remember a short-strike touch (ledger once) until the vertical closes."""
        pos = self.position
        if pos is None:
            return
        if side == "ce":
            if pos.ce_stop_pending or not pos.ce_open:
                return
            pos.ce_stop_pending = True
        else:
            if pos.pe_stop_pending or not pos.pe_open:
                return
            pos.pe_stop_pending = True
        self._append(
            {
                "event": "stop_pending",
                "ts": now.isoformat(),
                "day": pos.day,
                "side": side,
                "short_strike": pos.ce_short_strike if side == "ce" else pos.pe_short_strike,
            }
        )

    def _maybe_manage(
        self,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource | None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        spot = _f(feed.get("spot"))
        if spot is not None:
            if pos.ce_open and spot >= pos.ce_short_strike:
                self._arm_stop(now, "ce")
            if pos.pe_open and spot <= pos.pe_short_strike:
                self._arm_stop(now, "pe")
        # Sticky stop: retry until quotes fill (do not drop the touch).
        if pos.ce_open and pos.ce_stop_pending:
            closed = self._close_side(now, book, "ce", "spot_touch_short")
            if closed is not None:
                return closed
        if self.position is not None and self.position.pe_open and self.position.pe_stop_pending:
            closed = self._close_side(now, book, "pe", "spot_touch_short")
            if closed is not None:
                return closed
        if hm_ge(now, SQUARE_OFF):
            # 15:15–15:25: keep retrying quotes; force only after FORCE_FLAT.
            return self._flatten_all(
                now,
                book,
                "time",
                force=hm_ge(now, FORCE_FLAT),
            )
        return None

    def _close_side(
        self,
        now: datetime,
        book: QuoteSource | None,
        side: Side,
        reason: str,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        if side == "ce" and not pos.ce_open:
            return None
        if side == "pe" and not pos.pe_open:
            return None
        if side == "ce":
            short_sym, long_sym = pos.ce_short_symbol, pos.ce_long_symbol
            short_e, long_e = pos.ce_short_entry, pos.ce_long_entry
        else:
            short_sym, long_sym = pos.pe_short_symbol, pos.pe_long_symbol
            short_e, long_e = pos.pe_short_entry, pos.pe_long_entry
        short_px = quote_ltp(book.get(short_sym)) if book else None
        long_px = quote_ltp(book.get(long_sym)) if book else None
        # Wings may be 0; shorts must have a print. Do not recurse into flatten.
        if short_px is None or short_px <= 0 or long_px is None or long_px < 0:
            return None
        short_fill = fill_buy(float(short_px))
        long_fill = fill_sell(float(long_px), floor=0.0)
        credit_side = short_e - long_e
        debit_side = short_fill - long_fill
        pnl_gross = round((credit_side - debit_side) * pos.qty, 2)
        charges_close = float(
            kite_nfo_charges(_close_vertical_legs(short_fill, long_fill, pos.qty))["total"]
        )
        # Always split entry charges 50/50 across the two verticals.
        open_share = float(pos.charges_open) * 0.5
        charges = round(open_share + charges_close, 2)
        pnl = round(pnl_gross - charges, 2)
        new_day = round(self.day_pnl + pnl, 2)
        event = self._append(
            {
                "event": "close_vertical",
                "ts": now.isoformat(),
                "day": pos.day,
                "reason": reason,
                "side": side,
                "qty": pos.qty,
                "short_strike": pos.ce_short_strike if side == "ce" else pos.pe_short_strike,
                "long_strike": pos.ce_long_strike if side == "ce" else pos.pe_long_strike,
                "short_symbol": short_sym,
                "long_symbol": long_sym,
                "entry": round(credit_side, 2),
                "exit": round(debit_side, 2),
                "short_ltp": round(float(short_px), 2),
                "long_ltp": round(float(long_px), 2),
                "pnl_gross": pnl_gross,
                "charges": charges,
                "charges_close": charges_close,
                "pnl": pnl,
                "pnl_known": True,
                "opened_at": pos.opened_at,
                "day_pnl": new_day,
                "day_pnl_pct": round(new_day / self.capital * 100.0, 4) if self.capital else 0.0,
                "capital": self.capital,
                "equity": round(self.capital + new_day, 2),
            }
        )
        if event is None:
            return None
        if side == "ce":
            pos.ce_open = False
            pos.ce_stop_pending = False
        else:
            pos.pe_open = False
            pos.pe_stop_pending = False
        pos.charges_closed = round(pos.charges_closed + charges_close, 2)
        self.day_pnl = new_day
        if not pos.ce_open and not pos.pe_open:
            self.position = None
            seal = self._seal_close(now, pos, reason)
            return seal or event
        return event

    def _force_close_side(self, now: datetime, side: Side, reason: str) -> dict[str, Any] | None:
        """Clear a side with unknown marks (weekend / missing quotes at square-off)."""
        pos = self.position
        if pos is None:
            return None
        if side == "ce" and not pos.ce_open:
            return None
        if side == "pe" and not pos.pe_open:
            return None
        open_share = float(pos.charges_open) * 0.5
        new_day = round(self.day_pnl - open_share, 2)
        pos.pnl_unknown = True
        event = self._append(
            {
                "event": "close_vertical",
                "ts": now.isoformat(),
                "day": pos.day,
                "reason": reason,
                "side": side,
                "qty": pos.qty,
                "short_strike": pos.ce_short_strike if side == "ce" else pos.pe_short_strike,
                "long_strike": pos.ce_long_strike if side == "ce" else pos.pe_long_strike,
                "pnl_gross": None,
                "charges": open_share,
                "pnl": None,
                "pnl_known": False,
                "opened_at": pos.opened_at,
                "day_pnl": new_day,
                "day_pnl_pct": round(new_day / self.capital * 100.0, 4) if self.capital else 0.0,
                "capital": self.capital,
                "equity": round(self.capital + new_day, 2),
            }
        )
        if event is None:
            return None
        if side == "ce":
            pos.ce_open = False
            pos.ce_stop_pending = False
        else:
            pos.pe_open = False
            pos.pe_stop_pending = False
        self.day_pnl = new_day
        if not pos.ce_open and not pos.pe_open:
            self.position = None
            seal = self._seal_close(now, pos, reason)
            return seal or event
        return event

    def _flatten_all(
        self,
        now: datetime,
        book: QuoteSource | None,
        reason: str,
        *,
        marked: bool = True,
        force: bool | None = None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        # ``force`` overrides: after FORCE_FLAT / weekend / session gap.
        do_force = (not marked) if force is None else bool(force)
        last: dict[str, Any] | None = None
        for side in ("ce", "pe"):
            if self.position is None:
                break
            if side == "ce" and not self.position.ce_open:
                continue
            if side == "pe" and not self.position.pe_open:
                continue
            closed = self._close_side(now, book, side, reason)  # type: ignore[arg-type]
            if closed is None and do_force:
                closed = self._force_close_side(now, side, reason)  # type: ignore[arg-type]
            if closed is not None:
                last = closed
        if self.position is not None and not self.position.ce_open and not self.position.pe_open:
            self.position = None
        if self.position is None:
            # Avoid duplicate close if last side already sealed.
            if last is not None and last.get("event") == "close":
                return last
            return self._seal_close(now, pos, reason) or last
        return last
