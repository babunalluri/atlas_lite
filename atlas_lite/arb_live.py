"""NSE–BSE arb live-orders switch and day-loss kill.

Dual gate (same pattern as the agent):
  - ATLAS_LITE_ARB_LIVE_ALLOWED=1 unlocks enabling
  - data/arb_live.json file must say enabled
Both required for real orders. Default locked.

Day stop: once realized live P&L for the IST day is ≤ −DAY_LOSS_STOP
(default ₹2,000), no further live entries that day. Persisted under data/arb/
so a restart still respects the stop.

Live window (IST): ATLAS_LITE_ARB_LIVE_AFTER / ATLAS_LITE_ARB_LIVE_UNTIL
(default 09:15–09:20). Paper attempts stop at ATTEMPT_UNTIL (15:15) so
closing-session quote artifacts are not traded or scored as tradable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
LIVE_FLAG_NAME = "arb_live.json"
DEFAULT_DAY_LOSS_STOP = 2_000.0
LIVE_LATENCY_MS = 300  # only this delay fires real orders (paper keeps 100/300/600)
LIVE_ORDER_TAG = "atlas_arb"
# No paper/live entry attempts at/after this (closing-session gaps are untradable).
ATTEMPT_UNTIL = "15:15"
# Force-flatten any still-open live hedges/residuals before broker MIS square-off.
LIVE_FLAT_BY = "15:10"
DEFAULT_LIVE_AFTER = "09:15"
DEFAULT_LIVE_UNTIL = "09:20"  # open window only; paper after 09:20 still recorded


def live_allowed_by_env() -> bool:
    raw = os.environ.get("ATLAS_LITE_ARB_LIVE_ALLOWED", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def day_loss_stop_rs() -> float:
    raw = os.environ.get("ATLAS_LITE_ARB_LIVE_DAY_STOP", "").strip()
    if not raw:
        return DEFAULT_DAY_LOSS_STOP
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_DAY_LOSS_STOP


def live_after_hm() -> str:
    return (os.environ.get("ATLAS_LITE_ARB_LIVE_AFTER") or DEFAULT_LIVE_AFTER).strip() or DEFAULT_LIVE_AFTER


def live_until_hm() -> str:
    return (os.environ.get("ATLAS_LITE_ARB_LIVE_UNTIL") or DEFAULT_LIVE_UNTIL).strip() or DEFAULT_LIVE_UNTIL


def hm_now() -> str:
    return datetime.now(IST).strftime("%H:%M")


def within_attempt_hours(hm: str | None = None) -> bool:
    """Paper + live entries allowed before ATTEMPT_UNTIL."""
    return (hm or hm_now()) < ATTEMPT_UNTIL


def within_live_window(hm: str | None = None) -> bool:
    """Real orders only inside the configured open window and before ATTEMPT_UNTIL."""
    t = hm or hm_now()
    if t >= ATTEMPT_UNTIL:
        return False
    return live_after_hm() <= t < live_until_hm()


class ArbLiveFlag:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def snapshot(self) -> dict[str, Any]:
        allowed = live_allowed_by_env()
        raw = self._raw()
        file_on = bool(raw.get("enabled"))
        armed_day = str(raw.get("day") or "").strip()
        today = datetime.now(IST).strftime("%Y-%m-%d")
        # Finding #4 / audit A: enabled without day==today is stale (missing day counts).
        # A pre-day field ops_restore file must not arm live at the next open.
        stale = bool(file_on and armed_day != today)
        enabled = bool(allowed and file_on and not stale)
        return {
            "ok": True,
            "enabled": enabled,
            "file_enabled": file_on,
            "allowed": allowed,
            "day": armed_day or None,
            "stale": stale,
            "day_loss_stop": day_loss_stop_rs(),
            "latency_ms": LIVE_LATENCY_MS,
            "live_after": live_after_hm(),
            "live_until": live_until_hm(),
            "attempt_until": ATTEMPT_UNTIL,
            "flat_by": LIVE_FLAT_BY,
            "path": str(self.path),
            "updated_at": self._updated_at(),
        }

    def is_enabled(self) -> bool:
        return bool(self.snapshot()["enabled"])

    def set_enabled(self, enabled: bool, *, source: str = "ops") -> dict[str, Any]:
        if enabled and not live_allowed_by_env():
            raise ValueError("live_not_allowed")
        today = datetime.now(IST).strftime("%Y-%m-%d")
        body = {
            "enabled": bool(enabled),
            "day": today if enabled else "",
            "updated_at": datetime.now(IST).isoformat(timespec="seconds"),
            "source": str(source or "ops"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        return self.snapshot()

    def clear_if_stale(self) -> dict[str, Any] | None:
        """If file says enabled for a prior day, turn it off. Returns new snapshot or None."""
        snap = self.snapshot()
        if not snap.get("stale"):
            return None
        return self.set_enabled(False, source="stale_day")

    def _raw(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _file_enabled(self) -> bool:
        return bool(self._raw().get("enabled"))

    def _updated_at(self) -> str | None:
        value = self._raw().get("updated_at")
        return str(value) if value else None


class ArbLiveDayState:
    """Realized live P&L and day-stop for one IST session."""

    def __init__(self, path: Path, *, day: str, stop_rs: float | None = None) -> None:
        self.path = Path(path)
        self.day = day
        self.stop_rs = float(stop_rs if stop_rs is not None else day_loss_stop_rs())
        self.day_pnl = 0.0
        self.stopped = False
        self.stop_reason = ""
        self.trades = 0
        self.opens_blocked = False  # sticky after unwind_failed / residual risk
        self._load()

    def snapshot(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "day_pnl": round(self.day_pnl, 2),
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
            "stop_rs": self.stop_rs,
            "trades": self.trades,
            "opens_blocked": self.opens_blocked,
            "entries_allowed": not self.stopped and not self.opens_blocked,
        }

    def note_trade(self, net: float | None, *, count: bool = True) -> dict[str, Any]:
        """Apply realized net. count=False for zero-fill / none (finding #9)."""
        if net is not None:
            self.day_pnl = round(self.day_pnl + float(net), 2)
            if count:
                self.trades += 1
        if (not self.stopped) and self.day_pnl <= -self.stop_rs:
            self.stopped = True
            self.stop_reason = f"day_loss_stop:{self.day_pnl}"
        self._save()
        return self.snapshot()

    def block_opens(self, reason: str) -> dict[str, Any]:
        self.opens_blocked = True
        if not self.stop_reason:
            self.stop_reason = str(reason or "opens_blocked")
        self._save()
        return self.snapshot()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict) or str(raw.get("day") or "") != self.day:
            return
        try:
            self.day_pnl = float(raw.get("day_pnl") or 0.0)
        except (TypeError, ValueError):
            self.day_pnl = 0.0
        self.stopped = bool(raw.get("stopped"))
        self.stop_reason = str(raw.get("stop_reason") or "")
        self.opens_blocked = bool(raw.get("opens_blocked"))
        try:
            self.trades = int(raw.get("trades") or 0)
        except (TypeError, ValueError):
            self.trades = 0
        if (not self.stopped) and self.day_pnl <= -self.stop_rs:
            self.stopped = True
            self.stop_reason = self.stop_reason or f"day_loss_stop:{self.day_pnl}"

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            **self.snapshot(),
            "updated_at": datetime.now(IST).isoformat(timespec="seconds"),
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
