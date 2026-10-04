#!/usr/bin/env python3
"""Score short-hold ATM option scalps on Atlas recordings (1 lot, Kite NFO charges).

Impulse = last LOOKBACK same-day spot closes. Fade = long the opposite wing
(up → PE, down → CE). That is the live paper book; momentum is the control.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.recorder import record_dir  # noqa: E402
from scripts.score_recordings import _hm_to_min, _index_by_day, load_minute_rows  # noqa: E402
from scripts.scan_option_ideas import _settle, _trade  # noqa: E402

QTY = 65
AFTER = "09:30"
BEFORE = "14:45"
UNTIL = "15:14"


def _impulse(rows: list[dict], i: int, lookback: int) -> float | None:
    if i < lookback:
        return None
    now = rows[i].get("spot")
    prev = rows[i - lookback].get("spot")
    if now is None or prev is None:
        return None
    return float(now) - float(prev)


def _walk_hold(by_day, entry, field, side, hold_min, target, stop):
    px0 = entry.get(field)
    if px0 is None or px0 <= 0:
        return None
    start = _hm_to_min(entry["hm"]) + 1
    end = min(_hm_to_min(UNTIL), start + hold_min - 1)
    last = None
    for t in range(start, end + 1):
        hm = f"{t // 60:02d}:{t % 60:02d}"
        row = by_day[entry["date"]].get(hm)
        if not row or row.get(field) is None:
            continue
        px = row[field]
        last = (px, hm)
        pct = side * (px - px0) / px0
        if target is not None and pct >= target:
            return px, hm, "target"
        if stop is not None and pct <= stop:
            return px, hm, "stop"
    if last is not None:
        reason = "time" if last[1] == UNTIL or _hm_to_min(last[1]) >= end else "tape_end"
        return last[0], last[1], reason
    return None


def run_impulse(
    by_day,
    *,
    lookback: int,
    min_move: float,
    hold_min: int,
    target: float,
    stop: float,
    max_day: int,
    cooldown: int,
    fade: bool = False,
):
    trades = []
    for day in sorted(by_day):
        rows = [by_day[day][hm] for hm in sorted(by_day[day])]
        entries = 0
        last_exit = -10_000
        i = 0
        while i < len(rows):
            row = rows[i]
            hm = row["hm"]
            if hm < AFTER or hm > BEFORE or entries >= max_day:
                i += 1
                continue
            if _hm_to_min(hm) < last_exit + cooldown:
                i += 1
                continue
            delta = _impulse(rows, i, lookback)
            if delta is None or abs(delta) < min_move:
                i += 1
                continue
            side_long = delta > 0
            if fade:
                side_long = not side_long
            field = "ce" if side_long else "pe"
            if not row.get(field):
                i += 1
                continue
            walked = _walk_hold(by_day, row, field, 1, hold_min, target, stop)
            if walked is None:
                i += 1
                continue
            px1, ehm, reason = walked
            trades.append(
                _trade(
                    row[field],
                    px1,
                    1,
                    QTY,
                    1,
                    reason,
                    {
                        "date": day,
                        "entry": hm,
                        "exit": ehm,
                        "px0": row[field],
                        "px1": px1,
                        "side": field,
                        "impulse": round(delta, 2),
                    },
                )
            )
            entries += 1
            last_exit = _hm_to_min(ehm)
            i += 1
            while i < len(rows) and _hm_to_min(rows[i]["hm"]) <= last_exit:
                i += 1
    return trades


def split_days(by_day):
    keys = sorted(by_day)
    cut = max(1, int(len(keys) * 0.6))
    return keys[:cut], keys[cut:]


def subset(by_day, keys):
    return {k: by_day[k] for k in keys if k in by_day}


def main() -> int:
    rec = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else record_dir(ROOT / "data")
    rows = load_minute_rows(rec)
    by_day = _index_by_day(rows)
    train_k, test_k = split_days(by_day)
    print(f"dir={rec} minutes={len(rows)} days={sorted(by_day)}")
    print(f"train={train_k} test={test_k}")
    ideas = [
        ("fade 3m≥12 12m 8/-6 4/d", dict(lookback=3, min_move=12, hold_min=12, target=0.08, stop=-0.06, max_day=4, cooldown=8, fade=True)),
        ("fade 3m≥15 12m 8/-6 4/d", dict(lookback=3, min_move=15, hold_min=12, target=0.08, stop=-0.06, max_day=4, cooldown=8, fade=True)),
        ("fade 3m≥12 8m 6/-5 4/d", dict(lookback=3, min_move=12, hold_min=8, target=0.06, stop=-0.05, max_day=4, cooldown=8, fade=True)),
        ("fade 3m≥12 15m 10/-6 3/d", dict(lookback=3, min_move=12, hold_min=15, target=0.10, stop=-0.06, max_day=3, cooldown=8, fade=True)),
        ("fade 5m≥18 12m 8/-6 3/d", dict(lookback=5, min_move=18, hold_min=12, target=0.08, stop=-0.06, max_day=3, cooldown=8, fade=True)),
        ("fade 3m≥12 12m 8/-6 2/d", dict(lookback=3, min_move=12, hold_min=12, target=0.08, stop=-0.06, max_day=2, cooldown=8, fade=True)),
        ("mom  3m≥12 12m 8/-6 4/d", dict(lookback=3, min_move=12, hold_min=12, target=0.08, stop=-0.06, max_day=4, cooldown=8)),
    ]
    print(f"{'idea':<26} {'n':>4} {'W':>3} {'win%':>6} {'pnl':>9} {'avg':>7} {'dd':>8}  train     test")
    for name, kw in ideas:
        all_t = run_impulse(by_day, **kw)
        tr = _settle(run_impulse(subset(by_day, train_k), **kw), exclude=("tape_end",))
        te = _settle(run_impulse(subset(by_day, test_k), **kw), exclude=("tape_end",))
        s = _settle(all_t, exclude=("tape_end",))
        print(
            f"{name:<26} {s['n']:4} {s['wins']:3} {s['win_pct']:5.1f}% "
            f"{s['pnl']:9.0f} {s['avg']:7.0f} {s['max_dd']:8.0f}  "
            f"{tr['pnl']:7.0f} {te['pnl']:7.0f}"
        )
        reasons = {}
        for t in all_t:
            if t.get("reason") == "tape_end":
                continue
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        print(f"  reasons {reasons}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
