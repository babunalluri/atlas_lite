#!/usr/bin/env python3
"""Download ~1y NIFTY 5m + India VIX day from Kite; report afternoon regimes.

Usage:
  python3 scripts/kite_regime_history.py
  python3 scripts/kite_regime_history.py --days 365 --out data/regime
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.config import load_settings  # noqa: E402
from atlas_lite.instruments import lookup_token  # noqa: E402
from atlas_lite.kite_rest import KiteRest  # noqa: E402
from atlas_lite.specs import NIFTY_SYMBOL, VIX_SYMBOLS  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = ROOT / "data"

# Kite per-request caps (days)
CHUNK_BY_INTERVAL = {
    "minute": 60,
    "5minute": 100,
    "15minute": 200,
    "60minute": 400,
    "day": 2000,
}


def _load_csvs() -> List[str]:
    csvs: List[str] = []
    for name in ("nfo_instruments.csv", "nse_instruments.csv", "bse_instruments.csv"):
        path = DATA_DIR / name
        if path.is_file():
            csvs.append(path.read_text(encoding="utf-8"))
    if not csvs:
        raise FileNotFoundError(
            f"No instrument CSVs under {DATA_DIR}. Start ./run.sh once, then retry."
        )
    return csvs


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _candle_dt(raw: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


async def fetch_chunked(
    rest: KiteRest,
    token: int,
    interval: str,
    *,
    days: int,
    sleep_s: float = 0.4,
) -> List[List[Any]]:
    chunk = CHUNK_BY_INTERVAL.get(interval, 60)
    now = datetime.now(IST)
    start = now - timedelta(days=days)
    out: List[List[Any]] = []
    seen: set = set()
    cursor = start
    while cursor < now:
        end = min(cursor + timedelta(days=chunk - 1), now)
        candles = await rest.historical(
            token,
            interval,
            from_date=_fmt(cursor),
            to_date=_fmt(end),
        )
        for c in candles:
            if not isinstance(c, (list, tuple)) or len(c) < 5:
                continue
            key = str(c[0])
            if key in seen:
                continue
            seen.add(key)
            out.append(list(c))
        cursor = end + timedelta(days=1)
        await asyncio.sleep(sleep_s)
    out.sort(key=lambda c: str(c[0]))
    return out


def _session_slices(
    nifty_5m: List[List[Any]],
) -> Dict[str, Dict[str, Any]]:
    """Per trading day: open, noon, close, afternoon range, afternoon |move|."""
    by_day: DefaultDict[str, List[Tuple[datetime, float, float, float, float]]] = (
        defaultdict(list)
    )
    for c in nifty_5m:
        dt = _candle_dt(c[0])
        if dt is None:
            continue
        # NSE cash session roughly 09:15–15:30
        if dt.hour < 9 or (dt.hour == 9 and dt.minute < 15):
            continue
        if dt.hour > 15 or (dt.hour == 15 and dt.minute > 30):
            continue
        o, h, low, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
        by_day[dt.date().isoformat()].append((dt, o, h, low, cl))

    days: Dict[str, Dict[str, Any]] = {}
    for day, bars in by_day.items():
        bars.sort(key=lambda x: x[0])
        if len(bars) < 10:
            continue
        open_px = bars[0][1]
        close_px = bars[-1][4]
        noon = next((b for b in bars if b[0].hour == 12 and b[0].minute == 0), None)
        aft = [b for b in bars if (b[0].hour > 13) or (b[0].hour == 13 and b[0].minute >= 0)]
        if not aft:
            continue
        aft_hi = max(b[2] for b in aft)
        aft_lo = min(b[3] for b in aft)
        aft_open = aft[0][1]
        aft_close = aft[-1][4]
        days[day] = {
            "open": round(open_px, 2),
            "close": round(close_px, 2),
            "day_chg_pct": round(100.0 * (close_px - open_px) / open_px, 3),
            "noon": round(noon[4], 2) if noon else None,
            "aft_range": round(aft_hi - aft_lo, 2),
            "aft_move": round(aft_close - aft_open, 2),
            "aft_move_pct": round(100.0 * (aft_close - aft_open) / aft_open, 3),
            "aft_range_pct": round(100.0 * (aft_hi - aft_lo) / aft_open, 3),
        }
    return days


def _vix_map(vix_day: List[List[Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for c in vix_day:
        dt = _candle_dt(c[0])
        if dt is None:
            continue
        out[dt.date().isoformat()] = float(c[4])
    return out


def _bucket_vix(v: float) -> str:
    if v < 12:
        return "VIX<12"
    if v < 14:
        return "12-14"
    if v < 16:
        return "14-16"
    if v < 18:
        return "16-18"
    return "VIX>=18"


def regime_report(
    session: Dict[str, Dict[str, Any]],
    vix: Dict[str, float],
) -> Dict[str, Any]:
    """Summarize when afternoon is quiet (short-premium friendly context)."""
    rows = []
    for day, s in sorted(session.items()):
        rows.append(
            {
                "date": day,
                **s,
                "vix": vix.get(day),
            }
        )

    def summarize(subset: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
        if not subset:
            return {"label": label, "n": 0}
        ranges = [float(r["aft_range_pct"]) for r in subset]
        moves = [abs(float(r["aft_move_pct"])) for r in subset]
        quiet = sum(1 for r in subset if float(r["aft_range_pct"]) < 0.35)
        return {
            "label": label,
            "n": len(subset),
            "avg_aft_range_pct": round(sum(ranges) / len(ranges), 3),
            "avg_abs_aft_move_pct": round(sum(moves) / len(moves), 3),
            "quiet_aft_pct": round(100.0 * quiet / len(subset), 1),
            "median_aft_range_pct": round(sorted(ranges)[len(ranges) // 2], 3),
        }

    by_vix: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_dow: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("vix") is not None:
            by_vix[_bucket_vix(float(r["vix"]))].append(r)
        dow = datetime.fromisoformat(r["date"]).strftime("%a")
        by_dow[dow].append(r)

    # Quiet afternoon heuristic: range < 0.35% of spot from 13:00
    quiet_days = [r for r in rows if float(r["aft_range_pct"]) < 0.35]
    wild_days = [r for r in rows if float(r["aft_range_pct"]) >= 0.60]

    return {
        "days": len(rows),
        "overall": summarize(rows, "all"),
        "quiet_afternoons": summarize(quiet_days, "aft_range<0.35%"),
        "wild_afternoons": summarize(wild_days, "aft_range>=0.60%"),
        "by_vix": [
            summarize(by_vix[k], k)
            for k in ("VIX<12", "12-14", "14-16", "16-18", "VIX>=18")
        ],
        "by_weekday": [
            summarize(by_dow[k], k)
            for k in ("Mon", "Tue", "Wed", "Thu", "Fri")
        ],
        "note": (
            "Quiet afternoon (low aft_range_pct) is the index context that "
            "usually favors short ATM premium 13:00→15:20 — not a fill guarantee."
        ),
    }


def _print_report(report: Dict[str, Any]) -> None:
    print(f"session_days={report['days']}")
    print(report["note"])
    print()
    for key in ("overall", "quiet_afternoons", "wild_afternoons"):
        s = report[key]
        if not s.get("n"):
            print(f"{s.get('label')}: n=0")
            continue
        print(
            f"{s['label']}: n={s['n']} avg_range={s['avg_aft_range_pct']}% "
            f"avg_|move|={s['avg_abs_aft_move_pct']}% quiet%={s['quiet_aft_pct']}"
        )
    print()
    print("=== By VIX ===")
    for s in report["by_vix"]:
        if not s.get("n"):
            print(f"{s['label']}: n=0")
            continue
        print(
            f"{s['label']}: n={s['n']} avg_range={s['avg_aft_range_pct']}% "
            f"quiet%={s['quiet_aft_pct']}"
        )
    print()
    print("=== By weekday ===")
    for s in report["by_weekday"]:
        if not s.get("n"):
            print(f"{s['label']}: n=0")
            continue
        print(
            f"{s['label']}: n={s['n']} avg_range={s['avg_aft_range_pct']}% "
            f"quiet%={s['quiet_aft_pct']}"
        )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch 1y NIFTY 5m + VIX day and score afternoon regimes"
    )
    parser.add_argument("--days", type=int, default=365, help="Lookback calendar days")
    parser.add_argument(
        "--out",
        type=Path,
        default=DATA_DIR / "regime",
        help="Output directory for candles + report JSON",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Reuse candles already under --out",
    )
    args = parser.parse_args()
    out_dir = args.out.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    nifty_path = out_dir / "nifty_5m.json"
    vix_path = out_dir / "vix_day.json"
    report_path = out_dir / "regime_report.json"

    if args.skip_fetch and nifty_path.is_file() and vix_path.is_file():
        nifty_5m = json.loads(nifty_path.read_text(encoding="utf-8"))
        vix_day = json.loads(vix_path.read_text(encoding="utf-8"))
    else:
        csvs = _load_csvs()
        nifty_token = lookup_token(csvs, NIFTY_SYMBOL)
        if nifty_token is None:
            raise RuntimeError(f"Token not found for {NIFTY_SYMBOL}")
        vix_token = None
        for sym in VIX_SYMBOLS:
            vix_token = lookup_token(csvs, sym)
            if vix_token is not None:
                break
        if vix_token is None:
            raise RuntimeError("Token not found for India VIX")

        settings = load_settings()
        rest = KiteRest(settings.api_key, settings.access_token)
        t0 = time.perf_counter()
        try:
            print(f"Fetching NIFTY 5m ~{args.days}d …")
            nifty_5m = await fetch_chunked(
                rest, nifty_token, "5minute", days=args.days
            )
            print(f"  candles={len(nifty_5m)}")
            print(f"Fetching India VIX day ~{args.days}d …")
            vix_day = await fetch_chunked(rest, vix_token, "day", days=args.days)
            print(f"  candles={len(vix_day)}")
        finally:
            await rest.close()
        print(f"fetch_elapsed_s={time.perf_counter() - t0:.1f}")

        nifty_path.write_text(json.dumps(nifty_5m), encoding="utf-8")
        vix_path.write_text(json.dumps(vix_day), encoding="utf-8")

    session = _session_slices(nifty_5m)
    vix = _vix_map(vix_day)
    report = regime_report(session, vix)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _print_report(report)
    print()
    print(f"wrote {nifty_path}")
    print(f"wrote {vix_path}")
    print(f"wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
