"""Recording JSONL filters for history (Entry READY, gates)."""

from __future__ import annotations

import json
from pathlib import Path

from atlas_lite.recorder import read_recording_day, read_recording_file, recording_row_matches


def _row(*, ready: bool, passed: int, failing: list[str] | None = None) -> dict:
    return {
        "evaluation": {
            "entry_ready": ready,
            "passed": passed,
            "gates_total": 7,
            "failing_gates": failing or [],
        }
    }


def test_recording_row_matches_entry_ready() -> None:
    ready = _row(ready=True, passed=7)
    wait = _row(ready=False, passed=3, failing=["PCR"])
    assert recording_row_matches(ready, entry="ready") is True
    assert recording_row_matches(wait, entry="ready") is False
    assert recording_row_matches(wait, entry="not_ready") is True
    assert recording_row_matches(ready, entry="not_ready") is False
    assert recording_row_matches(wait, entry="all") is True


def test_recording_row_matches_gates_and_failing() -> None:
    six = _row(ready=False, passed=6, failing=["PCR"])
    four = _row(ready=False, passed=4, failing=["PCR", "ADX"])
    assert recording_row_matches(six, gates="6") is True
    assert recording_row_matches(six, gates="7") is False
    assert recording_row_matches(four, gates="le4") is True
    assert recording_row_matches(six, gates="le4") is False
    assert recording_row_matches(six, failing="PCR") is True
    assert recording_row_matches(six, failing="ADX") is False


def test_read_recording_file_filters_ready(tmp_path: Path) -> None:
    path = tmp_path / "2026-09-09_15-30.jsonl"
    lines = [
        {"ts": "a", **_row(ready=False, passed=3, failing=["PCR"])},
        {"ts": "b", **_row(ready=True, passed=7)},
        {"ts": "c", **_row(ready=False, passed=6, failing=["PCR"])},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in lines), encoding="utf-8")
    ready = read_recording_file(tmp_path, path.name, entry="ready")
    assert ready["total"] == 1
    assert ready["entries"][0]["ts"] == "b"
    six = read_recording_file(tmp_path, path.name, gates="6")
    assert six["total"] == 1
    assert six["entries"][0]["ts"] == "c"
    pcr = read_recording_file(tmp_path, path.name, failing="PCR")
    assert pcr["total"] == 2
    all_rows = read_recording_file(tmp_path, path.name)
    assert all_rows["total"] == 3
    assert all_rows["scope"] == "file"


def test_read_recording_day_ready_spans_slots(tmp_path: Path) -> None:
    morning = tmp_path / "2026-09-09_09-00.jsonl"
    lunch = tmp_path / "2026-09-09_09-30.jsonl"
    other = tmp_path / "2026-09-04_10-00.jsonl"
    morning.write_text(
        json.dumps({"ts": "m1", **_row(ready=False, passed=2, failing=["PCR"])}) + "\n",
        encoding="utf-8",
    )
    lunch.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [
                {"ts": "l1", **_row(ready=False, passed=6, failing=["PCR"])},
                {"ts": "l2", **_row(ready=True, passed=7)},
            ]
        ),
        encoding="utf-8",
    )
    other.write_text(
        json.dumps({"ts": "o1", **_row(ready=True, passed=7)}) + "\n",
        encoding="utf-8",
    )
    ready = read_recording_day(tmp_path, "2026-09-09", entry="ready")
    assert ready["scope"] == "day"
    assert ready["day"] == "2026-09-09"
    assert ready["file_count"] == 2
    assert ready["total"] == 1
    assert ready["entries"][0]["ts"] == "l2"
    assert ready["entries"][0]["file"] == "2026-09-09_09-30.jsonl"
    six = read_recording_day(tmp_path, "2026-09-09", gates="6")
    assert six["total"] == 1
    assert six["entries"][0]["ts"] == "l1"
    page = read_recording_day(tmp_path, "2026-09-09", limit=1, offset=1)
    assert page["total"] == 3
    assert page["entries"][0]["ts"] == "l1"
