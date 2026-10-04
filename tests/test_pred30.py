"""PRED30 width helpers. The chart target lives in static/pred30.js."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from atlas_lite.pred30 import (
    TRADING_MINUTES_PER_YEAR,
    atr_half_width,
    ensemble_band,
    ichimoku_span_a,
    iv_half_width,
    iv_or_straddle_width,
    linreg_lookback,
    linreg_slope_and_sigma,
    minutes_left_in_session,
    straddle_half_width,
    trading_minutes_to_expiry,
    vote_side,
)

IST = ZoneInfo("Asia/Kolkata")


def test_minutes_left_floors_at_horizon() -> None:
    assert minutes_left_in_session(10, 0) == 330
    assert minutes_left_in_session(15, 20) == 30
    assert minutes_left_in_session(15, 45) == 30


def test_iv_uses_trading_minute_year() -> None:
    assert TRADING_MINUTES_PER_YEAR == 375.0 * 252.0
    # 25000 * 0.12 * sqrt(30 / 94500) ≈ 53.45
    w = iv_half_width(25000.0, 12.0)
    assert w == 53.45
    assert iv_half_width(23450.0, 0) is None
    assert iv_half_width(None, 12.0) is None


def test_straddle_scales_to_expiry_not_session() -> None:
    assert straddle_half_width(120.0, 270.0) == 40.0
    # 3 weekdays out + remaining today ≈ 3*375 + 270 = 1395
    assert straddle_half_width(320.0, 1395.0) == 46.93
    assert straddle_half_width(180.0, 30.0) == 180.0
    assert straddle_half_width(180.0, 29.0) is None
    assert straddle_half_width(0, 375) is None


def test_trading_minutes_to_expiry() -> None:
    now = datetime(2026, 9, 24, 11, 0, tzinfo=IST)
    assert trading_minutes_to_expiry(date(2026, 9, 24), now) == 270.0
    assert trading_minutes_to_expiry(date(2026, 9, 29), now) == 270.0 + 375.0 * 3
    assert trading_minutes_to_expiry(date(2026, 9, 24), datetime(2026, 9, 24, 15, 20, tzinfo=IST)) is None
    assert trading_minutes_to_expiry(None, now) is None


def test_atr_scales_with_bar_minutes() -> None:
    assert atr_half_width(10.0, 1) == 54.77
    assert atr_half_width(10.0, 5) == 24.49
    assert atr_half_width(10.0, 30) == 10.0
    assert atr_half_width(None, 1) is None


def test_linreg_lookback_covers_30m() -> None:
    assert linreg_lookback(1) == 30
    assert linreg_lookback(5) == 8
    assert linreg_lookback(15) == 8


def test_linreg_up_slope_and_residual() -> None:
    closes = [100.0 + i for i in range(12)]
    slope, sigma = linreg_slope_and_sigma(closes)
    assert slope is not None and slope > 0.9
    assert sigma is None or sigma < 0.1
    noisy = [100.0 + i + (2.0 if i % 2 else -2.0) for i in range(12)]
    n_slope, n_sigma = linreg_slope_and_sigma(noisy)
    assert n_slope is not None and n_slope > 0.8
    assert n_sigma is not None and n_sigma > 1.0


def test_ichimoku_span_a_mid_range() -> None:
    highs = [110.0] * 26
    lows = [90.0] * 26
    assert ichimoku_span_a(highs, lows) == 100.0
    assert ichimoku_span_a(highs[:20], lows[:20]) is None


def test_ensemble_median_and_tight() -> None:
    band = ensemble_band([21.0, 52.0, 40.0, 35.0])
    assert band["n"] == 4
    assert band["tight"] == 21.0
    assert band["median"] == 37.5
    assert ensemble_band([None, 0, -1])["n"] == 0


def test_vote_majority_label() -> None:
    assert vote_side([1, 1, 1, -1])["label"] == "3/4 up"
    assert vote_side([-1, -1, 1, -1])["label"] == "3/4 down"
    assert vote_side([1, 1, -1, -1])["label"] == "2/4 flat"
    assert vote_side([0, 0, 0, 0])["label"] == "0/0"


def test_iv_preferred_over_straddle() -> None:
    iv_w = iv_or_straddle_width(
        spot=25000.0, iv_pct=12.0, ce=90.0, pe=90.0, minutes_to_expiry=270
    )
    strap = iv_or_straddle_width(
        spot=25000.0, iv_pct=None, ce=60.0, pe=60.0, minutes_to_expiry=270
    )
    assert iv_w == 53.45
    assert strap == 40.0
    assert (
        iv_or_straddle_width(
            spot=25000.0, iv_pct=None, ce=None, pe=None, minutes_to_expiry=270
        )
        is None
    )
    assert (
        iv_or_straddle_width(
            spot=25000.0, iv_pct=None, ce=60.0, pe=60.0, minutes_to_expiry=20
        )
        is None
    )
