"""RV helpers used by scripts/rv_iv_study.py — no Kite."""

from scripts.rv_iv_study import close_to_close_rv_pct, parkinson_rv_pct


def test_parkinson_rv_positive_when_range() -> None:
    rv = parkinson_rv_pct(24800.0, 24600.0)
    assert rv is not None
    assert 5.0 < rv < 25.0


def test_kite_offset_without_colon_parses() -> None:
    from scripts.rv_iv_study import _candle_day

    assert _candle_day("2026-08-10T00:00:00+0530") == "2026-08-10"


def test_atm_straddle_credit_matches_gap_table() -> None:
    from scripts.rv_iv_study import atm_straddle_credit_pts

    credit = atm_straddle_credit_pts(23939.0, 10.16)
    assert 245.0 < credit < 255.0


def test_close_to_close_needs_window() -> None:
    closes = [24000.0 + i for i in range(10)]
    assert close_to_close_rv_pct(closes, window=20) is None
    trend = [24000.0 * (1.002 ** i) for i in range(30)]
    rv = close_to_close_rv_pct(trend, window=20)
    assert rv is not None
    assert rv > 0
