"""COMBO confluence votes — same math as static/index.html calcConfluenceRows.

Paper / scan only. Do not change the chart overlay to “fix” this module.

≥4 of session VWAP, Bollinger(20,2), RSI(14) 60/40, MACD(12,26,9), ADX/DMI(14)
agree. ADX abstains below 18.

The overlay (and this copy) **resets `side` when votes drop below NEED**. The
next ≥4 print is a new `signal`, even if the letter matches. That is not a
strict B↔S latch. Paper uses it: flatten on `side is None`, re-enter on the
reprint; exit also on opposite letter, +8/−6, 12m, or 15:14.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from atlas_lite.metrics import wilder_dmi_series

PULLBACK_FRAC = 0.5
MIN_SIGMA = 1.0
MIN_BARS = 5
BOLL_N = 20
BOLL_MULT = 2.0
ADX_MIN = 18.0
RSI_BULL = 60.0
RSI_BEAR = 40.0
NEED = 4
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

Side = Literal["B", "S"]


def ema_series(values: list[float | None], period: int) -> list[float | None]:
    """Match chart calcEmaSeries: skip non-finite, emit after `period` samples."""
    out: list[float | None] = [None] * len(values)
    k = 2.0 / (period + 1)
    ema: float | None = None
    seen = 0
    for i, raw in enumerate(values):
        if raw is None or not math.isfinite(float(raw)):
            continue
        v = float(raw)
        seen += 1
        ema = v if ema is None else v * k + ema * (1.0 - k)
        if seen >= period:
            out[i] = ema
    return out


def rsi_series(closes: list[float], period: int = RSI_PERIOD) -> list[float | None]:
    """Match chart calcRsiSeries (Wilder averages from bar `period`)."""
    n = len(closes)
    out: list[float | None] = [None] * n
    avg_gain = 0.0
    avg_loss = 0.0
    for i in range(1, n):
        change = float(closes[i]) - float(closes[i - 1])
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        if i < period:
            avg_gain += gain
            avg_loss += loss
            continue
        if i == period:
            avg_gain = (avg_gain + gain) / period
            avg_loss = (avg_loss + loss) / period
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0 and avg_gain == 0:
            out[i] = 50.0
        elif avg_loss == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def macd_series(
    closes: list[float],
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> list[tuple[float | None, float | None]]:
    """(dif, dea) per bar — chart calcMacdSeries."""
    nums: list[float | None] = [float(c) for c in closes]
    ema_fast = ema_series(nums, fast)
    ema_slow = ema_series(nums, slow)
    dif: list[float | None] = [
        (a - b) if a is not None and b is not None else None
        for a, b in zip(ema_fast, ema_slow)
    ]
    dea = ema_series(dif, signal)
    return list(zip(dif, dea))


def confluence_rows(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per bar. ``signal`` fires when ``side`` appears or changes.

    ``side`` clears when votes drop below NEED — same latch as the chart.
    """
    n = len(bars)
    if n == 0:
        return []
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    rsi = rsi_series(closes)
    macd = macd_series(closes)
    pdi_s, mdi_s, adx_s = wilder_dmi_series(highs, lows, closes)

    day = ""
    eq_sum = 0.0
    eq_sum_sq = 0.0
    eq_n = 0
    side: str | None = None
    rows: list[dict[str, Any]] = []
    for i, bar in enumerate(bars):
        t = str(bar.get("t") or "").replace("T", " ")
        key = t[:10]
        if key != day:
            day = key
            eq_sum = 0.0
            eq_sum_sq = 0.0
            eq_n = 0
            side = None
        high, low, close = highs[i], lows[i], closes[i]
        typical = (high + low + close) / 3.0
        eq_sum += typical
        eq_sum_sq += typical * typical
        eq_n += 1
        vwap = eq_sum / eq_n
        variance = max(0.0, eq_sum_sq / eq_n - vwap * vwap)
        sigma = math.sqrt(variance)
        vwap_ready = sigma >= MIN_SIGMA and eq_n >= MIN_BARS
        pull_long = vwap - PULLBACK_FRAC * sigma
        pull_short = vwap + PULLBACK_FRAC * sigma

        boll_mid = None
        if i >= BOLL_N - 1:
            window = closes[i - BOLL_N + 1 : i + 1]
            boll_mid = sum(window) / BOLL_N
            b_var = sum((c - boll_mid) ** 2 for c in window) / BOLL_N
            b_sigma = math.sqrt(b_var)
            boll_up = boll_mid + BOLL_MULT * b_sigma
            boll_dn = boll_mid - BOLL_MULT * b_sigma
        else:
            boll_up = boll_dn = None

        v_vwap = 0
        if vwap_ready:
            if close > vwap and low <= pull_long:
                v_vwap = 1
            elif close < vwap and high >= pull_short:
                v_vwap = -1
            elif close > vwap:
                v_vwap = 1
            elif close < vwap:
                v_vwap = -1

        v_boll = 0
        if boll_mid is not None and boll_dn is not None and boll_up is not None:
            if low <= boll_dn and close > boll_dn:
                v_boll = 1
            elif high >= boll_up and close < boll_up:
                v_boll = -1
            elif close > boll_mid:
                v_boll = 1
            elif close < boll_mid:
                v_boll = -1

        v_rsi = 0
        if rsi[i] is not None:
            if rsi[i] >= RSI_BULL:
                v_rsi = 1
            elif rsi[i] <= RSI_BEAR:
                v_rsi = -1

        v_macd = 0
        dif, dea = macd[i]
        if dif is not None and dea is not None:
            if dif > dea:
                v_macd = 1
            elif dif < dea:
                v_macd = -1

        v_adx = 0
        adx = adx_s[i]
        pdi = pdi_s[i]
        mdi = mdi_s[i]
        if adx is not None and adx >= ADX_MIN and pdi is not None and mdi is not None:
            if pdi > mdi:
                v_adx = 1
            elif mdi > pdi:
                v_adx = -1

        votes = {"vwap": v_vwap, "boll": v_boll, "rsi": v_rsi, "macd": v_macd, "adx": v_adx}
        bull = sum(1 for v in votes.values() if v > 0)
        bear = sum(1 for v in votes.values() if v < 0)
        nxt: str | None = None
        if bull >= NEED:
            nxt = "B"
        elif bear >= NEED:
            nxt = "S"
        signal = None
        if nxt and nxt != side:
            signal = nxt
            side = nxt
        elif not nxt:
            side = None
        rows.append(
            {
                "t": t[:16],
                "votes": votes,
                "bull": bull,
                "bear": bear,
                "need": NEED,
                "side": side,
                "signal": signal,
                "close": close,
            }
        )
    return rows


def last_day_combo(bars: list[dict[str, Any]], day: str) -> dict[str, Any] | None:
    """Newest same-session confluence row (caller must pass closed bars)."""
    last: dict[str, Any] | None = None
    for row in confluence_rows(bars):
        if str(row.get("t") or "")[:10] == day:
            last = row
    return last
