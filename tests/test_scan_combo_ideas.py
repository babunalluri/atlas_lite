"""COMBO scanner: opposite letter / confluence lost; 5m uses bucket close."""

from __future__ import annotations

from scripts.scan_combo_ideas import _walk_option, tape_hm


def test_tape_hm_1m_is_bar_minute() -> None:
    assert tape_hm("2026-09-04 10:00", 1) == "10:00"


def test_tape_hm_5m_is_bucket_close() -> None:
    assert tape_hm("2026-09-04 10:00", 5) == "10:04"


def test_walk_ignores_same_letter_reprint() -> None:
    by_day = {
        "2026-09-04": {
            "10:24": {
                "date": "2026-09-04",
                "hm": "10:24",
                "ce": 100.0,
                "combo_signal": "B",
                "combo_side": "B",
                "combo_stamped": True,
            },
            "10:25": {
                "date": "2026-09-04",
                "hm": "10:25",
                "ce": 101.0,
                "combo_signal": "B",
                "combo_side": "B",
                "combo_stamped": True,
            },
            "10:26": {
                "date": "2026-09-04",
                "hm": "10:26",
                "ce": 102.0,
                "combo_signal": None,
                "combo_side": None,
                "combo_stamped": True,
            },
        }
    }
    px, hm, reason = _walk_option(
        by_day,
        {"date": "2026-09-04", "hm": "10:24", "ce": 100.0},
        "ce",
        "B",
        "10:36",
        0.08,
        -0.06,
    )
    assert reason == "confluence"
    assert hm == "10:26"
    assert px == 102.0


def test_walk_exits_on_opposite_letter() -> None:
    by_day = {
        "2026-09-04": {
            "10:24": {"date": "2026-09-04", "hm": "10:24", "ce": 100.0},
            "10:25": {
                "date": "2026-09-04",
                "hm": "10:25",
                "ce": 101.0,
                "combo_signal": "S",
                "combo_side": "S",
                "combo_stamped": True,
            },
        }
    }
    px, hm, reason = _walk_option(
        by_day,
        {"date": "2026-09-04", "hm": "10:24", "ce": 100.0},
        "ce",
        "B",
        "10:36",
        0.08,
        -0.06,
    )
    assert reason == "flip"
    assert hm == "10:25"
    assert px == 101.0
