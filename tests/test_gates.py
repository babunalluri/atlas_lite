"""Entry gate evaluation tests — notebook 17/8/26."""

from __future__ import annotations

from atlas_lite.metrics import evaluate_rule, evaluate_sheet
from atlas_lite.specs import ALL_SPECS


def _spec(spec_id: str) -> dict:
    for spec in ALL_SPECS:
        if spec["id"] == spec_id:
            return spec
    raise KeyError(spec_id)


def _ready_feed(**kwargs) -> dict:
    feed = {
        "adx": 20.0,
        "vix_chg": 0.1,
        "pcr": 1.1,
        "ivp": 40.0,
        "iv": 10.0,
        "iv_day_high": 12.0,
        "iv_day_low": 10.0,
        "oi_vs_day_high": 100.0,
        "fut_oi": 10_000_000.0,
        "fut_oi_day_high": 10_000_000.0,
        "ce": 100.0,
        "pe": 95.0,
        "nifty_ltp": 23800.0,
        "index_nifty_chg": 0.1,
        "index_banknifty_chg": 0.1,
        "index_niftyit_chg": 0.1,
        "index_sensex_chg": 0.1,
    }
    feed.update(kwargs)
    return feed


def test_vix_chg_gate_within_threshold() -> None:
    spec = _spec("vix_chg")
    assert evaluate_rule(spec, 0.1, {}) is True
    assert evaluate_rule(spec, 2.99, {}) is True
    assert evaluate_rule(spec, -2.99, {}) is True


def test_vix_chg_gate_outside_threshold() -> None:
    spec = _spec("vix_chg")
    assert evaluate_rule(spec, 3.0, {}) is False
    assert evaluate_rule(spec, -3.01, {}) is False


def test_adx_gate_requires_below_25() -> None:
    spec = _spec("adx")
    assert evaluate_rule(spec, 25.0, {}) is False
    assert evaluate_rule(spec, 24.99, {}) is True
    assert evaluate_rule(spec, 23.91, {}) is True
    assert evaluate_rule(spec, 26.0, {}) is False


def test_iv_near_day_low_gate() -> None:
    spec = _spec("iv_day_high")
    assert evaluate_rule(spec, 10.0, {"iv": 10.0, "iv_day_low": 10.0}) is True
    assert evaluate_rule(spec, 10.05, {"iv": 10.05, "iv_day_low": 10.0}) is True
    assert evaluate_rule(spec, 10.06, {"iv": 10.06, "iv_day_low": 10.0}) is False
    assert evaluate_rule(spec, 12.0, {"iv": 12.0, "iv_day_low": 10.0}) is False


def test_iv_near_day_low_missing_data() -> None:
    spec = _spec("iv_day_high")
    assert evaluate_rule(spec, 10.0, {"iv": 10.0}) is None


def test_ce_pe_balanced_gate() -> None:
    spec = _spec("ce")
    assert evaluate_rule(spec, 100.0, {"ce": 100.0, "pe": 95.0}) is True
    assert evaluate_rule(spec, 100.0, {"ce": 100.0, "pe": 50.0}) is False


def test_index_pct_does_not_gate() -> None:
    for spec_id in ("nifty_chg", "banknifty_chg", "niftyit_chg", "sensex_chg"):
        spec = _spec(spec_id)
        assert spec["gates_entry"] is False


def test_max_pain_does_not_gate() -> None:
    spec = _spec("max_pain")
    assert spec["gates_entry"] is False
    assert evaluate_rule(spec, 24000, {"nifty_ltp": 23900, "max_pain": 24000}) is None


def test_evaluate_sheet_all_notebook_gates_ready() -> None:
    result = evaluate_sheet(_ready_feed())
    assert result["gates_total"] == 7
    assert result["entry_ready"] is True
    assert result["failing_gates"] == []
    assert result["missing_gates"] == []


def test_index_move_does_not_block_bell() -> None:
    result = evaluate_sheet(
        _ready_feed(
            index_nifty_chg=1.2,
            index_banknifty_chg=-0.8,
            index_niftyit_chg=-2.0,
            index_sensex_chg=0.9,
        )
    )
    assert result["entry_ready"] is True
    assert result["failing_gates"] == []


def test_evaluate_sheet_counts_failing_gates() -> None:
    result = evaluate_sheet(
        _ready_feed(adx=30.0, pcr=0.72, iv=10.06, iv_day_low=10.0, iv_day_high=10.06)
    )
    assert "ADX" in result["failing_gates"]
    assert "PCR" in result["failing_gates"]
    assert "IV vs today low" in result["failing_gates"]
    assert result["entry_ready"] is False


def test_oi_near_day_high_gate() -> None:
    spec = _spec("oi_day_high")
    assert evaluate_rule(spec, 100.0, {}) is True
    assert evaluate_rule(spec, 99.5, {}) is True
    assert evaluate_rule(spec, 99.4, {}) is False
    result = evaluate_sheet(_ready_feed(oi_vs_day_high=97.0))
    assert "OI vs today high" in result["failing_gates"]
    assert result["entry_ready"] is False
