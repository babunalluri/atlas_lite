"""ADX minute-bar pipeline tests."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from atlas_lite.metrics import compute_adx, compute_atr
from atlas_lite.minute_bars import MinuteBarBuilder, _minute_key

IST = ZoneInfo("Asia/Kolkata")


def _seed_bars(n: int, *, start_minute: int = 0) -> MinuteBarBuilder:
    b = MinuteBarBuilder(symbol="NSE:NIFTY 50")
    for i in range(n):
        px = 24000.0 + i
        b.bars.append(
            {
                "t": f"2026-09-02 10:{start_minute + i:02d}",
                "o": px,
                "h": px + 5,
                "l": px - 5,
                "c": px + 1,
            }
        )
    return b


def test_trim_incomplete_current_bar() -> None:
    b = _seed_bars(3)
    with patch("atlas_lite.minute_bars._minute_key", return_value=b.bars[-1]["t"]):
        assert b.trim_incomplete_current_bar() is True
        assert len(b.bars) == 2


def test_day_ohlc_does_not_corrupt_minute_bar() -> None:
    b = MinuteBarBuilder(symbol="NSE:NIFTY 50")
    day_ohlc = {"open": 23800, "high": 24500, "low": 23700, "close": 24100}
    with patch("atlas_lite.minute_bars._minute_key", return_value="2026-09-02 10:01"):
        b.ingest(24100.0, day_ohlc)
        b.ingest(24101.0, day_ohlc)
    assert b._high == 24101.0
    assert b._low == 24100.0


def test_ingest_finalizes_on_minute_roll() -> None:
    b = _seed_bars(30)
    with patch("atlas_lite.minute_bars._minute_key", return_value="2026-09-02 10:29"):
        assert b.ingest(24029.0) is False
    with patch("atlas_lite.minute_bars._minute_key", return_value="2026-09-02 10:45"):
        finalized = b.ingest(24100.0)
    assert finalized is True
    assert b.bars[-1]["t"] == "2026-09-02 10:29"
    assert b._current_key == "2026-09-02 10:45"


def test_cash_session_filter_drops_preopen_bars() -> None:
    from atlas_lite.minute_bars import is_cash_session_minute

    assert is_cash_session_minute("2026-09-07 09:15")
    assert is_cash_session_minute("2026-09-07 15:29")
    assert not is_cash_session_minute("2026-09-07 08:59")
    assert not is_cash_session_minute("2026-09-07 07:30")
    assert not is_cash_session_minute("2026-09-07 15:30")
    b = MinuteBarBuilder(symbol="NSE:NIFTY 50")
    b.bars = [
        {"t": "2026-09-07 07:30", "o": 1, "h": 2, "l": 1, "c": 1.5},
        {"t": "2026-09-07 09:20", "o": 1, "h": 2, "l": 1, "c": 1.5},
        {"t": "2026-09-07 15:30", "o": 1, "h": 2, "l": 1, "c": 1.5},
    ]
    assert b.drop_non_session_bars() == 2
    assert len(b.bars) == 1
    assert b.bars[0]["t"] == "2026-09-07 09:20"


def test_in_adx_seed_window_weekdays_only() -> None:
    from atlas_lite.minute_bars import in_adx_seed_window

    mon_open = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
    mon_last = datetime(2026, 9, 7, 15, 29, tzinfo=IST)
    mon_close = datetime(2026, 9, 7, 15, 30, tzinfo=IST)
    mon_pre = datetime(2026, 9, 7, 7, 30, tzinfo=IST)
    mon_night = datetime(2026, 9, 7, 2, 0, tzinfo=IST)
    sat_noon = datetime(2026, 9, 5, 11, 0, tzinfo=IST)
    assert in_adx_seed_window(mon_open)
    assert in_adx_seed_window(mon_last)
    assert not in_adx_seed_window(mon_close)
    assert not in_adx_seed_window(mon_pre)
    assert not in_adx_seed_window(mon_night)
    assert not in_adx_seed_window(sat_noon)


def test_adx_from_closed_bars() -> None:
    b = _seed_bars(40)
    highs, lows, closes = b.ohlc_series(closed_only=True)
    adx = compute_adx(highs, lows, closes)
    atr = compute_atr(highs, lows, closes)
    assert adx is not None
    assert atr is not None
    assert 0 <= adx <= 100


def test_load_candles_trims_current_minute() -> None:
    current = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    candles = [
        ["2026-09-02 10:00:00", 100, 101, 99, 100.5],
        [f"{current}:00", 200, 201, 199, 200.5],
    ]
    b = MinuteBarBuilder(symbol="NSE:NIFTY 50")
    with patch("atlas_lite.minute_bars._minute_key", return_value=current):
        b.load_candles(candles)
    assert len(b.bars) == 1


def test_ohlc_series_closed_only_skips_current_minute_in_bars() -> None:
    b = _seed_bars(5)
    current = b.bars[-1]["t"]
    with patch("atlas_lite.minute_bars._minute_key", return_value=current):
        highs, lows, closes = b.ohlc_series(closed_only=True)
    assert len(closes) == 4
    assert closes[-1] == b.bars[-2]["c"]


def test_chart_bars_includes_live_minute() -> None:
    b = _seed_bars(2)
    with patch("atlas_lite.minute_bars._minute_key", return_value="2026-09-02 10:45"):
        b.ingest(24111.0)
        bars = b.chart_bars()
    assert [bar["t"] for bar in bars] == [
        "2026-09-02 10:00",
        "2026-09-02 10:01",
        "2026-09-02 10:45",
    ]
    assert bars[-1]["c"] == 24111.0


def test_load_candles_keeps_volume() -> None:
    b = MinuteBarBuilder(symbol="NSE:NIFTY 50")
    b.load_candles(
        [
            ["2026-09-02 10:00:00", 100, 101, 99, 100.5, 1200],
            ["2026-09-02 10:01:00", 100.5, 102, 100, 101.2, 800],
        ]
    )
    assert b.bars[0]["v"] == 1200
    assert b.bars[1]["v"] == 800


def test_merge_volume_fills_zero_bars() -> None:
    b = MinuteBarBuilder(symbol="NSE:NIFTY 50")
    b.bars = [
        {"t": "2026-09-02 10:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 0},
        {"t": "2026-09-02 10:01", "o": 1, "h": 1, "l": 1, "c": 1, "v": 50},
    ]
    updated = b.merge_volume_from_candles(
        [
            ["2026-09-02 10:00:00", 1, 1, 1, 1, 900],
            ["2026-09-02 10:01:00", 1, 1, 1, 1, 400],
        ]
    )
    assert updated == 1
    assert b.bars[0]["v"] == 900
    assert b.bars[1]["v"] == 50


def test_merge_fut_fills_oi() -> None:
    b = MinuteBarBuilder(symbol="NSE:NIFTY 50")
    b.bars = [
        {"t": "2026-09-02 10:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 0, "oi": 0},
    ]
    updated = b.merge_volume_from_candles(
        [["2026-09-02 10:00:00", 1, 1, 1, 1, 900, 125000]]
    )
    assert updated >= 1
    assert b.bars[0]["v"] == 900
    assert b.bars[0]["oi"] == 125000


def test_ingest_volume_accumulates_session_delta() -> None:
    b = MinuteBarBuilder(symbol="NSE:NIFTY 50")
    with patch("atlas_lite.minute_bars._minute_key", return_value="2026-09-02 10:45"):
        b.ingest(24100.0)
        b.ingest_volume(1000)
        b.ingest_volume(1300)
        bars = b.chart_bars()
    assert bars[-1]["v"] == 300


def test_ingest_oi_carries_into_live_bar() -> None:
    b = MinuteBarBuilder(symbol="NSE:NIFTY 50")
    with patch("atlas_lite.minute_bars._minute_key", return_value="2026-09-02 10:45"):
        b.ingest(24100.0)
        b.ingest_oi(125000)
        bars = b.chart_bars()
    assert bars[-1]["oi"] == 125000
