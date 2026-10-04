#!/usr/bin/env python3
"""Blank-slate strategy search on kite_1m_bars.json (NIFTY 50 index 1m).

Walk-forward by session (55% IS / 45% OOS). Option P&L = delta0.5*spot + NFO + slip.
Ranks: high WR, then robust profit with WR≥50%.
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


def load_bars(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "bars" in raw:
        return raw["bars"]
    if isinstance(raw, list):
        return raw
    raise ValueError("unexpected 1m json shape")


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
    if len(pnls) < 10:
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


def simulate(
    bars,
    atr,
    signal,
    *,
    after,
    until,
    stop_atr,
    target_r,
    max_day,
    cooldown,
    allow_days,
    name,
    sides_fixed=None,
    fresh=True,
):
    pnls = []
    day_n = defaultdict(int)
    cool = -1
    prev = None
    for i in range(len(bars) - 1):
        d = day(bars[i]["t"])
        if allow_days is not None and d not in allow_days:
            continue
        t = hm(bars[i]["t"])
        if t < after or t > until:
            prev = None
            continue
        if day_n[d] >= max_day or i < cool:
            continue
        a = atr[i]
        if a is None or a <= 0:
            continue
        side = signal(i)
        if side not in ("ce", "pe"):
            prev = None
            continue
        if sides_fixed and side != sides_fixed:
            continue
        if fresh and side == prev:
            continue
        prev = side
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
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "kite_1m_bars.json"
    bars = load_bars(path)
    c = [b["c"] for b in bars]
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    o = [b["o"] for b in bars]
    # Risk ATR ≈ 5m ATR14: ~70 one-minute bars. Signal ATR stays short.
    atr_risk = atr_series(h, l, c, 70)  # used for stop/target/premium
    atr_sig = atr_series(h, l, c, 20)  # distance filters
    e9 = ema(c, 9)
    e21 = ema(c, 21)
    r14 = rsi(c, 14)
    # slower RSI (~5m RSI14 proxy)
    r70 = rsi(c, 70)
    vw = vwap_series(bars)

    days = sorted({day(b["t"]) for b in bars})
    split = max(15, int(len(days) * 0.55))
    is_d, oos_d = set(days[:split]), set(days[split:])
    print(f"bars={len(bars)} days={len(days)} IS={days[0]}..{days[split-1]} ({len(is_d)})")
    print(f"OOS={days[split]}..{days[-1]} ({len(oos_d)})")

    # clock bias next 15m / 30m
    print("\n=== CLOCK BIAS (avg spot next 15m) ===")
    buckets = defaultdict(list)
    for i in range(len(bars) - 15):
        t = hm(bars[i]["t"])
        if t < "09:20" or t > "14:45":
            continue
        hh, mm = map(int, t.split(":"))
        key = f"{hh:02d}:{(mm // 15) * 15:02d}"
        buckets[key].append(c[i + 15] - c[i])
    for k in sorted(buckets):
        xs = buckets[k]
        if len(xs) < 80:
            continue
        avg = sum(xs) / len(xs)
        up = 100 * sum(1 for x in xs if x > 0) / len(xs)
        print(f"  {k} n={len(xs)} avg={avg:+.2f} up%={up:.1f}")

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

    exits = [
        (1.0, 0.7),
        (1.2, 0.8),
        (1.5, 0.8),
        (1.5, 1.0),
        (1.0, 1.0),
        (1.5, 1.5),
        (1.2, 1.5),
        (1.5, 2.0),
        (2.0, 1.0),
        (2.0, 1.5),
        (2.0, 2.0),
    ]
    windows = [
        ("09:30", "10:00"),
        ("09:45", "10:30"),
        ("10:00", "10:45"),
        ("10:00", "11:30"),
        ("10:15", "11:15"),
        ("10:30", "12:00"),
        ("11:00", "12:30"),
        ("11:30", "13:00"),
        ("12:00", "13:30"),
        ("13:00", "14:00"),
        ("09:45", "12:00"),
        ("10:00", "13:00"),
    ]

    results = []

    def eval_rule(sig, tag, after, until, stop, tr, *, sides=None, max_day=3, cooldown=8, fresh=True):
        # Always risk-manage with atr_risk (session-scale), never raw 1m ATR14.
        name = f"{tag}_{after}-{until}_s{stop}_r{tr}"
        if sides:
            name += f"_{sides}"
        common = dict(
            after=after,
            until=until,
            stop_atr=stop,
            target_r=tr,
            max_day=max_day,
            cooldown=cooldown,
            name=name,
            sides_fixed=sides,
            fresh=fresh,
        )
        ris = simulate(bars, atr_risk, sig, allow_days=is_d, **common)
        roos = simulate(bars, atr_risk, sig, allow_days=oos_d, **common)
        rfull = simulate(bars, atr_risk, sig, allow_days=None, **common)
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

    # A) clock always-side (fresh=False: one scheduled slot/day via max_day=1)
    for (a, u), side, (st, tr) in itertools.product(windows, ["pe", "ce"], exits):
        eval_rule(
            lambda i, s=side: s,
            f"clock_{side}",
            a,
            u,
            st,
            tr,
            max_day=1,
            cooldown=20,
            fresh=False,
        )

    # B) VWAP with/fade
    def vwap_sig(i, mode):
        if vw[i] is None:
            return None
        above = c[i] > vw[i]
        if mode == "with":
            return "ce" if above else "pe"
        return "pe" if above else "ce"

    for (a, u), mode, (st, tr), side in itertools.product(
        windows, ["fade", "with"], exits, [None, "pe", "ce"]
    ):
        eval_rule(lambda i, m=mode: vwap_sig(i, m), f"vwap_{mode}", a, u, st, tr, sides=side, max_day=3, cooldown=10)

    # C) open-distance fade/go (distance in atr_risk units)
    def odist(i, mode, thr):
        d = day(bars[i]["t"])
        a = atr_risk[i]
        if d not in day_open or a is None or a <= 0:
            return None
        dist = (c[i] - day_open[d]) / a
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

    for thr, mode, (a, u), (st, tr), side in itertools.product(
        [0.8, 1.2, 1.8, 2.5],
        ["fade", "go"],
        [("10:00", "11:30"), ("10:00", "13:00"), ("10:30", "13:00"), ("11:00", "14:00")],
        exits,
        [None, "pe"],
    ):
        eval_rule(
            lambda i, m=mode, t=thr: odist(i, m, t),
            f"opendist_{mode}_{thr}",
            a,
            u,
            st,
            tr,
            sides=side,
            max_day=3,
            cooldown=12,
        )

    # D) RSI extremes — fast (14) and slow (~5m proxy 70)
    def rsi_sig(i, series, hi=70, lo=30):
        v = series[i]
        if v is None:
            return None
        if v >= hi:
            return "pe"
        if v <= lo:
            return "ce"
        return None

    for series, tag, (a, u), (st, tr), side in itertools.product(
        [(r14, "rsi14"), (r70, "rsi70")],
        ["x"],
        windows[:8],
        exits,
        [None, "pe", "ce"],
    ):
        ser, tname = series
        eval_rule(
            lambda i, s=ser: rsi_sig(i, s),
            tname,
            a,
            u,
            st,
            tr,
            sides=side,
            max_day=4,
            cooldown=8,
        )

    # E) EMA9/21
    def ema_sig(i, mode):
        if None in (e9[i], e21[i]):
            return None
        bull = e9[i] > e21[i]
        if mode == "with":
            return "ce" if bull else "pe"
        return "pe" if bull else "ce"

    for (a, u), mode, (st, tr), side in itertools.product(
        [("09:45", "11:30"), ("10:00", "12:00"), ("10:00", "13:00"), ("11:00", "13:30")],
        ["with", "fade"],
        exits,
        [None, "pe"],
    ):
        eval_rule(lambda i, m=mode: ema_sig(i, m), f"ema_{mode}", a, u, st, tr, sides=side)

    # F) ORB break / fail
    for om, tag, (a, u), (st, tr), side, mode in itertools.product(
        [(o15, "15"), (o30, "30")],
        ["x"],
        [("09:35", "11:00"), ("09:50", "11:30"), ("10:00", "12:30")],
        exits,
        [None, "pe", "ce"],
        ["brk", "fail"],
    ):
        omap, ot = om

        def sig(i, m=omap, md=mode):
            d = day(bars[i]["t"])
            if d not in m or i < 2:
                return None
            hi, lo = m[d]
            if md == "brk":
                if c[i] > hi:
                    return "ce"
                if c[i] < lo:
                    return "pe"
            else:
                if h[i - 1] > hi and c[i] < hi and c[i] > lo:
                    return "pe"
                if l[i - 1] < lo and c[i] > lo and c[i] < hi:
                    return "ce"
            return None

        eval_rule(sig, f"orb{ot}_{mode}", a, u, st, tr, sides=side, max_day=2, cooldown=15)

    # G) gap fade/go morning
    def gap_sig(i, mode):
        d = day(bars[i]["t"])
        a = atr_risk[i]
        if d not in pc or d not in day_open or a is None or a <= 0:
            return None
        g = (day_open[d] - pc[d]) / a
        if abs(g) < 0.5:
            return None
        if mode == "fade":
            return "pe" if g > 0 else "ce"
        return "ce" if g > 0 else "pe"

    for mode, (st, tr), side in itertools.product(["fade", "go"], exits, [None, "pe"]):
        eval_rule(
            lambda i, m=mode: gap_sig(i, m),
            f"gap_{mode}",
            "09:30",
            "10:30",
            st,
            tr,
            sides=side,
            max_day=1,
            cooldown=30,
        )

    # H) body reverse / cont on 1m impulse (need meaningful bar vs atr_sig)
    def body_sig(i, mode):
        a = atr_sig[i]
        if a is None or a <= 0:
            return None
        body = c[i] - o[i]
        if abs(body) < 0.8 * a:
            return None
        up = body > 0
        if mode == "cont":
            return "ce" if up else "pe"
        return "pe" if up else "ce"

    for (a, u), mode, (st, tr), side in itertools.product(
        [("09:45", "11:00"), ("10:00", "12:00"), ("11:00", "13:00")],
        ["cont", "rev"],
        exits,
        [None, "pe"],
    ):
        eval_rule(lambda i, m=mode: body_sig(i, m), f"body_{mode}", a, u, st, tr, sides=side, cooldown=10)

    print(f"\nIS+OOS green: {len(results)}")

    # A high WR
    hi = [
        r
        for r in results
        if r["full_wr"] >= 55
        and r["oos_wr"] >= 50
        and r["is_wr"] >= 50
        and r["full_n"] >= 18
        and r["oos_n"] >= 8
        and r["is_pf"] >= 1.15
        and r["oos_pf"] >= 1.15
        and r["robust"] >= 1500
    ]
    hi.sort(key=lambda r: (r["full_wr"], r["robust"], r["full_net"]), reverse=True)

    # B best robust with WR>=50
    prof = [
        r
        for r in results
        if r["full_wr"] >= 50
        and r["oos_wr"] >= 48
        and r["is_wr"] >= 48
        and r["full_n"] >= 20
        and r["robust"] >= 2500
        and r["is_pf"] >= 1.2
        and r["oos_pf"] >= 1.2
    ]
    prof.sort(key=lambda r: (r["robust"], r["full_net"]), reverse=True)

    # C max robust any WR
    anyr = [
        r
        for r in results
        if r["robust"] >= 3000
        and r["is_n"] >= 12
        and r["oos_n"] >= 10
        and r["is_pf"] >= 1.2
        and r["oos_pf"] >= 1.2
    ]
    anyr.sort(key=lambda r: (r["robust"], r["full_net"]), reverse=True)

    def show(title, rows, n=20):
        print(f"\n=== {title} (n={len(rows)}) ===")
        print(f"{'rk':>3} {'WR':>6} {'full':>9} {'rob':>8} {'oos':>8} {'n':>4} {'PF':>5}  name")
        for i, r in enumerate(rows[:n], 1):
            print(
                f"{i:3} {r['full_wr']:6.1f} {r['full_net']:+9.0f} {r['robust']:+8.0f} "
                f"{r['oos_net']:+8.0f} {r['full_n']:4} {r['full_pf']:5.2f}  {r['name']}"
            )

    show("A HIGH WR (≥55, robust≥1k, PF≥1.15 both)", hi)
    show("B BEST PROFIT WR≥50 robust", prof)
    show("C MAX ROBUST (any WR)", anyr)

    best = prof[0] if prof else (hi[0] if hi else (anyr[0] if anyr else None))
    # prefer balanced high WR+profit: score
    scored = []
    for r in results:
        if r["full_n"] < 25 or r["robust"] < 2000:
            continue
        if r["is_pf"] < 1.2 or r["oos_pf"] < 1.2:
            continue
        if r["full_wr"] < 50:
            continue
        score = r["robust"] * (r["full_wr"] / 50.0) * min(r["is_pf"], r["oos_pf"])
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    print("\n=== RECOMMENDED (robust × WR × minPF) ===")
    if scored:
        best = scored[0][1]
        for i, (sc, r) in enumerate(scored[:10], 1):
            print(
                f"{i:3} score={sc:.0f} WR={r['full_wr']} full={r['full_net']:+.0f} "
                f"rob={r['robust']:+.0f} PF={r['full_pf']}  {r['name']}"
            )
        print("\nPICK:")
        print(json.dumps(best, indent=2))
    else:
        print("NONE — falling back")
        print(json.dumps(best, indent=2))

    out = ROOT / "data" / "blank_1m_results.json"
    out.write_text(
        json.dumps(
            {
                "best": best,
                "scored": [r for _, r in scored[:25]],
                "high_wr": hi[:25],
                "profit": prof[:25],
                "max_robust": anyr[:25],
                "split": {"is": days[:split], "oos": days[split:]},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("saved", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
