"""Once-a-minute options structure tape (wings + next-week ATM)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite.recorder import (
    RECORDING_NAME_RE,
    STRUCTURE_NAME_RE,
    SheetRecorder,
    list_recording_files,
    recording_files_for_day,
    safe_recording_path,
    structure_day_path,
    write_day_archive,
)

IST = ZoneInfo("Asia/Kolkata")


def test_structure_day_path_and_name() -> None:
    now = datetime(2026, 9, 23, 11, 4, tzinfo=IST)
    path = structure_day_path(Path("/tmp/rec"), now=now)
    assert path.name == "structure-2026-09-23.jsonl"
    assert STRUCTURE_NAME_RE.match(path.name)
    assert not RECORDING_NAME_RE.match(path.name)
    assert safe_recording_path(Path("/tmp/rec"), path.name).name == path.name


def test_sheet_day_list_excludes_structure(tmp_path: Path) -> None:
    day = "2026-09-23"
    slot = tmp_path / f"{day}_11-00.jsonl"
    extra = tmp_path / f"structure-{day}.jsonl"
    slot.write_text("{}\n", encoding="utf-8")
    extra.write_text("{}\n", encoding="utf-8")
    names = [p.name for p in recording_files_for_day(tmp_path, day)]
    assert names == [f"{day}_11-00.jsonl"]


def test_history_list_puts_slots_before_structure(tmp_path: Path) -> None:
    day = "2026-09-23"
    structure = tmp_path / f"structure-{day}.jsonl"
    slot = tmp_path / f"{day}_11-00.jsonl"
    structure.write_text("{}\n", encoding="utf-8")
    import time as _time

    _time.sleep(0.02)
    slot.write_text("{}\n", encoding="utf-8")
    # Touch structure later so mtime alone would put it first.
    _time.sleep(0.02)
    structure.write_text("{}\n{}\n", encoding="utf-8")
    names = [row["name"] for row in list_recording_files(tmp_path)]
    assert names[0] == slot.name
    assert names[-1] == structure.name


def test_day_archive_includes_structure(tmp_path: Path) -> None:
    import zipfile

    day = "2026-09-23"
    (tmp_path / f"{day}_11-00.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / f"structure-{day}.jsonl").write_text("{}\n", encoding="utf-8")
    zip_path, _name = write_day_archive(tmp_path, day)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
        assert f"{day}_11-00.jsonl" in names
        assert f"structure-{day}.jsonl" in names
    finally:
        zip_path.unlink(missing_ok=True)


def test_append_structure_one_line(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_RECORD", "1")
    now = datetime(2026, 9, 23, 11, 4, tzinfo=IST)
    rec = SheetRecorder(tmp_path)
    asyncio.run(rec.append_structure({"hm": "11:04", "atm": 23450}, now=now))
    path = structure_day_path(tmp_path, now=now)
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert '"atm":23450' in lines[0]
