#!/usr/bin/env python3
"""Score ATM option ideas on Atlas recordings with Kite NFO charges (1 lot)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402
from atlas_lite.recorder import record_dir  # noqa: E402
from scripts.score_recordings import load_minute_rows, _index_by_day, _hm_to_min  # noqa: E402

QTY = 65


def _ch(legs: list[tuple[float, int, str]]) -> float:
    return float(kite_nfo_charges(legs)["total"])


def _settle(trades: list[dict], *, exclude: tuple[str, ...] = ()) -> dict:
    if exclude:
        trades = [t for t in trades if t.get("reason") not in exclude]
    if not trades:
        return {"n": 0, "wins": 0, "pnl": 0.0, "win_pct": 0.0, "avg": 0.0, "max_dd": 0.0}
    pnl = [float(t["pnl"]) for t in trades]
    eq = peak = dd = 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    wins = sum(1 for x in pnl if x > 0)
    return {
        "n": len(trades),
        "wins": wins,
        "pnl": round(sum(pnl), 2),
        "win_pct": round(100.0 * wins / len(pnl), 1),
        "avg": round(sum(pnl) / len(pnl), 2),
        "max_dd": round(dd, 2),
    }


def _walk(by_day, entry, field, side, until, target=None, stop=None):
    """side +1 long premium, -1 short. target/stop are fraction of entry (optional)."""
    px0 = entry.get(field)
    if px0 is None or px0 <= 0:
        return None
    day = entry["date"]
    last: tuple[float, str] | None = None
    # Entry minute is the fill, not a stop bar — start at the next minute.
    for t in range(_hm_to_min(entry["hm"]) + 1, _hm_to_min(until) + 1):
        hm = f"{t // 60:02d}:{t % 60:02d}"
        row = by_day[day].get(hm)
        if not row or row.get(field) is None:
            continue
        px = row[field]
        last = (px, hm)
        pct = side * (px - px0) / px0
        if target is not None and pct >= target:
            return px, hm, "target"
        if stop is not None and pct <= stop:
            return px, hm, "stop"
    end = by_day[day].get(until)
    if end and end.get(field) is not None:
        return end[field], until, "time"
    if last is not None:
        return last[0], last[1], "tape_end"
    return None


def _trade(entry_px, exit_px, side, qty, n_legs, reason, extra):
    # Each structure: open n_legs, close n_legs. Short: sell then buy. Long: buy then sell.
    open_side = "sell" if side < 0 else "buy"
    close_side = "buy" if side < 0 else "sell"
    # Approximate equal split across legs for charges (ATM CE≈PE).
    per = entry_px / n_legs
    per_x = exit_px / n_legs
    legs = [(per, qty, open_side)] * n_legs + [(per_x, qty, close_side)] * n_legs
    ch = _ch(legs)
    gross = side * (exit_px - entry_px) * qty
    body = {
        "gross": round(gross, 2),
        "charges": ch,
        "pnl": round(gross - ch, 2),
        "reason": reason,
        **extra,
    }
    return body


def run_clock(by_day, field, side, after, before, until, target, stop, n_legs):
    trades = []
    for day in sorted(by_day):
        hit = None
        for hm in sorted(by_day[day]):
            if hm < after or hm > before:
                continue
            row = by_day[day][hm]
            if row.get(field):
                hit = row
                break
        if hit is None:
            continue
        walked = _walk(by_day, hit, field, side, until, target, stop)
        if walked is None:
            continue
        px1, ehm, reason = walked
        trades.append(
            _trade(
                hit[field],
                px1,
                side,
                QTY,
                n_legs,
                reason,
                {"date": day, "entry": hit["hm"], "exit": ehm, "px0": hit[field], "px1": px1},
            )
        )
    return trades


def run_pred(by_day, pred, field, side, after, before, until, target, stop, n_legs):
    trades = []
    for day in sorted(by_day):
        hit = None
        for hm in sorted(by_day[day]):
            if hm < after or hm > before:
                continue
            row = by_day[day][hm]
            if pred(row):
                hit = row
                break
        if hit is None:
            continue
        walked = _walk(by_day, hit, field, side, until, target, stop)
        if walked is None:
            continue
        px1, ehm, reason = walked
        trades.append(
            _trade(
                hit[field],
                px1,
                side,
                QTY,
                n_legs,
                reason,
                {"date": day, "entry": hit["hm"], "exit": ehm, "px0": hit[field], "px1": px1},
            )
        )
    return trades


def main() -> int:
    rec = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else record_dir(ROOT / "data")
    rows = load_minute_rows(rec)
    by_day = _index_by_day(rows)
    print(f"dir={rec} minutes={len(rows)} days={sorted(by_day)}")
    ideas = [
        ("SHORT STR 10:00 hold", lambda: run_clock(by_day, "straddle", -1, "10:00", "10:05", "15:14", None, None, 2)),
        ("SHORT STR 11:00 hold", lambda: run_clock(by_day, "straddle", -1, "11:00", "11:05", "15:14", None, None, 2)),
        ("SHORT STR 12:00 hold", lambda: run_clock(by_day, "straddle", -1, "12:00", "12:05", "15:14", None, None, 2)),
        ("SHORT STR 13:00 hold", lambda: run_clock(by_day, "straddle", -1, "13:00", "13:05", "15:14", None, None, 2)),
        ("SHORT STR 14:00 hold", lambda: run_clock(by_day, "straddle", -1, "14:00", "14:05", "15:14", None, None, 2)),
        ("SHORT STR 13:00 4/-6%", lambda: run_clock(by_day, "straddle", -1, "13:00", "13:05", "15:14", 0.04, -0.06, 2)),
        ("SHORT STR 12:00 4/-6%", lambda: run_clock(by_day, "straddle", -1, "12:00", "12:05", "15:14", 0.04, -0.06, 2)),
        ("LONG STR 09:45 hold", lambda: run_clock(by_day, "straddle", 1, "09:45", "09:50", "15:14", None, None, 2)),
        ("LONG STR 09:45 8/-8%", lambda: run_clock(by_day, "straddle", 1, "09:45", "09:50", "15:14", 0.08, -0.08, 2)),
        (
            "LONG CE ADX>20 PCR<0.8",
            lambda: run_pred(
                by_day,
                lambda r: r.get("ce") and r.get("adx") and r["adx"] > 20 and r.get("pcr") is not None and r["pcr"] < 0.8,
                "ce",
                1,
                "09:45",
                "11:30",
                "15:14",
                0.10,
                -0.10,
                1,
            ),
        ),
        (
            "LONG PE ADX>20 PCR>1.1",
            lambda: run_pred(
                by_day,
                lambda r: r.get("pe") and r.get("adx") and r["adx"] > 20 and r.get("pcr") is not None and r["pcr"] > 1.1,
                "pe",
                1,
                "09:45",
                "11:30",
                "15:14",
                0.10,
                -0.10,
                1,
            ),
        ),
        (
            "LONG CE nifty>+0.35 ADX>18",
            lambda: run_pred(
                by_day,
                lambda r: r.get("ce")
                and r.get("adx")
                and r["adx"] > 18
                and r.get("nifty_chg") is not None
                and r["nifty_chg"] > 0.35,
                "ce",
                1,
                "10:30",
                "11:15",
                "15:14",
                0.10,
                -0.08,
                1,
            ),
        ),
        (
            "LONG PE nifty<-0.35 ADX>18",
            lambda: run_pred(
                by_day,
                lambda r: r.get("pe")
                and r.get("adx")
                and r["adx"] > 18
                and r.get("nifty_chg") is not None
                and r["nifty_chg"] < -0.35,
                "pe",
                1,
                "10:30",
                "11:15",
                "15:14",
                0.10,
                -0.08,
                1,
            ),
        ),
        (
            "SHORT CE PCR>1.15",
            lambda: run_pred(
                by_day,
                lambda r: r.get("ce") and r.get("pcr") is not None and r["pcr"] > 1.15,
                "ce",
                -1,
                "10:00",
                "13:00",
                "15:14",
                0.08,
                -0.10,
                1,
            ),
        ),
        (
            "SHORT PE PCR<0.75",
            lambda: run_pred(
                by_day,
                lambda r: r.get("pe") and r.get("pcr") is not None and r["pcr"] < 0.75,
                "pe",
                -1,
                "10:00",
                "13:00",
                "15:14",
                0.08,
                -0.10,
                1,
            ),
        ),
        (
            "SHORT STR |chg|<0.35 @12:00",
            lambda: run_pred(
                by_day,
                lambda r: r.get("straddle") and r.get("spot"),
                "straddle",
                -1,
                "12:00",
                "12:10",
                "15:14",
                None,
                None,
                2,
            ),
        ),
    ]
    print(f"{'idea':<32} {'n':>3} {'W':>3} {'win%':>6} {'pnl':>10} {'avg':>8} {'dd':>8}")
    best = None
    for name, fn in ideas:
        trades = fn()
        s = _settle(trades, exclude=("tape_end",))
        cut = _settle([t for t in trades if t.get("reason") == "tape_end"])
        print(
            f"{name:<32} {s['n']:3} {s['wins']:3} {s['win_pct']:5.1f}% "
            f"{s['pnl']:10.1f} {s['avg']:8.1f} {s['max_dd']:8.1f}"
        )
        if cut["n"]:
            print(
                f"{'  tape_end (cutoff)':<32} {cut['n']:3} {cut['wins']:3} "
                f"{cut['win_pct']:5.1f}% {cut['pnl']:10.1f}"
            )
        if s["n"] and (best is None or s["pnl"] > best[1]["pnl"]):
            best = (name, s, trades)
        if s["pnl"] > 0 and s["n"]:
            for t in trades:
                if t.get("reason") == "tape_end":
                    continue
                print(
                    f"  {t['date']} {t['entry']}->{t['exit']} {t['reason']:6} "
                    f"gross={t['gross']:+.0f} ch={t['charges']:.0f} pnl={t['pnl']:+.0f}"
                )
    if best:
        print(f"\nbest by pnl: {best[0]} {best[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
