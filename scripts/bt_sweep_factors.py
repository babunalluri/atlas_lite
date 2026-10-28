"""Usage: python3 scripts/build_spot_cache.py && python3 scripts/bt_sweep_factors.py data/spot_cache.pkl

Needs ``data/px_cache.pkl`` (or legacy repo-root ``px_cache.pkl``).

Liquidity-sweep setup + factor filters on the tape, full day by time band.

Book rules as live v2: 1 lot, +20%/-10%/30m, max 3/day, -2000 day stop, one position,
side blocks (stop 15m, loss 10m, win 5m), side lock after stop <= -400 or 2 stops.
Costs: 0.5 pt slip per leg + Kite charges.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import bt_common as eng  # noqa: E402


def main(spot_pkl: str) -> None:
    eng.bootstrap(spot_pkl)
    print(f"{len(eng.days)} days {eng.days[0]}..{eng.days[-1]}; halves split at {max(eng.half1)}\n")
    print("=== V2 (spot vs open >= 100), full day, by band ===")
    v2 = eng.run(lambda d: eng.v2_signals(d, "09:30", "14:44"))
    print(eng.summ(v2, "v2 09:30-14:44"))
    eng.bands(v2)
    print("\n=== SWEEP variants (X pts beyond level, N min to reclaim), 09:30-14:44, skip vs v2 trend ===")
    for X in (5, 10, 20):
        for N in (3, 5, 10):
            tr = eng.run(lambda d, X=X, N=N: eng.sweep_signals(d, "09:30", "14:44", X, N, True))
            print(eng.summ(tr, f"X={X:2d} N={N:2d}"))
    print("\n=== SWEEP X=10 N=5 detail ===")
    base = lambda d: eng.sweep_signals(d, "09:30", "14:44", 10, 5, True)
    tr = eng.run(base)
    print(eng.summ(tr, "base (no-conflict)"))
    eng.bands(tr)
    tr2 = eng.run(lambda d: eng.sweep_signals(d, "09:30", "14:44", 10, 5, False))
    print(eng.summ(tr2, "base incl. vs-trend"))
    for lv in ("OR", "PD"):
        t3 = eng.run(
            lambda d, lv=lv: eng.sweep_signals(d, "09:30", "14:44", 10, 5, True, only=(lv,))
        )
        print(eng.summ(t3, f"levels {lv} only"))
    print("\n=== FILTERS (each alone) ===")
    for name in eng.FILTERS:
        if name == "none":
            continue
        for lab, fn in (("v2", lambda d: eng.v2_signals(d, "09:30", "14:44")), ("sweep", base)):
            t = eng.run(fn, name)
            print(eng.summ(t, f"{lab} + {name}"))
            eng.bands(t, "      ")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 scripts/bt_sweep_factors.py data/spot_cache.pkl")
    main(sys.argv[1])
