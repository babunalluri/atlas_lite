"""A live-orders switch that is good for one trading day only.

Two locks, both required for real orders:
  - an environment variable on the host (ops unlock; default locked);
  - a small JSON file, written by the login-gated dashboard checkbox, stamped with the day it was
    switched on. A file switched on for an earlier day (or with no day at all) does not count, so a
    forgotten tick never carries into the next morning.
Switching off is always allowed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class DayLiveFlag:
    def __init__(self, path: Path, env_name: str) -> None:
        self.path = Path(path)
        self.env_name = env_name

    def allowed(self) -> bool:
        return os.environ.get(self.env_name, "0").strip().lower() in ("1", "true", "yes", "on")

    def snapshot(self) -> dict[str, Any]:
        raw = self._raw()
        file_on = bool(raw.get("enabled"))
        day = str(raw.get("day") or "").strip()
        today = datetime.now(IST).strftime("%Y-%m-%d")
        stale = file_on and day != today
        allowed = self.allowed()
        return {
            "ok": True,
            "enabled": bool(allowed and file_on and not stale),
            "allowed": allowed,
            "file_enabled": file_on,
            "day": day or None,
            "stale": stale,
            "updated_at": raw.get("updated_at"),
            "source": raw.get("source"),
        }

    def is_enabled(self) -> bool:
        return bool(self.snapshot()["enabled"])

    def set_enabled(self, enabled: bool, *, source: str = "ui") -> dict[str, Any]:
        if enabled and not self.allowed():
            raise ValueError("live_not_allowed")
        now = datetime.now(IST)
        body = {
            "enabled": bool(enabled),
            "day": now.strftime("%Y-%m-%d") if enabled else "",
            "updated_at": now.isoformat(timespec="seconds"),
            "source": str(source or "ui"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        return self.snapshot()

    def turn_off_if_on(self, *, source: str) -> bool:
        """End-of-day auto-off. True if it was on."""
        if not self._raw().get("enabled"):
            return False
        self.set_enabled(False, source=source)
        return True

    def _raw(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}


class OnOffFlag:
    """A persistent on/off switch (not tied to a day), ON when its file is missing. Used for the LLM buyer's
    "Paper" box: off = the book makes no LLM calls and no trades at all."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def snapshot(self) -> dict[str, Any]:
        raw = self._raw()
        return {"ok": True, "enabled": bool(raw.get("enabled", True)), "updated_at": raw.get("updated_at"),
                "source": raw.get("source")}

    def is_enabled(self) -> bool:
        return bool(self.snapshot()["enabled"])

    def set_enabled(self, enabled: bool, *, source: str = "ui") -> dict[str, Any]:
        body = {"enabled": bool(enabled), "updated_at": datetime.now(IST).isoformat(timespec="seconds"),
                "source": str(source or "ui")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        return self.snapshot()

    def _raw(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}
