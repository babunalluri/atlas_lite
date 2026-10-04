#!/usr/bin/env python3
"""Backtest improved nifty_vwap_long ideas on 5m bars.

Improvements (from strategy notes):
  1) Wider target: +1.5σ or +2σ (vs +1σ)
  2) Higher entry headroom: ≥15–20 pts to target (vs 5)
  3) Trailing stop after profit (vs fixed −1σ only)

Baseline paper entry geometry kept: ST up, close>VWAP, pullback −0.5σ.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.minute_bars import is_cash_session_minute  # noqa: E402
from atlas_lite.paper_vwap_long import (  # noqa: E402
    ENTRY_AFTER,
    ENTRY_UNTIL,
    MAX_ENTRIES_PER_DAY,
    PAPER_BAR_MINUTES,
    QTY,
    SQUARE_OFF,
    aggregate_bars,
    scan_vwap_signals,
    spot_order_charges,
    supertrend_series,
)


def _load_bars(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    bars = raw["bars"] if isinstance(raw, dict) and "bars" in raw else raw
    out = []
    for b in bars:
        t = str(b.get("t") or "").replace("T", " ")[:16]
        if not t or not is_cash_session_minute(t):
            continue
        try:
            out.append(
                {
                    "t": t,
                    "o": float(b["o"]),
                    "h": float(b["h"]),
                    "l": float(b["l"]),
                    "c": float(b["c"]),
                    "v": float(b.get("v") or 0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x["t"])


def _ht(hm: str) -> tuple[int, int]:
    h, m = map(int, hm.split(":"))
    return h, m


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "wins": 0, "wr": 0.0, "gross_wr": 0.0, "net": 0.0, "avg": 0.0, "pf": 0.0}
    nets = [float(t["net"]) for t in trades]
    gross = [float(t["gross"]) for t in trades]
    wins = [p for p in nets if p > 0]
    g_wins = [p for p in gross if p > 0]
    losses = [p for p in nets if p <= 0]
    gl = abs(sum(losses)) or 1e-9
    return {
        "n": len(nets),
        "wins": len(wins),
        "wr": round(100 * len(wins) / len(nets), 1),
        "gross_wr": round(100 * len(g_wins) / len(gross), 1),
        "net": round(sum(nets), 2),
        "avg": round(sum(nets) / len(nets), 2),
        "pf": round(sum(wins) / gl, 2),
        "by_reason": {
            k: sum(1 for t in trades if t["reason"] == k)
            for k in sorted({t["reason"] for t in trades})
        },
    }


def simulate(
    bars_5m: list[dict],
    *,
    target_sigma: float = 2.0,
    min_headroom: float = 15.0,
    trail: bool = True,
    trail_arm_sigma: float = 0.5,
    trail_gap_sigma: float = 0.75,
    initial_stop_sigma: float = 1.0,
) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for b in bars_5m:
        by_day[b["t"][:10]].append(b)

    trades: list[dict] = []
    for day, sess in sorted(by_day.items()):
        if len(sess) < 8:
            continue
        rows = scan_vwap_signals(sess)
        st = {str(r.get("t")): r.get("dir") for r in supertrend_series(sess)}
        pos = None
        entries = 0
        for i, row in enumerate(rows):
            t = str(row["t"])
            hm = t[11:16]
            bar = sess[i]
            vwap = float(row["vwap"])
            sigma = float(row["sigma"])

            if pos is not None:
                # update peak
                pos["peak"] = max(float(pos["peak"]), float(bar["h"]))
                # dynamic levels
                tgt = vwap + target_sigma * sigma
                init_stop = vwap - initial_stop_sigma * sigma
                stop = float(pos["stop"])
                # arm trail once peak ≥ entry + trail_arm_sigma*σ_entry
                if trail:
                    armed = pos["peak"] >= float(pos["entry"]) + trail_arm_sigma * float(
                        pos["sigma0"]
                    )
                    if armed:
                        # trail under peak by trail_gap_sigma * current σ; never loosen
                        trail_stop = pos["peak"] - trail_gap_sigma * sigma
                        # also ratchet to VWAP once above VWAP+0.25σ
                        if pos["peak"] >= vwap + 0.25 * sigma:
                            trail_stop = max(trail_stop, vwap)
                        stop = max(stop, trail_stop, init_stop)
                        pos["stop"] = stop
                else:
                    stop = init_stop
                    pos["stop"] = stop

                reason = fill = None
                if float(bar["l"]) <= stop:
                    reason, fill = "stop", stop
                elif float(bar["h"]) >= tgt and tgt > float(pos["entry"]):
                    reason, fill = "target", tgt
                elif st.get(t) == -1:
                    reason, fill = "st_flip", float(bar["c"])
                elif _ht(hm) >= SQUARE_OFF:
                    reason, fill = "time", float(bar["c"])
                if reason is not None and fill is not None:
                    ch_close = float(spot_order_charges(fill, QTY, "sell")["total"])
                    gross = (fill - float(pos["entry"])) * QTY
                    net = round(gross - float(pos["ch_open"]) - ch_close, 2)
                    trades.append(
                        {
                            "day": day,
                            "entry_hm": pos["hm"],
                            "exit_hm": hm,
                            "gross": round(gross, 2),
                            "net": net,
                            "reason": reason,
                        }
                    )
                    pos = None
                continue

            if _ht(hm) >= SQUARE_OFF:
                continue
            if not ((_ht(hm) >= ENTRY_AFTER) and (_ht(hm) < ENTRY_UNTIL)):
                continue
            if entries >= MAX_ENTRIES_PER_DAY:
                continue
            if not row.get("long_setup"):
                continue
            if st.get(t) != 1:
                continue
            spot = float(bar["c"])
            tgt = vwap + target_sigma * sigma
            if spot >= tgt:
                continue
            if (tgt - spot) < min_headroom:
                continue
            ch_open = float(spot_order_charges(spot, QTY, "buy")["total"])
            pos = {
                "entry": spot,
                "hm": hm,
                "ch_open": ch_open,
                "sigma0": sigma,
                "peak": spot,
                "stop": vwap - initial_stop_sigma * sigma,
            }
            entries += 1
        if pos is not None:
            last = sess[-1]
            fill = float(last["c"])
            ch_close = float(spot_order_charges(fill, QTY, "sell")["total"])
            gross = (fill - float(pos["entry"])) * QTY
            trades.append(
                {
                    "day": day,
                    "entry_hm": pos["hm"],
                    "exit_hm": last["t"][11:16],
                    "gross": round(gross, 2),
                    "net": round(gross - float(pos["ch_open"]) - ch_close, 2),
                    "reason": "eod",
                }
            )
    return trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=Path, required=True)
    ap.add_argument("--agg", type=int, default=0)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    bars = _load_bars(args.bars)
    if args.agg > 1:
        bars = aggregate_bars(bars, args.agg)
        print(f"agg={args.agg}m bars={len(bars)}", flush=True)
    else:
        # auto 1m→5m
        if len(bars) >= 3:
            from datetime import datetime

            try:
                a = datetime.fromisoformat(bars[0]["t"].replace(" ", "T"))
                b = datetime.fromisoformat(bars[1]["t"].replace(" ", "T"))
                if (b - a).total_seconds() <= 90:
                    bars = aggregate_bars(bars, PAPER_BAR_MINUTES)
                    print(f"auto-agg 5m bars={len(bars)}", flush=True)
                else:
                    print(f"native bars={len(bars)}", flush=True)
            except ValueError:
                print(f"bars={len(bars)}", flush=True)

    # Baseline (current paper)
    baseline = simulate(
        bars, target_sigma=1.0, min_headroom=5.0, trail=False, initial_stop_sigma=1.0
    )
    print("\n=== BASELINE (current paper: +1σ / headroom5 / fixed −1σ) ===")
    print(json.dumps(_stats(baseline), indent=2))

    grid = []
    for tgt, head, trail in itertools.product(
        [1.5, 2.0],
        [15.0, 20.0],
        [True],
    ):
        trades = simulate(
            bars,
            target_sigma=tgt,
            min_headroom=head,
            trail=trail,
            trail_arm_sigma=0.5,
            trail_gap_sigma=0.75,
            initial_stop_sigma=1.0,
        )
        s = _stats(trades)
        s["cfg"] = {
            "target_sigma": tgt,
            "min_headroom": head,
            "trail": trail,
        }
        grid.append(s)

    # also trail + 2σ + 15 without ST? keep ST
    grid.sort(key=lambda r: (r["net"], r["wr"], r["gross_wr"]), reverse=True)

    print("\n=== IMPROVED VARIANTS (ranked by net) ===")
    print(f"{'rk':>2} {'net':>9} {'WR':>6} {'gWR':>6} {'n':>3} {'PF':>5}  cfg")
    for i, r in enumerate(grid, 1):
        c = r["cfg"]
        print(
            f"{i:2} {r['net']:+9.0f} {r['wr']:6.1f} {r['gross_wr']:6.1f} {r['n']:3} {r['pf']:5.2f}  "
            f"tgt={c['target_sigma']}σ head≥{c['min_headroom']:.0f} trail={c['trail']}"
        )

    best = grid[0] if grid else None
    # Recommended: best net among n>=10, else best overall
    solid = [r for r in grid if r["n"] >= 10]
    pick = solid[0] if solid else best

    print("\n=== RECOMMENDED IMPROVED ===")
    print(json.dumps(pick, indent=2))

    out = {
        "baseline": _stats(baseline),
        "variants": grid,
        "recommended": pick,
        "source": str(args.bars),
        "n_bars": len(bars),
    }
    if args.out:
        args.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
        print("saved", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
