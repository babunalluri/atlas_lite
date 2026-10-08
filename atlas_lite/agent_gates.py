"""Persistent agent book gates (allow / pause / skip_entries)."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

GateMode = Literal["allow", "pause", "skip_entries"]

# NSE cash close — pause/skip without a parseable ``until`` expire here (IST).
SESSION_END = dt_time(15, 30)
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

# NSE cash holidays used only for gate ``until`` roll-forward (not a full exchange calendar).
# Update yearly. Source: NSE circulars / trading holidays.
NSE_HOLIDAYS = frozenset(
    {
        # 2026
        "2026-01-26",  # Republic Day
        "2026-03-03",  # Holi
        "2026-03-26",  # Ram Navami (tentative — verify)
        "2026-03-31",  # Mahavir Jayanti (tentative)
        "2026-04-03",  # Good Friday
        "2026-04-14",  # Dr Ambedkar Jayanti
        "2026-05-01",  # Maharashtra Day
        "2026-05-28",  # Bakri Id (tentative)
        "2026-06-26",  # Muharram (tentative)
        "2026-08-15",  # Independence Day
        "2026-09-14",  # Ganesh Chaturthi (observed in some years)
        "2026-10-02",  # Gandhi Jayanti
        "2026-10-20",  # Dussehra (tentative)
        "2026-11-08",  # Diwali Laxmi Pujan (tentative)
        "2026-11-09",  # Diwali Balipratipada (tentative)
        "2026-11-24",  # Guru Nanak Jayanti (tentative)
        "2026-12-25",  # Christmas
    }
)


def _is_nse_trading_day(day) -> bool:
    if day.weekday() >= 5:
        return False
    return day.isoformat() not in NSE_HOLIDAYS

KNOWN_BOOKS = (
    "iron_fly",
    "vwap_long",
    "short_straddle",
    "skew_fade",
    "long_iron_condor",
    "short_iron_condor",
    "theta_cliff",
    "impulse_fade",
    "combo",
    "agent",
    "ict",
)

BOOK_ALIASES = {
    "short_iron_fly": "iron_fly",
    "fly": "iron_fly",
    "paper": "iron_fly",
    "atm_impulse_fade": "impulse_fade",
    "scalp": "impulse_fade",
    "combo_confluence": "combo",
    "ict_paper": "ict",
    "agent_paper": "agent",
    "paper_agent": "agent",
    "theta_cliff_fence": "theta_cliff",
    "theta-cliff": "theta_cliff",
    "fence": "theta_cliff",
    "short_ic": "short_iron_condor",
    "credit_iron_condor": "short_iron_condor",
    "credit_ic": "short_iron_condor",
}


def normalize_book(book: str) -> str:
    key = str(book or "").strip().lower()
    return BOOK_ALIASES.get(key, key)


def _as_ist(now: datetime | None = None) -> datetime:
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def next_session_end(now: datetime | None = None) -> datetime:
    """Next NSE cash close (15:30 IST on a trading day).

    Skips weekends and a small hard-coded NSE holiday set (extend yearly).
    """
    now = _as_ist(now)
    day = now.date()
    end = datetime.combine(day, SESSION_END, tzinfo=IST)
    if now >= end or not _is_nse_trading_day(day):
        day = day + timedelta(days=1)
        while not _is_nse_trading_day(day):
            day += timedelta(days=1)
        end = datetime.combine(day, SESSION_END, tzinfo=IST)
    return end


def session_end_until(now: datetime | None = None) -> str:
    """ISO timestamp for the next NSE cash close (15:30 IST)."""
    return next_session_end(now).isoformat()


def parse_gate_until(raw: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse gate expiry. Accepts ISO or HH:MM (today IST). None/blank → None.

    Raises ValueError for junk strings (``none``, ``end of day``, etc.).
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    now = _as_ist(now)

    hm = _HHMM_RE.match(text)
    if hm:
        hour, minute = int(hm.group(1)), int(hm.group(2))
        if hour > 23 or minute > 59:
            raise ValueError(f"invalid gate until: {raw!r}")
        return datetime.combine(now.date(), dt_time(hour, minute), tzinfo=IST)

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid gate until: {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


class AgentGateStore:
    """In-memory + JSON file gate store. Manual override always wins when set."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._gates: dict[str, dict[str, Any]] = {}
        self._manual: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        gates = raw.get("gates") if isinstance(raw.get("gates"), dict) else raw
        manual = raw.get("manual") if isinstance(raw.get("manual"), dict) else {}
        if isinstance(gates, dict):
            self._gates = {normalize_book(k): dict(v) for k, v in gates.items() if isinstance(v, dict)}
        if isinstance(manual, dict):
            self._manual = {
                normalize_book(k): dict(v) for k, v in manual.items() if isinstance(v, dict)
            }

    def _save(self) -> None:
        payload = {
            "updated_at": datetime.now(IST).isoformat(),
            "gates": self._gates,
            "manual": self._manual,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _expired(row: dict[str, Any], now: datetime) -> bool:
        until = row.get("until")
        if not until:
            # Missing expiry on a restrictive gate must not pause forever.
            mode = str(row.get("mode") or "allow").lower()
            return mode in ("pause", "skip_entries")
        try:
            dt = parse_gate_until(str(until), now=now)
        except ValueError:
            # Junk until (legacy) → treat as expired so entries are not blocked forever.
            return True
        if dt is None:
            mode = str(row.get("mode") or "allow").lower()
            return mode in ("pause", "skip_entries")
        return now >= dt

    def _effective_row(self, book: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(IST)
        key = normalize_book(book)
        manual = self._manual.get(key)
        if manual and not self._expired(manual, now):
            return {"book": key, "source": "manual", **manual}
        gate = self._gates.get(key)
        if gate and not self._expired(gate, now):
            src = str(gate.get("source") or "agent")
            body = {k: v for k, v in gate.items() if k != "source"}
            return {"book": key, "source": src, **body}
        return {"book": key, "mode": "allow", "source": "default", "until": None, "reason": None}

    def get(self, book: str, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            return self._effective_row(book, now)

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            now = now or datetime.now(IST)
            books = sorted(set(KNOWN_BOOKS) | set(self._gates) | set(self._manual))
            return {
                "ok": True,
                "path": str(self.path),
                "gates": {b: self._effective_row(b, now) for b in books},
            }

    def entries_allowed(self, book: str, now: datetime | None = None) -> bool:
        row = self.get(book, now=now)
        mode = str(row.get("mode") or "allow").lower()
        return mode == "allow"

    def set_gate(
        self,
        book: str,
        mode: GateMode,
        *,
        until: str | None = None,
        reason: str | None = None,
        source: str = "agent",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        key = normalize_book(book)
        if key not in KNOWN_BOOKS and key not in BOOK_ALIASES.values():
            # still allow unknown keys for forward compat
            pass
        mode_n = str(mode).strip().lower()
        if mode_n not in ("allow", "pause", "skip_entries"):
            raise ValueError(f"invalid gate mode: {mode}")
        now = _as_ist(now)

        until_iso: str | None
        if mode_n == "allow":
            if until:
                parsed_allow = parse_gate_until(until, now=now)
                until_iso = parsed_allow.isoformat() if parsed_allow else None
            else:
                until_iso = None
        else:
            parsed = parse_gate_until(until, now=now) if until else None
            # Missing or already-past until → next session close (after-hours pause must stick).
            if parsed is None or parsed <= now:
                until_iso = session_end_until(now)
            else:
                until_iso = parsed.isoformat()

        src = str(source or "agent").strip().lower() or "agent"
        row = {
            "mode": mode_n,
            "until": until_iso,
            "reason": (reason or "")[:300] or None,
            "set_at": now.isoformat(),
            "source": src,
        }
        with self._lock:
            if src == "manual":
                self._manual[key] = {k: v for k, v in row.items() if k != "source"}
            else:
                self._gates[key] = row
            self._save()
            return self._effective_row(key, now)

    def clear_manual(self, book: str | None = None) -> None:
        with self._lock:
            if book is None:
                self._manual.clear()
            else:
                self._manual.pop(normalize_book(book), None)
            self._save()
