"""Kite NSE options charges used in paper P&L."""

from atlas_lite.kite_charges import kite_nfo_charges, kite_nfo_order_charges


def test_stt_rounds_up_like_zerodha_example() -> None:
    # Zerodha: 50 qty * ₹60 = ₹3,000 premium; 0.15% = ₹4.50 → ₹5.
    sell = kite_nfo_order_charges(60.0, 50, "sell")
    assert sell["stt"] == 5.0
    assert sell["stamp"] == 0.0
    buy = kite_nfo_order_charges(60.0, 50, "buy")
    assert buy["stt"] == 0.0
    assert buy["brokerage"] == 20.0


def test_one_lot_nifty_straddle_round_trip_has_four_orders() -> None:
    qty = 65
    opened = kite_nfo_charges([(100.0, qty, "buy"), (95.0, qty, "buy")])
    closed = kite_nfo_charges([(108.0, qty, "sell"), (95.0, qty, "sell")])
    assert opened["orders"] == 2
    assert closed["orders"] == 2
    assert opened["brokerage"] == 40.0
    assert closed["brokerage"] == 40.0
    assert closed["stt"] > 0
    assert opened["stt"] == 0.0
    total = round(opened["total"] + closed["total"], 2)
    assert total > 80.0


def test_iron_fly_is_eight_orders() -> None:
    qty = 65
    opened = kite_nfo_charges(
        [
            (100.0, qty, "sell"),
            (95.0, qty, "sell"),
            (20.0, qty, "buy"),
            (18.0, qty, "buy"),
        ]
    )
    assert opened["orders"] == 4
    assert opened["brokerage"] == 80.0
    assert opened["stt"] > 0
    assert opened["total"] > 80.0
