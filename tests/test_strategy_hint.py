"""Single notebook-entry bell tests."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from atlas_lite.strategy_hint import ce_pe_parity, suggest_strategy

IST = ZoneInfo("Asia/Kolkata")


def _feed(**kwargs):
    base = {
        "ce": 100.0,
        "pe": 95.0,
        "atr": 8.0,
        "pcr": 1.1,
        "adx": 20.0,
        "iv": 12.0,
        "iv_day_high": 13.0,
        "iv_day_low": 12.0,
        "ivp": 40.0,
        "vix_chg": 0.2,
        "oi_vs_day_high": 100.0,
        "index_nifty_chg": 0.1,
        "index_banknifty_chg": 0.1,
        "index_niftyit_chg": 0.1,
        "index_sensex_chg": 0.1,
        "nifty_ltp": 23800.0,
    }
    base.update(kwargs)
    return base


def test_parity_tight_near_away():
    assert ce_pe_parity(100.0, 100.0)[0] == "tight"
    assert ce_pe_parity(100.0, 90.0)[0] == "tight"
    assert ce_pe_parity(100.0, 80.0)[0] == "near"
    assert ce_pe_parity(100.0, 50.0)[0] == "away"


def test_enter_when_all_notebook_gates_pass():
    now = datetime(2026, 9, 4, 10, 30, tzinfo=IST)
    hint = suggest_strategy(_feed(), now=now)
    assert hint["action"] == "ENTER"
    assert hint["bell"] == "enter"
    assert hint["strategy"] == "NOTEBOOK_ENTRY"


def test_sit_when_adx_not_below_25():
    now = datetime(2026, 9, 4, 10, 30, tzinfo=IST)
    hint = suggest_strategy(_feed(adx=26.0), now=now)
    assert hint["action"] == "SIT_OUT"
    assert hint["bell"] == "idle"
    assert "ADX" in (hint.get("failing_gates") or [])


def test_sit_when_ce_pe_away():
    now = datetime(2026, 9, 4, 10, 30, tzinfo=IST)
    hint = suggest_strategy(_feed(ce=100.0, pe=50.0), now=now)
    assert hint["action"] == "SIT_OUT"


def test_weekend_sit():
    now = datetime(2026, 9, 5, 10, 30, tzinfo=IST)
    hint = suggest_strategy(_feed(), now=now)
    assert hint["action"] == "SIT_OUT"


def test_index_move_does_not_block_enter():
    now = datetime(2026, 9, 4, 10, 30, tzinfo=IST)
    hint = suggest_strategy(
        _feed(index_nifty_chg=1.5, index_niftyit_chg=-2.0, index_sensex_chg=0.9),
        now=now,
    )
    assert hint["action"] == "ENTER"
    assert hint["bell"] == "enter"


def test_sit_when_iv_off_day_low():
    now = datetime(2026, 9, 4, 10, 30, tzinfo=IST)
    hint = suggest_strategy(_feed(iv=12.0, iv_day_low=10.0), now=now)
    assert hint["action"] == "SIT_OUT"
    assert "IV vs today low" in (hint.get("failing_gates") or [])


def test_sit_when_oi_off_day_high():
    now = datetime(2026, 9, 4, 10, 30, tzinfo=IST)
    hint = suggest_strategy(_feed(oi_vs_day_high=97.0), now=now)
    assert hint["action"] == "SIT_OUT"
    assert "OI vs today high" in (hint.get("failing_gates") or [])


def test_sit_when_pcr_outside_band():
    now = datetime(2026, 9, 4, 10, 30, tzinfo=IST)
    hint = suggest_strategy(_feed(pcr=0.8), now=now)
    assert hint["action"] == "SIT_OUT"
    assert "PCR" in (hint.get("failing_gates") or [])
