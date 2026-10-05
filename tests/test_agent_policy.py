"""Lean regime → books policy (allocation + kill switches)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite.agent_gates import AgentGateStore
from atlas_lite.agent_policy import (
    apply_book_policy,
    classify_regime,
    day_close_stats,
    kill_switch_hit,
    regime_book_modes,
)

IST = ZoneInfo("Asia/Kolkata")


def test_classify_regime_bands() -> None:
    assert classify_regime(25.0) == "trend"
    assert classify_regime(15.0) == "range"
    assert classify_regime(20.0) == "mixed"
    assert classify_regime(None, "trend") == "trend"
    assert classify_regime(None, None) == "mixed"


def test_regime_book_modes_prefer_evidence() -> None:
    trend = regime_book_modes("trend")
    assert trend["combo"] == "allow"
    assert trend["iron_fly"] == "allow"  # evidence book — never regime-skipped
    assert trend["short_straddle"] == "skip_entries"
    assert trend["theta_cliff"] == "skip_entries"
    assert trend["skew_fade"] == "skip_entries"

    ranging = regime_book_modes("range")
    assert ranging["iron_fly"] == "allow"
    assert ranging["theta_cliff"] == "allow"
    assert ranging["combo"] == "skip_entries"
    assert ranging["skew_fade"] == "skip_entries"

    mixed = regime_book_modes("mixed")
    assert mixed["iron_fly"] == "allow"
    assert mixed["theta_cliff"] == "allow"
    assert mixed["combo"] == "skip_entries"


def test_day_close_stats_morning_losses_and_streak(tmp_path: Path) -> None:
    path = tmp_path / "paper_combo.jsonl"
    day = "2026-10-01"
    rows = [
        {
            "event": "close",
            "day": day,
            "pnl": -400.0,
            "day_pnl": -400.0,
            "ts": "2026-10-01T09:50:00+05:30",
        },
        {
            "event": "close",
            "day": day,
            "pnl": -350.0,
            "day_pnl": -750.0,
            "ts": "2026-10-01T10:20:00+05:30",
        },
        {
            "event": "close",
            "day": day,
            "pnl": 100.0,
            "day_pnl": -650.0,
            "ts": "2026-10-01T13:00:00+05:30",
        },
        {
            "event": "close",
            "day": day,
            "pnl": -200.0,
            "day_pnl": -850.0,
            "ts": "2026-10-01T14:00:00+05:30",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    stats = day_close_stats(path, day=day)
    assert stats["n_closes"] == 4
    assert stats["morning_losses"] == 2
    assert stats["loss_streak"] == 1
    assert stats["day_pnl"] == -850.0
    assert kill_switch_hit(stats) == "morning_losses=2 day_pnl=-850.0"


def test_kill_switch_streak() -> None:
    stats = {
        "loss_streak": 3,
        "morning_losses": 0,
        "day_pnl": -900.0,
    }
    assert kill_switch_hit(stats) == "streak=3"
    assert kill_switch_hit({"loss_streak": 1, "morning_losses": 1, "day_pnl": -100.0}) is None


def test_day_close_stats_counts_short_ic_close_set(tmp_path: Path) -> None:
    """Kill switch must see Short IC PnL on close_set (flatten close has pnl=null)."""
    path = tmp_path / "paper_short_iron_condor.jsonl"
    day = "2026-10-05"
    rows = [
        {
            "event": "close_set",
            "day": day,
            "side": "ce",
            "pnl": -5766.0,
            "ts": "2026-10-05T10:10:00+05:30",
        },
        {
            "event": "close_set",
            "day": day,
            "side": "ce",
            "pnl": -6011.0,
            "ts": "2026-10-05T11:05:00+05:30",
        },
        {
            "event": "close_set",
            "day": day,
            "side": "ce",
            "pnl": -5574.0,
            "ts": "2026-10-05T12:40:00+05:30",
        },
        {
            "event": "close",
            "day": day,
            "pnl": None,
            "realized_pnl": -17351.0,
            "ts": "2026-10-05T12:40:01+05:30",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    stats = day_close_stats(path, day=day)
    assert stats["n_closes"] == 3
    assert stats["loss_streak"] == 3
    assert stats["day_pnl"] == -17351.0
    assert kill_switch_hit(stats) == "streak=3"


def test_day_close_stats_counts_theta_close_vertical(tmp_path: Path) -> None:
    """Kill switch must see theta_cliff PnL on close_vertical (seal close has pnl=null)."""
    path = tmp_path / "paper_theta_cliff.jsonl"
    day = "2026-10-06"
    rows = [
        {
            "event": "close_vertical",
            "day": day,
            "side": "ce",
            "pnl": -800.0,
            "day_pnl": -800.0,
            "ts": "2026-10-06T12:30:00+05:30",
        },
        {
            "event": "close_vertical",
            "day": day,
            "side": "pe",
            "pnl": -700.0,
            "day_pnl": -1500.0,
            "ts": "2026-10-06T13:10:00+05:30",
        },
        {
            "event": "close",
            "day": day,
            "pnl": None,
            "day_pnl": -1500.0,
            "ts": "2026-10-06T13:10:01+05:30",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    stats = day_close_stats(path, day=day)
    assert stats["n_closes"] == 2
    assert stats["n_losses"] == 2
    assert stats["day_pnl"] == -1500.0
    # Only 2 losses — streak pause needs 3; morning gate needs 2 before noon.
    assert kill_switch_hit(stats) is None

    # Third losing vertical trips the streak kill.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "event": "close_vertical",
                    "day": day,
                    "side": "ce",
                    "pnl": -500.0,
                    "day_pnl": -2000.0,
                    "ts": "2026-10-06T14:00:00+05:30",
                }
            )
            + "\n"
        )
    stats2 = day_close_stats(path, day=day)
    assert stats2["loss_streak"] == 3
    assert kill_switch_hit(stats2) == "streak=3"


def test_apply_book_policy_kill_short_ic_close_set(tmp_path: Path) -> None:
    gates = AgentGateStore(tmp_path / "gates.json")
    day = "2026-10-05"
    ledger = tmp_path / "paper_short_iron_condor.jsonl"
    rows = [
        {
            "event": "close_set",
            "day": day,
            "pnl": -1000.0,
            "ts": "2026-10-05T10:00:00+05:30",
        },
        {
            "event": "close_set",
            "day": day,
            "pnl": -1100.0,
            "ts": "2026-10-05T11:00:00+05:30",
        },
        {
            "event": "close_set",
            "day": day,
            "pnl": -1200.0,
            "ts": "2026-10-05T12:30:00+05:30",
        },
        {"event": "close", "day": day, "pnl": None, "ts": "2026-10-05T12:30:01+05:30"},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    now = datetime(2026, 10, 5, 13, 0, tzinfo=IST)
    apply_book_policy(
        gates,
        data_dir=tmp_path,
        adx=14.0,
        adx_regime="range",
        now=now,
    )
    row = gates.get("short_iron_condor", now=now)
    assert row["mode"] == "skip_entries"
    assert str(row["reason"]).startswith("policy:kill:streak=")


def test_apply_book_policy_range_skips_combo(tmp_path: Path) -> None:
    gates = AgentGateStore(tmp_path / "gates.json")
    now = datetime(2026, 10, 1, 11, 0, tzinfo=IST)
    out = apply_book_policy(
        gates,
        data_dir=tmp_path,
        adx=14.0,
        adx_regime="range",
        now=now,
    )
    assert out["regime"] == "range"
    assert gates.get("combo", now=now)["mode"] == "skip_entries"
    assert str(gates.get("combo", now=now)["reason"]).startswith("policy:regime:")
    assert gates.get("combo", now=now)["source"] == "policy"
    assert gates.get("iron_fly", now=now)["mode"] == "allow"
    assert gates.get("skew_fade", now=now)["mode"] == "skip_entries"
    # impulse / agent never touched
    assert gates.get("impulse_fade", now=now)["source"] == "default"
    assert gates.get("agent", now=now)["source"] == "default"


def test_apply_book_policy_kill_beats_trend_allow(tmp_path: Path) -> None:
    gates = AgentGateStore(tmp_path / "gates.json")
    day = "2026-10-01"
    ledger = tmp_path / "paper_combo.jsonl"
    rows = [
        {
            "event": "close",
            "day": day,
            "pnl": -400.0,
            "day_pnl": -400.0,
            "ts": "2026-10-01T09:45:00+05:30",
        },
        {
            "event": "close",
            "day": day,
            "pnl": -500.0,
            "day_pnl": -900.0,
            "ts": "2026-10-01T10:15:00+05:30",
        },
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    now = datetime(2026, 10, 1, 10, 30, tzinfo=IST)
    out = apply_book_policy(
        gates,
        data_dir=tmp_path,
        adx=28.0,
        adx_regime="trend",
        now=now,
    )
    assert out["regime"] == "trend"
    row = gates.get("combo", now=now)
    assert row["mode"] == "skip_entries"
    assert str(row["reason"]).startswith("policy:kill:")


def test_manual_override_wins(tmp_path: Path) -> None:
    gates = AgentGateStore(tmp_path / "gates.json")
    now = datetime(2026, 10, 1, 11, 0, tzinfo=IST)
    gates.set_gate("combo", "allow", source="manual", reason="user force", now=now)
    apply_book_policy(
        gates,
        data_dir=tmp_path,
        adx=14.0,
        now=now,
    )
    assert gates.get("combo", now=now)["mode"] == "allow"
    assert gates.get("combo", now=now)["source"] == "manual"


def test_kill_not_cleared_by_regime_flip(tmp_path: Path) -> None:
    gates = AgentGateStore(tmp_path / "gates.json")
    day = "2026-10-01"
    ledger = tmp_path / "paper_combo.jsonl"
    rows = [
        {
            "event": "close",
            "day": day,
            "pnl": -400.0,
            "day_pnl": -400.0,
            "ts": "2026-10-01T09:45:00+05:30",
        },
        {
            "event": "close",
            "day": day,
            "pnl": -500.0,
            "day_pnl": -900.0,
            "ts": "2026-10-01T10:15:00+05:30",
        },
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    morning = datetime(2026, 10, 1, 10, 30, tzinfo=IST)
    apply_book_policy(gates, data_dir=tmp_path, adx=28.0, now=morning)
    assert gates.get("combo", now=morning)["mode"] == "skip_entries"

    # Wipe ledger so kill condition is gone, but active kill must stick until expiry.
    ledger.write_text("", encoding="utf-8")
    later = datetime(2026, 10, 1, 13, 0, tzinfo=IST)
    apply_book_policy(gates, data_dir=tmp_path, adx=28.0, now=later)
    assert gates.get("combo", now=later)["mode"] == "skip_entries"
    assert str(gates.get("combo", now=later)["reason"]).startswith("policy:kill:")
