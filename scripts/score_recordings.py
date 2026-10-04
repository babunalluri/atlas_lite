#!/usr/bin/env python3
"""Score candidate strategies against Atlas Lite JSONL recordings.

Usage:
  python3 scripts/score_recordings.py
  python3 scripts/score_recordings.py --dir ~/atlas_lite/data/recordings
  python3 scripts/score_recordings.py --dir /path/to/recordings --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, DefaultDict, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.recorder import (  # noqa: E402
    iter_jsonl_dicts,
    list_slot_recording_paths,
    record_dir,
)

IST = ZoneInfo("Asia/Kolkata")
MinuteRow = Dict[str, Any]
DayMap = DefaultDict[str, Dict[str, MinuteRow]]


def _fnum(x: Any) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _parse_ts(raw: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def load_minute_rows(rec_dir: Path) -> List[MinuteRow]:
    """Collapse change-driven JSONL into one sample per IST minute (last wins)."""
    by_min: Dict[Tuple[str, str], MinuteRow] = {}
    for path in list_slot_recording_paths(rec_dir):
        for obj in iter_jsonl_dicts(path):
            ts = obj.get("ts")
            if not isinstance(ts, str):
                continue
            dt = _parse_ts(ts)
            if dt is None:
                continue
            feed = obj.get("feed") if isinstance(obj.get("feed"), dict) else {}
            ce = _fnum(feed.get("ce"))
            pe = _fnum(feed.get("pe"))
            key = (dt.date().isoformat(), dt.strftime("%H:%M"))
            by_min[key] = {
                "date": key[0],
                "hm": key[1],
                "hour": dt.hour,
                "minute": dt.minute,
                "ce": ce,
                "pe": pe,
                "straddle": (ce + pe) if ce is not None and pe is not None else None,
                "spot": _fnum(obj.get("spot") or feed.get("nifty_ltp")),
                "atr": _fnum(feed.get("atr")),
                "adx": _fnum(feed.get("adx")),
                "pcr": _fnum(feed.get("pcr")),
                "ivp": _fnum(feed.get("ivp")),
                "mp": _fnum(feed.get("max_pain")),
                "vix": _fnum(feed.get("vix_chg")),
                "nifty_chg": _fnum(feed.get("index_nifty_chg")),
                "iv": _fnum(feed.get("iv")),
            }
    return sorted(by_min.values(), key=lambda r: (r["date"], r["hm"]))


def _hm_to_min(hm: str) -> int:
    h, m = map(int, hm.split(":"))
    return h * 60 + m


def _index_by_day(rows: List[MinuteRow]) -> DayMap:
    out: DayMap = defaultdict(dict)
    for row in rows:
        out[row["date"]][row["hm"]] = row
    return out


def _simulate(
    by_day: DayMap,
    entry: MinuteRow,
    field: str,
    *,
    target: float,
    stop: float,
    until: str,
    side: int,
) -> Optional[Tuple[str, float, float, str]]:
    """Return (label, pct, pnl, exit_hm). side=+1 long, -1 short."""
    price = entry.get(field)
    if price is None or price <= 0:
        return None
    day = entry["date"]
    t0 = _hm_to_min(entry["hm"])
    t1 = _hm_to_min(until)
    for t in range(t0 + 1, t1 + 1):
        hm = f"{t // 60:02d}:{t % 60:02d}"
        row = by_day[day].get(hm)
        if not row or row.get(field) is None:
            continue
        pnl = side * (row[field] - price)
        pct = pnl / price
        if pct >= target:
            return ("WIN", pct, pnl, hm)
        if pct <= stop:
            return ("LOSS", pct, pnl, hm)
    end = by_day[day].get(until)
    if not end or end.get(field) is None:
        return None
    pnl = side * (end[field] - price)
    return ("TIME", pnl / price, pnl, until)


Pred = Callable[[MinuteRow], bool]


STRATEGIES: List[Tuple[str, Pred, str, int, float, float, str, str, str]] = [
    (
        "SHORT_STR @12:00",
        lambda r: r["hm"] >= "12:00" and r["hm"] <= "12:05" and r["straddle"],
        "straddle",
        -1,
        0.04,
        -0.06,
        "15:20",
        "12:00",
        "12:05",
    ),
    (
        "SHORT_STR @13:00",
        lambda r: r["hm"] >= "13:00" and r["hm"] <= "13:05" and r["straddle"],
        "straddle",
        -1,
        0.04,
        -0.06,
        "15:20",
        "13:00",
        "13:05",
    ),
    (
        "LONG_STR ATR7-10 PCR0.7-0.9 AM",
        lambda r: (
            r.get("atr") is not None
            and 7 <= r["atr"] <= 10
            and r.get("pcr") is not None
            and 0.7 <= r["pcr"] <= 0.9
            and r["straddle"]
        ),
        "straddle",
        1,
        0.05,
        -0.08,
        "14:30",
        "09:45",
        "11:30",
    ),
    (
        "LONG_CE PCR<0.75 ADX>20",
        lambda r: (
            r.get("pcr") is not None
            and r["pcr"] < 0.75
            and r.get("adx") is not None
            and r["adx"] > 20
            and r["ce"]
        ),
        "ce",
        1,
        0.08,
        -0.08,
        "14:30",
        "09:45",
        "11:30",
    ),
    (
        "LONG_PE PCR>1.2 ADX>20",
        lambda r: (
            r.get("pcr") is not None
            and r["pcr"] > 1.2
            and r.get("adx") is not None
            and r["adx"] > 20
            and r["pe"]
        ),
        "pe",
        1,
        0.08,
        -0.08,
        "14:30",
        "09:45",
        "11:30",
    ),
]


def score_strategies(rows: List[MinuteRow]) -> List[Dict[str, Any]]:
    by_day = _index_by_day(rows)
    reports: List[Dict[str, Any]] = []
    for name, pred, field, side, target, stop, until, after, before in STRATEGIES:
        trades: List[Dict[str, Any]] = []
        for day in sorted(by_day):
            hit: Optional[MinuteRow] = None
            for hm in sorted(by_day[day]):
                if hm < after or hm > before:
                    continue
                row = by_day[day][hm]
                if pred(row):
                    hit = row
                    break
            if hit is None:
                continue
            sim = _simulate(
                by_day, hit, field, target=target, stop=stop, until=until, side=side
            )
            if sim is None:
                continue
            label, pct, pnl, exit_hm = sim
            trades.append(
                {
                    "date": day,
                    "entry": hit["hm"],
                    "exit": exit_hm,
                    "result": label,
                    "pct": round(pct, 4),
                    "pnl": round(pnl, 2),
                }
            )
        wins = sum(
            1
            for t in trades
            if t["result"] == "WIN" or (t["result"] == "TIME" and t["pnl"] > 0)
        )
        losses = len(trades) - wins
        pnl_sum = sum(float(t["pnl"]) for t in trades)
        reports.append(
            {
                "strategy": name,
                "trades": len(trades),
                "wins": wins,
                "losses": losses,
                "sum_pnl": round(pnl_sum, 2),
                "details": trades,
            }
        )
    return reports


def afternoon_decay_table(rows: List[MinuteRow]) -> List[Dict[str, Any]]:
    by_day = _index_by_day(rows)
    out: List[Dict[str, Any]] = []
    for entry_hm in ("11:00", "12:00", "13:00", "14:00"):
        for day, day_rows in sorted(by_day.items()):
            entry = day_rows.get(entry_hm)
            if not entry or entry.get("straddle") is None:
                continue
            for exit_hm in ("14:30", "15:20"):
                end = day_rows.get(exit_hm)
                if not end or end.get("straddle") is None:
                    continue
                long_pnl = end["straddle"] - entry["straddle"]
                out.append(
                    {
                        "date": day,
                        "entry": entry_hm,
                        "exit": exit_hm,
                        "long_pnl": round(long_pnl, 2),
                        "short_pnl": round(-long_pnl, 2),
                        "entry_px": round(entry["straddle"], 2),
                        "exit_px": round(end["straddle"], 2),
                    }
                )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Score strategies on JSONL recordings")
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Recordings directory (default: data/recordings or ATLAS_LITE_RECORD_DIR)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON summary")
    args = parser.parse_args()

    rec_dir = args.dir.expanduser() if args.dir else record_dir(ROOT / "data")
    if not rec_dir.is_dir():
        print(f"Recordings dir not found: {rec_dir}", file=sys.stderr)
        return 1

    rows = load_minute_rows(rec_dir)
    if not rows:
        print(f"No minute samples in {rec_dir}", file=sys.stderr)
        return 1

    reports = score_strategies(rows)
    decay = afternoon_decay_table(rows)
    dates = sorted({r["date"] for r in rows})

    if args.json:
        print(
            json.dumps(
                {
                    "dir": str(rec_dir),
                    "minutes": len(rows),
                    "dates": dates,
                    "strategies": reports,
                    "afternoon_decay": decay,
                },
                indent=2,
            )
        )
        return 0

    print(f"dir={rec_dir}")
    print(f"minutes={len(rows)} dates={dates}")
    print()
    print("=== Strategies (1 trade/day, walk-forward) ===")
    for rep in reports:
        print(
            f"{rep['strategy']}: trades={rep['trades']} "
            f"W={rep['wins']} L={rep['losses']} sumPnL={rep['sum_pnl']:+.1f}"
        )
        for t in rep["details"]:
            print(
                f"  {t['date']} {t['entry']}->{t['exit']} "
                f"{t['result']} pct={t['pct']:+.1%} pnl={t['pnl']:+.1f}"
            )
    print()
    print("=== Afternoon ATM straddle decay (short = -long) ===")
    for row in decay:
        print(
            f"{row['date']} {row['entry']}->{row['exit']} "
            f"long={row['long_pnl']:+.1f} short={row['short_pnl']:+.1f} "
            f"({row['entry_px']:.1f}->{row['exit_px']:.1f})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
