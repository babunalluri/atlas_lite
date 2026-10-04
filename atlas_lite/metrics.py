"""Sheet metrics — ADX, chain PCR/max pain, evaluation (Kite-sourced inputs only)."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from atlas_lite.specs import ALL_SPECS

IST = ZoneInfo("Asia/Kolkata")
# Index options: Black-76 with r=0 (rate/dividend already in the forward).
# Kept for any legacy callers; live ATM IV no longer uses spot Black-Scholes.
RISK_FREE = 0.0
EXPIRY_HHMM = time(15, 30)
TRADING_DAYS_PER_YEAR = 252.0


def wilder_smooth(prev: float, latest: float, period: int) -> float:
    return prev - (prev / period) + latest


def compute_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    """Wilder ATR(period) in price points (matches Kite chart ATR)."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, n):
        high, low, close_prev = highs[i], lows[i], closes[i - 1]
        tr = max(high - low, abs(high - close_prev), abs(low - close_prev))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 2)


def wilder_dmi_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Wilder +DI, -DI, ADX per bar (aligned with Kite chart DMI).

    LOCKED: matches Kite DMI(14,14) on NIFTY 1m — do not change smoothing or period.
    """
    n = min(len(highs), len(lows), len(closes))
    pdi_out: list[float | None] = [None] * n
    mdi_out: list[float | None] = [None] * n
    adx_out: list[float | None] = [None] * n
    if n < period * 2 + 1:
        return pdi_out, mdi_out, adx_out

    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        high, low, close_prev = highs[i], lows[i], closes[i - 1]
        high_prev, low_prev = highs[i - 1], lows[i - 1]
        tr = max(high - low, abs(high - close_prev), abs(low - close_prev))
        up = high - high_prev
        down = low_prev - low
        trs.append(tr)
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    if len(trs) < period:
        return pdi_out, mdi_out, adx_out

    atr = sum(trs[:period])
    plus = sum(plus_dm[:period])
    minus = sum(minus_dm[:period])
    dx_values: list[float] = []
    adx: float | None = None
    for i in range(period - 1, len(trs)):
        if i >= period:
            atr = wilder_smooth(atr, trs[i], period)
            plus = wilder_smooth(plus, plus_dm[i], period)
            minus = wilder_smooth(minus, minus_dm[i], period)
        bar_idx = i + 1
        if atr <= 0:
            dx = 0.0
            pdi_out[bar_idx] = 0.0
            mdi_out[bar_idx] = 0.0
        else:
            plus_di = 100.0 * plus / atr
            minus_di = 100.0 * minus / atr
            pdi_out[bar_idx] = round(plus_di, 2)
            mdi_out[bar_idx] = round(minus_di, 2)
            denom = plus_di + minus_di
            dx = 0.0 if denom <= 0 else 100.0 * abs(plus_di - minus_di) / denom
        dx_values.append(dx)
        if len(dx_values) >= period:
            if len(dx_values) == period:
                adx = sum(dx_values[:period]) / period
            else:
                assert adx is not None
                adx = (adx * (period - 1) + dx) / period
            adx_out[bar_idx] = round(adx, 2)
    return pdi_out, mdi_out, adx_out


def compute_adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    _, _, adx_series = wilder_dmi_series(highs, lows, closes, period)
    valid = [v for v in adx_series if v is not None]
    return valid[-1] if valid else None


def pick_float(row: dict[str, Any] | None, *keys: str) -> float | None:
    if not row:
        return None
    for key in keys:
        val = row.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def quote_ltp(row: dict[str, Any] | None) -> float | None:
    return pick_float(row, "last_price", "ltp", "last")


def quote_session_open(row: dict[str, Any] | None) -> float | None:
    """Session open from Kite ``ohlc.open``. Returns None if the quote has no open yet."""
    if not row:
        return None
    ohlc = row.get("ohlc") if isinstance(row.get("ohlc"), dict) else {}
    return pick_float(ohlc, "open")


def quote_oi(row: dict[str, Any] | None) -> float | None:
    return pick_float(row, "oi", "open_interest")


def quote_oi_day_high(row: dict[str, Any] | None) -> float | None:
    """Exchange session OI high from Kite full-mode ticks (bytes 52–55)."""
    return pick_float(row, "oi_day_high", "oi_high")


def update_oi_day_high(
    current: float,
    kite_high: float | None,
    tracked: float | None,
) -> float | None:
    """Session OI high: Kite's oi_day_high, else a running max of live OI.

    Kite often sends oi_day_high=0 even when oi is live — treat 0 as missing.
    """
    high: float | None = None
    for cand in (tracked, kite_high, current):
        if cand is None:
            continue
        val = float(cand)
        if val <= 0:
            continue
        if high is None or val > high:
            high = val
    return high


def oi_pct_of_day_high(current: float, day_high: float) -> float | None:
    if day_high <= 0:
        return None
    return round(float(current) / float(day_high) * 100.0, 2)


def iv_pct_of_day_low(current: float, day_low: float) -> float | None:
    """100 = at the session low; lower means IV has bounced above the low."""
    if current <= 0 or day_low <= 0:
        return None
    return round(float(day_low) / float(current) * 100.0, 2)


def quote_volume(row: dict[str, Any] | None) -> float | None:
    return pick_float(row, "volume", "volume_traded")


def _depth_top_level(row: dict[str, Any] | None, side: str) -> dict[str, Any] | None:
    if not row:
        return None
    depth = row.get("depth") if isinstance(row.get("depth"), dict) else {}
    levels = depth.get(side) if isinstance(depth, dict) else None
    if not isinstance(levels, list) or not levels:
        return None
    top = levels[0] if isinstance(levels[0], dict) else None
    if not top:
        return None
    qty = pick_float(top, "quantity", "qty")
    if qty is not None and qty <= 0:
        return None
    return top


def _depth_top_price(row: dict[str, Any] | None, side: str) -> float | None:
    top = _depth_top_level(row, side)
    if not top:
        return None
    return pick_float(top, "price")


def _depth_top_qty(row: dict[str, Any] | None, side: str) -> float | None:
    top = _depth_top_level(row, side)
    if not top:
        return None
    return pick_float(top, "quantity", "qty")


def quote_bid(row: dict[str, Any] | None) -> float | None:
    return pick_float(row, "bid", "best_bid", "buy_price") or _depth_top_price(row, "buy")


def quote_ask(row: dict[str, Any] | None) -> float | None:
    return pick_float(row, "ask", "best_ask", "sell_price") or _depth_top_price(row, "sell")


def quote_bid_qty(row: dict[str, Any] | None) -> float | None:
    """Best-bid size from depth; None when top-of-book is missing/empty."""
    return _depth_top_qty(row, "buy")


def quote_ask_qty(row: dict[str, Any] | None) -> float | None:
    """Best-ask size from depth; None when top-of-book is missing/empty."""
    return _depth_top_qty(row, "sell")


def quote_buy_qty(row: dict[str, Any] | None) -> float | None:
    """Total buy quantity from the quote packet (all visible interest)."""
    return pick_float(row, "buy_quantity", "total_buy_quantity")


def quote_sell_qty(row: dict[str, Any] | None) -> float | None:
    """Total sell quantity from the quote packet (all visible interest)."""
    return pick_float(row, "sell_quantity", "total_sell_quantity")


def _compact_depth_side(row: dict[str, Any] | None, side: str) -> list[list[float]]:
    """Up to 5 [price, qty] levels with qty > 0."""
    if not row:
        return []
    depth = row.get("depth") if isinstance(row.get("depth"), dict) else {}
    levels = depth.get(side) if isinstance(depth, dict) else None
    if not isinstance(levels, list):
        return []
    out: list[list[float]] = []
    for level in levels[:5]:
        if not isinstance(level, dict):
            continue
        qty = pick_float(level, "quantity", "qty")
        px = pick_float(level, "price")
        if qty is None or qty <= 0 or px is None:
            continue
        out.append([px, qty])
    return out


def top_of_book(row: dict[str, Any] | None) -> dict[str, Any]:
    """Best bid/ask + sizes, totals, 5-level depth, and exchange timestamps."""
    exch = row.get("exchange_timestamp") if row else None
    trade = row.get("last_trade_time") if row else None
    try:
        exch_ts = int(exch) if exch is not None else None
    except (TypeError, ValueError):
        exch_ts = None
    try:
        trade_ts = int(trade) if trade is not None else None
    except (TypeError, ValueError):
        trade_ts = None
    return {
        "ltp": quote_ltp(row),
        "bid": quote_bid(row),
        "ask": quote_ask(row),
        "bid_qty": quote_bid_qty(row),
        "ask_qty": quote_ask_qty(row),
        "buy_qty": quote_buy_qty(row),
        "sell_qty": quote_sell_qty(row),
        "buy": _compact_depth_side(row, "buy"),
        "sell": _compact_depth_side(row, "sell"),
        "exch_ts": exch_ts,
        "trade_ts": trade_ts,
    }


def quote_greek(row: dict[str, Any] | None, name: str) -> float | None:
    if not row:
        return None
    greeks = row.get("greeks") if isinstance(row.get("greeks"), dict) else {}
    if isinstance(greeks, dict):
        val = pick_float(greeks, name)
        if val is not None:
            return val
    return pick_float(row, name)


def strike_pcr(ce_oi: float | None, pe_oi: float | None) -> float | None:
    if ce_oi is None or pe_oi is None:
        return None
    ce = float(ce_oi)
    if ce <= 0:
        return None
    return round(float(pe_oi) / ce, 3)


def quote_iv(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    greeks = row.get("greeks")
    if isinstance(greeks, dict):
        iv = pick_float(greeks, "iv")
        if iv is not None:
            return _normalize_iv_percent(iv)
    iv = pick_float(row, "implied_volatility", "iv")
    return _normalize_iv_percent(iv) if iv is not None else None


def _normalize_iv_percent(iv: float) -> float:
    """Kite greeks.iv is percent; guard rare decimal payloads."""
    if 0 < iv < 1.0:
        return round(iv * 100.0, 4)
    return round(iv, 4)


def atm_greeks_iv(
    ce_row: dict[str, Any] | None,
    pe_row: dict[str, Any] | None,
) -> float | None:
    """ATM IV from Kite greeks only (no Black-Scholes fallback)."""
    return merge_option_iv(ce_row, pe_row)


def quote_change_pct(row: dict[str, Any] | None) -> float | None:
    """% change vs previous session close (Kite ohlc.close)."""
    if not row:
        return None
    ltp = quote_ltp(row)
    ohlc = row.get("ohlc") if isinstance(row.get("ohlc"), dict) else {}
    prev = pick_float(ohlc, "close")
    if prev is None and ltp is not None:
        net = pick_float(row, "net_change", "change")
        if net is not None:
            prev = ltp - net
    if ltp is not None and prev not in (None, 0):
        return round((ltp - prev) / prev * 100, 3)
    return None


def quote_change_from_open_pct(row: dict[str, Any] | None) -> float | None:
    """% change vs today's session open — intraday direction (not vs yesterday)."""
    if not row:
        return None
    ltp = quote_ltp(row)
    open_px = quote_session_open(row)
    if ltp is not None and open_px not in (None, 0):
        return round((ltp - open_px) / open_px * 100, 3)
    return None


def quote_change_pts(row: dict[str, Any] | None) -> float | None:
    """Absolute change vs previous close (index points)."""
    if not row:
        return None
    ltp = quote_ltp(row)
    ohlc = row.get("ohlc") if isinstance(row.get("ohlc"), dict) else {}
    prev = pick_float(ohlc, "close")
    if prev is None and ltp is not None:
        net = pick_float(row, "net_change", "change")
        if net is not None:
            prev = ltp - net
    if ltp is not None and prev not in (None, 0):
        return round(ltp - prev, 2)
    net = pick_float(row, "net_change", "change")
    if net is not None:
        return round(net, 2)
    return None


def merge_option_iv(ce_row: dict[str, Any] | None, pe_row: dict[str, Any] | None) -> float | None:
    vals = [v for v in (quote_iv(ce_row), quote_iv(pe_row)) if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black76_greeks(
    forward: float,
    strike: float,
    tte_years: float,
    sigma: float,
    *,
    call: bool,
) -> dict[str, float]:
    """Black-76 greeks on forward F. Vega is per 1 vol point; theta is per trading day (252)."""
    if tte_years <= 0 or sigma <= 0 or forward <= 0 or strike <= 0:
        return {}
    sqrt_t = math.sqrt(tte_years)
    d1 = (math.log(forward / strike) + 0.5 * sigma * sigma * tte_years) / (sigma * sqrt_t)
    nd1 = _norm_pdf(d1)
    delta = _norm_cdf(d1) if call else _norm_cdf(d1) - 1.0
    gamma = nd1 / (forward * sigma * sqrt_t)
    vega = forward * nd1 * sqrt_t / 100.0
    theta = -forward * nd1 * sigma / (2.0 * sqrt_t) / TRADING_DAYS_PER_YEAR
    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "vega": round(vega, 4),
        "theta": round(theta, 4),
    }


def _black76_price(
    forward: float,
    strike: float,
    tte_years: float,
    r: float,
    sigma: float,
    *,
    call: bool,
) -> float:
    """Black-76 option price on forward F (Kite/Sensibull-style for index F&O)."""
    disc = math.exp(-r * max(tte_years, 0.0))
    if tte_years <= 0 or sigma <= 0 or forward <= 0 or strike <= 0:
        intrinsic = max(0.0, forward - strike) if call else max(0.0, strike - forward)
        return disc * intrinsic
    vol_t = sigma * math.sqrt(tte_years)
    d1 = (math.log(forward / strike) + 0.5 * sigma * sigma * tte_years) / vol_t
    d2 = d1 - vol_t
    if call:
        return disc * (forward * _norm_cdf(d1) - strike * _norm_cdf(d2))
    return disc * (strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1))


def trading_years_to_expiry(expiry: date, now: datetime | None = None) -> float:
    """Time to 15:30 IST expiry in years on a 252 trading-day basis (Sensibull/Kite)."""
    now = now or datetime.now(IST)
    exp_dt = datetime.combine(expiry, EXPIRY_HHMM, tzinfo=IST)
    if now >= exp_dt:
        return 1.0 / TRADING_DAYS_PER_YEAR
    days = 0.0
    cursor = now.date()
    while cursor <= expiry:
        if cursor.weekday() < 5:
            close = datetime.combine(cursor, EXPIRY_HHMM, tzinfo=IST)
            if cursor == now.date() and cursor == expiry:
                days += max((exp_dt - now).total_seconds() / 86400.0, 1.0 / 24.0)
            elif cursor == now.date():
                if now < close:
                    days += (close - now).total_seconds() / 86400.0
            elif cursor == expiry:
                days += 15.5 / 24.0
            else:
                days += 1.0
        cursor += timedelta(days=1)
    return max(days, 1.0 / 24.0) / TRADING_DAYS_PER_YEAR


def years_to_expiry(expiry: date, now: datetime | None = None) -> float:
    """Alias — ATM IV uses trading-day / 252 (Kite-aligned)."""
    return trading_years_to_expiry(expiry, now=now)


def synthetic_forward(
    spot: float,
    strike: float,
    ce_ltp: float | None,
    pe_ltp: float | None,
) -> float:
    """Put-call parity forward with r=0: F = K + C − P. Falls back to spot."""
    if (
        ce_ltp is not None
        and pe_ltp is not None
        and ce_ltp > 0
        and pe_ltp > 0
        and strike > 0
    ):
        return float(strike) + float(ce_ltp) - float(pe_ltp)
    return float(spot)


CARRY_RATE_ANNUAL = 0.065


def option_carry_from_fut(
    spot: float,
    fut: float | None,
    *,
    option_dte: float | None,
    fut_dte: float | None = None,
    carry_rate: float = CARRY_RATE_ANNUAL,
) -> tuple[float | None, float | None]:
    """Carry / forward matched to *option* expiry, not monthly fut expiry.

    Monthly fut basis overstates weekly carry. Scale:
    ``carry = (fut − spot) × option_dte / fut_dte``.
    Falls back to ``spot × r × option_dte / 365`` when fut/dte missing.
    Returns ``(forward, carry_pts)``.
    """
    try:
        s = float(spot)
    except (TypeError, ValueError):
        return None, None
    odte = None if option_dte is None else max(0.0, float(option_dte))
    fdte = None if fut_dte is None else max(0.0, float(fut_dte))
    carry: float | None = None
    if fut is not None and odte is not None and fdte is not None and fdte > 0:
        carry = (float(fut) - s) * odte / fdte
    elif fut is not None and (odte is None or fdte is None or fdte <= 0):
        # No safe scale → do not use full monthly basis; estimate from rate if possible.
        if odte is not None:
            carry = s * float(carry_rate) * odte / 365.0
        else:
            carry = float(fut) - s
    elif odte is not None:
        carry = s * float(carry_rate) * odte / 365.0
    else:
        return s, 0.0
    carry = round(float(carry), 2)
    return round(s + carry, 2), carry


# Ignore option-implied forward when cash is the better ATM (thin/close quotes).
# NIFTY basis is typically ~30–55 pts; a 25-pt cap rejected almost all healthy forwards.
ATM_FORWARD_MIN_GAP_PTS = 50.0
ATM_FORWARD_GAP_SPOT_PCT = 0.005  # 0.5% of cash (~118 pts at 23600)
ATM_FORWARD_MIN_PREMIUM = 15.0
ATM_SPOT_ONLY_AFTER = time(15, 10)


def atm_forward_gap_limit(spot: float) -> float:
    """Allow normal FUT/cash basis; reject only wild CE−PE spikes."""
    return max(ATM_FORWARD_MIN_GAP_PTS, abs(float(spot)) * ATM_FORWARD_GAP_SPOT_PCT)


def atm_ref_price(
    spot: float,
    strike: float | None,
    ce_ltp: float | None,
    pe_ltp: float | None,
    *,
    now: datetime | None = None,
) -> float:
    """ATM input: synthetic forward only when both legs are liquid and near spot.

    After 15:10 IST (cash winding down / expiry afternoon) always use cash spot so
    dying option quotes cannot hunt ATM across strikes.
    """
    cash = float(spot)
    clock = now or datetime.now(IST)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=IST)
    else:
        clock = clock.astimezone(IST)
    if clock.time() >= ATM_SPOT_ONLY_AFTER:
        return cash
    if strike is None or ce_ltp is None or pe_ltp is None:
        return cash
    if ce_ltp < ATM_FORWARD_MIN_PREMIUM or pe_ltp < ATM_FORWARD_MIN_PREMIUM:
        return cash
    fwd = synthetic_forward(cash, float(strike), ce_ltp, pe_ltp)
    if abs(fwd - cash) > atm_forward_gap_limit(cash):
        return cash
    return fwd


def implied_volatility(
    price: float,
    forward: float,
    strike: float,
    tte_years: float,
    *,
    call: bool = True,
    r: float = RISK_FREE,
) -> float | None:
    """Black-76 IV as annualized percent (e.g. 14.5). `forward` is F, not spot."""
    if price <= 0 or forward <= 0 or strike <= 0 or tte_years <= 0:
        return None
    disc = math.exp(-r * tte_years)
    intrinsic = disc * (
        max(0.0, forward - strike) if call else max(0.0, strike - forward)
    )
    if price <= intrinsic + 1e-6:
        return None
    lo, hi = 1e-4, 5.0
    p_lo = _black76_price(forward, strike, tte_years, r, lo, call=call)
    p_hi = _black76_price(forward, strike, tte_years, r, hi, call=call)
    if not (p_lo <= price <= p_hi):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2.0
        model = _black76_price(forward, strike, tte_years, r, mid, call=call)
        if model > price:
            hi = mid
        else:
            lo = mid
    return round(((lo + hi) / 2.0) * 100.0, 4)


def atm_iv_from_prices(
    ce: float | None,
    pe: float | None,
    spot: float,
    strike: float,
    expiry: date,
    *,
    now: datetime | None = None,
) -> float | None:
    """Black-76 ATM IV from CE/PE premiums (synthetic forward, 252d TTE)."""
    tte = trading_years_to_expiry(expiry, now=now)
    forward = synthetic_forward(spot, float(strike), ce, pe)
    ivs: list[float] = []
    if ce is not None and ce > 0:
        iv = implied_volatility(ce, forward, float(strike), tte, call=True)
        if iv is not None:
            ivs.append(iv)
    if pe is not None and pe > 0:
        iv = implied_volatility(pe, forward, float(strike), tte, call=False)
        if iv is not None:
            ivs.append(iv)
    if not ivs:
        return None
    return round(sum(ivs) / len(ivs), 4)


def atm_iv_from_ltp(
    ce_row: dict[str, Any] | None,
    pe_row: dict[str, Any] | None,
    spot: float,
    strike: int,
    expiry: date,
    *,
    now: datetime | None = None,
) -> float | None:
    """ATM IV via Black-76 on synthetic forward (Kite/Sensibull-aligned fallback)."""
    return atm_iv_from_prices(
        quote_ltp(ce_row),
        quote_ltp(pe_row),
        spot,
        float(strike),
        expiry,
        now=now,
    )


def resolve_atm_iv(
    ce_row: dict[str, Any] | None,
    pe_row: dict[str, Any] | None,
    spot: float | None,
    strike: int | None,
    expiry: date | None,
) -> float | None:
    iv = merge_option_iv(ce_row, pe_row)
    if iv is not None:
        return iv
    if spot is None or strike is None or expiry is None:
        return None
    return atm_iv_from_ltp(ce_row, pe_row, spot, strike, expiry)


def max_pain_strike(
    strikes: list[int],
    ce_oi: dict[int, float],
    pe_oi: dict[int, float],
) -> int | None:
    if not strikes:
        return None
    total_oi = sum(ce_oi.get(s, 0.0) for s in strikes) + sum(pe_oi.get(s, 0.0) for s in strikes)
    if total_oi <= 0:
        return None
    best_strike: int | None = None
    best_pain = float("inf")
    tied = False
    for candidate in strikes:
        pain = 0.0
        for strike in strikes:
            ce = ce_oi.get(strike, 0.0)
            pe = pe_oi.get(strike, 0.0)
            pain += ce * max(0.0, candidate - strike) + pe * max(0.0, strike - candidate)
        if pain < best_pain:
            best_pain = pain
            best_strike = candidate
            tied = False
        elif abs(pain - best_pain) < 1e-3 and best_strike is not None:
            tied = True
    return None if tied else best_strike


def _option_leg_snapshot(row: dict[str, Any] | None) -> dict[str, float | None]:
    return {
        "ltp": quote_ltp(row),
        "oi": quote_oi(row),
        "chg_pct": quote_change_pct(row),
        "iv": quote_iv(row),
        "vol": quote_volume(row),
        "bid": quote_bid(row),
        "ask": quote_ask(row),
        "delta": quote_greek(row, "delta"),
        "theta": quote_greek(row, "theta"),
        "vega": quote_greek(row, "vega"),
        "gamma": quote_greek(row, "gamma"),
    }


def _fill_model_greeks(
    leg: dict[str, float | None],
    *,
    call: bool,
    forward: float,
    strike: float,
    expiry: date,
) -> None:
    """Fill missing IV/greeks from Black-76. Never overwrites Kite values."""
    ltp = leg.get("ltp")
    if ltp is None or ltp <= 0 or forward <= 0 or strike <= 0:
        return
    tte = trading_years_to_expiry(expiry)
    iv_pct = leg.get("iv")
    if iv_pct is None:
        iv_pct = implied_volatility(float(ltp), forward, strike, tte, call=call)
        if iv_pct is not None:
            leg["iv"] = iv_pct
    if iv_pct is None:
        return
    if all(leg.get(k) is not None for k in ("delta", "theta", "vega", "gamma")):
        return
    greeks = black76_greeks(forward, strike, tte, float(iv_pct) / 100.0, call=call)
    for key, val in greeks.items():
        if leg.get(key) is None:
            leg[key] = val


def option_chain_rows(
    strikes: list[int],
    ce_rows: list[dict[str, Any] | None],
    pe_rows: list[dict[str, Any] | None],
    *,
    atm_strike: int | None = None,
    wing_strikes: int | None = None,
    strike_step: int = 50,
    extras: bool = False,
    spot: float | None = None,
    expiry: date | None = None,
) -> list[dict[str, Any]]:
    """Build UI rows for NIFTY option chain (CE | strike | PE)."""
    pairs = list(zip(strikes, ce_rows, pe_rows))
    if wing_strikes is not None and atm_strike is not None:
        lo = atm_strike - wing_strikes * strike_step
        hi = atm_strike + wing_strikes * strike_step
        pairs = [(s, ce, pe) for s, ce, pe in pairs if lo <= s <= hi]

    rows: list[dict[str, Any]] = []
    for strike, ce_row, pe_row in pairs:
        ce_leg = _option_leg_snapshot(ce_row)
        pe_leg = _option_leg_snapshot(pe_row)
        if extras and spot is not None and expiry is not None:
            fwd = synthetic_forward(float(spot), float(strike), ce_leg.get("ltp"), pe_leg.get("ltp"))
            _fill_model_greeks(ce_leg, call=True, forward=fwd, strike=float(strike), expiry=expiry)
            _fill_model_greeks(pe_leg, call=False, forward=fwd, strike=float(strike), expiry=expiry)
        rows.append(
            {
                "strike": strike,
                "is_atm": strike == atm_strike,
                "pcr": strike_pcr(ce_leg.get("oi"), pe_leg.get("oi")),
                "ce": ce_leg,
                "pe": pe_leg,
            }
        )
    return rows


def _accumulate_leg(rows: list[dict[str, Any]], leg_key: str) -> dict[str, float | None]:
    """Sum OI/LTP for visible chain; aggregate % from summed prev-close implied prices."""
    oi_sum = 0.0
    ltp_sum = 0.0
    prev_sum = 0.0
    oi_n = ltp_n = pct_n = 0
    for row in rows:
        leg = row.get(leg_key) if isinstance(row.get(leg_key), dict) else {}
        if not leg:
            continue
        oi = leg.get("oi")
        ltp = leg.get("ltp")
        chg = leg.get("chg_pct")
        if oi is not None:
            oi_sum += float(oi)
            oi_n += 1
        if ltp is not None:
            ltp_sum += float(ltp)
            ltp_n += 1
        if ltp is not None and chg is not None:
            ltp_f = float(ltp)
            chg_f = float(chg)
            if chg_f <= -100.0:
                continue
            denom = 1.0 + chg_f / 100.0
            if abs(denom) < 1e-12:
                continue
            prev_sum += ltp_f / denom
            pct_n += 1
    chg_pct: float | None = None
    if pct_n > 0 and prev_sum > 0:
        ltp_for_pct = sum(
            float((row.get(leg_key) or {}).get("ltp"))
            for row in rows
            if isinstance(row.get(leg_key), dict)
            and (row.get(leg_key) or {}).get("ltp") is not None
            and (row.get(leg_key) or {}).get("chg_pct") is not None
            and float((row.get(leg_key) or {}).get("chg_pct")) > -100.0
        )
        chg_pct = round((ltp_for_pct - prev_sum) / prev_sum * 100, 2)
    return {
        "oi": round(oi_sum) if oi_n else None,
        "ltp": round(ltp_sum, 2) if ltp_n else None,
        "chg_pct": chg_pct,
    }


def chain_accumulated_totals(rows: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    """Totals for visible option chain rows (OI sum, LTP sum, calc % chg)."""
    if not rows:
        return {"ce": {"oi": None, "ltp": None, "chg_pct": None}, "pe": {"oi": None, "ltp": None, "chg_pct": None}}
    return {"ce": _accumulate_leg(rows, "ce"), "pe": _accumulate_leg(rows, "pe")}


def chain_pcr_max_pain(
    strikes: list[int],
    ce_rows: list[dict[str, Any] | None],
    pe_rows: list[dict[str, Any] | None],
) -> dict[str, float]:
    ce_oi: dict[int, float] = {}
    pe_oi: dict[int, float] = {}
    ce_total = pe_total = 0.0
    matched = 0
    for strike, ce_row, pe_row in zip(strikes, ce_rows, pe_rows):
        if ce_row is not None or pe_row is not None:
            matched += 1
        ce = quote_oi(ce_row) or 0.0
        pe = quote_oi(pe_row) or 0.0
        ce_oi[strike] = ce
        pe_oi[strike] = pe
        ce_total += ce
        pe_total += pe
    if matched == 0:
        return {}
    out: dict[str, float] = {}
    if pe_total > 0 and ce_total > 0:
        out["pcr"] = round(pe_total / ce_total, 4)
    mp = max_pain_strike(strikes, ce_oi, pe_oi)
    if mp is not None:
        out["max_pain"] = float(mp)
    return out


def format_target(spec: dict[str, Any]) -> str:
    rule = str(spec.get("rule") or "")
    target = float(spec.get("target") or 0)
    high = spec.get("target_high")
    sid = str(spec.get("id") or "")
    if rule == "iv_near_day_low":
        return f"near session low (≥{target:g}%)"
    if rule == "ce_pe_balanced":
        return f"CE ≈ PE (Δ≤{target:g}%)"
    if sid == "vix_chg":
        return f"within ±{target:g} pts"
    if sid == "pe":
        return "ATM live"
    if rule == "lt":
        return f"< {target:g}"
    if rule == "gte":
        if sid == "oi_day_high":
            return f"≥ {target:g}% of day high"
        return f"≥ {target:g}"
    if rule == "gt":
        return f"> {target:g}"
    if rule == "abs_lte":
        return f"within ±{target:g}%"
    if rule == "between":
        return f"{target:g} – {float(high or target):g}"
    if rule == "spot_below_max_pain":
        return "spot < max pain"
    if rule == "info":
        return "live"
    return str(target)


def evaluate_rule(
    spec: dict[str, Any],
    value: float | None,
    feed: dict[str, Any],
) -> bool | None:
    rule = str(spec.get("rule") or "info")
    target = float(spec.get("target") or 0)
    high = spec.get("target_high")
    sid = str(spec.get("id") or "")
    if rule == "info":
        return None
    if value is None:
        return None
    if rule == "lt":
        return value < target
    if rule == "gte":
        return value >= target
    if rule == "gt":
        return value > target
    if rule == "abs_lte":
        return abs(value) <= target
    if rule == "between":
        return target <= value <= float(high or target)
    if rule == "iv_near_day_low":
        iv = feed.get("iv")
        day_low = feed.get("iv_day_low")
        if iv is None or day_low is None:
            return None
        pct = iv_pct_of_day_low(float(iv), float(day_low))
        if pct is None:
            return None
        return pct >= target
    if rule == "ce_pe_balanced":
        ce = feed.get("ce")
        pe = feed.get("pe")
        if ce is None or pe is None:
            return None
        try:
            ce_f = float(ce)
            pe_f = float(pe)
        except (TypeError, ValueError):
            return None
        if ce_f <= 0 or pe_f <= 0:
            return False
        avg = (abs(ce_f) + abs(pe_f)) / 2.0
        if avg <= 0:
            return False
        return (abs(ce_f - pe_f) / avg * 100.0) <= target
    if rule == "spot_below_max_pain":
        spot = feed.get("nifty_ltp")
        mp = feed.get("max_pain")
        if spot is None or mp is None:
            return None
        return float(spot) < float(mp)
    if sid == "vix_chg":
        return abs(value) <= target
    return None


def evaluate_sheet(feed: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    evaluable = passed = gates_total = 0
    missing_gates: list[str] = []
    failing_gates: list[str] = []
    table_row = 0
    for spec in ALL_SPECS:
        feed_key = str(spec.get("feed_key") or spec["id"])
        raw = feed.get(feed_key)
        value = float(raw) if raw is not None else None
        ok = evaluate_rule(spec, value, feed)
        gates = bool(spec.get("gates_entry"))
        label = str(spec.get("label") or spec.get("id") or feed_key)
        if gates:
            gates_total += 1
            if ok is None:
                missing_gates.append(label)
            elif ok:
                passed += 1
                evaluable += 1
            else:
                failing_gates.append(label)
                evaluable += 1
        display = None if spec.get("rule") == "info" else ok
        if spec.get("show_in_table") is False:
            continue
        table_row += 1
        rows.append(
            {
                "row": spec.get("row", table_row),
                "id": spec["id"],
                "label": label,
                "value": value,
                "target": format_target(spec),
                "passed": display,
                "gates_entry": gates,
                "hint": spec.get("hint"),
            }
        )
    return {
        "rows": rows,
        "entry_ready": evaluable > 0 and evaluable == gates_total and passed == evaluable,
        "passed": passed,
        "evaluable": evaluable,
        "gates_total": gates_total,
        "missing_gates": missing_gates,
        "failing_gates": failing_gates,
    }
