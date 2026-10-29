"""Usage: python3 scripts/bt_straddle.py SPOT_PKL PX_PKL [--side long|short] [--at 09:15,09:30]

ATM straddle, one per day: buy (long) or sell (short) CE + PE at a fixed time,
exit on combined premium target / stop / hold, flat 15:14. Long: target = premium
up X%, stop = down Y%. Short: target = premium down X%, stop = up Y%.
1 lot, 0.5 pt slip per leg per side plus Kite charges. Every combo is printed.

  python3 scripts/bt_straddle.py data/spot_cache.pkl data/px_cache.pkl                 # 23 recorded days, real premiums
  python3 scripts/bt_straddle.py data/spot_cache.pkl data/px_est_recorded.pkl          # same days, estimated
  python3 scripts/bt_straddle.py data/kite_spot_cache.pkl data/px_est_kite_year.pkl    # Kite year, estimated
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import bt_common as eng  # noqa: E402
from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402

ENTRIES = ("09:20", "09:30", "10:00", "11:00", "12:00", "13:00")
EXITS = [  # (target, stop, hold min); None = no target/stop
    (0.20, 0.15, 60),
    (0.30, 0.20, 120),
    (0.50, 0.30, 999),
    (None, None, 999),
]


def straddle(d: str, at: str, ex, short: bool = False) -> dict | None:
    px = eng.PX[d]
    t = eng.h2m(at)
    row = px.get(at) or {}
    atm = row.get("_atm")
    if not atm or not row.get(atm[0]) or not row.get(atm[1]):
        return None
    ce_s, pe_s = atm
    e_ce, e_pe = row[ce_s], row[pe_s]
    e = e_ce + e_pe
    tg, sp, hold = ex
    end = min(eng.SQ, t + hold)
    last_ce, last_pe = e_ce, e_pe
    why, xt = "time" if hold < 999 else "close", end
    for u in range(t + 1, end + 1):
        r = px.get(eng.m2h(u)) or {}
        last_ce = r.get(ce_s, last_ce)
        last_pe = r.get(pe_s, last_pe)
        v = last_ce + last_pe
        gain = (e - v) if short else (v - e)
        if tg is not None and gain >= e * tg:
            why, xt = "target", u
            break
        if sp is not None and gain <= -e * sp:
            why, xt = "stop", u
            break
    q, s = eng.QTY, eng.SLIP
    if short:
        fills = [(e_ce - s, q, "sell"), (e_pe - s, q, "sell"), (last_ce + s, q, "buy"), (last_pe + s, q, "buy")]
        gross = (e - last_ce - last_pe - 4 * s) * q
    else:
        fills = [(e_ce + s, q, "buy"), (e_pe + s, q, "buy"), (last_ce - s, q, "sell"), (last_pe - s, q, "sell")]
        gross = (last_ce + last_pe - e - 4 * s) * q
    return {"d": d, "net": gross - kite_nfo_charges(fills)["total"], "why": why, "entry": e, "t": xt}


def summary(tr: list[dict]) -> str:
    if not tr:
        return "no trades"
    v = [x["net"] for x in tr]
    tot = sum(v)
    gw = sum(x for x in v if x > 0)
    gl = -sum(x for x in v if x < 0)
    h1 = sum(x["net"] for x in tr if x["d"] in eng.half1)
    return (f"n {len(v):3d}  tot ₹{tot:8.0f}  ₹/day {tot / len(v):5.0f}  win {sum(x > 0 for x in v) / len(v):4.0%}  "
            f"PF {gw / gl if gl else 0:4.2f}  H1 {h1:7.0f} H2 {tot - h1:7.0f}  worst ₹{min(v):6.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("spot_pkl")
    ap.add_argument("px_pkl")
    ap.add_argument("--side", choices=("long", "short"), default="long")
    ap.add_argument("--at", default=",".join(ENTRIES), help="comma-separated entry times")
    ap.add_argument("--max-hold", type=int, help="minutes; switches to a tight-exit grid for short holds")
    args = ap.parse_args()
    exits = EXITS
    if args.max_hold:
        h = args.max_hold
        exits = [(0.05, 0.05, h), (0.10, 0.05, h), (0.10, 0.10, h), (0.20, 0.10, h), (None, None, h)]
    entries = tuple(args.at.split(","))
    short = args.side == "short"
    eng.bootstrap(args.spot_pkl, args.px_pkl)
    print(f"{args.side} straddle · {len(eng.days)} days {eng.days[0]}..{eng.days[-1]}  ({len(entries) * len(exits)} combos)")
    best = None
    for at in entries:
        for ex in exits:
            tr = [r for d in eng.days if (r := straddle(d, at, ex, short))]
            lab = f"{'sell' if short else 'buy'} {at}  exits {('hold to 15:14' if ex[2] == 999 else f'hold {ex[2]}m') if ex[0] is None else f'+{ex[0]:.0%}/-{ex[1]:.0%}/' + ('close' if ex[2] == 999 else f'{ex[2]}m')}"
            print(f"  {lab:34s} {summary(tr)}")
            tot = sum(x["net"] for x in tr)
            if best is None or tot > best[0]:
                best = (tot, lab, tr)
    tot, lab, tr = best
    print(f"\nbest combo: {lab} (best of {len(entries) * len(exits)}, so optimistic)")
    for mo in sorted({d[:7] for d in eng.days}):
        t = [x for x in tr if x["d"][:7] == mo]
        if t:
            print(f"  {mo}  n {len(t):2d}  ₹{sum(x['net'] for x in t):7.0f}")
    exp = [x for x in tr if eng.SP[x["d"]].get("15:00", {}).get("expiry") == x["d"]]
    if exp:
        print(f"  expiry days only: {summary(exp)}")


if __name__ == "__main__":
    main()
