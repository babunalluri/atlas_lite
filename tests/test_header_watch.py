"""Notebook header strip — extra quotes, display only (not 7/7 AND)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from atlas_lite.feed_engine import FeedEngine
from atlas_lite.instruments import nearest_fut_symbol, resolve_header_watch
from atlas_lite.kite_rest import KiteRest
from atlas_lite.metrics import evaluate_sheet
from atlas_lite.specs import ALL_SPECS, HEADER_WATCHLIST


_FUT_CSV = """instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange
1,1,NIFTY26SEPFUT,NIFTY,0,2026-09-29,0,0.1,65,FUT,NFO-FUT,NFO
2,2,NIFTY26OCTFUT,NIFTY,0,2026-10-27,0,0.1,65,FUT,NFO-FUT,NFO
3,3,BANKNIFTY26SEPFUT,BANKNIFTY,0,2026-09-29,0,0.2,30,FUT,NFO-FUT,NFO
4,4,SENSEX26SEPFUT,SENSEX,0,2026-09-28,0,1,10,FUT,BFO-FUT,BFO
5,5,BANKEX26SEPFUT,BANKEX,0,2026-09-28,0,1,15,FUT,BFO-FUT,BFO
6,6,BANKEX26OCTFUT,BANKEX,0,2026-10-26,0,1,15,FUT,BFO-FUT,BFO
"""


def test_watchlist_order_and_missing_names() -> None:
    labels = [item["label"] for item in HEADER_WATCHLIST]
    assert labels == [
        "NIFTY 50",
        "SENSEX",
        "BANKNIFTY",
        "BANKEX",
        "NIFTY IT",
        "NIFTY FUT",
        "SENSEX FUT",
        "BANKNIFTY FUT",
        "BANKEX FUT",
        "RELIANCE",
        "HDFC BANK",
        "ICICI BANK",
        "TCS",
        "INFOSYS",
        "SBI",
        "AIRTEL",
        "L&T",
        "M&M",
        "BAJAJ FINANCE",
        "SUNPHARMA",
    ]
    assert HEADER_WATCHLIST[3]["symbol"] == "BSE:BANKEX"
    assert HEADER_WATCHLIST[9]["symbol"] == "NSE:RELIANCE"
    assert HEADER_WATCHLIST[16]["symbol"] == "NSE:LT"


def test_watchlist_is_not_an_entry_gate() -> None:
    extra_ids = {"bankex", "nifty_fut", "reliance"}
    spec_ids = {spec["id"] for spec in ALL_SPECS}
    assert extra_ids.isdisjoint(spec_ids)
    gating = [spec for spec in ALL_SPECS if spec.get("gates_entry")]
    assert len(gating) == 7
    result = evaluate_sheet(
        {
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
            "index_nifty_chg": 9.0,
        }
    )
    assert result["gates_total"] == 7
    assert result["entry_ready"] is True


def test_nearest_fut_picks_front_month() -> None:
    today = date(2026, 9, 8)
    assert nearest_fut_symbol(_FUT_CSV, "NIFTY", today=today) == "NFO:NIFTY26SEPFUT"
    assert nearest_fut_symbol(_FUT_CSV, "BANKNIFTY", today=today) == "NFO:BANKNIFTY26SEPFUT"
    assert nearest_fut_symbol(_FUT_CSV, "SENSEX", today=today) == "BFO:SENSEX26SEPFUT"
    assert nearest_fut_symbol(_FUT_CSV, "BANKEX", today=today) == "BFO:BANKEX26SEPFUT"
    assert nearest_fut_symbol(_FUT_CSV, "BANKEX", today=date(2026, 9, 30)) == "BFO:BANKEX26OCTFUT"
    assert nearest_fut_symbol(_FUT_CSV, "SENSEX", today=date(2026, 9, 30)) is None
    assert nearest_fut_symbol("", "NIFTY", today=today) is None


def test_resolve_header_watch_fills_fut_symbols() -> None:
    resolved = resolve_header_watch(HEADER_WATCHLIST, [_FUT_CSV], today=date(2026, 9, 8))
    by_id = {item["id"]: item for item in resolved}
    assert by_id["nifty_fut"]["symbol"] == "NFO:NIFTY26SEPFUT"
    assert by_id["banknifty_fut"]["symbol"] == "NFO:BANKNIFTY26SEPFUT"
    assert by_id["sensex_fut"]["symbol"] == "BFO:SENSEX26SEPFUT"
    assert by_id["bankex_fut"]["symbol"] == "BFO:BANKEX26SEPFUT"
    assert by_id["nifty"]["symbol"] == "NSE:NIFTY 50"


def test_build_indices_includes_unresolved_fut_card() -> None:
    eng = FeedEngine(rest=MagicMock(spec=KiteRest), data_dir=Path("data"))
    eng._header_watch = resolve_header_watch(HEADER_WATCHLIST, [_FUT_CSV], today=date(2026, 9, 8))
    eng.book.rows["NSE:NIFTY 50"] = {
        "last_price": 23650.25,
        "ohlc": {"close": 23600.0},
    }
    eng.book.rows["NSE:RELIANCE"] = {
        "last_price": 1400.5,
        "ohlc": {"close": 1390.0},
    }
    cards = eng._build_indices()
    assert len(cards) == 20
    nifty = next(c for c in cards if c["id"] == "nifty")
    assert nifty["group"] == "index"
    assert nifty["ltp"] == 23650.25
    rel = next(c for c in cards if c["id"] == "reliance")
    assert rel["group"] == "stock"
    assert rel["ltp"] == 1400.5
    fut = next(c for c in cards if c["id"] == "nifty_fut")
    assert fut["group"] == "fut"
    assert fut["symbol"] == "NFO:NIFTY26SEPFUT"
    assert "ltp" not in fut
