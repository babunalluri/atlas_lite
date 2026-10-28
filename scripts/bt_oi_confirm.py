"""Usage: python3 scripts/bt_oi_confirm.py data/spot_cache.pkl

Trend entries (spot vs open) with futures-OI confirmation: take the trade only when
NIFTY futures OI is building (new positions) while price moves. Price down + OI up =
short build-up -> PE; price up + OI up = long build-up -> CE. Book rules and costs
as scripts/bt_sweep_factors.py; 2-fold cross-check by halves.
"""
import sys
from datetime import date
src = open(__file__.replace("bt_oi_confirm.py", "bt_setup_search.py")).read().split("FLT = (")[0]
exec(src.replace('__file__.replace("bt_setup_search.py", "bt_sweep_factors.py")', '__file__.replace("bt_oi_confirm.py", "bt_sweep_factors.py")'))

ROLL = {"2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"}  # monthly expiry week: OI = rollover


def oi_at(d, t):
    for u in range(t, t - 5, -1):
        v = (SP[d].get(m2h(u)) or {}).get("fut_oi")
        if v: return v
    return None


def confirmed(base_fn, mode, x):
    def f(d):
        out = {}
        for t, sig in base_fn(d).items():
            now = oi_at(d, t)
            ref = oi_at(d, h2m("09:16")) if mode in ("open", "falling") else oi_at(d, t - 15)
            if not now or not ref: continue
            if mode == "skip_roll":
                out[t] = sig if d not in ROLL else None
                continue
            chg = now / ref - 1
            if mode.startswith("falling"):
                if chg <= -x: out[t] = sig
            elif chg >= x: out[t] = sig
        return {t: s for t, s in out.items() if s}
    return f


def tot(tr): return sum(x["net"] for x in tr)


def line(lab, tr):
    if not tr: return f"  {lab:34s} no trades"
    h1 = sum(x["net"] for x in tr if x["d"] in half1); h2 = tot(tr) - h1
    ce = sum(x["net"] for x in tr if x["side"] == "ce"); pe = tot(tr) - ce
    nce = sum(x["side"] == "ce" for x in tr)
    byd = {}
    for x in tr: byd[x["d"]] = byd.get(x["d"], 0) + x["net"]
    top2 = sum(sorted(byd.values())[-2:])
    ok = "both halves +" if h1 > 0 and h2 > 0 else ""
    return (f"  {lab:34s} n {len(tr):3d} ₹/tr {tot(tr)/len(tr):5.0f} tot ₹{tot(tr):6.0f}  H1 {h1:6.0f} H2 {h2:6.0f}"
            f"  CE({nce}) {ce:6.0f} PE({len(tr)-nce}) {pe:6.0f}  w/o best 2 days ₹{tot(tr)-top2:6.0f}  {ok}")


VARIANTS = [("no OI check", None, 0)] + \
    [(f"OI up >= {x:.1%} since open", "open", x) for x in (0.0, 0.005, 0.01, 0.02)] + \
    [(f"OI up >= {x:.1%} last 15m", "15m", x) for x in (0.0, 0.002, 0.005)] + \
    [("OI DOWN since open (covering)", "falling_open", 0.0)]

for T, a, b, ex, lab in [(100, "09:30", "11:59", (0.2, 0.1, 30), "v2 live: T100 morning 20/10/30"),
                         (50, "09:30", "14:44", (0.2, 0.1, 30), "T50 full day 20/10/30"),
                         (50, "09:30", "14:44", (0.5, 0.25, 90), "T50 full day 50/25/90"),
                         (100, "09:30", "14:44", (0.2, 0.1, 30), "T100 full day 20/10/30")]:
    print(f"\n=== {lab} ===")
    base = trend(T, a, b)
    for vlab, mode, x in VARIANTS:
        fn = base if mode is None else confirmed(base, mode if mode != "falling_open" else "falling_open", x)
        if mode == "falling_open":
            fn = confirmed(base, "falling", 0.0)
            fn2 = lambda d, fn=fn: fn(d)
        tr = runx(fn, ex, "none", days)
        print(line(vlab, tr))
    tr = runx(lambda d, base=base: {} if d in ROLL else base(d), ex, "none", days)
    print(line("no OI check, rollover week removed", tr))
    tr = runx(lambda d, c=confirmed(base, "open", 0.005): {} if d in ROLL else c(d), ex, "none", days)
    print(line("OI up >=0.5% open, no rollover wk", tr))
