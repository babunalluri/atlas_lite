"""Persist the agent live-orders switch. File off by default; env allows the UI.

The UI writes this file through a login-gated API. Ops can force the book to stay
paper-only with ATLAS_LITE_AGENT_LIVE_ALLOWED=0 even if the file says enabled.
A live open must still close on the broker when the flag flips off mid-trade.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
LIVE_FLAG_NAME = "agent_live.json"


def live_allowed_by_env() -> bool:
    """Master kill. Default locked. Set ATLAS_LITE_AGENT_LIVE_ALLOWED=1 to unlock the UI."""
    raw = os.environ.get("ATLAS_LITE_AGENT_LIVE_ALLOWED", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


class AgentLiveFlag:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def snapshot(self) -> dict[str, Any]:
        allowed = live_allowed_by_env()
        file_on = self._file_enabled()
        enabled = bool(allowed and file_on)
        return {
            "ok": True,
            "enabled": enabled,
            "file_enabled": file_on,
            "allowed": allowed,
            "path": str(self.path),
            "updated_at": self._updated_at(),
        }

    def is_enabled(self) -> bool:
        return bool(self.snapshot()["enabled"])

    def set_enabled(self, enabled: bool, *, source: str = "ui") -> dict[str, Any]:
        if enabled and not live_allowed_by_env():
            raise ValueError("live_not_allowed")
        body = {
            "enabled": bool(enabled),
            "updated_at": datetime.now(IST).isoformat(timespec="seconds"),
            "source": str(source or "ui"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        return self.snapshot()

    def _raw(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _file_enabled(self) -> bool:
        raw = self._raw()
        return bool(raw.get("enabled"))

    def _updated_at(self) -> str | None:
        value = self._raw().get("updated_at")
        return str(value) if value else None
