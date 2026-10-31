"""Usage: python3 scripts/paper_vs_backtest.py [--fetch] [--ledgers data/oci]

Each paper trade next to what its backtest rule made on the same days, so a gap
between paper and the backtest shows within days, not months.

- Scalper v3: the 13:00 ATM straddle sale (tp30/sl30, flat 15:14) replayed on REAL
  recorded premiums (data/px_real.pkl, with data/px_cache.pkl for older days) by
  scripts/bt_short_premium.trade, the engine behind the v3 backtest.
- Weekly strangle / condor: the same expiry from scripts/bt_iron_condor_eod.run on NSE
  end-of-day prices (data/nifty_opt_eod.pkl, slip 1 pt). Until the expiry's bhavcopy is
  downloaded (scripts/nse_nifty_options_history.py) the row says pending.

--fetch first rebuilds the real prices on the server (scripts/build_px_real.py) and
copies them plus the paper ledgers here (ledgers into --ledgers, never over data/paper_*).
Host and key as in scripts/deploy-oci.sh (ATLAS_OCI_HOST, ATLAS_OCI_KEY, ATLAS_LITE_OCI_DIR).
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import bt_common as eng  # noqa: E402
import bt_short_premium as bsp  # noqa: E402

LEDGERS = ("paper_scalper_v3.jsonl", "paper_weekly_strangle.jsonl", "paper_weekly_condor5.jsonl")
V3_RULE = ("13:00", None, (0.3, 0.3))
WEEKLY = {"weekly_strangle": ("strangle", 0.02), "weekly_condor5": ("condor", 0.05)}
WEEKLY_SLIP = 1.0


def fetch(ledger_dir: Path) -> None:
    key = os.environ.get("ATLAS_OCI_KEY", "/Users/babunalluri/MyWork/OCI/atlas-oci.key.key")
    host = os.environ.get("ATLAS_OCI_HOST", "opc@137.23.61.107")
    remote = os.environ.get("ATLAS_LITE_OCI_DIR", "~/atlas_lite")
    ssh = ["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", host]
    scp = ["scp", "-q", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
    # The builder goes up with each fetch, so this does not wait for a deploy.
    subprocess.run(ssh + ["mkdir -p ~/px_build"], check=True)
    subprocess.run(scp + [str(ROOT / "scripts" / "build_px_real.py"), f"{host}:px_build/"], check=True)
    subprocess.run(ssh + [f"nice -n 10 python3 ~/px_build/build_px_real.py --rec {remote}/data/recordings "
                          f"--out-dir ~/px_build"], check=True)
    subprocess.run(scp + [f"{host}:px_build/px_real.pkl", f"{host}:px_build/spot_real.pkl", str(ROOT / "data")], check=True)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(scp + [f"{host}:{remote}/data/{name}" for name in LEDGERS] + [str(ledger_dir)], check=True)


def trades(path: Path) -> list[dict[str, Any]]:
    """Open events with their close (None while still open)."""
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    current: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "open":
            current = {"open": ev, "close": None}
            out.append(current)
        elif ev.get("event") == "close" and current is not None:
            current["close"] = ev
            current = None
    return out


def rupees(value: float | None) -> str:
    return "—" if value is None else f"₹{value:,.0f}"


def real_premiums() -> dict:
    px: dict = {}
    legacy = ROOT / "data" / "px_cache.pkl"
    if legacy.is_file():
        px.update(pickle.loads(legacy.read_bytes()))
    fresh = ROOT / "data" / "px_real.pkl"
    if fresh.is_file():
        px.update(pickle.loads(fresh.read_bytes()))  # the structure tape wins where both exist
    return px


def scalper_v3(ledger_dir: Path) -> None:
    rows = trades(ledger_dir / "paper_scalper_v3.jsonl")
    print("Scalper v3 — paper vs the backtest rule on real premiums (13:00, tp30/sl30, flat 15:14)")
    if not rows:
        print("  no paper trades yet\n")
        return
    eng.PX = real_premiums()
    print(f"  {'day':10s}  {'gamma':>12s}  {'credit paper/bt':>16s}  {'exit paper/bt':>20s}  {'paper':>8s}  {'backtest':>9s}  {'gap':>7s}")
    paper_sum = bt_sum = 0.0
    n = 0
    by_regime: dict[str, list[float]] = {}
    for t in rows:
        o, c = t["open"], t["close"]
        day = o["day"]
        bt = bsp.trade(day, *V3_RULE) if day in eng.PX else None
        pnl = c.get("pnl") if c else None
        exit_p = (c.get("reason") if c else "open") or "?"
        g = o.get("gamma") or {}
        regime = f"{g['regime']} {g['share']:+.2f}" if g else "—"
        if g and pnl is not None:
            by_regime.setdefault(g["regime"], []).append(pnl)
        if bt is None:
            print(f"  {day}  {regime:>12s}  {o['credit_ltp']:7.2f} / {'—':>6s}  {exit_p:>9s} / {'—':>8s}  {rupees(pnl):>8s}  {'no real px':>9s}")
            continue
        gap = None if pnl is None else pnl - bt["net"]
        print(f"  {day}  {regime:>12s}  {o['credit_ltp']:7.2f} / {bt['credit']:6.2f}  {exit_p:>9s} / {bt['why']:>8s}  "
              f"{rupees(pnl):>8s}  {rupees(bt['net']):>9s}  {rupees(gap):>7s}")
        if pnl is not None:
            paper_sum += pnl
            bt_sum += bt["net"]
            n += 1
    if n:
        print(f"  {n} closed with real prices: paper {rupees(paper_sum)}, backtest {rupees(bt_sum)}, "
              f"gap {rupees(paper_sum - bt_sum)} ({rupees((paper_sum - bt_sum) / n)}/trade)")
    for regime, pnls in sorted(by_regime.items()):
        print(f"  paper by gamma regime — {regime}: {len(pnls)} trades, {rupees(sum(pnls))} ({rupees(sum(pnls) / len(pnls))}/trade)")
    print()


def weekly(ledger_dir: Path) -> None:
    import bt_iron_condor_eod as ic

    days, chains = ic.load()
    print(f"Weekly books — paper vs the backtest rule on NSE end-of-day prices (bhavcopy to {days[-1]}, slip {WEEKLY_SLIP} pt)")
    for book, (structure, wing) in WEEKLY.items():
        rows = trades(ledger_dir / f"paper_{book}.jsonl")
        if not rows:
            print(f"  {book}: no paper trades yet")
            continue
        recent = days[-40:]
        bt_by_exp = {t["exp"]: t for t in ic.run(recent, chains, slip=WEEKLY_SLIP, structure=structure, wing_pct=wing)}
        for t in rows:
            o, c = t["open"], t["close"]
            exp = o["expiry"]
            shorts = sorted(leg["strike"] for leg in o["legs"] if leg["sign"] < 0)
            pnl = c.get("pnl") if c else None
            status = (c.get("reason") if c else "open") or "?"
            bt = bt_by_exp.get(exp)
            if bt is None:
                when = "after expiry" if exp > days[-1] else "no matching trade"
                print(f"  {book:15s} exp {exp}  shorts {shorts}  credit {o['credit_ltp']:.2f}  paper {status} {rupees(pnl)}"
                      f"  | backtest pending ({when}: run scripts/nse_nifty_options_history.py --start {o['day']})")
                continue
            bt_shorts = [bt["strikes"][1], bt["strikes"][2]]
            # The backtest's credit is after slippage on every leg; paper's credit_ltp is before it.
            bt_credit = bt["credit"] + (2 if structure == "strangle" else 4) * WEEKLY_SLIP
            gap = None if pnl is None else pnl - bt["net"]
            print(f"  {book:15s} exp {exp}  shorts paper {shorts} / bt {bt_shorts}  credit {o['credit_ltp']:.2f} / "
                  f"{bt_credit:.2f}  exit {status} / {bt['why']}  paper {rupees(pnl)}  bt {rupees(bt['net'])}  gap {rupees(gap)}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="rebuild real prices on the server and copy ledgers here")
    ap.add_argument("--ledgers", default=str(ROOT / "data" / "oci"), help="directory holding the paper ledgers")
    args = ap.parse_args()
    ledger_dir = Path(args.ledgers)
    if args.fetch:
        fetch(ledger_dir)
    scalper_v3(ledger_dir)
    weekly(ledger_dir)


if __name__ == "__main__":
    main()
