"""Option chain row builder."""

from datetime import datetime
from zoneinfo import ZoneInfo

from atlas_lite.instruments import sticky_atm_strike
from atlas_lite.metrics import (
    chain_accumulated_totals,
    option_chain_rows,
    synthetic_forward,
    atm_ref_price,
)

IST = ZoneInfo("Asia/Kolkata")


def test_option_chain_rows_wings_filter():
    strikes = [24000, 24050, 24100, 24150, 24200]
    ce_rows = [{"last_price": 100 + i} for i in range(5)]
    pe_rows = [{"last_price": 90 + i} for i in range(5)]
    rows = option_chain_rows(
        strikes,
        ce_rows,
        pe_rows,
        atm_strike=24100,
        wing_strikes=1,
    )
    assert [r["strike"] for r in rows] == [24050, 24100, 24150]
    assert rows[1]["is_atm"] is True
    assert rows[1]["ce"]["ltp"] == 102.0


def test_option_chain_rows_full_when_wings_none():
    strikes = [24000, 24050]
    rows = option_chain_rows(strikes, [{}, {}], [{}, {}], atm_strike=24000, wing_strikes=None)
    assert len(rows) == 2


def test_chain_accumulated_totals():
    rows = [
        {
            "strike": 24000,
            "ce": {"oi": 1000, "ltp": 100.0, "chg_pct": 10.0},
            "pe": {"oi": 2000, "ltp": 50.0, "chg_pct": -5.0},
        },
        {
            "strike": 24050,
            "ce": {"oi": 500, "ltp": 80.0, "chg_pct": 0.0},
            "pe": {"oi": 300, "ltp": 60.0, "chg_pct": 5.0},
        },
    ]
    totals = chain_accumulated_totals(rows)
    assert totals["ce"]["oi"] == 1500
    assert totals["ce"]["ltp"] == 180.0
    assert totals["pe"]["oi"] == 2300
    assert totals["pe"]["ltp"] == 110.0
    assert totals["ce"]["chg_pct"] is not None
    assert totals["pe"]["chg_pct"] is not None


def test_chain_accumulated_totals_skips_zero_ltp_minus_100() -> None:
    rows = [
        {
            "strike": 24600,
            "ce": {"oi": 100, "ltp": 0.0, "chg_pct": -100.0},
            "pe": {"oi": 200, "ltp": 50.0, "chg_pct": 5.0},
        },
    ]
    totals = chain_accumulated_totals(rows)
    assert totals["ce"]["oi"] == 100
    assert totals["ce"]["ltp"] == 0.0
    assert totals["pe"]["ltp"] == 50.0


def test_sticky_atm_holds_through_50pt_mid() -> None:
    assert sticky_atm_strike(24026.0, None) == 24050
    assert sticky_atm_strike(24026.0, 24000) == 24000
    assert sticky_atm_strike(24032.9, 24000) == 24000
    assert sticky_atm_strike(24033.0, 24000) == 24050
    assert sticky_atm_strike(23967.1, 24000) == 24000
    assert sticky_atm_strike(23967.0, 24000) == 23950


def test_synthetic_forward_keeps_atm_when_ce_pe_balanced() -> None:
    spot = 24030.0
    strike = 24000.0
    forward = synthetic_forward(spot, strike, 100.0, 100.0)
    assert forward == 24000.0
    assert sticky_atm_strike(forward, 24000) == 24000
    rich_call = synthetic_forward(spot, strike, 150.0, 50.0)
    assert sticky_atm_strike(rich_call, 24000) == 24100


def test_atm_ref_uses_forward_when_legs_are_liquid() -> None:
    midday = datetime(2026, 9, 8, 12, 0, tzinfo=IST)
    spot = 23654.6
    # Balanced around 23650 — forward stays near cash.
    ref = atm_ref_price(spot, 23650, 80.0, 76.0, now=midday)
    assert abs(ref - (23650 + 80.0 - 76.0)) < 0.01
    assert sticky_atm_strike(ref, 23650) == 23650


def test_atm_ref_pins_to_spot_after_1510() -> None:
    """Sep 8 close: cash frozen, thin options must not hunt ATM."""
    close = datetime(2026, 9, 8, 15, 20, 43, tzinfo=IST)
    spot = 23640.05
    # Tape: CE 26.55 / PE 11.1 would imply a forward far below cash.
    ref = atm_ref_price(spot, 23600, 26.55, 11.1, now=close)
    assert ref == spot
    assert sticky_atm_strike(ref, 23650) == 23650


def test_atm_ref_rejects_thin_premiums_before_1510() -> None:
    midday = datetime(2026, 9, 8, 14, 0, tzinfo=IST)
    spot = 23640.05
    ref = atm_ref_price(spot, 23650, 8.0, 40.0, now=midday)
    assert ref == spot


def test_atm_ref_allows_typical_nifty_basis() -> None:
    """Tape basis is ~+28 to +54 pts. A 25-pt cap used to reject these."""
    midday = datetime(2026, 9, 8, 12, 0, tzinfo=IST)
    spot = 23640.05
    # F = 23650 + 80 - 52 = 23678 (~+38 pts, tape p50). Holds 23650 via hysteresis.
    ref = atm_ref_price(spot, 23650, 80.0, 52.0, now=midday)
    assert abs(ref - (23650 + 80.0 - 52.0)) < 0.01
    assert sticky_atm_strike(ref, 23650) == 23650


def test_atm_ref_rejects_forward_far_from_spot() -> None:
    midday = datetime(2026, 9, 8, 14, 0, tzinfo=IST)
    spot = 23640.05
    # F = 23650 + 280 - 20 = 23910, ~270 pts above cash — not a real basis.
    ref = atm_ref_price(spot, 23650, 280.0, 20.0, now=midday)
    assert ref == spot
    assert sticky_atm_strike(ref, 23650) == 23650
