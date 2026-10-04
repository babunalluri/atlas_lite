"""Paper ATM skew fade — 11:00 sell-rich-wing, 15% stop, 1/day."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite.paper_skew_fade import (
    PaperSkewFade,
    in_skew_fade_entry_window,
    rich_atm_side,
)

IST = ZoneInfo("Asia/Kolkata")


class _Book:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def get(self, symbol: str):
        return self.rows.get(symbol)


def _now(hm: str, day: str = "2026-09-04") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def _bot(path: Path, **kwargs) -> PaperSkewFade:
    return PaperSkewFade(path=path / "skew.jsonl", lot_size=65, lots=1, **kwargs)


def _feed(**kwargs) -> dict:
    feed = {
        "ce": 130.0,
        "pe": 100.0,
        "ce_symbol": "NFO:CE",
        "pe_symbol": "NFO:PE",
        "index_nifty_chg": 0.2,
    }
    feed.update(kwargs)
    return feed


def _book(ce: float, pe: float) -> _Book:
    return _Book({"NFO:CE": {"last_price": ce}, "NFO:PE": {"last_price": pe}})


def _open(bot: PaperSkewFade, hm: str = "11:00", **feed_kw) -> dict | None:
    return bot.on_frame(
        now=_now(hm),
        feed=_feed(**feed_kw),
        book=_book(feed_kw.get("ce", 130.0), feed_kw.get("pe", 100.0)),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )


def test_rich_side() -> None:
    assert rich_atm_side(130.0, 100.0) == "ce"
    assert rich_atm_side(100.0, 130.0) == "pe"
    assert rich_atm_side(110.0, 108.0) is None
    assert rich_atm_side(112.0, 100.0) == "ce"


def test_entry_window() -> None:
    assert in_skew_fade_entry_window(_now("10:59")) is False
    assert in_skew_fade_entry_window(_now("11:00")) is True
    assert in_skew_fade_entry_window(_now("11:15")) is True
    assert in_skew_fade_entry_window(_now("11:16")) is False
    assert in_skew_fade_entry_window(_now("13:00")) is False
    assert in_skew_fade_entry_window(_now("15:14")) is False


def test_opens_rich_ce_at_1100(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = _open(bot)
    assert ev is not None
    assert ev["event"] == "open"
    assert ev["side"] == "ce"
    assert ev["entry"] == 130.0
    assert ev["stop"] == 149.5
    assert ev["skew"] == 30.0
    assert bot.position is not None
    assert bot.entries_today == 1
    assert bot.filled_day == "2026-09-04"


def test_opens_rich_pe(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = _open(bot, ce=90.0, pe=120.0)
    assert ev is not None
    assert ev["side"] == "pe"
    assert ev["entry"] == 120.0
    assert ev["stop"] == 138.0


def test_blocks_flat_skew(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot, ce=110.0, pe=108.0) is None
    assert bot.position is None


def test_blocks_before_window(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot, "10:59") is None
    assert bot.position is None


def test_one_entry_per_day(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot, "11:00") is not None
    bot.position = None
    assert _open(bot, "11:05") is None


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
    assert closed["exit"] == 100.0
    assert closed["pnl_gross"] == 1950.0  # (130-100)*65
    assert closed["pnl"] is not None and closed["pnl"] < closed["pnl_gross"]
    assert closed["pnl"] > 0
    assert bot.position is None


def test_stop_when_rich_wing_rises_15pct(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    # 130 * 1.15 = 149.5
    closed = bot.on_frame(
        now=_now("12:40"),
        feed=_feed(),
        book=_book(150.0, 100.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert closed is not None
    assert closed["reason"] == "stop"
    assert closed["exit"] == 150.0
    assert closed["pnl"] < 0


def test_no_early_take_when_premium_decays(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    mid = bot.on_frame(
        now=_now("13:00"),
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
    assert bot.filled_day == "2026-09-04"
    rows = [
        __import__("json").loads(line)
        for line in bot.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(r.get("event") == "day_pnl" and r.get("day") == "2026-09-04" for r in rows)
    blocked = bot.on_frame(
        now=_now("11:00", day="2026-09-07"),
        feed=_feed(ce=100.0, pe=90.0),
        book=_book(100.0, 90.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert blocked is None
    assert bot.position is None


def test_default_is_one_lot(tmp_path: Path) -> None:
    bot = PaperSkewFade(path=tmp_path / "skew.jsonl", lot_size=65)
    ev = _open(bot)
    assert ev is not None
    assert ev["lots"] == 1
    assert ev["qty"] == 65
    assert ev["side"] == "ce"


def test_trend_missing_blocks(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot, index_nifty_chg=None) is None
    assert bot.position is None


def test_held_quotes_ignore_current_atm_feed(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    mid = bot.on_frame(
        now=_now("13:00"),
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
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    closed = bot.on_frame(
        now=_now("12:40"),
        feed=_feed(ce=160.0, pe=90.0),
        book=_book(160.0, 90.0),
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
        book=_book(130.0, 100.0),
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


def test_idle_day_skips_zero_eod(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = bot.on_frame(
        now=_now("15:14"),
        feed=_feed(),
        book=_book(130.0, 100.0),
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
    path = tmp_path / "skew.jsonl"
    path.write_text(
        '{"event":"open","strategy":"atm_skew_fade","day":"2026-09-04",'
        '"atm":23400,"side":"ce","symbol":"NFO:CE","ce_symbol":"NFO:CE",'
        '"pe_symbol":"NFO:PE","qty":65,"lots":1,"entry":130,"stop":0,'
        '"ce_entry":130,"pe_entry":100,"charges":0}\n'
    )
    bot = PaperSkewFade(path=path, lot_size=65, lots=1)
    assert bot.position is not None
    assert bot.position.stop == 149.5


def test_restores_open(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _open(bot) is not None
    again = PaperSkewFade(path=tmp_path / "skew.jsonl", lot_size=65, lots=1)
    assert again.position is not None
    assert again.position.side == "ce"
    assert again.position.entry == 130.0
    assert again.entries_today == 1
    assert again.filled_day == "2026-09-04"


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
    again = PaperSkewFade(path=tmp_path / "skew.jsonl", lot_size=65, lots=1)
    assert again.position is None
    assert again.traded_day == "2026-09-07"
    assert again.entries_today == 1
    assert again.filled_day == "2026-09-04"
    blocked = again.on_frame(
        now=_now("11:00", day="2026-09-07"),
        feed=_feed(ce=100.0, pe=90.0),
        book=_book(100.0, 90.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
    )
    assert blocked is None
    assert again.position is None
