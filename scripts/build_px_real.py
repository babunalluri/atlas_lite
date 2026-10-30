"""Usage: python3 scripts/build_px_real.py [--rec data/recordings] [--out-dir data]

Real per-minute option prices and NIFTY spot from the engine's own recordings, for
backtests on real premiums instead of estimates. Standard library only, so it runs
on the server next to the recordings (see scripts/paper_vs_backtest.py --fetch).

- px_real.pkl:   {day: {"HH:MM": {symbol: last price, "_atm": (ce, pe)}}} from the
  structure tape (structure-*.jsonl[.gz]): ATM call/put plus strikes +-50/100/250,
  last row of each minute, 09:15-15:30. Recorded since 2026-09-24.
- spot_real.pkl: {day: {"HH:MM": {o, h, l, c, iv, vix, ...}}} from the main tape.

Days with fewer than 200 minutes are dropped. Read-only on the recordings.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import pickle
from typing import Any, Iterator


def _lines(path: str) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def premiums(rec: str) -> dict[str, dict[str, dict[str, Any]]]:
    px: dict[str, dict[str, dict[str, Any]]] = {}
    for path in sorted(glob.glob(os.path.join(rec, "structure-*.jsonl*"))):
        for r in _lines(path):
            day, hm = r.get("day"), r.get("hm")
            if not day or not hm or not ("09:15" <= hm <= "15:30"):
                continue
            if not (r.get("ce") and r.get("pe") and r.get("ce_symbol") and r.get("pe_symbol")):
                continue
            row: dict[str, Any] = {
                r["ce_symbol"]: float(r["ce"]),
                r["pe_symbol"]: float(r["pe"]),
                "_atm": (r["ce_symbol"], r["pe_symbol"]),
            }
            for wing in (r.get("wings") or {}).values():
                for side in ("ce", "pe"):
                    if wing.get(side) and wing.get(side + "_symbol"):
                        row[wing[side + "_symbol"]] = float(wing[side])
            px.setdefault(day, {})[hm] = row
    return {d: m for d, m in px.items() if len(m) > 200}


def spot(rec: str) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for path in sorted(glob.glob(os.path.join(rec, "2026-*_*.jsonl*"))):
        day = os.path.basename(path)[:10]
        minutes = out.setdefault(day, {})
        for r in _lines(path):
            hm = str(r.get("ts") or "")[11:16]
            s = r.get("spot")
            if not hm or hm < "09:15" or hm > "15:30" or not s:
                continue
            s = float(s)
            bar = minutes.get(hm)
            if bar is None:
                minutes[hm] = bar = {"o": s, "h": s, "l": s, "c": s}
            bar["h"] = max(bar["h"], s)
            bar["l"] = min(bar["l"], s)
            bar["c"] = s
            feed = r.get("feed") or {}
            for key in ("iv", "vix", "vix_chg", "ivp", "adx"):
                if feed.get(key) is not None:
                    bar[key] = feed[key]
    return {d: m for d, m in out.items() if len(m) > 200}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", default="data/recordings")
    ap.add_argument("--out-dir", default="data")
    args = ap.parse_args()
    px, sp = premiums(args.rec), spot(args.rec)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "px_real.pkl"), "wb") as fh:
        pickle.dump(px, fh)
    with open(os.path.join(args.out_dir, "spot_real.pkl"), "wb") as fh:
        pickle.dump(sp, fh)
    span = lambda d: f"{min(d)}..{max(d)}" if d else "none"  # noqa: E731
    print(f"premium days {len(px)} ({span(px)}) | spot days {len(sp)} ({span(sp)})")


if __name__ == "__main__":
    main()
