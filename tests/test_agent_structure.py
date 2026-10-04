"""1m structure / trap cues for the paper agent."""

from __future__ import annotations

from atlas_lite.agent_scorecard import build_strategy_scorecard
from atlas_lite.agent_structure import build_structure_expert


def _bar(t: str, o: float, h: float, l: float, c: float) -> dict:
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


def test_bullish_pin_and_bias() -> None:
    bars = [
        _bar("2026-10-01 09:20", 100, 101, 99.5, 100.5),
        _bar("2026-10-01 09:21", 100.5, 101, 100, 100.2),
        # Long lower wick, close near high.
        _bar("2026-10-01 09:22", 100.0, 100.4, 98.0, 100.3),
    ]
    out = build_structure_expert(bars)
    assert out["ok"] is True
    assert out["pin"] == "bullish_rejection"
    assert out["bias"] in ("bullish_structure", "neutral")


def test_bear_trap_sweep_lows() -> None:
    # Prior lows around 100; last bar sweeps to 98.5 then closes back above 100.
    bars = [
        _bar(f"2026-10-01 09:{20+i:02d}", 100.5, 101.0, 100.0, 100.4) for i in range(8)
    ]
    bars.append(_bar("2026-10-01 09:28", 100.2, 100.6, 98.5, 100.3))
    out = build_structure_expert(bars)
    assert out["ok"] is True
    # Failed low sweep — generic bear_trap or equal-low liquidity grab.
    assert out["trap"] in ("bear_trap", "eql_liquidity_grab")
    assert out["bias"] == "bullish_structure"

    card = build_strategy_scorecard(
        {
            "spot_chg_open_pct": 0.0,
            "adx": 16,
            "adx_regime": "range",
            "structure": out,
        }
    )
    long_ce = next(c for c in card["candidates"] if c["side"] == "ce" and c["style"] == "long")
    long_pe = next(c for c in card["candidates"] if c["side"] == "pe" and c["style"] == "long")
    assert long_ce["score"] > long_pe["score"]
    assert any("bear_trap" in r or "eql_liquidity_grab" in r for r in long_ce["reasons"])


def test_bull_trap_penalizes_long_ce() -> None:
    bars = [
        _bar(f"2026-10-01 10:{i:02d}", 100.0, 100.8, 99.8, 100.2) for i in range(8)
    ]
    # Sweep above prior highs (~100.8) then fail.
    bars.append(_bar("2026-10-01 10:08", 100.5, 102.0, 100.0, 100.4))
    out = build_structure_expert(bars)
    assert out["trap"] in ("bull_trap", "eqh_liquidity_grab")
    card = build_strategy_scorecard(
        {
            "spot_chg_open_pct": 0.05,
            "adx": 20,
            "adx_regime": "mixed",
            "structure": out,
        }
    )
    long_ce = next(c for c in card["candidates"] if c["side"] == "ce" and c["style"] == "long")
    assert any("bull_trap" in r or "eqh_liquidity_grab" in r for r in long_ce["reasons"])
    assert long_ce["score"] < 0 or any("-2.0" in r for r in long_ce["reasons"])


def test_insufficient_bars() -> None:
    out = build_structure_expert([_bar("2026-10-01 09:20", 1, 2, 0.5, 1.5)])
    assert out["ok"] is False
    assert out["bias"] == "unknown"


def _bar_v(t: str, o: float, h: float, l: float, c: float, v: float = 1000.0) -> dict:
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def test_demand_order_block_on_1m() -> None:
    bars = [
        _bar_v(f"2026-10-01 10:{i:02d}", 100.0, 100.4, 99.8, 100.1, 1000) for i in range(12)
    ]
    # Bearish OB candle then impulsive bullish continuation.
    bars.append(_bar_v("2026-10-01 10:12", 100.1, 100.2, 98.5, 98.8, 2000))
    bars.append(_bar_v("2026-10-01 10:13", 98.8, 103.0, 98.7, 102.6, 9000))
    for i in range(4):
        bars.append(_bar_v(f"2026-10-01 10:{14+i:02d}", 102.5, 103.0, 102.2, 102.7, 1500))
    out = build_structure_expert(bars, spot=102.7)
    assert out["ok"] is True
    obs = out.get("order_blocks") or []
    assert any(o.get("side") == "demand" for o in obs)
    demand = next(o for o in obs if o.get("side") == "demand")
    assert demand["low"] <= 98.8


def test_eqh_grab_requires_min_overshoot() -> None:
    # Two swing highs near 101, then a shallow poke (no ≥0.5 overshoot) must not grab.
    bars = [
        _bar("2026-10-01 11:00", 100.0, 100.2, 99.8, 100.0),
        _bar("2026-10-01 11:01", 100.0, 101.0, 99.9, 100.5),  # swing high ~101
        _bar("2026-10-01 11:02", 100.5, 100.6, 100.0, 100.2),
        _bar("2026-10-01 11:03", 100.2, 100.4, 99.9, 100.1),
        _bar("2026-10-01 11:04", 100.1, 101.0, 100.0, 100.4),  # equal swing high
        _bar("2026-10-01 11:05", 100.4, 100.5, 100.0, 100.2),
        _bar("2026-10-01 11:06", 100.2, 100.4, 100.0, 100.1),
        _bar("2026-10-01 11:07", 100.1, 101.2, 99.8, 100.0),  # +0.2 overshoot only
    ]
    out = build_structure_expert(bars)
    assert out.get("trap") != "eqh_liquidity_grab"


def test_equal_pools_use_swings_not_every_bar() -> None:
    bars = []
    # Flat tape should NOT invent a 10-touch equal-low pool from every bar low.
    for i in range(20):
        bars.append(_bar_v(f"2026-10-01 11:{i:02d}", 100.0, 100.3, 99.8, 100.1, 1000))
    out = build_structure_expert(bars)
    pools = out.get("liquidity_pools") or {}
    for row in (pools.get("equal_lows") or []) + (pools.get("equal_highs") or []):
        assert int(row.get("touches") or 0) <= 6


def test_early_session_does_not_use_yesterday() -> None:
    bars = [
        _bar("2026-09-30 15:20", 100, 101, 99, 100.5),
        _bar("2026-09-30 15:21", 100.5, 101, 100, 100.2),
        _bar("2026-10-01 09:15", 100, 100.4, 99.8, 100.1),
        _bar("2026-10-01 09:16", 100.1, 100.3, 100.0, 100.2),
    ]
    out = build_structure_expert(bars)
    assert out["ok"] is False
    assert out["reason"] == "need_≥3_bars_today"


def test_vp_trap_suppressed_without_real_volume() -> None:
    # No v field → volume_known false → no vp trap promotion.
    bars = [
        _bar(f"2026-10-01 12:{i:02d}", 100, 100.5, 99.5, 100.2) for i in range(16)
    ]
    # Last bar wicked through mid and closed lower (would be VP reject if vol known).
    bars[-1] = _bar("2026-10-01 12:15", 100.2, 101.0, 99.0, 99.2)
    out = build_structure_expert(bars)
    vp = out.get("volume_profile") or {}
    assert vp.get("ok") is True
    assert vp.get("volume_known") is False
    assert vp.get("trap") in (None, )
    assert out.get("trap") not in (
        "vp_reject_from_hvn_bearish",
        "vp_reject_from_hvn_bullish",
    )


def test_volume_profile_ok_with_volume() -> None:
    bars = [
        _bar_v(f"2026-10-01 12:{i:02d}", 100 + (i % 3) * 0.2, 100.5 + (i % 3) * 0.2, 99.8, 100.2, 1000 + i * 50)
        for i in range(16)
    ]
    out = build_structure_expert(bars)
    vp = out.get("volume_profile") or {}
    assert vp.get("ok") is True
    assert vp.get("hvn") is not None
    assert vp.get("volume_known") is True
    assert (out.get("intent_proxy") or {}).get("note") == "proxy_only_not_true_institutional_intent"
