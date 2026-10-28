"""Usage: python3 scripts/bt_oi_confirm.py data/spot_cache.pkl

Trend entries (spot vs open) with futures-OI confirmation: take the trade only when
NIFTY futures OI is building (new positions) while price moves. Price down + OI up =
short build-up -> PE; price up + OI up = long build-up -> CE. Book rules and costs
via scripts/bt_common.py; 2-fold cross-check by halves.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import bt_common as eng  # noqa: E402
from bt_setup_search import runx, tot, trend  # noqa: E402

ROLL = {"2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"}  # monthly expiry week: OI = rollover


def oi_at(d, t):
    for u in range(t, t - 5, -1):
        v = (eng.SP[d].get(eng.m2h(u)) or {}).get("fut_oi")
        if v:
            return v
    return None


def confirmed(base_fn, mode, x):
    def f(d):
        out = {}
        for t, sig in base_fn(d).items():
            now = oi_at(d, t)
            ref = (
                oi_at(d, eng.h2m("09:16"))
                if mode in ("open", "falling")
                else oi_at(d, t - 15)
            )
            if not now or not ref:
                continue
            if mode == "skip_roll":
                if d not in ROLL:
                    out[t] = sig
                continue
            chg = now / ref - 1
            if mode.startswith("falling"):
                if chg <= -x:
                    out[t] = sig
            elif chg >= x:
                out[t] = sig
        return {t: s for t, s in out.items() if s}

    return f


def line(lab, tr):
    if not tr:
        return f"  {lab:34s} no trades"
    h1 = sum(x["net"] for x in tr if x["d"] in eng.half1)
    h2 = tot(tr) - h1
    ce = sum(x["net"] for x in tr if x["side"] == "ce")
    pe = tot(tr) - ce
    nce = sum(x["side"] == "ce" for x in tr)
    byd = {}
    for x in tr:
        byd[x["d"]] = byd.get(x["d"], 0) + x["net"]
    top2 = sum(sorted(byd.values())[-2:])
    ok = "both halves +" if h1 > 0 and h2 > 0 else ""
    return (
        f"  {lab:34s} n {len(tr):3d} ₹/tr {tot(tr)/len(tr):5.0f} tot ₹{tot(tr):6.0f}  "
        f"H1 {h1:6.0f} H2 {h2:6.0f}"
        f"  CE({nce}) {ce:6.0f} PE({len(tr)-nce}) {pe:6.0f}  "
        f"w/o best 2 days ₹{tot(tr)-top2:6.0f}  {ok}"
    )


def main(spot_pkl: str) -> None:
    eng.bootstrap(spot_pkl)
    variants = [("no OI check", None, 0)] + [
        (f"OI up >= {x:.1%} since open", "open", x) for x in (0.0, 0.005, 0.01, 0.02)
    ] + [
        (f"OI up >= {x:.1%} last 15m", "15m", x) for x in (0.0, 0.002, 0.005)
    ] + [("OI DOWN since open (covering)", "falling", 0.0)]

    for T, a, b, ex, lab in [
        (100, "09:30", "11:59", (0.2, 0.1, 30), "v2 live: T100 morning 20/10/30"),
        (50, "09:30", "14:44", (0.2, 0.1, 30), "T50 full day 20/10/30"),
        (50, "09:30", "14:44", (0.5, 0.25, 90), "T50 full day 50/25/90"),
        (100, "09:30", "14:44", (0.2, 0.1, 30), "T100 full day 20/10/30"),
    ]:
        print(f"\n=== {lab} ===")
        base = trend(T, a, b)
        for vlab, mode, x in variants:
            fn = base if mode is None else confirmed(base, mode, x)
            tr = runx(fn, ex, "none", eng.days)
            print(line(vlab, tr))
        tr = runx(lambda d, base=base: {} if d in ROLL else base(d), ex, "none", eng.days)
        print(line("no OI check, rollover week removed", tr))
        tr = runx(
            lambda d, c=confirmed(base, "open", 0.005): {} if d in ROLL else c(d),
            ex,
            "none",
            eng.days,
        )
        print(line("OI up >=0.5% open, no rollover wk", tr))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 scripts/bt_oi_confirm.py data/spot_cache.pkl")
    main(sys.argv[1])
