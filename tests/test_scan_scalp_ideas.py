"""Impulse-fade scanner: opposite wing, skip the entry minute."""

from __future__ import annotations

from scripts.scan_scalp_ideas import _impulse, _walk_hold, run_impulse


def test_impulse_uses_lookback_closes() -> None:
    rows = [
        {"spot": 100.0},
        {"spot": 104.0},
        {"spot": 108.0},
        {"spot": 113.0},
    ]
    assert _impulse(rows, 3, 3) == 13.0
    assert _impulse(rows, 2, 3) is None


def test_walk_hold_hits_target_after_entry() -> None:
    by_day = {
        "2026-09-04": {
            "10:24": {"date": "2026-09-04", "hm": "10:24", "pe": 100.0},
            "10:25": {"date": "2026-09-04", "hm": "10:25", "pe": 101.0},
            "10:26": {"date": "2026-09-04", "hm": "10:26", "pe": 109.0},
        }
    }
    entry = {"date": "2026-09-04", "hm": "10:24", "pe": 100.0}
    px, hm, reason = _walk_hold(by_day, entry, "pe", 1, 12, 0.08, -0.06)
    assert reason == "target"
    assert hm == "10:26"
    assert px == 109.0


def test_run_impulse_fades_up_move() -> None:
    by_day = {
        "2026-09-04": {
            "10:21": {"date": "2026-09-04", "hm": "10:21", "spot": 100.0, "ce": 80.0, "pe": 100.0},
            "10:22": {"date": "2026-09-04", "hm": "10:22", "spot": 104.0, "ce": 84.0, "pe": 96.0},
            "10:23": {"date": "2026-09-04", "hm": "10:23", "spot": 108.0, "ce": 88.0, "pe": 92.0},
            "10:24": {"date": "2026-09-04", "hm": "10:24", "spot": 113.0, "ce": 94.0, "pe": 100.0},
            "10:25": {"date": "2026-09-04", "hm": "10:25", "spot": 110.0, "ce": 90.0, "pe": 109.0},
        }
    }
    trades = run_impulse(
        by_day,
        lookback=3,
        min_move=12,
        hold_min=12,
        target=0.08,
        stop=-0.06,
        max_day=4,
        cooldown=8,
        fade=True,
    )
    assert len(trades) == 1
    assert trades[0]["side"] == "pe"
    assert trades[0]["reason"] == "target"
