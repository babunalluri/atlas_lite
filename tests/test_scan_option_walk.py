"""Honest walk rules for the option idea scanner."""

from __future__ import annotations

from scripts.scan_option_ideas import _settle, _walk


def test_walk_skips_entry_minute_stop() -> None:
    by_day = {
        "2026-09-04": {
            "09:30": {"date": "2026-09-04", "hm": "09:30", "ce": 128.0},
            "09:31": {"date": "2026-09-04", "hm": "09:31", "ce": 130.0},
            "15:14": {"date": "2026-09-04", "hm": "15:14", "ce": 130.0},
        }
    }
    entry = {"date": "2026-09-04", "hm": "09:30", "ce": 128.0}
    px, hm, reason = _walk(by_day, entry, "ce", 1, "15:14", None, -0.14)
    assert reason == "time"
    assert hm == "15:14"
    assert px == 130.0


def test_walk_settles_cutoff_tape() -> None:
    by_day = {
        "2026-09-04": {
            "13:00": {"date": "2026-09-04", "hm": "13:00", "ce": 100.0},
            "13:10": {"date": "2026-09-04", "hm": "13:10", "ce": 128.0},
        }
    }
    entry = {"date": "2026-09-04", "hm": "13:00", "ce": 100.0}
    px, hm, reason = _walk(by_day, entry, "ce", 1, "15:14", None, None)
    assert reason == "tape_end"
    assert hm == "13:10"
    assert px == 128.0


def test_settle_excludes_tape_end_from_headline() -> None:
    trades = [
        {"pnl": 100.0, "reason": "time"},
        {"pnl": 50.0, "reason": "tape_end"},
    ]
    headline = _settle(trades, exclude=("tape_end",))
    assert headline["n"] == 1
    assert headline["pnl"] == 100.0
    cut = _settle([t for t in trades if t["reason"] == "tape_end"])
    assert cut["n"] == 1
    assert cut["pnl"] == 50.0
