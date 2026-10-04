#!/usr/bin/env python3
"""Aggressive 5m hunter — walk-forward, trail/BE exits, new families, book stack.

Ignores prior books. Ranks by out-of-sample net (second half of days).
Option P&L ≈ delta0.5 * spot move + NFO + slip (same proxy as other scans).
"""

from __future__ import annotations

import itertools
import json
import math
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


def _wd(d: str) -> int:
    try:
        return date.fromisoformat(d).weekday()
    except ValueError:
        return -1


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


def _sma(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    buf: list[float] = []
    for i, v in enumerate(xs):
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
        tr.append(
            h[i] - l[i]
            if i == 0
            else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        )
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


def _bb(c: list[float], n: int = 20, k: float = 2.0):
    mid = _sma(c, n)
    up: list[float | None] = [None] * len(c)
    lo: list[float | None] = [None] * len(c)
    width: list[float | None] = [None] * len(c)
    for i in range(len(c)):
        if mid[i] is None or i < n - 1:
            continue
        window = c[i - n + 1 : i + 1]
        mu = mid[i]
        var = sum((x - mu) ** 2 for x in window) / n
        sd = math.sqrt(var)
        up[i] = mu + k * sd
        lo[i] = mu - k * sd
        width[i] = (2 * k * sd) / mu if mu else None
    return mid, up, lo, width


def _prem(spot, atr):
    return max(20.0, round(0.004 * spot + 0.35 * atr, 2))


def _ch(e, x):
    return float(kite_nfo_charges([(e, QTY, "buy"), (x, QTY, "sell")])["total"])


def _stats(trades: list[float], name: str) -> dict[str, Any] | None:
    if len(trades) < 8:
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


def simulate(
    bars,
    *,
    signal: Callable[[int], str | None],
    atr_list,
    after="09:30",
    until="14:45",
    stop_atr=1.0,
    target_r=1.5,
    trail_atr: float | None = None,
    be_at_r: float | None = None,
    time_bars: int | None = None,
    max_day=4,
    cooldown=3,
    skip_wd: set[int] | None = None,
    skip_tue_pm=True,
    sides="both",
    fresh=False,
    allow_days: set[str] | None = None,
    name="",
):
    trades: list[float] = []
    day_n: dict[str, int] = defaultdict(int)
    cool = -1
    prev = None
    for i in range(len(bars) - 1):
        day = _day(bars[i]["t"])
        if allow_days is not None and day not in allow_days:
            continue
        hm = _hm(bars[i]["t"])
        if hm < after or hm > until:
            prev = None
            continue
        wd = _wd(day)
        if skip_wd and wd in skip_wd:
            continue
        if skip_tue_pm and wd == 1 and hm >= "13:00":
            continue
        if day_n[day] >= max_day or i < cool:
            continue
        a = atr_list[i]
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
        risk = stop_atr * a
        stop = spot0 - risk if side == "ce" else spot0 + risk
        tgt = (
            spot0 + target_r * risk
            if side == "ce" and target_r
            else spot0 - target_r * risk
            if side == "pe" and target_r
            else None
        )
        be_level = None
        if be_at_r:
            be_level = (
                spot0 + be_at_r * risk if side == "ce" else spot0 - be_at_r * risk
            )
        eopt = _prem(spot0, a) + SLIP
        exit_spot, reason = spot0, "square_off"
        peak = spot0
        for j in range(i + 1, len(bars)):
            b = bars[j]
            if _day(b["t"]) != day:
                break
            hm2 = _hm(b["t"])
            if time_bars is not None and (j - i) >= time_bars:
                exit_spot, reason = b["c"], "time"
                break
            if side == "ce":
                peak = max(peak, b["h"])
                if be_level is not None and peak >= be_level and stop < spot0:
                    stop = spot0  # break-even
                if trail_atr:
                    stop = max(stop, peak - trail_atr * a)
                if b["l"] <= stop:
                    exit_spot, reason = stop, "stop"
                    break
                if tgt is not None and b["h"] >= tgt:
                    exit_spot, reason = tgt, "target"
                    break
            else:
                peak = min(peak, b["l"])
                if be_level is not None and peak <= be_level and stop > spot0:
                    stop = spot0
                if trail_atr:
                    stop = min(stop, peak + trail_atr * a)
                if b["h"] >= stop:
                    exit_spot, reason = stop, "stop"
                    break
                if tgt is not None and b["l"] <= tgt:
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
    return _stats(trades, name)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "kite_5m_backtest_bars.json"
    bars = json.loads(path.read_text(encoding="utf-8"))
    c = [b["c"] for b in bars]
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    o = [b["o"] for b in bars]
    v = [float(b.get("v") or 0) for b in bars]
    atr = _atr(h, l, c)
    rsi = _rsi(c)
    vwap = _vwap(bars)
    ema8 = _ema(c, 8)
    ema21 = _ema(c, 21)
    ema55 = _ema(c, 55)
    pdi, mdi, adx = wilder_dmi_series(h, l, c, 14)
    bb_mid, bb_up, bb_lo, bb_w = _bb(c, 20, 2.0)
    ranges = [h[i] - l[i] for i in range(len(bars))]

    # day features
    closes_by_day: dict[str, list[float]] = defaultdict(list)
    day_open: dict[str, float] = {}
    day_bars_idx: dict[str, list[int]] = defaultdict(list)
    for i, b in enumerate(bars):
        d = _day(b["t"])
        closes_by_day[d].append(b["c"])
        day_bars_idx[d].append(i)
        if d not in day_open and _hm(b["t"]) >= "09:15":
            day_open[d] = b["o"]
    days_sorted = sorted(closes_by_day)
    pc = {days_sorted[i]: closes_by_day[days_sorted[i - 1]][-1] for i in range(1, len(days_sorted))}

    # walk-forward split by day
    split = max(12, int(len(days_sorted) * 0.55))
    is_days = set(days_sorted[:split])
    oos_days = set(days_sorted[split:])
    print(f"bars={len(bars)} days={len(days_sorted)} IS={len(is_days)} OOS={len(oos_days)}")
    print(f"IS {days_sorted[0]}..{days_sorted[split-1]} | OOS {days_sorted[split]}..{days_sorted[-1]}")

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

    # first bar direction / body
    first_bar = {}
    for d, idxs in day_bars_idx.items():
        for i in idxs:
            if _hm(bars[i]["t"]) == "09:15":
                first_bar[d] = bars[i]
                break

    # rolling vol SMA
    vol_sma = []
    for i in range(len(v)):
        w = [x for x in v[max(0, i - 20) : i] if x > 0]
        vol_sma.append(sum(w) / len(w) if w else None)

    # ATR percentile
    atr_pct = [None] * len(c)
    for i in range(len(c)):
        if atr[i] is None:
            continue
        window = [float(atr[j]) for j in range(max(0, i - 50), i) if atr[j] is not None]
        if len(window) < 20:
            continue
        atr_pct[i] = sum(1 for x in window if x <= float(atr[i])) / len(window)

    # BB width percentile (squeeze)
    bbw_pct = [None] * len(c)
    for i in range(len(c)):
        if bb_w[i] is None:
            continue
        window = [float(bb_w[j]) for j in range(max(0, i - 50), i) if bb_w[j] is not None]
        if len(window) < 20:
            continue
        bbw_pct[i] = sum(1 for x in window if x <= float(bb_w[i])) / len(window)

    # session VWAP reclaim helpers: crossed vwap this bar
    def crossed_vwap_up(i):
        if i < 1 or vwap[i] is None or vwap[i - 1] is None:
            return False
        return c[i - 1] < vwap[i - 1] and c[i] >= vwap[i]

    def crossed_vwap_dn(i):
        if i < 1 or vwap[i] is None or vwap[i - 1] is None:
            return False
        return c[i - 1] > vwap[i - 1] and c[i] <= vwap[i]

    exit_grid = [
        # stop, target_r, trail, be_at_r, time_bars
        (1.0, 1.5, None, None, None),
        (1.5, 1.5, None, None, None),
        (1.5, 2.5, None, None, None),
        (1.2, 2.0, None, None, None),
        (1.0, 3.0, None, None, None),
        (1.5, 0.0, 1.0, None, None),  # trail only
        (1.2, 0.0, 0.8, 1.0, None),  # trail + BE
        (1.0, 2.0, None, 1.0, None),  # BE then target
        (1.5, 2.0, None, None, 6),  # time stop 30m
        (1.0, 1.5, None, None, 4),
        (2.0, 3.0, None, None, None),
    ]

    candidates: list[dict] = []

    def run_cfg(sig, *, after, until, sides, max_day, cooldown, fresh, tag, skip_wd=None):
        for stop, tr, trail, be, tbar in exit_grid:
            # target_r=0 means no hard target
            tr_use = tr if tr > 0 else 99.0
            name = (
                f"{tag}_{sides}_{after}-{until}_s{stop}"
                f"{'_r'+str(tr) if tr else '_trail'+str(trail)}"
                f"{'_be'+str(be) if be else ''}"
                f"{'_t'+str(tbar) if tbar else ''}"
            )
            common = dict(
                signal=sig,
                atr_list=atr,
                after=after,
                until=until,
                stop_atr=stop,
                target_r=tr_use if tr > 0 else 99.0,
                trail_atr=trail,
                be_at_r=be,
                time_bars=tbar,
                max_day=max_day,
                cooldown=cooldown,
                skip_wd=skip_wd,
                sides=sides,
                fresh=fresh,
            )
            # disable hard target when trail-only
            if tr <= 0:
                common["target_r"] = 99.0
            ris = simulate(bars, allow_days=is_days, name=name, **common)
            roos = simulate(bars, allow_days=oos_days, name=name, **common)
            rfull = simulate(bars, allow_days=None, name=name, **common)
            if not (ris and roos and rfull):
                continue
            if ris["net"] <= 0 or roos["net"] <= 0:
                continue
            if ris["n"] < 8 or roos["n"] < 6:
                continue
            candidates.append(
                {
                    "name": name,
                    "is_net": ris["net"],
                    "oos_net": roos["net"],
                    "full_net": rfull["net"],
                    "is_wr": ris["wr"],
                    "oos_wr": roos["wr"],
                    "full_wr": rfull["wr"],
                    "is_n": ris["n"],
                    "oos_n": roos["n"],
                    "full_n": rfull["n"],
                    "is_pf": ris["pf"],
                    "oos_pf": roos["pf"],
                    "full_pf": rfull["pf"],
                    "full_avg": rfull["avg"],
                    "score": round(roos["net"] + 0.35 * ris["net"], 2),
                }
            )

    # ===== NEW FAMILIES =====

    # A) VWAP reclaim (not distance fade)
    def vwap_reclaim(i):
        if atr[i] is None:
            return None
        if crossed_vwap_up(i) and (rsi[i] or 50) < 60:
            return "ce"
        if crossed_vwap_dn(i) and (rsi[i] or 50) > 40:
            return "pe"
        return None

    for until, sides in itertools.product(["11:15", "12:30", "14:00"], ["both", "pe", "ce"]):
        run_cfg(
            vwap_reclaim,
            after="09:45",
            until=until,
            sides=sides,
            max_day=3,
            cooldown=4,
            fresh=True,
            tag="vwap_reclaim",
        )

    # B) BB squeeze break (width pctile low then break)
    def squeeze_brk(i, look=3):
        if i < look or bbw_pct[i - 1] is None or bb_up[i] is None:
            return None
        if bbw_pct[i - 1] > 0.25:
            return None
        if c[i] > bb_up[i] and (pdi[i] or 0) >= (mdi[i] or 0):
            return "ce"
        if c[i] < bb_lo[i] and (mdi[i] or 0) >= (pdi[i] or 0):
            return "pe"
        return None

    for until, sides in itertools.product(["11:30", "13:00", "14:30"], ["both", "pe"]):
        run_cfg(
            squeeze_brk,
            after="09:50",
            until=until,
            sides=sides,
            max_day=2,
            cooldown=6,
            fresh=True,
            tag="bb_squeeze",
        )

    # C) Failed ORB (break then back inside → fade)
    def failed_orb(i, om):
        d = _day(bars[i]["t"])
        if d not in om or i < 2:
            return None
        hi, lo = om[d]
        # prior bar broke, this bar closes back inside
        if h[i - 1] > hi and c[i] < hi and c[i] > lo:
            return "pe"
        if l[i - 1] < lo and c[i] > lo and c[i] < hi:
            return "ce"
        return None

    for om, tag, until, sides in itertools.product(
        [(orb15, "15"), (orb30, "30"), (orb45, "45")],
        ["x"],
        ["11:30", "12:30", "14:00"],
        ["both", "pe"],
    ):
        omap, ot = om
        run_cfg(
            lambda i, m=omap: failed_orb(i, m),
            after="09:40" if ot != "45" else "10:05",
            until=until,
            sides=sides,
            max_day=2,
            cooldown=5,
            fresh=True,
            tag=f"failorb{ot}",
        )

    # D) First-bar reverse (fade 09:15 impulse after 09:30)
    def first_fade(i):
        d = _day(bars[i]["t"])
        if d not in first_bar or atr[i] is None:
            return None
        fb = first_bar[d]
        body = fb["c"] - fb["o"]
        if abs(body) < 0.4 * atr[i]:
            return None
        # only first 45m after open
        if _hm(bars[i]["t"]) > "10:15":
            return None
        if body > 0 and c[i] < fb["c"] and c[i] < (vwap[i] or c[i]):
            return "pe"
        if body < 0 and c[i] > fb["c"] and c[i] > (vwap[i] or c[i]):
            return "ce"
        return None

    for sides in ["both", "pe", "ce"]:
        run_cfg(
            first_fade,
            after="09:30",
            until="10:15",
            sides=sides,
            max_day=1,
            cooldown=8,
            fresh=True,
            tag="firstbar_fade",
        )

    # E) EMA55 trend pullback (higher TF proxy)
    def ema55_pb(i):
        if None in (ema55[i], ema21[i], atr[i], adx[i]):
            return None
        if adx[i] < 18:
            return None
        if ema21[i] > ema55[i] and l[i] <= ema21[i] <= h[i] and c[i] > ema21[i]:
            return "ce"
        if ema21[i] < ema55[i] and l[i] <= ema21[i] <= h[i] and c[i] < ema21[i]:
            return "pe"
        return None

    for until, sides in itertools.product(["11:30", "13:30", "14:30"], ["both", "pe", "ce"]):
        run_cfg(
            ema55_pb,
            after="09:45",
            until=until,
            sides=sides,
            max_day=3,
            cooldown=4,
            fresh=True,
            tag="ema55_pb",
        )

    # F) RSI failure swing
    def rsi_fail(i):
        if i < 5 or None in (rsi[i], rsi[i - 2], atr[i]):
            return None
        # bullish: rsi made lower low then turned up while price higher low
        if rsi[i - 2] < 30 and rsi[i] > rsi[i - 1] > rsi[i - 2] and l[i] > l[i - 2]:
            return "ce"
        if rsi[i - 2] > 70 and rsi[i] < rsi[i - 1] < rsi[i - 2] and h[i] < h[i - 2]:
            return "pe"
        return None

    for until, sides in itertools.product(["11:30", "14:00"], ["both", "pe"]):
        run_cfg(
            rsi_fail,
            after="09:45",
            until=until,
            sides=sides,
            max_day=3,
            cooldown=5,
            fresh=True,
            tag="rsi_fail",
        )

    # G) High-volume rejection (wick + vol)
    def vol_reject(i):
        if vol_sma[i] is None or atr[i] is None or atr[i] <= 0:
            return None
        if v[i] < 1.8 * vol_sma[i]:
            return None
        rng = ranges[i]
        if rng < 0.8 * atr[i]:
            return None
        upper = h[i] - max(o[i], c[i])
        lower = min(o[i], c[i]) - l[i]
        body = abs(c[i] - o[i]) or 1e-9
        if lower >= 1.8 * body and lower >= 0.5 * rng:
            return "ce"
        if upper >= 1.8 * body and upper >= 0.5 * rng:
            return "pe"
        return None

    for until, sides in itertools.product(["11:15", "12:30", "14:00"], ["both", "pe"]):
        run_cfg(
            vol_reject,
            after="09:40",
            until=until,
            sides=sides,
            max_day=3,
            cooldown=4,
            fresh=True,
            tag="vol_reject",
        )

    # H) Midday mean-revert to open (range day: ADX low)
    def midday_open(i):
        d = _day(bars[i]["t"])
        if d not in day_open or atr[i] is None or adx[i] is None:
            return None
        if adx[i] > 22:
            return None
        dist = (c[i] - day_open[d]) / atr[i]
        if dist >= 1.2:
            return "pe"
        if dist <= -1.2:
            return "ce"
        return None

    for sides in ["both", "pe"]:
        run_cfg(
            midday_open,
            after="11:00",
            until="13:30",
            sides=sides,
            max_day=2,
            cooldown=6,
            fresh=True,
            tag="midday_open",
        )

    # I) Gap + ORB combo
    def gap_orb(i, om, mode="fade"):
        d = _day(bars[i]["t"])
        if d not in pc or d not in day_open or d not in om or atr[i] is None:
            return None
        gap = (day_open[d] - pc[d]) / atr[i]
        hi, lo = om[d]
        if mode == "fade":
            if gap >= 0.8 and c[i] < lo:
                return "pe"  # gap up failed
            if gap <= -0.8 and c[i] > hi:
                return "ce"
        else:
            if gap >= 0.8 and c[i] > hi:
                return "ce"
            if gap <= -0.8 and c[i] < lo:
                return "pe"
        return None

    for om, ot, mode, sides in itertools.product(
        [(orb15, "15"), (orb30, "30")],
        ["x"],
        ["fade", "go"],
        ["both", "pe"],
    ):
        omap, _ = om
        run_cfg(
            lambda i, m=omap, md=mode: gap_orb(i, m, md),
            after="09:35",
            until="11:30",
            sides=sides,
            max_day=2,
            cooldown=8,
            fresh=True,
            tag=f"gaporb{ot}_{mode}",
        )

    # J) Triple-tap / equal highs fade
    def equal_hilo(i, thr=0.15):
        if i < 8 or atr[i] is None:
            return None
        # recent swing high within thr*ATR touched again
        win_h = max(h[i - 8 : i])
        win_l = min(l[i - 8 : i])
        if abs(h[i] - win_h) <= thr * atr[i] and c[i] < o[i] and (rsi[i] or 50) > 60:
            return "pe"
        if abs(l[i] - win_l) <= thr * atr[i] and c[i] > o[i] and (rsi[i] or 50) < 40:
            return "ce"
        return None

    for until, sides in itertools.product(["11:30", "14:00"], ["both", "pe"]):
        run_cfg(
            equal_hilo,
            after="10:00",
            until=until,
            sides=sides,
            max_day=3,
            cooldown=5,
            fresh=True,
            tag="equal_hl",
        )

    # K) Low ADX + VWAP band fade (range toolkit)
    def range_vwap_fade(i, thr=1.0):
        if None in (vwap[i], atr[i], adx[i]) or atr[i] <= 0:
            return None
        if adx[i] > 20:
            return None
        d = (c[i] - vwap[i]) / atr[i]
        if d >= thr:
            return "pe"
        if d <= -thr:
            return "ce"
        return None

    for thr, until, sides in itertools.product(
        [0.7, 1.0, 1.4], ["11:15", "12:30", "14:00"], ["both", "pe"]
    ):
        run_cfg(
            lambda i, t=thr: range_vwap_fade(i, t),
            after="10:00",
            until=until,
            sides=sides,
            max_day=3,
            cooldown=3,
            fresh=True,
            tag=f"range_vwap_{thr}",
        )

    # L) Momentum burst then continuation (skip first 15m)
    def burst_cont(i):
        if i < 2 or atr[i] is None:
            return None
        if ranges[i - 1] < 1.2 * atr[i]:
            return None
        if c[i - 1] > o[i - 1] and c[i] > h[i - 1]:
            return "ce"
        if c[i - 1] < o[i - 1] and c[i] < l[i - 1]:
            return "pe"
        return None

    for until, sides in itertools.product(["11:00", "12:00", "14:00"], ["both", "pe", "ce"]):
        run_cfg(
            burst_cont,
            after="09:35",
            until=until,
            sides=sides,
            max_day=3,
            cooldown=5,
            fresh=True,
            tag="burst_cont",
        )

    # M) Weekday-specific PE morning (calendar edge)
    for wd_skip, tag in [
        ({0, 1, 3, 4}, "wed_only"),  # only Wednesday = skip others
        ({0, 2, 3, 4}, "tue_only"),
        ({1, 2, 3, 4}, "mon_only"),
        ({0, 1, 2, 4}, "thu_only"),
    ]:

        def pe_morn(i):
            return "pe"

        run_cfg(
            pe_morn,
            after="10:00",
            until="11:15",
            sides="pe",
            max_day=1,
            cooldown=12,
            fresh=True,
            tag=f"clock_pe_{tag}",
            skip_wd=wd_skip,
        )

    # N) Classic ORB with trail exits (revisit with new exits)
    for om, ot, sides in itertools.product(
        [(orb30, "30"), (orb45, "45")],
        ["x"],
        ["both", "pe", "ce"],
    ):
        omap, _ = om

        def sig(i, m=omap):
            d = _day(bars[i]["t"])
            if d not in m:
                return None
            hi, lo = m[d]
            if c[i] > hi:
                return "ce"
            if c[i] < lo:
                return "pe"
            return None

        run_cfg(
            sig,
            after="09:50" if ot == "30" else "10:05",
            until="12:30",
            sides=sides,
            max_day=2,
            cooldown=8,
            fresh=True,
            tag=f"orb{ot}",
        )

    # rank by OOS-heavy score
    candidates.sort(key=lambda r: (r["oos_net"], r["oos_pf"], r["full_net"]), reverse=True)
    print(f"\nOOS-validated candidates: {len(candidates)}")
    print(
        f"{'rk':>3} {'oos':>8} {'is':>8} {'full':>8} {'oosWR':>6} {'oosN':>4} {'oosPF':>5}  name"
    )
    for i, r in enumerate(candidates[:30], 1):
        print(
            f"{i:3} {r['oos_net']:+8.0f} {r['is_net']:+8.0f} {r['full_net']:+8.0f} "
            f"{r['oos_wr']:6.1f} {r['oos_n']:4} {r['oos_pf']:5.2f}  {r['name']}"
        )

    # portfolio: greedily stack top names with different family prefixes
    portfolio = []
    used_fam = set()
    for r in candidates:
        fam = r["name"].split("_")[0]
        # allow range_vwap / failorb variants as distinct if prefix differs
        key = "_".join(r["name"].split("_")[:2])
        if key in used_fam:
            continue
        if r["oos_pf"] < 1.15:
            continue
        used_fam.add(key)
        portfolio.append(r)
        if len(portfolio) >= 4:
            break

    port_oos = sum(x["oos_net"] for x in portfolio)
    port_full = sum(x["full_net"] for x in portfolio)
    print("\n=== STACKED BOOK (distinct families, OOS-validated) ===")
    for r in portfolio:
        print(
            f"  {r['full_net']:+.0f} full | {r['oos_net']:+.0f} oos | pf={r['oos_pf']} | {r['name']}"
        )
    print(f"STACK OOS≈{port_oos:+.0f}  FULL≈{port_full:+.0f}  (independent sum; overlap unadjusted)")

    best = candidates[0] if candidates else None
    print("\n=== BEST SINGLE (OOS net) ===")
    print(json.dumps(best, indent=2))

    out = ROOT / "data" / "hunter_5m_results.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "split": {
                        "is_days": sorted(is_days),
                        "oos_days": sorted(oos_days),
                    },
                    "top": candidates[:40],
                    "best": best,
                    "portfolio": portfolio,
                    "portfolio_oos": port_oos,
                    "portfolio_full": port_full,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print("saved", out)
    except OSError as e:
        print("save_fail", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
