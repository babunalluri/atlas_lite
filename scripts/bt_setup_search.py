"""Usage: python3 scripts/bt_setup_search.py data/spot_cache.pkl

Search buy-only setup families x exits x filters on the tape, with 2-fold
cross-validation: pick the best params on one half of the days, score them on
the other half (and the reverse). Only out-of-sample results count.
Engine (book rules, costs) is shared via scripts/bt_common.py.
"""

from __future__ import annotations

import itertools
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import bt_common as eng  # noqa: E402

EXITS = [
    (0.20, 0.10, 30),
    (0.15, 0.10, 20),
    (0.25, 0.10, 30),
    (0.30, 0.15, 45),
    (0.40, 0.20, 60),
    (0.50, 0.25, 90),
]


def simx(px, t, side, ex):
    tg, sp, hold = ex
    hm = eng.m2h(t)
    a = px.get(hm, {}).get("_atm")
    if not a:
        return None
    sym = a[0] if side == "ce" else a[1]
    e = px[hm].get(sym)
    if not e:
        return None
    end = min(eng.SQ, t + hold)
    last = e
    x = None
    xt = end
    why = "time"
    for u in range(t + 1, end + 1):
        p = px.get(eng.m2h(u), {}).get(sym)
        if p is None:
            continue
        last = p
        if p >= e * (1 + tg):
            x, xt, why = p, u, "target"
            break
        if p <= e * (1 - sp):
            x, xt, why = p, u, "stop"
            break
    x = last if x is None else x
    fills = [(e + eng.SLIP, eng.QTY, "buy"), (x - eng.SLIP, eng.QTY, "sell")]
    return (x - e - 2 * eng.SLIP) * eng.QTY - eng.kite_nfo_charges(fills)["total"], xt, why


def runx(sigfn, ex, flt, dset):
    trades = []
    for d in dset:
        sig = sigfn(d)
        n = 0
        dayp = 0.0
        t_free = 0
        block = {}
        locked = set()
        stops = 0
        for t in sorted(sig):
            side, tag = sig[t]
            if n >= 3 or dayp <= -2000 or t < t_free or side in locked or t < block.get(side, 0):
                continue
            if t > eng.h2m("15:14") - 1:
                continue
            if not eng.FILTERS[flt](eng.feats(d, t)):
                continue
            r = simx(eng.PX[d], t, side, ex)
            if not r:
                continue
            net, xt, why = r
            n += 1
            dayp += net
            t_free = xt + 5
            trades.append({"d": d, "t": t, "side": side, "net": net, "why": why})
            if why == "stop":
                stops += 1
                block[side] = xt + 15
                if net <= -400 or stops >= 2:
                    locked.add(side)
            elif net < 0:
                block[side] = xt + 10
            else:
                block[side] = xt + 5
    return trades


def prev_close(d):
    i = eng.days.index(d)
    if i == 0:
        return None
    p = eng.days[i - 1]
    if (date.fromisoformat(d) - date.fromisoformat(p)).days > 4:
        return None
    return eng.SP[p][max(eng.SP[p])]["c"]


def win(a, b):
    return range(eng.h2m(a), eng.h2m(b) + 1)


def trend(T, a, b):
    def f(d):
        sp = eng.SP[d]
        o = sp.get("09:15", {}).get("o")
        out = {}
        for t in win(a, b):
            x = sp.get(eng.m2h(t))
            if o and x and abs(x["c"] - o) >= T:
                out[t] = ("ce" if x["c"] > o else "pe", "trend")
        return out

    return f


def orb(X, b):
    def f(d):
        sp = eng.SP[d]
        orr = [sp[eng.m2h(t)] for t in win("09:15", "09:29") if eng.m2h(t) in sp]
        if not orr:
            return {}
        hi, lo = max(x["h"] for x in orr), min(x["l"] for x in orr)
        out = {}
        up = dn = False
        for t in win("09:30", b):
            x = sp.get(eng.m2h(t))
            if not x:
                continue
            if not up and x["c"] > hi + X:
                up = True
                out[t + 1] = ("ce", "orb")
            if not dn and x["c"] < lo - X:
                dn = True
                out[t + 1] = ("pe", "orb")
        return out

    return f


def pullback(T, P, b):
    def f(d):
        sp = eng.SP[d]
        o = sp.get("09:15", {}).get("o")
        out = {}
        if not o:
            return out
        ext = None
        pulled = False
        prev = None
        for t in win("09:30", b):
            x = sp.get(eng.m2h(t))
            if not x:
                continue
            tr = x["c"] - o
            if abs(tr) >= T:
                s = 1 if tr > 0 else -1
                if ext is None or (s > 0 and x["h"] > ext) or (s < 0 and x["l"] < ext):
                    if pulled and prev is not None:
                        out[t + 1] = ("ce" if s > 0 else "pe", "pullback")
                        pulled = False
                    ext = x["h"] if s > 0 else x["l"]
                elif ext is not None and abs(ext - x["c"]) >= P:
                    pulled = True
            prev = x
        return out

    return f


def gap(G, mode, at):
    def f(d):
        pc = prev_close(d)
        o = eng.SP[d].get("09:15", {}).get("o")
        if not pc or not o or abs(o - pc) < G:
            return {}
        up = o > pc
        side = ("ce" if up else "pe") if mode == "follow" else ("pe" if up else "ce")
        return {eng.h2m(at): (side, "gap")}

    return f


def sweep(X, N, b):
    return lambda d: eng.sweep_signals(d, "09:30", b, X, N, True)


def tot(tr):
    return sum(x["net"] for x in tr)


def main(spot_pkl: str) -> None:
    eng.bootstrap(spot_pkl)
    fam = {
        "trend": [
            (f"T={T} {a}-{b}", trend(T, a, b))
            for T in (50, 75, 100, 125, 150)
            for a, b in (
                ("09:30", "11:59"),
                ("09:30", "10:59"),
                ("10:00", "12:59"),
                ("09:30", "14:44"),
            )
        ],
        "orb": [
            (f"X={X} until {b}", orb(X, b))
            for X in (0, 10, 20, 30)
            for b in ("11:59", "14:44")
        ],
        "pullback": [
            (f"T={T} P={P} until {b}", pullback(T, P, b))
            for T in (50, 75, 100)
            for P in (20, 40)
            for b in ("11:59", "14:44")
        ],
        "gap": [
            (f"G={G} {m} at {at}", gap(G, m, at))
            for G in (50, 100)
            for m in ("follow", "fade")
            for at in ("09:30", "09:45")
        ],
        "sweep": [
            (f"X={X} N={N} until {b}", sweep(X, N, b))
            for X in (5, 10)
            for N in (3, 5, 10)
            for b in ("11:59", "14:44")
        ],
    }
    flt = ("none", "iv_rise<10%", "vix_rise<5%")
    h1 = [d for d in eng.days if d in eng.half1]
    h2 = [d for d in eng.days if d not in eng.half1]

    ncombos = 0
    grand = []
    print(
        f"{len(eng.days)} days; H1 {h1[0]}..{h1[-1]} ({len(h1)}), H2 {h2[0]}..{h2[-1]} ({len(h2)})\n"
    )
    for fam_name, sigs in fam.items():
        cands = []
        for (name, fn), ex, f in itertools.product(sigs, EXITS, flt):
            ncombos += 1
            a = runx(fn, ex, f, h1)
            b = runx(fn, ex, f, h2)
            cands.append((name, ex, f, a, b))
        print(f"=== {fam_name}: {len(cands)} combos ===")
        oos = 0.0
        oos_n = 0
        for fit, test, lab in ((3, 4, "fit H1 -> test H2"), (4, 3, "fit H2 -> test H1")):
            ok = [c for c in cands if len(c[fit]) >= 5]
            if not ok:
                print(f"  {lab}: no combo with >=5 trades")
                continue
            best = max(ok, key=lambda c: tot(c[fit]))
            o = tot(best[test])
            oos += o
            oos_n += len(best[test])
            print(
                f"  {lab}: best {best[0]} exits {best[1]} {best[2]}  "
                f"fit ₹{tot(best[fit]):6.0f} ({len(best[fit])} tr)  "
                f"-> OUT-OF-SAMPLE ₹{o:6.0f} ({len(best[test])} tr)"
            )
        print(f"  cross-validated total: ₹{oos:6.0f} over {oos_n} unseen trades")
        pos = [
            c
            for c in cands
            if len(c[3]) + len(c[4]) >= 12 and tot(c[3]) > 0 and tot(c[4]) > 0
        ]
        print(f"  combos positive in BOTH halves (>=12 trades): {len(pos)}/{len(cands)}")
        for c in sorted(pos, key=lambda c: -(tot(c[3]) + tot(c[4])))[:5]:
            allt = c[3] + c[4]
            ce = sum(x["net"] for x in allt if x["side"] == "ce")
            pe = tot(allt) - ce
            print(
                f"    {c[0]:24s} exits {c[1]} {c[2]:12s} n {len(allt):3d} "
                f"₹/tr {tot(allt)/len(allt):5.0f}  H1 {tot(c[3]):6.0f} H2 {tot(c[4]):6.0f} "
                f"CE {ce:6.0f} PE {pe:6.0f}"
            )
            grand.append((fam_name,) + c)
        print()
    print(f"combos tried: {ncombos}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 scripts/bt_setup_search.py data/spot_cache.pkl")
    main(sys.argv[1])
