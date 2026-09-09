#!/usr/bin/env python3
"""Replay paper 1-lot long straddle on JSONL recordings (no broker).

  python3 scripts/replay_paper_straddle.py
  python3 scripts/replay_paper_straddle.py --dir ~/atlas_lite/data/recordings
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.metrics import (  # noqa: E402
    iv_pct_of_day_low,
    oi_pct_of_day_high,
)
from atlas_lite.paper_straddle import PaperStraddle, evaluate_paper_regime  # noqa: E402
from atlas_lite.recorder import RECORDING_NAME_RE, record_dir  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


class _Book:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def get(self, symbol: str) -> dict[str, Any] | None:
        return self.rows.get(symbol)

    def set_legs(self, ce_sym: str | None, pe_sym: str | None, ce: Any, pe: Any) -> None:
        if ce_sym and ce is not None:
            self.rows[ce_sym] = {"last_price": float(ce)}
        if pe_sym and pe is not None:
            self.rows[pe_sym] = {"last_price": float(pe)}


def _f(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def replay(rec_dir: Path, lot_size: int = 65) -> list[dict[str, Any]]:
    files = sorted(p for p in rec_dir.glob("*.jsonl") if RECORDING_NAME_RE.match(p.name))
    iv_low: float | None = None
    oi_high: float | None = None
    last_day = ""
    events: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        bot = PaperStraddle(path=Path(tmp) / "paper.jsonl", lot_size=lot_size)
        book = _Book()
        for path in files:
            day = path.name[:10]
            if day != last_day:
                iv_low = None
                oi_high = None
                last_day = day
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = obj.get("ts")
                    if not isinstance(ts, str):
                        continue
                    now = _parse_ts(ts)
                    if now is None:
                        continue
                    feed = dict(obj.get("feed") or {})
                    iv = _f(feed.get("iv"))
                    if iv is not None and iv > 0:
                        iv_low = iv if iv_low is None else min(iv_low, iv)
                        feed["iv_day_low"] = iv_low
                        pct = iv_pct_of_day_low(iv, iv_low)
                        if pct is not None:
                            feed["iv_vs_day_low"] = pct
                    oi = _f(feed.get("fut_oi"))
                    rec_high = _f(feed.get("fut_oi_day_high"))
                    if rec_high:
                        oi_high = rec_high if oi_high is None else max(oi_high, rec_high)
                    if oi is not None and oi > 0:
                        oi_high = oi if oi_high is None else max(oi_high, oi)
                    if oi is not None and oi_high:
                        pct = oi_pct_of_day_high(oi, oi_high)
                        if pct is not None:
                            feed["oi_vs_day_high"] = pct
                    ev = evaluate_paper_regime(feed, now=now)
                    ce_sym = obj.get("ce_symbol") or feed.get("ce_symbol")
                    pe_sym = obj.get("pe_symbol") or feed.get("pe_symbol")
                    book.set_legs(ce_sym, pe_sym, feed.get("ce"), feed.get("pe"))
                    event = bot.on_frame(
                        now=now,
                        entry_ready=ev.get("strategy") is not None,
                        strategy=ev.get("strategy"),
                        feed=feed,
                        book=book,
                        ce_symbol=ce_sym,
                        pe_symbol=pe_sym,
                        atm=obj.get("atm_strike") or feed.get("atm"),
                    )
                    if event:
                        events.append(event)
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay paper long straddle on recordings")
    parser.add_argument("--dir", type=Path, default=None)
    args = parser.parse_args()
    rec = args.dir.expanduser() if args.dir else record_dir(ROOT / "data")
    if not rec.is_dir():
        print(f"Recordings dir not found: {rec}", file=sys.stderr)
        return 1
    events = replay(rec)
    if not events:
        print(f"No paper fills in {rec} (tape filters never held in 09:15–15:14).")
        return 0
    for ev in events:
        kind = ev.get("event")
        if kind == "open":
            print(
                f"OPEN {ev.get('ts')} ATM {ev.get('atm')} "
                f"CE {ev.get('ce')} + PE {ev.get('pe')} = {ev.get('straddle')} "
                f"qty={ev.get('qty')}"
            )
        elif kind == "day_pnl":
            print(
                f"EOD {ev.get('ts')} capital={ev.get('capital')} "
                f"trades={ev.get('trades')} "
                f"pnl={ev.get('day_pnl')} "
                f"pct={float(ev.get('day_pnl_pct') or 0):+.4f}% of 2L "
                f"equity={ev.get('equity')}"
            )
        else:
            known = ev.get("pnl_known", True)
            pct = ev.get("pct")
            pct_s = "n/a" if pct is None else f"{float(pct):+.2f}%"
            print(
                f"CLOSE {ev.get('ts')} reason={ev.get('reason')} "
                f"pct={pct_s} pnl={ev.get('pnl')} known={known}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
