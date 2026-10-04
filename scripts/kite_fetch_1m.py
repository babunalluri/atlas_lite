#!/usr/bin/env python3
"""Fetch as many days of Kite 1-minute OHLC as the API allows.

Zerodha typically caps ``minute`` history at ~60 calendar days. This script
requests up to ``--days`` (default 90) in small chunks, keeps whatever returns,
and saves JSON + a compact summary.

Usage:
  python3 scripts/kite_fetch_1m.py
  python3 scripts/kite_fetch_1m.py --days 90 --source index
  python3 scripts/kite_fetch_1m.py --source fut
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.config import load_settings  # noqa: E402
from atlas_lite.kite_rest import KiteRest  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
# Minute candles are dense — keep chunks short to avoid payload/timeouts.
CHUNK_DAYS = 7


def _hm(ts: str) -> str:
    return str(ts).replace("T", " ")[11:16]


def _day(ts: str) -> str:
    return str(ts).replace("T", " ")[:10]


def _parse_candle(c: list[Any]) -> dict[str, Any] | None:
    if not c or len(c) < 5:
        return None
    ts = str(c[0]).replace("T", " ")[:16]
    try:
        o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
        v = float(c[5]) if len(c) > 5 and c[5] is not None else 0.0
    except (TypeError, ValueError):
        return None
    return {"t": ts, "o": o, "h": h, "l": l, "c": cl, "v": v}


def _nifty_index_token(data: Path) -> int | None:
    nse = data / "nse_instruments.csv"
    if not nse.is_file():
        return None
    with nse.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("tradingsymbol") or "").strip()
            if sym in ("NIFTY 50", "NIFTY50"):
                return int(row["instrument_token"])
    return None


def _nearest_nifty_fut(data: Path) -> tuple[int | None, str | None]:
    nfo = data / "nfo_instruments.csv"
    if not nfo.is_file():
        return None, None
    today = date.today()
    best: tuple[date, int, str] | None = None
    with nfo.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("name") or "").strip() != "NIFTY":
                continue
            if (row.get("instrument_type") or "").strip() != "FUT":
                continue
            exp = (row.get("expiry") or "").strip()
            try:
                ed = date.fromisoformat(exp[:10])
            except ValueError:
                continue
            if ed < today:
                continue
            tok = int(row["instrument_token"])
            sym = row.get("tradingsymbol") or ""
            if best is None or ed < best[0]:
                best = (ed, tok, sym)
    if not best:
        return None, None
    return best[1], best[2]


async def fetch_1m(
    rest: KiteRest,
    token: int,
    days: int,
    *,
    oi: int = 0,
    continuous: int = 0,
) -> list[dict[str, Any]]:
    now = datetime.now(IST)
    start = now - timedelta(days=max(1, days))
    out: dict[str, dict[str, Any]] = {}
    cursor = start
    empty_streak = 0
    while cursor < now:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), now)
        frm = cursor.strftime("%Y-%m-%d %H:%M:%S")
        to = chunk_end.strftime("%Y-%m-%d %H:%M:%S")
        n = 0
        try:
            candles = await rest.historical(
                token, "minute", from_date=frm, to_date=to, continuous=continuous, oi=oi
            )
        except Exception as exc:  # noqa: BLE001
            print(f"warn {frm}->{to}: {exc}", file=sys.stderr)
            candles = []
        for c in candles:
            bar = _parse_candle(c)
            if not bar:
                continue
            t = _hm(bar["t"])
            if t < "09:15" or t > "15:29":
                continue
            out[bar["t"]] = bar
            n += 1
        print(f"  chunk {frm[:10]}..{to[:10]} candles={n} total={len(out)}", flush=True)
        if n == 0:
            empty_streak += 1
            # early window often empty beyond API retention — keep walking forward
            if empty_streak >= 3 and len(out) == 0 and (now - cursor).days > 70:
                pass
        else:
            empty_streak = 0
        await asyncio.sleep(0.35)
        cursor = chunk_end
    return [out[k] for k in sorted(out)]


async def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch max Kite 1m history")
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Calendar days to request (Kite minute cap ~60; extras return empty)",
    )
    parser.add_argument(
        "--source",
        choices=("index", "fut", "both"),
        default="index",
        help="NIFTY 50 index (default), nearest fut, or both",
    )
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    data: Path = args.data_dir
    data.mkdir(parents=True, exist_ok=True)

    settings = load_settings()
    rest = KiteRest(settings.api_key, settings.access_token)
    saved: list[Path] = []
    try:
        await rest.check_session()
        print("Kite session OK", flush=True)

        jobs: list[tuple[str, int, str, int, int]] = []
        if args.source in ("index", "both"):
            idx = _nifty_index_token(data)
            if idx:
                jobs.append(("index", idx, "NIFTY 50", 0, 0))
            else:
                print("warn: NIFTY 50 token not found in nse_instruments.csv", file=sys.stderr)
        if args.source in ("fut", "both"):
            fut_tok, fut_sym = _nearest_nifty_fut(data)
            if fut_tok and fut_sym:
                jobs.append(("fut", fut_tok, fut_sym, 1, 0))
            else:
                print("warn: NIFTY fut token not found", file=sys.stderr)

        if not jobs:
            print("No tokens to fetch", file=sys.stderr)
            return 1

        meta: dict[str, Any] = {"requested_days": args.days, "sources": []}
        for kind, token, sym, oi, cont in jobs:
            print(f"\nFetching 1m {kind} token={token} ({sym}) days={args.days}", flush=True)
            bars = await fetch_1m(rest, token, args.days, oi=oi, continuous=cont)
            # If fut is thin (new contract), retry continuous
            if kind == "fut" and len(bars) < 500:
                print("  fut thin — retry continuous=1", flush=True)
                cont_bars = await fetch_1m(rest, token, args.days, oi=oi, continuous=1)
                if len(cont_bars) > len(bars):
                    bars = cont_bars
                    cont = 1
            if not bars:
                print(f"  no bars for {kind}", file=sys.stderr)
                continue
            days = sorted({_day(b["t"]) for b in bars})
            out_name = "kite_1m_bars.json" if kind == "index" else "kite_1m_fut_bars.json"
            out_path = data / out_name
            payload = {
                "meta": {
                    "interval": "minute",
                    "source": kind,
                    "symbol": sym,
                    "token": token,
                    "continuous": cont,
                    "oi": oi,
                    "requested_days": args.days,
                    "n_bars": len(bars),
                    "n_sessions": len(days),
                    "from": days[0],
                    "to": days[-1],
                    "fetched_at": datetime.now(IST).isoformat(),
                    "note": "Kite minute history typically retained ~60 calendar days",
                },
                "bars": bars,
            }
            out_path.write_text(json.dumps(payload), encoding="utf-8")
            saved.append(out_path)
            meta["sources"].append(payload["meta"])
            print(
                f"  saved {out_path} bars={len(bars)} sessions={len(days)} "
                f"{days[0]} -> {days[-1]}",
                flush=True,
            )

        (data / "kite_1m_fetch_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print("\nDone:", json.dumps(meta, indent=2))
        return 0 if saved else 1
    finally:
        await rest.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
