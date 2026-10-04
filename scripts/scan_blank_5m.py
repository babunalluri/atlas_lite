#!/usr/bin/env python3
"""Blank-slate 5m discovery — no prior strategy assumptions.

1) Measure forward spot bias by clock bucket (pure stats).
2) Brute simple long-option rules with walk-forward.
3) Leaderboards: (A) high WR profitable  (B) max OOS net with WR≥45%.

P&L proxy: delta0.5 * spot + NFO charges + slip, 1 lot.
"""

from __future__ import annotations

import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402

QTY = 65
SLIP = 0.5
DELTA = 0.5
SQUARE = "15:14"


def hm(ts: str) -> str:
    return str(ts).replace("T", " ")[11:16]


def day(ts: str) -> str:
    return str(ts).replace("T", " ")[:10]


def atr_series(h, l, c, n=14):
    out = [None] * len(c)
    tr = []
    for i in range(len(c)):
        tr.append(
            h[i] - l[i]
            if i == 0
            else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        )
    if len(c) < n + 1:
        return out
    p = sum(tr[1 : n + 1]) / n
    out[n] = p
    for i in range(n + 1, len(c)):
        p = (p * (n - 1) + tr[i]) / n
        out[i] = p
    return out


def ema(xs, n):
    out = [None] * len(xs)
    k = 2 / (n + 1)
    prev = None
    seed = []
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


def rsi(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = [max(c[i] - c[i - 1], 0) for i in range(1, n + 1)]
    lo = [max(c[i - 1] - c[i], 0) for i in range(1, n + 1)]
    ag, al = sum(g) / n, sum(lo) / n
    out[n] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def vwap_series(bars):
    out = [None] * len(bars)
    pv = vol = 0.0
    d0 = ""
    for i, b in enumerate(bars):
        d = day(b["t"])
        if d != d0:
            d0, pv, vol = d, 0.0, 0.0
        v = float(b.get("v") or 0) or 1.0
        pv += ((b["h"] + b["l"] + b["c"]) / 3) * v
        vol += v
        out[i] = pv / vol
    return out


def prem(spot, a):
    return max(20.0, round(0.004 * spot + 0.35 * a, 2))


def charges(e, x):
    return float(kite_nfo_charges([(e, QTY, "buy"), (x, QTY, "sell")])["total"])


def stats(pnls, name):
    if len(pnls) < 8:
        return None
    w = [p for p in pnls if p > 0]
    l = [p for p in pnls if p <= 0]
    gl = abs(sum(l)) or 1e-9
    return {
        "name": name,
        "n": len(pnls),
        "wr": round(100 * len(w) / len(pnls), 1),
        "net": round(sum(pnls), 2),
        "avg": round(sum(pnls) / len(pnls), 2),
        "pf": round(sum(w) / gl, 2),
    }


def simulate(bars, atr, signal, *, after, until, stop_atr, target_r, max_day, cooldown, allow_days, name):
    pnls = []
    day_n = defaultdict(int)
    cool = -1
    for i in range(len(bars) - 1):
        d = day(bars[i]["t"])
        if allow_days is not None and d not in allow_days:
            continue
        t = hm(bars[i]["t"])
        if t < after or t > until:
            continue
        if day_n[d] >= max_day or i < cool:
            continue
        a = atr[i]
        if a is None or a <= 0:
            continue
        side = signal(i)
        if side not in ("ce", "pe"):
            continue
        spot0 = bars[i]["c"]
        risk = stop_atr * a
        stop = spot0 - risk if side == "ce" else spot0 + risk
        tgt = spot0 + target_r * risk if side == "ce" else spot0 - target_r * risk
        e = prem(spot0, a) + SLIP
        exit_spot = spot0
        for j in range(i + 1, len(bars)):
            b = bars[j]
            if day(b["t"]) != d:
                break
            if side == "ce":
                if b["l"] <= stop:
                    exit_spot = stop
                    break
                if b["h"] >= tgt:
                    exit_spot = tgt
                    break
            else:
                if b["h"] >= stop:
                    exit_spot = stop
                    break
                if b["l"] <= tgt:
                    exit_spot = tgt
                    break
            if hm(b["t"]) >= SQUARE:
                exit_spot = b["c"]
                break
            exit_spot = b["c"]
        move = (exit_spot - spot0) if side == "ce" else (spot0 - exit_spot)
        x = max(0.05, e - SLIP + DELTA * move - SLIP)
        pnls.append(round((x - e) * QTY - charges(e, x), 2))
        day_n[d] += 1
        cool = i + cooldown
    return stats(pnls, name)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "kite_5m_backtest_bars.json"
    bars = json.loads(path.read_text(encoding="utf-8"))
    c = [b["c"] for b in bars]
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    o = [b["o"] for b in bars]
    atr = atr_series(h, l, c)
    e8 = ema(c, 8)
    e21 = ema(c, 21)
    r14 = rsi(c)
    vw = vwap_series(bars)

    days = sorted({day(b["t"]) for b in bars})
    split = max(12, int(len(days) * 0.55))
    is_d, oos_d = set(days[:split]), set(days[split:])
    print(f"bars={len(bars)} days={len(days)} IS={days[0]}..{days[split-1]} OOS={days[split]}..{days[-1]}")

    # ---------- 1) Pure clock bias (forward 6 bars = 30m) ----------
    buckets = defaultdict(list)
    for i in range(len(bars) - 6):
        t = hm(bars[i]["t"])
        if t < "09:20" or t > "14:45":
            continue
        # bucket to 15m
        hh, mm = map(int, t.split(":"))
        m = (mm // 15) * 15
        key = f"{hh:02d}:{m:02d}"
        fwd = c[i + 6] - c[i]
        buckets[key].append(fwd)

    print("\n=== CLOCK BIAS (avg spot move next 30m) ===")
    print(f"{'bucket':>7} {'n':>5} {'avg':>8} {'up%':>6}")
    clock_rank = []
    for k in sorted(buckets):
        xs = buckets[k]
        if len(xs) < 30:
            continue
        avg = sum(xs) / len(xs)
        up = 100 * sum(1 for x in xs if x > 0) / len(xs)
        clock_rank.append((k, avg, up, len(xs)))
        print(f"{k:>7} {len(xs):5} {avg:+8.2f} {up:6.1f}")

    # strongest directional buckets
    pe_bias = sorted(clock_rank, key=lambda x: x[1])[:5]  # most negative → PE
    ce_bias = sorted(clock_rank, key=lambda x: x[1], reverse=True)[:5]
    print("\nStrongest DOWN buckets (PE lean):", [(a, round(b, 2), round(c, 1)) for a, b, c, _ in pe_bias])
    print("Strongest UP buckets (CE lean):", [(a, round(b, 2), round(c, 1)) for a, b, c, _ in ce_bias])

    # day open / prev close
    day_open = {}
    closes = defaultdict(list)
    for b in bars:
        d = day(b["t"])
        closes[d].append(b["c"])
        if d not in day_open and hm(b["t"]) >= "09:15":
            day_open[d] = b["o"]
    pc = {}
    ds = sorted(closes)
    for i in range(1, len(ds)):
        pc[ds[i]] = closes[ds[i - 1]][-1]

    def orb(mins):
        out = {}
        buck = defaultdict(list)
        s0, s1 = 9 * 60 + 15, 9 * 60 + 15 + mins
        for b in bars:
            hh, mm = map(int, hm(b["t"]).split(":"))
            m = hh * 60 + mm
            if s0 <= m < s1:
                buck[day(b["t"])].append(b)
        for d, xs in buck.items():
            if xs:
                out[d] = (max(x["h"] for x in xs), min(x["l"] for x in xs))
        return out

    o15, o30 = orb(15), orb(30)

    # ---------- 2) Brute simple rules ----------
    # Exit grid includes HIGH-WR style (target < stop) and balanced
    exits = [
        (1.0, 0.6),
        (1.2, 0.7),
        (1.5, 0.8),
        (1.5, 1.0),
        (1.0, 1.0),
        (1.2, 1.2),
        (1.5, 1.5),
        (1.0, 1.5),
        (1.5, 2.0),
        (2.0, 1.0),
        (2.0, 1.5),
    ]

    windows = [
        ("09:30", "10:00"),
        ("09:45", "10:15"),
        ("10:00", "10:30"),
        ("10:00", "11:00"),
        ("10:15", "11:15"),
        ("10:30", "11:30"),
        ("11:00", "12:00"),
        ("11:30", "12:30"),
        ("12:00", "13:00"),
        ("13:00", "14:00"),
        ("09:45", "11:30"),
        ("10:00", "12:30"),
    ]

    results = []

    def eval_rule(sig, tag, after, until, stop, tr, sides_fixed=None, max_day=2, cooldown=4):
        def wrapped(i, s=sides_fixed):
            side = sig(i)
            if s and side != s:
                return None
            return side

        name = f"{tag}_{after}-{until}_s{stop}_r{tr}"
        if sides_fixed:
            name += f"_{sides_fixed}"
        common = dict(
            after=after,
            until=until,
            stop_atr=stop,
            target_r=tr,
            max_day=max_day,
            cooldown=cooldown,
            name=name,
        )
        ris = simulate(bars, atr, wrapped, allow_days=is_d, **common)
        roos = simulate(bars, atr, wrapped, allow_days=oos_d, **common)
        rfull = simulate(bars, atr, wrapped, allow_days=None, **common)
        if not (ris and roos and rfull):
            return
        if ris["net"] <= 0 or roos["net"] <= 0:
            return
        results.append(
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
                "robust": min(ris["net"], roos["net"]),
            }
        )

    # A) Pure clock: always CE or PE in window
    for (after, until), side, (stop, tr) in itertools.product(windows, ["ce", "pe"], exits):
        eval_rule(lambda i, s=side: s, f"clock_{side}", after, until, stop, tr, max_day=1, cooldown=8)

    # B) Above/below VWAP
    def vwap_side(i, mode="with"):
        if vw[i] is None:
            return None
        if mode == "with":
            return "ce" if c[i] > vw[i] else "pe"
        return "pe" if c[i] > vw[i] else "ce"

    for (after, until), mode, (stop, tr), fixed in itertools.product(
        windows, ["with", "fade"], exits, [None, "pe", "ce"]
    ):
        eval_rule(lambda i, m=mode: vwap_side(i, m), f"vwap_{mode}", after, until, stop, tr, sides_fixed=fixed)

    # C) EMA8 vs EMA21
    def ema_side(i, mode="with"):
        if None in (e8[i], e21[i]):
            return None
        bull = e8[i] > e21[i]
        if mode == "with":
            return "ce" if bull else "pe"
        return "pe" if bull else "ce"

    for (after, until), mode, (stop, tr), fixed in itertools.product(
        [("09:45", "11:30"), ("10:00", "12:30"), ("10:00", "11:00"), ("11:00", "13:00")],
        ["with", "fade"],
        exits,
        [None, "pe"],
    ):
        eval_rule(lambda i, m=mode: ema_side(i, m), f"ema_{mode}", after, until, stop, tr, sides_fixed=fixed)

    # D) RSI extreme
    def rsi_side(i):
        if r14[i] is None:
            return None
        if r14[i] >= 70:
            return "pe"
        if r14[i] <= 30:
            return "ce"
        return None

    for (after, until), (stop, tr), fixed in itertools.product(
        windows[:8], exits, [None, "pe", "ce"]
    ):
        eval_rule(rsi_side, "rsi_x", after, until, stop, tr, sides_fixed=fixed)

    # E) ORB break
    for om, tag, (after, until), (stop, tr), fixed in itertools.product(
        [(o15, "15"), (o30, "30")],
        ["x"],
        [("09:35", "11:00"), ("09:50", "11:30"), ("10:00", "12:30")],
        exits,
        [None, "pe", "ce"],
    ):
        omap, ot = om

        def sig(i, m=omap):
            d = day(bars[i]["t"])
            if d not in m:
                return None
            hi, lo = m[d]
            if c[i] > hi:
                return "ce"
            if c[i] < lo:
                return "pe"
            return None

        eval_rule(sig, f"orb{ot}", after, until, stop, tr, sides_fixed=fixed, max_day=1, cooldown=8)

    # F) Gap from prev close at open window
    def gap_side(i, mode="fade"):
        d = day(bars[i]["t"])
        if d not in pc or d not in day_open or atr[i] is None:
            return None
        g = (day_open[d] - pc[d]) / atr[i]
        if abs(g) < 0.6:
            return None
        if mode == "fade":
            return "pe" if g > 0 else "ce"
        return "ce" if g > 0 else "pe"

    for mode, (stop, tr), fixed in itertools.product(["fade", "go"], exits, [None, "pe"]):
        eval_rule(
            lambda i, m=mode: gap_side(i, m),
            f"gap_{mode}",
            "09:30",
            "10:30",
            stop,
            tr,
            sides_fixed=fixed,
            max_day=1,
            cooldown=10,
        )

    # G) Prior bar engulf / body
    def body_side(i, mode="cont"):
        if i < 1 or atr[i] is None:
            return None
        body = c[i] - o[i]
        if abs(body) < 0.35 * atr[i]:
            return None
        up = body > 0
        if mode == "cont":
            return "ce" if up else "pe"
        return "pe" if up else "ce"

    for (after, until), mode, (stop, tr), fixed in itertools.product(
        [("09:45", "11:00"), ("10:00", "12:00"), ("11:00", "13:00")],
        ["cont", "rev"],
        exits,
        [None, "pe"],
    ):
        eval_rule(lambda i, m=mode: body_side(i, m), f"body_{mode}", after, until, stop, tr, sides_fixed=fixed)

    # H) Distance from day open
    def open_dist(i, mode="fade", thr=1.0):
        d = day(bars[i]["t"])
        if d not in day_open or atr[i] is None or atr[i] <= 0:
            return None
        dist = (c[i] - day_open[d]) / atr[i]
        if mode == "fade":
            if dist >= thr:
                return "pe"
            if dist <= -thr:
                return "ce"
        else:
            if dist >= thr:
                return "ce"
            if dist <= -thr:
                return "pe"
        return None

    for thr, mode, (after, until), (stop, tr), fixed in itertools.product(
        [0.8, 1.2, 1.6],
        ["fade", "go"],
        [("10:00", "11:30"), ("10:00", "13:00"), ("11:00", "14:00")],
        exits,
        [None, "pe"],
    ):
        eval_rule(
            lambda i, t=thr, m=mode: open_dist(i, m, t),
            f"opendist_{mode}_{thr}",
            after,
            until,
            stop,
            tr,
            sides_fixed=fixed,
        )

    print(f"\nOOS+IS green rules: {len(results)}")

    # Leaderboard A: high WR (full WR≥50, oos WR≥45, both profitable)
    hi_wr = [
        r
        for r in results
        if r["full_wr"] >= 50
        and r["oos_wr"] >= 45
        and r["is_wr"] >= 45
        and r["full_n"] >= 15
        and r["oos_n"] >= 6
        and r["is_pf"] >= 1.1
        and r["oos_pf"] >= 1.1
    ]
    hi_wr.sort(key=lambda r: (r["full_wr"], r["robust"], r["full_net"]), reverse=True)

    print("\n=== A) HIGH WIN-RATE (WR≥50 full, ≥45 both halves, PF≥1.1) ===")
    print(f"{'rk':>3} {'WR':>6} {'full':>8} {'rob':>8} {'oos':>8} {'n':>4} {'PF':>5}  name")
    for i, r in enumerate(hi_wr[:20], 1):
        print(
            f"{i:3} {r['full_wr']:6.1f} {r['full_net']:+8.0f} {r['robust']:+8.0f} "
            f"{r['oos_net']:+8.0f} {r['full_n']:4} {r['full_pf']:5.2f}  {r['name']}"
        )

    # Leaderboard B: max robust profit with WR≥45
    profit = [
        r
        for r in results
        if r["full_wr"] >= 45
        and r["oos_wr"] >= 40
        and r["full_n"] >= 15
        and r["robust"] >= 1500
        and r["oos_pf"] >= 1.15
        and r["is_pf"] >= 1.15
    ]
    profit.sort(key=lambda r: (r["robust"], r["full_net"]), reverse=True)

    print("\n=== B) BEST PROFIT with WR≥45 (robust / walk-forward) ===")
    print(f"{'rk':>3} {'rob':>8} {'full':>8} {'WR':>6} {'oosWR':>6} {'n':>4} {'PF':>5}  name")
    for i, r in enumerate(profit[:20], 1):
        print(
            f"{i:3} {r['robust']:+8.0f} {r['full_net']:+8.0f} {r['full_wr']:6.1f} "
            f"{r['oos_wr']:6.1f} {r['full_n']:4} {r['full_pf']:5.2f}  {r['name']}"
        )

    # Leaderboard C: absolute best robust (any WR) — for honesty
    anyr = [
        r
        for r in results
        if r["robust"] >= 2000
        and r["is_n"] >= 8
        and r["oos_n"] >= 6
        and r["is_pf"] >= 1.2
        and r["oos_pf"] >= 1.2
    ]
    anyr.sort(key=lambda r: (r["robust"], r["full_net"]), reverse=True)
    print("\n=== C) MAX ROBUST PROFIT (any WR, PF≥1.2 both) ===")
    for i, r in enumerate(anyr[:15], 1):
        print(
            f"{i:3} rob={r['robust']:+.0f} full={r['full_net']:+.0f} WR={r['full_wr']} "
            f"PF={r['full_pf']} n={r['full_n']}  {r['name']}"
        )

    best_wr = hi_wr[0] if hi_wr else None
    best_prof = profit[0] if profit else None
    best_any = anyr[0] if anyr else None

    print("\n=== SUMMARY PICKS ===")
    print("HIGH_WR:", json.dumps(best_wr, indent=2) if best_wr else "NONE")
    print("PROFIT_WR45:", json.dumps(best_prof, indent=2) if best_prof else "NONE")
    print("MAX_ROBUST:", json.dumps(best_any, indent=2) if best_any else "NONE")

    out = ROOT / "data" / "blank_5m_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "clock_pe": pe_bias,
                "clock_ce": ce_bias,
                "high_wr": hi_wr[:30],
                "profit_wr45": profit[:30],
                "max_robust": anyr[:30],
                "best_wr": best_wr,
                "best_profit_wr45": best_prof,
                "best_any": best_any,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("saved", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
