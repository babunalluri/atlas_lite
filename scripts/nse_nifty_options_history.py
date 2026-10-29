#!/usr/bin/env python3
"""Download NIFTY option end-of-day prices from NSE F&O bhavcopies.

NSE publishes a bhavcopy for every trading day with close and settlement prices
for every option contract, expired ones included. This keeps only NIFTY options
in the nearest two expiries per day, so positional weekly strategies can be
tested on real prices from the start of NIFTY weeklies (Feb 2019).

Writes data/nifty_opt_eod.pkl:
  {"days": {"YYYY-MM-DD": [(expiry, strike, "CE"|"PE", close, settle, contracts, underlying|None), ...]},
   "missing": ["YYYY-MM-DD", ...]}   # weekdays with no bhavcopy (holidays)
Resumable: days already in the file are skipped.

Usage:
  python3 scripts/nse_nifty_options_history.py
  python3 scripts/nse_nifty_options_history.py --start 2019-02-11 --end 2026-10-28
  python3 scripts/nse_nifty_options_history.py --retry-missing   # re-try days recorded as missing
                                                                 # (a sleep or network drop records a trading day as missing)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import pickle
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.nse_fo_bhav import _f, _parse_expiry, _s, fetch_fo_bhav_csv  # noqa: E402

OUT = ROOT / "data" / "nifty_opt_eod.pkl"
KEEP_EXPIRIES = 2
PAUSE_S = 0.4


def parse_day(text: str, day: date) -> list[tuple]:
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        if _s(row, "TckrSymb", "SYMBOL") != "NIFTY":
            continue
        opt = _s(row, "OptnTp", "OPTION_TYP").upper()
        inst = _s(row, "FinInstrmTp", "INSTRUMENT").upper()
        if opt not in ("CE", "PE") or (inst and inst not in ("IDO", "OPTIDX")):
            continue
        exp = _parse_expiry(_s(row, "XpryDt", "EXPIRY_DT"))
        strike = _f(row, "StrkPric", "STRIKE_PR")
        if exp is None or strike is None or exp < day:
            continue
        rows.append((
            exp.isoformat(),
            int(round(strike)),
            opt,
            _f(row, "ClsPric", "CLOSE"),
            _f(row, "SttlmPric", "SETTLE_PR"),
            _f(row, "TtlTradgVol", "CONTRACTS") or 0.0,
            _f(row, "UndrlygPric"),
        ))
    keep = sorted({r[0] for r in rows})[:KEEP_EXPIRIES]
    return [r for r in rows if r[0] in keep]


def load() -> dict:
    if OUT.is_file():
        return pickle.loads(OUT.read_bytes())
    return {"days": {}, "missing": []}


def save(store: dict) -> None:
    tmp = OUT.with_suffix(".tmp")
    tmp.write_bytes(pickle.dumps(store))
    tmp.replace(OUT)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-02-11")  # first NIFTY weekly expiry week
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--retry-missing", action="store_true", help="fetch weekdays recorded as missing again")
    args = ap.parse_args()
    store = load()
    if args.retry_missing:
        print(f"retrying {len(store['missing'])} missing weekdays", flush=True)
        store["missing"] = []
    done = set(store["days"]) | set(store["missing"])
    day, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    todo = []
    while day <= end:
        if day.weekday() < 5 and day.isoformat() not in done:
            todo.append(day)
        day += timedelta(days=1)
    print(f"{len(store['days'])} days cached, {len(todo)} to fetch", flush=True)
    async with httpx.AsyncClient(timeout=40) as client:
        for i, d in enumerate(todo, 1):
            text = None
            for attempt in range(3):
                try:
                    text = await fetch_fo_bhav_csv(client, d, None, persist=False)
                    break
                except Exception as exc:  # noqa: BLE001 — network/zip errors: retry, then record as missing
                    print(f"  {d} attempt {attempt + 1}: {exc}", flush=True)
                    await asyncio.sleep(3 * (attempt + 1))
            if text:
                store["days"][d.isoformat()] = parse_day(text, d)
            else:
                store["missing"].append(d.isoformat())
            if i % 25 == 0 or i == len(todo):
                save(store)
                print(f"  {i}/{len(todo)} through {d} · days {len(store['days'])} · missing {len(store['missing'])}", flush=True)
            await asyncio.sleep(PAUSE_S)
    save(store)
    print(f"done: {len(store['days'])} trading days, {len(store['missing'])} weekdays without a bhavcopy")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
