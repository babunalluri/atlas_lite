"""Paper afternoon short ATM straddle — 14:00 hold, no premium stop, 1/day."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite.paper_short_straddle import (
    PaperShortStraddle,
    in_short_str_entry_window,
)

IST = ZoneInfo("Asia/Kolkata")


class _Book:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def get(self, symbol: str):
        return self.rows.get(symbol)


def _now(hm: str, day: str = "2026-09-04") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def _bot(path: Path, **kwargs) -> PaperShortStraddle:
    return PaperShortStraddle(path=path / "short.jsonl", lot_size=65, lots=1, **kwargs)


def _feed(**kwargs) -> dict:
    feed = {
        "ce": 120.0,
        "pe": 110.0,
        "ce_symbol": "NFO:CE",
        "pe_symbol": "NFO:PE",
        "index_nifty_chg": 0.2,
    }
    feed.update(kwargs)
    return feed


def _book(ce: float, pe: float) -> _Book:
    return _Book({"NFO:CE": {"last_price": ce}, "NFO:PE": {"last_price": pe}})


def _open(bot: PaperShortStraddle, hm: str = "14:00", **feed_kw) -> dict | None:
    return bot.on_frame(
        now=_now(hm),
        feed=_feed(**feed_kw),
        book=_book(feed_kw.get("ce", 120.0), feed_kw.get("pe", 110.0)),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )


def test_entry_window() -> None:
    assert in_short_str_entry_window(_now("13:59")) is False
    assert in_short_str_entry_window(_now("14:00")) is True
    assert in_short_str_entry_window(_now("14:15")) is True
    assert in_short_str_entry_window(_now("14:16")) is False
    assert in_short_str_entry_window(_now("15:14")) is False


def test_opens_at_1400(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = _open(bot)
    assert ev is not None
    assert ev["event"] == "open"
    assert ev["straddle_entry"] == 230.0
    assert ev["stop_straddle"] == 0.0
    assert bot.position is not None
    assert bot.entries_today == 1


def test_blocks_before_window(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot, "13:59") is None
    assert bot.position is None


def test_one_entry_per_day(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot, "14:00") is not None
    bot.position = None
    assert _open(bot, "14:05") is None


def test_skips_when_skew_already_used(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = bot.on_frame(
        now=_now("14:00"),
        feed=_feed(),
        book=_book(120.0, 110.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
        allow_entry=False,
    )
    assert ev is None
    assert bot.position is None


def test_trend_filter_blocks(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot, index_nifty_chg=0.9) is None
    assert _open(bot, index_nifty_chg=-0.81) is None
    assert _open(bot, index_nifty_chg=0.75) is not None


def test_hold_to_square_off_is_profit(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    closed = bot.on_frame(
        now=_now("15:14"),
        feed=_feed(ce=100.0, pe=90.0),
        book=_book(100.0, 90.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert closed is not None
    assert closed["event"] == "close"
    assert closed["reason"] == "time"
    assert closed["pnl_gross"] == 2600.0  # (230-190)*65
    assert closed["pnl"] is not None and closed["pnl"] < closed["pnl_gross"]
    assert closed["pnl"] > 0
    assert bot.position is None


def test_no_stop_when_straddle_rises(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    mid = bot.on_frame(
        now=_now("14:20"),
        feed=_feed(),
        book=_book(130.0, 114.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert mid is None
    assert bot.position is not None


def test_no_early_take_when_premium_decays(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    mid = bot.on_frame(
        now=_now("14:00"),
        feed=_feed(ce=100.0, pe=90.0),
        book=_book(100.0, 90.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert mid is None
    assert bot.position is not None


def test_session_gap_marks_when_quotes_live(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    closed = bot.on_frame(
        now=_now("09:20", day="2026-09-07"),
        feed=_feed(ce=100.0, pe=90.0),
        book=_book(100.0, 90.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert closed is not None
    assert closed["reason"] == "session_gap"
    assert closed["day"] == "2026-09-07"
    assert closed["pnl"] is not None and closed["pnl"] > 0
    assert bot.traded_day == "2026-09-07"
    assert bot.day_pnl == closed["pnl"]
    assert bot.entries_today == 1
    rows = [
        __import__("json").loads(line)
        for line in bot.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(r.get("event") == "day_pnl" and r.get("day") == "2026-09-04" for r in rows)
    blocked = bot.on_frame(
        now=_now("14:00", day="2026-09-07"),
        feed=_feed(ce=100.0, pe=90.0),
        book=_book(100.0, 90.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert blocked is None
    assert bot.position is None


def test_trend_missing_blocks(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot, index_nifty_chg=None) is None
    assert bot.position is None


def test_held_quotes_ignore_current_atm_feed(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    mid = bot.on_frame(
        now=_now("14:00"),
        feed=_feed(ce=10.0, pe=10.0),
        book=_Book({}),
        ce_symbol="NFO:CE2",
        pe_symbol="NFO:PE2",
        atm=23500,
    )
    assert mid is None
    assert bot.position is not None
    closed = bot.on_frame(
        now=_now("15:14"),
        feed=_feed(ce=10.0, pe=10.0),
        book=_Book({}),
        ce_symbol="NFO:CE2",
        pe_symbol="NFO:PE2",
        atm=23500,
    )
    assert closed is not None
    assert closed["reason"] == "time_flat"
    assert closed["pnl_known"] is False
    assert closed["pnl"] is None
    assert bot.day_pnl == 0.0


def test_close_write_fail_keeps_position(tmp_path: Path, monkeypatch) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    monkeypatch.setattr(bot, "_append", lambda ev: None)
    closed = bot.on_frame(
        now=_now("15:14"),
        feed=_feed(ce=100.0, pe=90.0),
        book=_book(100.0, 90.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert closed is None
    assert bot.position is not None
    assert bot.day_pnl == 0.0


def test_seals_day_pnl_before_roll(tmp_path: Path) -> None:
    bot = _bot(tmp_path, stop_pct=0.06)
    assert _open(bot) is not None
    closed = bot.on_frame(
        now=_now("14:20"),
        feed=_feed(ce=140.0, pe=120.0),
        book=_book(140.0, 120.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert closed is not None
    assert closed["pnl"] < 0
    loss = closed["pnl"]
    assert bot.eod_written is False
    sealed = bot.on_frame(
        now=_now("09:20", day="2026-09-07"),
        feed=_feed(),
        book=_book(120.0, 110.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert sealed is not None
    assert sealed["event"] == "day_pnl"
    assert sealed["day"] == "2026-09-04"
    assert sealed["day_pnl"] == loss
    assert bot.traded_day == "2026-09-07"
    assert bot.day_pnl == 0.0
    assert bot.entries_today == 0


def test_idle_day_skips_zero_eod(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = bot.on_frame(
        now=_now("15:14"),
        feed=_feed(),
        book=_book(120.0, 110.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert ev is None
    assert bot.eod_written is True
    assert (not bot.path.exists()) or bot.path.read_text() == ""


def test_weekend_flatten_reason(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    closed = bot.on_frame(
        now=_now("10:00", day="2026-09-05"),
        feed=_feed(ce=100.0, pe=90.0),
        book=_book(100.0, 90.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert closed is not None
    assert closed["reason"] == "weekend"
    assert closed["day"] == "2026-09-05"
    assert bot.traded_day == "2026-09-05"
    assert bot.day_pnl == closed["pnl"]


def test_restored_zero_stop_is_recomputed(tmp_path: Path) -> None:
    path = tmp_path / "short.jsonl"
    path.write_text(
        '{"event":"open","strategy":"short_atm_straddle","day":"2026-09-04",'
        '"atm":23400,"ce_symbol":"NFO:CE","pe_symbol":"NFO:PE","qty":65,'
        '"lots":1,"ce_entry":120,"pe_entry":110,"straddle_entry":230,'
        '"stop_straddle":0,"charges":0}\n'
    )
    bot = PaperShortStraddle(path=path, lot_size=65, lots=1)
    assert bot.position is not None
    assert bot.position.stop_straddle == 0.0


def test_restores_open(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    again = PaperShortStraddle(path=tmp_path / "short.jsonl", lot_size=65, lots=1)
    assert again.position is not None
    assert again.position.straddle_entry == 230.0
    assert again.entries_today == 1


def test_restart_keeps_leftover_flatten_cap(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    closed = bot.on_frame(
        now=_now("09:20", day="2026-09-07"),
        feed=_feed(ce=100.0, pe=90.0),
        book=_book(100.0, 90.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert closed is not None
    assert bot.entries_today == 1
    again = PaperShortStraddle(path=tmp_path / "short.jsonl", lot_size=65, lots=1)
    assert again.position is None
    assert again.traded_day == "2026-09-07"
    assert again.entries_today == 1
    blocked = again.on_frame(
        now=_now("14:00", day="2026-09-07"),
        feed=_feed(ce=100.0, pe=90.0),
        book=_book(100.0, 90.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert blocked is None
    assert again.position is None
