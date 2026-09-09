"""Append-only JSONL recorder — one line per UI-visible change."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from atlas_lite.log_util import get_logger

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_RECORD_ROTATE_MIN = 30
DEFAULT_RECORD_START = (9, 0)
DEFAULT_RECORD_END = (15, 35)
RECORDING_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}\.jsonl$")
RECORDING_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_RECORDING_READ = 5000
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def record_enabled() -> bool:
    return os.environ.get("ATLAS_LITE_RECORD", "").lower() in ("1", "true", "yes")


def record_rotate_minutes() -> int:
    raw = os.environ.get("ATLAS_LITE_RECORD_ROTATE_MIN", str(DEFAULT_RECORD_ROTATE_MIN)).strip()
    try:
        minutes = int(raw)
    except ValueError:
        minutes = DEFAULT_RECORD_ROTATE_MIN
    return max(1, min(minutes, 60))


def record_dir(data_dir: Path) -> Path:
    raw = os.environ.get("ATLAS_LITE_RECORD_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return data_dir / "recordings"


def _parse_hhmm(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    match = _HHMM_RE.match(raw.strip())
    if not match:
        return default
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return default
    return hour, minute


def record_session_bounds() -> tuple[tuple[int, int], tuple[int, int]]:
    start = _parse_hhmm(
        os.environ.get("ATLAS_LITE_RECORD_START", "09:00"),
        DEFAULT_RECORD_START,
    )
    end = _parse_hhmm(
        os.environ.get("ATLAS_LITE_RECORD_END", "15:35"),
        DEFAULT_RECORD_END,
    )
    return start, end


def record_weekdays_only() -> bool:
    return os.environ.get("ATLAS_LITE_RECORD_WEEKDAYS_ONLY", "1").lower() in (
        "1",
        "true",
        "yes",
    )


def _seconds_of_day(hour: int, minute: int, second: int = 0) -> int:
    return hour * 3600 + minute * 60 + second


def is_record_session(now: datetime | None = None) -> bool:
    """NSE cash session window in IST (default 09:00–15:35, Mon–Fri)."""
    now = now or datetime.now(IST)
    if record_weekdays_only() and now.weekday() >= 5:
        return False
    start, end = record_session_bounds()
    now_s = _seconds_of_day(now.hour, now.minute, now.second)
    start_s = _seconds_of_day(start[0], start[1], 0)
    end_s = _seconds_of_day(end[0], end[1], 59)
    return start_s <= now_s <= end_s


def record_slot_path(base_dir: Path, *, now: datetime | None = None) -> Path:
    """IST file name for the current rotation window, e.g. 2026-09-02_10-30.jsonl."""
    now = now or datetime.now(IST)
    slot_minutes = record_rotate_minutes()
    minute = (now.minute // slot_minutes) * slot_minutes
    slot = now.replace(minute=minute, second=0, microsecond=0)
    return base_dir / f"{slot.strftime('%Y-%m-%d_%H-%M')}.jsonl"


def safe_recording_path(base_dir: Path, name: str) -> Path:
    if not RECORDING_NAME_RE.match(name):
        raise ValueError(f"invalid recording file name: {name}")
    root = base_dir.resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise ValueError("invalid recording file path")
    return path


def list_recording_files(base_dir: Path) -> list[dict[str, Any]]:
    if not base_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(base_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        out.append(
            {
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified_ms": int(stat.st_mtime * 1000),
            }
        )
    return out


def recording_files_for_day(base_dir: Path, day: str) -> list[Path]:
    """IST-day slots named YYYY-MM-DD_HH-MM.jsonl."""
    if not RECORDING_DAY_RE.match(day):
        raise ValueError(f"invalid recording day: {day}")
    if not base_dir.is_dir():
        return []
    return sorted(
        path
        for path in base_dir.glob(f"{day}_*.jsonl")
        if RECORDING_NAME_RE.match(path.name)
    )


def write_day_archive(base_dir: Path, day: str) -> tuple[Path, str]:
    """Build a zip of every slot for ``day``. Caller must delete the temp file."""
    files = recording_files_for_day(base_dir, day)
    if not files:
        raise FileNotFoundError(f"no recordings for {day}")
    filename = f"atlas-recordings-{day}.zip"
    handle = tempfile.NamedTemporaryFile(prefix="atlas-rec-", suffix=".zip", delete=False)
    handle.close()
    zip_path = Path(handle.name)
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, arcname=path.name)
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise
    return zip_path, filename


def _iter_jsonl_dicts(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _normalize_recording_filters(
    entry: str, gates: str, failing: str
) -> tuple[str, str, str, bool]:
    want_entry = str(entry or "all").strip().lower() or "all"
    want_gates = str(gates or "all").strip().lower() or "all"
    fail_want = str(failing or "").strip()
    filtered = want_entry not in ("all", "") or want_gates not in ("all", "") or bool(fail_want)
    return want_entry, want_gates, fail_want, filtered


def recording_row_matches(
    row: dict[str, Any],
    *,
    entry: str = "all",
    gates: str = "all",
    failing: str = "",
) -> bool:
    """Filter a recorded snapshot. entry: all|ready|not_ready. gates: all|7|6|5|le4."""
    ev = row.get("evaluation") if isinstance(row.get("evaluation"), dict) else {}
    ready = bool(ev.get("entry_ready"))
    want_entry = str(entry or "all").strip().lower()
    if want_entry == "ready" and not ready:
        return False
    if want_entry in ("not_ready", "not-ready", "wait") and ready:
        return False
    passed = 0
    raw_passed = ev.get("passed")
    try:
        if raw_passed is not None:
            passed = int(raw_passed)
    except (TypeError, ValueError):
        passed = 0
    want_gates = str(gates or "all").strip().lower()
    if want_gates == "7" and passed != 7:
        return False
    if want_gates == "6" and passed != 6:
        return False
    if want_gates == "5" and passed != 5:
        return False
    if want_gates in ("le4", "lte4", "<=4") and passed > 4:
        return False
    fail_want = str(failing or "").strip()
    if fail_want:
        fails = ev.get("failing_gates") or []
        if fail_want not in fails:
            return False
    return True


def read_recording_file(
    base_dir: Path,
    name: str,
    *,
    limit: int = 1000,
    offset: int = 0,
    entry: str = "all",
    gates: str = "all",
    failing: str = "",
) -> dict[str, Any]:
    path = safe_recording_path(base_dir, name)
    if not path.is_file():
        raise FileNotFoundError(name)
    limit = max(1, min(limit, MAX_RECORDING_READ))
    offset = max(0, offset)
    want_entry, want_gates, fail_want, filtered = _normalize_recording_filters(
        entry, gates, failing
    )
    entries, total = _collect_matching_rows(
        [path],
        limit=limit,
        offset=offset,
        want_entry=want_entry,
        want_gates=want_gates,
        fail_want=fail_want,
        filtered=filtered,
        tag_file=False,
    )
    return {
        "name": name,
        "scope": "file",
        "total": total,
        "offset": offset,
        "limit": limit,
        "entry": want_entry,
        "gates": want_gates,
        "failing": fail_want,
        "entries": entries,
    }


def read_recording_day(
    base_dir: Path,
    day: str,
    *,
    limit: int = 1000,
    offset: int = 0,
    entry: str = "all",
    gates: str = "all",
    failing: str = "",
) -> dict[str, Any]:
    """Filter every 30-minute slot for an IST day (YYYY-MM-DD)."""
    files = recording_files_for_day(base_dir, day)
    if not files:
        raise FileNotFoundError(day)
    limit = max(1, min(limit, MAX_RECORDING_READ))
    offset = max(0, offset)
    want_entry, want_gates, fail_want, filtered = _normalize_recording_filters(
        entry, gates, failing
    )
    entries, total = _collect_matching_rows(
        files,
        limit=limit,
        offset=offset,
        want_entry=want_entry,
        want_gates=want_gates,
        fail_want=fail_want,
        filtered=filtered,
        tag_file=True,
    )
    return {
        "day": day,
        "scope": "day",
        "files": [path.name for path in files],
        "file_count": len(files),
        "total": total,
        "offset": offset,
        "limit": limit,
        "entry": want_entry,
        "gates": want_gates,
        "failing": fail_want,
        "entries": entries,
    }


def _collect_matching_rows(
    paths: list[Path],
    *,
    limit: int,
    offset: int,
    want_entry: str,
    want_gates: str,
    fail_want: str,
    filtered: bool,
    tag_file: bool,
) -> tuple[list[dict[str, Any]], int]:
    entries: list[dict[str, Any]] = []
    total = 0
    for path in paths:
        for row in _iter_jsonl_dicts(path):
            if filtered and not recording_row_matches(
                row, entry=want_entry, gates=want_gates, failing=fail_want
            ):
                continue
            if total >= offset and len(entries) < limit:
                item = dict(row)
                if tag_file:
                    item["file"] = path.name
                entries.append(item)
            total += 1
    return entries, total


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


class SheetRecorder:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._log = get_logger("record")
        self._lines = 0
        self._lines_in_file = 0
        self._current_file: str | None = None
        self._log.info(
            "sheet recorder enabled dir=%s rotate_min=%d",
            self.base_dir,
            record_rotate_minutes(),
        )

    def _path_for_slot(self) -> Path:
        return record_slot_path(self.base_dir)

    async def append(self, frame: dict[str, Any]) -> None:
        if not is_record_session():
            return
        row = {
            "ts": datetime.now(IST).isoformat(timespec="milliseconds"),
            "ts_ms": int(time.time() * 1000),
            "spot": frame.get("spot"),
            "atm_strike": frame.get("atm_strike"),
            "feed": frame.get("feed"),
            "indices": frame.get("indices"),
            "evaluation": frame.get("evaluation"),
            "strategy_hint": frame.get("strategy_hint"),
            "live_warnings": frame.get("live_warnings"),
            "ticker": frame.get("ticker"),
        }
        line = json.dumps(row, default=str, separators=(",", ":")) + "\n"
        async with self._lock:
            path = self._path_for_slot()
            await asyncio.to_thread(_append_line, path, line)
            self._lines += 1
            if self._current_file != path.name:
                self._current_file = path.name
                self._lines_in_file = 1
                self._log.info("recording file=%s", path.name)
            else:
                self._lines_in_file += 1
            if self._lines_in_file % 500 == 0:
                self._log.info(
                    "recorded lines=%d file=%s total=%d",
                    self._lines_in_file,
                    path.name,
                    self._lines,
                )
