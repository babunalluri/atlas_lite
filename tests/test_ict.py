"""ICT bias, sweep, FVG entry, and paper open."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite.ict import ICTConfig, aggregate_bars, generate_entry, htf_bias
from atlas_lite.paper_ict import PaperICT

IST = ZoneInfo("Asia/Kolkata")
CFG = ICTConfig(
    swing_left=1,
    swing_right=1,
    atr_period=3,
    body_median_period=3,
    displacement_body_mult=1.2,
    fvg_atr_min=0.05,
    minimum_rr=2.0,
    sweep_lookback=8,
    fvg_retrace_bars=4,
    sl_atr_buffer=0.05,
)


def _bar(i: int, o: float, h: float, l: float, c: float, day: str = "2026-10-07") -> dict:
    minute = 15 + i
    hour = 9 + minute // 60
    minute = minute % 60
    return {"t": f"{day} {hour:02d}:{minute:02d}", "o": o, "h": h, "l": l, "c": c}


def _uptrend(n: int = 16) -> list[dict]:
    """15m-like bars with two higher swing highs and two higher swing lows."""
    bars = []
    px = 100.0
    for i in range(n):
        # Plant swing lows at 3 and 8, swing highs at 5 and 11.
        if i == 3:
            bars.append(_bar(i, px, px + 1, 96.0, px))
        elif i == 5:
            bars.append(_bar(i, px, 104.0, px - 0.4, px + 0.2))
        elif i == 8:
            bars.append(_bar(i, px, px + 1, 98.0, px))
        elif i == 11:
            bars.append(_bar(i, px, 108.0, px - 0.4, px + 0.2))
        else:
            bars.append(_bar(i, px, px + 0.8, px - 0.4, px + 0.3))
        px += 0.4
    return bars


def test_htf_bias_long() -> None:
    assert htf_bias(_uptrend(), CFG) == "LONG"


def test_aggregate_5m() -> None:
    bars = [
        {"t": "2026-10-07 09:15", "o": 1, "h": 2, "l": 0.5, "c": 1.5},
        {"t": "2026-10-07 09:16", "o": 1.5, "h": 3, "l": 1.2, "c": 2.5},
        {"t": "2026-10-07 09:20", "o": 2.5, "h": 2.6, "l": 2.0, "c": 2.1},
    ]
    out = aggregate_bars(bars, 5)
    assert len(out) == 2
    assert out[0]["t"] == "2026-10-07 09:15"
    assert out[0]["h"] == 3
    assert out[0]["l"] == 0.5
    assert out[0]["c"] == 2.5
    assert out[1]["o"] == 2.5


def _long_execution() -> list[dict]:
    """Sell-side sweep, displacement as the middle candle, then a later touch of the gap.

    The gap is high[bar before displacement] to low[bar after]. The last bar
    opens above that gap and trades into it. Its close stays above the gap.
    """
    bars: list[dict] = []
    px = 100.0
    for i in range(20):
        bars.append(_bar(i, px, px + 0.6, px - 0.3, px + 0.2))
        px += 0.15
    bars[7] = _bar(7, 101.2, 101.8, 101.0, 101.3)
    bars[8] = _bar(8, 101.0, 101.4, 100.0, 101.1)
    bars[9] = _bar(9, 101.1, 101.6, 100.8, 101.2)
    bars[11] = _bar(11, 101.8, 102.4, 101.6, 102.0)
    bars[12] = _bar(12, 102.0, 112.0, 101.6, 102.2)
    bars[13] = _bar(13, 102.1, 102.8, 101.7, 102.3)
    bars[14] = _bar(14, 101.4, 101.8, 99.85, 100.6)
    bars[15] = _bar(15, 100.7, 101.0, 100.4, 100.8)
    bars[16] = _bar(16, 100.8, 101.0, 100.5, 100.9)  # candle before the impulse
    bars[17] = _bar(17, 101.2, 116.0, 104.0, 115.0)  # displacement, middle candle
    bars[18] = _bar(18, 114.0, 115.0, 102.0, 114.2)  # prints the gap above 101.0
    bars[19] = _bar(19, 106.0, 106.4, 101.5, 105.5)  # opens above the gap, trades into it
    return bars


def test_generate_long_on_fvg_retrace() -> None:
    sig = generate_entry(_uptrend(), _long_execution(), CFG)
    assert sig.signal == "LONG", sig.reason
    assert sig.stop_loss is not None and sig.target is not None and sig.entry is not None
    assert sig.stop_loss < sig.entry < sig.target
    assert sig.risk_reward is not None and sig.risk_reward >= 2.0
    assert sig.entry == 102.0  # first touch is the top of the gap, not the close
    assert sig.metadata["setup_id"]


class _Book:
    def __init__(self, px: float) -> None:
        self.px = px

    def get(self, _symbol: str) -> dict:
        return {"last_price": self.px}


def test_paper_ict_opens_call_and_stops(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.ict import ICTSignal

    sig = ICTSignal(
        signal="LONG",
        reason="ICT long setup confirmed",
        entry=102.2,
        stop_loss=99.5,
        target=108.0,
        risk_reward=2.4,
    )
    monkeypatch.setattr("atlas_lite.paper_ict.generate_entry", lambda *_a, **_k: sig)
    bot = PaperICT(path=tmp_path / "paper_ict.jsonl", lot_size=65, lots=1, config=CFG)
    assert bot.max_entries_per_day == 0
    bars = [{"t": "2026-10-07 10:00", "o": 102, "h": 103, "l": 101, "c": 102.2}]
    now = datetime(2026, 10, 7, 10, 10, tzinfo=IST)
    ev = bot.on_frame(
        now=now,
        feed={"ce_symbol": "NFO:CE", "pe_symbol": "NFO:PE", "spot": 102.2},
        book=_Book(40.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=22600,
        bars_1m=bars,
        spot=102.2,
    )
    assert ev is not None, bot.last_reject
    assert ev["event"] == "open"
    assert ev["side"] == "ce"
    assert ev["symbol"] == "NFO:CE"
    assert bot.position is not None
    closed = bot.on_frame(
        now=datetime(2026, 10, 7, 10, 12, tzinfo=IST),
        feed={},
        book=_Book(30.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=22600,
        bars_1m=bars,
        spot=99.0,
    )
    assert closed is not None
    assert closed["event"] == "close"
    assert closed["reason"] == "stop"
    assert closed["pnl"] is not None
    assert bot.position is None


def test_fill_skips_when_spot_has_eaten_the_reward(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.ict import ICTSignal

    sig = ICTSignal(
        signal="LONG",
        reason="ICT long setup confirmed",
        entry=102.2,
        stop_loss=99.5,
        target=108.0,
        risk_reward=2.4,
        metadata={"setup_id": "sweep|disp"},
    )
    monkeypatch.setattr("atlas_lite.paper_ict.generate_entry", lambda *_a, **_k: sig)
    bot = PaperICT(path=tmp_path / "paper_ict.jsonl", lot_size=65, lots=1, config=CFG)
    bars = [{"t": "2026-10-07 10:00", "o": 102, "h": 106, "l": 101, "c": 105.5}]
    ev = bot.on_frame(
        now=datetime(2026, 10, 7, 10, 10, tzinfo=IST),
        feed={},
        book=_Book(40.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=22600,
        bars_1m=bars,
        spot=105.5,
    )
    assert ev is None
    assert bot.last_reject == "risk_reward_at_fill"
    assert bot.position is None
    assert "sweep|disp" not in bot.used_setups


def test_fill_rejects_when_spot_is_missing(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.ict import ICTSignal

    sig = ICTSignal(
        signal="LONG",
        reason="ICT long setup confirmed",
        entry=102.2,
        stop_loss=99.5,
        target=108.0,
        risk_reward=2.4,
    )
    monkeypatch.setattr("atlas_lite.paper_ict.generate_entry", lambda *_a, **_k: sig)
    bot = PaperICT(path=tmp_path / "paper_ict.jsonl", lot_size=65, lots=1, config=CFG)
    bars = [{"t": "2026-10-07 10:00", "o": 102, "h": 103, "l": 101, "c": 102.2}]
    ev = bot.on_frame(
        now=datetime(2026, 10, 7, 10, 10, tzinfo=IST),
        feed={},
        book=_Book(40.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=22600,
        bars_1m=bars,
        spot=None,
    )
    assert ev is None
    assert bot.last_reject == "missing_spot"
    assert bot.position is None


def test_signal_bar_wick_does_not_exit(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.ict import ICTSignal

    sig = ICTSignal(
        signal="LONG",
        reason="ICT long setup confirmed",
        entry=102.2,
        stop_loss=99.5,
        target=108.0,
        risk_reward=2.4,
        metadata={"setup_id": "sweep|disp"},
    )
    monkeypatch.setattr("atlas_lite.paper_ict.generate_entry", lambda *_a, **_k: sig)
    bot = PaperICT(path=tmp_path / "paper_ict.jsonl", lot_size=65, lots=1, config=CFG)
    bars = [{"t": "2026-10-07 10:00", "o": 102, "h": 109, "l": 99.0, "c": 102.2}]
    opened = bot.on_frame(
        now=datetime(2026, 10, 7, 10, 10, tzinfo=IST),
        feed={},
        book=_Book(40.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=22600,
        bars_1m=bars,
        spot=102.2,
    )
    assert opened is not None and opened["event"] == "open"
    still = bot.on_frame(
        now=datetime(2026, 10, 7, 10, 11, tzinfo=IST),
        feed={},
        book=_Book(40.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=22600,
        bars_1m=bars,
        spot=102.0,
    )
    assert still is None
    assert bot.position is not None


def test_same_setup_is_not_reentered(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.ict import ICTSignal

    sig = ICTSignal(
        signal="LONG",
        reason="ICT long setup confirmed",
        entry=102.2,
        stop_loss=99.5,
        target=108.0,
        risk_reward=2.4,
        metadata={"setup_id": "sweep|disp"},
    )
    monkeypatch.setattr("atlas_lite.paper_ict.generate_entry", lambda *_a, **_k: sig)
    bot = PaperICT(path=tmp_path / "paper_ict.jsonl", lot_size=65, lots=1, config=CFG)
    bars = [{"t": "2026-10-07 10:00", "o": 102, "h": 103, "l": 101, "c": 102.2}]
    kwargs = dict(
        feed={},
        book=_Book(40.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=22600,
        bars_1m=bars,
        spot=102.2,
    )
    opened = bot.on_frame(now=datetime(2026, 10, 7, 10, 10, tzinfo=IST), **kwargs)
    assert opened is not None
    closed = bot.on_frame(
        now=datetime(2026, 10, 7, 10, 12, tzinfo=IST),
        feed={},
        book=_Book(30.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=22600,
        bars_1m=bars,
        spot=99.0,
    )
    assert closed is not None and closed["reason"] == "stop"
    later = [{"t": "2026-10-07 10:20", "o": 102, "h": 103, "l": 101, "c": 102.2}]
    again = bot.on_frame(
        now=datetime(2026, 10, 7, 10, 30, tzinfo=IST),
        feed={},
        book=_Book(40.0),
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=22600,
        bars_1m=later,
        spot=102.2,
    )
    assert again is None
    assert bot.last_reject == "setup_used"
    assert bot.position is None
