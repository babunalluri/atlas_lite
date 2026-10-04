"""Paper ATM impulse-fade scalp — opposite wing after a 3m ≥12pt move."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite.paper_impulse_fade import (
    PaperImpulseFade,
    fade_side,
    impulse_pts,
    in_impulse_fade_entry_window,
    last_session_bar_minute,
    session_spot_closes,
)

IST = ZoneInfo("Asia/Kolkata")
UP = [23100.0, 23104.0, 23108.0, 23113.0]
DN = [23113.0, 23108.0, 23104.0, 23100.0]
FLAT = [23100.0, 23104.0, 23108.0, 23110.0]


class _Book:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def get(self, symbol: str):
        return self.rows.get(symbol)


def _now(hm: str, day: str = "2026-09-04") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def _bot(path: Path, **kwargs) -> PaperImpulseFade:
    return PaperImpulseFade(path=path / "scalp.jsonl", lot_size=65, lots=1, **kwargs)


def _feed(**kwargs) -> dict:
    feed = {
        "ce": 120.0,
        "pe": 100.0,
        "ce_symbol": "NFO:CE",
        "pe_symbol": "NFO:PE",
    }
    feed.update(kwargs)
    return feed


def _book(ce: float, pe: float) -> _Book:
    return _Book({"NFO:CE": {"last_price": ce}, "NFO:PE": {"last_price": pe}})


def _frame(
    bot: PaperImpulseFade,
    hm: str = "10:24",
    *,
    closes: list[float] | None = None,
    day: str = "2026-09-04",
    ce: float = 120.0,
    pe: float = 100.0,
    signal_minute: str | None = None,
    **feed_kw,
) -> dict | None:
    return bot.on_frame(
        now=_now(hm, day=day),
        feed=_feed(ce=ce, pe=pe, **feed_kw),
        book=_book(ce, pe),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=23400,
        spot_closes=closes if closes is not None else UP,
        signal_minute=signal_minute,
    )


def test_session_closes_ignore_prior_day_and_forming_spike() -> None:
    bars = [
        {"t": "2026-09-03 15:28", "c": 23000.0},
        {"t": "2026-09-03 15:29", "c": 23010.0},
        {"t": "2026-09-04 09:28", "c": 23100.0},
        {"t": "2026-09-04 09:29", "c": 23104.0},
        {"t": "2026-09-04 09:30", "c": 23108.0},
        {"t": "2026-09-04 09:31", "c": 23105.0},
    ]
    closes = session_spot_closes(bars, "2026-09-04")
    assert closes == [23100.0, 23104.0, 23108.0, 23105.0]
    assert impulse_pts(closes) == 5.0
    assert last_session_bar_minute(bars, "2026-09-04") == "2026-09-04 09:31"
    gap = [23000.0, 23010.0, 23100.0, 23104.0, 23108.0]
    assert impulse_pts(gap) == 98.0


def test_impulse_and_fade_side() -> None:
    assert impulse_pts(UP) == 13.0
    assert impulse_pts(FLAT) == 10.0
    assert impulse_pts(UP[:3]) is None
    assert fade_side(13.0) == "pe"
    assert fade_side(-13.0) == "ce"
    assert fade_side(0.0) is None


def test_entry_window() -> None:
    assert in_impulse_fade_entry_window(_now("09:29")) is False
    assert in_impulse_fade_entry_window(_now("09:30")) is True
    assert in_impulse_fade_entry_window(_now("14:45")) is True
    assert in_impulse_fade_entry_window(_now("14:46")) is False
    assert in_impulse_fade_entry_window(_now("15:14")) is False


def test_opens_pe_on_up_impulse(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = _frame(bot)
    assert ev is not None
    assert ev["event"] == "open"
    assert ev["side"] == "pe"
    assert ev["entry"] == 100.0
    assert ev["target"] == 108.0
    assert ev["stop"] == 94.0
    assert ev["impulse"] == 13.0
    assert bot.entries_today == 1


def test_opens_ce_on_down_impulse(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = _frame(bot, closes=DN)
    assert ev is not None
    assert ev["side"] == "ce"
    assert ev["entry"] == 120.0
    assert ev["target"] == 129.6
    assert ev["stop"] == 112.8


def test_blocks_small_impulse(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, closes=FLAT) is None
    assert bot.position is None


def test_blocks_before_window(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:29") is None


def test_target_is_profit(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    closed = _frame(bot, "10:26", pe=108.5, ce=110.0)
    assert closed is not None
    assert closed["reason"] == "target"
    assert closed["pnl"] is not None
    assert closed["pnl"] > 0


def test_stop_is_loss(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    closed = _frame(bot, "10:26", pe=93.0, ce=130.0)
    assert closed is not None
    assert closed["reason"] == "stop"
    assert closed["pnl"] < 0


def test_time_stop_at_12m(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    assert _frame(bot, "10:35", pe=101.0) is None
    closed = _frame(bot, "10:36", pe=101.0)
    assert closed is not None
    assert closed["reason"] == "time"


def test_max_four_per_day(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    slots = (("09:40", "09:42"), ("10:10", "10:12"), ("10:40", "10:42"), ("11:10", "11:12"))
    for open_hm, close_hm in slots:
        assert _frame(bot, open_hm) is not None
        closed = _frame(bot, close_hm, pe=108.5)
        assert closed is not None
        assert closed["event"] == "close"
    assert bot.entries_today == 4
    assert _frame(bot, "12:00") is None


def test_same_closed_minute_fires_once(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    first = _frame(bot, "10:24", signal_minute="2026-09-04 10:23")
    assert first is not None
    assert first["signal_minute"] == "2026-09-04 10:23"
    assert _frame(bot, "10:26", pe=108.5) is not None
    bot.last_exit_at = None
    assert _frame(bot, "10:34", signal_minute="2026-09-04 10:23") is None
    opened = _frame(bot, "10:34", signal_minute="2026-09-04 10:24")
    assert opened is not None
    assert opened["signal_minute"] == "2026-09-04 10:24"


def test_cooldown_blocks_reentry(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "10:24") is not None
    assert _frame(bot, "10:26", pe=108.5) is not None
    assert bot.position is None
    assert _frame(bot, "10:30") is None
    assert _frame(bot, "10:34") is not None


def test_restores_open(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    again = PaperImpulseFade(path=tmp_path / "scalp.jsonl", lot_size=65, lots=1)
    assert again.position is not None
    assert again.position.side == "pe"
    assert again.position.entry == 100.0
    assert again.position.target == 108.0
    assert again.entries_today == 1


def test_seals_day_pnl_before_roll(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    closed = _frame(bot, "10:26", pe=108.5)
    assert closed is not None
    profit = closed["pnl"]
    sealed = _frame(bot, "09:40", day="2026-09-07")
    assert sealed is not None
    assert sealed["event"] == "day_pnl"
    assert sealed["day"] == "2026-09-04"
    assert sealed["day_pnl"] == profit
    assert bot.traded_day == "2026-09-07"
    assert bot.day_pnl == 0.0


def test_leftover_flatten_does_not_cap_the_day(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    closed = _frame(bot, "09:20", day="2026-09-07", pe=101.0)
    assert closed is not None
    assert closed["reason"] == "session_gap"
    assert bot.entries_today == 1
    assert bot.position is None
    opened = _frame(bot, "09:40", day="2026-09-07")
    assert opened is not None
    assert opened["event"] == "open"
    assert bot.entries_today == 2


def test_weekend_flatten_reason(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    closed = _frame(bot, "10:00", day="2026-09-05", pe=101.0)
    assert closed is not None
    assert closed["reason"] == "weekend"


def test_close_write_fail_keeps_position(tmp_path: Path, monkeypatch) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot) is not None
    monkeypatch.setattr(bot, "_append", lambda ev: None)
    closed = _frame(bot, "10:36", pe=101.0)
    assert closed is None
    assert bot.position is not None
