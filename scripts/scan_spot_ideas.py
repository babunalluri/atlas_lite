#!/usr/bin/env python3
"""Score simple NIFTY spot ideas on data/regime/nifty_5m.json (Kite 5m).

Same paper book as VWAP long: qty=1, ₹1/pt, Zerodha equity-intraday charges,
flatten 15:15, no new entries after 14:00. No parameter search — fixed rules.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.paper_vwap_long import (  # noqa: E402
    spot_order_charges,
    supertrend_series,
)

IST = ZoneInfo("Asia/Kolkata")
SRC = ROOT / "data" / "regime" / "nifty_5m.json"
QTY = 1
SQUARE = "15:15"
ENTRY_UNTIL = "14:00"
ENTRY_AFTER = "09:30"


def _hm(ts: str) -> str:
    raw = str(ts).replace("T", " ")
    return raw[11:16]


def _day(ts: str) -> str:
    return str(ts).replace("T", " ")[:10]


def _load() -> list[dict[str, Any]]:
    raw = json.loads(SRC.read_text(encoding="utf-8"))
    bars: list[dict[str, Any]] = []
    for row in raw:
        ts = str(row[0])
        bars.append(
            {
                "t": ts.replace("T", " ")[:16],
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5] or 0),
            }
        )
    return bars


def _charges(entry: float, exit_px: float) -> float:
    buy = spot_order_charges(entry, QTY, "buy")["total"]
    sell = spot_order_charges(exit_px, QTY, "sell")["total"]
    return round(buy + sell, 2)


def _by_day(bars: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for b in bars:
        hm = _hm(b["t"])
        if hm < "09:15" or hm > "15:25":
            continue
        out[_day(b["t"])].append(b)
    return dict(out)


def _settle(trades: list[dict[str, Any]], *, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    if exclude:
        trades = [t for t in trades if t.get("reason") not in exclude]
    if not trades:
        return {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "gross": 0.0,
            "charges": 0.0,
            "win_pct": 0.0,
            "avg": 0.0,
            "max_dd": 0.0,
            "pf": 0.0,
        }
    pnl = [float(t["pnl"]) for t in trades]
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    gp = sum(x for x in pnl if x > 0)
    gl = -sum(x for x in pnl if x < 0)
    return {
        "n": len(trades),
        "wins": sum(1 for x in pnl if x > 0),
        "losses": sum(1 for x in pnl if x <= 0),
        "pnl": round(sum(pnl), 2),
        "gross": round(sum(float(t["gross"]) for t in trades), 2),
        "charges": round(sum(float(t["charges"]) for t in trades), 2),
        "win_pct": round(100.0 * sum(1 for x in pnl if x > 0) / len(pnl), 1),
        "avg": round(sum(pnl) / len(pnl), 2),
        "max_dd": round(max_dd, 2),
        "pf": round(gp / gl, 2) if gl else 99.0,
    }


def _close_trade(
    *,
    day: str,
    side: int,
    entry: float,
    exit_px: float,
    entry_hm: str,
    exit_hm: str,
    reason: str,
) -> dict[str, Any]:
    gross = (exit_px - entry) * side * QTY
    ch = _charges(entry, exit_px)
    return {
        "day": day,
        "side": "L" if side > 0 else "S",
        "entry_hm": entry_hm,
        "exit_hm": exit_hm,
        "entry": round(entry, 2),
        "exit": round(exit_px, 2),
        "reason": reason,
        "gross": round(gross, 2),
        "charges": ch,
        "pnl": round(gross - ch, 2),
    }


def orb(
    days: dict[str, list[dict[str, Any]]],
    *,
    range_until: str,
    sides: str,
    stop: str,
) -> list[dict[str, Any]]:
    """Opening-range break. stop=range|mid."""
    trades: list[dict[str, Any]] = []
    for day, bars in days.items():
        rng = [b for b in bars if _hm(b["t"]) < range_until]
        rest = [b for b in bars if range_until <= _hm(b["t"]) <= SQUARE]
        if len(rng) < 2 or not rest:
            continue
        hi = max(b["h"] for b in rng)
        lo = min(b["l"] for b in rng)
        if hi - lo < 8:
            continue
        mid = (hi + lo) / 2
        pos = None
        for b in rest:
            hm = _hm(b["t"])
            entered_this_bar = False
            if pos is None and hm <= ENTRY_UNTIL:
                if sides in ("both", "long") and b["h"] >= hi and b["c"] > hi:
                    pos = (1, hi, hm, lo if stop == "range" else mid)
                    entered_this_bar = True
                elif sides in ("both", "short") and b["l"] <= lo and b["c"] < lo:
                    pos = (-1, lo, hm, hi if stop == "range" else mid)
                    entered_this_bar = True
            if pos is None or entered_this_bar:
                continue
            side, entry, ehm, stop_px = pos
            if side > 0 and b["l"] <= stop_px:
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=stop_px,
                        entry_hm=ehm, exit_hm=hm, reason="stop",
                    )
                )
                pos = None
                break
            if side < 0 and b["h"] >= stop_px:
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=stop_px,
                        entry_hm=ehm, exit_hm=hm, reason="stop",
                    )
                )
                pos = None
                break
            if hm >= SQUARE:
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=b["c"],
                        entry_hm=ehm, exit_hm=hm, reason="time",
                    )
                )
                pos = None
                break
        if pos is not None and rest:
            side, entry, ehm, _stop_px = pos
            last = rest[-1]
            trades.append(
                _close_trade(
                    day=day, side=side, entry=entry, exit_px=last["c"],
                    entry_hm=ehm, exit_hm=_hm(last["t"]), reason="tape_end",
                )
            )
    return trades


def st_flip(days: dict[str, list[dict[str, Any]]], *, long_only: bool) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for day, bars in days.items():
        st = supertrend_series(bars)
        pos = None
        entries = 0
        prev = None
        for b, row in zip(bars, st):
            hm = _hm(b["t"])
            direction = row.get("dir")
            if pos is not None:
                side, entry, ehm = pos
                flip = prev is not None and direction is not None and direction != prev
                if flip or hm >= SQUARE:
                    trades.append(
                        _close_trade(
                            day=day,
                            side=side,
                            entry=entry,
                            exit_px=b["c"],
                            entry_hm=ehm,
                            exit_hm=hm,
                            reason="flip" if flip and hm < SQUARE else "time",
                        )
                    )
                    pos = None
            if (
                pos is None
                and entries < 2
                and ENTRY_AFTER <= hm <= ENTRY_UNTIL
                and prev is not None
                and direction is not None
                and direction != prev
            ):
                if direction > 0 or not long_only:
                    pos = (1 if direction > 0 else -1, b["c"], hm)
                    entries += 1
            if direction is not None:
                prev = direction
        if pos is not None:
            side, entry, ehm = pos
            last = bars[-1]
            trades.append(
                _close_trade(
                    day=day, side=side, entry=entry, exit_px=last["c"],
                    entry_hm=ehm, exit_hm=_hm(last["t"]), reason="time",
                )
            )
    return trades


def pdh_break(days: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    prev_hi = None
    prev_lo = None
    for day, bars in sorted(days.items()):
        if prev_hi is None:
            prev_hi = max(b["h"] for b in bars)
            prev_lo = min(b["l"] for b in bars)
            continue
        pos = None
        for b in bars:
            hm = _hm(b["t"])
            entered_this_bar = False
            if pos is None and ENTRY_AFTER <= hm <= ENTRY_UNTIL and b["c"] > prev_hi:
                pos = (1, b["c"], hm, prev_lo)
                entered_this_bar = True
            if pos is None or entered_this_bar:
                continue
            side, entry, ehm, stop_px = pos
            if b["l"] <= stop_px:
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=stop_px,
                        entry_hm=ehm, exit_hm=hm, reason="stop",
                    )
                )
                pos = None
                break
            if hm >= SQUARE:
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=b["c"],
                        entry_hm=ehm, exit_hm=hm, reason="time",
                    )
                )
                pos = None
                break
        prev_hi = max(b["h"] for b in bars)
        prev_lo = min(b["l"] for b in bars)
    return trades


def gap_fade(days: dict[str, list[dict[str, Any]]], *, min_gap: float) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    prev_c = None
    for day, bars in sorted(days.items()):
        if not bars:
            continue
        open_px = bars[0]["o"]
        if prev_c is None:
            prev_c = bars[-1]["c"]
            continue
        gap = open_px - prev_c
        if abs(gap) < min_gap:
            prev_c = bars[-1]["c"]
            continue
        side = -1 if gap > 0 else 1
        entry = None
        ehm = None
        stop = None
        target = prev_c
        for b in bars:
            hm = _hm(b["t"])
            entered_this_bar = False
            if entry is None and ENTRY_AFTER <= hm <= "10:30":
                entry = b["c"]
                ehm = hm
                risk = abs(entry - target)
                if risk < 5:
                    break
                stop = entry + risk if side < 0 else entry - risk
                entered_this_bar = True
            if entry is None or entered_this_bar:
                continue
            if side > 0 and (b["l"] <= stop or b["h"] >= target):
                hit = stop if b["l"] <= stop else target
                reason = "stop" if hit == stop else "target"
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=hit,
                        entry_hm=ehm, exit_hm=hm, reason=reason,
                    )
                )
                entry = None
                break
            if side < 0 and (b["h"] >= stop or b["l"] <= target):
                hit = stop if b["h"] >= stop else target
                reason = "stop" if hit == stop else "target"
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=hit,
                        entry_hm=ehm, exit_hm=hm, reason=reason,
                    )
                )
                entry = None
                break
            if hm >= SQUARE:
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=b["c"],
                        entry_hm=ehm, exit_hm=hm, reason="time",
                    )
                )
                entry = None
                break
        prev_c = bars[-1]["c"]
    return trades


def vwap_pullback(days: dict[str, list[dict[str, Any]]], *, long_only: bool) -> list[dict[str, Any]]:
    """Existing paper rule: tag −0.5σ, close > VWAP, ST up. Long only unless told."""
    trades: list[dict[str, Any]] = []
    for day, bars in days.items():
        st = supertrend_series(bars)
        eq_sum = eq_sum_sq = 0.0
        eq_n = 0
        pos = None
        entries = 0
        for b, row in zip(bars, st):
            hm = _hm(b["t"])
            typical = (b["h"] + b["l"] + b["c"]) / 3
            eq_sum += typical
            eq_sum_sq += typical * typical
            eq_n += 1
            vwap = eq_sum / eq_n
            sigma = max(0.0, eq_sum_sq / eq_n - vwap * vwap) ** 0.5
            dn05 = vwap - 0.5 * sigma
            up1 = vwap + sigma
            dn1 = vwap - sigma
            up05 = vwap + 0.5 * sigma
            if pos is not None:
                side, entry, ehm = pos
                hit = None
                reason = None
                if side > 0:
                    if b["l"] <= dn1:
                        hit, reason = dn1, "stop"
                    elif b["h"] >= up1:
                        hit, reason = up1, "take"
                    elif row.get("dir") == -1:
                        hit, reason = b["c"], "st_flip"
                else:
                    if b["h"] >= up1:
                        hit, reason = up1, "stop"
                    elif b["l"] <= dn1:
                        hit, reason = dn1, "take"
                    elif row.get("dir") == 1:
                        hit, reason = b["c"], "st_flip"
                if hm >= SQUARE:
                    hit, reason = b["c"], "time"
                if hit is not None:
                    trades.append(
                        _close_trade(
                            day=day, side=side, entry=entry, exit_px=hit,
                            entry_hm=ehm, exit_hm=hm, reason=reason or "time",
                        )
                    )
                    pos = None
                    if reason != "time":
                        continue
                    break
            if (
                pos is None
                and entries < 2
                and ENTRY_AFTER <= hm <= ENTRY_UNTIL
                and sigma >= 1
                and eq_n >= 5
            ):
                st_up = row.get("dir") == 1
                st_dn = row.get("dir") == -1
                if st_up and b["c"] > vwap and b["l"] <= dn05:
                    pos = (1, b["c"], hm)
                    entries += 1
                elif (not long_only) and st_dn and b["c"] < vwap and b["h"] >= up05:
                    pos = (-1, b["c"], hm)
                    entries += 1
    return trades


def orb_1r(
    days: dict[str, list[dict[str, Any]]],
    *,
    range_until: str,
    sides: str,
    min_w: float,
    max_w: float,
) -> list[dict[str, Any]]:
    """ORB with 1R take at range width; skip very tight / very wide opens."""
    trades: list[dict[str, Any]] = []
    for day, bars in days.items():
        rng = [b for b in bars if _hm(b["t"]) < range_until]
        rest = [b for b in bars if range_until <= _hm(b["t"]) <= SQUARE]
        if len(rng) < 2 or not rest:
            continue
        hi = max(b["h"] for b in rng)
        lo = min(b["l"] for b in rng)
        width = hi - lo
        if width < min_w or width > max_w:
            continue
        pos = None
        for b in rest:
            hm = _hm(b["t"])
            entered_this_bar = False
            if pos is None and hm <= ENTRY_UNTIL:
                if sides in ("both", "long") and b["c"] > hi:
                    pos = (1, b["c"], hm, lo, b["c"] + width)
                    entered_this_bar = True
                elif sides in ("both", "short") and b["c"] < lo:
                    pos = (-1, b["c"], hm, hi, b["c"] - width)
                    entered_this_bar = True
            if pos is None or entered_this_bar:
                continue
            side, entry, ehm, stop_px, take = pos
            if side > 0:
                if b["l"] <= stop_px:
                    trades.append(
                        _close_trade(
                            day=day, side=side, entry=entry, exit_px=stop_px,
                            entry_hm=ehm, exit_hm=hm, reason="stop",
                        )
                    )
                    pos = None
                    break
                if b["h"] >= take:
                    trades.append(
                        _close_trade(
                            day=day, side=side, entry=entry, exit_px=take,
                            entry_hm=ehm, exit_hm=hm, reason="take",
                        )
                    )
                    pos = None
                    break
            else:
                if b["h"] >= stop_px:
                    trades.append(
                        _close_trade(
                            day=day, side=side, entry=entry, exit_px=stop_px,
                            entry_hm=ehm, exit_hm=hm, reason="stop",
                        )
                    )
                    pos = None
                    break
                if b["l"] <= take:
                    trades.append(
                        _close_trade(
                            day=day, side=side, entry=entry, exit_px=take,
                            entry_hm=ehm, exit_hm=hm, reason="take",
                        )
                    )
                    pos = None
                    break
            if hm >= SQUARE:
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=b["c"],
                        entry_hm=ehm, exit_hm=hm, reason="time",
                    )
                )
                pos = None
                break
    return trades


def hold_bias(
    days: dict[str, list[dict[str, Any]]],
    *,
    until: str,
    use_stop: bool,
) -> list[dict[str, Any]]:
    """After first-hour close vs open: hold that side to 15:15 (optional hour-low/high stop)."""
    trades: list[dict[str, Any]] = []
    for day, bars in days.items():
        hour = [b for b in bars if _hm(b["t"]) < until]
        rest = [b for b in bars if until <= _hm(b["t"]) <= SQUARE]
        if len(hour) < 2 or not rest:
            continue
        o = hour[0]["o"]
        c = hour[-1]["c"]
        if abs(c - o) < 8:
            continue
        side = 1 if c > o else -1
        entry = rest[0]["o"]
        ehm = _hm(rest[0]["t"])
        stop = min(b["l"] for b in hour) if side > 0 else max(b["h"] for b in hour)
        for b in rest:
            hm = _hm(b["t"])
            if use_stop:
                if side > 0 and b["l"] <= stop:
                    trades.append(
                        _close_trade(
                            day=day, side=side, entry=entry, exit_px=stop,
                            entry_hm=ehm, exit_hm=hm, reason="stop",
                        )
                    )
                    break
                if side < 0 and b["h"] >= stop:
                    trades.append(
                        _close_trade(
                            day=day, side=side, entry=entry, exit_px=stop,
                            entry_hm=ehm, exit_hm=hm, reason="stop",
                        )
                    )
                    break
            if hm >= SQUARE:
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=b["c"],
                        entry_hm=ehm, exit_hm=hm, reason="time",
                    )
                )
                break
    return trades


def donchian(days: dict[str, list[dict[str, Any]]], n: int = 12) -> list[dict[str, Any]]:
    """Session Donchian: buy close > prior N-bar high, stop N-bar low, 1/day."""
    trades: list[dict[str, Any]] = []
    for day, bars in days.items():
        pos = None
        for i, b in enumerate(bars):
            hm = _hm(b["t"])
            if i < n:
                continue
            window = bars[i - n : i]
            hi = max(x["h"] for x in window)
            lo = min(x["l"] for x in window)
            entered_this_bar = False
            if pos is None and ENTRY_AFTER <= hm <= ENTRY_UNTIL and b["c"] > hi:
                pos = (1, b["c"], hm, lo)
                entered_this_bar = True
            if pos is None or entered_this_bar:
                continue
            side, entry, ehm, stop_px = pos
            if b["l"] <= stop_px:
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=stop_px,
                        entry_hm=ehm, exit_hm=hm, reason="stop",
                    )
                )
                pos = None
                break
            if hm >= SQUARE:
                trades.append(
                    _close_trade(
                        day=day, side=side, entry=entry, exit_px=b["c"],
                        entry_hm=ehm, exit_hm=hm, reason="time",
                    )
                )
                pos = None
                break
    return trades


def split_days(days: dict[str, list[dict[str, Any]]]) -> tuple[list[str], list[str]]:
    keys = sorted(days)
    cut = int(len(keys) * 0.6)
    return keys[:cut], keys[cut:]


def subset(days: dict[str, list[dict[str, Any]]], keys: list[str]) -> dict[str, list[dict[str, Any]]]:
    return {k: days[k] for k in keys if k in days}


def main() -> int:
    bars = _load()
    days = _by_day(bars)
    train_k, test_k = split_days(days)
    ideas = [
        ("ORB15 both stop=range", lambda d: orb(d, range_until="09:30", sides="both", stop="range")),
        ("ORB15 long stop=range", lambda d: orb(d, range_until="09:30", sides="long", stop="range")),
        ("ORB15 short stop=range", lambda d: orb(d, range_until="09:30", sides="short", stop="range")),
        ("ORB15 both stop=mid", lambda d: orb(d, range_until="09:30", sides="both", stop="mid")),
        ("ORB30 both stop=range", lambda d: orb(d, range_until="09:45", sides="both", stop="range")),
        ("ORB30 long stop=range", lambda d: orb(d, range_until="09:45", sides="long", stop="range")),
        ("ST flip both", lambda d: st_flip(d, long_only=False)),
        ("ST flip long-only", lambda d: st_flip(d, long_only=True)),
        ("PDH break long", pdh_break),
        ("Gap fade ≥40pt", lambda d: gap_fade(d, min_gap=40)),
        ("Gap fade ≥80pt", lambda d: gap_fade(d, min_gap=80)),
        ("VWAP long (current paper)", lambda d: vwap_pullback(d, long_only=True)),
        ("VWAP long+short", lambda d: vwap_pullback(d, long_only=False)),
        ("Donchian 12 long", lambda d: donchian(d, 12)),
        ("ORB15 1R both 12-50", lambda d: orb_1r(d, range_until="09:30", sides="both", min_w=12, max_w=50)),
        ("ORB15 1R long 12-50", lambda d: orb_1r(d, range_until="09:30", sides="long", min_w=12, max_w=50)),
        ("ORB15 1R short 12-50", lambda d: orb_1r(d, range_until="09:30", sides="short", min_w=12, max_w=50)),
        ("ORB30 1R both 15-60", lambda d: orb_1r(d, range_until="09:45", sides="both", min_w=15, max_w=60)),
        ("First hour hold", lambda d: hold_bias(d, until="10:15", use_stop=False)),
        ("First hour hold+stop", lambda d: hold_bias(d, until="10:15", use_stop=True)),
        ("0930 hold", lambda d: hold_bias(d, until="09:30", use_stop=False)),
        ("0930 hold+stop", lambda d: hold_bias(d, until="09:30", use_stop=True)),
    ]
    print(f"bars={len(bars)} days={len(days)} train={train_k[0]}..{train_k[-1]} test={test_k[0]}..{test_k[-1]}")
    print(f"{'idea':<28} {'n':>5} {'W':>4} {'L':>4} {'win%':>6} {'pnl':>10} {'avg':>8} {'dd':>8} {'pf':>5}  train_pnl  test_pnl")
    rows = []
    for name, fn in ideas:
        all_t = fn(days)
        tr = _settle(fn(subset(days, train_k)), exclude=("tape_end",))
        te = _settle(fn(subset(days, test_k)), exclude=("tape_end",))
        s = _settle(all_t, exclude=("tape_end",))
        cut = _settle([t for t in all_t if t.get("reason") == "tape_end"])
        rows.append((name, s, tr, te, all_t))
        print(
            f"{name:<28} {s['n']:5} {s['wins']:4} {s['losses']:4} {s['win_pct']:5.1f}% "
            f"{s['pnl']:10.1f} {s['avg']:8.1f} {s['max_dd']:8.1f} {s['pf']:5.2f}  "
            f"{tr['pnl']:8.1f} {te['pnl']:8.1f}"
        )
        if cut["n"]:
            print(
                f"{'  tape_end (cutoff)':<28} {cut['n']:5} {cut['wins']:4} "
                f"{cut['losses']:4} {cut['win_pct']:5.1f}% {cut['pnl']:10.1f}"
            )
    print()
    print("=== first/last 5 of each profitable-all-sample idea ===")
    for name, s, tr, te, all_t in rows:
        if s["pnl"] <= 0 or s["n"] < 20:
            continue
        print(f"\n{name} n={s['n']} pnl={s['pnl']}")
        for t in all_t[:3] + all_t[-2:]:
            print(
                f"  {t['day']} {t['side']} {t['entry_hm']}->{t['exit_hm']} "
                f"{t['reason']:7} {t['pnl']:+.1f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
