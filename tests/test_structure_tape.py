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
    maintain_recordings,
    recording_files_for_day,
    safe_recording_path,
    sensex_structure_day_path,
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


def test_sensex_structure_goes_to_its_own_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_RECORD", "1")
    now = datetime(2026, 11, 10, 11, 4, tzinfo=IST)
    rec = SheetRecorder(tmp_path)
    asyncio.run(rec.append_structure({"hm": "11:04", "atm": 23450}, now=now))
    asyncio.run(rec.append_structure({"hm": "11:04", "atm": 80000}, now=now, sensex=True))
    sx = sensex_structure_day_path(tmp_path, now=now)
    assert sx.name == "sensex-structure-2026-11-10.jsonl" and STRUCTURE_NAME_RE.match(sx.name)
    assert '"atm":80000' in sx.read_text() and '"atm":80000' not in structure_day_path(tmp_path, now=now).read_text()
    # The NIFTY price builders read structure-*: the SENSEX file must not match that glob.
    assert [p.name for p in tmp_path.glob("structure-*.jsonl*")] == ["structure-2026-11-10.jsonl"]
    assert safe_recording_path(tmp_path, sx.name) == sx.resolve()


def test_todays_sensex_structure_is_not_compressed_while_live(tmp_path: Path) -> None:
    now = datetime(2026, 11, 10, 11, 4, tzinfo=IST)
    live = tmp_path / "sensex-structure-2026-11-10.jsonl"
    done = tmp_path / "sensex-structure-2026-11-06.jsonl"
    live.write_text("{}\n"); done.write_text("{}\n")
    maintain_recordings(tmp_path, now=now, keep_days=30)
    assert live.is_file() and (tmp_path / "sensex-structure-2026-11-06.jsonl.gz").is_file()


def test_engine_structure_snapshot_uses_strike_steps(tmp_path: Path) -> None:
    from datetime import date
    from types import SimpleNamespace

    from atlas_lite.feed_engine import FeedEngine

    class Uni:
        strike_step = 100
        expiry = date(2026, 11, 12)

        def option_symbol(self, k: int, opt: str) -> str:
            return f"BFO:SENSEX26N12{k}{opt}"

    nb = SimpleNamespace(enabled=True, universe=Uni(), atm=SimpleNamespace(strike=80000, ce_symbol="BFO:SENSEX26N1280000CE",
                         pe_symbol="BFO:SENSEX26N1280000PE"), next_universe=None)
    rows = {"BFO:SENSEX26N1280000CE": {"last_price": 300.0}, "BFO:SENSEX26N1280500PE": {"last_price": 500.0}}
    eng = SimpleNamespace(notebooks={"sensex": nb}, book=SimpleNamespace(get=rows.get), _spot=lambda _nb: 80010.0)
    snap = FeedEngine._structure_snapshot(eng, datetime(2026, 11, 10, 9, 16, tzinfo=IST), "sensex")
    assert sorted(snap["wings"]) == ["+100", "+200", "+500", "-100", "-200", "-500"]
    assert snap["ce"] == 300.0 and snap["wings"]["+500"]["pe"] == 500.0 and snap["atm"] == 80000
