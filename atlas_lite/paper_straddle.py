"""Paper-only NIFTY strategy: Rich-IV Iron Fly (never Kite orders).

Sheet + bell stay on the customer notebook 7/7. Do not change those.

This book is intentionally NOT the notebook gate set. From Sep 2026
session recordings (ATM-straddle proxy on ~11 cash days):

* Edge came from **selling** rich implied vs 1m-ATR realized vol.
* Long straddle overlay was rare and mixed → **removed** from the live book.
* Structure kept as **short iron fly** (ATM short + 250-pt wings) for
  defined risk on ₹2L / 1 lot (credit ≥ 100, max loss bounded by wing width).
* Entries only **09:20–13:00**; flatten by **15:14**. Skip open noise; leave
  time for theta. Day-trend filter ``|NIFTY chg| ≤ 0.75%``.

Soft sheet metrics (ADX, PCR, VIX, BN, SENSEX) are display-only — not ANDs.

Paper P&L is always net of Kite NSE options charges. Stops/targets fire on
premium P&L (½ credit target / ½ defined-loss stop).
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger, ist_now
from atlas_lite.metrics import quote_ltp
from atlas_lite.minute_bars import hm_ge, hm_le

IST = ZoneInfo("Asia/Kolkata")
LOTS = 1
MAX_ENTRIES_PER_DAY = 5
CAPITAL = 200_000.0
TARGET_PCT = 0.06
STOP_PCT = -0.04
STOP_PTS = 10.0
# Skip open auction; no new risk after 13:00 (afternoon fills were the losers).
ENTRY_AFTER = (9, 20)
ENTRY_UNTIL = (13, 0)
SQUARE_OFF = (15, 14)
DEFAULT_LOT_SIZE = 65
PAPER_IVP_LT = 40.0  # legacy long-overlay helper only (not used for entries)
PAPER_CE_PE_PCT = 15.0
# Was 0.49; recording grid peaked near 0.75 with more fills and similar WR.
PAPER_INDEX_ABS = 0.75
PAPER_OI_MIN = 50_000.0
PAPER_BARS_PER_DAY = 375.0
PAPER_TRADING_DAYS = 252.0
PAPER_TRAIL_PCT = 0.02
PAPER_TRAIL_PTS = 4.0
PAPER_GATES = "rich_iv_fly"
PAPER_INDEX_KEYS: tuple[tuple[str, str], ...] = (
    ("NIFTY 50", "index_nifty_chg"),
)
STRATEGY_LONG = "long_straddle"  # kept for ledger replay only — not opened
STRATEGY_FLY = "short_iron_fly"
FLY_WING_PTS = 250
FLY_CREDIT_MIN = 100.0
FLY_IVP_MAX = 90.0
FLY_VOL_OF_VOL_PTS = 5.0
# Require IV at least this many points rich vs RV (sell premium only when paid).
FLY_MIN_IV_RICH = 0.5
FLY_STOP_FRAC = 0.50
FLY_TARGET_FRAC = 0.50
# Long overlay disabled — recordings did not support it as a profit book.
PAPER_ENABLE_LONG = False


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def paper_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER", "1").strip().lower()
    return raw in ("1", "true", "yes")


def in_paper_entry_window(now: datetime) -> bool:
    """Cash open through the minute before square-off. 15:14 is flatten-only."""
    return (
        hm_ge(now, ENTRY_AFTER)
        and hm_le(now, ENTRY_UNTIL)
        and not hm_ge(now, SQUARE_OFF)
    )


def _weekday(now: datetime) -> bool:
    return now.weekday() < 5


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def kite_open_legs(pos: "PaperPosition") -> list[tuple[float, int, str]]:
    qty = int(pos.qty)
    if pos.strategy == STRATEGY_FLY:
        return [
            (pos.ce_entry, qty, "sell"),
            (pos.pe_entry, qty, "sell"),
            (pos.ce_wing_entry, qty, "buy"),
            (pos.pe_wing_entry, qty, "buy"),
        ]
    return [
        (pos.ce_entry, qty, "buy"),
        (pos.pe_entry, qty, "buy"),
    ]


def kite_close_legs(
    pos: "PaperPosition",
    ce: float,
    pe: float,
    wing_ce: float | None = None,
    wing_pe: float | None = None,
) -> list[tuple[float, int, str]] | None:
    qty = int(pos.qty)
    if pos.strategy == STRATEGY_FLY:
        if wing_ce is None or wing_pe is None:
            return None
        return [
            (float(ce), qty, "buy"),
            (float(pe), qty, "buy"),
            (float(wing_ce), qty, "sell"),
            (float(wing_pe), qty, "sell"),
        ]
    return [
        (float(ce), qty, "sell"),
        (float(pe), qty, "sell"),
    ]


def kite_charges_open(pos: "PaperPosition") -> dict[str, float]:
    return kite_nfo_charges(kite_open_legs(pos))


def kite_charges_close(
    pos: "PaperPosition",
    ce: float,
    pe: float,
    wing_ce: float | None = None,
    wing_pe: float | None = None,
) -> dict[str, float] | None:
    legs = kite_close_legs(pos, ce, pe, wing_ce, wing_pe)
    if legs is None:
        return None
    return kite_nfo_charges(legs)


def _net_pnl(gross: float, *charges: float) -> float:
    return round(float(gross) - sum(float(c) for c in charges), 2)


def stop_straddle_px(entry: float, *, stop_pct: float, stop_pts: float) -> float:
    """Stop price for a long straddle: farther of % and points (more room).

    `min()` of the two exit prices is the wider loss. Cheap straddles get at
    least `stop_pts` of room so a 4% stop is not tighter than bid-ask chatter.
    """
    from_pct = float(entry) * (1.0 + float(stop_pct))
    from_pts = float(entry) - float(stop_pts)
    return round(min(from_pct, from_pts), 2)


def target_straddle_px(entry: float, *, target_pct: float) -> float:
    return round(float(entry) * (1.0 + float(target_pct)), 2)


def trail_gap_px(entry: float, *, trail_pct: float, trail_pts: float) -> float:
    return round(max(float(entry) * float(trail_pct), float(trail_pts)), 2)


def realised_vol_pct(atr: float, spot: float) -> float:
    """Annualize 1m ATR to the same percent units as feed IV.

    ``(ATR / spot) × √(375 × 252) × 100`` — NSE cash has 375 one-minute
    bars; 252 trading days. Do not compare a remaining-session ATR path
    to a multi-day ATM straddle premium.
    """
    return (float(atr) / float(spot)) * math.sqrt(PAPER_BARS_PER_DAY * PAPER_TRADING_DAYS) * 100.0


def _ce_pe_balanced(ce: float | None, pe: float | None, *, max_pct: float) -> bool | None:
    if ce is None or pe is None or ce <= 0 or pe <= 0:
        return None
    avg = (abs(ce) + abs(pe)) / 2.0
    if avg <= 0:
        return None
    return (abs(ce - pe) / avg * 100.0) <= max_pct


def _iv_percent(value: float) -> float:
    if 0 < value < 1.0:
        return value * 100.0
    return value


def evaluate_paper_entry(
    feed: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Paper must-haves AND. Independent of notebook ``evaluate_sheet``.

    ``now`` is accepted so callers can pass session time; the entry window
    is applied in ``PaperStraddle.on_frame``, not here.
    """
    _ = now
    feed = feed if isinstance(feed, dict) else {}
    failing: list[str] = []
    missing: list[str] = []

    def _need(label: str, ok: bool | None) -> None:
        if ok is None:
            missing.append(label)
        elif not ok:
            failing.append(label)

    ivp = _f(feed.get("ivp"))
    _need("IV Percentile", None if ivp is None else ivp < PAPER_IVP_LT)

    ce = _f(feed.get("ce"))
    pe = _f(feed.get("pe"))
    _need("ATM CE", _ce_pe_balanced(ce, pe, max_pct=PAPER_CE_PE_PCT))

    for label, key in PAPER_INDEX_KEYS:
        chg = _f(feed.get(key))
        _need(label, None if chg is None else abs(chg) <= PAPER_INDEX_ABS)

    ce_oi = _f(feed.get("ce_oi"))
    pe_oi = _f(feed.get("pe_oi"))
    if ce_oi is None or pe_oi is None:
        _need("Liquidity", None)
    else:
        _need("Liquidity", min(ce_oi, pe_oi) >= PAPER_OI_MIN)

    atr = _f(feed.get("atr"))
    spot = _f(feed.get("nifty_ltp"))
    if spot is None:
        spot = _f(feed.get("spot"))
    iv_raw = _f(feed.get("iv"))
    realised: float | None = None
    implied: float | None = None
    if atr is None or atr <= 0 or spot is None or spot <= 0 or iv_raw is None or iv_raw <= 0:
        _need("RV vs IV", None)
    else:
        realised = realised_vol_pct(atr, spot)
        implied = _iv_percent(iv_raw)
        _need("RV vs IV", realised > implied)

    evaluable_ok = not failing and not missing
    edge = None
    if realised is not None and implied is not None:
        edge = realised - implied
    return {
        "ready": evaluable_ok,
        "failing_gates": failing,
        "missing_gates": missing,
        "gates": "tape",  # diagnostic only — live book does not open longs
        "realised_vol": realised,
        "implied_vol": implied,
        "straddle_edge": edge,
    }


def _vol_edge(feed: dict[str, Any]) -> tuple[float | None, float | None, bool | None]:
    """Return (RV, IV, RV>IV). Last value is None when inputs are missing."""
    atr = _f(feed.get("atr"))
    spot = _f(feed.get("nifty_ltp"))
    if spot is None:
        spot = _f(feed.get("spot"))
    iv_raw = _f(feed.get("iv"))
    if atr is None or atr <= 0 or spot is None or spot <= 0 or iv_raw is None or iv_raw <= 0:
        return None, None, None
    realised = realised_vol_pct(atr, spot)
    implied = _iv_percent(iv_raw)
    return realised, implied, realised > implied


def iron_fly_strikes(atm: int, *, wing_pts: int = FLY_WING_PTS) -> tuple[int, int]:
    """PE wing (ATM − width), CE wing (ATM + width)."""
    width = int(wing_pts)
    return int(atm) - width, int(atm) + width


def fly_value_pts(
    short_ce: float,
    short_pe: float,
    wing_ce: float,
    wing_pe: float,
    *,
    width: float | None = None,
) -> float:
    """Debit to close a short iron fly (short straddle minus long wings).

    Long wings may mark at 0. When ``width`` is set, clamp to [0, width] so
    MTM cannot exceed defined max loss if the far OTM wing quotes 0.
    """
    raw = float(short_ce) + float(short_pe) - float(wing_ce) - float(wing_pe)
    if width is not None and width > 0:
        raw = min(float(width), max(0.0, raw))
    return round(raw, 2)


def evaluate_paper_fly(
    feed: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Short iron-fly AND. Independent of notebook ``evaluate_sheet``.

    Sell defined-risk when IV is rich vs RV by ``FLY_MIN_IV_RICH`` points,
    |NIFTY day chg| is moderate, and IV is not a crisis spike. Skip
    vol-of-vol when 5-day IV change exceeds ``FLY_VOL_OF_VOL_PTS``. Missing
    5-day IV change does not block. Long overlay is a separate function.
    """
    _ = now
    feed = feed if isinstance(feed, dict) else {}
    failing: list[str] = []
    missing: list[str] = []

    def _need(label: str, ok: bool | None) -> None:
        if ok is None:
            missing.append(label)
        elif not ok:
            failing.append(label)

    ivp = _f(feed.get("ivp"))
    _need("IV Percentile", None if ivp is None else ivp < FLY_IVP_MAX)

    ce = _f(feed.get("ce"))
    pe = _f(feed.get("pe"))
    _need("ATM CE", _ce_pe_balanced(ce, pe, max_pct=PAPER_CE_PE_PCT))

    for label, key in PAPER_INDEX_KEYS:
        chg = _f(feed.get(key))
        _need(label, None if chg is None else abs(chg) <= PAPER_INDEX_ABS)

    ce_oi = _f(feed.get("ce_oi"))
    pe_oi = _f(feed.get("pe_oi"))
    if ce_oi is None or pe_oi is None:
        _need("Liquidity", None)
    else:
        _need("Liquidity", min(ce_oi, pe_oi) >= PAPER_OI_MIN)

    realised, implied, _rv_gt_iv = _vol_edge(feed)
    if realised is None or implied is None:
        _need("RV vs IV", None)
    else:
        # Sell only when implied is meaningfully rich (study: negative edge won).
        _need("RV vs IV", (implied - realised) >= FLY_MIN_IV_RICH)

    iv_chg = _f(feed.get("iv_chg_5d"))
    if iv_chg is not None and iv_chg > FLY_VOL_OF_VOL_PTS:
        failing.append("Vol-of-vol")

    evaluable_ok = not failing and not missing
    edge = None
    if realised is not None and implied is not None:
        edge = realised - implied
    return {
        "ready": evaluable_ok,
        "failing_gates": failing,
        "missing_gates": missing,
        "gates": "fly",
        "realised_vol": realised,
        "implied_vol": implied,
        "straddle_edge": edge,
        "wing_pts": FLY_WING_PTS,
    }


def evaluate_paper_regime(
    feed: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rich-IV iron fly only. Long overlay is off (``PAPER_ENABLE_LONG``)."""
    long_ev = evaluate_paper_entry(feed, now=now)
    fly_ev = evaluate_paper_fly(feed, now=now)
    strategy = None
    if PAPER_ENABLE_LONG and long_ev.get("ready"):
        strategy = STRATEGY_LONG
    elif fly_ev.get("ready"):
        strategy = STRATEGY_FLY
    return {
        "strategy": strategy,
        "long": long_ev,
        "fly": fly_ev,
        "realised_vol": fly_ev.get("realised_vol") if strategy == STRATEGY_FLY else long_ev.get("realised_vol"),
        "implied_vol": fly_ev.get("implied_vol") if strategy == STRATEGY_FLY else long_ev.get("implied_vol"),
        "straddle_edge": fly_ev.get("straddle_edge") if strategy == STRATEGY_FLY else long_ev.get("straddle_edge"),
        "book": "rich_iv_fly",
    }


def _position_from_open_event(event: dict[str, Any], *, lot_size: int) -> PaperPosition | None:
    ce_sym = str(event.get("ce_symbol") or "")
    pe_sym = str(event.get("pe_symbol") or "")
    atm = event.get("atm")
    ce = _f(event.get("ce"))
    pe = _f(event.get("pe"))
    straddle = _f(event.get("straddle"))
    if straddle is None and ce is not None and pe is not None:
        straddle = round(ce + pe, 2)
    if not ce_sym or not pe_sym or atm is None or ce is None or pe is None:
        return None
    if straddle is None or straddle <= 0:
        return None
    stop_px = _f(event.get("stop_loss"))
    target_px = _f(event.get("target"))
    if stop_px is None:
        stop_px = stop_straddle_px(straddle, stop_pct=STOP_PCT, stop_pts=STOP_PTS)
    if target_px is None:
        target_px = target_straddle_px(straddle, target_pct=TARGET_PCT)
    lots = int(event.get("lots") or LOTS)
    qty = int(event.get("qty") or 0)
    if qty <= 0:
        qty = lots * int(lot_size)
    day = str(event.get("day") or "")
    opened_at = str(event.get("opened_at") or event.get("ts") or "")
    try:
        atm_i = int(atm)
    except (TypeError, ValueError):
        return None
    strategy = str(event.get("strategy") or STRATEGY_LONG)
    wing_ce_sym = str(event.get("wing_ce_symbol") or "")
    wing_pe_sym = str(event.get("wing_pe_symbol") or "")
    wing_pts = int(event.get("wing_pts") or 0)
    ce_wing = _f(event.get("ce_wing"))
    pe_wing = _f(event.get("pe_wing"))
    credit = _f(event.get("credit"))
    max_loss_pts = _f(event.get("max_loss_pts"))
    if strategy == STRATEGY_FLY:
        if not wing_ce_sym or not wing_pe_sym:
            return None
        if ce_wing is None or pe_wing is None or credit is None or credit <= 0:
            return None
        if wing_pts <= 0:
            wing_pts = FLY_WING_PTS
        if max_loss_pts is None:
            max_loss_pts = round(float(wing_pts) - credit, 2)
        if not event.get("stop_pnl"):
            event = dict(event)
            event["stop_pnl"] = round(-FLY_STOP_FRAC * float(max_loss_pts) * qty, 2)
        if not event.get("target_pnl"):
            event = dict(event)
            event["target_pnl"] = round(FLY_TARGET_FRAC * credit * qty, 2)
    pos = PaperPosition(
        day=day,
        atm=atm_i,
        ce_symbol=ce_sym,
        pe_symbol=pe_sym,
        qty=qty,
        lots=lots,
        ce_entry=round(ce, 2),
        pe_entry=round(pe, 2),
        straddle_entry=round(straddle, 2),
        stop_straddle=round(stop_px, 2),
        target_straddle=round(target_px, 2),
        opened_at=opened_at,
        trail_armed=False,
        peak_straddle=round(straddle, 2),
        strategy=strategy,
        wing_pts=wing_pts,
        wing_ce_symbol=wing_ce_sym,
        wing_pe_symbol=wing_pe_sym,
        ce_wing_entry=0.0 if ce_wing is None else round(ce_wing, 2),
        pe_wing_entry=0.0 if pe_wing is None else round(pe_wing, 2),
        credit=0.0 if credit is None else round(credit, 2),
        max_loss_pts=0.0 if max_loss_pts is None else round(max_loss_pts, 2),
        pe_wing_strike=int(event.get("pe_wing_strike") or 0),
        ce_wing_strike=int(event.get("ce_wing_strike") or 0),
        stop_pnl=float(event.get("stop_pnl") or 0),
        target_pnl=float(event.get("target_pnl") or 0),
        charges_open=0.0,
    )
    stored = _f(event.get("charges"))
    pos.charges_open = (
        round(stored, 2) if stored is not None else round(float(kite_charges_open(pos)["total"]), 2)
    )
    return pos


@dataclass
class PaperPosition:
    day: str
    atm: int
    ce_symbol: str
    pe_symbol: str
    qty: int
    lots: int
    ce_entry: float
    pe_entry: float
    straddle_entry: float
    stop_straddle: float
    target_straddle: float
    opened_at: str
    trail_armed: bool = False
    peak_straddle: float = 0.0
    strategy: str = STRATEGY_LONG
    wing_pts: int = 0
    wing_ce_symbol: str = ""
    wing_pe_symbol: str = ""
    ce_wing_entry: float = 0.0
    pe_wing_entry: float = 0.0
    credit: float = 0.0
    max_loss_pts: float = 0.0
    pe_wing_strike: int = 0
    ce_wing_strike: int = 0
    stop_pnl: float = 0.0
    target_pnl: float = 0.0
    charges_open: float = 0.0


@dataclass
class PaperStraddle:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = LOTS
    max_entries_per_day: int = MAX_ENTRIES_PER_DAY
    capital: float = CAPITAL
    target_pct: float = TARGET_PCT
    stop_pct: float = STOP_PCT
    stop_pts: float = STOP_PTS
    trail_pct: float = PAPER_TRAIL_PCT
    trail_pts: float = PAPER_TRAIL_PTS
    _log: Any = field(default_factory=lambda: get_logger("paper"), repr=False)
    position: PaperPosition | None = None
    traded_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    eod_written: bool = False
    last_event: dict[str, Any] | None = None
    _ready_prev: bool = False

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restore_from_ledger()

    def _restore_from_ledger(self) -> None:
        """Rebuild today's cap and any unmatched open after a process restart."""
        if not self.path.is_file():
            return
        day = datetime.now(IST).strftime("%Y-%m-%d")
        opens = 0
        day_pnl = 0.0
        eod_written = False
        last_today: dict[str, Any] | None = None
        last_any: dict[str, Any] | None = None
        try:
            with self.path.open(encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    last_any = event
                    if str(event.get("day") or "") != day:
                        continue
                    last_today = event
                    kind = event.get("event")
                    if kind == "open":
                        opens += 1
                    elif kind == "close":
                        day_pnl += float(event.get("pnl") or 0)
                    elif kind == "day_pnl":
                        eod_written = True
                        if event.get("day_pnl") is not None:
                            day_pnl = float(event["day_pnl"])
        except OSError as exc:
            self._log.warning("paper ledger read failed: %s", exc)
            return
        self.last_event = last_any
        if last_today is not None:
            self.traded_day = day
            self.entries_today = opens
            self.day_pnl = round(day_pnl, 2)
            self.eod_written = eod_written
            self._ready_prev = True
        leftover = last_any if last_any and last_any.get("event") == "open" else None
        if leftover is None:
            return
        pos = _position_from_open_event(leftover, lot_size=self.lot_size)
        if pos is None:
            self._log.warning("paper ledger open incomplete; cap restored count=%s", opens)
            return
        self.position = pos
        self._log.info(
            "PAPER restored open atm=%s qty=%s day=%s entries_today=%s",
            pos.atm,
            pos.qty,
            pos.day,
            self.entries_today,
        )

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        pos = None
        open_pnl = 0.0
        open_pnl_gross = 0.0
        charges = 0.0
        charges_open = 0.0
        charges_close = 0.0
        if self.position is not None:
            pos = {
                "day": self.position.day,
                "strategy": self.position.strategy,
                "atm": self.position.atm,
                "ce_symbol": self.position.ce_symbol,
                "pe_symbol": self.position.pe_symbol,
                "qty": self.position.qty,
                "lots": self.position.lots,
                "ce_entry": self.position.ce_entry,
                "pe_entry": self.position.pe_entry,
                "straddle_entry": self.position.straddle_entry,
                "stop_straddle": self.position.stop_straddle,
                "target_straddle": self.position.target_straddle,
                "trail_armed": self.position.trail_armed,
                "peak_straddle": self.position.peak_straddle,
                "opened_at": self.position.opened_at,
                "wing_pts": self.position.wing_pts,
                "wing_ce_symbol": self.position.wing_ce_symbol,
                "wing_pe_symbol": self.position.wing_pe_symbol,
                "ce_wing_entry": self.position.ce_wing_entry,
                "pe_wing_entry": self.position.pe_wing_entry,
                "credit": self.position.credit,
                "max_loss_pts": self.position.max_loss_pts,
                "pe_wing_strike": self.position.pe_wing_strike,
                "ce_wing_strike": self.position.ce_wing_strike,
                "charges_open": self.position.charges_open,
            }
            charges_open = round(float(self.position.charges_open), 2)
            charges = charges_open
            if book is not None:
                marked = self._mark_open_pnl(book)
                if marked is not None:
                    open_pnl = marked["pnl"]
                    open_pnl_gross = marked["pnl_gross"]
                    charges = marked["charges"]
                    charges_close = marked["charges_close"]
        stats = self._capital_stats()
        mtm = round(stats["day_pnl"] + open_pnl, 2)
        mtm_pct = round((mtm / stats["capital"]) * 100.0, 4) if stats["capital"] else 0.0
        return {
            "ok": True,
            "mode": "paper",
            "live_orders": False,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "qty_per_leg": self.lots * self.lot_size,
            "max_entries_per_day": self.max_entries_per_day,
            "entries_today": self.entries_today,
            "capital": stats["capital"],
            "day_pnl": stats["day_pnl"],
            "day_pnl_pct": stats["day_pnl_pct"],
            "open_pnl": open_pnl,
            "open_pnl_gross": open_pnl_gross,
            "charges": charges,
            "charges_open": charges_open,
            "charges_close": charges_close,
            "mtm_pnl": mtm,
            "mtm_pnl_pct": mtm_pct,
            "equity": round(stats["capital"] + mtm, 2),
            "eod": self.eod_written,
            "target_pct": self.target_pct,
            "trail_pct": self.trail_pct,
            "trail_pts": self.trail_pts,
            "stop_loss_pct": abs(self.stop_pct) * 100.0,
            "stop_loss_pts": self.stop_pts,
            "stop_pct": self.stop_pct,
            "square_off": f"{SQUARE_OFF[0]:02d}:{SQUARE_OFF[1]:02d}",
            "entry_window": (
                f"{ENTRY_AFTER[0]:02d}:{ENTRY_AFTER[1]:02d}"
                f"-{ENTRY_UNTIL[0]:02d}:{ENTRY_UNTIL[1]:02d}"
            ),
            "entry_filters": PAPER_GATES,
            "fly_wing_pts": FLY_WING_PTS,
            "fly_credit_min": FLY_CREDIT_MIN,
            "position": pos,
            "traded_day": self.traded_day or None,
            "last_event": self.last_event,
        }

    def _capital_stats(self) -> dict[str, float]:
        pnl = round(float(self.day_pnl), 2)
        capital = float(self.capital)
        pct = round((pnl / capital) * 100.0, 4) if capital else 0.0
        return {
            "capital": capital,
            "day_pnl": pnl,
            "day_pnl_pct": pct,
            "equity": round(capital + pnl, 2),
        }

    def _mark_open_pnl(self, book: QuoteSource) -> dict[str, float] | None:
        pos = self.position
        if pos is None:
            return None
        ce, pe, wce, wpe = self._quote_legs(book, pos)
        if not self._legs_ok(ce, pe, wce, wpe, pos.strategy):
            return None
        assert ce is not None and pe is not None
        if pos.strategy == STRATEGY_FLY:
            assert wce is not None and wpe is not None
            value = fly_value_pts(ce, pe, wce, wpe, width=pos.wing_pts)
            gross = round((pos.credit - value) * pos.qty, 2)
        else:
            wce = wpe = None
            gross = round((ce + pe - pos.straddle_entry) * pos.qty, 2)
        close_ch = kite_charges_close(pos, ce, pe, wce, wpe) or {"total": 0.0}
        open_ch = round(float(pos.charges_open), 2)
        close_total = round(float(close_ch["total"]), 2)
        charges = round(open_ch + close_total, 2)
        return {
            "pnl_gross": gross,
            "charges_open": open_ch,
            "charges_close": close_total,
            "charges": charges,
            "pnl": _net_pnl(gross, open_ch, close_total),
        }

    @staticmethod
    def _quote_legs(
        book: QuoteSource, pos: PaperPosition
    ) -> tuple[float | None, float | None, float | None, float | None]:
        ce = quote_ltp(book.get(pos.ce_symbol))
        pe = quote_ltp(book.get(pos.pe_symbol))
        wce = wpe = None
        if pos.strategy == STRATEGY_FLY:
            wce = quote_ltp(book.get(pos.wing_ce_symbol))
            wpe = quote_ltp(book.get(pos.wing_pe_symbol))
        return ce, pe, wce, wpe

    @staticmethod
    def _legs_ok(
        ce: float | None,
        pe: float | None,
        wce: float | None,
        wpe: float | None,
        strategy: str,
        *,
        open_new: bool = False,
    ) -> bool:
        if ce is None or pe is None:
            return False
        if open_new:
            if ce <= 0 or pe <= 0:
                return False
        elif ce < 0 or pe < 0:
            return False
        if strategy == STRATEGY_FLY:
            if wce is None or wpe is None:
                return False
            if open_new:
                return wce > 0 and wpe > 0
            # 0.00 is a valid mark on any leg (far OTM). Only None is missing.
            return wce >= 0 and wpe >= 0
        return True

    def on_frame(
        self,
        *,
        now: datetime,
        entry_ready: bool,
        feed: dict[str, Any],
        book: QuoteSource,
        ce_symbol: str | None,
        pe_symbol: str | None,
        atm: int | None,
        strategy: str | None = None,
        wing_ce_symbol: str | None = None,
        wing_pe_symbol: str | None = None,
        wing_pts: int | None = None,
    ) -> dict[str, Any] | None:
        """Advance paper state. Returns the ledger event if one was written."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)
        else:
            now = now.astimezone(IST)
        want: str | None = None
        if strategy in (STRATEGY_LONG, STRATEGY_FLY):
            # Explicit strategy from regime (fly) or unit tests / ledger replay.
            want = strategy
        elif entry_ready:
            # Legacy test helper path — live paper always passes strategy= from regime.
            want = STRATEGY_LONG
        day = now.strftime("%Y-%m-%d")
        if self.position is not None and self.position.day != day:
            return self._flatten_stale(now, book)
        if not _weekday(now):
            return None
        if self.traded_day != day and self.position is None:
            self.traded_day = day
            self.entries_today = 0
            self.day_pnl = 0.0
            self.eod_written = False
            self._ready_prev = False
        if self.position is not None:
            self._ready_prev = want is not None
            closed = self._maybe_exit(now, book)
            if hm_ge(now, SQUARE_OFF):
                eod = self._write_eod_if_needed(now)
                return closed or eod
            return closed
        if hm_ge(now, SQUARE_OFF):
            return self._write_eod_if_needed(now)
        if want is None:
            self._ready_prev = False
            return None
        if self._ready_prev:
            return None
        if self.entries_today >= int(self.max_entries_per_day):
            return None
        if not in_paper_entry_window(now):
            return None
        if want == STRATEGY_FLY:
            opened = self._open_fly(
                now,
                feed,
                book,
                ce_symbol,
                pe_symbol,
                atm,
                wing_ce_symbol=wing_ce_symbol,
                wing_pe_symbol=wing_pe_symbol,
                wing_pts=wing_pts,
            )
        else:
            opened = self._open(now, feed, book, ce_symbol, pe_symbol, atm)
        if opened is not None:
            self._ready_prev = True
        return opened

    def _open(
        self,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource,
        ce_symbol: str | None,
        pe_symbol: str | None,
        atm: int | None,
    ) -> dict[str, Any] | None:
        ce_sym = ce_symbol or str(feed.get("ce_symbol") or "")
        pe_sym = pe_symbol or str(feed.get("pe_symbol") or "")
        if not ce_sym or not pe_sym or atm is None:
            return None
        ce = quote_ltp(book.get(ce_sym))
        pe = quote_ltp(book.get(pe_sym))
        if ce is None:
            ce = _f(feed.get("ce"))
        if pe is None:
            pe = _f(feed.get("pe"))
        if ce is None or pe is None or ce <= 0 or pe <= 0:
            return None
        qty = int(self.lots) * int(self.lot_size)
        if qty <= 0:
            return None
        straddle = round(ce + pe, 2)
        stop_px = stop_straddle_px(straddle, stop_pct=self.stop_pct, stop_pts=self.stop_pts)
        target_px = target_straddle_px(straddle, target_pct=self.target_pct)
        pos = PaperPosition(
            day=now.strftime("%Y-%m-%d"),
            atm=int(atm),
            ce_symbol=ce_sym,
            pe_symbol=pe_sym,
            qty=qty,
            lots=int(self.lots),
            ce_entry=round(ce, 2),
            pe_entry=round(pe, 2),
            straddle_entry=straddle,
            stop_straddle=stop_px,
            target_straddle=target_px,
            opened_at=now.isoformat(),
            trail_armed=False,
            peak_straddle=straddle,
        )
        pos.charges_open = round(float(kite_charges_open(pos)["total"]), 2)
        self.position = pos
        self.traded_day = pos.day
        self.entries_today += 1
        event = {
            "event": "open",
            "mode": "paper",
            "strategy": "long_straddle",
            "gates": "tape",
            "ts": now.isoformat(),
            "day": pos.day,
            "entry_n": self.entries_today,
            "max_entries_per_day": self.max_entries_per_day,
            "atm": pos.atm,
            "ce_symbol": pos.ce_symbol,
            "pe_symbol": pos.pe_symbol,
            "lots": pos.lots,
            "lot_size": self.lot_size,
            "qty": pos.qty,
            "ce": pos.ce_entry,
            "pe": pos.pe_entry,
            "straddle": pos.straddle_entry,
            "stop_loss": pos.stop_straddle,
            "stop_loss_pct": abs(self.stop_pct) * 100.0,
            "stop_loss_pts": self.stop_pts,
            "target": pos.target_straddle,
            "trail_pct": self.trail_pct,
            "trail_pts": self.trail_pts,
            "premium": round(straddle * qty, 2),
            "charges": pos.charges_open,
        }
        return self._commit(event)

    def _open_fly(
        self,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource,
        ce_symbol: str | None,
        pe_symbol: str | None,
        atm: int | None,
        *,
        wing_ce_symbol: str | None,
        wing_pe_symbol: str | None,
        wing_pts: int | None,
    ) -> dict[str, Any] | None:
        ce_sym = ce_symbol or str(feed.get("ce_symbol") or "")
        pe_sym = pe_symbol or str(feed.get("pe_symbol") or "")
        wce_sym = wing_ce_symbol or str(feed.get("wing_ce_symbol") or "")
        wpe_sym = wing_pe_symbol or str(feed.get("wing_pe_symbol") or "")
        width = int(wing_pts or feed.get("wing_pts") or FLY_WING_PTS)
        if not ce_sym or not pe_sym or not wce_sym or not wpe_sym or atm is None:
            return None
        ce = quote_ltp(book.get(ce_sym))
        pe = quote_ltp(book.get(pe_sym))
        wce = quote_ltp(book.get(wce_sym))
        wpe = quote_ltp(book.get(wpe_sym))
        if ce is None:
            ce = _f(feed.get("ce"))
        if pe is None:
            pe = _f(feed.get("pe"))
        if wce is None:
            wce = _f(feed.get("ce_wing"))
        if wpe is None:
            wpe = _f(feed.get("pe_wing"))
        if not self._legs_ok(ce, pe, wce, wpe, STRATEGY_FLY, open_new=True):
            return None
        assert ce is not None and pe is not None and wce is not None and wpe is not None
        credit = fly_value_pts(ce, pe, wce, wpe)
        if credit < FLY_CREDIT_MIN or credit >= width:
            return None
        qty = int(self.lots) * int(self.lot_size)
        if qty <= 0:
            return None
        max_loss_pts = round(float(width) - credit, 2)
        stop_pnl = round(-FLY_STOP_FRAC * max_loss_pts * qty, 2)
        target_pnl = round(FLY_TARGET_FRAC * credit * qty, 2)
        pe_wing_strike, ce_wing_strike = iron_fly_strikes(int(atm), wing_pts=width)
        straddle = round(ce + pe, 2)
        pos = PaperPosition(
            day=now.strftime("%Y-%m-%d"),
            atm=int(atm),
            ce_symbol=ce_sym,
            pe_symbol=pe_sym,
            qty=qty,
            lots=int(self.lots),
            ce_entry=round(ce, 2),
            pe_entry=round(pe, 2),
            straddle_entry=straddle,
            stop_straddle=0.0,
            target_straddle=0.0,
            opened_at=now.isoformat(),
            trail_armed=False,
            peak_straddle=straddle,
            strategy=STRATEGY_FLY,
            wing_pts=width,
            wing_ce_symbol=wce_sym,
            wing_pe_symbol=wpe_sym,
            ce_wing_entry=round(wce, 2),
            pe_wing_entry=round(wpe, 2),
            credit=credit,
            max_loss_pts=max_loss_pts,
            pe_wing_strike=pe_wing_strike,
            ce_wing_strike=ce_wing_strike,
            stop_pnl=stop_pnl,
            target_pnl=target_pnl,
        )
        pos.charges_open = round(float(kite_charges_open(pos)["total"]), 2)
        self.position = pos
        self.traded_day = pos.day
        self.entries_today += 1
        event = {
            "event": "open",
            "mode": "paper",
            "strategy": STRATEGY_FLY,
            "gates": "fly",
            "ts": now.isoformat(),
            "day": pos.day,
            "entry_n": self.entries_today,
            "max_entries_per_day": self.max_entries_per_day,
            "atm": pos.atm,
            "ce_symbol": pos.ce_symbol,
            "pe_symbol": pos.pe_symbol,
            "wing_ce_symbol": pos.wing_ce_symbol,
            "wing_pe_symbol": pos.wing_pe_symbol,
            "pe_wing_strike": pos.pe_wing_strike,
            "ce_wing_strike": pos.ce_wing_strike,
            "wing_pts": pos.wing_pts,
            "lots": pos.lots,
            "lot_size": self.lot_size,
            "qty": pos.qty,
            "ce": pos.ce_entry,
            "pe": pos.pe_entry,
            "ce_wing": pos.ce_wing_entry,
            "pe_wing": pos.pe_wing_entry,
            "straddle": pos.straddle_entry,
            "credit": pos.credit,
            "max_loss_pts": pos.max_loss_pts,
            "max_loss": round(pos.max_loss_pts * qty, 2),
            "stop_pnl": pos.stop_pnl,
            "target_pnl": pos.target_pnl,
            "premium": round(credit * qty, 2),
            "charges": pos.charges_open,
        }
        return self._commit(event)

    def _maybe_exit(self, now: datetime, book: QuoteSource) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        ce, pe, wce, wpe = self._quote_legs(book, pos)
        if not self._legs_ok(ce, pe, wce, wpe, pos.strategy):
            if hm_ge(now, SQUARE_OFF):
                return self._close(now, None, None, "time_flat", marked=False)
            return None
        assert ce is not None and pe is not None
        if pos.strategy == STRATEGY_FLY:
            assert wce is not None and wpe is not None
            value = fly_value_pts(ce, pe, wce, wpe, width=pos.wing_pts)
            pnl = round((pos.credit - value) * pos.qty, 2)
            reason = None
            if pnl <= pos.stop_pnl:
                reason = "stop"
            elif pnl >= pos.target_pnl:
                reason = "target"
            elif hm_ge(now, SQUARE_OFF):
                reason = "time"
            if reason is None:
                return None
            return self._close(now, ce, pe, reason, wing_ce=wce, wing_pe=wpe)
        straddle = ce + pe
        reason = None
        if straddle <= pos.stop_straddle:
            reason = "stop"
        else:
            if not pos.trail_armed and straddle >= pos.target_straddle:
                pos.trail_armed = True
                pos.peak_straddle = round(straddle, 2)
            if pos.trail_armed:
                pos.peak_straddle = round(max(pos.peak_straddle, straddle), 2)
                gap = trail_gap_px(
                    pos.straddle_entry,
                    trail_pct=self.trail_pct,
                    trail_pts=self.trail_pts,
                )
                if straddle <= pos.peak_straddle - gap:
                    reason = "trail"
            if reason is None and hm_ge(now, SQUARE_OFF):
                reason = "time"
        if reason is None:
            return None
        return self._close(now, ce, pe, reason)

    def _flatten_stale(self, now: datetime, book: QuoteSource) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        ce, pe, wce, wpe = self._quote_legs(book, pos)
        if not self._legs_ok(ce, pe, wce, wpe, pos.strategy):
            return self._close(now, None, None, "session_gap", marked=False)
        return self._close(now, ce, pe, "session_gap", wing_ce=wce, wing_pe=wpe)

    def _close(
        self,
        now: datetime,
        ce: float | None,
        pe: float | None,
        reason: str,
        *,
        marked: bool = True,
        wing_ce: float | None = None,
        wing_pe: float | None = None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        straddle = None
        pnl: float | None = None
        pnl_gross: float | None = None
        charges: float | None = None
        charges_open: float | None = None
        charges_close: float | None = None
        pct: float | None = None
        ce_exit: float | None = None
        pe_exit: float | None = None
        wce_exit: float | None = None
        wpe_exit: float | None = None
        value = None
        if marked and pos.strategy == STRATEGY_FLY:
            if (
                ce is not None
                and pe is not None
                and wing_ce is not None
                and wing_pe is not None
                and ce >= 0
                and pe >= 0
                and wing_ce >= 0
                and wing_pe >= 0
            ):
                straddle = round(float(ce) + float(pe), 2)
                value = fly_value_pts(ce, pe, wing_ce, wing_pe, width=pos.wing_pts)
                pnl_pt = round(pos.credit - value, 2)
                pnl_gross = round(pnl_pt * pos.qty, 2)
                close_ch = kite_charges_close(pos, ce, pe, wing_ce, wing_pe) or {"total": 0.0}
                charges_close = round(float(close_ch["total"]), 2)
                charges_open = round(float(pos.charges_open), 2)
                charges = round(charges_open + charges_close, 2)
                pnl = _net_pnl(pnl_gross, charges)
                pct = round((pnl_pt / pos.credit) * 100.0, 4) if pos.credit else 0.0
                ce_exit = round(float(ce), 2)
                pe_exit = round(float(pe), 2)
                wce_exit = round(float(wing_ce), 2)
                wpe_exit = round(float(wing_pe), 2)
            else:
                marked = False
        elif marked and ce is not None and pe is not None:
            straddle = round(float(ce) + float(pe), 2)
            pnl_pt = round(straddle - pos.straddle_entry, 2)
            pnl_gross = round(pnl_pt * pos.qty, 2)
            close_ch = kite_charges_close(pos, ce, pe) or {"total": 0.0}
            charges_close = round(float(close_ch["total"]), 2)
            charges_open = round(float(pos.charges_open), 2)
            charges = round(charges_open + charges_close, 2)
            pnl = _net_pnl(pnl_gross, charges)
            pct = (
                round((pnl_pt / pos.straddle_entry) * 100.0, 4) if pos.straddle_entry else 0.0
            )
            ce_exit = round(float(ce), 2)
            pe_exit = round(float(pe), 2)
        event = {
            "event": "close",
            "mode": "paper",
            "strategy": pos.strategy,
            "ts": now.isoformat(),
            "day": pos.day,
            "reason": reason,
            "atm": pos.atm,
            "ce_symbol": pos.ce_symbol,
            "pe_symbol": pos.pe_symbol,
            "qty": pos.qty,
            "ce_entry": pos.ce_entry,
            "pe_entry": pos.pe_entry,
            "ce_exit": ce_exit,
            "pe_exit": pe_exit,
            "straddle_entry": pos.straddle_entry,
            "straddle_exit": straddle,
            "stop_loss": pos.stop_straddle,
            "stop_loss_pct": abs(self.stop_pct) * 100.0,
            "pct": pct,
            "pnl_gross": pnl_gross,
            "charges": charges,
            "charges_open": charges_open,
            "charges_close": charges_close,
            "pnl": pnl,
            "pnl_known": marked and pnl is not None,
            "opened_at": pos.opened_at,
        }
        if pos.strategy == STRATEGY_FLY:
            event["wing_ce_symbol"] = pos.wing_ce_symbol
            event["wing_pe_symbol"] = pos.wing_pe_symbol
            event["ce_wing_entry"] = pos.ce_wing_entry
            event["pe_wing_entry"] = pos.pe_wing_entry
            event["ce_wing_exit"] = wce_exit
            event["pe_wing_exit"] = wpe_exit
            event["credit"] = pos.credit
            event["credit_exit"] = value
            event["max_loss_pts"] = pos.max_loss_pts
            event["wing_pts"] = pos.wing_pts
        self.position = None
        if marked and pnl is not None and pos.day == now.strftime("%Y-%m-%d"):
            self.day_pnl = round(self.day_pnl + pnl, 2)
        stats = self._capital_stats()
        event["day_pnl"] = stats["day_pnl"]
        event["day_pnl_pct"] = stats["day_pnl_pct"]
        event["capital"] = stats["capital"]
        event["equity"] = stats["equity"]
        return self._commit(event)

    def _write_eod_if_needed(self, now: datetime) -> dict[str, Any] | None:
        if self.eod_written or self.position is not None:
            return None
        stats = self._capital_stats()
        event = {
            "event": "day_pnl",
            "mode": "paper",
            "strategy": "paper",
            "ts": now.isoformat(),
            "day": now.strftime("%Y-%m-%d"),
            "capital": stats["capital"],
            "trades": self.entries_today,
            "day_pnl": stats["day_pnl"],
            "day_pnl_pct": stats["day_pnl_pct"],
            "equity": stats["equity"],
        }
        self.eod_written = True
        return self._commit(event)

    def _commit(self, event: dict[str, Any]) -> dict[str, Any]:
        event = dict(event)
        event.setdefault("logged_at", ist_now())
        line = json.dumps(event, default=str)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.last_event = event
        kind = event.get("event")
        strat = event.get("strategy") or STRATEGY_LONG
        if kind == "open":
            if strat == STRATEGY_FLY:
                self._log.info(
                    "PAPER SELL iron fly atm=%s wings=%s/%s credit=%.2f qty=%s max_loss=%.2f",
                    event.get("atm"),
                    event.get("pe_wing_strike"),
                    event.get("ce_wing_strike"),
                    float(event.get("credit") or 0),
                    event.get("qty"),
                    float(event.get("max_loss") or 0),
                )
            else:
                self._log.info(
                    "PAPER BUY straddle atm=%s qty=%s ce=%.2f pe=%.2f SL=%.2f",
                    event.get("atm"),
                    event.get("qty"),
                    float(event.get("ce") or 0),
                    float(event.get("pe") or 0),
                    float(event.get("stop_loss") or 0),
                )
        elif kind == "day_pnl":
            self._log.info(
                "PAPER EOD capital=%.0f pnl=%.2f pct=%+.4f%% trades=%s equity=%.2f",
                float(event.get("capital") or 0),
                float(event.get("day_pnl") or 0),
                float(event.get("day_pnl_pct") or 0),
                event.get("trades"),
                float(event.get("equity") or 0),
            )
        else:
            known = event.get("pnl_known", True)
            self._log.info(
                "PAPER CLOSE %s reason=%s pct=%s pnl=%s gross=%s charges=%s day_pnl=%.2f known=%s",
                strat,
                event.get("reason"),
                event.get("pct"),
                event.get("pnl"),
                event.get("pnl_gross"),
                event.get("charges"),
                float(event.get("day_pnl") or 0),
                known,
            )
        return event
