"""Account-level risk checks (shadow by default)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from atlas_lite.risk_manager import RiskManager

NOW = datetime.fromisoformat("2026-11-06T09:16:00+05:30")  # Friday


def close(day: str, pnl: float, reason: str = "time", live: bool = True, hm: str = "13:00") -> str:
    return json.dumps({"event": "close", "ts": f"{day}T{hm}:00+05:30", "day": day, "pnl": pnl, "reason": reason,
                       "live": live, "strategy": "long_straddle"})


def ledger(tmp_path: Path, *lines: str) -> None:
    (tmp_path / "paper_long_straddle.jsonl").write_text("\n".join(lines) + "\n")


def test_clean_account_passes_and_logs(tmp_path) -> None:
    out = RiskManager(tmp_path, enforce=False).check("long_straddle", NOW)
    assert out["ok"] is True and out["blocks"] is False and out["day_pnl"] == 0
    logged = [json.loads(x) for x in (tmp_path / "risk_checks.jsonl").read_text().splitlines()]
    assert logged[-1]["book"] == "long_straddle"


def test_week_stop_counts_live_closes_since_monday_only(tmp_path) -> None:
    ledger(tmp_path, close("2026-10-30", -9000),  # last week: ignored
           close("2026-11-03", -2500), close("2026-11-04", -2600), close("2026-11-05", 400, live=False))
    out = RiskManager(tmp_path, enforce=False).check("long_straddle", NOW)
    assert out["week_pnl"] == -5100 and "week_stop:-5100" in out["reasons"]
    assert out["ok"] is False and out["blocks"] is False  # shadow: logged, never blocks


def test_day_stop_includes_arb_and_blocks_when_enforced(tmp_path) -> None:
    (tmp_path / "arb").mkdir()
    (tmp_path / "arb" / "live-state-2026-11-06.json").write_text(json.dumps({"day": "2026-11-06", "day_pnl": -2100}))
    out = RiskManager(tmp_path, enforce=True).check("long_straddle", NOW)
    assert out["day_pnl"] == -2100 and out["blocks"] is True and any(r.startswith("day_stop") for r in out["reasons"])


def test_kill_rule_three_stops_in_a_row_and_reset(tmp_path) -> None:
    ledger(tmp_path, close("2026-10-20", -900, "stop"), close("2026-10-21", -900, "stop"),
           close("2026-10-22", -900, "stop"))
    rm = RiskManager(tmp_path, enforce=False)
    assert rm.kill_reason("long_straddle") == "kill:3 stops in a row"
    (tmp_path / "risk_reset.json").write_text(json.dumps({"long_straddle": "2026-10-23T00:00:00+05:30"}))
    assert rm.kill_reason("long_straddle") is None


def test_kill_rule_loss_beyond_backtest_worst(tmp_path) -> None:
    ledger(tmp_path, close("2026-10-20", -4200))
    assert "beyond backtest worst" in RiskManager(tmp_path).kill_reason("long_straddle")


def test_event_day_blocks_selling_only(tmp_path) -> None:
    (tmp_path / "event_days.json").write_text(json.dumps({"days": {"2026-11-06": "test event"}}))
    rm = RiskManager(tmp_path, enforce=True)
    assert rm.check("long_straddle", NOW)["ok"] is True
    out = rm.check("scalper_v3", NOW, selling=True)
    assert out["blocks"] is True and out["reasons"] == ["event_day:test event"]


def test_shadow_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ATLAS_LITE_RISK_ENFORCE", raising=False)
    assert RiskManager(tmp_path).enforce is False
    monkeypatch.setenv("ATLAS_LITE_RISK_ENFORCE", "1")
    assert RiskManager(tmp_path).enforce is True


def test_built_in_event_calendar(tmp_path) -> None:
    from datetime import date

    assert RiskManager(tmp_path).event_today(date(2027, 2, 1)) == "Union Budget"
    assert RiskManager(tmp_path).event_today(date(2027, 2, 2)) is None


def test_a_live_close_not_yet_on_disk_still_counts(tmp_path) -> None:
    ledger(tmp_path, close("2026-11-05", -3000))
    pending = [{"event": "close", "day": "2026-11-06", "ts": "2026-11-06T13:00:00+05:30", "pnl": -2500.0,
                "reason": "time", "live": True, "strategy": "long_straddle"}]
    rm = RiskManager(tmp_path, enforce=True, pending=lambda: pending)
    out = rm.check("long_straddle", NOW)
    assert out["day_pnl"] == -2500 and out["week_pnl"] == -5500 and out["blocks"] is True
    assert set(out["reasons"]) == {"day_stop:-2500", "week_stop:-5500"}
    pending.clear()  # written to the ledger: counted once, from the file
    assert RiskManager(tmp_path, pending=lambda: pending).check("long_straddle", NOW)["week_pnl"] == -3000


def test_kill_counts_only_stop_exits(tmp_path) -> None:
    ledger(tmp_path, close("2026-10-20", -900, "stop"), close("2026-10-21", -900, "time"),
           close("2026-10-22", -900, "stop_confirmed"), close("2026-10-23", -900, "stop"))
    rm = RiskManager(tmp_path)
    assert rm.kill_reason("long_straddle") is None  # time exit breaks the run of three
    ledger(tmp_path, close("2026-10-21", -900, "stop_confirmed"), close("2026-10-22", -900, "stop"),
           close("2026-10-23", -900, "STOP"))
    assert rm.kill_reason("long_straddle") == "kill:3 stops in a row"
