"""AI agent paper book — ATM CE/PE from a tape-trained model. No Kite orders.

Independent of the scorecard agent: own ledger, own switch, own position.
The exit profile matches the agent (morning and after-10:30). The model only
chooses the side. It does not move the stop, the day-loss line, or the flat time.
A new entry is scored once, on the minute that just closed, which is the bar
the fit sees. A short is skipped when that option is already up over 15 minutes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from atlas_lite.ai_agent_model import (
    ACTIONS,
    FEATURE_NAMES,
    MAX_ENTRIES_PER_DAY,
    MODEL_NAME,
    PREMIUM_LOOKBACK_MIN,
    _days_left,
    features_from_values,
    load_model,
    predict_action,
    skip_rising_short,
)
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.log_util import get_logger
from atlas_lite.metrics import quote_ltp
from atlas_lite.minute_bars import hm_ge, hm_le
from atlas_lite.paper_agent import (
    CAPITAL,
    COOLDOWN_MIN,
    DAILY_LOSS_STOP,
    DEFAULT_LOT_SIZE,
    ENTRY_AFTER,
    ENTRY_UNTIL,
    HOLD_MINUTES,
    LOTS,
    SQUARE_OFF,
    agent_exit_profile,
    agent_hold_until,
    initial_target_stop,
    trail_gap_px,
)

IST = ZoneInfo("Asia/Kolkata")
STRATEGY = "ai_agent"
# A human writes the model's trained_at here. The nightly fit never does.
APPROVAL_NAME = "ai_agent_approved.txt"


def paper_ai_agent_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_AI_AGENT", "1").strip().lower()
    return raw in ("1", "true", "yes")


def packaged_model_path() -> Path:
    return Path(__file__).with_name(MODEL_NAME)


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def _parse_ist(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def in_ai_entry_window(now: datetime) -> bool:
    return hm_ge(now, ENTRY_AFTER) and hm_le(now, ENTRY_UNTIL) and not hm_ge(now, SQUARE_OFF)


@dataclass
class AiPosition:
    day: str
    atm: int
    side: str
    style: str
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
    reason: str
    trail_armed: bool = False
    best_px: float = 0.0
    trail_arm_pct: float = 0.04
    trail_pct: float = 0.03
    trail_pts: float = 1.5


@dataclass
class PaperAiAgent:
    path: Path
    model_path: Path | None = None
    lot_size: int = DEFAULT_LOT_SIZE
    lots: int = LOTS
    capital: float = CAPITAL
    hold_minutes: int = HOLD_MINUTES
    cooldown_min: int = COOLDOWN_MIN
    daily_loss_stop: float = DAILY_LOSS_STOP
    position: AiPosition | None = None
    traded_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    last_exit_at: datetime | None = None
    last_reject: str | None = None
    last_action: str | None = None
    last_predicted_net: float | None = None
    max_entries_per_day: int = MAX_ENTRIES_PER_DAY
    shadow: AiPosition | None = None
    model: dict[str, Any] | None = None
    _premium_memory: dict[str, tuple[int | None, float | None, float | None]] = field(default_factory=dict)
    _open_minute_key: str = ""
    _open_minute: dict[str, Any] | None = None
    _pending_minute: dict[str, Any] | None = None
    _decided_minute: str = ""
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_ai_agent"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        fallback = self.model_path is None
        if self.model_path is None:
            # Always the ledger directory, so the after-close fit lands where reload looks.
            self.model_path = self.path.parent / MODEL_NAME
        else:
            self.model_path = Path(self.model_path)
        object.__setattr__(self, "_model_fallback", fallback)
        self.model = load_model(self.model_path)
        if self.model is None and fallback:
            self.model = load_model(packaged_model_path())
        self._restore()

    def reload_model(self) -> bool:
        """Load the after-close fit. Falls back to the packaged file if today's fit is missing."""
        loaded = load_model(self.model_path)
        if loaded is None and getattr(self, "_model_fallback", False):
            loaded = load_model(packaged_model_path())
        if loaded is None:
            return False
        self.model = loaded
        return True

    @property
    def promoted(self) -> bool:
        return bool(self.model and self.model.get("promoted"))

    def _approval_path(self) -> Path:
        return Path(self.model_path).parent / APPROVAL_NAME

    @property
    def approved(self) -> bool:
        """True only when a person has accepted this exact fit."""
        trained_at = str((self.model or {}).get("trained_at") or "").strip()
        if not trained_at:
            return False
        try:
            stamp = self._approval_path().read_text(encoding="utf-8").strip()
        except OSError:
            return False
        return stamp == trained_at

    @property
    def can_open(self) -> bool:
        return self.promoted and self.approved

    def approve(self, *, trained_at: str | None = None) -> dict[str, Any]:
        """Accept the current fit so paper opens (not only shadow) can run.

        Writes ``ai_agent_approved.txt`` with this model's ``trained_at``. A later
        refit clears approval until someone accepts the new stamp.
        """
        stamp = str(trained_at or (self.model or {}).get("trained_at") or "").strip()
        if not stamp:
            return {"ok": False, "error": "no_trained_at", "approved": False, "promoted": self.promoted}
        if not self.promoted:
            return {
                "ok": False,
                "error": "not_promoted",
                "approved": False,
                "promoted": False,
                "trained_at": stamp,
            }
        path = self._approval_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(stamp + "\n", encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": str(exc), "approved": False, "promoted": self.promoted}
        return {
            "ok": True,
            "approved": self.approved,
            "promoted": self.promoted,
            "can_open": self.can_open,
            "trained_at": stamp,
            "path": str(path),
        }

    def _weights(self) -> dict[str, list[float]] | None:
        if not self.model:
            return None
        weights = self.model.get("weights")
        if not isinstance(weights, dict):
            return None
        return weights

    def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event.setdefault("strategy", STRATEGY)
        event.setdefault("live_orders", False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError as exc:
            self._log.warning("ai agent ledger write failed: %s", exc)
            return None
        return event

    def _restore(self) -> None:
        if not self.path.is_file():
            return
        open_ev: dict[str, Any] | None = None
        shadow_ev: dict[str, Any] | None = None
        last_close: dict[str, Any] | None = None
        opens_by_day: dict[str, int] = {}
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
            if ev.get("strategy") not in (None, STRATEGY):
                continue
            if ev.get("event") == "open":
                open_ev = ev
                shadow_ev = None
            elif ev.get("event") == "close":
                open_ev = None
                shadow_ev = None
                last_close = ev
            elif ev.get("event") == "shadow_open":
                shadow_ev = ev
            elif ev.get("event") == "shadow_close":
                shadow_ev = None
            if ev.get("event") in ("open", "shadow_open"):
                day_key = str(ev.get("day") or "")
                opens_by_day[day_key] = opens_by_day.get(day_key, 0) + 1
        close_day = ""
        if last_close is not None:
            close_day = str(last_close.get("day") or "")
            self.traded_day = close_day
            self.day_pnl = float(last_close.get("day_pnl") or 0.0)
            closed_at = _parse_ist(str(last_close.get("ts") or ""))
            if closed_at is not None:
                self.last_exit_at = closed_at
        if open_ev is not None:
            self.position = self._position_from_event(open_ev)
            self.traded_day = self.position.day
            self.entries_today = opens_by_day.get(self.position.day, 0)
            if close_day != self.position.day:
                self.day_pnl = 0.0
                self.last_exit_at = None
            return
        if shadow_ev is not None:
            self.shadow = self._position_from_event(shadow_ev)
            self.traded_day = self.shadow.day
            self.entries_today = opens_by_day.get(self.shadow.day, 0)

    def _position_from_event(self, event: dict[str, Any]) -> AiPosition:
        return AiPosition(
            day=str(event.get("day") or ""),
            atm=int(event.get("atm") or 0),
            side=str(event.get("side") or "ce"),
            style=str(event.get("style") or "long"),
            symbol=str(event.get("symbol") or ""),
            ce_symbol=str(event.get("ce_symbol") or ""),
            pe_symbol=str(event.get("pe_symbol") or ""),
            qty=int(event.get("qty") or self.lot_size),
            lots=int(event.get("lots") or self.lots),
            entry=float(event.get("entry") or 0.0),
            target=float(event.get("target") or 0.0),
            stop=float(event.get("stop") or 0.0),
            hold_until=str(event.get("hold_until") or ""),
            opened_at=str(event.get("ts") or ""),
            charges_open=float(event.get("charges") or 0.0),
            reason=str(event.get("reason") or ""),
            trail_armed=bool(event.get("trail_armed")),
            best_px=float(event.get("best_px") or event.get("entry") or 0.0),
            trail_arm_pct=float(event.get("trail_arm_pct") or 0.04),
            trail_pct=float(event.get("trail_pct") or 0.03),
            trail_pts=float(event.get("trail_pts") or 1.5),
        )

    def _ltp(self, book: QuoteSource | None, symbol: str) -> float | None:
        if book is None or not symbol:
            return None
        return quote_ltp(book.get(symbol))

    def _mark(self, book: QuoteSource | None, pos: AiPosition) -> float | None:
        symbol = pos.ce_symbol if pos.side == "ce" else pos.pe_symbol
        return self._ltp(book, symbol)

    def _in_cooldown(self, now: datetime) -> bool:
        if self.last_exit_at is None or self.cooldown_min <= 0:
            return False
        return now < self.last_exit_at + timedelta(minutes=int(self.cooldown_min))

    def _note_session_premium(self, now: datetime, feed: dict[str, Any], atm: int | None) -> None:
        """Store this minute's premium from the open, before any entry gate."""
        if now.weekday() >= 5:
            return
        hm = now.strftime("%H:%M")
        if hm < "09:15" or hm > "15:30":
            return
        strike = float(atm) if atm is not None else _f(feed.get("atm"))
        self._note_premium(now, strike, _f(feed.get("ce")), _f(feed.get("pe")))

    def _note_premium(self, now: datetime, atm: float | None, ce: float | None, pe: float | None) -> None:
        key = now.strftime("%Y-%m-%dT%H:%M")
        atm_i = int(atm) if atm is not None else None
        self._premium_memory[key] = (atm_i, ce, pe)
        cutoff = (now - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M")
        for old in [item for item in self._premium_memory if item < cutoff]:
            del self._premium_memory[old]

    def _premium_change(self, now: datetime, atm: float | None) -> tuple[float | None, float | None]:
        """Change in the call and the put over 15 minutes, on this strike only."""
        prev = self._premium_memory.get((now - timedelta(minutes=PREMIUM_LOOKBACK_MIN)).strftime("%Y-%m-%dT%H:%M"))
        cur = self._premium_memory.get(now.strftime("%Y-%m-%dT%H:%M"))
        if prev is None or cur is None:
            return None, None
        if prev[0] is None or cur[0] is None or prev[0] != cur[0]:
            return None, None
        if atm is not None and cur[0] != int(atm):
            return None, None
        ce_chg = None if cur[1] is None or prev[1] is None else cur[1] - prev[1]
        pe_chg = None if cur[2] is None or prev[2] is None else cur[2] - prev[2]
        return ce_chg, pe_chg

    def _decide(self, feed: dict[str, Any], now: datetime) -> tuple[str, str] | None:
        weights = self._weights()
        if weights is None:
            self.last_reject = "model_missing"
            self.last_action = None
            return None
        ce = _f(feed.get("ce"))
        pe = _f(feed.get("pe"))
        skew = (ce - pe) if ce is not None and pe is not None else None
        atm = _f(feed.get("atm"))
        ce_chg, pe_chg = self._premium_change(now, atm)
        expiry = str(feed.get("expiry") or "")[:10] or None
        features = features_from_values(
            spot_open_pct=_f(feed.get("spot_chg_open_pct")),
            adx=_f(feed.get("adx")),
            pcr=_f(feed.get("pcr")),
            skew=skew,
            atr=_f(feed.get("atr")),
            hm=now.strftime("%H:%M"),
            nifty_chg=_f(feed.get("index_nifty_chg")),
            spot=_f(feed.get("spot") if feed.get("spot") is not None else feed.get("nifty_ltp")),
            atm=atm,
            days_left=_days_left(now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), expiry),
            ce_chg=ce_chg,
            pe_chg=pe_chg,
        )
        if len(features) != len(FEATURE_NAMES):
            self.last_reject = "bad_features"
            return None
        action, predicted = predict_action(weights, features)
        self.last_action = action
        self.last_predicted_net = predicted
        if skip_rising_short(action, ce_chg, pe_chg) == "wait" and action in ACTIONS:
            self.last_reject = "rising_premium"
            return None
        if action not in ACTIONS:
            self.last_reject = "model_wait" if predicted <= 0 else "edge_too_small"
            return None
        style, side = action.split("_", 1)
        self.last_reject = None
        return side, style

    def _open(
        self,
        now: datetime,
        book: QuoteSource | None,
        *,
        side: str,
        style: str,
        ce_symbol: str,
        pe_symbol: str,
        atm: int,
        spot: float | None,
    ) -> dict[str, Any] | None:
        symbol = ce_symbol if side == "ce" else pe_symbol
        entry = self._ltp(book, symbol)
        if entry is None or entry <= 0:
            self.last_reject = "no_quote"
            return None
        target_pct, stop_pct, arm_pct, trail_pct, trail_pts = agent_exit_profile(
            now, style
        )
        target, stop = initial_target_stop(entry, style, target_pct=target_pct, stop_pct=stop_pct)  # type: ignore[arg-type]
        qty = int(self.lots) * int(self.lot_size)
        open_side = "sell" if style == "short" else "buy"
        charges_open = float(kite_nfo_charges([(entry, qty, open_side)])["total"])
        hold_until = agent_hold_until(now, style).isoformat()
        pos = AiPosition(
            day=now.strftime("%Y-%m-%d"),
            atm=int(atm),
            side=side,
            style=style,
            symbol=symbol,
            ce_symbol=ce_symbol,
            pe_symbol=pe_symbol,
            qty=qty,
            lots=int(self.lots),
            entry=round(entry, 2),
            target=target,
            stop=stop,
            hold_until=hold_until,
            opened_at=now.isoformat(),
            charges_open=round(charges_open, 2),
            reason=f"ai_agent {style} {side}",
            best_px=round(entry, 2),
            trail_arm_pct=arm_pct,
            trail_pct=trail_pct,
            trail_pts=trail_pts,
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
                "trail_arm_pct": pos.trail_arm_pct,
                "trail_pct": pos.trail_pct,
                "trail_pts": pos.trail_pts,
                "hold_until": pos.hold_until,
                "charges": pos.charges_open,
                "reason": pos.reason,
                "predicted_net": self.last_predicted_net,
                "promoted": self.promoted,
                "spot": spot,
            }
        )
        if event is None:
            return None
        self.position = pos
        self.entries_today += 1
        self.traded_day = pos.day
        self.last_reject = None
        return event

    def _open_shadow(
        self,
        now: datetime,
        book: QuoteSource | None,
        *,
        side: str,
        style: str,
        ce_symbol: str,
        pe_symbol: str,
        atm: int,
        spot: float | None,
    ) -> dict[str, Any] | None:
        """Record the decision and follow it on paper without a position the book owns."""
        symbol = ce_symbol if side == "ce" else pe_symbol
        entry = self._ltp(book, symbol)
        if entry is None or entry <= 0:
            self.last_reject = "no_quote"
            return None
        target_pct, stop_pct, arm_pct, trail_pct, trail_pts = agent_exit_profile(
            now, style
        )
        target, stop = initial_target_stop(entry, style, target_pct=target_pct, stop_pct=stop_pct)  # type: ignore[arg-type]
        qty = int(self.lots) * int(self.lot_size)
        open_side = "sell" if style == "short" else "buy"
        charges_open = float(kite_nfo_charges([(entry, qty, open_side)])["total"])
        pos = AiPosition(
            day=now.strftime("%Y-%m-%d"),
            atm=int(atm),
            side=side,
            style=style,
            symbol=symbol,
            ce_symbol=ce_symbol,
            pe_symbol=pe_symbol,
            qty=qty,
            lots=int(self.lots),
            entry=round(entry, 2),
            target=target,
            stop=stop,
            hold_until=agent_hold_until(now, style).isoformat(),
            opened_at=now.isoformat(),
            charges_open=round(charges_open, 2),
            reason=f"shadow {style} {side}",
            best_px=round(entry, 2),
            trail_arm_pct=arm_pct,
            trail_pct=trail_pct,
            trail_pts=trail_pts,
        )
        event = self._append(
            {
                "event": "shadow_open",
                "ts": now.isoformat(),
                "day": pos.day,
                "atm": pos.atm,
                "side": pos.side,
                "style": pos.style,
                "symbol": pos.symbol,
                "qty": pos.qty,
                "entry": pos.entry,
                "target": pos.target,
                "stop": pos.stop,
                "hold_until": pos.hold_until,
                "charges": pos.charges_open,
                "reason": pos.reason,
                "predicted_net": self.last_predicted_net,
                "promoted": False,
                "spot": spot,
            }
        )
        if event is None:
            return None
        self.shadow = pos
        self.entries_today += 1
        self.traded_day = pos.day
        self.last_reject = "shadow"
        return event

    def _close_shadow(
        self,
        now: datetime,
        book: QuoteSource | None,
        reason: str,
        *,
        exit_px: float | None = None,
        spot: float | None = None,
    ) -> dict[str, Any] | None:
        pos = self.shadow
        if pos is None:
            return None
        if exit_px is None:
            exit_px = self._mark(book, pos)
        marked = exit_px is not None and exit_px > 0
        if not marked:
            exit_px = pos.entry
        exit_px = round(float(exit_px), 2)
        close_side = "buy" if pos.style == "short" else "sell"
        charges_close = float(kite_nfo_charges([(exit_px, pos.qty, close_side)])["total"]) if marked else 0.0
        charges = round(pos.charges_open + charges_close, 2)
        if pos.style == "short":
            gross = round((pos.entry - exit_px) * pos.qty, 2) if marked else None
        else:
            gross = round((exit_px - pos.entry) * pos.qty, 2) if marked else None
        pnl = round(float(gross) - charges, 2) if gross is not None else None
        event = self._append(
            {
                "event": "shadow_close",
                "ts": now.isoformat(),
                "day": pos.day,
                "side": pos.side,
                "style": pos.style,
                "symbol": pos.symbol,
                "qty": pos.qty,
                "entry": pos.entry,
                "exit": exit_px,
                "pnl_gross": gross,
                "charges": charges,
                "pnl": pnl,
                "reason": reason,
                "spot": spot,
            }
        )
        if event is None:
            return None
        self.shadow = None
        self.last_exit_at = now
        return event

    def _maybe_shadow_exit(
        self,
        now: datetime,
        book: QuoteSource | None,
        *,
        spot: float | None,
    ) -> dict[str, Any] | None:
        pos = self.shadow
        if pos is None:
            return None
        session_due = hm_ge(now, SQUARE_OFF)
        px = self._mark(book, pos)
        if px is None or px <= 0:
            if session_due:
                return self._close_shadow(now, book, "square_off", spot=spot)
            return None
        self._update_trail(pos, px)
        if session_due:
            return self._close_shadow(now, book, "square_off", exit_px=px, spot=spot)
        if pos.style == "short":
            if px <= pos.target:
                return self._close_shadow(now, book, "target", exit_px=px, spot=spot)
            if px >= pos.stop:
                reason = "trail" if pos.trail_armed else "stop"
                return self._close_shadow(now, book, reason, exit_px=px, spot=spot)
        else:
            if px >= pos.target:
                return self._close_shadow(now, book, "target", exit_px=px, spot=spot)
            if px <= pos.stop:
                reason = "trail" if pos.trail_armed else "stop"
                return self._close_shadow(now, book, reason, exit_px=px, spot=spot)
        try:
            hold = datetime.fromisoformat(pos.hold_until)
        except ValueError:
            hold = now
        if hold.tzinfo is None:
            hold = hold.replace(tzinfo=IST)
        if now >= hold:
            return self._close_shadow(now, book, "time", exit_px=px, spot=spot)
        return None

    def _update_trail(self, pos: AiPosition, px: float) -> None:
        gap = trail_gap_px(pos.entry, trail_pct=pos.trail_pct, trail_pts=pos.trail_pts)
        if pos.style == "short":
            pos.best_px = round(min(pos.best_px or pos.entry, px), 2)
            if not pos.trail_armed and px <= pos.entry * (1.0 - pos.trail_arm_pct):
                pos.trail_armed = True
            if pos.trail_armed:
                pos.stop = round(min(pos.stop, pos.best_px + gap), 2)
        else:
            pos.best_px = round(max(pos.best_px or pos.entry, px), 2)
            if not pos.trail_armed and px >= pos.entry * (1.0 + pos.trail_arm_pct):
                pos.trail_armed = True
            if pos.trail_armed:
                pos.stop = round(max(pos.stop, pos.best_px - gap), 2)

    def _close(
        self,
        now: datetime,
        book: QuoteSource | None,
        reason: str,
        *,
        exit_px: float | None = None,
        spot: float | None = None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        if exit_px is None:
            exit_px = self._mark(book, pos)
        marked = exit_px is not None and exit_px > 0
        if not marked:
            exit_px = pos.entry
        exit_px = round(float(exit_px), 2)
        close_side = "buy" if pos.style == "short" else "sell"
        charges_close = float(kite_nfo_charges([(exit_px, pos.qty, close_side)])["total"]) if marked else 0.0
        charges = round(pos.charges_open + charges_close, 2)
        if pos.style == "short":
            gross = round((pos.entry - exit_px) * pos.qty, 2) if marked else None
        else:
            gross = round((exit_px - pos.entry) * pos.qty, 2) if marked else None
        pnl = round(float(gross) - charges, 2) if gross is not None else None
        day = now.strftime("%Y-%m-%d")
        if pos.day != day:
            self.day_pnl = 0.0
            self.entries_today = 0
        if pnl is not None:
            self.day_pnl = round(self.day_pnl + pnl, 2)
        event = self._append(
            {
                "event": "close",
                "ts": now.isoformat(),
                "day": day if pos.day != day else pos.day,
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
                "pnl_gross": gross,
                "charges": charges,
                "pnl": pnl,
                "reason": reason,
                "day_pnl": round(self.day_pnl, 2),
                "spot": spot,
            }
        )
        if event is None:
            return None
        self.position = None
        self.last_exit_at = now
        self.traded_day = day
        return event

    def _maybe_exit(
        self,
        now: datetime,
        book: QuoteSource | None,
        *,
        spot: float | None,
    ) -> dict[str, Any] | None:
        pos = self.position
        if pos is None:
            return None
        session_due = hm_ge(now, SQUARE_OFF)
        px = self._mark(book, pos)
        if px is None or px <= 0:
            if session_due:
                return self._close(now, book, "square_off", spot=spot)
            return None
        self._update_trail(pos, px)
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
        try:
            hold = datetime.fromisoformat(pos.hold_until)
        except ValueError:
            hold = now
        if hold.tzinfo is None:
            hold = hold.replace(tzinfo=IST)
        if now >= hold:
            return self._close(now, book, "time", exit_px=px, spot=spot)
        return None

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
        # Remember the premium from 09:15, including while a trade is open or in cooldown.
        self._note_session_premium(now, feed, atm)
        closed = self._finish_minute(now, feed, atm, ce_symbol, pe_symbol)
        if closed is not None:
            self._pending_minute = closed
        day = now.strftime("%Y-%m-%d")
        if now.weekday() >= 5:
            if self.position is not None:
                return self._close(now, book, "weekend", spot=spot)
            if self.shadow is not None:
                return self._close_shadow(now, book, "weekend", spot=spot)
            return None
        if self.position is not None and self.position.day != day:
            return self._close(now, book, "session_gap", spot=spot)
        if self.shadow is not None and self.shadow.day != day:
            return self._close_shadow(now, book, "session_gap", spot=spot)
        if self.traded_day and self.traded_day != day and self.position is None:
            self.traded_day = day
            self.entries_today = 0
            self.day_pnl = 0.0
        if self.position is not None:
            return self._maybe_exit(now, book, spot=spot)
        if self.shadow is not None:
            return self._maybe_shadow_exit(now, book, spot=spot)
        pending = self._pending_minute
        if pending is None or str(pending.get("key") or "") == self._decided_minute:
            return None
        bar_key = str(pending["key"])
        bar_now = pending.get("as_of")
        if not isinstance(bar_now, datetime):
            self._decided_minute = bar_key
            return None
        if not allow_new_entries or not in_ai_entry_window(bar_now) or hm_ge(now, SQUARE_OFF):
            self._decided_minute = bar_key
            return None
        if self.day_pnl <= float(self.daily_loss_stop):
            self.last_reject = "daily_loss_stop"
            self._decided_minute = bar_key
            return None
        if self.entries_today >= int(self.max_entries_per_day):
            self.last_reject = "max_trades_day"
            self._decided_minute = bar_key
            return None
        if self._in_cooldown(now):
            self.last_reject = "cooldown"
            self._decided_minute = bar_key
            return None
        bar_ce = pending.get("ce_symbol")
        bar_pe = pending.get("pe_symbol")
        bar_atm = pending.get("atm")
        if not bar_ce or not bar_pe or bar_atm is None:
            self.last_reject = "no_atm"
            self._decided_minute = bar_key
            return None
        bar_feed = pending.get("feed")
        if not isinstance(bar_feed, dict):
            self._decided_minute = bar_key
            return None
        chosen = self._decide(bar_feed, bar_now)
        if chosen is None:
            self._decided_minute = bar_key
            return None
        side, style = chosen
        if not self.can_open:
            opened = self._open_shadow(
                now,
                book,
                side=side,
                style=style,
                ce_symbol=str(bar_ce),
                pe_symbol=str(bar_pe),
                atm=int(bar_atm),
                spot=spot,
            )
        else:
            opened = self._open(
                now,
                book,
                side=side,
                style=style,
                ce_symbol=str(bar_ce),
                pe_symbol=str(bar_pe),
                atm=int(bar_atm),
                spot=spot,
            )
        if opened is None:
            return None
        self._decided_minute = bar_key
        return opened

    def _finish_minute(
        self,
        now: datetime,
        feed: dict[str, Any],
        atm: int | None,
        ce_symbol: str | None,
        pe_symbol: str | None,
    ) -> dict[str, Any] | None:
        """Keep the latest quote of this minute. Return the previous minute once it has closed."""
        key = now.strftime("%Y-%m-%dT%H:%M")
        closed = self._open_minute if self._open_minute_key and key != self._open_minute_key else None
        snap = {
            "ce": feed.get("ce"),
            "pe": feed.get("pe"),
            "spot": feed.get("spot"),
            "nifty_ltp": feed.get("nifty_ltp"),
            "spot_chg_open_pct": feed.get("spot_chg_open_pct"),
            "adx": feed.get("adx"),
            "atr": feed.get("atr"),
            "pcr": feed.get("pcr"),
            "index_nifty_chg": feed.get("index_nifty_chg"),
            "expiry": feed.get("expiry"),
            "atm": atm if atm is not None else feed.get("atm"),
        }
        self._open_minute_key = key
        self._open_minute = {
            "key": key,
            "as_of": now,
            "feed": snap,
            "atm": atm,
            "ce_symbol": ce_symbol,
            "pe_symbol": pe_symbol,
        }
        return closed

    def snapshot(self, book: QuoteSource | None = None, *, spot: float | None = None) -> dict[str, Any]:
        pos = self.position
        mark = self._mark(book, pos) if pos is not None else None
        pos_body = None
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
                "mark": mark,
                "hold_until": pos.hold_until,
                "reason": pos.reason,
            }
        report = self.model or {}
        return {
            "ok": True,
            "mode": "paper",
            "live_orders": False,
            "book": STRATEGY,
            "name": "AI agent",
            "enabled": True,
            "promoted": self.promoted,
            "approved": self.approved,
            "can_open": self.can_open,
            "shadow": self.shadow is not None,
            "trained_at": report.get("trained_at"),
            "max_entries_per_day": self.max_entries_per_day,
            "model_reason": report.get("reason"),
            "test_net": report.get("test_net"),
            "scorecard_test_net": report.get("scorecard_test_net"),
            "position": pos_body,
            "entries_today": self.entries_today,
            "day_pnl": self.day_pnl,
            "last_reject": self.last_reject,
            "last_action": self.last_action,
            "last_predicted_net": self.last_predicted_net,
            "daily_loss_stop": self.daily_loss_stop,
            "spot": spot,
        }
