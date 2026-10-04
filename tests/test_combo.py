"""COMBO confluence matches the chart votes (latch resets below 4)."""

from datetime import datetime, timedelta

from atlas_lite.combo import confluence_rows, rsi_series


def test_rsi_bull_threshold() -> None:
    closes = [100.0 + i for i in range(20)]
    rsi = rsi_series(closes)
    assert rsi[-1] is not None
    assert rsi[-1] > 60


def _up_bars(n: int = 50) -> list[dict]:
    start = datetime(2026, 9, 4, 9, 15)
    bars = []
    px = 22800.0
    for i in range(n):
        t = start + timedelta(minutes=i)
        px += 4.0
        bars.append(
            {
                "t": t.strftime("%Y-%m-%d %H:%M"),
                "o": px - 2,
                "h": px + 2,
                "l": px - 2,
                "c": px,
            }
        )
    return bars


def test_flip_needs_four_and_only_on_change() -> None:
    rows = confluence_rows(_up_bars())
    flips = [r for r in rows if r["signal"]]
    assert flips
    assert flips[0]["signal"] == "B"
    assert all(r["bull"] >= 4 or r["bear"] >= 4 for r in flips)
    sides = [r["signal"] for r in flips]
    for a, b in zip(sides, sides[1:]):
        assert a != b
