"""Paper agent book — ATM CE/PE long or short from agent intents (never Kite orders)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger, ist_now
from atlas_lite.metrics import quote_ltp
from atlas_lite.minute_bars import hm_ge, hm_le

IST = ZoneInfo("Asia/Kolkata")

STRATEGY = "agent_paper"
CAPITAL = 200_000.0
LOTS = 1
DEFAULT_LOT_SIZE = 65
# 0 = unlimited agent paper opens per IST day.
MAX_ENTRIES_PER_DAY = 0
# Skip open auction chop; still trade the morning trend once tape settles.
ENTRY_AFTER = (9, 45)
# Scalp profile: allow entries later; still flat by SQUARE_OFF.
ENTRY_UNTIL = (15, 0)
SQUARE_OFF = (15, 14)
HOLD_MINUTES = 20
TARGET_PCT = 0.10
STOP_PCT = 0.06
# Trail arms after +TRAIL_ARM_PCT favorable move; stop follows best by TRAIL_PCT (min TRAIL_PTS).
TRAIL_ARM_PCT = 0.04
TRAIL_PCT = 0.03
TRAIL_PTS = 1.5
# Persist trail ledger only when stop moves by this much (frac of gap, min pts).
TRAIL_LEDGER_STEP_FRAC = 0.5
TRAIL_LEDGER_STEP_MIN = 0.5
# Longer gap between fills — cuts LLM flip churn after scratches/stops.
COOLDOWN_MIN = 3
# After a hard stop, block the same side+style so the LLM cannot immediately re-enter.
THESIS_COOLDOWN_MIN = 15
# After any losing close (agent_exit etc.), shorter same-thesis block.
LOSS_COOLDOWN_MIN = 10
# After ANY short close (win or loss), do not re-short the same side for this long.
# Stops chasing rich-premium shorts right after a winner into a bounce.
SHORT_REENTRY_COOLDOWN_MIN = 20
# Reject short open if option premium already spiked vs last same-side short exit.
SHORT_SPIKE_PCT = 0.05  # 5%
SHORT_SPIKE_LOOKBACK_MIN = 30
INTENT_MAX_AGE_S = 120
MAX_SPOT_DRIFT_PCT = 0.15  # reject if spot moved >0.15% since advise
DAILY_LOSS_STOP = -5000.0
# LLM agent_exit when estimated net ≥ this (winner) — else trail/target/stop/time.
MIN_AGENT_EXIT_NET = 140.0
# …or when estimated net ≤ −this (thesis broken / cut loser before hard stop).
MAX_AGENT_CUT_NET = 200.0
LESSONS_FILE = "agent_lessons.jsonl"


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_hhmm(name: str, default: tuple[int, int]) -> tuple[int, int]:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw or ":" not in raw:
        return default
    try:
        h_s, m_s = raw.split(":", 1)
        hour, minute = int(h_s), int(m_s)
    except ValueError:
        return default
    if hour > 23 or minute > 59:
        return default
    return hour, minute


def agent_max_entries_per_day() -> int:
    """Max agent paper opens per IST day (env ATLAS_LITE_AGENT_MAX_TRADES_DAY).

    ``0`` (default) means unlimited. Set a positive int to cap.
    """
    return max(0, _env_int("ATLAS_LITE_AGENT_MAX_TRADES_DAY", MAX_ENTRIES_PER_DAY))


def agent_daily_loss_stop() -> float:
    """Daily loss stop in rupees (negative). Env may be 5000 or -5000."""
    raw = _env_float("ATLAS_LITE_AGENT_DAILY_LOSS_STOP", abs(DAILY_LOSS_STOP))
    return -abs(raw)


def agent_cooldown_min() -> int:
    """Minutes after exit before a new agent entry (env ATLAS_LITE_AGENT_COOLDOWN_MIN)."""
    return max(0, _env_int("ATLAS_LITE_AGENT_COOLDOWN_MIN", COOLDOWN_MIN))


def agent_thesis_cooldown_min() -> int:
    """Minutes to block same side+style after a stop (env ATLAS_LITE_AGENT_THESIS_COOLDOWN_MIN)."""
    return max(0, _env_int("ATLAS_LITE_AGENT_THESIS_COOLDOWN_MIN", THESIS_COOLDOWN_MIN))


def agent_loss_cooldown_min() -> int:
    """Minutes to block same side+style after any losing close."""
    return max(0, _env_int("ATLAS_LITE_AGENT_LOSS_COOLDOWN_MIN", LOSS_COOLDOWN_MIN))


def agent_short_reentry_cooldown_min() -> int:
    """Minutes to block re-shorting the same side after any short close."""
    return max(
        0, _env_int("ATLAS_LITE_AGENT_SHORT_REENTRY_COOLDOWN_MIN", SHORT_REENTRY_COOLDOWN_MIN)
    )


def agent_entry_after() -> tuple[int, int]:
    """Earliest IST HH:MM for new agent entries (env ATLAS_LITE_AGENT_ENTRY_AFTER)."""
    return _env_hhmm("ATLAS_LITE_AGENT_ENTRY_AFTER", ENTRY_AFTER)


def agent_entry_until() -> tuple[int, int]:
    """Last IST HH:MM for new agent entries (env ATLAS_LITE_AGENT_ENTRY_UNTIL)."""
    return _env_hhmm("ATLAS_LITE_AGENT_ENTRY_UNTIL", ENTRY_UNTIL)


def agent_min_exit_net() -> float:
    """Min estimated net ₹ before LLM take-profit exit is accepted (env ATLAS_LITE_AGENT_MIN_EXIT_NET)."""
    return max(0.0, _env_float("ATLAS_LITE_AGENT_MIN_EXIT_NET", MIN_AGENT_EXIT_NET))


def agent_max_cut_net() -> float:
    """Allow LLM cut when estimated net ≤ −this ₹ (env ATLAS_LITE_AGENT_MAX_CUT_NET)."""
    return max(0.0, _env_float("ATLAS_LITE_AGENT_MAX_CUT_NET", MAX_AGENT_CUT_NET))


Side = Literal["ce", "pe"]
Style = Literal["long", "short"]


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def paper_agent_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_AGENT", "1").strip().lower()
    return raw in ("1", "true", "yes")


def in_agent_entry_window(now: datetime) -> bool:
    after = agent_entry_after()
    until = agent_entry_until()
    return hm_ge(now, after) and hm_le(now, until) and not hm_ge(now, SQUARE_OFF)


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _weekday(now: datetime) -> bool:
    return now.weekday() < 5


def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _normalize_style(raw: str | None) -> Style | None:
    text = str(raw or "long").strip().lower()
    if text in ("long", "buy", "l"):
        return "long"
    if text in ("short", "sell", "s"):
        return "short"
    return None


def trail_gap_px(entry: float, *, trail_pct: float = TRAIL_PCT, trail_pts: float = TRAIL_PTS) -> float:
    return round(max(float(entry) * float(trail_pct), float(trail_pts)), 2)


def trail_ledger_step_px(
    entry: float,
    *,
    trail_pct: float = TRAIL_PCT,
    trail_pts: float = TRAIL_PTS,
    step_frac: float = TRAIL_LEDGER_STEP_FRAC,
    step_min: float = TRAIL_LEDGER_STEP_MIN,
) -> float:
    gap = trail_gap_px(entry, trail_pct=trail_pct, trail_pts=trail_pts)
    return round(max(float(gap) * float(step_frac), float(step_min)), 2)


def initial_target_stop(entry: float, style: Style, *, target_pct: float, stop_pct: float) -> tuple[float, float]:
    entry = float(entry)
    if style == "short":
        target = round(entry * (1.0 - float(target_pct)), 2)
        stop = round(entry * (1.0 + float(stop_pct)), 2)
    else:
        target = round(entry * (1.0 + float(target_pct)), 2)
        stop = round(entry * (1.0 - float(stop_pct)), 2)
    return target, stop


def _open_legs(style: Style, px: float, qty: int) -> list[tuple[float, int, str]]:
    action = "sell" if style == "short" else "buy"
    return [(float(px), qty, action)]


def _close_legs(style: Style, px: float, qty: int) -> list[tuple[float, int, str]]:
    action = "buy" if style == "short" else "sell"
    return [(float(px), qty, action)]


def _pnl_gross(style: Style, entry: float, exit_px: float, qty: int) -> float:
    if style == "short":
        return round((float(entry) - float(exit_px)) * int(qty), 2)
    return round((float(exit_px) - float(entry)) * int(qty), 2)


@dataclass
class AgentPosition:
    day: str
    atm: int
    side: Side
    style: Style
    symbol: str
    ce_symbol: str
    pe_symbol: str
    qty: int
    lots: int
    entry: float
    target: float
    stop: float
    hold_until: str
    opened_at: str
    charges_open: float
    reason: str = ""
    trail_armed: bool = False
    best_px: float = 0.0  # peak for long / trough for short
    trail_ledger_stop: float | None = None  # last stop written to ledger


@dataclass
class PendingIntent:
    side: Side
    style: Style
    ts: datetime
    spot: float | None
    reason: str
    want_exit: bool = False


@dataclass
class PaperAgent:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = LOTS
    capital: float = CAPITAL
    max_entries_per_day: int = field(default_factory=agent_max_entries_per_day)
    hold_minutes: int = HOLD_MINUTES
    target_pct: float = TARGET_PCT
    stop_pct: float = STOP_PCT
    trail_arm_pct: float = TRAIL_ARM_PCT
    trail_pct: float = TRAIL_PCT
    trail_pts: float = TRAIL_PTS
    trail_ledger_step_frac: float = TRAIL_LEDGER_STEP_FRAC
    trail_ledger_step_min: float = TRAIL_LEDGER_STEP_MIN
    cooldown_min: int = field(default_factory=agent_cooldown_min)
    thesis_cooldown_min: int = field(default_factory=agent_thesis_cooldown_min)
    loss_cooldown_min: int = field(default_factory=agent_loss_cooldown_min)
    short_reentry_cooldown_min: int = field(default_factory=agent_short_reentry_cooldown_min)
    short_spike_pct: float = SHORT_SPIKE_PCT
    short_spike_lookback_min: int = SHORT_SPIKE_LOOKBACK_MIN
    min_exit_net: float = field(default_factory=agent_min_exit_net)
    max_cut_net: float = field(default_factory=agent_max_cut_net)
    intent_max_age_s: int = INTENT_MAX_AGE_S
    daily_loss_stop: float = field(default_factory=agent_daily_loss_stop)
    position: AgentPosition | None = None
    pending: PendingIntent | None = None
    traded_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    eod_written: bool = False
    last_event: dict[str, Any] | None = None
    last_exit_at: datetime | None = None
    last_reject: str | None = None
    # Which setup last_reject=same_thesis_cooldown refers to (avoid sticky UI when another key is active).
    last_reject_thesis_key: str | None = None
    # Keyed by ``{style}_{side}`` so CE/PE (or long/short) blocks do not overwrite each other.
    thesis_blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_agent"), repr=False)

    @staticmethod
    def _thesis_key(side: str, style: str) -> str:
        s = "pe" if str(side or "").lower() == "pe" else "ce"
        st = _normalize_style(str(style or "long")) or "long"
        return f"{st}_{s}"

    @property
    def lessons_path(self) -> Path:
        return self.path.parent / LESSONS_FILE

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restore()

    def _restore(self) -> None:
        if not self.path.is_file():
            return
        last_open: dict[str, Any] | None = None
        last_trail: dict[str, Any] | None = None
        day = ""
        day_pnl = 0.0
        entries = 0
        eod_written = False
        last_exit_at: datetime | None = None
        last_any: dict[str, Any] | None = None
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
                last_trail = None
                last_exit_at = None
                eod_written = False
            if kind == "open":
                last_open = ev
                last_trail = None
                entries += 1
            elif kind == "trail":
                if last_open is not None:
                    last_trail = ev
            elif kind == "close":
                last_open = None
                last_trail = None
                if entries == 0:
                    entries = 1
                if ev.get("day_pnl") is not None:
                    day_pnl = float(ev["day_pnl"])
                last_exit_at = _parse_ts(str(ev.get("ts") or ""))
            elif kind == "day_pnl":
                if ev.get("day_pnl") is not None:
                    day_pnl = float(ev["day_pnl"])
                eod_written = True
        self.last_event = last_any
        self.traded_day = day
        self.entries_today = entries
        self.day_pnl = round(day_pnl, 2)
        self.eod_written = bool(eod_written)
        self.last_exit_at = last_exit_at
        self._restore_thesis_blocks()
        if last_open:
            entry = float(last_open.get("entry") or 0)
            style = _normalize_style(str(last_open.get("style") or "long")) or "long"
            target, stop = initial_target_stop(
                entry, style, target_pct=self.target_pct, stop_pct=self.stop_pct
            )
            best = float(last_open.get("best_px") or entry)
            trail_armed = bool(last_open.get("trail_armed"))
            stop_px = float(last_open.get("stop") or stop)
            if last_trail:
                if last_trail.get("best_px") is not None:
                    best = float(last_trail["best_px"])
                if last_trail.get("stop") is not None:
                    stop_px = float(last_trail["stop"])
                trail_armed = bool(last_trail.get("trail_armed", trail_armed))
            self.position = AgentPosition(
                day=str(last_open["day"]),
                atm=int(last_open["atm"]),
                side="pe" if last_open.get("side") == "pe" else "ce",
                style=style,
                symbol=str(last_open.get("symbol") or ""),
                ce_symbol=str(last_open["ce_symbol"]),
                pe_symbol=str(last_open["pe_symbol"]),
                qty=int(last_open.get("qty") or self.lots * self.lot_size),
                lots=int(last_open.get("lots") or self.lots),
                entry=entry,
                target=float(last_open.get("target") or target),
                stop=stop_px,
                hold_until=str(last_open.get("hold_until") or ""),
                opened_at=str(last_open.get("ts") or ""),
                charges_open=float(last_open.get("charges") or 0.0),
                reason=str(last_open.get("reason") or ""),
                trail_armed=trail_armed,
                best_px=best,
                trail_ledger_stop=stop_px if last_trail else None,
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
            self._log.warning("paper agent ledger write failed: %s", exc)
            return None
        self.last_event = event
        return event

    def _book_quotes(
        self, book: QuoteSource | None, ce_sym: str, pe_sym: str
    ) -> tuple[float | None, float | None]:
        if book is None:
            return None, None
        return quote_ltp(book.get(ce_sym)), quote_ltp(book.get(pe_sym))

    def _in_cooldown(self, now: datetime) -> bool:
        if self.last_exit_at is None:
            return False
        return now < self.last_exit_at + timedelta(minutes=int(self.cooldown_min))

    def _mark_net_pnl(self, pos: AgentPosition, mark: float) -> tuple[float, float, float]:
        """Return (gross, estimated_roundtrip_charges, net) at mark."""
        mark = float(mark)
        gross = float(_pnl_gross(pos.style, pos.entry, mark, pos.qty))
        close_ch = float(kite_nfo_charges(_close_legs(pos.style, mark, pos.qty))["total"])
        charges = round(float(pos.charges_open) + close_ch, 2)
        return gross, charges, round(gross - charges, 2)

    def _agent_exit_allowed(self, pos: AgentPosition, mark: float | None) -> tuple[bool, str | None]:
        """LLM exits outside the scratch band; code target/stop/trail/time still always run.

        Allowed when estimated net ≥ min_exit_net (take profit past fees) OR
        net ≤ −max_cut_net (cut a real loser / broken thesis before hard stop).
        Blocked in between (e.g. −199 … +139) so micro scratches cannot churn.
        """
        if mark is None or mark <= 0:
            return False, "missing_mark"
        _gross, _charges, net = self._mark_net_pnl(pos, mark)
        floor = float(self.min_exit_net)
        cut = -abs(float(self.max_cut_net))
        if net >= floor or net <= cut:
            return True, None
        return False, "fee_floor"

    def _clear_thesis_block(self, key: str | None = None) -> None:
        if key is None:
            self.thesis_blocks.clear()
            if self.last_reject == "same_thesis_cooldown":
                self.last_reject = None
                self.last_reject_thesis_key = None
            return
        self.thesis_blocks.pop(key, None)
        if (
            self.last_reject == "same_thesis_cooldown"
            and self.last_reject_thesis_key == key
        ):
            self.last_reject = None
            self.last_reject_thesis_key = None

    def _release_thesis_block(
        self,
        key: str,
        *,
        now: datetime,
        reason: str = "favorable_flip",
    ) -> None:
        """Clear one block and persist so restart does not revive it."""
        block = self.thesis_blocks.get(key)
        if not isinstance(block, dict):
            return
        side = str(block.get("side") or "")
        style = str(block.get("style") or "long")
        self._clear_thesis_block(key)
        self._append_lesson(
            {
                "ts": now.isoformat(),
                "day": now.strftime("%Y-%m-%d"),
                "kind": "release",
                "side": side,
                "style": style,
                "spot": block.get("spot"),
                "until": now.isoformat(),
                "reason": str(reason or "favorable_flip")[:40],
                "lesson": f"released {style} {side} early ({reason})",
            }
        )

    def _upsert_thesis_block(
        self,
        *,
        side: str,
        style: str,
        until: datetime,
        spot: float | None,
        reason: str,
    ) -> dict[str, Any]:
        key = self._thesis_key(side, style)
        prev = self.thesis_blocks.get(key)
        # Do not shorten an active longer block on the same key.
        if (
            isinstance(prev, dict)
            and isinstance(prev.get("until"), datetime)
            and prev["until"] > until
        ):
            return prev
        s = "pe" if str(side).lower() == "pe" else "ce"
        st = _normalize_style(str(style or "long")) or "long"
        row = {
            "side": s,
            "style": st,
            "until": until,
            "spot": spot,
            "reason": str(reason or "loss")[:40],
        }
        self.thesis_blocks[key] = row
        return row

    @staticmethod
    def _spot_chg_pct_vs_block(spot: float | None, block_spot: float | None) -> float | None:
        if spot is None or block_spot is None or float(block_spot) <= 0:
            return None
        return (float(spot) - float(block_spot)) / float(block_spot) * 100.0

    def _favorable_thesis_flip(self, block: dict[str, Any], spot: float | None) -> bool:
        """True only when spot moved enough in favor of that blocked thesis (not adverse)."""
        chg = self._spot_chg_pct_vs_block(spot, block.get("spot"))
        side = str(block.get("side") or "")
        if chg is None or side not in ("ce", "pe"):
            return False
        style = str(block.get("style") or "long")
        # long CE / short PE → need spot up; long PE / short CE → need spot down.
        wants_up = (side == "ce" and style == "long") or (side == "pe" and style == "short")
        if wants_up:
            return chg > MAX_SPOT_DRIFT_PCT
        return chg < -MAX_SPOT_DRIFT_PCT

    def _refresh_thesis_block(
        self, *, now: datetime | None = None, spot: float | None = None
    ) -> None:
        """Drop expired/flipped thesis blocks (does not touch unrelated last_reject)."""
        now = now or datetime.now(IST)
        if not self.thesis_blocks:
            return
        for key, block in list(self.thesis_blocks.items()):
            until = block.get("until")
            if not isinstance(until, datetime) or now >= until:
                self._clear_thesis_block(key)
                continue
            if self._favorable_thesis_flip(block, spot):
                self._release_thesis_block(key, now=now, reason="favorable_flip")

    def _restore_thesis_blocks(self, *, now: datetime | None = None) -> None:
        """Rebuild active blocks from lessons (stop/loss), fallback to ledger stops."""
        now = now or datetime.now(IST)
        self.thesis_blocks = {}
        released_at: dict[str, datetime] = {}
        path = self.lessons_path
        lesson_rows: list[dict[str, Any]] = []
        if path.is_file():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                lesson_rows.append(ev)
                if str(ev.get("kind") or "") != "release":
                    continue
                side = "pe" if ev.get("side") == "pe" else "ce"
                style = _normalize_style(str(ev.get("style") or "long")) or "long"
                key = self._thesis_key(side, style)
                ts = _parse_ts(str(ev.get("ts") or ev.get("until") or ""))
                if ts is None:
                    continue
                prev = released_at.get(key)
                if prev is None or ts > prev:
                    released_at[key] = ts
            for ev in lesson_rows:
                kind = str(ev.get("kind") or "")
                if kind not in ("stop", "loss", "short_reentry"):
                    continue
                closed_at = _parse_ts(str(ev.get("ts") or ""))
                until = _parse_ts(str(ev.get("until") or ""))
                if until is None:
                    if closed_at is None:
                        continue
                    if kind == "stop":
                        mins = int(self.thesis_cooldown_min)
                    elif kind == "short_reentry":
                        mins = int(self.short_reentry_cooldown_min)
                    else:
                        mins = int(self.loss_cooldown_min)
                    until = closed_at + timedelta(minutes=mins)
                if until <= now:
                    continue
                side = "pe" if ev.get("side") == "pe" else "ce"
                style = _normalize_style(str(ev.get("style") or "long")) or "long"
                key = self._thesis_key(side, style)
                rel = released_at.get(key)
                if rel is not None and (closed_at is None or rel >= closed_at):
                    continue
                self._upsert_thesis_block(
                    side=side,
                    style=style,
                    until=until,
                    spot=_f(ev.get("spot")),
                    reason=kind,
                )
        # Ledger fallback for keys missing from lessons (or lessons file absent).
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("strategy") != STRATEGY or ev.get("event") != "close":
                continue
            pnl = _f(ev.get("pnl"))
            if pnl is None or pnl >= 0:
                continue
            reason = str(ev.get("reason") or "")
            closed_at = _parse_ts(str(ev.get("ts") or ""))
            if closed_at is None:
                continue
            mins = (
                int(self.thesis_cooldown_min)
                if reason == "stop"
                else int(self.loss_cooldown_min)
            )
            if mins <= 0:
                continue
            until = closed_at + timedelta(minutes=mins)
            if until <= now:
                continue
            side = "pe" if ev.get("side") == "pe" else "ce"
            style = _normalize_style(str(ev.get("style") or "long")) or "long"
            key = self._thesis_key(side, style)
            if key in self.thesis_blocks:
                continue
            rel = released_at.get(key)
            if rel is not None and rel >= closed_at:
                continue
            self._upsert_thesis_block(
                side=side,
                style=style,
                until=until,
                spot=_f(ev.get("spot")),
                reason=reason or "loss",
            )

    def _last_reject_still_applies(
        self, *, now: datetime, mark: float | None = None
    ) -> bool:
        """True when last_reject names a condition that is still active."""
        reason = self.last_reject
        if not reason:
            return False
        if reason == "cooldown":
            return self._in_cooldown(now)
        if reason == "outside_session":
            return not in_agent_entry_window(now)
        if reason == "daily_loss_stop":
            return self.day_pnl <= float(self.daily_loss_stop)
        if reason == "same_thesis_cooldown":
            key = self.last_reject_thesis_key
            if key:
                return key in self.thesis_blocks
            return bool(self.thesis_blocks)
        if reason == "already_open":
            return self.position is not None
        if reason == "max_trades_day":
            return int(self.max_entries_per_day) > 0 and self.entries_today >= int(
                self.max_entries_per_day
            )
        if reason == "day_roll_pending":
            day = now.strftime("%Y-%m-%d")
            return bool(self.traded_day and self.traded_day != day and self.position is None)
        if reason == "fee_floor":
            if self.position is None:
                return False
            # Clear once mark recovers above the floor so the LLM can retry exit.
            if mark is not None and mark > 0:
                ok, _ = self._agent_exit_allowed(self.position, mark)
                return not ok
            return True
        if reason == "missing_mark":
            if self.position is None:
                return False
            return mark is None or mark <= 0
        # One-shot rejects (stale_intent, spot_drift, missing_*, invalid_*) stay until
        # the next propose/fill overwrites them.
        return True

    def _scrub_stale_last_reject(
        self,
        *,
        now: datetime | None = None,
        spot: float | None = None,
        mark: float | None = None,
    ) -> None:
        """Clear last_reject when its named condition no longer holds."""
        now = now or datetime.now(IST)
        self._refresh_thesis_block(now=now, spot=spot)
        if self.last_reject and not self._last_reject_still_applies(now=now, mark=mark):
            self.last_reject = None

    def _thesis_blocked(
        self,
        side: str,
        style: Style,
        now: datetime,
        spot: float | None,
    ) -> bool:
        self._scrub_stale_last_reject(now=now, spot=spot)
        return self._thesis_key(side, style) in self.thesis_blocks

    def recent_closes(self, *, limit: int = 10) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("strategy") != STRATEGY or ev.get("event") != "close":
                continue
            out.append(
                {
                    "ts": ev.get("ts"),
                    "day": ev.get("day"),
                    "side": ev.get("side"),
                    "style": ev.get("style") or "long",
                    "entry": ev.get("entry"),
                    "exit": ev.get("exit"),
                    "pnl": ev.get("pnl"),
                    "reason": ev.get("reason"),
                    "spot": ev.get("spot"),
                }
            )
            if len(out) >= max(1, int(limit)):
                break
        out.reverse()
        return out

    def lessons_today(self, *, limit: int = 10, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(IST)
        day = now.strftime("%Y-%m-%d")
        path = self.lessons_path
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(ev.get("day") or "") != day:
                continue
            out.append(ev)
            if len(out) >= max(1, int(limit)):
                break
        out.reverse()
        return out

    def thesis_blocks_snapshot(
        self, *, now: datetime | None = None, spot: float | None = None
    ) -> list[dict[str, Any]]:
        now = now or datetime.now(IST)
        self._scrub_stale_last_reject(now=now, spot=spot)
        out: list[dict[str, Any]] = []
        for key in sorted(self.thesis_blocks):
            block = self.thesis_blocks[key]
            until = block.get("until")
            if not isinstance(until, datetime):
                continue
            out.append(
                {
                    "side": block.get("side"),
                    "style": block.get("style") or "long",
                    "until": until.isoformat(),
                    "spot": block.get("spot"),
                    "reason": block.get("reason") or "stop",
                    "minutes_left": max(0, int((until - now).total_seconds() // 60)),
                }
            )
        return out

    def thesis_block_snapshot(
        self,
        *,
        now: datetime | None = None,
        spot: float | None = None,
        side: str | None = None,
        style: str | None = None,
    ) -> dict[str, Any] | None:
        blocks = self.thesis_blocks_snapshot(now=now, spot=spot)
        if not blocks:
            return None
        if side is not None:
            key = self._thesis_key(side, style or "long")
            for row in blocks:
                if self._thesis_key(str(row.get("side")), str(row.get("style"))) == key:
                    return row
            return None
        return blocks[0]

    def _append_lesson(self, lesson: dict[str, Any]) -> None:
        row = dict(lesson)
        row.setdefault("logged_at", ist_now())
        try:
            self.lessons_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lessons_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError as exc:
            self._log.warning("agent lesson write failed: %s", exc)

    def _record_stop_lesson(
        self,
        *,
        now: datetime,
        pos: AgentPosition,
        exit_px: float,
        pnl: float | None,
        spot: float | None,
    ) -> None:
        until = now + timedelta(minutes=int(self.thesis_cooldown_min))
        self._upsert_thesis_block(
            side=pos.side,
            style=pos.style,
            until=until,
            spot=spot,
            reason="stop",
        )
        lesson = (
            f"stop on {pos.style} {pos.side.upper()} @ {exit_px} (entry {pos.entry}, pnl {pnl}). "
            f"Do not re-enter the same side+style for {self.thesis_cooldown_min}m "
            f"unless spot flips in favor of that thesis."
        )
        self._append_lesson(
            {
                "ts": now.isoformat(),
                "day": now.strftime("%Y-%m-%d"),
                "kind": "stop",
                "side": pos.side,
                "style": pos.style,
                "entry": pos.entry,
                "exit": exit_px,
                "pnl": pnl,
                "spot": spot,
                "until": until.isoformat(),
                "lesson": lesson,
            }
        )

    def _record_loss_cooldown(
        self,
        *,
        now: datetime,
        pos: AgentPosition,
        exit_px: float,
        pnl: float | None,
        spot: float | None,
        reason: str,
    ) -> None:
        """Block same side+style after any losing close (not only hard stops)."""
        mins = int(self.loss_cooldown_min)
        if mins <= 0:
            return
        until = now + timedelta(minutes=mins)
        row = self._upsert_thesis_block(
            side=pos.side,
            style=pos.style,
            until=until,
            spot=spot,
            reason=str(reason or "loss")[:40],
        )
        lesson = (
            f"loss ({row.get('reason')}) on {pos.style} {pos.side.upper()} @ {exit_px} "
            f"(entry {pos.entry}, pnl {pnl}). Same side+style blocked {mins}m "
            f"unless spot flips in favor of that thesis."
        )
        self._append_lesson(
            {
                "ts": now.isoformat(),
                "day": now.strftime("%Y-%m-%d"),
                "kind": "loss",
                "side": pos.side,
                "style": pos.style,
                "entry": pos.entry,
                "exit": exit_px,
                "pnl": pnl,
                "spot": spot,
                "until": (
                    row["until"].isoformat()
                    if isinstance(row.get("until"), datetime)
                    else until.isoformat()
                ),
                "lesson": lesson,
            }
        )

    def propose_entry(
        self,
        *,
        side: str,
        style: str = "long",
        reason: str = "",
        spot: float | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(IST)
        day = now.strftime("%Y-%m-%d")
        if self.traded_day and self.traded_day != day and self.position is None:
            sealed = self._seal_then_roll(now, day)
            if sealed is None and self.traded_day != day:
                # Prior day_pnl write failed — do not wipe counters yet.
                self.last_reject = "day_roll_pending"
                return {"ok": False, "rejected": "day_roll_pending"}
        side_n = str(side or "").strip().lower()
        if side_n not in ("ce", "pe"):
            self.last_reject = "invalid_side"
            return {"ok": False, "rejected": "invalid_side"}
        style_n = _normalize_style(style)
        if style_n is None:
            self.last_reject = "invalid_style"
            return {"ok": False, "rejected": "invalid_style"}
        if self.position is not None:
            self.last_reject = "already_open"
            return {"ok": False, "rejected": "already_open"}
        if self.day_pnl <= float(self.daily_loss_stop):
            self.last_reject = "daily_loss_stop"
            return {"ok": False, "rejected": "daily_loss_stop"}
        if int(self.max_entries_per_day) > 0 and self.entries_today >= int(self.max_entries_per_day):
            self.last_reject = "max_trades_day"
            return {"ok": False, "rejected": "max_trades_day"}
        if not in_agent_entry_window(now):
            self.last_reject = "outside_session"
            return {"ok": False, "rejected": "outside_session"}
        if self._in_cooldown(now):
            self.last_reject = "cooldown"
            return {"ok": False, "rejected": "cooldown"}
        if self._thesis_blocked(side_n, style_n, now, _f(spot)):
            self.last_reject = "same_thesis_cooldown"
            self.last_reject_thesis_key = self._thesis_key(side_n, style_n)
            block = (
                self.thesis_block_snapshot(
                    now=now, spot=_f(spot), side=side_n, style=style_n
                )
                or {}
            )
            return {
                "ok": False,
                "rejected": "same_thesis_cooldown",
                "thesis_block": block,
            }
        self.pending = PendingIntent(
            side=side_n,  # type: ignore[arg-type]
            style=style_n,
            ts=now,
            spot=_f(spot),
            reason=(reason or "")[:300],
            want_exit=False,
        )
        self.last_reject = None
        self.last_reject_thesis_key = None
        return {
            "ok": True,
            "pending": True,
            "side": side_n,
            "style": style_n,
            "expires_s": self.intent_max_age_s,
            "reason": self.pending.reason,
        }

    def propose_exit(
        self,
        *,
        reason: str = "",
        now: datetime | None = None,
        mark: float | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(IST)
        if self.position is None:
            return {"ok": False, "rejected": "flat"}
        ok, rejected = self._agent_exit_allowed(self.position, mark)
        if not ok:
            self.last_reject = rejected
            detail: dict[str, Any] = {"ok": False, "rejected": rejected}
            if mark is not None and mark > 0:
                gross, charges, net = self._mark_net_pnl(self.position, mark)
                detail.update(
                    {
                        "mark": round(float(mark), 2),
                        "gross": gross,
                        "charges_est": charges,
                        "net": net,
                        "min_exit_net": float(self.min_exit_net),
                        "max_cut_net": float(self.max_cut_net),
                    }
                )
            return detail
        self.pending = PendingIntent(
            side=self.position.side,
            style=self.position.style,
            ts=now,
            spot=None,
            reason=(reason or "agent_exit")[:300],
            want_exit=True,
        )
        self.last_reject = None
        return {"ok": True, "pending_exit": True, "reason": self.pending.reason}

    def clear_pending(self) -> None:
        self.pending = None

    def snapshot(
        self,
        book: QuoteSource | None = None,
        *,
        spot: float | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(IST)
        pos = self.position
        mark = None
        if pos and book is not None:
            ce, pe = self._book_quotes(book, pos.ce_symbol, pos.pe_symbol)
            mark = ce if pos.side == "ce" else pe
        self._scrub_stale_last_reject(now=now, spot=spot, mark=mark)
        pos_body: dict[str, Any] | None = None
        if pos is not None:
            pos_body = {
                "side": pos.side,
                "style": pos.style,
                "symbol": pos.symbol,
                "atm": pos.atm,
                "entry": pos.entry,
                "target": pos.target,
                "stop": pos.stop,
                "trail_armed": pos.trail_armed,
                "best_px": pos.best_px,
                "mark": mark,
                "hold_until": pos.hold_until,
                "reason": pos.reason,
                "gross": None,
                "charges_est": None,
                "net": None,
                "exit_allowed": None,
            }
            if mark is not None and mark > 0:
                gross, charges, net = self._mark_net_pnl(pos, mark)
                ok_exit, _ = self._agent_exit_allowed(pos, mark)
                pos_body["gross"] = gross
                pos_body["charges_est"] = charges
                pos_body["net"] = net
                pos_body["exit_allowed"] = ok_exit
        return {
            "ok": True,
            "mode": "paper",
            "live_orders": False,
            "book": STRATEGY,
            "position": pos_body,
            "pending": None
            if self.pending is None
            else {
                "side": self.pending.side,
                "style": self.pending.style,
                "want_exit": self.pending.want_exit,
                "ts": self.pending.ts.isoformat(),
                "spot": self.pending.spot,
                "reason": self.pending.reason,
            },
            "entries_today": self.entries_today,
            "day_pnl": self.day_pnl,
            "last_reject": self.last_reject,
            "max_entries_per_day": self.max_entries_per_day,
            "max_entries_unlimited": int(self.max_entries_per_day) <= 0,
            "daily_loss_stop": self.daily_loss_stop,
            "trail_arm_pct": self.trail_arm_pct,
            "trail_pct": self.trail_pct,
            "thesis_cooldown_min": self.thesis_cooldown_min,
            "loss_cooldown_min": self.loss_cooldown_min,
            "min_exit_net": float(self.min_exit_net),
            "max_cut_net": float(self.max_cut_net),
            "thesis_block": self.thesis_block_snapshot(now=now, spot=spot),
            "thesis_blocks": self.thesis_blocks_snapshot(now=now, spot=spot),
            # Enough history for daily_review + multi-day setup memory (~10 sessions).
            "recent_closes": self.recent_closes(limit=120),
            "lessons_today": self.lessons_today(limit=20, now=now),
        }

    def on_frame(
        self,
        *,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource | None,
        ce_symbol: str | None,
        pe_symbol: str | None,
        atm: int | None,
        allow_new_entries: bool = True,
        spot: float | None = None,
    ) -> dict[str, Any] | None:
        day = now.strftime("%Y-%m-%d")
        if not _weekday(now):
            if self.position is not None:
                return self._flatten_stale(now, book, "weekend")
            self.pending = None
            return None
        if self.position is not None and self.position.day != day:
            return self._flatten_stale(now, book, "session_gap")
        if self.traded_day and self.traded_day != day and self.position is None:
            sealed = self._seal_then_roll(now, day)
            if sealed is not None:
                return sealed
            if self.traded_day != day:
                return None
        if self.position is not None:
            pos = self.position
            ce_m, pe_m = self._book_quotes(book, pos.ce_symbol, pos.pe_symbol)
            mark = ce_m if pos.side == "ce" else pe_m
            self._scrub_stale_last_reject(now=now, spot=spot, mark=mark)
            if self.pending and self.pending.want_exit:
                ok, rejected = self._agent_exit_allowed(pos, mark)
                if not ok:
                    self.pending = None
                    self.last_reject = rejected
                    closed = self._maybe_exit(now, book, spot=spot)
                    if hm_ge(now, SQUARE_OFF):
                        return closed or self._write_eod_if_needed(now)
                    return closed
                self.pending = None
                return self._close(now, book, "agent_exit", spot=spot)
            closed = self._maybe_exit(now, book, spot=spot)
            if hm_ge(now, SQUARE_OFF):
                return closed or self._write_eod_if_needed(now)
            return closed
        self._scrub_stale_last_reject(now=now, spot=spot)
        if hm_ge(now, SQUARE_OFF):
            self.pending = None
            return self._write_eod_if_needed(now)
        if not allow_new_entries:
            self.pending = None
            return None
        if self.pending is None or self.pending.want_exit:
            return None
        return self._try_open_from_pending(
            now, feed, book, ce_symbol, pe_symbol, atm, spot=spot
        )

    def _roll_to_day(self, day: str) -> None:
        self.traded_day = day
        self.entries_today = 0
        self.day_pnl = 0.0
        self.eod_written = False
        self.pending = None
        self.last_exit_at = None
        self._clear_thesis_block()

    def _seal_then_roll(self, now: datetime, day: str) -> dict[str, Any] | None:
        """Persist prior-day day_pnl before counters reset (process may have missed 15:14)."""
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
                "equity": round(self.capital + self.day_pnl, 2),
            }
        )
        if event:
            self.eod_written = True
        return event

    def _flatten_stale(
        self,
        now: datetime,
        book: QuoteSource | None,
        reason: str,
    ) -> dict[str, Any] | None:
        """Close a leftover position onto today's book; seal prior day first if needed."""
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
            and self._write_eod_if_needed(now, allow_open=True) is None
        ):
            return None
        return self._close(now, book, reason, book_day=book_day)

    def _try_open_from_pending(
        self,
        now: datetime,
        feed: dict[str, Any],
        book: QuoteSource | None,
        ce_symbol: str | None,
        pe_symbol: str | None,
        atm: int | None,
        *,
        spot: float | None,
    ) -> dict[str, Any] | None:
        intent = self.pending
        if intent is None:
            return None
        age = (now - intent.ts).total_seconds()
        if age > float(self.intent_max_age_s):
            self.last_reject = "stale_intent"
            self.pending = None
            return None
        if intent.spot is not None and spot is not None and intent.spot > 0:
            drift = abs(spot - intent.spot) / intent.spot * 100.0
            if drift > MAX_SPOT_DRIFT_PCT:
                self.last_reject = "spot_drift"
                self.pending = None
                return None
        if int(self.max_entries_per_day) > 0 and self.entries_today >= int(self.max_entries_per_day):
            self.last_reject = "max_trades_day"
            self.pending = None
            return None
        if self._in_cooldown(now):
            self.last_reject = "cooldown"
            return None
        if self.day_pnl <= float(self.daily_loss_stop):
            self.last_reject = "daily_loss_stop"
            self.pending = None
            return None
        if not in_agent_entry_window(now):
            self.last_reject = "outside_session"
            self.pending = None
            return None
        ce_sym = ce_symbol or str(feed.get("ce_symbol") or "")
        pe_sym = pe_symbol or str(feed.get("pe_symbol") or "")
        if not ce_sym or not pe_sym or atm is None:
            self.last_reject = "missing_symbols"
            return None
        ce, pe = self._book_quotes(book, ce_sym, pe_sym)
        if ce is None:
            ce = _f(feed.get("ce"))
        if pe is None:
            pe = _f(feed.get("pe"))
        px = ce if intent.side == "ce" else pe
        if px is None or px <= 0:
            self.last_reject = "missing_ltp"
            return None
        qty = int(self.lots) * int(self.lot_size)
        entry = round(float(px), 2)
        style = intent.style
        target, stop = initial_target_stop(
            entry, style, target_pct=self.target_pct, stop_pct=self.stop_pct
        )
        hold_until = (now + timedelta(minutes=int(self.hold_minutes))).isoformat()
        charges_open = float(kite_nfo_charges(_open_legs(style, entry, qty))["total"])
        pos = AgentPosition(
            day=now.strftime("%Y-%m-%d"),
            atm=int(atm),
            side=intent.side,
            style=style,
            symbol=ce_sym if intent.side == "ce" else pe_sym,
            ce_symbol=ce_sym,
            pe_symbol=pe_sym,
            qty=qty,
            lots=int(self.lots),
            entry=entry,
            target=target,
            stop=stop,
            hold_until=hold_until,
            opened_at=now.isoformat(),
            charges_open=round(charges_open, 2),
            reason=intent.reason,
            trail_armed=False,
            best_px=entry,
        )
        event = self._append(
            {
                "event": "open",
                "ts": now.isoformat(),
                "day": pos.day,
                "atm": pos.atm,
                "side": pos.side,
                "style": pos.style,
                "symbol": pos.symbol,
                "ce_symbol": pos.ce_symbol,
                "pe_symbol": pos.pe_symbol,
                "qty": pos.qty,
                "lots": pos.lots,
                "lot_size": self.lot_size,
                "entry": pos.entry,
                "target": pos.target,
                "stop": pos.stop,
                "trail_armed": False,
                "best_px": pos.best_px,
                "hold_until": pos.hold_until,
                "charges": pos.charges_open,
                "reason": pos.reason,
                "gates": "agent",
                "spot": spot,
            }
        )
        if event is None:
            return None
        self.position = pos
        self.entries_today += 1
        self.traded_day = pos.day
        self.pending = None
        self.last_reject = None
        return event

    def _hold_due(self, now: datetime, pos: AgentPosition) -> bool:
        until = _parse_ts(pos.hold_until)
        if until is None:
            opened = _parse_ts(pos.opened_at)
            if opened is None:
                return False
            until = opened + timedelta(minutes=int(self.hold_minutes))
        return now >= until

    def _update_trail(self, pos: AgentPosition, px: float, *, now: datetime) -> None:
        """Ratchet stop after a favorable move; never loosen the initial hard stop.

        In-memory stop follows every tick once armed. Ledger rows only when the
        trail arms or the stop moves by ≥ half the trail gap (min ₹0.5), so a
        steady trend does not write ~1 row/sec.
        """
        px = float(px)
        armed_before = pos.trail_armed
        gap = trail_gap_px(pos.entry, trail_pct=self.trail_pct, trail_pts=self.trail_pts)
        if pos.style == "short":
            pos.best_px = round(min(pos.best_px or pos.entry, px), 2)
            arm_level = pos.entry * (1.0 - float(self.trail_arm_pct))
            if not pos.trail_armed and px <= arm_level:
                pos.trail_armed = True
            if pos.trail_armed:
                trailed = round(pos.best_px + gap, 2)
                pos.stop = round(min(pos.stop, trailed), 2)
        else:
            pos.best_px = round(max(pos.best_px or pos.entry, px), 2)
            arm_level = pos.entry * (1.0 + float(self.trail_arm_pct))
            if not pos.trail_armed and px >= arm_level:
                pos.trail_armed = True
            if pos.trail_armed:
                trailed = round(pos.best_px - gap, 2)
                pos.stop = round(max(pos.stop, trailed), 2)
        just_armed = pos.trail_armed and not armed_before
        if not just_armed:
            last = pos.trail_ledger_stop
            if last is None:
                if not pos.trail_armed:
                    return
            else:
                step = trail_ledger_step_px(
                    pos.entry,
                    trail_pct=self.trail_pct,
                    trail_pts=self.trail_pts,
                    step_frac=self.trail_ledger_step_frac,
                    step_min=self.trail_ledger_step_min,
                )
                if abs(pos.stop - float(last)) + 1e-9 < step:
                    return
        self._append(
            {
                "event": "trail",
                "ts": now.isoformat(),
                "day": pos.day,
                "side": pos.side,
                "style": pos.style,
                "entry": pos.entry,
                "stop": pos.stop,
                "best_px": pos.best_px,
                "trail_armed": pos.trail_armed,
                "mark": px,
                "gates": "agent",
            }
        )
        pos.trail_ledger_stop = pos.stop

    def _maybe_exit(
        self,
        now: datetime,
        book: QuoteSource | None,
        *,
        spot: float | None = None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        session_due = hm_ge(now, SQUARE_OFF)
        ce, pe = self._book_quotes(book, pos.ce_symbol, pos.pe_symbol)
        px = ce if pos.side == "ce" else pe
        if px is None or px <= 0:
            # Mid-session / hold expiry: wait for a quote (same idea as LIC).
            # Unmarked flatten only at square-off so day PnL is not silently dropped.
            if session_due:
                return self._close(now, book, "square_off", marked=False, spot=spot)
            return None
        self._update_trail(pos, px, now=now)
        if session_due:
            return self._close(now, book, "square_off", exit_px=px, spot=spot)
        if pos.style == "short":
            if px <= pos.target:
                return self._close(now, book, "target", exit_px=px, spot=spot)
            if px >= pos.stop:
                reason = "trail" if pos.trail_armed else "stop"
                return self._close(now, book, reason, exit_px=px, spot=spot)
        else:
            if px >= pos.target:
                return self._close(now, book, "target", exit_px=px, spot=spot)
            if px <= pos.stop:
                reason = "trail" if pos.trail_armed else "stop"
                return self._close(now, book, reason, exit_px=px, spot=spot)
        if self._hold_due(now, pos):
            return self._close(now, book, "time", exit_px=px, spot=spot)
        return None

    def _close(
        self,
        now: datetime,
        book: QuoteSource | None,
        reason: str,
        *,
        exit_px: float | None = None,
        marked: bool = True,
        book_day: str | None = None,
        spot: float | None = None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        if exit_px is None and book is not None:
            ce, pe = self._book_quotes(book, pos.ce_symbol, pos.pe_symbol)
            exit_px = ce if pos.side == "ce" else pe
        if exit_px is None or exit_px <= 0:
            marked = False
            exit_px = pos.entry
        exit_px = round(float(exit_px), 2)
        book_day = book_day or pos.day
        rolling = book_day != (self.traded_day or pos.day)
        new_day_pnl = 0.0 if rolling else self.day_pnl
        charges_close = (
            float(kite_nfo_charges(_close_legs(pos.style, exit_px, pos.qty))["total"]) if marked else 0.0
        )
        charges = round(pos.charges_open + charges_close, 2) if marked else round(pos.charges_open, 2)
        pnl_gross = _pnl_gross(pos.style, pos.entry, exit_px, pos.qty) if marked else None
        pnl = round(float(pnl_gross) - charges, 2) if pnl_gross is not None else None
        if pnl is not None:
            new_day_pnl = round(new_day_pnl + pnl, 2)
        event = self._append(
            {
                "event": "close",
                "ts": now.isoformat(),
                "day": book_day,
                "atm": pos.atm,
                "side": pos.side,
                "style": pos.style,
                "symbol": pos.symbol,
                "qty": pos.qty,
                "entry": pos.entry,
                "exit": exit_px,
                "target": pos.target,
                "stop": pos.stop,
                "trail_armed": pos.trail_armed,
                "best_px": pos.best_px,
                "pnl_gross": pnl_gross,
                "charges": charges,
                "pnl": pnl,
                "reason": reason,
                "pnl_known": marked and pnl is not None,
                "day_pnl": round(new_day_pnl, 2),
                "spot": spot,
                "gates": "agent",
            }
        )
        if event is None:
            return None
        if not rolling and pnl is not None and pnl < 0:
            if reason == "stop":
                self._record_stop_lesson(
                    now=now, pos=pos, exit_px=exit_px, pnl=pnl, spot=spot
                )
            else:
                self._record_loss_cooldown(
                    now=now,
                    pos=pos,
                    exit_px=exit_px,
                    pnl=pnl,
                    spot=spot,
                    reason=reason,
                )
        if rolling:
            self._roll_to_day(book_day)
            # Leftover flatten consumes today's entry slot (same as other paper books).
            self.entries_today = 1
        self.position = None
        self.day_pnl = new_day_pnl
        self.last_exit_at = now
        self.pending = None
        return event
