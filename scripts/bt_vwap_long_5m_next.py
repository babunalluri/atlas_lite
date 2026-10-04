#!/usr/bin/env python3
"""Next-step VWAP long BT: realistic qty (2–5) + ATR day filter on OCI 5m."""

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


def _atr_by_day(bars: list[dict], n: int = 14) -> dict[str, float]:
    """Session ATR proxy: mean TR of first n 5m bars (or all if fewer)."""
    by: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        by[b["t"][:10]].append(b)
    out: dict[str, float] = {}
    for day, sess in by.items():
        if len(sess) < 3:
            continue
        trs = []
        prev_c = float(sess[0]["c"])
        for b in sess[1 : min(len(sess), n + 1)]:
            h, l, c = float(b["h"]), float(b["l"]), float(b["c"])
            trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
            prev_c = c
        if trs:
            out[day] = sum(trs) / len(trs)
    return out


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
    qty: int = 1,
    target_sigma: float = 1.5,
    min_headroom: float = 15.0,
    trail: bool = True,
    atr_min: float | None = None,
    atr_day: dict[str, float] | None = None,
    trail_arm_sigma: float = 0.5,
    trail_gap_sigma: float = 0.75,
    initial_stop_sigma: float = 1.0,
) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for b in bars_5m:
        by_day[b["t"][:10]].append(b)
    atr_day = atr_day or {}

    trades: list[dict] = []
    for day, sess in sorted(by_day.items()):
        if atr_min is not None:
            a = atr_day.get(day)
            if a is None or a < atr_min:
                continue
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
                pos["peak"] = max(float(pos["peak"]), float(bar["h"]))
                tgt = vwap + target_sigma * sigma
                init_stop = vwap - initial_stop_sigma * sigma
                stop = float(pos["stop"])
                if trail:
                    armed = pos["peak"] >= float(pos["entry"]) + trail_arm_sigma * float(
                        pos["sigma0"]
                    )
                    if armed:
                        trail_stop = pos["peak"] - trail_gap_sigma * sigma
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
                    ch_close = float(spot_order_charges(fill, qty, "sell")["total"])
                    gross = (fill - float(pos["entry"])) * qty
                    net = round(gross - float(pos["ch_open"]) - ch_close, 2)
                    trades.append(
                        {
                            "day": day,
                            "gross": round(gross, 2),
                            "net": net,
                            "reason": reason,
                            "qty": qty,
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
            if not row.get("long_setup") or st.get(t) != 1:
                continue
            spot = float(bar["c"])
            tgt = vwap + target_sigma * sigma
            if spot >= tgt or (tgt - spot) < min_headroom:
                continue
            ch_open = float(spot_order_charges(spot, qty, "buy")["total"])
            pos = {
                "entry": spot,
                "ch_open": ch_open,
                "sigma0": sigma,
                "peak": spot,
                "stop": vwap - initial_stop_sigma * sigma,
            }
            entries += 1
        if pos is not None:
            last = sess[-1]
            fill = float(last["c"])
            ch_close = float(spot_order_charges(fill, qty, "sell")["total"])
            gross = (fill - float(pos["entry"])) * qty
            trades.append(
                {
                    "day": day,
                    "gross": round(gross, 2),
                    "net": round(gross - float(pos["ch_open"]) - ch_close, 2),
                    "reason": "eod",
                    "qty": qty,
                }
            )
    return trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    bars = _load_bars(args.bars)
    atr_day = _atr_by_day(bars)
    atr_vals = sorted(atr_day.values())
    p25 = atr_vals[len(atr_vals) // 4] if atr_vals else 0
    p50 = atr_vals[len(atr_vals) // 2] if atr_vals else 0
    print(f"bars={len(bars)} days={len(atr_day)} ATR5m p25={p25:.2f} p50={p50:.2f}")

    # Improved base from prior note
    base_kw = dict(target_sigma=1.5, min_headroom=15.0, trail=True)

    rows = []
    print(f"\n{'cfg':<42} {'n':>3} {'WR':>6} {'gWR':>6} {'net':>9} {'avg':>8} {'PF':>5}")
    for qty, atr_min in itertools.product([1, 2, 3, 5], [None, round(p25, 2), round(p50, 2)]):
        tr = simulate(
            bars,
            qty=qty,
            atr_min=atr_min,
            atr_day=atr_day,
            **base_kw,
        )
        s = _stats(tr)
        label = f"q={qty} atr_min={atr_min if atr_min is not None else 'none'} +1.5σ/h15/trail"
        s["cfg"] = {"qty": qty, "atr_min": atr_min, **base_kw}
        rows.append(s)
        print(
            f"{label:<42} {s['n']:3} {s['wr']:6.1f} {s['gross_wr']:6.1f} "
            f"{s['net']:+9.0f} {s['avg']:+8.1f} {s['pf']:5.2f}"
        )

    # Also +2σ / head20 with best qty ideas
    print("\n--- +2σ head≥20 trail ---")
    for qty, atr_min in itertools.product([3, 5], [None, round(p50, 2)]):
        tr = simulate(
            bars,
            qty=qty,
            atr_min=atr_min,
            atr_day=atr_day,
            target_sigma=2.0,
            min_headroom=20.0,
            trail=True,
        )
        s = _stats(tr)
        label = f"q={qty} atr_min={atr_min if atr_min is not None else 'none'} +2σ/h20/trail"
        s["cfg"] = {
            "qty": qty,
            "atr_min": atr_min,
            "target_sigma": 2.0,
            "min_headroom": 20.0,
            "trail": True,
        }
        rows.append(s)
        print(
            f"{label:<42} {s['n']:3} {s['wr']:6.1f} {s['gross_wr']:6.1f} "
            f"{s['net']:+9.0f} {s['avg']:+8.1f} {s['pf']:5.2f}"
        )

    ranked = sorted(rows, key=lambda r: (r["net"], r["wr"]), reverse=True)
    print("\n=== BEST BY NET ===")
    print(json.dumps(ranked[0], indent=2))
    # best with wr>=45 and n>=8
    good = [r for r in ranked if r["wr"] >= 45 and r["n"] >= 8]
    print("\n=== BEST netWR≥45 n≥8 ===")
    print(json.dumps(good[0] if good else {"none": True}, indent=2))

    out = {
        "atr_p25": p25,
        "atr_p50": p50,
        "all": rows,
        "best_net": ranked[0],
        "best_wr45": good[0] if good else None,
    }
    if args.out:
        args.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
        print("saved", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
