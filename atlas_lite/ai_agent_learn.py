"""Refit the AI agent once after the cash session. No broker orders.

The scorecard agent is not involved. Today's tape is merged with any Kite rows
already saved. The new file is written beside the AI agent ledger. A fit that
does not have enough days leaves the previous model in place.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from atlas_lite.ai_agent_model import MODEL_NAME, save_model, walk_forward
from atlas_lite.recorder import iter_jsonl_dicts, list_slot_recording_paths

IST = ZoneInfo("Asia/Kolkata")
# Recordings run until 15:35. Learn after that file has been written.
LEARN_AFTER = (15, 40)
STAMP_NAME = "ai_agent_learn_day.txt"
KITE_ROWS_NAME = "ai_agent_kite_rows.json"


def learn_due(now: datetime, trained_day: str) -> bool:
    """True once, after 15:40 IST, on a weekday that has not been fit yet."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    if now.weekday() >= 5:
        return False
    if (now.hour, now.minute) < LEARN_AFTER:
        return False
    return trained_day != now.strftime("%Y-%m-%d")


def read_learn_day(data_dir: Path) -> str:
    path = Path(data_dir) / STAMP_NAME
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def load_tape_rows(rec_dir: Path) -> list[dict[str, Any]]:
    """One row per IST minute from the option tape. Last write in that minute wins."""
    by_min: dict[tuple[str, str], dict[str, Any]] = {}
    if not rec_dir.is_dir():
        return []
    for path in list_slot_recording_paths(rec_dir):
        for obj in iter_jsonl_dicts(path):
            ts = obj.get("ts")
            if not isinstance(ts, str):
                continue
            dt = _parse_ts(ts)
            if dt is None:
                continue
            feed = obj.get("feed") if isinstance(obj.get("feed"), dict) else {}
            key = (dt.date().isoformat(), dt.strftime("%H:%M"))
            atm = feed.get("atm")
            try:
                atm_i = int(atm) if atm not in (None, "") else None
            except (TypeError, ValueError):
                atm_i = None
            by_min[key] = {
                "date": key[0],
                "hm": key[1],
                "atm": atm_i,
                "ce": _num(feed.get("ce")),
                "pe": _num(feed.get("pe")),
                "spot": _num(obj.get("spot") or feed.get("nifty_ltp")),
                "atr": _num(feed.get("atr")),
                "adx": _num(feed.get("adx")),
                "pcr": _num(feed.get("pcr")),
                "nifty_chg": _num(feed.get("index_nifty_chg")),
                "expiry": str(feed.get("expiry") or "")[:10] or None,
                "source": "tape",
            }
    return [by_min[key] for key in sorted(by_min)]


def merged_rows(data_dir: Path, rec_dir: Path) -> list[dict[str, Any]]:
    """Kite history first, then the tape overwrites the same minute."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    kite_path = Path(data_dir) / KITE_ROWS_NAME
    if kite_path.is_file():
        try:
            raw = json.loads(kite_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = []
        if isinstance(raw, list):
            for row in raw:
                if isinstance(row, dict) and row.get("date") and row.get("hm"):
                    merged[(str(row["date"]), str(row["hm"]))] = row
    for row in load_tape_rows(rec_dir):
        merged[(row["date"], row["hm"])] = row
    return [merged[key] for key in sorted(merged)]


def run_daily_learn(data_dir: Path, rec_dir: Path) -> dict[str, Any]:
    """Fit and save. A too-small sample does not replace the model already on disk."""
    data_dir = Path(data_dir)
    rows = merged_rows(data_dir, Path(rec_dir))
    report = walk_forward(rows)
    # The fit may say promoted. A person still has to approve that file before the book opens.
    if report.get("reason") != "not_enough_days":
        save_model(data_dir / MODEL_NAME, report)
    day = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        (data_dir / STAMP_NAME).write_text(day + "\n", encoding="utf-8")
    except OSError:
        pass
    report["learned_day"] = day
    report["rows"] = len(rows)
    return report
