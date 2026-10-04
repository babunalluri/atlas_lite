"""Width helpers for the 30-minute forecast.

The on-chart target and range are computed in static/pred30.js, which is what
the page renders. This module covers IV, straddle, ATR, linreg, and the
direction vote. Do not add a second copy of the price path here.

IV uses a 252 × 375 trading-minute year (same basis as paper RV). The ATM
straddle is the remaining-life expected move, so it is scaled by minutes to
expiry — not minutes left in today's session.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

HORIZON_MIN = 30
SESSION_START_MIN = 9 * 60 + 15
SESSION_END_MIN = 15 * 60 + 30
SESSION_MINUTES = float(SESSION_END_MIN - SESSION_START_MIN)
TRADING_DAYS_PER_YEAR = 252.0
TRADING_MINUTES_PER_YEAR = SESSION_MINUTES * TRADING_DAYS_PER_YEAR
LINREG_MIN_BARS = 8
IST = ZoneInfo("Asia/Kolkata")


def minutes_left_in_session(hour: int, minute: int) -> float:
    """Minutes to 15:30 IST, floored at the 30-minute horizon so the cone does not explode."""
    left = SESSION_END_MIN - (int(hour) * 60 + int(minute))
    return float(max(left, HORIZON_MIN))


def trading_minutes_to_expiry(expiry: date | None, now: datetime | None = None) -> float | None:
    """NSE cash-session minutes from ``now`` to 15:30 IST on ``expiry`` (weekdays only)."""
    if expiry is None:
        return None
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    today = now.date()
    if expiry < today:
        return None
    clock = now.hour * 60 + now.minute

    def remaining_today() -> float:
        if today.weekday() >= 5:
            return 0.0
        if clock >= SESSION_END_MIN:
            return 0.0
        if clock <= SESSION_START_MIN:
            return SESSION_MINUTES
        return float(SESSION_END_MIN - clock)

    minutes = remaining_today()
    cursor = today + timedelta(days=1)
    while cursor <= expiry:
        if cursor.weekday() < 5:
            minutes += SESSION_MINUTES
        cursor += timedelta(days=1)
    if minutes < HORIZON_MIN:
        return None
    return float(minutes)


def iv_half_width(spot: float | None, iv_pct: float | None) -> float | None:
    """30-minute expected move from annualized IV on a 252 × 375 trading-minute year."""
    if spot is None or iv_pct is None or spot <= 0 or iv_pct <= 0:
        return None
    return round(
        float(spot) * (float(iv_pct) / 100.0) * math.sqrt(HORIZON_MIN / TRADING_MINUTES_PER_YEAR),
        2,
    )


def straddle_half_width(straddle: float | None, minutes_to_expiry: float | None) -> float | None:
    """Scale the ATM straddle (remaining-life move) down to 30 trading minutes."""
    if straddle is None or minutes_to_expiry is None or straddle <= 0 or minutes_to_expiry < HORIZON_MIN:
        return None
    return round(float(straddle) * math.sqrt(HORIZON_MIN / float(minutes_to_expiry)), 2)


def atr_half_width(atr: float | None, bar_minutes: float) -> float | None:
    if atr is None or atr <= 0 or bar_minutes <= 0:
        return None
    return round(float(atr) * math.sqrt(HORIZON_MIN / float(bar_minutes)), 2)


def linreg_lookback(bar_minutes: float) -> int:
    if bar_minutes <= 0:
        return LINREG_MIN_BARS
    return max(LINREG_MIN_BARS, int(math.ceil(HORIZON_MIN / float(bar_minutes))))


def linreg_slope_and_sigma(closes: list[float]) -> tuple[float | None, float | None]:
    n = len(closes)
    if n < 5:
        return None, None
    mean_x = (n - 1) / 2.0
    mean_y = sum(closes) / n
    var_x = sum((i - mean_x) ** 2 for i in range(n))
    if var_x <= 0:
        return None, None
    cov = sum((i - mean_x) * (closes[i] - mean_y) for i in range(n))
    slope = cov / var_x
    intercept = mean_y - slope * mean_x
    resid = [closes[i] - (intercept + slope * i) for i in range(n)]
    mean_r = sum(resid) / n
    var_r = sum((r - mean_r) ** 2 for r in resid) / max(n - 2, 1)
    sigma = math.sqrt(max(var_r, 0.0))
    return slope, (round(sigma, 2) if sigma > 0 else None)


def ichimoku_span_a(highs: list[float], lows: list[float]) -> float | None:
    if len(highs) < 26 or len(lows) < 26:
        return None
    tenkan = (max(highs[-9:]) + min(lows[-9:])) / 2.0
    kijun = (max(highs[-26:]) + min(lows[-26:])) / 2.0
    return (tenkan + kijun) / 2.0


def ensemble_band(widths: list[float | None]) -> dict[str, Any]:
    vals = sorted(float(w) for w in widths if w is not None and w > 0)
    if not vals:
        return {"n": 0, "median": None, "tight": None, "widths": []}
    mid_i = len(vals) // 2
    if len(vals) % 2:
        mid = vals[mid_i]
    else:
        mid = (vals[mid_i - 1] + vals[mid_i]) / 2.0
    return {
        "n": len(vals),
        "median": round(mid, 2),
        "tight": round(vals[0], 2),
        "widths": [round(v, 2) for v in vals],
    }


def vote_side(votes: list[int]) -> dict[str, Any]:
    """+1 up, -1 down, 0 abstain."""
    up = sum(1 for v in votes if v > 0)
    down = sum(1 for v in votes if v < 0)
    total = up + down
    if total == 0:
        return {"up": 0, "down": 0, "total": 0, "side": "flat", "label": "0/0"}
    if up > down:
        side = "up"
    elif down > up:
        side = "down"
    else:
        side = "flat"
    winning = up if up >= down else down
    return {
        "up": up,
        "down": down,
        "total": total,
        "side": side,
        "label": f"{winning}/{total} {side}",
    }


def iv_or_straddle_width(
    *,
    spot: float | None,
    iv_pct: float | None,
    ce: float | None,
    pe: float | None,
    minutes_to_expiry: float | None,
) -> float | None:
    """Prefer IV percent; else ATM straddle scaled by remaining life to expiry."""
    iv_w = iv_half_width(spot, iv_pct)
    if iv_w is not None:
        return iv_w
    if ce is None or pe is None:
        return None
    return straddle_half_width(float(ce) + float(pe), minutes_to_expiry)
