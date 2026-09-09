"""Paper long-straddle tests — tape filters, 1 lot, no broker."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite.paper_straddle import (
    PaperStraddle,
    evaluate_paper_entry,
    in_paper_entry_window,
    realised_vol_pct,
    stop_straddle_px,
    trail_gap_px,
)

IST = ZoneInfo("Asia/Kolkata")


class _Book:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def get(self, symbol: str):
        return self.rows.get(symbol)


def _now(hm: str, day: str = "2026-09-04") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def _trader(tmp_path: Path) -> PaperStraddle:
    return PaperStraddle(path=tmp_path / "paper.jsonl", lot_size=65, lots=1)


def _tape_feed(**kwargs) -> dict:
    feed = {
        "adx": 20.0,
        "atr": 10.0,
        "vix_chg": 0.5,
        "pcr": 1.1,
        "ivp": 30.0,
        "iv": 10.0,
        "iv_day_high": 12.0,
        "nifty_ltp": 24800.0,
        "ce": 50.0,
        "pe": 48.0,
        "ce_oi": 80_000.0,
        "pe_oi": 80_000.0,
        "index_nifty_chg": 0.1,
        "index_banknifty_chg": 0.1,
        "index_sensex_chg": 0.1,
    }
    feed.update(kwargs)
    return feed


def _trail_off(bot: PaperStraddle, now, **kwargs) -> dict:
    """+6% arms the trail; next tick below peak − gap closes it."""
    book = kwargs["book"]
    book.rows["NFO:CE"] = {"last_price": 112.0}
    book.rows["NFO:PE"] = {"last_price": 95.0}
    armed = bot.on_frame(now=now, entry_ready=False, **kwargs)
    assert armed is None
    assert bot.position is not None
    assert bot.position.trail_armed is True
    book.rows["NFO:CE"] = {"last_price": 100.0}
    book.rows["NFO:PE"] = {"last_price": 95.0}
    closed = bot.on_frame(now=now, entry_ready=False, **kwargs)
    assert closed is not None
    assert closed["reason"] == "trail"
    return closed


def test_tape_filters_ready_on_suggested_feed() -> None:
    result = evaluate_paper_entry(_tape_feed())
    assert result["ready"] is True
    assert result["failing_gates"] == []
    assert result["missing_gates"] == []
    assert result["gates"] == "tape"
    assert result["straddle_edge"] > 0


def test_tape_soft_metrics_are_not_and_gates() -> None:
    result = evaluate_paper_entry(
        _tape_feed(
            adx=40.0,
            pcr=0.5,
            vix_chg=5.0,
            oi_vs_day_high=90.0,
            iv_day_low=8.0,
            iv=9.5,
            iv_day_high=9.5,
            index_banknifty_chg=0.75,
            index_sensex_chg=0.74,
        )
    )
    assert result["ready"] is True
    assert "BANKNIFTY" not in result["failing_gates"]
    assert "SENSEX" not in result["failing_gates"]


def test_tape_rejects_ivp_at_40() -> None:
    result = evaluate_paper_entry(_tape_feed(ivp=40.0))
    assert result["ready"] is False
    assert "IV Percentile" in result["failing_gates"]
    assert "PCR" not in result["failing_gates"]


def test_tape_requires_rv_vs_iv_and_index_band() -> None:
    result = evaluate_paper_entry({k: v for k, v in _tape_feed().items() if k != "atr"})
    assert result["ready"] is False
    assert "RV vs IV" in result["missing_gates"]
    no_spot = evaluate_paper_entry({k: v for k, v in _tape_feed().items() if k != "nifty_ltp"})
    assert "RV vs IV" in no_spot["missing_gates"]
    stretched = evaluate_paper_entry(_tape_feed(index_nifty_chg=0.5))
    assert stretched["ready"] is False
    assert "NIFTY 50" in stretched["failing_gates"]
    at_band = evaluate_paper_entry(
        _tape_feed(index_nifty_chg=0.49, index_banknifty_chg=0.9, index_sensex_chg=0.9)
    )
    assert at_band["ready"] is True
    assert "BANKNIFTY" not in at_band["failing_gates"]
    assert "SENSEX" not in at_band["failing_gates"]
    rich_iv = evaluate_paper_entry(_tape_feed(iv=20.0))
    assert rich_iv["ready"] is False
    assert "RV vs IV" in rich_iv["failing_gates"]
    assert rich_iv["straddle_edge"] < 0


def test_realised_vol_annualizes_1m_atr() -> None:
    rv = realised_vol_pct(10.0, 24800.0)
    assert round(rv, 4) == round((10.0 / 24800.0) * (375.0 * 252.0) ** 0.5 * 100.0, 4)
    assert rv > 12.0
    assert rv < 13.0


def test_tape_requires_liquidity_oi() -> None:
    missing = evaluate_paper_entry({k: v for k, v in _tape_feed().items() if k not in ("ce_oi", "pe_oi")})
    assert missing["ready"] is False
    assert "Liquidity" in missing["missing_gates"]
    thin = evaluate_paper_entry(_tape_feed(ce_oi=10_000.0, pe_oi=80_000.0))
    assert thin["ready"] is False
    assert "Liquidity" in thin["failing_gates"]


def test_trail_gap_is_wider_of_2pct_and_4pts() -> None:
    assert trail_gap_px(160.0, trail_pct=0.02, trail_pts=4.0) == 4.0
    assert trail_gap_px(195.0, trail_pct=0.02, trail_pts=4.0) == 4.0
    assert trail_gap_px(400.0, trail_pct=0.02, trail_pts=4.0) == 8.0


def test_stop_is_wider_of_4pct_and_10pts() -> None:
    # Cheap: 4% of 160 is 6.4 pts → floor 10 pts.
    assert stop_straddle_px(160.0, stop_pct=-0.04, stop_pts=10.0) == 150.0
    # Mid: 4% of 200 is 8 pts → floor 10 pts.
    assert stop_straddle_px(200.0, stop_pct=-0.04, stop_pts=10.0) == 190.0
    # Rich: 4% of 400 is 16 pts → % stop is wider than 10 pts.
    assert stop_straddle_px(400.0, stop_pct=-0.04, stop_pts=10.0) == 384.0


def test_paper_opens_on_first_7_of_7(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 95.0},
        }
    )
    event = bot.on_frame(
        now=_now("11:20"),
        entry_ready=True,
        feed={"ce": 100.0, "pe": 95.0},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert event is not None
    assert event["event"] == "open"
    assert event["qty"] == 65
    assert event["straddle"] == 195.0
    assert event["gates"] == "tape"
    assert bot.position is not None


def test_paper_skips_second_entry_same_7_of_7_cluster(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 95.0},
        }
    )
    bot.on_frame(
        now=_now("11:20"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    bot.position = None
    again = bot.on_frame(
        now=_now("11:21"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert again is None


def test_paper_reenters_after_close_on_fresh_7_of_7(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 95.0},
        }
    )
    kwargs = dict(
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert bot.on_frame(now=_now("11:20"), entry_ready=True, **kwargs)["event"] == "open"
    _trail_off(bot, _now("11:30"), **kwargs)
    book.rows["NFO:CE"] = {"last_price": 100.0}
    book.rows["NFO:PE"] = {"last_price": 95.0}
    bot.on_frame(now=_now("10:20"), entry_ready=False, **kwargs)
    second = bot.on_frame(now=_now("10:21"), entry_ready=True, **kwargs)
    assert second is not None
    assert second["event"] == "open"
    assert second["entry_n"] == 2


def test_paper_caps_at_five_entries_per_day(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 95.0},
        }
    )
    kwargs = dict(
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    slots = [
        ("09:46", "09:54", "09:55"),
        ("10:05", "10:13", "10:14"),
        ("10:25", "10:33", "10:34"),
        ("10:45", "10:53", "10:54"),
        ("11:05", "11:13", "11:14"),
    ]
    for n, (open_hm, close_hm, flat_hm) in enumerate(slots):
        opened = bot.on_frame(now=_now(open_hm), entry_ready=True, **kwargs)
        assert opened is not None
        assert opened["entry_n"] == n + 1
        closed = _trail_off(bot, _now(close_hm), **kwargs)
        assert closed["reason"] == "trail"
        book.rows["NFO:CE"] = {"last_price": 100.0}
        book.rows["NFO:PE"] = {"last_price": 95.0}
        bot.on_frame(now=_now(flat_hm), entry_ready=False, **kwargs)
    sixth = bot.on_frame(now=_now("11:20"), entry_ready=True, **kwargs)
    assert sixth is None
    assert bot.entries_today == 5


def test_paper_trails_after_6pct(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 95.0},
        }
    )
    kwargs = dict(
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    bot.on_frame(now=_now("11:20"), entry_ready=True, **kwargs)
    book.rows["NFO:CE"] = {"last_price": 112.0}
    book.rows["NFO:PE"] = {"last_price": 95.0}
    armed = bot.on_frame(
        now=_now("11:30"),
        entry_ready=False,
        feed={},
        book=book,
        ce_symbol="NFO:XX",
        pe_symbol="NFO:YY",
        atm=24100,
    )
    assert armed is None
    assert bot.position is not None
    assert bot.position.trail_armed is True
    book.rows["NFO:CE"] = {"last_price": 108.0}
    book.rows["NFO:PE"] = {"last_price": 95.0}
    event = bot.on_frame(
        now=_now("11:31"),
        entry_ready=False,
        feed={},
        book=book,
        ce_symbol="NFO:XX",
        pe_symbol="NFO:YY",
        atm=24100,
    )
    assert event is not None
    assert event["reason"] == "trail"
    assert event["straddle_exit"] == 203.0
    assert event["pnl_gross"] == 520.0
    assert event["charges"] > 0
    assert event["pnl"] == round(520.0 - event["charges"], 2)
    assert event["pnl"] < event["pnl_gross"]
    assert bot.position is None


def test_paper_exits_on_stop(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 100.0},
        }
    )
    bot.on_frame(
        now=_now("11:20"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    book.rows["NFO:CE"] = {"last_price": 90.0}
    book.rows["NFO:PE"] = {"last_price": 90.0}
    event = bot.on_frame(
        now=_now("12:00"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert event["reason"] == "stop"
    assert event["pnl"] < 0


def test_paper_10pt_floor_on_cheap_straddle(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 80.0},
            "NFO:PE": {"last_price": 80.0},
        }
    )
    open_ev = bot.on_frame(
        now=_now("11:20"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert open_ev["stop_loss"] == 150.0
    assert open_ev["stop_loss_pct"] == 4.0
    assert open_ev["stop_loss_pts"] == 10.0
    book.rows["NFO:CE"] = {"last_price": 75.0}
    book.rows["NFO:PE"] = {"last_price": 75.0}
    event = bot.on_frame(
        now=_now("12:00"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert event["reason"] == "stop"
    assert event["straddle_exit"] == 150.0


def test_paper_square_off_at_1514(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 100.0},
        }
    )
    bot.on_frame(
        now=_now("11:20"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    event = bot.on_frame(
        now=_now("15:14"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert event["reason"] == "time"
    assert bot.eod_written
    assert bot.last_event is not None
    assert bot.last_event["event"] == "day_pnl"
    assert bot.last_event["capital"] == 200000.0
    assert bot.last_event["day_pnl"] == event["pnl"]
    assert bot.last_event["equity"] == 200000.0 + event["pnl"]


def test_paper_no_entry_before_0915(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 100.0},
        }
    )
    event = bot.on_frame(
        now=_now("09:14"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert event is None
    assert bot.position is None


def test_paper_still_opens_at_1513(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 100.0},
        }
    )
    event = bot.on_frame(
        now=_now("15:13"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert event is not None
    assert event["event"] == "open"
    assert bot.position is not None


def test_paper_no_new_entry_at_1514(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 100.0},
        }
    )
    event = bot.on_frame(
        now=_now("15:14"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert (event is None) or (event.get("event") == "day_pnl")
    assert bot.position is None
    assert in_paper_entry_window(_now("15:13")) is True
    assert in_paper_entry_window(_now("15:14")) is False


def test_paper_opens_at_0915(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 100.0},
        }
    )
    event = bot.on_frame(
        now=_now("09:15"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert event is not None
    assert event["event"] == "open"


def _today_open_event(**extra: object) -> dict:
    day = datetime.now(IST).strftime("%Y-%m-%d")
    body = {
        "event": "open",
        "day": day,
        "atm": 24000,
        "ce_symbol": "NFO:CE",
        "pe_symbol": "NFO:PE",
        "lots": 1,
        "qty": 65,
        "ce": 100.0,
        "pe": 95.0,
        "straddle": 195.0,
        "stop_loss": 183.0,
        "target": 206.7,
        "ts": f"{day}T10:01:00+05:30",
        "opened_at": f"{day}T10:01:00+05:30",
    }
    body.update(extra)
    return body


def test_restart_restores_open_position(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    path.write_text(json.dumps(_today_open_event()) + "\n", encoding="utf-8")
    bot = PaperStraddle(path=path, lot_size=65, lots=1)
    assert bot.position is not None
    assert bot.position.atm == 24000
    assert bot.position.ce_symbol == "NFO:CE"
    assert bot.position.straddle_entry == 195.0
    assert bot.position.stop_straddle == 183.0
    assert bot.entries_today == 1
    assert bot.traded_day == datetime.now(IST).strftime("%Y-%m-%d")


def test_restart_does_not_open_second_while_restored_position_open(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    day = datetime.now(IST).strftime("%Y-%m-%d")
    path.write_text(json.dumps(_today_open_event()) + "\n", encoding="utf-8")
    bot = PaperStraddle(path=path, lot_size=65, lots=1)
    weekday = datetime.fromisoformat(f"{day}T12:01:00+05:30")
    if weekday.weekday() >= 5:
        weekday = datetime.fromisoformat("2026-09-07T12:01:00+05:30")
        bot.traded_day = "2026-09-07"
        if bot.position is not None:
            bot.position.day = "2026-09-07"
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 95.0},
        }
    )
    again = bot.on_frame(
        now=weekday,
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert again is None
    assert bot.position is not None
    assert bot.entries_today == 1


def test_restart_keeps_daily_cap_after_closed_trades(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    day = datetime.now(IST).strftime("%Y-%m-%d")
    lines = []
    for n in range(1, 6):
        lines.append(json.dumps(_today_open_event(entry_n=n)))
        lines.append(
            json.dumps(
                {
                    "event": "close",
                    "day": day,
                    "reason": "stop",
                    "atm": 24000,
                    "ce_symbol": "NFO:CE",
                    "pe_symbol": "NFO:PE",
                    "qty": 65,
                    "ce_entry": 100.0,
                    "pe_entry": 95.0,
                    "straddle_entry": 195.0,
                    "straddle_exit": 183.0,
                    "pnl": -780.0,
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bot = PaperStraddle(path=path, lot_size=65, lots=1)
    assert bot.position is None
    assert bot.entries_today == 5
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 95.0},
        }
    )
    now = datetime.fromisoformat(f"{day}T12:01:00+05:30")
    if now.weekday() >= 5:
        now = datetime.fromisoformat("2026-09-07T12:01:00+05:30")
        bot.traded_day = "2026-09-07"
        bot._ready_prev = False
    bot._ready_prev = False
    sixth = bot.on_frame(
        now=now,
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert sixth is None
    assert bot.entries_today == 5


def test_restart_closed_last_event_stays_flat(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    day = datetime.now(IST).strftime("%Y-%m-%d")
    lines = [
        json.dumps(_today_open_event()),
        json.dumps({"event": "close", "day": day, "reason": "target"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bot = PaperStraddle(path=path, lot_size=65, lots=1)
    assert bot.position is None
    assert bot.entries_today == 1


def test_eod_pnl_vs_2l_capital(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 95.0},
        }
    )
    kwargs = dict(
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    bot.on_frame(now=_now("11:20"), entry_ready=True, **kwargs)
    closed = _trail_off(bot, _now("11:30"), **kwargs)
    assert closed["reason"] == "trail"
    eod = bot.on_frame(now=_now("15:14"), entry_ready=False, **kwargs)
    assert eod is not None
    assert eod["event"] == "day_pnl"
    assert eod["capital"] == 200000.0
    assert eod["trades"] == 1
    assert eod["day_pnl"] == closed["pnl"]
    assert eod["equity"] == 200000.0 + closed["pnl"]
    assert round(eod["day_pnl_pct"], 4) == round(closed["pnl"] / 200000.0 * 100.0, 4)
    snap = bot.snapshot()
    assert snap["eod"] is True
    assert snap["capital"] == 200000.0
    assert snap["day_pnl"] == closed["pnl"]


def test_restart_restores_day_pnl_vs_2l(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    day = datetime.now(IST).strftime("%Y-%m-%d")
    lines = [
        json.dumps(_today_open_event()),
        json.dumps({"event": "close", "day": day, "reason": "stop", "pnl": -780.0}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bot = PaperStraddle(path=path, lot_size=65, lots=1)
    assert bot.day_pnl == -780.0
    snap = bot.snapshot()
    assert snap["capital"] == 200000.0
    assert snap["day_pnl"] == -780.0
    assert snap["equity"] == 199220.0
    assert snap["eod"] is False


def test_failed_open_retries_when_quotes_arrive(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book({})
    kwargs = dict(
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert bot.on_frame(now=_now("11:20"), entry_ready=True, **kwargs) is None
    assert bot.position is None
    book.rows["NFO:CE"] = {"last_price": 100.0}
    book.rows["NFO:PE"] = {"last_price": 95.0}
    opened = bot.on_frame(now=_now("11:20"), entry_ready=True, **kwargs)
    assert opened is not None
    assert opened["event"] == "open"


def test_prior_day_open_flattens_without_hitting_today_2l(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    prior = _today_open_event()
    prior["day"] = "2026-09-01"
    path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    bot = PaperStraddle(path=path, lot_size=65, lots=1)
    assert bot.position is not None
    assert bot.entries_today == 0
    book = _Book(
        {
            "NFO:CE": {"last_price": 90.0},
            "NFO:PE": {"last_price": 90.0},
        }
    )
    event = bot.on_frame(
        now=_now("10:00", "2026-09-07"),
        entry_ready=False,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert event is not None
    assert event["reason"] == "session_gap"
    assert event["day"] == "2026-09-01"
    assert event["pnl_known"] is True
    assert bot.position is None
    assert bot.day_pnl == 0.0


def test_time_flat_without_quotes_does_not_book_zero_pnl(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 100.0},
        }
    )
    bot.on_frame(
        now=_now("11:20"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    event = bot.on_frame(
        now=_now("15:14"),
        entry_ready=True,
        feed={},
        book=_Book({}),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert event is not None
    assert event["reason"] == "time_flat"
    assert event["pnl_known"] is False
    assert event["pnl"] is None
    assert event["pct"] is None
    assert bot.day_pnl == 0.0
    assert bot.position is None
    assert bot.eod_written is True


def test_session_gap_without_quotes_does_not_book_zero_pnl(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    prior = _today_open_event()
    prior["day"] = "2026-09-01"
    path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    bot = PaperStraddle(path=path, lot_size=65, lots=1)
    event = bot.on_frame(
        now=_now("10:00", "2026-09-07"),
        entry_ready=False,
        feed={},
        book=_Book({}),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert event is not None
    assert event["reason"] == "session_gap"
    assert event["pnl_known"] is False
    assert event["pnl"] is None
    assert bot.day_pnl == 0.0


def test_close_pct_is_percent_like_stop_loss_pct(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 95.0},
        }
    )
    open_ev = bot.on_frame(
        now=_now("11:20"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert open_ev["stop_loss_pct"] == 4.0
    book.rows["NFO:CE"] = {"last_price": 112.0}
    book.rows["NFO:PE"] = {"last_price": 95.0}
    armed = bot.on_frame(
        now=_now("11:30"),
        entry_ready=False,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert armed is None
    book.rows["NFO:CE"] = {"last_price": 108.0}
    book.rows["NFO:PE"] = {"last_price": 95.0}
    closed = bot.on_frame(
        now=_now("11:31"),
        entry_ready=False,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    assert closed["pnl_known"] is True
    assert closed["reason"] == "trail"
    assert closed["stop_loss_pct"] == 4.0
    assert closed["pct"] == round(8.0 / 195.0 * 100.0, 4)
    assert closed["pct"] > 1.0
    assert closed["pnl"] == round(closed["pnl_gross"] - closed["charges"], 2)


def test_snapshot_marks_open_trade_against_2l(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 95.0},
        }
    )
    bot.on_frame(
        now=_now("11:20"),
        entry_ready=True,
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
    )
    book.rows["NFO:CE"] = {"last_price": 110.0}
    book.rows["NFO:PE"] = {"last_price": 95.0}
    snap = bot.snapshot(book=book)
    assert snap["day_pnl"] == 0.0
    assert snap["open_pnl_gross"] == 650.0
    assert snap["charges"] > 0
    assert snap["open_pnl"] == round(650.0 - snap["charges"], 2)
    assert snap["mtm_pnl"] == snap["open_pnl"]
    assert snap["equity"] == round(200000.0 + snap["open_pnl"], 2)
    assert snap["entry_filters"] == "tape"
    assert snap["entry_window"] == "09:15-15:14"
    assert snap["square_off"] == "15:14"


def _fly_feed(**kwargs) -> dict:
    """Calm, implied-rich tape: fly AND, not the long overlay."""
    feed = _tape_feed(iv=20.0, ivp=55.0)
    feed.update(kwargs)
    return feed


def _fly_book() -> _Book:
    return _Book(
        {
            "NFO:CE": {"last_price": 100.0},
            "NFO:PE": {"last_price": 95.0},
            "NFO:WCE": {"last_price": 20.0},
            "NFO:WPE": {"last_price": 18.0},
        }
    )


def _fly_kwargs(book: _Book, **extra) -> dict:
    body = dict(
        feed={},
        book=book,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=24000,
        strategy="short_iron_fly",
        wing_ce_symbol="NFO:WCE",
        wing_pe_symbol="NFO:WPE",
        wing_pts=250,
        entry_ready=True,
    )
    body.update(extra)
    return body


def test_fly_gates_ready_when_implied_rich() -> None:
    from atlas_lite.paper_straddle import evaluate_paper_fly, evaluate_paper_regime

    result = evaluate_paper_fly(_fly_feed())
    assert result["ready"] is True
    assert result["failing_gates"] == []
    assert result["straddle_edge"] < 0
    regime = evaluate_paper_regime(_fly_feed())
    assert regime["strategy"] == "short_iron_fly"
    long_wins = evaluate_paper_regime(_tape_feed())
    assert long_wins["strategy"] == "long_straddle"
    hot = evaluate_paper_fly(_fly_feed(iv_chg_5d=3.1))
    assert hot["ready"] is False
    assert "Vol-of-vol" in hot["failing_gates"]
    crisis = evaluate_paper_fly(_fly_feed(ivp=80.0))
    assert crisis["ready"] is False
    assert "IV Percentile" in crisis["failing_gates"]
    missing_chg = evaluate_paper_fly(_fly_feed())
    assert "Vol-of-vol" not in missing_chg["failing_gates"]


def test_iron_fly_strikes_are_250_wide() -> None:
    from atlas_lite.paper_straddle import iron_fly_strikes

    assert iron_fly_strikes(24000) == (23750, 24250)


def test_paper_opens_short_iron_fly(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _fly_book()
    event = bot.on_frame(now=_now("10:15"), **_fly_kwargs(book))
    assert event is not None
    assert event["event"] == "open"
    assert event["strategy"] == "short_iron_fly"
    assert event["credit"] == 157.0
    assert event["wing_pts"] == 250
    assert event["qty"] == 65
    assert event["max_loss"] == round((250.0 - 157.0) * 65, 2)
    assert bot.position is not None
    assert bot.position.strategy == "short_iron_fly"


def test_paper_fly_takes_profit_at_half_credit(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _fly_book()
    opened = bot.on_frame(now=_now("10:15"), **_fly_kwargs(book))
    assert opened is not None
    book.rows["NFO:CE"] = {"last_price": 50.0}
    book.rows["NFO:PE"] = {"last_price": 50.0}
    closed = bot.on_frame(now=_now("11:00"), **_fly_kwargs(book, entry_ready=False))
    assert closed is not None
    assert closed["reason"] == "target"
    assert closed["strategy"] == "short_iron_fly"
    assert closed["pnl"] > 0
    assert closed["pnl_known"] is True


def test_paper_fly_stops_at_half_defined_loss(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _fly_book()
    bot.on_frame(now=_now("10:15"), **_fly_kwargs(book))
    book.rows["NFO:CE"] = {"last_price": 160.0}
    book.rows["NFO:PE"] = {"last_price": 160.0}
    closed = bot.on_frame(now=_now("11:00"), **_fly_kwargs(book, entry_ready=False))
    assert closed is not None
    assert closed["reason"] == "stop"
    assert closed["pnl"] < 0


def test_paper_fly_stop_fires_when_far_wing_quotes_zero(tmp_path: Path) -> None:
    """0.00 on a long wing is a valid mark. Stop must still fire and book P&L."""
    from atlas_lite.paper_straddle import fly_value_pts

    assert fly_value_pts(0.0, 400.0, 0.0, 0.0, width=250) == 250.0
    assert fly_value_pts(0.0, 0.0, 0.0, 0.0, width=250) == 0.0
    bot = _trader(tmp_path)
    book = _fly_book()
    opened = bot.on_frame(now=_now("10:15"), **_fly_kwargs(book))
    assert opened is not None
    assert opened["credit"] == 157.0
    book.rows["NFO:CE"] = {"last_price": 0.0}
    book.rows["NFO:PE"] = {"last_price": 350.0}
    book.rows["NFO:WCE"] = {"last_price": 0.0}
    book.rows["NFO:WPE"] = {"last_price": 100.0}
    closed = bot.on_frame(now=_now("11:00"), **_fly_kwargs(book, entry_ready=False))
    assert closed is not None
    assert closed["reason"] == "stop"
    assert closed["pnl_known"] is True
    assert closed["pnl_gross"] is not None
    assert closed["charges"] > 0
    assert closed["pnl"] == round(closed["pnl_gross"] - closed["charges"], 2)
    assert closed["pnl"] < closed["pnl_gross"]
    assert closed["pnl"] >= -opened["max_loss"] - closed["charges"]
    assert closed["credit_exit"] == 250.0


def test_paper_fly_stop_fires_when_short_leg_quotes_zero(tmp_path: Path) -> None:
    """Max-loss tape: one short and one wing at 0.00. Must stop and book the cap."""
    cases = [
        (0.0, 700.0, 0.0, 450.0),
        (700.0, 0.0, 450.0, 0.0),
        (0.0, 400.0, 0.0, 150.0),
        (400.0, 0.0, 150.0, 0.0),
    ]
    for i, (ce, pe, wce, wpe) in enumerate(cases):
        bot = _trader(tmp_path / f"fly_zero_{i}")
        book = _fly_book()
        opened = bot.on_frame(now=_now("10:15"), **_fly_kwargs(book))
        assert opened is not None
        book.rows["NFO:CE"] = {"last_price": ce}
        book.rows["NFO:PE"] = {"last_price": pe}
        book.rows["NFO:WCE"] = {"last_price": wce}
        book.rows["NFO:WPE"] = {"last_price": wpe}
        closed = bot.on_frame(now=_now("11:00"), **_fly_kwargs(book, entry_ready=False))
        assert closed is not None, (ce, pe, wce, wpe)
        assert closed["reason"] == "stop", (ce, pe, wce, wpe, closed)
        assert closed["pnl_known"] is True
        assert closed["pnl_gross"] == -opened["max_loss"]
        assert closed["charges"] > 0
        assert closed["pnl"] == round(closed["pnl_gross"] - closed["charges"], 2)
        assert closed["credit_exit"] == 250.0


def test_paper_fly_does_not_open_on_zero_wings(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _fly_book()
    book.rows["NFO:WCE"] = {"last_price": 0.0}
    book.rows["NFO:WPE"] = {"last_price": 0.0}
    event = bot.on_frame(now=_now("10:15"), **_fly_kwargs(book))
    assert event is None
    assert bot.position is None


def test_paper_fly_squares_off_at_1514(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _fly_book()
    bot.on_frame(now=_now("10:15"), **_fly_kwargs(book))
    closed = bot.on_frame(now=_now("15:14"), **_fly_kwargs(book, entry_ready=False))
    assert closed is not None
    assert closed["reason"] == "time"
    assert bot.eod_written is True


def test_paper_does_not_open_debit_fly(tmp_path: Path) -> None:
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 20.0},
            "NFO:PE": {"last_price": 20.0},
            "NFO:WCE": {"last_price": 40.0},
            "NFO:WPE": {"last_price": 40.0},
        }
    )
    event = bot.on_frame(now=_now("10:15"), **_fly_kwargs(book))
    assert event is None
    assert bot.position is None


def test_paper_does_not_open_thin_credit_fly(tmp_path: Path) -> None:
    """Sub-100 pt credit is a 2L-hostile max loss even though it is still a credit."""
    bot = _trader(tmp_path)
    book = _Book(
        {
            "NFO:CE": {"last_price": 40.0},
            "NFO:PE": {"last_price": 40.0},
            "NFO:WCE": {"last_price": 20.0},
            "NFO:WPE": {"last_price": 20.0},
        }
    )
    event = bot.on_frame(now=_now("10:15"), **_fly_kwargs(book))
    assert event is None
    assert bot.position is None
    book.rows["NFO:CE"] = {"last_price": 80.0}
    book.rows["NFO:PE"] = {"last_price": 40.0}
    book.rows["NFO:WCE"] = {"last_price": 10.0}
    book.rows["NFO:WPE"] = {"last_price": 10.0}
    opened = bot.on_frame(now=_now("10:16"), **_fly_kwargs(book))
    assert opened is not None
    assert opened["credit"] == 100.0
    assert opened["max_loss"] == 9750.0


def test_iv_change_n_days() -> None:
    from atlas_lite.iv_history import iv_change_n_days

    history = {
        "NSE:NIFTY 50": [
            {"day": "2026-08-31", "iv": 10.0},
            {"day": "2026-09-01", "iv": 11.0},
            {"day": "2026-09-02", "iv": 12.0},
            {"day": "2026-09-03", "iv": 12.5},
            {"day": "2026-09-04", "iv": 13.0},
            {"day": "2026-09-07", "iv": 13.2},
        ]
    }
    # today is 2026-09-08 per user_info; last 5 prior ends at 09-07 lookback 5 = 09-01
    chg = iv_change_n_days(history, 16.6, n=5)
    assert chg is not None
    assert chg == round(16.6 - 11.0, 3)
    assert iv_change_n_days({"NSE:NIFTY 50": []}, 10.0) is None
