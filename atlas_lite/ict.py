"""ICT liquidity sweep → displacement → MSS → FVG (no broker orders).

The sample checked every condition on the latest bar at once. A sweep close
and a displacement close are different bars, and the fair-value gap is filled
on a later bar. This engine keeps that sequence:

* Higher-timeframe bias from confirmed swing highs/lows (default 15m).
* Execution (default 5m): sell-side sweep for a long, buy-side sweep for a short.
* A later displacement bar in the bias direction, with a market-structure shift.
* The fair-value gap is the space between the candle before that displacement
  and the candle after it. Entry is a later bar that trades back into the gap.
* Stop beyond the sweep extreme; target is the nearest opposing liquidity.
* Skip when reward/risk is below ``minimum_rr``.
* Sweep, displacement, and the gap must be on the same session day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Bias = Literal["LONG", "SHORT", "NEUTRAL"]
SignalName = Literal["LONG", "SHORT", "WAIT", "NO_TRADE"]

Bar = dict[str, Any]


@dataclass(frozen=True)
class ICTConfig:
    swing_left: int = 2
    swing_right: int = 2
    atr_period: int = 14
    body_median_period: int = 20
    displacement_body_mult: float = 1.5
    fvg_atr_min: float = 0.10
    sweep_max_penetration: float = 0.002
    sl_atr_buffer: float = 0.05
    minimum_rr: float = 2.0
    max_holding_bars: int = 24
    sweep_lookback: int = 8
    fvg_retrace_bars: int = 6


@dataclass
class ICTSignal:
    signal: SignalName
    reason: str
    entry: float | None = None
    stop_loss: float | None = None
    target: float | None = None
    risk_reward: float | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _f(bar: Bar, key: str) -> float | None:
    try:
        return float(bar[key])
    except (KeyError, TypeError, ValueError):
        return None


def bar_dt(bar: Bar) -> datetime | None:
    raw = str(bar.get("t") or "").replace("T", " ")[:16]
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def aggregate_bars(bars: list[Bar], minutes: int) -> list[Bar]:
    """Roll 1m (or finer) OHLC into ``minutes`` buckets. Last bucket may be open."""
    if minutes <= 0:
        return []
    order: list[str] = []
    buckets: dict[str, Bar] = {}
    for bar in bars:
        dt = bar_dt(bar)
        o, h, l, c = _f(bar, "o"), _f(bar, "h"), _f(bar, "l"), _f(bar, "c")
        if dt is None or None in (o, h, l, c):
            continue
        floored = dt.replace(minute=(dt.minute // minutes) * minutes, second=0, microsecond=0)
        key = floored.strftime("%Y-%m-%d %H:%M")
        assert o is not None and h is not None and l is not None and c is not None
        if key not in buckets:
            buckets[key] = {"t": key, "o": o, "h": h, "l": l, "c": c}
            order.append(key)
            continue
        row = buckets[key]
        row["h"] = max(float(row["h"]), h)
        row["l"] = min(float(row["l"]), l)
        row["c"] = c
    return [buckets[k] for k in order]


def drop_open_bucket(bars: list[Bar], *, minutes: int, now: datetime) -> list[Bar]:
    """Drop the bucket that ``now`` still sits inside."""
    if not bars or minutes <= 0:
        return bars
    naive = now.replace(tzinfo=None) if now.tzinfo else now
    floored = naive.replace(minute=(naive.minute // minutes) * minutes, second=0, microsecond=0)
    key = floored.strftime("%Y-%m-%d %H:%M")
    if bars[-1].get("t") == key:
        return bars[:-1]
    return bars


def true_ranges(bars: list[Bar]) -> list[float | None]:
    out: list[float | None] = []
    prev_c: float | None = None
    for bar in bars:
        h, l, c = _f(bar, "h"), _f(bar, "l"), _f(bar, "c")
        if None in (h, l, c):
            out.append(None)
            continue
        assert h is not None and l is not None and c is not None
        parts = [h - l]
        if prev_c is not None:
            parts.append(abs(h - prev_c))
            parts.append(abs(l - prev_c))
        out.append(max(parts))
        prev_c = c
    return out


def atr_at(bars: list[Bar], index: int, period: int) -> float | None:
    trs = true_ranges(bars)
    if index < period - 1:
        return None
    window = trs[index - period + 1 : index + 1]
    if any(v is None for v in window):
        return None
    vals = [float(v) for v in window if v is not None]
    return sum(vals) / len(vals)


def detect_swings(bars: list[Bar], cfg: ICTConfig) -> tuple[list[int], list[int]]:
    """Return confirmed swing-high and swing-low indexes."""
    left, right = cfg.swing_left, cfg.swing_right
    highs: list[int] = []
    lows: list[int] = []
    for i in range(left, len(bars) - right):
        h, l = _f(bars[i], "h"), _f(bars[i], "l")
        if h is None or l is None:
            continue
        left_h = [_f(bars[j], "h") for j in range(i - left, i)]
        right_h = [_f(bars[j], "h") for j in range(i + 1, i + right + 1)]
        left_l = [_f(bars[j], "l") for j in range(i - left, i)]
        right_l = [_f(bars[j], "l") for j in range(i + 1, i + right + 1)]
        if any(v is None for v in left_h + right_h + left_l + right_l):
            continue
        lh = [float(v) for v in left_h if v is not None]
        rh = [float(v) for v in right_h if v is not None]
        ll = [float(v) for v in left_l if v is not None]
        rl = [float(v) for v in right_l if v is not None]
        if h > max(lh) and h >= max(rh):
            highs.append(i)
        if l < min(ll) and l <= min(rl):
            lows.append(i)
    return highs, lows


def htf_bias(bars: list[Bar], cfg: ICTConfig | None = None) -> Bias:
    cfg = cfg or ICTConfig()
    highs, lows = detect_swings(bars, cfg)
    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"
    last_high = _f(bars[highs[-1]], "h")
    prev_high = _f(bars[highs[-2]], "h")
    last_low = _f(bars[lows[-1]], "l")
    prev_low = _f(bars[lows[-2]], "l")
    if None in (last_high, prev_high, last_low, prev_low):
        return "NEUTRAL"
    assert last_high is not None and prev_high is not None
    assert last_low is not None and prev_low is not None
    if last_high > prev_high and last_low > prev_low:
        return "LONG"
    if last_high < prev_high and last_low < prev_low:
        return "SHORT"
    return "NEUTRAL"


def _body(bar: Bar) -> float | None:
    o, c = _f(bar, "o"), _f(bar, "c")
    if o is None or c is None:
        return None
    return abs(c - o)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _sweep_at(bars: list[Bar], index: int, bias: Bias, cfg: ICTConfig, highs: list[int], lows: list[int]) -> dict[str, Any] | None:
    bar = bars[index]
    low, high, close = _f(bar, "l"), _f(bar, "h"), _f(bar, "c")
    if None in (low, high, close):
        return None
    assert low is not None and high is not None and close is not None
    if bias == "LONG":
        prior = [i for i in lows if i < index]
        if not prior:
            return None
        level = _f(bars[prior[-1]], "l")
        if level is None or level <= 0:
            return None
        penetration = (level - low) / level
        if low < level and close > level and 0 <= penetration <= cfg.sweep_max_penetration:
            return {"type": "SELL_SIDE_SWEEP", "level": level, "extreme": low, "index": index}
    if bias == "SHORT":
        prior = [i for i in highs if i < index]
        if not prior:
            return None
        level = _f(bars[prior[-1]], "h")
        if level is None or level <= 0:
            return None
        penetration = (high - level) / level
        if high > level and close < level and 0 <= penetration <= cfg.sweep_max_penetration:
            return {"type": "BUY_SIDE_SWEEP", "level": level, "extreme": high, "index": index}
    return None


def _is_displacement(bars: list[Bar], index: int, bias: Bias, cfg: ICTConfig) -> bool:
    if index < 1 or index < cfg.body_median_period:
        return False
    bodies: list[float] = []
    for j in range(index - cfg.body_median_period, index):
        body = _body(bars[j])
        if body is None:
            return False
        bodies.append(body)
    median = _median(bodies)
    current_body = _body(bars[index])
    if median is None or current_body is None:
        return False
    if current_body < cfg.displacement_body_mult * median:
        return False
    cur_o, cur_c = _f(bars[index], "o"), _f(bars[index], "c")
    prev_h, prev_l = _f(bars[index - 1], "h"), _f(bars[index - 1], "l")
    if None in (cur_o, cur_c, prev_h, prev_l):
        return False
    assert cur_o is not None and cur_c is not None and prev_h is not None and prev_l is not None
    if bias == "LONG":
        return cur_c > cur_o and cur_c > prev_h
    if bias == "SHORT":
        return cur_c < cur_o and cur_c < prev_l
    return False


def _same_session(a: Bar, b: Bar) -> bool:
    da, db = bar_dt(a), bar_dt(b)
    return da is not None and db is not None and da.date() == db.date()


def _fvg_at(bars: list[Bar], disp: int, bias: Bias, cfg: ICTConfig) -> dict[str, Any] | None:
    """Gap around the displacement candle: candle before vs candle after.

    The displacement bar is the middle candle. A bullish gap is
    ``low[d+1] > high[d-1]``. The bar that prints the gap is not an entry.
    """
    if disp < 1 or disp + 1 >= len(bars):
        return None
    left, mid, right = bars[disp - 1], bars[disp], bars[disp + 1]
    if not (_same_session(left, mid) and _same_session(mid, right)):
        return None
    atr = atr_at(bars, disp, cfg.atr_period)
    if atr is None or atr <= 0:
        return None
    if bias == "LONG":
        left_h, right_l = _f(left, "h"), _f(right, "l")
        if left_h is None or right_l is None or right_l <= left_h:
            return None
        size = right_l - left_h
        if size < cfg.fvg_atr_min * atr:
            return None
        return {
            "type": "BULLISH_FVG",
            "low": left_h,
            "high": right_l,
            "size": size,
            "index": disp + 1,
            "disp": disp,
        }
    if bias == "SHORT":
        left_l, right_h = _f(left, "l"), _f(right, "h")
        if left_l is None or right_h is None or right_h >= left_l:
            return None
        size = left_l - right_h
        if size < cfg.fvg_atr_min * atr:
            return None
        return {
            "type": "BEARISH_FVG",
            "low": right_h,
            "high": left_l,
            "size": size,
            "index": disp + 1,
            "disp": disp,
        }
    return None


def _touch_entry(bar: Bar, fvg: dict[str, Any], bias: Bias) -> float | None:
    """Limit touch of the gap. A close inside the zone is not required.

    A long fills at the top of the gap when the bar opens above it and trades
    down into it. A short fills at the bottom when the bar opens below it.
    """
    o, h, l = _f(bar, "o"), _f(bar, "h"), _f(bar, "l")
    if None in (o, h, l):
        return None
    assert o is not None and h is not None and l is not None
    lo, hi = float(fvg["low"]), float(fvg["high"])
    if l > hi or h < lo:
        return None
    if bias == "LONG":
        if o > hi:
            return hi
        if lo <= o <= hi:
            return o
        return None
    if o < lo:
        return lo
    if lo <= o <= hi:
        return o
    return None


def _mss(bars: list[Bar], disp: int, end: int, bias: Bias, highs: list[int], lows: list[int]) -> bool:
    if bias == "LONG":
        prior = [i for i in highs if i < disp]
        if not prior:
            return False
        level = _f(bars[prior[-1]], "h")
        if level is None:
            return False
        for j in range(disp, end + 1):
            close = _f(bars[j], "c")
            if close is not None and close > level:
                return True
        return False
    prior = [i for i in lows if i < disp]
    if not prior:
        return False
    level = _f(bars[prior[-1]], "l")
    if level is None:
        return False
    for j in range(disp, end + 1):
        close = _f(bars[j], "c")
        if close is not None and close < level:
            return True
    return False


def _target(
    bars: list[Bar],
    entry: float,
    bias: Bias,
    highs: list[int],
    lows: list[int],
    *,
    risk: float,
    minimum_rr: float,
) -> float | None:
    """Nearest opposing swing that still pays ``minimum_rr``.

    The very next swing is often only a few points away, while the stop sits
    beyond the sweep. That swing is not the target. Take the next one that is.
    """
    if risk <= 0:
        return None
    if bias == "LONG":
        levels = sorted(h for i in highs if (h := _f(bars[i], "h")) is not None and h > entry)
        for level in levels:
            if (level - entry) / risk >= minimum_rr:
                return level
        return None
    levels = sorted(
        (lv for i in lows if (lv := _f(bars[i], "l")) is not None and lv < entry),
        reverse=True,
    )
    for level in levels:
        if (entry - level) / risk >= minimum_rr:
            return level
    return None


def generate_entry(
    htf_bars: list[Bar],
    execution_bars: list[Bar],
    cfg: ICTConfig | None = None,
) -> ICTSignal:
    cfg = cfg or ICTConfig()
    bias = htf_bias(htf_bars, cfg)
    if bias == "NEUTRAL":
        return ICTSignal(signal="NO_TRADE", reason="HTF bias is neutral")
    if len(execution_bars) < cfg.atr_period + cfg.body_median_period + 3:
        return ICTSignal(signal="NO_TRADE", reason="Not enough execution bars")

    highs, lows = detect_swings(execution_bars, cfg)
    last = len(execution_bars) - 1
    # Entry is a bar after the gap prints, so the displacement is at least two bars back.
    newest_disp = last - 2
    oldest_disp = max(cfg.body_median_period, last - 1 - cfg.fvg_retrace_bars)
    if newest_disp < oldest_disp:
        return ICTSignal(signal="NO_TRADE", reason="No displacement", metadata={"bias": bias})

    waiting: ICTSignal | None = None
    fallback = ICTSignal(signal="NO_TRADE", reason="No liquidity sweep", metadata={"bias": bias})
    for disp in range(newest_disp, oldest_disp - 1, -1):
        if not _same_session(execution_bars[disp], execution_bars[last]):
            break
        if not _is_displacement(execution_bars, disp, bias, cfg):
            continue
        fvg = _fvg_at(execution_bars, disp, bias, cfg)
        if fvg is None:
            fallback = ICTSignal(signal="NO_TRADE", reason="No valid FVG", metadata={"bias": bias})
            continue
        if last > int(fvg["index"]) + cfg.fvg_retrace_bars:
            fallback = ICTSignal(signal="NO_TRADE", reason="FVG retrace window expired", metadata={"bias": bias})
            continue
        sweep_from = max(0, disp - cfg.sweep_lookback)
        sweep: dict[str, Any] | None = None
        for i in range(disp - 1, sweep_from - 1, -1):
            if not _same_session(execution_bars[i], execution_bars[disp]):
                break
            found = _sweep_at(execution_bars, i, bias, cfg, highs, lows)
            if found:
                sweep = found
                break
        if sweep is None:
            fallback = ICTSignal(signal="NO_TRADE", reason="No liquidity sweep", metadata={"bias": bias})
            continue
        if not _mss(execution_bars, disp, disp + 1, bias, highs, lows):
            fallback = ICTSignal(signal="NO_TRADE", reason="No MSS", metadata={"bias": bias})
            continue
        entry = _touch_entry(execution_bars[last], fvg, bias)
        if entry is None:
            waiting = ICTSignal(
                signal="WAIT",
                reason="Waiting for FVG retracement",
                metadata={"bias": bias, "fvg": fvg, "liquidity_sweep": sweep},
            )
            continue
        atr = atr_at(execution_bars, last, cfg.atr_period)
        if atr is None:
            fallback = ICTSignal(signal="NO_TRADE", reason="ATR unavailable")
            continue
        buffer = cfg.sl_atr_buffer * atr
        bar_low, bar_high = _f(execution_bars[last], "l"), _f(execution_bars[last], "h")
        if bias == "LONG":
            stop = min(float(sweep["extreme"]) - buffer, float(fvg["low"]) - buffer)
            if bar_low is not None and bar_low <= stop:
                fallback = ICTSignal(signal="NO_TRADE", reason="Entry bar already through stop")
                continue
            risk = entry - stop
            target = _target(
                execution_bars, entry, "LONG", highs, lows, risk=risk, minimum_rr=cfg.minimum_rr
            )
            if target is None:
                fallback = ICTSignal(signal="NO_TRADE", reason="Risk reward below minimum")
                continue
            reward = target - entry
        else:
            stop = max(float(sweep["extreme"]) + buffer, float(fvg["high"]) + buffer)
            if bar_high is not None and bar_high >= stop:
                fallback = ICTSignal(signal="NO_TRADE", reason="Entry bar already through stop")
                continue
            risk = stop - entry
            target = _target(
                execution_bars, entry, "SHORT", highs, lows, risk=risk, minimum_rr=cfg.minimum_rr
            )
            if target is None:
                fallback = ICTSignal(signal="NO_TRADE", reason="Risk reward below minimum")
                continue
            reward = entry - target
        if risk <= 0:
            fallback = ICTSignal(signal="NO_TRADE", reason="Invalid risk")
            continue
        rr = reward / risk
        if rr < cfg.minimum_rr:
            fallback = ICTSignal(
                signal="NO_TRADE",
                reason="Risk reward below minimum",
                risk_reward=round(rr, 3),
            )
            continue
        sweep_t = execution_bars[int(sweep["index"])].get("t")
        disp_t = execution_bars[disp].get("t")
        return ICTSignal(
            signal=bias,
            reason=f"ICT {bias.lower()} setup confirmed",
            entry=round(entry, 2),
            stop_loss=round(stop, 2),
            target=round(target, 2),
            risk_reward=round(rr, 3),
            confidence=80.0,
            metadata={
                "bias": bias,
                "liquidity_sweep": sweep,
                "fvg": fvg,
                "displacement_index": disp,
                "setup_id": f"{sweep_t}|{disp_t}",
                "bar": execution_bars[last].get("t"),
            },
        )
    return waiting or fallback
