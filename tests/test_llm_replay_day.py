"""Replay fills: never earlier than the decision moment (the 9 Nov look-ahead), ask/bid from the depth tape."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("llm_replay_day", Path(__file__).resolve().parents[1] / "scripts" / "llm_replay_day.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

PE = "NFO:NIFTY26N0924900PE"


def at(hm: str, sec: float = 0.0) -> float:
    return int(hm[:2]) * 3600 + int(hm[3:]) * 60 + sec


def test_fills_use_the_first_depth_quote_at_or_after_the_moment() -> None:
    depth = {PE: [(at("09:41", 1), 86.0, 87.0), (at("09:42", 4), 102.5, 103.25), (at("09:42", 9), 104.0, 105.0)]}
    q = R.ReplayQuotes(px={}, feed={}, depth=depth)
    q.at = at("09:42", 3)  # decision at 09:42:00 + 2 s answer + 1 s order
    row = q.get(PE)
    assert row["ask"] == 103.25 and row["bid"] == 102.5  # never the 09:41 quote seen before deciding


def test_without_depth_the_tape_minute_at_or_after_the_moment_is_used() -> None:
    px = {"09:42": {PE: 90.0}, "09:43": {PE: 100.0}}
    q = R.ReplayQuotes(px=px, feed={}, depth={})
    q.at = at("09:42")
    assert q.get(PE) == {"last_price": 90.0}  # stamped exactly at the moment
    q.at = at("09:42", 3)
    assert q.get(PE) == {"last_price": 100.0}  # 09:42:00 is already in the past: next stamp
    assert q.used["tape"] == 2


def test_the_finished_gz_wins_over_a_leftover_plain_copy(tmp_path: Path) -> None:
    for name in ("d_15-00.jsonl", "d_15-00.jsonl.gz", "d_14-30.jsonl"):
        (tmp_path / name).write_text("")
    got = [Path(p).name for p in R._day_files(str(tmp_path / "d_*.jsonl*"))]
    assert got == ["d_14-30.jsonl", "d_15-00.jsonl.gz"]
