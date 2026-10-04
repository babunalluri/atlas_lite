"""Unit tests for FO universe parsing (NIFTY NFO + SENSEX BFO)."""

from __future__ import annotations

from datetime import date

from atlas_lite.instruments import (
    full_chain_symbols,
    option_expiry_prefixes,
    parse_fo_csv,
    parse_next_option_universe,
    resolve_atm_legs,
)

NFO_SAMPLE = """instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange
1,1,NIFTY26SEPFUT,NIFTY,0,2026-09-29,0,0.05,65,FUT,NFO-FUT,NFO
2,2,NIFTY2691523450CE,NIFTY,0,2026-09-15,23450,0.05,65,CE,NFO-OPT,NFO
3,3,NIFTY2691523450PE,NIFTY,0,2026-09-15,23450,0.05,65,PE,NFO-OPT,NFO
4,4,NIFTY2691523500CE,NIFTY,0,2026-09-15,23500,0.05,65,CE,NFO-OPT,NFO
5,5,NIFTY2691523500PE,NIFTY,0,2026-09-15,23500,0.05,65,PE,NFO-OPT,NFO
6,6,NIFTY2692223450CE,NIFTY,0,2026-09-22,23450,0.05,65,CE,NFO-OPT,NFO
7,7,NIFTY2692223450PE,NIFTY,0,2026-09-22,23450,0.05,65,PE,NFO-OPT,NFO
"""

BFO_SAMPLE = """instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange
11,11,SENSEX26SEPFUT,SENSEX,0,2026-09-29,0,0.05,20,FUT,BFO-FUT,BFO
12,12,SENSEX2691580000CE,SENSEX,0,2026-09-15,80000,0.05,20,CE,BFO-OPT,BFO
13,13,SENSEX2691580000PE,SENSEX,0,2026-09-15,80000,0.05,20,PE,BFO-OPT,BFO
14,14,SENSEX2691580100CE,SENSEX,0,2026-09-15,80100,0.05,20,CE,BFO-OPT,BFO
15,15,SENSEX2691580100PE,SENSEX,0,2026-09-15,80100,0.05,20,PE,BFO-OPT,BFO
"""


def test_parse_nfo_nifty_universe() -> None:
    uni = parse_fo_csv(
        NFO_SAMPLE,
        name="NIFTY",
        exchange="NFO",
        fut_segment="NFO-FUT",
        strike_step=50,
        today=date(2026, 9, 11),
    )
    assert uni.fut_symbol == "NFO:NIFTY26SEPFUT"
    assert uni.expiry == date(2026, 9, 15)
    assert uni.fut_expiry == date(2026, 9, 29)
    assert uni.strike_step == 50
    legs = resolve_atm_legs(uni, 23460)
    assert legs.strike == 23450
    assert legs.ce_symbol.endswith("23450CE")


def test_parse_bfo_sensex_universe_strike_100() -> None:
    uni = parse_fo_csv(
        BFO_SAMPLE,
        name="SENSEX",
        exchange="BFO",
        fut_segment="BFO-FUT",
        strike_step=100,
        hysteresis_pts=16,
        today=date(2026, 9, 11),
    )
    assert uni.fut_symbol == "BFO:SENSEX26SEPFUT"
    assert uni.exchange == "BFO"
    assert uni.strike_step == 100
    legs = resolve_atm_legs(uni, 80040)
    assert legs.strike == 80000
    assert legs.ce_symbol == "BFO:SENSEX2691580000CE"
    strikes, ce, pe = full_chain_symbols(uni, BFO_SAMPLE)
    assert strikes == [80000, 80100]
    assert len(ce) == 2 and len(pe) == 2


def test_next_week_option_universe() -> None:
    today = date(2026, 9, 11)
    ranked = option_expiry_prefixes(NFO_SAMPLE, today, name="NIFTY")
    assert [exp for exp, _p in ranked] == [date(2026, 9, 15), date(2026, 9, 22)]
    nearest = parse_fo_csv(
        NFO_SAMPLE,
        name="NIFTY",
        exchange="NFO",
        fut_segment="NFO-FUT",
        today=today,
    )
    nxt = parse_next_option_universe(NFO_SAMPLE, nearest, today=today)
    assert nxt is not None
    assert nxt.expiry == date(2026, 9, 22)
    assert nxt.option_symbol(23450, "CE").endswith("23450CE")
    assert "26922" in nxt.option_symbol(23450, "CE")
