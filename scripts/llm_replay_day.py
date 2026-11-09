"""Replay one recorded day through the CURRENT atlas_lite/llm_buyer.py (prompt, rules, risk as in the code) with real
LLM calls, one decision a minute, 09:15-15:10.

Inputs come from data/recordings (the laptop copy pulled each morning):
  - feed per minute: the last dashboard snapshot in each minute;
  - today's 1-minute NIFTY candles built from the recorded spot (no volume, so no VWAP, same as live);
  - previous session open/high/low/close from the previous full recorded day (a day with < 300 minutes is skipped);
  - option prices from the structure tape (ATM, +-50/100/250), interpolated across recorded strikes; no bid/ask in
    the tape, so buys/sells are last +-1 pt (the book's SLIP_PTS).
The built day is cached as data/replays/day_<date>.pkl. Costs real OpenRouter credit (~315 calls a day).

Usage:
  .venv/bin/python scripts/llm_replay_day.py 2026-11-09
  .venv/bin/python scripts/llm_replay_day.py 2026-11-09 --model anthropic/claude-opus-5.5
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import pickle
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite import llm_buyer  # noqa: E402
from atlas_lite.config import read_llm_credentials  # noqa: E402
from atlas_lite.llm_buyer import LlmBuyer, LlmDecider  # noqa: E402

REC = ROOT / "data" / "recordings"
OUT = ROOT / "data" / "replays"
KEYS = ("index_nifty_chg", "index_sensex_chg", "vix", "vix_chg", "pcr", "ivp", "ce", "pe", "ce_symbol", "pe_symbol",
        "atm", "iv", "iv_day_high", "iv_day_low", "oi_vs_day_high", "adx", "spot", "expiry")
MIN_FULL_DAY = 300


def _lines(path: str):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                pass


def _feed_minutes(day: str) -> dict[str, dict]:
    feed: dict[str, dict] = {}
    for path in sorted(glob.glob(f"{REC}/{day}_*.jsonl*")):
        for o in _lines(path):
            f, hm = o.get("feed"), str(o.get("ts", ""))[11:16]
            if f and "09:15" <= hm <= "15:30":
                feed[hm] = {k: f.get(k) for k in KEYS}
    return feed


def build_day(day: str) -> dict:
    """feed per minute, 1-minute candles from the recorded spot, option prices per minute from the structure tape."""
    feed = _feed_minutes(day)
    if not feed:
        sys.exit(f"no recordings for {day} in {REC} (run scripts/pull_recordings.sh?)")
    bars: dict[str, dict] = {}
    for path in sorted(glob.glob(f"{REC}/{day}_*.jsonl*")):
        for o in _lines(path):
            f, hm = o.get("feed") or {}, str(o.get("ts", ""))[11:16]
            sp = f.get("spot")
            if sp and "09:15" <= hm <= "15:30":
                b = bars.setdefault(hm, {"t": f"{day} {hm}", "o": sp, "h": sp, "l": sp, "c": sp})
                b["h"], b["l"], b["c"] = max(b["h"], sp), min(b["l"], sp), sp
    tape = glob.glob(f"{REC}/structure-{day}.jsonl*")
    if not tape:
        sys.exit(f"no structure tape for {day}: option prices unknown")
    px: dict[str, dict] = {}
    for o in _lines(tape[0]):
        hm = o.get("hm") or str(o.get("ts", ""))[11:16]
        row = {}
        for src in [o, *(o.get("wings") or {}).values()]:
            for leg in ("ce", "pe"):
                if src.get(f"{leg}_symbol") and src.get(leg):
                    row[src[f"{leg}_symbol"]] = src[leg]
        if row:
            px[hm] = row
    prev_day = None
    days = sorted({Path(p).name[:10] for p in glob.glob(f"{REC}/20*_*.jsonl*")}, reverse=True)
    for d in (d for d in days if d < day):
        spots = [float(v["spot"]) for m, v in sorted(_feed_minutes(d).items()) if v.get("spot") and m <= "15:29"]
        if len(spots) >= MIN_FULL_DAY:
            prev_day = {"date": d, "open": spots[0], "high": max(spots), "low": min(spots), "close": spots[-1]}
            break
    return {"feed": feed, "bars": [bars[m] for m in sorted(bars)], "px": px, "prev_day": prev_day}


def interp(row: dict, sym: str, spot: float | None) -> float | None:
    """Exact if recorded; else linear in strike between neighbours; else intrinsic + the nearest strike's time value."""
    if row.get(sym):
        return row[sym]
    prefix, k, opt = sym[:-7], int(sym[-7:-2]), sym[-2:]
    pts = {int(s[len(prefix):-2]): v for s, v in row.items() if s.startswith(prefix) and s.endswith(opt) and v}
    if not pts or spot is None:
        return None
    lo, hi = [K for K in pts if K < k], [K for K in pts if K > k]
    if lo and hi:
        a, b = max(lo), min(hi)
        w = (k - a) / (b - a)
        return pts[a] * (1 - w) + pts[b] * w
    edge = max(lo) if lo else min(hi)
    intr = (lambda K: max(0.0, spot - K)) if opt == "CE" else (lambda K: max(0.0, K - spot))
    return max(0.05, intr(k) + max(0.0, pts[edge] - intr(edge)))


class TapeQuotes:
    """Quote source for the book: the latest recorded price at or before the replay minute (up to 5 minutes back)."""

    def __init__(self, px: dict, feed: dict) -> None:
        self.px, self.feed, self.hm = px, feed, "09:15"

    def get(self, sym: str):
        for m in sorted(x for x in self.px if x <= self.hm)[-5:][::-1]:
            v = interp(self.px[m], sym, float(self.feed.get(m, {}).get("spot") or 0) or None)
            if v:
                return {"last_price": v}
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("day", help="YYYY-MM-DD")
    ap.add_argument("--model", help="OpenRouter model for this replay only (default: the live model in llm_buyer.py)")
    ap.add_argument("--rebuild", action="store_true", help="rebuild the cached day file from the recordings")
    args = ap.parse_args()
    if args.model:
        llm_buyer.MODEL_OPENROUTER = args.model  # read at call time by LlmDecider.targets()
    OUT.mkdir(parents=True, exist_ok=True)
    cache = OUT / f"day_{args.day}.pkl"
    if args.rebuild or not cache.exists():
        with cache.open("wb") as fh:
            pickle.dump(build_day(args.day), fh)
    with cache.open("rb") as fh:
        D = pickle.load(fh)
    feed, bars = D["feed"], D["bars"]
    print(f"{args.day}: feed {len(feed)} min | bars {len(bars)} | px {len(D['px'])} min | prev day {D['prev_day']}")

    q = TapeQuotes(D["px"], feed)
    log = OUT / f"replay_{args.day}_{llm_buyer.MODEL_OPENROUTER.replace('/', '_')}_{datetime.now():%H%M%S}.jsonl"
    b = LlmBuyer(path=Path(tempfile.mkdtemp()) / "r.jsonl", decider=LlmDecider(read_llm_credentials()))
    acts, events, errs, lat = Counter(), [], 0, []
    with log.open("w") as out_fh:
        for hm in [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(60)]:
            if not ("09:15" <= hm <= "15:10") or hm not in feed:
                continue
            # feed[hm] is the minute's last snapshot, so candle hm is complete and matches its spot (as the 9 Nov
            # Sonnet replay); live decides ~a minute earlier in the tape.
            f = {**feed[hm], "bars_today": [x for x in bars if x["t"][11:16] <= hm]}
            if D["prev_day"]:
                f["prev_day"] = D["prev_day"]
            now = datetime.fromisoformat(f"{args.day}T{hm}:00+05:30")
            q.hm = hm
            ev = b.on_frame(now=now, feed=f, book=q)
            if ev and ev.get("event") == "close":
                events.append((hm, ev))
            if not b.decision_due(now):
                continue
            t0 = time.time()
            out = b.decider.decide(b.state(now, q))
            lat.append(time.time() - t0)
            out["hm"] = hm
            b.decided.add(hm)
            acts[out.get("action") or "ERROR"] += 1
            errs += 1 if out.get("error") else 0
            ev = b.apply(now, out, q)
            out_fh.write(json.dumps(out, default=str) + "\n")
            if out.get("action") not in ("WAIT", "HOLD"):
                why = (out.get("reason") or out.get("error") or "")[:150]
                print(f"  {hm} {out.get('action') or 'ERROR':8} NIFTY {f.get('spot')} | {why}")
            if ev and ev.get("event") in ("open", "close"):
                events.append((hm, ev))
    print("\ntrades:")
    for hm, e in events:
        if e["event"] == "open":
            print(f"  {hm} BUY  {e['leg'].upper()} {e['symbol'].split(':')[-1]} @ {e['fill']}")
        else:
            print(f"  {hm} SELL {e['leg'].upper()} @ {e['exit']}  P&L ₹{e['pnl']:,.0f}  ({e['reason']})")
    print(f"\nMODEL {llm_buyer.MODEL_OPENROUTER} | DAY RESULT ₹{b.day_realized:,.0f} | entries {b.entries} | "
          f"stopped: {b.stopped or '-'}")
    if lat:
        print(f"answers {dict(acts)} | errors {errs} | latency avg {sum(lat)/len(lat):.1f}s max {max(lat):.1f}s")
    print(f"decisions: {log}")


if __name__ == "__main__":
    main()
