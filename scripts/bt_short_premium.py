"""Usage: python3 scripts/bt_short_premium.py SPOT_PKL PX_PKL [--only LABEL]

Search short-premium structures, one per day, with a fit/test split:
pick on the first half of the days, score on the second half (never seen).

Structures: short ATM straddle; short iron fly (sell ATM straddle, buy CE/PE
wings W pts out). Exits on the structure's P&L vs entry credit: take-profit at
X% of credit, stop when the loss reaches Y% of credit, else flat 15:14.
Filters: all days / expiry day only / not expiry day.
1 lot, 0.5 pt slip per leg per side + Kite charges. Every combo is counted.

  python3 scripts/bt_short_premium.py data/kite_spot_cache.pkl data/px_est_kite_year.pkl
  python3 scripts/bt_short_premium.py data/spot_cache.pkl data/px_cache.pkl --only "fly300 12:00 tp50/sl50 all"
"""

from __future__ import annotations

import argparse
import itertools
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import bt_common as eng  # noqa: E402
from atlas_lite.agent_gates import NSE_HOLIDAYS  # noqa: E402
from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402

STRIKE_RE = re.compile(r"(\d{5})(CE|PE)$")
ENTRIES = ("09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00", "14:30")
STRUCTS = (("straddle", None), ("fly200", 200), ("fly300", 300))
EXITS = ((None, None), (0.5, 1.0), (0.5, 0.5), (0.3, 0.3))  # (take-profit, stop) as fraction of credit
FILTERS = ("all", "expiry", "not_expiry")
MIN_FIT_TRADES = 30


def chain(row: dict) -> dict[tuple[int, str], str]:
    out = {}
    for sym in row:
        m = STRIKE_RE.search(sym)
        if m:
            out[(int(m.group(1)), m.group(2))] = sym
    return out


def is_expiry(d: str) -> bool:
    """Weekly NIFTY expiry: Tuesday, or the session before when Tuesday is a holiday."""
    day = date.fromisoformat(d)
    tue = day + timedelta(days=(1 - day.weekday()) % 7)
    while tue.isoformat() in NSE_HOLIDAYS or tue.weekday() >= 5:
        tue -= timedelta(days=1)
    return tue == day


def trade(d: str, at: str, wing: int | None, ex: tuple) -> dict | None:
    px = eng.PX[d]
    row = px.get(at) or {}
    atm = row.get("_atm")
    if not atm:
        return None
    k = int(STRIKE_RE.search(atm[0]).group(1))
    ch = chain(row)
    legs = [(atm[0], -1), (atm[1], -1)]
    if wing:
        ce_w, pe_w = ch.get((k + wing, "CE")), ch.get((k - wing, "PE"))
        if not ce_w or not pe_w:
            return None
        legs += [(ce_w, +1), (pe_w, +1)]
    entry = {s: row.get(s) for s, _ in legs}
    if any(v is None or v <= 0 for v in entry.values()):
        return None
    credit = -sum(sign * entry[s] for s, sign in legs)
    if credit <= 0:
        return None
    tp, sl = ex
    last = dict(entry)
    t0 = eng.h2m(at)
    why = "close"
    for u in range(t0 + 1, eng.SQ + 1):
        r = px.get(eng.m2h(u)) or {}
        for s, _ in legs:
            if r.get(s) is not None:
                last[s] = r[s]
        value = -sum(sign * last[s] for s, sign in legs)  # cost to buy the structure back
        gain = credit - value
        if tp is not None and gain >= tp * credit:
            why = "target"
            break
        if sl is not None and gain <= -sl * credit:
            why = "stop"
            break
    q, slip = eng.QTY, eng.SLIP
    fills = []
    for s, sign in legs:
        if sign < 0:
            fills += [(entry[s] - slip, q, "sell"), (last[s] + slip, q, "buy")]
        else:
            fills += [(entry[s] + slip, q, "buy"), (last[s] - slip, q, "sell")]
    gross = (credit - (-sum(sign * last[s] for s, sign in legs)) - 2 * slip * len(legs)) * q
    return {"d": d, "net": gross - kite_nfo_charges(fills)["total"], "why": why, "credit": credit}


def run(days: list[str], at: str, wing: int | None, ex: tuple, flt: str) -> list[dict]:
    out = []
    for d in days:
        e = is_expiry(d)
        if (flt == "expiry" and not e) or (flt == "not_expiry" and e):
            continue
        r = trade(d, at, wing, ex)
        if r:
            out.append(r)
    return out


def label(name: str, at: str, ex: tuple, flt: str) -> str:
    exl = "hold" if ex[0] is None else f"tp{ex[0]*100:.0f}/sl{ex[1]*100:.0f}"
    return f"{name} {at} {exl} {flt}"


def summary(tr: list[dict]) -> str:
    if not tr:
        return "no trades"
    v = [x["net"] for x in tr]
    tot = sum(v)
    gw = sum(x for x in v if x > 0)
    gl = -sum(x for x in v if x < 0)
    byd = sorted(v)
    return (f"n {len(v):3d}  tot ₹{tot:8.0f}  ₹/tr {tot/len(v):5.0f}  win {sum(x>0 for x in v)/len(v):4.0%}  "
            f"PF {gw/gl if gl else 0:4.2f}  worst ₹{byd[0]:6.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("spot_pkl")
    ap.add_argument("px_pkl")
    ap.add_argument("--only", help="run one combo label on all days (e.g. a pick checked on real tape)")
    args = ap.parse_args()
    eng.bootstrap(args.spot_pkl, args.px_pkl)
    days = eng.days
    combos = list(itertools.product(STRUCTS, ENTRIES, EXITS, FILTERS))
    if args.only:
        for (name, wing), at, ex, flt in combos:
            if label(name, at, ex, flt) == args.only:
                tr = run(days, at, wing, ex, flt)
                print(f"{args.only} on {len(days)} days {days[0]}..{days[-1]}: {summary(tr)}")
                return
        raise SystemExit(f"unknown combo {args.only!r}")
    half = len(days) // 2
    fit_days, test_days = days[:half], days[half:]
    print(f"{len(combos)} combos · fit {fit_days[0]}..{fit_days[-1]} ({len(fit_days)}d) · "
          f"test {test_days[0]}..{test_days[-1]} ({len(test_days)}d)")
    rows = []
    for (name, wing), at, ex, flt in combos:
        fit = run(fit_days, at, wing, ex, flt)
        test = run(test_days, at, wing, ex, flt)
        rows.append((label(name, at, ex, flt), fit, test))
    tot = lambda tr: sum(x["net"] for x in tr)  # noqa: E731
    ok = [r for r in rows if len(r[1]) >= MIN_FIT_TRADES]
    ok.sort(key=lambda r: tot(r[1]), reverse=True)
    print(f"\ntop 10 picked on FIT half, then their unseen TEST half:")
    for lab, fit, test in ok[:10]:
        print(f"  {lab:34s} fit ₹{tot(fit):8.0f} (n {len(fit):3d}) -> TEST {summary(test)}")
    both = [r for r in ok if tot(r[1]) > 0 and tot(r[2]) > 0]
    print(f"\npositive in BOTH halves: {len(both)}/{len(ok)} combos with >= {MIN_FIT_TRADES} fit trades")
    for lab, fit, test in sorted(both, key=lambda r: -(tot(r[1]) + tot(r[2])))[:10]:
        print(f"  {lab:34s} fit ₹{tot(fit):8.0f}  test ₹{tot(test):8.0f}  all: {summary(fit + test)}")


if __name__ == "__main__":
    main()
