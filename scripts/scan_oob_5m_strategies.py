#!/usr/bin/env python3
"""Out-of-box 5m strategy search — ignore prior books; maximize net profit.

Uses kite_5m_backtest_bars.json. Option P&L = delta0.5*spot + NFO charges + slip.
Includes pattern, seasonality, vol-regime, and grid-searched exits.
"""

from __future__ import annotations

import itertools
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402
from atlas_lite.metrics import wilder_dmi_series  # noqa: E402

QTY = 65
SLIP = 0.5
DELTA = 0.5
SQUARE = "15:14"


def _hm(ts: str) -> str:
    return str(ts).replace("T", " ")[11:16]


def _day(ts: str) -> str:
    return str(ts).replace("T", " ")[:10]


def _tue(d: str) -> bool:
    try:
        return date.fromisoformat(d).weekday() == 1
    except ValueError:
        return False


def _ema(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    k = 2 / (n + 1)
    prev = None
    seed: list[float] = []
    for i, v in enumerate(xs):
        if prev is None:
            seed.append(v)
            if len(seed) < n:
                continue
            prev = sum(seed) / n
            out[i] = prev
            continue
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def _sma(xs: list[float | None], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    buf: list[float] = []
    for i, v in enumerate(xs):
        if v is None:
            buf = []
            continue
        buf.append(float(v))
        if len(buf) > n:
            buf.pop(0)
        if len(buf) == n:
            out[i] = sum(buf) / n
    return out


def _rsi(c: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(c)
    if len(c) <= n:
        return out
    g = [max(c[i] - c[i - 1], 0) for i in range(1, n + 1)]
    l = [max(c[i - 1] - c[i], 0) for i in range(1, n + 1)]
    ag, al = sum(g) / n, sum(l) / n
    out[n] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def _atr(h, l, c, n=14):
    out = [None] * len(c)
    if len(c) < n + 1:
        return out
    tr = []
    for i in range(len(c)):
        tr.append(h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    p = sum(tr[1 : n + 1]) / n
    out[n] = p
    for i in range(n + 1, len(c)):
        p = (p * (n - 1) + tr[i]) / n
        out[i] = p
    return out


def _vwap(bars):
    out = [None] * len(bars)
    pv = vol = 0.0
    day = ""
    for i, b in enumerate(bars):
        d = _day(b["t"])
        if d != day:
            day, pv, vol = d, 0.0, 0.0
        v = float(b.get("v") or 0) or 1.0
        pv += ((b["h"] + b["l"] + b["c"]) / 3) * v
        vol += v
        out[i] = pv / vol
    return out


def _linreg_slope(c: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(c)
    for i in range(n - 1, len(c)):
        ys = c[i - n + 1 : i + 1]
        mx = (n - 1) / 2
        my = sum(ys) / n
        varx = sum((x - mx) ** 2 for x in range(n))
        if varx <= 0:
            continue
        cov = sum((x - mx) * (ys[x] - my) for x in range(n))
        out[i] = cov / varx
    return out


def _prem(spot, atr):
    return max(20.0, round(0.004 * spot + 0.35 * atr, 2))


def _ch(e, x):
    return float(kite_nfo_charges([(e, QTY, "buy"), (x, QTY, "sell")])["total"])


def simulate(
    bars,
    *,
    signal: Callable[[int], str | None],
    after="09:30",
    until="14:45",
    stop_atr=1.0,
    target_r=1.5,
    max_day=4,
    cooldown=3,
    skip_tue_pm=True,
    atr_list=None,
    sides="both",
    fresh=False,
    name="",
):
    trades = []
    day_n = defaultdict(int)
    cool = -1
    prev = None
    for i in range(len(bars) - 1):
        day = _day(bars[i]["t"])
        hm = _hm(bars[i]["t"])
        if hm < after or hm > until:
            prev = None
            continue
        if skip_tue_pm and _tue(day) and hm >= "13:00":
            continue
        if day_n[day] >= max_day or i < cool:
            continue
        a = atr_list[i] if atr_list else None
        if a is None or a <= 0:
            continue
        side = signal(i)
        if side is None:
            prev = None
            continue
        if sides == "ce" and side != "ce":
            continue
        if sides == "pe" and side != "pe":
            continue
        if fresh and side == prev:
            continue
        prev = side
        spot0 = bars[i]["c"]
        stop = spot0 - stop_atr * a if side == "ce" else spot0 + stop_atr * a
        tgt = spot0 + target_r * stop_atr * a if side == "ce" else spot0 - target_r * stop_atr * a
        eopt = _prem(spot0, a) + SLIP
        exit_spot, reason = spot0, "square_off"
        for j in range(i + 1, len(bars)):
            b = bars[j]
            if _day(b["t"]) != day:
                break
            hm2 = _hm(b["t"])
            if side == "ce":
                if b["l"] <= stop:
                    exit_spot, reason = stop, "stop"
                    break
                if b["h"] >= tgt:
                    exit_spot, reason = tgt, "target"
                    break
            else:
                if b["h"] >= stop:
                    exit_spot, reason = stop, "stop"
                    break
                if b["l"] <= tgt:
                    exit_spot, reason = tgt, "target"
                    break
            if hm2 >= SQUARE:
                exit_spot, reason = b["c"], "square_off"
                break
            exit_spot = b["c"]
        move = (exit_spot - spot0) if side == "ce" else (spot0 - exit_spot)
        xopt = max(0.05, eopt - SLIP + DELTA * move - SLIP)
        pnl = round((xopt - eopt) * QTY - _ch(eopt, xopt), 2)
        trades.append(pnl)
        day_n[day] += 1
        cool = i + cooldown
    if len(trades) < 10:
        return None
    wins = [p for p in trades if p > 0]
    losses = [p for p in trades if p <= 0]
    gl = abs(sum(losses)) or 1e-9
    return {
        "name": name,
        "n": len(trades),
        "wr": round(100 * len(wins) / len(trades), 1),
        "net": round(sum(trades), 2),
        "avg": round(sum(trades) / len(trades), 2),
        "pf": round(sum(wins) / gl, 2),
    }


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "kite_5m_backtest_bars.json"
    bars = json.loads(path.read_text(encoding="utf-8"))
    c = [b["c"] for b in bars]
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    v = [float(b.get("v") or 0) for b in bars]
    atr = _atr(h, l, c)
    rsi = _rsi(c)
    vwap = _vwap(bars)
    ema8 = _ema(c, 8)
    ema21 = _ema(c, 21)
    ema55 = _ema(c, 55)
    pdi, mdi, adx = wilder_dmi_series(h, l, c, 14)
    slope8 = _linreg_slope(c, 8)
    slope21 = _linreg_slope(c, 21)
    # rolling mean / z
    mean21 = _sma(c, 21)  # type: ignore
    # ATR percentile rank last 50
    atr_pct = [None] * len(c)
    for i in range(len(c)):
        if atr[i] is None:
            continue
        window = [float(atr[j]) for j in range(max(0, i - 50), i) if atr[j] is not None]
        if len(window) < 20:
            continue
        atr_pct[i] = sum(1 for x in window if x <= float(atr[i])) / len(window)

    # session features
    day_open = {}
    day_hi = {}
    day_lo = {}
    prev_close = {}
    last_day = None
    last_c = None
    for b in bars:
        d = _day(b["t"])
        if d != last_day:
            if last_day and last_c is not None:
                prev_close[d] = last_c  # will set wrong; fix below
            if d not in day_open and _hm(b["t"]) >= "09:15":
                day_open[d] = b["o"]
                day_hi[d] = b["h"]
                day_lo[d] = b["l"]
            last_day = d
        day_hi[d] = max(day_hi.get(d, b["h"]), b["h"])
        day_lo[d] = min(day_lo.get(d, b["l"]), b["l"])
        last_c = b["c"]
    # proper prev close
    closes_by_day = defaultdict(list)
    for b in bars:
        closes_by_day[_day(b["t"])].append(b["c"])
    days_sorted = sorted(closes_by_day)
    pc = {}
    for i, d in enumerate(days_sorted):
        if i:
            pc[d] = closes_by_day[days_sorted[i - 1]][-1]

    # ORB windows
    def orb(mins: int) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        buckets: dict[str, list] = defaultdict(list)
        start_m, end_m = 9 * 60 + 15, 9 * 60 + 15 + mins
        for b in bars:
            hh, mm = map(int, _hm(b["t"]).split(":"))
            m = hh * 60 + mm
            if start_m <= m < end_m:
                buckets[_day(b["t"])].append(b)
        for d, xs in buckets.items():
            if xs:
                out[d] = (max(x["h"] for x in xs), min(x["l"] for x in xs))
        return out

    orb15, orb30, orb45 = orb(15), orb(30), orb(45)

    # volume z
    vol_sma = []
    for i in range(len(v)):
        w = [x for x in v[max(0, i - 20) : i] if x > 0]
        vol_sma.append(sum(w) / len(w) if w else None)

    results = []

    def push(r):
        if r and r["n"] >= 12:
            results.append(r)

    # ========== 1) Clock seasonality: fixed half-hour PE/CE ==========
    for hh_start, hh_end, side in itertools.product(
        ["09:45", "10:00", "10:30", "11:00", "11:30", "12:00", "13:00", "13:30", "14:00"],
        ["10:15", "10:45", "11:15", "11:45", "12:30", "13:15", "13:45", "14:15", "14:45"],
        ["pe", "ce"],
    ):
        if hh_end <= hh_start:
            continue

        def sig(i, s=side, a0=hh_start, a1=hh_end):
            hm = _hm(bars[i]["t"])
            if a0 <= hm <= a1:
                return s
            return None

        for stop, tr in [(1.0, 1.5), (1.5, 2.0), (1.2, 1.0)]:
            push(
                simulate(
                    bars,
                    signal=sig,
                    after=hh_start,
                    until=hh_end,
                    stop_atr=stop,
                    target_r=tr,
                    max_day=2,
                    cooldown=6,
                    atr_list=atr,
                    sides=side,
                    fresh=True,
                    name=f"clock_{side}_{hh_start}-{hh_end}_s{stop}_r{tr}",
                )
            )

    # ========== 2) Wick rejection / pin ==========
    def pin_sig(i):
        if atr[i] is None or atr[i] <= 0:
            return None
        o, hh, ll, cc = bars[i]["o"], h[i], l[i], c[i]
        rng = hh - ll
        if rng < 0.6 * atr[i]:
            return None
        upper = hh - max(o, cc)
        lower = min(o, cc) - ll
        body = abs(cc - o)
        if lower >= 2.0 * body and lower >= 0.55 * rng and cc > o:
            return "ce"  # hammer → long CE
        if upper >= 2.0 * body and upper >= 0.55 * rng and cc < o:
            return "pe"
        return None

    for after, until, sides, stop, tr, adx_min in itertools.product(
        ["09:45", "10:00"],
        ["11:30", "12:30", "14:00"],
        ["both", "pe", "ce"],
        [1.0, 1.5],
        [1.5, 2.0],
        [0, 20, 25],
    ):

        def sig(i, amin=adx_min):
            if amin and (adx[i] is None or adx[i] < amin):
                return None
            return pin_sig(i)

        push(
            simulate(
                bars,
                signal=sig,
                after=after,
                until=until,
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                max_day=4,
                cooldown=4,
                name=f"pin_{sides}_{after}-{until}_s{stop}_r{tr}_adx{adx_min}",
            )
        )

    # ========== 3) Engulfing ==========
    def eng(i):
        if i < 1:
            return None
        o0, c0 = bars[i - 1]["o"], bars[i - 1]["c"]
        o1, c1 = bars[i]["o"], bars[i]["c"]
        if c1 > o1 and c0 < o0 and c1 >= o0 and o1 <= c0:
            return "ce"
        if c1 < o1 and c0 > o0 and c1 <= o0 and o1 >= c0:
            return "pe"
        return None

    for until, sides, stop, tr in itertools.product(
        ["11:30", "13:00", "14:30"], ["both", "pe"], [1.0, 1.5], [1.5, 2.0]
    ):
        push(
            simulate(
                bars,
                signal=eng,
                after="09:45",
                until=until,
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                name=f"engulf_{sides}_-{until}_s{stop}_r{tr}",
            )
        )

    # ========== 4) NR7 / compression break ==========
    ranges = [h[i] - l[i] for i in range(len(bars))]

    def nr_break(i, look=7):
        if i < look + 1 or atr[i] is None:
            return None
        # narrowest range of last look bars at i-1
        window = ranges[i - look : i]
        if ranges[i - 1] != min(window):
            return None
        # break
        if c[i] > h[i - 1] and (pdi[i] or 0) >= (mdi[i] or 0):
            return "ce"
        if c[i] < l[i - 1] and (mdi[i] or 0) >= (pdi[i] or 0):
            return "pe"
        return None

    for until, sides, stop, tr in itertools.product(
        ["11:30", "14:00"], ["both", "pe"], [1.2, 1.5], [2.0, 2.5]
    ):
        push(
            simulate(
                bars,
                signal=lambda i: nr_break(i),
                after="09:50",
                until=until,
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                max_day=3,
                name=f"nr7brk_{sides}_-{until}_s{stop}_r{tr}",
            )
        )

    # ========== 5) Z-score mean reversion vs momentum ==========
    def zsig(i, mode="fade", thr=1.5):
        if mean21[i] is None or atr[i] is None or atr[i] <= 0:
            return None
        z = (c[i] - mean21[i]) / atr[i]
        if mode == "fade":
            if z >= thr:
                return "pe"
            if z <= -thr:
                return "ce"
        else:
            if z >= thr and (pdi[i] or 0) > (mdi[i] or 0):
                return "ce"
            if z <= -thr and (mdi[i] or 0) > (pdi[i] or 0):
                return "pe"
        return None

    for mode, thr, until, sides, stop, tr in itertools.product(
        ["fade", "mom"],
        [1.0, 1.5, 2.0],
        ["11:30", "14:00"],
        ["both", "pe"],
        [1.0, 1.5],
        [1.0, 1.5, 2.0],
    ):
        push(
            simulate(
                bars,
                signal=lambda i, m=mode, t=thr: zsig(i, m, t),
                after="09:45",
                until=until,
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                name=f"z{mode}_{thr}_{sides}_-{until}_s{stop}_r{tr}",
            )
        )

    # ========== 6) Slope regime + pullback ==========
    def slope_pb(i):
        if None in (slope21[i], ema21[i], atr[i]):
            return None
        if slope21[i] > 0.5 and l[i] <= ema21[i] <= h[i] and c[i] > ema21[i]:
            return "ce"
        if slope21[i] < -0.5 and l[i] <= ema21[i] <= h[i] and c[i] < ema21[i]:
            return "pe"
        return None

    for until, sides, stop, tr in itertools.product(
        ["11:30", "13:00"], ["both", "pe"], [1.0, 1.5], [1.5, 2.0]
    ):
        push(
            simulate(
                bars,
                signal=slope_pb,
                after="09:45",
                until=until,
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                name=f"slope_pb_{sides}_-{until}_s{stop}_r{tr}",
            )
        )

    # ========== 7) ORB variants ==========
    for orbmap, tag, until, sides, stop, tr in itertools.product(
        [(orb15, "15"), (orb30, "30"), (orb45, "45")],
        ["x"],
        ["11:00", "11:30", "12:30"],
        ["both", "pe", "ce"],
        [1.0, 1.5, 2.0],
        [1.5, 2.0, 2.5],
    ):
        om, ot = orbmap

        def sig(i, om=om):
            d = _day(bars[i]["t"])
            if d not in om:
                return None
            hi, lo = om[d]
            if c[i] > hi:
                return "ce"
            if c[i] < lo:
                return "pe"
            return None

        push(
            simulate(
                bars,
                signal=sig,
                after="09:35" if ot != "45" else "10:05",
                until=until,
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                max_day=2,
                cooldown=8,
                name=f"orb{ot}_{sides}_-{until}_s{stop}_r{tr}",
            )
        )

    # ========== 8) Gap fade / gap go ==========
    def gap(i, mode="fade"):
        d = _day(bars[i]["t"])
        if d not in pc or d not in day_open or atr[i] is None:
            return None
        if _hm(bars[i]["t"]) > "10:30":
            return None
        gap_pts = day_open[d] - pc[d]
        if atr[i] <= 0:
            return None
        g = gap_pts / atr[i]
        if mode == "fade":
            if g >= 1.0 and c[i] < day_open[d]:
                return "pe"
            if g <= -1.0 and c[i] > day_open[d]:
                return "ce"
        else:
            if g >= 1.0 and c[i] > day_open[d] and c[i] > (vwap[i] or c[i]):
                return "ce"
            if g <= -1.0 and c[i] < day_open[d] and c[i] < (vwap[i] or c[i]):
                return "pe"
        return None

    for mode, sides, stop, tr in itertools.product(
        ["fade", "go"], ["both", "pe"], [1.0, 1.5], [1.5, 2.0]
    ):
        push(
            simulate(
                bars,
                signal=lambda i, m=mode: gap(i, m),
                after="09:30",
                until="11:00",
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                max_day=2,
                name=f"gap_{mode}_{sides}_s{stop}_r{tr}",
            )
        )

    # ========== 9) Vol climax reverse ==========
    def climax(i):
        if i < 3 or vol_sma[i] is None or atr[i] is None:
            return None
        if v[i] < 2.0 * vol_sma[i]:
            return None
        # big range bar
        if ranges[i] < 1.3 * atr[i]:
            return None
        # reverse next direction of close vs open
        if c[i] > bars[i]["o"] and rsi[i] and rsi[i] >= 68:
            return "pe"
        if c[i] < bars[i]["o"] and rsi[i] and rsi[i] <= 32:
            return "ce"
        return None

    for until, sides, stop, tr in itertools.product(
        ["11:30", "14:00"], ["both", "pe"], [1.2, 1.5], [1.2, 1.8]
    ):
        push(
            simulate(
                bars,
                signal=climax,
                after="09:45",
                until=until,
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                name=f"climax_{sides}_-{until}_s{stop}_r{tr}",
            )
        )

    # ========== 10) Low-vol only / high-vol only ==========
    def ema_cross(i, vol_mode="low"):
        if i < 1 or None in (ema8[i], ema21[i], ema8[i - 1], ema21[i - 1], atr_pct[i]):
            return None
        if vol_mode == "low" and atr_pct[i] > 0.4:
            return None
        if vol_mode == "high" and atr_pct[i] < 0.6:
            return None
        if ema8[i] > ema21[i] and ema8[i - 1] <= ema21[i - 1]:
            return "ce"
        if ema8[i] < ema21[i] and ema8[i - 1] >= ema21[i - 1]:
            return "pe"
        return None

    for vm, until, sides, stop, tr in itertools.product(
        ["low", "high"], ["11:30", "14:00"], ["both", "pe"], [1.0, 1.5], [1.5, 2.0]
    ):
        push(
            simulate(
                bars,
                signal=lambda i, m=vm: ema_cross(i, m),
                after="09:45",
                until=until,
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                name=f"ema8_21_{vm}vol_{sides}_-{until}_s{stop}_r{tr}",
            )
        )

    # ========== 11) Consecutive bars ==========
    def consec(i, n=3, mode="cont"):
        if i < n:
            return None
        ups = all(c[i - k] > bars[i - k]["o"] for k in range(n))
        dns = all(c[i - k] < bars[i - k]["o"] for k in range(n))
        if mode == "cont":
            if ups:
                return "ce"
            if dns:
                return "pe"
        else:  # reverse
            if ups and rsi[i] and rsi[i] > 65:
                return "pe"
            if dns and rsi[i] and rsi[i] < 35:
                return "ce"
        return None

    for nbar, mode, until, sides, stop, tr in itertools.product(
        [3, 4], ["cont", "rev"], ["11:30", "14:00"], ["both", "pe"], [1.0, 1.5], [1.5, 2.0]
    ):
        push(
            simulate(
                bars,
                signal=lambda i, n=nbar, m=mode: consec(i, n, m),
                after="09:45",
                until=until,
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                name=f"consec{nbar}_{mode}_{sides}_-{until}_s{stop}_r{tr}",
            )
        )

    # ========== 12) VWAP distance bands (fresh ideas) ==========
    def vwap_band(i, thr=1.0, mode="fade"):
        if vwap[i] is None or atr[i] is None or atr[i] <= 0:
            return None
        d = (c[i] - vwap[i]) / atr[i]
        if mode == "fade":
            if d > thr:
                return "pe"
            if d < -thr:
                return "ce"
        else:
            if d > thr:
                return "ce"
            if d < -thr:
                return "pe"
        return None

    for mode, thr, until, sides, stop, tr in itertools.product(
        ["fade", "ride"],
        [0.8, 1.2, 1.8],
        ["11:15", "12:00", "14:00"],
        ["pe", "both"],
        [1.0, 1.5, 2.0],
        [1.0, 1.5, 2.0],
    ):
        push(
            simulate(
                bars,
                signal=lambda i, m=mode, t=thr: vwap_band(i, t, m),
                after="10:00",
                until=until,
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                max_day=3,
                name=f"vwap_{mode}_{thr}_{sides}_-{until}_s{stop}_r{tr}",
            )
        )

    # ========== 13) Inside-bar break ==========
    def inside_brk(i):
        if i < 2:
            return None
        # bar i-1 inside i-2
        if not (h[i - 1] <= h[i - 2] and l[i - 1] >= l[i - 2]):
            return None
        if c[i] > h[i - 1]:
            return "ce"
        if c[i] < l[i - 1]:
            return "pe"
        return None

    for until, sides, stop, tr in itertools.product(
        ["11:30", "14:00"], ["both", "pe"], [1.0, 1.5], [1.5, 2.0]
    ):
        push(
            simulate(
                bars,
                signal=inside_brk,
                after="09:45",
                until=until,
                stop_atr=stop,
                target_r=tr,
                atr_list=atr,
                sides=sides,
                fresh=True,
                name=f"insidebrk_{sides}_-{until}_s{stop}_r{tr}",
            )
        )

    # rank
    results.sort(key=lambda r: (r["net"], r["pf"], r["wr"]), reverse=True)
    # filter absurd overfit: n between 12 and 120, and not pure lottery
    sane = [r for r in results if 12 <= r["n"] <= 100]
    print(f"bars={len(bars)} trials_kept={len(results)} sane_n12_100={len(sane)}")
    print(f"\n{'rk':>3} {'net':>9} {'wr':>6} {'n':>4} {'pf':>5} {'avg':>7}  name")
    for i, r in enumerate(sane[:25], 1):
        print(
            f"{i:3} {r['net']:+9.0f} {r['wr']:6.1f} {r['n']:4} {r['pf']:5.2f} {r['avg']:+7.0f}  {r['name']}"
        )
    print("\n=== TOP OVERALL (any n>=12) ===")
    for i, r in enumerate(results[:10], 1):
        print(f"{i:3} {r['net']:+9.0f} wr={r['wr']} n={r['n']} pf={r['pf']}  {r['name']}")

    best = sane[0] if sane else (results[0] if results else None)
    print("\n=== RECOMMENDED (profit priority, n=12..100) ===")
    print(json.dumps(best, indent=2))

    # save top 50
    out = ROOT / "data" / "oob_5m_scan_results.json"
    try:
        out.write_text(json.dumps({"top": sane[:50], "best": best}, indent=2), encoding="utf-8")
        print("saved", out)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
