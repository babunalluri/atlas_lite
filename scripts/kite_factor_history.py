#!/usr/bin/env python3
"""Download ~1 year of factor_paper inputs from Kite.

What Kite serves (checked 2026-10-28):
  - NIFTY 50 and INDIA VIX 1-minute candles: a full year (requested in 7-day chunks).
  - NIFTY futures OI: daily only for history (continuous=1). Continuous minute
    candles return 400, and expired contracts are gone, so 1-minute OI exists
    only for contracts still trading (~2-3 months).
  - Option premiums: not available for expired contracts.

Writes (under data/, not in git):
  data/kite_history/nifty_1m.json, vix_1m.json, nifty_fut_day_oi.json
  data/kite_spot_cache.pkl   same shape as data/spot_cache.pkl:
                             {day: {"HH:MM": {"o","h","l","c","vix"}}}

Usage:
  python3 scripts/kite_factor_history.py
  python3 scripts/kite_factor_history.py --days 365
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import pickle
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from atlas_lite.config import load_settings  # noqa: E402
from atlas_lite.kite_rest import KiteRest  # noqa: E402
from kite_fetch_1m import IST, fetch_1m  # noqa: E402

NIFTY_TOKEN = 256265
VIX_TOKEN = 264969


def _near_fut(data: Path) -> tuple[int, str]:
    rows = []
    with (data / "nfo_instruments.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("name") == "NIFTY" and row.get("instrument_type") == "FUT":
                if (row.get("expiry") or "") >= date.today().isoformat():
                    rows.append((row["expiry"], int(row["instrument_token"]), row["tradingsymbol"]))
    if not rows:
        raise SystemExit("no live NIFTY future in data/nfo_instruments.csv; start ./run.sh once")
    _, tok, sym = min(rows)
    return tok, sym


def _check(path: Path, bars: list[dict[str, Any]], what: str) -> None:
    """Refuse to replace a file with fewer bars (expired token, rate limit, network)."""
    if not bars:
        raise SystemExit(f"{what}: Kite returned no bars; existing files left untouched")
    if path.is_file():
        try:
            old = len(json.loads(path.read_text(encoding="utf-8")).get("bars") or [])
        except (OSError, ValueError):
            old = 0
        if len(bars) < old:
            raise SystemExit(
                f"{what}: got {len(bars)} bars, existing file has {old}; existing files left untouched"
            )


def _save(path: Path, meta: dict[str, Any], bars: list[dict[str, Any]]) -> None:
    days = sorted({b["t"][:10] for b in bars})
    meta = {
        **meta,
        "n_bars": len(bars),
        "n_sessions": len(days),
        "from": days[0] if days else None,
        "to": days[-1] if days else None,
        "fetched_at": datetime.now(IST).isoformat(),
    }
    path.write_text(json.dumps({"meta": meta, "bars": bars}), encoding="utf-8")
    print(f"  saved {path.relative_to(ROOT)} bars={len(bars)} sessions={len(days)} {meta['from']}..{meta['to']}")


def build_spot_cache(nifty: list[dict[str, Any]], vix: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    vix_at = {b["t"]: b["c"] for b in vix}
    out: dict[str, dict[str, Any]] = {}
    for b in nifty:
        day, hm = b["t"][:10], b["t"][11:16]
        bar = {"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
        if b["t"] in vix_at:
            bar["vix"] = vix_at[b["t"]]
        out.setdefault(day, {})[hm] = bar
    # Same rule as build_spot_cache.py: drop half sessions.
    return {d: m for d, m in out.items() if len(m) > 200}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    data: Path = args.data_dir
    out_dir = data / "kite_history"
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = load_settings()
    rest = KiteRest(settings.api_key, settings.access_token)
    try:
        await rest.check_session()
        print("Kite session OK", flush=True)

        print(f"\nNIFTY 50 1m, {args.days} days", flush=True)
        nifty = await fetch_1m(rest, NIFTY_TOKEN, args.days)
        _check(out_dir / "nifty_1m.json", nifty, "NIFTY 50")

        print(f"\nINDIA VIX 1m, {args.days} days", flush=True)
        vix = await fetch_1m(rest, VIX_TOKEN, args.days)
        _check(out_dir / "vix_1m.json", vix, "INDIA VIX")
        if len(vix) < 0.95 * len(nifty):
            raise SystemExit(f"VIX has {len(vix)} bars vs NIFTY {len(nifty)}; existing files left untouched")

        # Write only after both 1-minute series came back whole.
        _save(out_dir / "nifty_1m.json", {"symbol": "NIFTY 50", "token": NIFTY_TOKEN, "interval": "minute"}, nifty)
        _save(out_dir / "vix_1m.json", {"symbol": "INDIA VIX", "token": VIX_TOKEN, "interval": "minute"}, vix)

        fut_tok, fut_sym = _near_fut(data)
        print(f"\nNIFTY futures daily OI (continuous via {fut_sym})", flush=True)
        now = datetime.now(IST)
        raw = await rest.historical(
            fut_tok,
            "day",
            from_date=(now - timedelta(days=args.days)).strftime("%Y-%m-%d 00:00:00"),
            to_date=now.strftime("%Y-%m-%d 23:59:59"),
            continuous=1,
            oi=1,
        )
        fut = [
            {"t": str(c[0])[:10], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5], "oi": c[6]}
            for c in raw
            if len(c) >= 7
        ]
        _save(
            out_dir / "nifty_fut_day_oi.json",
            {"symbol": fut_sym, "token": fut_tok, "interval": "day", "continuous": 1, "oi": 1},
            fut,
        )
    finally:
        await rest.close()

    cache = build_spot_cache(nifty, vix)
    cache_path = data / "kite_spot_cache.pkl"
    cache_path.write_bytes(pickle.dumps(cache))
    days = sorted(cache)
    print(f"\nspot cache: {cache_path.relative_to(ROOT)} {len(days)} sessions {days[0]}..{days[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
