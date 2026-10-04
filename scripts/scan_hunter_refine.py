#!/usr/bin/env python3
"""Fine-grid around failed-ORB / range-VWAP + short-premium range book.

Walk-forward same split as scan_hunter_5m. Rank by robust = min(IS,OOS).
"""

from __future__ import annotations

import itertools
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

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


def _prem(spot, atr):
    return max(20.0, round(0.004 * spot + 0.35 * atr, 2))


def _ch_long(e, x):
    return float(kite_nfo_charges([(e, QTY, "buy"), (x, QTY, "sell")])["total"])


def _ch_short_straddle(credit, debit):
    # sell CE+PE, buy back
    return float(
        kite_nfo_charges(
            [
                (credit, QTY, "sell"),
                (credit, QTY, "sell"),
                (debit, QTY, "buy"),
                (debit, QTY, "buy"),
            ]
        )["total"]
    )


def _stats(trades, name):
    if len(trades) < 6:
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


def sim_long(
    bars,
    atr,
    signal,
    *,
    after,
    until,
    stop_atr,
    target_r,
    trail_atr=None,
    be_at_r=None,
    max_day=2,
    cooldown=5,
    sides="pe",
    fresh=True,
    allow_days=None,
    name="",
):
    trades = []
    day_n = defaultdict(int)
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
        if day_n[day] >= max_day or i < cool:
            continue
        a = atr[i]
        if a is None or a <= 0:
            continue
        side = signal(i)
        if side is None:
            prev = None
            continue
        if sides != "both" and side != sides:
            continue
        if fresh and side == prev:
            continue
        prev = side
        spot0 = bars[i]["c"]
        risk = stop_atr * a
        stop = spot0 - risk if side == "ce" else spot0 + risk
        tgt = spot0 + target_r * risk if side == "ce" else spot0 - target_r * risk
        be_level = spot0 + be_at_r * risk if (be_at_r and side == "ce") else (
            spot0 - be_at_r * risk if be_at_r else None
        )
        eopt = _prem(spot0, a) + SLIP
        exit_spot = spot0
        peak = spot0
        for j in range(i + 1, len(bars)):
            b = bars[j]
            if _day(b["t"]) != day:
                break
            hm2 = _hm(b["t"])
            if side == "ce":
                peak = max(peak, b["h"])
                if be_level and peak >= be_level and stop < spot0:
                    stop = spot0
                if trail_atr:
                    stop = max(stop, peak - trail_atr * a)
                if b["l"] <= stop:
                    exit_spot = stop
                    break
                if b["h"] >= tgt:
                    exit_spot = tgt
                    break
            else:
                peak = min(peak, b["l"])
                if be_level and peak <= be_level and stop > spot0:
                    stop = spot0
                if trail_atr:
                    stop = min(stop, peak + trail_atr * a)
                if b["h"] >= stop:
                    exit_spot = stop
                    break
                if b["l"] <= tgt:
                    exit_spot = tgt
                    break
            if hm2 >= SQUARE:
                exit_spot = b["c"]
                break
            exit_spot = b["c"]
        move = (exit_spot - spot0) if side == "ce" else (spot0 - exit_spot)
        xopt = max(0.05, eopt - SLIP + DELTA * move - SLIP)
        pnl = round((xopt - eopt) * QTY - _ch_long(eopt, xopt), 2)
        trades.append(pnl)
        day_n[day] += 1
        cool = i + cooldown
    return _stats(trades, name)


def sim_short_straddle(
    bars,
    atr,
    adx,
    *,
    after,
    until,
    flat_hm,
    adx_max,
    stop_move_atr,
    allow_days=None,
    name="",
):
    """One short ATM straddle/day when ADX low at entry window; exit by time or |move|."""
    trades = []
    done = set()
    for i in range(len(bars) - 1):
        day = _day(bars[i]["t"])
        if allow_days is not None and day not in allow_days:
            continue
        if day in done:
            continue
        hm = _hm(bars[i]["t"])
        if hm < after or hm > until:
            continue
        a = atr[i]
        if a is None or a <= 0 or adx[i] is None or adx[i] > adx_max:
            continue
        spot0 = bars[i]["c"]
        credit = _prem(spot0, a)  # each leg; straddle ≈ 2*prem
        entry_credit = 2 * credit - 2 * SLIP  # net credit after slip sell
        stop_pts = stop_move_atr * a
        exit_spot = spot0
        for j in range(i + 1, len(bars)):
            b = bars[j]
            if _day(b["t"]) != day:
                break
            hm2 = _hm(b["t"])
            move = abs(b["c"] - spot0)
            # adverse: use high/low excursion
            adverse = max(abs(b["h"] - spot0), abs(b["l"] - spot0))
            if adverse >= stop_pts:
                exit_spot = spot0 + stop_pts if b["h"] - spot0 >= stop_pts else spot0 - stop_pts
                break
            if hm2 >= flat_hm:
                exit_spot = b["c"]
                break
            exit_spot = b["c"]
        spot_move = abs(exit_spot - spot0)
        # buyback cost ≈ 2*(prem - delta*|move|) floored; short P&L = credit - debit
        debit_leg = max(0.05, credit - DELTA * spot_move + SLIP)
        debit = 2 * debit_leg
        pnl = round(entry_credit - debit - _ch_short_straddle(credit, debit_leg), 2)
        trades.append(pnl)
        done.add(day)
    return _stats(trades, name)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "kite_5m_backtest_bars.json"
    bars = json.loads(path.read_text(encoding="utf-8"))
    c = [b["c"] for b in bars]
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    atr = _atr(h, l, c)
    vwap = _vwap(bars)
    _, _, adx = wilder_dmi_series(h, l, c, 14)

    days = sorted({_day(b["t"]) for b in bars})
    split = max(12, int(len(days) * 0.55))
    is_days, oos_days = set(days[:split]), set(days[split:])

    def orb(mins):
        out = {}
        buckets = defaultdict(list)
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

    orb30 = orb(30)
    orb45 = orb(45)

    rows = []

    def push_pair(ris, roos, rfull, name):
        if not (ris and roos and rfull):
            return
        if ris["net"] <= 0 or roos["net"] <= 0:
            return
        rows.append(
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
                "robust": min(ris["net"], roos["net"]),
            }
        )

    # ---- Failed ORB fine grid ----
    for (omap, ot), after, until, sides, stop, tr, trail, be, max_day in itertools.product(
        [(orb30, "30"), (orb45, "45")],
        ["09:40", "09:50", "10:05"],
        ["11:00", "11:30", "12:00", "12:30"],
        ["pe", "both"],
        [1.0, 1.25, 1.5, 1.75, 2.0, 2.5],
        [1.5, 2.0, 2.5, 3.0, 3.5],
        [None, 1.0],
        [None, 1.0],
        [1, 2],
    ):
        # skip invalid after for orb45
        if ot == "45" and after < "10:00":
            continue
        if ot == "30" and after > "10:00":
            continue

        def sig(i, m=omap):
            d = _day(bars[i]["t"])
            if d not in m or i < 2:
                return None
            hi, lo = m[d]
            if h[i - 1] > hi and c[i] < hi and c[i] > lo:
                return "pe"
            if l[i - 1] < lo and c[i] > lo and c[i] < hi:
                return "ce"
            return None

        # skip most trail/be combos to keep runtime sane
        if trail and be:
            continue
        if trail and tr < 2.5:
            continue
        name = f"failorb{ot}_{sides}_{after}-{until}_s{stop}_r{tr}"
        if trail:
            name += f"_trail{trail}"
        if be:
            name += f"_be{be}"
        name += f"_md{max_day}"
        kw = dict(
            after=after,
            until=until,
            stop_atr=stop,
            target_r=tr,
            trail_atr=trail,
            be_at_r=be,
            max_day=max_day,
            cooldown=6,
            sides=sides,
            fresh=True,
            name=name,
        )
        push_pair(
            sim_long(bars, atr, sig, allow_days=is_days, **kw),
            sim_long(bars, atr, sig, allow_days=oos_days, **kw),
            sim_long(bars, atr, sig, allow_days=None, **kw),
            name,
        )

    # ---- Range VWAP fine ----
    for thr, after, until, sides, stop, tr, max_day in itertools.product(
        [0.8, 1.0, 1.2],
        ["09:45", "10:00", "10:15"],
        ["11:30", "12:00", "12:30", "13:00"],
        ["both", "pe"],
        [1.0, 1.25, 1.5, 2.0],
        [1.5, 2.0, 2.5, 3.0],
        [2, 3],
    ):

        def sig(i, t=thr):
            if None in (vwap[i], atr[i], adx[i]) or atr[i] <= 0:
                return None
            if adx[i] > 20:
                return None
            d = (c[i] - vwap[i]) / atr[i]
            if d >= t:
                return "pe"
            if d <= -t:
                return "ce"
            return None

        name = f"rvwap_{thr}_{sides}_{after}-{until}_s{stop}_r{tr}_md{max_day}"
        kw = dict(
            after=after,
            until=until,
            stop_atr=stop,
            target_r=tr,
            max_day=max_day,
            cooldown=3,
            sides=sides,
            fresh=True,
            name=name,
        )
        push_pair(
            sim_long(bars, atr, sig, allow_days=is_days, **kw),
            sim_long(bars, atr, sig, allow_days=oos_days, **kw),
            sim_long(bars, atr, sig, allow_days=None, **kw),
            name,
        )

    # ---- Short premium range ----
    for after, until, flat, adx_max, stop_atr in itertools.product(
        ["09:50", "10:15", "10:45"],
        ["10:00", "10:30", "11:00"],
        ["11:30", "12:30", "13:30", "14:30"],
        [16, 18, 20, 22],
        [1.5, 2.0, 2.5, 3.0],
    ):
        if until <= after or flat <= until:
            continue
        name = f"shortstr_{after}-{until}_flat{flat}_adx{adx_max}_s{stop_atr}"
        push_pair(
            sim_short_straddle(
                bars, atr, adx, after=after, until=until, flat_hm=flat, adx_max=adx_max, stop_move_atr=stop_atr, allow_days=is_days, name=name
            ),
            sim_short_straddle(
                bars, atr, adx, after=after, until=until, flat_hm=flat, adx_max=adx_max, stop_move_atr=stop_atr, allow_days=oos_days, name=name
            ),
            sim_short_straddle(
                bars, atr, adx, after=after, until=until, flat_hm=flat, adx_max=adx_max, stop_move_atr=stop_atr, allow_days=None, name=name
            ),
            name,
        )

    # robust filter
    rob = [
        r
        for r in rows
        if r["is_net"] >= 2500
        and r["oos_net"] >= 2500
        and r["is_pf"] >= 1.25
        and r["oos_pf"] >= 1.25
        and r["is_n"] >= 7
        and r["oos_n"] >= 7
    ]
    rob.sort(key=lambda r: (r["robust"], r["full_net"], r["oos_pf"]), reverse=True)
    print(f"trials={len(rows)} robust={len(rob)}")
    print(f"{'rk':>3} {'rob':>8} {'is':>8} {'oos':>8} {'full':>8} {'isPF':>5} {'oosPF':>5} {'fWR':>5}  name")
    for i, r in enumerate(rob[:25], 1):
        print(
            f"{i:3} {r['robust']:+8.0f} {r['is_net']:+8.0f} {r['oos_net']:+8.0f} {r['full_net']:+8.0f} "
            f"{r['is_pf']:5.2f} {r['oos_pf']:5.2f} {r['full_wr']:5.1f}  {r['name']}"
        )

    # stack distinct families
    port, used = [], set()
    for r in rob:
        fam = r["name"].split("_")[0]
        if fam in used:
            continue
        used.add(fam)
        port.append(r)
        if len(port) >= 3:
            break
    print("\n=== ROBUST STACK ===")
    for r in port:
        print(f"  {r['full_net']:+.0f} full | rob {r['robust']:+.0f} | {r['name']}")
    print(f"SUM full {sum(x['full_net'] for x in port):+.0f}")

    best = rob[0] if rob else None
    print("\n=== BEST ROBUST ===")
    print(json.dumps(best, indent=2))
    out = ROOT / "data" / "hunter_refine_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"top_robust": rob[:40], "best": best, "portfolio": port}, indent=2),
        encoding="utf-8",
    )
    print("saved", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
