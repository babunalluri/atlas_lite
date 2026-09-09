"""Tier 1/2 accuracy tests — ADX closed bars + ATM IV history."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from atlas_lite.iv_history import (
    compute_ivp,
    ivp_history_stats,
    ivp_sample_values,
    needs_iv_history_rescale,
    prune_weekend_iv_samples,
    should_record_eod_iv,
)
from atlas_lite.metrics import chain_accumulated_totals
from atlas_lite.minute_bars import MinuteBarBuilder

IST = ZoneInfo("Asia/Kolkata")


def test_merge_kite_candles_replaces_closed_bar() -> None:
    b = MinuteBarBuilder(symbol="NSE:NIFTY 50")
    b.bars = [{"t": "2026-09-02 10:00", "o": 100, "h": 101, "l": 99, "c": 100.5}]
    with patch("atlas_lite.minute_bars._minute_key", return_value="2026-09-02 10:05"):
        updated = b.merge_kite_candles(
            [["2026-09-02 10:00:00", 100, 102, 98, 101.0]],
        )
    assert updated == 1
    assert b.bars[0]["h"] == 102.0
    assert b.bars[0]["c"] == 101.0


def test_merge_kite_candles_skips_forming_minute() -> None:
    current = "2026-09-02 10:05"
    b = MinuteBarBuilder(symbol="NSE:NIFTY 50")
    with patch("atlas_lite.minute_bars._minute_key", return_value=current):
        updated = b.merge_kite_candles([[f"{current}:00", 1, 2, 1, 1.5]])
    assert updated == 0
    assert b.bars == []


def test_ivp_excludes_today_from_samples() -> None:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    history = {
        "NSE:NIFTY 50": [
            {"day": "2020-01-01", "iv": 10.0},
            {"day": "2020-01-02", "iv": 11.0},
            {"day": "2020-01-03", "iv": 12.0},
            {"day": "2020-01-04", "iv": 13.0},
            {"day": "2020-01-05", "iv": 14.0},
            {"day": today, "iv": 99.0},
        ],
    }
    samples = ivp_sample_values(history, exclude_today=True)
    assert len(samples) == 5
    assert 99.0 not in samples
    ivp = compute_ivp(samples, 13.5)
    assert ivp == 80.0


def test_ivp_history_stats_counts_proxy_and_real() -> None:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    history = {
        "NSE:NIFTY 50": [
            {"day": "2020-01-01", "iv": 10.0, "proxy": True},
            {"day": "2020-01-02", "iv": 11.0, "proxy": True},
            {"day": "2020-01-03", "iv": 12.0},
            {"day": today, "iv": 99.0},
        ],
    }
    stats = ivp_history_stats(history)
    assert stats == {"proxy": 2, "real": 1, "total": 3}


def test_should_record_eod_iv_skips_weekend() -> None:
    sat = datetime(2026, 9, 5, 16, 0, tzinfo=IST)
    fri = datetime(2026, 9, 4, 16, 0, tzinfo=IST)
    assert should_record_eod_iv(sat) is False
    assert should_record_eod_iv(fri) is True


def test_needs_iv_history_rescale_detects_unscaled_vix(tmp_path) -> None:
    from atlas_lite.iv_history import IVP_HISTORY_FILE, IVP_HISTORY_META, save_iv_history_meta

    history = {
        "NSE:NIFTY 50": [
            {"day": "2020-01-01", "iv": 12.0, "proxy": True},
            {"day": "2020-01-02", "iv": 12.1, "proxy": True},
            {"day": "2020-01-03", "iv": 12.2, "proxy": True},
            {"day": "2020-01-04", "iv": 12.3, "proxy": True},
            {"day": "2020-01-05", "iv": 12.4, "proxy": True},
        ],
    }
    (tmp_path / IVP_HISTORY_FILE).write_text(json.dumps(history), encoding="utf-8")
    save_iv_history_meta(
        tmp_path,
        {
            "version": 4,
            "source": "atm_iv",
            "bootstrap_scale": 1.0,
        },
    )
    assert needs_iv_history_rescale(tmp_path) is True


def test_prune_weekend_iv_samples(tmp_path) -> None:
    import json
    from atlas_lite.iv_history import IVP_HISTORY_FILE

    history = {
        "NSE:NIFTY 50": [
            {"day": "2026-09-04", "iv": 13.0},
            {"day": "2026-09-05", "iv": 13.0},
            {"day": "2026-09-06", "iv": 13.0},
        ],
    }
    (tmp_path / IVP_HISTORY_FILE).write_text(json.dumps(history), encoding="utf-8")
    removed = prune_weekend_iv_samples(tmp_path)
    assert removed == 2


def test_merge_keeps_explicit_real_over_proxy() -> None:
    from atlas_lite.iv_history import _merge_proxy_with_real

    scaled = [{"day": "2020-01-01", "iv": 14.0, "proxy": True}]
    existing = [{"day": "2020-01-01", "iv": 16.5}]  # unlabeled = treated as real
    merged = _merge_proxy_with_real(scaled, existing)
    assert merged[0]["iv"] == 16.5


def test_v2_migration_drops_unlabeled_vix_by_clearing_existing() -> None:
    """v2 VIX rows lack proxy flags; rebuild must pass existing=[] or they stick."""
    from atlas_lite.iv_history import _merge_proxy_with_real

    scaled = [{"day": "2020-01-01", "iv": 14.0, "proxy": True}]
    merged = _merge_proxy_with_real(scaled, [])
    assert merged[0]["iv"] == 14.0
    assert merged[0]["proxy"] is True


def test_ensure_iv_history_requires_atm_iv_even_if_vix_samples_exist(tmp_path) -> None:
    import asyncio
    from unittest.mock import AsyncMock, patch

    from atlas_lite.iv_history import (
        IVP_HISTORY_FILE,
        ensure_iv_history,
        save_iv_history_meta,
    )

    history = {
        "NSE:NIFTY 50": [{"day": f"2020-01-{i:02d}", "iv": 12.0} for i in range(1, 10)],
    }
    (tmp_path / IVP_HISTORY_FILE).write_text(json.dumps(history), encoding="utf-8")
    save_iv_history_meta(tmp_path, {"version": 2, "source": "vix"})

    async def _run() -> None:
        with patch(
            "atlas_lite.iv_history.fetch_nifty_atm_iv_series",
            new=AsyncMock(return_value=[]),
        ):
            await ensure_iv_history(
                rest=None,  # type: ignore[arg-type]
                csvs=[],
                data_dir=tmp_path,
                atm_iv=None,
                vix_live=11.0,
            )

    try:
        asyncio.get_event_loop().run_until_complete(_run())
        raised = False
    except RuntimeError as exc:
        raised = "ATM IV" in str(exc)
    assert raised


def test_parse_nifty_atm_iv_from_udiff_csv() -> None:
    from datetime import date

    from atlas_lite.nse_fo_bhav import parse_nifty_atm_iv

    csv_text = """TckrSymb,FinInstrmTp,OptnTp,XpryDt,StrkPric,LastPric,ClsPric,SttlmPric,UndrlygPric
NIFTY,IDO,CE,2026-09-08,23850.00,128.9,128.9,128.9,23873.45
NIFTY,IDO,PE,2026-09-08,23850.00,89.7,89.7,89.7,23873.45
NIFTY,IDO,CE,2026-09-08,23900.00,100,100,100,23873.45
NIFTY,IDO,PE,2026-09-08,23900.00,110,110,110,23873.45
"""
    iv = parse_nifty_atm_iv(csv_text, date(2026, 9, 3))
    assert iv is not None
    assert 8.0 < iv < 20.0


def test_rebuild_prefers_bhav_over_vix_proxy(tmp_path) -> None:
    import asyncio
    from unittest.mock import AsyncMock, patch

    from atlas_lite.iv_history import IVP_HISTORY_FILE, rebuild_atm_iv_history

    async def _run() -> int:
        with patch(
            "atlas_lite.iv_history.fetch_nifty_atm_iv_series",
            new=AsyncMock(
                return_value=[
                    {"day": "2026-09-01", "iv": 10.7},
                    {"day": "2026-09-02", "iv": 10.8},
                    {"day": "2026-09-03", "iv": 11.1},
                ]
            ),
        ), patch(
            "atlas_lite.iv_history._fetch_vix_daily_series",
            new=AsyncMock(
                return_value=[
                    {"day": "2026-09-01", "iv": 11.0},
                    {"day": "2026-09-02", "iv": 11.2},
                    {"day": "2026-09-03", "iv": 11.0},
                    {"day": "2026-09-04", "iv": 10.8},
                ]
            ),
        ):
            return await rebuild_atm_iv_history(
                rest=None,  # type: ignore[arg-type]
                csvs=[],
                days=252,
                data_dir=tmp_path,
                atm_iv=12.2,
                vix_live=10.8,
            )

    n = asyncio.get_event_loop().run_until_complete(_run())
    history = json.loads((tmp_path / IVP_HISTORY_FILE).read_text(encoding="utf-8"))
    by_day = {row["day"]: row for row in history["NSE:NIFTY 50"]}
    assert n >= 3
    assert by_day["2026-09-03"]["iv"] == 11.1
    assert not by_day["2026-09-03"].get("proxy")
    assert by_day["2026-09-04"].get("proxy") is True


def test_black76_atm_iv_ce_pe_agree_on_synthetic_forward() -> None:
    """Put-call parity forward makes CE/PE Black-76 IVs agree (Kite-style)."""
    from atlas_lite.metrics import implied_volatility, synthetic_forward

    spot = 23986.0
    strike = 24000
    ce, pe = 101.0, 92.7
    forward = synthetic_forward(spot, strike, ce, pe)
    assert abs(forward - (strike + ce - pe)) < 1e-9
    tte = 2.0 / 252.0
    ce_iv = implied_volatility(ce, forward, float(strike), tte, call=True)
    pe_iv = implied_volatility(pe, forward, float(strike), tte, call=False)
    assert ce_iv is not None and pe_iv is not None
    assert abs(ce_iv - pe_iv) < 0.05


def test_atm_iv_from_ltp_uses_black76() -> None:
    from datetime import date
    from unittest.mock import patch

    from atlas_lite.metrics import atm_iv_from_ltp

    ist = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 9, 3, 11, 30, tzinfo=ist)
    expiry = date(2026, 9, 8)
    with patch("atlas_lite.metrics.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.combine = datetime.combine
        iv = atm_iv_from_ltp(
            {"last_price": 145.0},
            {"last_price": 105.0},
            24750.0,
            24750,
            expiry,
        )
    assert iv is not None
    # Short weekly with weekend → 252d TTE yields higher IV than old calendar BS.
    assert iv > 11.0


def test_ivp_v3_meta_needs_rebuild_for_v4(tmp_path) -> None:
    from atlas_lite.iv_history import (
        IVP_HISTORY_FILE,
        needs_iv_history_rebuild,
        save_iv_history_meta,
    )

    history = {
        "NSE:NIFTY 50": [
            {"day": f"2020-01-{i:02d}", "iv": 12.0, "proxy": True} for i in range(1, 10)
        ],
    }
    (tmp_path / IVP_HISTORY_FILE).write_text(json.dumps(history), encoding="utf-8")
    save_iv_history_meta(tmp_path, {"version": 3, "source": "atm_greeks", "bootstrap_scale": 0.88})
    assert needs_iv_history_rebuild(tmp_path) is True


def test_full_mode_tick_parses_oi_day_high() -> None:
    import struct

    from atlas_lite.kite_ws import parse_binary_ticks
    from atlas_lite.metrics import oi_pct_of_day_high, quote_oi_day_high, update_oi_day_high

    packet = bytearray(184)
    token = 256265
    struct.pack_into(">i", packet, 0, token)
    struct.pack_into(">i", packet, 4, 2400000)
    for off in (28, 32, 36, 40):
        struct.pack_into(">i", packet, off, 2400000)
    struct.pack_into(">I", packet, 48, 10_000_000)
    struct.pack_into(">I", packet, 52, 10_200_000)
    struct.pack_into(">I", packet, 56, 9_800_000)
    payload = struct.pack(">HH", 1, 184) + bytes(packet)
    ticks = parse_binary_ticks(payload)
    assert len(ticks) == 1
    row = ticks[0]
    assert row["oi"] == 10_000_000
    assert row["oi_day_high"] == 10_200_000
    assert row["oi_day_low"] == 9_800_000
    assert quote_oi_day_high(row) == 10_200_000
    high = update_oi_day_high(10_000_000, 10_200_000, None)
    assert high == 10_200_000
    assert oi_pct_of_day_high(10_000_000, high) == 98.04
    assert update_oi_day_high(10_300_000, 10_200_000, 10_200_000) == 10_300_000
    assert update_oi_day_high(10_000_000, 0, None) == 10_000_000
    assert update_oi_day_high(0, 0, None) is None

    quote_pkt = bytearray(44)
    struct.pack_into(">i", quote_pkt, 0, token)
    quote_payload = struct.pack(">HH", 1, 44) + bytes(quote_pkt)
    quote_ticks = parse_binary_ticks(quote_payload)
    assert "oi_day_high" not in quote_ticks[0]
