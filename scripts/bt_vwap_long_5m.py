#!/usr/bin/env python3
"""Backtest nifty_vwap_long paper rules on 5m bars (OCI / local).

Uses the same geometry + Supertrend(10,3) gates as paper_vwap_long.
P&L = spot points × qty=1 − equity-style charges (same as the book).

Usage:
  python3 scripts/bt_vwap_long_5m.py --bars data/kite_5m_backtest_bars.json
  python3 scripts/bt_vwap_long_5m.py --bars data/minute_bars.json --agg 5
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.minute_bars import is_cash_session_minute  # noqa: E402
from atlas_lite.paper_vwap_long import (  # noqa: E402
    ENTRY_AFTER,
    ENTRY_UNTIL,
    MAX_ENTRIES_PER_DAY,
    MIN_EDGE_BUFFER_PTS,
    PAPER_BAR_MINUTES,
    QTY,
    SQUARE_OFF,
    aggregate_bars,
    scan_vwap_signals,
    spot_order_charges,
    supertrend_series,
)

IST = ZoneInfo("Asia/Kolkata")


def _load_bars(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "bars" in raw:
        bars = raw["bars"]
    elif isinstance(raw, list):
        bars = raw
    else:
        raise ValueError(f"unexpected bars shape in {path}")
    out = []
    for b in bars:
        t = str(b.get("t") or b.get("time") or "")
        if not t or not is_cash_session_minute(t):
            continue
        try:
            out.append(
                {
                    "t": t.replace("T", " ")[:16],
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


def _hm_tuple(hm: str) -> tuple[int, int]:
    h, m = map(int, hm.split(":"))
    return h, m


def _in_entry(hm: str) -> bool:
    hh, mm = _hm_tuple(hm)
    after = (hh, mm) >= ENTRY_AFTER
    until = (hh, mm) < ENTRY_UNTIL
    return after and until


def _square(hm: str) -> bool:
    return _hm_tuple(hm) >= SQUARE_OFF


def run(bars_5m: list[dict]) -> dict:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for b in bars_5m:
        by_day[b["t"][:10]].append(b)

    trades: list[dict] = []
    for day in sorted(by_day):
        sess = by_day[day]
        if len(sess) < 8:
            continue
        rows = scan_vwap_signals(sess)
        st_rows = supertrend_series(sess)
        st_dir = {str(r.get("t")): r.get("dir") for r in st_rows}
        pos = None  # dict
        entries = 0
        for i, row in enumerate(rows):
            t = str(row["t"])
            hm = t[11:16]
            bar = sess[i]
            # manage open
            if pos is not None:
                stop_px = float(row["dn1"])
                up1 = float(row["up1"])
                reason = None
                fill = None
                if float(bar["l"]) <= stop_px:
                    reason, fill = "stop", stop_px
                elif float(bar["h"]) >= up1 and up1 > float(pos["entry"]):
                    reason, fill = "signal_s", up1
                elif st_dir.get(t) == -1:
                    reason, fill = "st_flip", float(bar["c"])
                elif _square(hm):
                    reason, fill = "time", float(bar["c"])
                if reason is not None and fill is not None:
                    ch_open = float(pos["charges_open"])
                    ch_close = float(spot_order_charges(fill, QTY, "sell")["total"])
                    pnl = round((fill - float(pos["entry"])) * QTY - ch_open - ch_close, 2)
                    trades.append(
                        {
                            "day": day,
                            "entry_hm": pos["hm"],
                            "exit_hm": hm,
                            "entry": pos["entry"],
                            "exit": fill,
                            "pnl": pnl,
                            "reason": reason,
                        }
                    )
                    pos = None
                continue

            if _square(hm):
                continue
            if not _in_entry(hm) or entries >= MAX_ENTRIES_PER_DAY:
                continue
            if not row.get("long_setup"):
                continue
            if st_dir.get(t) != 1:
                continue
            spot = float(bar["c"])
            up1 = float(row["up1"])
            if spot >= up1 or (up1 - spot) < MIN_EDGE_BUFFER_PTS:
                continue
            ch_open = float(spot_order_charges(spot, QTY, "buy")["total"])
            pos = {"entry": spot, "hm": hm, "charges_open": ch_open}
            entries += 1
        if pos is not None:
            last = sess[-1]
            fill = float(last["c"])
            ch_close = float(spot_order_charges(fill, QTY, "sell")["total"])
            pnl = round(
                (fill - float(pos["entry"])) * QTY - float(pos["charges_open"]) - ch_close, 2
            )
            trades.append(
                {
                    "day": day,
                    "entry_hm": pos["hm"],
                    "exit_hm": last["t"][11:16],
                    "entry": pos["entry"],
                    "exit": fill,
                    "pnl": pnl,
                    "reason": "eod",
                }
            )

    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gl = abs(sum(losses)) or 1e-9
    by_reason: dict[str, int] = defaultdict(int)
    for t in trades:
        by_reason[str(t["reason"])] += 1
    return {
        "n_days": len(by_day),
        "from": min(by_day) if by_day else None,
        "to": max(by_day) if by_day else None,
        "n": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_pct": round(100.0 * len(wins) / len(pnls), 1) if pnls else 0.0,
        "net": round(sum(pnls), 2) if pnls else 0.0,
        "avg": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "pf": round(sum(wins) / gl, 2) if pnls else 0.0,
        "by_reason": dict(by_reason),
        "trades": trades,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=Path, required=True)
    ap.add_argument("--agg", type=int, default=0, help="Aggregate N-minute if source is 1m")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    bars = _load_bars(args.bars)
    agg = args.agg or 0
    # auto-detect 1m → 5m
    if agg <= 0 and bars:
        # if median gap ~1m, aggregate
        gaps = []
        for i in range(1, min(50, len(bars))):
            try:
                a = datetime.fromisoformat(bars[i - 1]["t"].replace(" ", "T"))
                b = datetime.fromisoformat(bars[i]["t"].replace(" ", "T"))
                gaps.append((b - a).total_seconds())
            except ValueError:
                pass
        med = sorted(gaps)[len(gaps) // 2] if gaps else 300
        if med <= 90:
            agg = PAPER_BAR_MINUTES
    if agg > 1:
        bars = aggregate_bars(bars, agg)
        print(f"aggregated to {agg}m → {len(bars)} bars", flush=True)
    else:
        print(f"native bars={len(bars)} (treating as paper TF)", flush=True)

    result = run(bars)
    slim = {k: v for k, v in result.items() if k != "trades"}
    print(json.dumps(slim, indent=2))
    print(
        f"\nWIN_RATE={result['win_pct']}%  n={result['n']}  "
        f"wins={result['wins']}  net={result['net']}  "
        f"range={result['from']}→{result['to']}"
    )
    if args.out:
        args.out.write_text(json.dumps(result, indent=2)[:2_000_000], encoding="utf-8")
        print("saved", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
