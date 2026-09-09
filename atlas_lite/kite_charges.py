"""Zerodha Kite NSE F&O options charges for paper P&L.

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


def _rupee_ceil(value: float) -> float:
    if value <= 0:
        return 0.0
    return float(math.ceil(round(value, 8)))


def _rupee_nearest(value: float) -> float:
    if value <= 0:
        return 0.0
    return float(math.floor(value + 0.5))


def kite_nfo_order_charges(premium_pts: float, qty: int, side: str) -> dict[str, float]:
    """Charges for one executed NSE options order on Kite."""
    side_l = str(side).strip().lower()
    if side_l not in ("buy", "sell"):
        raise ValueError(f"side must be buy or sell, got {side!r}")
    turnover = max(0.0, float(premium_pts) * int(qty))
    brokerage = KITE_OPTIONS_BROKERAGE
    txn = round(turnover * KITE_NSE_TXN, 2)
    sebi = round(turnover * KITE_SEBI, 2)
    stamp = _rupee_nearest(turnover * KITE_STAMP_BUY) if side_l == "buy" else 0.0
    stt = _rupee_ceil(turnover * KITE_STT_SELL) if side_l == "sell" else 0.0
    gst = round(KITE_GST * (brokerage + txn + sebi), 2)
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
