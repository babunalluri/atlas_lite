#!/usr/bin/env python3
"""VWAP mean-reversion long BT on 5m (pivot away from trend-follow nifty_vwap_long).

Entry: tag lower band (−0.5σ or −1σ) then bullish reversal toward VWAP
       (close turns up / closes back above the band, aiming at VWAP).
Exit: VWAP or +0.5σ/+1σ target; stop below swing/band; optional time flat.

Does not enable the live paper book.
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
    QTY,
    SQUARE_OFF,
    scan_vwap_signals,
    spot_order_charges,
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
    bars: list[dict],
    *,
    band_sigma: float = 1.0,
    target: str = "vwap",  # vwap | up0.5 | up1
    stop_extra_sigma: float = 0.25,
    qty: int = 1,
    require_close_up: bool = True,
    min_headroom: float = 8.0,
) -> list[dict]:
    """Mean-reversion long from lower band toward VWAP."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        by_day[b["t"][:10]].append(b)

    trades: list[dict] = []
    for day, sess in sorted(by_day.items()):
        if len(sess) < 8:
            continue
        rows = scan_vwap_signals(sess)
        pos = None
        entries = 0
        tagged = False  # saw a touch of lower band recently
        for i, row in enumerate(rows):
            t = str(row["t"])
            hm = t[11:16]
            bar = sess[i]
            if not row.get("bands_ready"):
                tagged = False
                continue
            vwap = float(row["vwap"])
            sigma = float(row["sigma"])
            band = vwap - band_sigma * sigma
            # target
            if target == "vwap":
                tgt = vwap
            elif target == "up0.5":
                tgt = vwap + 0.5 * sigma
            else:
                tgt = vwap + 1.0 * sigma

            if pos is not None:
                stop = float(pos["stop"])
                reason = fill = None
                if float(bar["l"]) <= stop:
                    reason, fill = "stop", stop
                elif float(bar["h"]) >= tgt and tgt > float(pos["entry"]):
                    reason, fill = "target", tgt
                elif _ht(hm) >= SQUARE_OFF:
                    reason, fill = "time", float(bar["c"])
                if reason is not None and fill is not None:
                    ch_c = float(spot_order_charges(fill, qty, "sell")["total"])
                    gross = (fill - float(pos["entry"])) * qty
                    trades.append(
                        {
                            "day": day,
                            "gross": round(gross, 2),
                            "net": round(gross - float(pos["ch"]) - ch_c, 2),
                            "reason": reason,
                        }
                    )
                    pos = None
                    tagged = False
                continue

            if _ht(hm) >= SQUARE_OFF:
                continue
            if not ((_ht(hm) >= ENTRY_AFTER) and (_ht(hm) < ENTRY_UNTIL)):
                continue
            if entries >= MAX_ENTRIES_PER_DAY:
                continue

            # tag lower band
            if float(bar["l"]) <= band:
                tagged = True

            if not tagged:
                continue

            close = float(bar["c"])
            open_ = float(bar["o"])
            # bullish reversal toward VWAP: close back above band, preferably green,
            # and still at/under VWAP (mean-reversion, not breakout)
            if close <= band:
                continue
            if close >= vwap:
                # already through VWAP — skip chase
                tagged = False
                continue
            if require_close_up and close <= open_:
                continue
            if (tgt - close) < min_headroom:
                continue

            ch = float(spot_order_charges(close, qty, "buy")["total"])
            stop = band - stop_extra_sigma * sigma
            pos = {"entry": close, "ch": ch, "stop": stop}
            entries += 1
            tagged = False

        if pos is not None:
            last = sess[-1]
            fill = float(last["c"])
            ch_c = float(spot_order_charges(fill, qty, "sell")["total"])
            gross = (fill - float(pos["entry"])) * qty
            trades.append(
                {
                    "day": day,
                    "gross": round(gross, 2),
                    "net": round(gross - float(pos["ch"]) - ch_c, 2),
                    "reason": "eod",
                }
            )
    return trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    bars = _load_bars(args.bars)
    print(f"bars={len(bars)} days={len({b['t'][:10] for b in bars})}")

    results = []
    print(f"\n{'cfg':<40} {'n':>3} {'WR':>6} {'gWR':>6} {'net':>9} {'PF':>5}")
    for band, tgt, head, qty in itertools.product(
        [0.5, 1.0],
        ["vwap", "up0.5", "up1"],
        [5.0, 8.0, 12.0],
        [1, 3, 5],
    ):
        tr = simulate(
            bars,
            band_sigma=band,
            target=tgt,
            min_headroom=head,
            qty=qty,
            require_close_up=True,
        )
        s = _stats(tr)
        s["cfg"] = {
            "band_sigma": band,
            "target": tgt,
            "min_headroom": head,
            "qty": qty,
        }
        results.append(s)

    results.sort(key=lambda r: (r["net"], r["wr"], r["gross_wr"]), reverse=True)
    for i, r in enumerate(results[:15], 1):
        c = r["cfg"]
        label = f"band−{c['band_sigma']}σ→{c['target']} h≥{c['min_headroom']:.0f} q={c['qty']}"
        print(
            f"{label:<40} {r['n']:3} {r['wr']:6.1f} {r['gross_wr']:6.1f} "
            f"{r['net']:+9.0f} {r['pf']:5.2f}"
        )

    best = results[0]
    solid = [r for r in results if r["n"] >= 15 and r["wr"] >= 45]
    print("\n=== BEST BY NET ===")
    print(json.dumps(best, indent=2))
    print("\n=== BEST netWR≥45 n≥15 ===")
    print(json.dumps(solid[0] if solid else {"none": True}, indent=2))

    # Compare vs abandoned trend book reminder
    print(
        "\nNOTE: nifty_vwap_long trend-follow (close>VWAP + ST) stays abandoned; "
        "this is MR-only research."
    )

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "best": best,
                    "best_wr45": solid[0] if solid else None,
                    "top": results[:20],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print("saved", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
