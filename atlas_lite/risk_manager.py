"""Account-level risk checks for live entries (doc/trader-design.md). Shadow by default.

Before a live book sends its entry orders it asks check(); each answer is written to
data/risk_checks.jsonl. In shadow mode (default) the answer is logged and never blocks;
ATLAS_LITE_RISK_ENFORCE=1 makes a "no" keep the book on paper for that entry.

Rules (limits set by the operator, 6 Nov 2026):

- Day stop: live P&L closed today across all live books <= -DAY_STOP (Rs 2,000) -> no new live entries.
- Week stop: live P&L closed since Monday <= -WEEK_STOP (Rs 5,000) -> no new live entries
  until the next week (or a reset).
- Kill rule per book: its last 3 live closes were all stops, or one live loss was worse than
  its backtest's worst day -> that book stays paper until reset (data/risk_reset.json).
- Event days (data/event_days.json): no new selling entries; buying books are not blocked.

Live P&L comes from the books' own ledgers (close lines marked live), live closes a book holds in
memory because their ledger line could not be written yet (``pending``), and the NSE-BSE arb's day
state files. An open live trade is not counted until it closes. A "stop-out" for the kill rule is a
close whose reason contains "stop"; time, target, trail and broker square-off exits are not.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from atlas_lite.log_util import get_logger

IST = ZoneInfo("Asia/Kolkata")
DAY_STOP = 2_000.0
WEEK_STOP = 5_000.0
KILL_STOPS_IN_A_ROW = 3
LOG_NAME = "risk_checks.jsonl"
EVENTS_NAME = "event_days.json"
RESET_NAME = "risk_reset.json"

# book -> (ledger, worst backtest day in Rs, as a positive number)
LIVE_BOOKS: dict[str, tuple[str, float]] = {
    "long_straddle": ("paper_long_straddle.jsonl", 4_141.0),  # real prices 3 Sep-30 Oct, nearest strike
    "agent_paper": ("paper_agent.jsonl", 2_000.0),  # its own day-loss stop
}
ARB_STATE_GLOB = "arb/live-state-*.json"

# Selling-book blackout days (IST trading day the news hits). data/event_days.json adds to these.
# Sources checked 6 Nov 2026: RBI FY27 MPC calendar; Fed FOMC 2026-27 calendar (decision ~00:00 IST,
# so the Indian session after it); Union Budget on 1 Feb.
EVENT_DAYS: dict[str, str] = {
    "2026-12-04": "RBI policy",
    "2026-12-10": "US FOMC decision (overnight)",
    "2027-01-28": "US FOMC decision (overnight)",
    "2027-02-01": "Union Budget",
    "2027-02-05": "RBI policy",
}


def risk_enforced() -> bool:
    return os.environ.get("ATLAS_LITE_RISK_ENFORCE", "0").strip().lower() in ("1", "true", "yes", "on")


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


@dataclass
class RiskManager:
    data_dir: Path
    day_stop: float = DAY_STOP
    week_stop: float = WEEK_STOP
    enforce: bool = field(default_factory=risk_enforced)
    # Live close events not yet on disk (each with its "strategy"); set by the engine.
    pending: Callable[[], list[dict[str, Any]]] | None = None
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.risk_manager"), repr=False)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)

    # ---- inputs ----

    def _live_closes(self, book: str) -> list[dict[str, Any]]:
        ledger, _worst = LIVE_BOOKS[book]
        path = self.data_dir / ledger
        out: list[dict[str, Any]] = []
        lines: list[str] = []
        if path.is_file():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
        events: list[Any] = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if self.pending is not None:
            events += [ev for ev in self.pending() if isinstance(ev, dict) and ev.get("strategy") == book]
        for ev in events:
            if not isinstance(ev, dict) or ev.get("event") != "close" or not ev.get("live"):
                continue
            if ev.get("strategy", book) != book or ev.get("pnl") is None:
                continue
            out.append(ev)
        return out

    def _arb_pnl(self, start: date, end: date) -> float:
        total = 0.0
        for path in self.data_dir.glob(ARB_STATE_GLOB):
            try:
                st = json.loads(path.read_text(encoding="utf-8"))
                d = date.fromisoformat(str(st.get("day")))
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                continue
            if start <= d <= end:
                total += float(st.get("day_pnl") or 0.0)
        return total

    def live_pnl(self, start: date, end: date) -> float:
        total = self._arb_pnl(start, end)
        for book in LIVE_BOOKS:
            for ev in self._live_closes(book):
                try:
                    d = date.fromisoformat(str(ev.get("day") or str(ev.get("ts", ""))[:10]))
                except ValueError:
                    continue
                if start <= d <= end:
                    total += float(ev["pnl"])
        return round(total, 2)

    def _reset_at(self, book: str) -> str:
        """ISO time after which the book's live closes count for the kill rule (operator reset)."""
        try:
            raw = json.loads((self.data_dir / RESET_NAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str((raw or {}).get(book) or "")

    def kill_reason(self, book: str) -> str | None:
        if book not in LIVE_BOOKS:
            return None
        since = self._reset_at(book)
        closes = [ev for ev in self._live_closes(book) if str(ev.get("ts", "")) > since]
        worst = LIVE_BOOKS[book][1]
        for ev in closes:
            if float(ev["pnl"]) < -worst:
                return f"kill:loss {ev['pnl']} on {ev.get('day')} beyond backtest worst -{worst:.0f}"
        last = closes[-KILL_STOPS_IN_A_ROW:]
        if len(last) == KILL_STOPS_IN_A_ROW and all("stop" in str(ev.get("reason") or "").lower() for ev in last):
            return f"kill:{KILL_STOPS_IN_A_ROW} stops in a row"
        return None

    def event_today(self, today: date) -> str | None:
        try:
            raw = json.loads((self.data_dir / EVENTS_NAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        days = raw.get("days", raw) if isinstance(raw, dict) else {}
        what = (days.get(today.isoformat()) if isinstance(days, dict) else None) or EVENT_DAYS.get(today.isoformat())
        return str(what) if what else None

    # ---- the check ----

    def check(self, book: str, now: datetime, *, selling: bool = False) -> dict[str, Any]:
        """Never raises. Returns {"ok", "blocks", "reasons", "enforced", "day_pnl", "week_pnl"}."""
        try:
            today = now.astimezone(IST).date()
            day_pnl = self.live_pnl(today, today)
            week_pnl = self.live_pnl(_week_start(today), today)
            reasons = []
            if day_pnl <= -abs(self.day_stop):
                reasons.append(f"day_stop:{day_pnl:.0f}")
            if week_pnl <= -abs(self.week_stop):
                reasons.append(f"week_stop:{week_pnl:.0f}")
            kill = self.kill_reason(book)
            if kill:
                reasons.append(kill)
            event = self.event_today(today) if selling else None
            if event:
                reasons.append(f"event_day:{event}")
        except Exception as exc:  # noqa: BLE001 - a broken check is logged, and blocks only when enforced
            day_pnl = week_pnl = None
            reasons = [f"check_failed:{exc}"]
        out = {
            "ts": now.isoformat(), "book": book, "selling": selling, "ok": not reasons, "reasons": reasons,
            "enforced": self.enforce, "blocks": bool(reasons) and self.enforce,
            "day_pnl": day_pnl, "week_pnl": week_pnl,
            "limits": {"day": self.day_stop, "week": self.week_stop},
        }
        try:
            with (self.data_dir / LOG_NAME).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(out, separators=(",", ":")) + "\n")
        except OSError as exc:
            self._log.warning("risk check log write failed: %s", exc)
        if reasons:
            self._log.warning("risk check %s %s: %s", book, "BLOCKS" if out["blocks"] else "would block (shadow)", reasons)
        return out
