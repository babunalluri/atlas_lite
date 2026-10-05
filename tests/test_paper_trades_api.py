"""Paper trades list API — latest first, open/close only."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from atlas_lite.feed_engine import FeedEngine
from atlas_lite.kite_rest import KiteRest


def _engine(tmp_path: Path) -> FeedEngine:
    eng = FeedEngine(rest=MagicMock(spec=KiteRest), data_dir=tmp_path)
    return eng


def test_list_paper_trades_latest_first_with_entry_exit_info(tmp_path: Path) -> None:
    (tmp_path / "paper_impulse_fade.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-09-29T09:30:00+05:30",
                        "day": "2026-09-29",
                        "event": "open",
                        "strategy": "atm_impulse_fade",
                        "side": "ce",
                        "symbol": "NFO:NIFTY26SEP22650CE",
                        "entry": 50.0,
                        "impulse": 12.5,
                        "target": 55.0,
                        "stop": 45.0,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-29T09:31:00+05:30",
                        "day": "2026-09-29",
                        "event": "close",
                        "strategy": "atm_impulse_fade",
                        "side": "ce",
                        "symbol": "NFO:NIFTY26SEP22650CE",
                        "entry": 50.0,
                        "exit": 55.0,
                        "pnl": 300.0,
                        "reason": "target",
                        "target": 55.0,
                        "stop": 45.0,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-29T09:32:00+05:30",
                        "day": "2026-09-29",
                        "event": "day_pnl",
                        "strategy": "atm_impulse_fade",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "paper_combo.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-09-29T09:45:00+05:30",
                "day": "2026-09-29",
                "event": "open",
                "strategy": "combo_confluence",
                "side": "pe",
                "symbol": "NFO:NIFTY26SEP22650PE",
                "entry": 40.0,
                "letter": "S",
                "bull": 2,
                "bear": 4,
                "target": 44.0,
                "stop": 36.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    out = _engine(tmp_path).list_paper_trades(limit=10)
    assert out["ok"] is True
    assert out["count"] == 2  # paired impulse close + still-open combo
    trades = out["trades"]
    assert [t["event"] for t in trades] == ["open", "close"]
    assert trades[0]["strategy"] == "combo_confluence"
    assert "letter=S" in trades[0]["info"]
    assert "votes=B2/S4" in trades[0]["info"]
    assert trades[1]["reason"] == "target"
    assert "impulse=12.5" in trades[1]["info"]
    assert "exit=target" in trades[1]["info"]
    assert out["pnl_total"] == 300.0
    assert out["closed_count"] == 1
    assert out["open_count"] == 1
    by = {(r["day"], r["strategy"]): r for r in out["by_book_day"]}
    assert by[("2026-09-29", "atm_impulse_fade")]["pnl_total"] == 300.0
    assert by[("2026-09-29", "combo_confluence")]["open"] == 1


def test_list_paper_trades_day_filter(tmp_path: Path) -> None:
    path = tmp_path / "paper_trades.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-09-28T10:00:00+05:30",
                        "day": "2026-09-28",
                        "event": "open",
                        "strategy": "short_iron_fly",
                        "gates": "fly",
                        "atm": 23000,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-29T10:00:00+05:30",
                        "day": "2026-09-29",
                        "event": "open",
                        "strategy": "short_iron_fly",
                        "gates": "fly",
                        "atm": 22600,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = _engine(tmp_path).list_paper_trades(day="2026-09-29")
    assert out["count"] == 1
    assert out["trades"][0]["atm"] == 22600
    assert "entry=fly" in out["trades"][0]["info"]
    assert out["days"] == ["2026-09-29", "2026-09-28"]


def test_paper_trade_stop_zero_is_kept() -> None:
    from atlas_lite.feed_engine import FeedEngine

    info = FeedEngine._paper_trade_info(
        {"event": "open", "gates": "fly", "stop": 0, "target": 10}
    )
    assert "stop=0" in info
    assert "entry=fly" in info
    assert "tgt=10" in info


def test_paper_trade_info_includes_book_gates_and_entry_metrics() -> None:
    from atlas_lite.feed_engine import FeedEngine

    info = FeedEngine._paper_trade_info(
        {
            "event": "open",
            "strategy": "short_iron_condor",
            "spot": 22420.0,
            "atm": 22400,
            "ce_credit": 4.5,
            "pe_credit": 4.5,
            "credit": 9.0,
            "lots": 6,
            "qty": 390,
            "target": 2000.0,
        }
    )
    assert "Entry:" in info
    assert "Exit:" in info
    legend = FeedEngine._paper_book_legend("short_iron_condor")
    assert "₹2,000" in info or "₹2,000" in legend
    assert "hold to weekly expiry" in info
    assert "At entry:" in info
    assert "spot=22420" in info
    assert "CE credit=4.5" in info


def test_fly_and_straddle_entry_aliases(tmp_path: Path) -> None:
    (tmp_path / "paper_trades.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-09-25T10:00:00+05:30",
                        "day": "2026-09-25",
                        "event": "open",
                        "strategy": "short_iron_fly",
                        "premium": 10887.5,
                        "credit": 167.5,
                        "straddle": 238.2,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-25T15:14:00+05:30",
                        "day": "2026-09-25",
                        "event": "close",
                        "strategy": "short_iron_fly",
                        "credit": 167.5,
                        "credit_exit": 160.45,
                        "straddle_entry": 238.2,
                        "straddle_exit": 218.6,
                        "pnl": 62.86,
                        "reason": "time",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "paper_short_straddle.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-09-25T13:00:00+05:30",
                        "day": "2026-09-25",
                        "event": "open",
                        "strategy": "short_atm_straddle",
                        "straddle": 211.35,
                        "straddle_entry": 211.35,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-25T14:03:00+05:30",
                        "day": "2026-09-25",
                        "event": "close",
                        "strategy": "short_atm_straddle",
                        "straddle_entry": 211.35,
                        "straddle_exit": 224.55,
                        "pnl": -986.32,
                        "reason": "stop",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "paper_long_iron_condor.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-09-25T09:16:00+05:30",
                "day": "2026-09-25",
                "event": "open",
                "strategy": "long_iron_condor",
                "debit": 147.25,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    out = _engine(tmp_path).list_paper_trades(limit=20)
    by_key = {(t["strategy"], t["event"]): t for t in out["trades"]}
    # Paired closes hide their opens; LIC still open.
    assert ("short_iron_fly", "open") not in by_key
    fly_close = by_key[("short_iron_fly", "close")]
    short_close = by_key[("short_atm_straddle", "close")]
    lic_open = by_key[("long_iron_condor", "open")]

    assert fly_close["entry"] == 167.5  # credit, not premium notional
    assert fly_close["exit"] == 160.45
    assert short_close["entry"] == 211.35
    assert short_close["exit"] == 224.55
    assert lic_open["entry"] == 147.25
    assert out["pnl_total"] == round(62.86 - 986.32, 2)
    by = {(r["day"], r["strategy"]): r for r in out["by_book_day"]}
    assert by[("2026-09-25", "short_iron_fly")]["pnl_total"] == 62.86
    assert by[("2026-09-25", "short_atm_straddle")]["pnl_total"] == -986.32
    assert by[("2026-09-25", "long_iron_condor")]["open"] == 1


def test_short_ic_close_set_pnl_and_overnight_pairing(tmp_path: Path) -> None:
    """Short IC books PnL on close_set; overnight open must not stick as forever-open."""
    opened = "2026-09-29T10:05:00+05:30"
    (tmp_path / "paper_short_iron_condor.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": opened,
                        "day": "2026-09-29",
                        "event": "open",
                        "strategy": "short_iron_condor",
                        "opened_at": opened,
                        "atm": 22400,
                        "credit": 9.0,
                        "ce_credit": 4.5,
                        "pe_credit": 4.5,
                        "qty": 390,
                        "lots": 6,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-30T11:20:00+05:30",
                        "day": "2026-09-30",
                        "event": "close_set",
                        "strategy": "short_iron_condor",
                        "opened_at": opened,
                        "side": "ce",
                        "reason": "set_stop_4x",
                        "atm": 22400,
                        "credit": 4.5,  # remaining combined — must not win over credit_entry
                        "credit_entry": 4.5,
                        "credit_exit": 19.3,
                        "short_symbol": "NFO:NIFTY26SEP22800CE",
                        "qty": 390,
                        "pnl": -5765.73,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-30T11:21:00+05:30",
                        "day": "2026-09-30",
                        "event": "reentry",
                        "strategy": "short_iron_condor",
                        "opened_at": opened,
                        "side": "ce",
                        "reason": "reentry_after_stop",
                        "atm": 22400,
                        "credit": 4.2,
                        "short_symbol": "NFO:NIFTY26SEP22900CE",
                        "qty": 390,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-10-01T15:20:00+05:30",
                        "day": "2026-10-01",
                        "event": "close_set",
                        "strategy": "short_iron_condor",
                        "opened_at": opened,
                        "side": "ce",
                        "reason": "expiry",
                        "atm": 22400,
                        "credit_entry": 4.2,
                        "credit_exit": 0.05,
                        "credit": 4.2,
                        "short_symbol": "NFO:NIFTY26SEP22900CE",
                        "qty": 390,
                        "pnl": 1480.0,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-10-01T15:20:01+05:30",
                        "day": "2026-10-01",
                        "event": "close_set",
                        "strategy": "short_iron_condor",
                        "opened_at": opened,
                        "side": "pe",
                        "reason": "expiry",
                        "atm": 22400,
                        "credit_entry": 4.5,
                        "credit_exit": 0.1,
                        "credit": 9.0,
                        "short_symbol": "NFO:NIFTY26SEP22000PE",
                        "qty": 390,
                        "pnl": 1487.95,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-10-01T15:20:02+05:30",
                        "day": "2026-10-01",
                        "event": "close",
                        "strategy": "short_iron_condor",
                        "opened_at": opened,
                        "reason": "expiry",
                        "atm": 22400,
                        "pnl": None,
                        "realized_pnl": -2797.78,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out = _engine(tmp_path).list_paper_trades(limit=50)
    trades = out["trades"]
    events = [t["event"] for t in trades]
    assert "open" not in events  # fully flat — no ghost open
    assert events.count("close") == 0  # null-pnl flatten skipped
    assert events.count("close_set") == 3
    assert events.count("reentry") == 0  # reentry paired into later close_set

    by_side_day = {(t["day"], t["side"]): t for t in trades if t["event"] == "close_set"}
    ce_stop = by_side_day[("2026-09-30", "ce")]
    assert ce_stop["entry"] == 4.5
    assert ce_stop["exit"] == 19.3
    assert ce_stop["pnl"] == -5765.73

    pe_exp = by_side_day[("2026-10-01", "pe")]
    assert pe_exp["entry"] == 4.5  # credit_entry, not combined credit=9
    assert pe_exp["pnl"] == 1487.95

    assert out["pnl_total"] == round(-5765.73 + 1480.0 + 1487.95, 2)
    by = {(r["day"], r["strategy"]): r for r in out["by_book_day"]}
    assert by[("2026-09-30", "short_iron_condor")]["pnl_total"] == -5765.73
    assert by[("2026-09-30", "short_iron_condor")]["closed"] == 1
    assert by[("2026-10-01", "short_iron_condor")]["pnl_total"] == round(1480.0 + 1487.95, 2)
    assert by[("2026-10-01", "short_iron_condor")]["closed"] == 2
    assert ("2026-09-29", "short_iron_condor") not in by  # open day consumed


def test_short_ic_overnight_still_open_shows_once(tmp_path: Path) -> None:
    opened = "2026-09-29T10:05:00+05:30"
    (tmp_path / "paper_short_iron_condor.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": opened,
                        "day": "2026-09-29",
                        "event": "open",
                        "strategy": "short_iron_condor",
                        "opened_at": opened,
                        "atm": 22400,
                        "credit": 9.0,
                        "qty": 390,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-30T11:20:00+05:30",
                        "day": "2026-09-30",
                        "event": "close_set",
                        "strategy": "short_iron_condor",
                        "opened_at": opened,
                        "side": "ce",
                        "credit_entry": 4.5,
                        "credit_exit": 18.0,
                        "credit": 4.5,
                        "pnl": -5000.0,
                        "qty": 390,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = _engine(tmp_path).list_paper_trades(limit=20)
    assert out["open_count"] == 1
    assert out["closed_count"] == 1
    opens = [t for t in out["trades"] if t["event"] == "open"]
    assert len(opens) == 1
    assert opens[0]["day"] == "2026-09-29"
    assert out["pnl_total"] == -5000.0


def test_theta_cliff_skips_null_pnl_seal_close(tmp_path: Path) -> None:
    # Historical opens omitted opened_at; closes still carry opened_at == open ts.
    opened = "2026-09-29T09:20:00+05:30"
    (tmp_path / "paper_theta_cliff.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": opened,
                        "day": "2026-09-29",
                        "event": "open",
                        "strategy": "theta_cliff",
                        "credit": 120.0,
                        "qty": 65,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-29T14:00:00+05:30",
                        "day": "2026-09-29",
                        "event": "close_vertical",
                        "strategy": "theta_cliff",
                        "opened_at": opened,
                        "side": "ce",
                        "entry": 60.0,
                        "exit": 80.0,
                        "pnl": -1400.0,
                        "qty": 65,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-29T14:01:00+05:30",
                        "day": "2026-09-29",
                        "event": "close_vertical",
                        "strategy": "theta_cliff",
                        "opened_at": opened,
                        "side": "pe",
                        "entry": 60.0,
                        "exit": 55.0,
                        "pnl": 320.43,
                        "qty": 65,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-29T14:01:01+05:30",
                        "day": "2026-09-29",
                        "event": "close",
                        "strategy": "theta_cliff",
                        "opened_at": opened,
                        "pnl": None,
                        "reason": "stopped",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = _engine(tmp_path).list_paper_trades(limit=20)
    events = [t["event"] for t in out["trades"]]
    assert "open" not in events
    assert out["open_count"] == 0
    assert events.count("close") == 0
    assert events.count("close_vertical") == 2
    assert out["pnl_total"] == round(-1400.0 + 320.43, 2)
    by = {(r["day"], r["strategy"]): r for r in out["by_book_day"]}
    assert by[("2026-09-29", "theta_cliff")]["closed"] == 2
    assert by[("2026-09-29", "theta_cliff")]["open"] == 0
    assert by[("2026-09-29", "theta_cliff")]["pnl_total"] == round(-1400.0 + 320.43, 2)
    assert by[("2026-09-29", "theta_cliff")]["pnl_estimated"] is False


def test_book_day_marks_estimated_pnl(tmp_path: Path) -> None:
    opened = "2026-09-29T10:00:00+05:30"
    (tmp_path / "paper_short_iron_condor.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": opened,
                        "day": "2026-09-29",
                        "event": "open",
                        "strategy": "short_iron_condor",
                        "opened_at": opened,
                        "credit": 9.0,
                        "qty": 390,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-10-01T15:20:00+05:30",
                        "day": "2026-10-01",
                        "event": "close_set",
                        "strategy": "short_iron_condor",
                        "opened_at": opened,
                        "side": "ce",
                        "credit_entry": 4.5,
                        "credit_exit": 0.0,
                        "pnl": 1500.0,
                        "pnl_known": False,
                        "settle": "intrinsic",
                        "qty": 390,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-10-01T15:20:01+05:30",
                        "day": "2026-10-01",
                        "event": "close_set",
                        "strategy": "short_iron_condor",
                        "opened_at": opened,
                        "side": "pe",
                        "credit_entry": 4.5,
                        "credit_exit": 0.0,
                        "pnl": 1400.0,
                        "pnl_known": True,
                        "qty": 390,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = _engine(tmp_path).list_paper_trades(limit=20)
    by = {(r["day"], r["strategy"]): r for r in out["by_book_day"]}
    row = by[("2026-10-01", "short_iron_condor")]
    assert row["pnl_total"] == 2900.0
    assert row["pnl_estimated"] is True
    assert out["open_count"] == 0
