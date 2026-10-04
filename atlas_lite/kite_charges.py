"""Zerodha Kite charges for paper P&L (NSE F&O options + equity intraday).

Does not place orders. Rates from https://zerodha.com/charges/ (options).
STT 0.15% on sell premium is the 1 Apr 2026 rate.
"""

from __future__ import annotations

import math
from typing import Sequence

KITE_OPTIONS_BROKERAGE = 20.0
KITE_STT_SELL = 0.0015  # 0.15% of sell premium
KITE_NSE_TXN = 0.0003553  # 0.03553% of premium (NSE, IPFT included)
KITE_SEBI = 10.0 / 10_000_000  # ₹10 / crore
KITE_STAMP_BUY = 0.00003  # 0.003% of buy premium
KITE_GST = 0.18

_CHARGE_KEYS = ("brokerage", "stt", "txn", "sebi", "stamp", "gst", "total")


def rupee_ceil(value: float) -> float:
    if value <= 0:
        return 0.0
    return float(math.ceil(round(value, 8)))


def rupee_nearest(value: float) -> float:
    if value <= 0:
        return 0.0
    return float(math.floor(value + 0.5))


def order_charges(
    turnover: float,
    *,
    side: str,
    brokerage: float,
    txn_rate: float,
    stt_sell_rate: float,
    stamp_buy_rate: float = KITE_STAMP_BUY,
    sebi_rate: float = KITE_SEBI,
    gst_rate: float = KITE_GST,
) -> dict[str, float]:
    """Zerodha charge waterfall for one executed order.

    Single source of the rounding / GST-base rules; options and equity books
    only differ in the rates (and brokerage) they pass in.
    """
    side_l = str(side).strip().lower()
    if side_l not in ("buy", "sell"):
        raise ValueError(f"side must be buy or sell, got {side!r}")
    turnover = max(0.0, float(turnover))
    brokerage = float(brokerage)
    txn = round(turnover * txn_rate, 2)
    sebi = round(turnover * sebi_rate, 2)
    stamp = rupee_nearest(turnover * stamp_buy_rate) if side_l == "buy" else 0.0
    stt = rupee_ceil(turnover * stt_sell_rate) if side_l == "sell" else 0.0
    gst = round(gst_rate * (brokerage + txn + sebi), 2)
    total = round(brokerage + stt + txn + sebi + stamp + gst, 2)
    return {
        "side": side_l,
        "turnover": round(turnover, 2),
        "brokerage": brokerage,
        "stt": stt,
        "txn": txn,
        "sebi": sebi,
        "stamp": stamp,
        "gst": gst,
        "total": total,
    }


def kite_nfo_order_charges(premium_pts: float, qty: int, side: str) -> dict[str, float]:
    """Charges for one executed NSE options order on Kite."""
    return order_charges(
        float(premium_pts) * int(qty),
        side=side,
        brokerage=KITE_OPTIONS_BROKERAGE,
        txn_rate=KITE_NSE_TXN,
        stt_sell_rate=KITE_STT_SELL,
        stamp_buy_rate=KITE_STAMP_BUY,
    )


def kite_nfo_charges(
    legs: Sequence[tuple[float, int, str]],
) -> dict[str, float]:
    """Sum Kite charges across executed option legs. Each leg is (pts, qty, buy|sell)."""
    totals = {key: 0.0 for key in _CHARGE_KEYS}
    turnover = 0.0
    orders = 0
    for pts, qty, side in legs:
        one = kite_nfo_order_charges(pts, qty, side)
        for key in _CHARGE_KEYS:
            totals[key] += one[key]
        turnover += one["turnover"]
        orders += 1
    body = {key: round(totals[key], 2) for key in _CHARGE_KEYS}
    body["turnover"] = round(turnover, 2)
    body["orders"] = orders
    return body
