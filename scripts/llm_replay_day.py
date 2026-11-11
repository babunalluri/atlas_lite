"""Replay one recorded day through the CURRENT atlas_lite/llm_buyer.py (prompt, rules, risk as in the code), one
decision a minute, 09:16 to the book's last decision (15:07), with the timing and fills a live day would have.

What the model sees at decision minute D (as live does at D:00):
  - the dashboard feed from the end of minute D-1 (the last snapshot before D:00);
  - today's CLOSED 1-minute NIFTY candles (< D), built from the recorded spot (no volume, so no VWAP, same as live);
  - the previous session's open/high/low/close (previous trading day recorded with >= 300 minutes, not flat).
How its orders fill:
  - at D:00 + the LLM's own answer time + ORDER_LATENCY_S, buys at the recorded best ASK, sells at the best BID
    (depth tape: ATM +-1 strike, several quotes a second);
  - a strike missing from the depth tape falls back to the 1-minute structure tape stamped at or after that moment
    (ATM, +-50/100/250, interpolated across strikes), last +-1 pt; the report counts these fallbacks.
The 9 Nov 2026 replays before this fix gave the model the minute-end snapshot but filled at the minute-start price:
a one-minute look-ahead that turned real losses into profits (Opus +Rs 1,770 vs -Rs 2,835 at the real ask/bid).

Modes:
  .venv/bin/python scripts/llm_replay_day.py 2026-11-09                         # real LLM calls (~$0.80/day Sonnet)
  .venv/bin/python scripts/llm_replay_day.py 2026-11-09 --model anthropic/claude-opus-5.5
  .venv/bin/python scripts/llm_replay_day.py 2026-11-09 --reprice data/replays/replay_....jsonl   # free: same
      decisions, refilled with this timing (a log from the old look-ahead replay: its minute-hm decisions saw the
      minute-end snapshot, so they fill at hm+1:00 + 5 s)
Built days are cached in data/replays/ (day_<date>.pkl, depth_<date>.pkl).
"""

from __future__ import annotations

import argparse
import bisect
import glob
import gzip
import json
import pickle
import sys
import tempfile
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite import llm_buyer  # noqa: E402
from atlas_lite.agent_gates import _is_nse_trading_day  # noqa: E402
from atlas_lite.config import read_llm_credentials  # noqa: E402
from atlas_lite.llm_buyer import LlmBuyer, LlmDecider  # noqa: E402

REC = ROOT / "data" / "recordings"
OUT = ROOT / "data" / "replays"
KEYS = ("index_nifty_chg", "index_sensex_chg", "vix", "vix_chg", "pcr", "ivp", "ce", "pe", "ce_symbol", "pe_symbol",
        "atm", "iv", "iv_day_high", "iv_day_low", "oi_vs_day_high", "adx", "spot", "expiry")
MIN_FULL_DAY = 300
ORDER_LATENCY_S = 1.0  # order send -> exchange, after the LLM's answer
DEPTH_MAX_AGE_S = 20.0  # a depth quote further than this after the fill moment is not used
OLD_LOG_FILL_DELAY_S = 65.0  # --reprice of a look-ahead log: decision at hm saw hm:59, so fill at hm+1:00 + 5 s


def _lines(path: str):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                pass


def _day_files(pattern: str) -> list[str]:
    """Recording files for a pattern; when a file exists both plain and compressed, the .gz (the finished copy: a
    leftover plain file can stop early, e.g. 2026-11-09_15-00.jsonl ends at 15:23, its .gz at 15:29)."""
    paths = sorted(glob.glob(pattern))
    gz = {p for p in paths if p.endswith(".gz")}
    return [p for p in paths if p.endswith(".gz") or p + ".gz" not in gz]


def _feed_minutes(day: str) -> dict[str, dict]:
    feed: dict[str, dict] = {}
    for path in _day_files(f"{REC}/{day}_*.jsonl*"):
        for o in _lines(path):
            f, hm = o.get("feed"), str(o.get("ts", ""))[11:16]
            if f and "09:14" <= hm <= "15:30":
                feed[hm] = {k: f.get(k) for k in KEYS}
    return feed


def build_day(day: str) -> dict:
    """feed per minute (last snapshot in the minute), 1-minute candles from the recorded spot, the structure tape."""
    feed = _feed_minutes(day)
    if not feed:
        sys.exit(f"no recordings for {day} in {REC} (run scripts/pull_recordings.sh?)")
    bars: dict[str, dict] = {}
    for path in _day_files(f"{REC}/{day}_*.jsonl*"):
        for o in _lines(path):
            f, hm = o.get("feed") or {}, str(o.get("ts", ""))[11:16]
            sp = f.get("spot")
            if sp and "09:15" <= hm <= "15:30":
                b = bars.setdefault(hm, {"t": f"{day} {hm}", "o": sp, "h": sp, "l": sp, "c": sp})
                b["h"], b["l"], b["c"] = max(b["h"], sp), min(b["l"], sp), sp
    tape = _day_files(f"{REC}/structure-{day}.jsonl*")
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
        spots = [float(v["spot"]) for m, v in sorted(_feed_minutes(d).items())
                 if v.get("spot") and "09:15" <= m <= "15:29"]
        if len(spots) >= MIN_FULL_DAY and max(spots) > min(spots) and _is_nse_trading_day(date.fromisoformat(d)):
            prev_day = {"date": d, "open": spots[0], "high": max(spots), "low": min(spots), "close": spots[-1]}
            break
    return {"feed": feed, "bars": [bars[m] for m in sorted(bars)], "px": px, "prev_day": prev_day}


def build_depth(day: str) -> dict[str, list[tuple[float, float, float]]]:
    """Best bid/ask per option symbol from the depth tape: {symbol: [(seconds since midnight, bid, ask), ...]}."""
    out: dict[str, list[tuple[float, float, float]]] = {}
    for path in _day_files(f"{REC}/depth-{day}.jsonl*"):
        for o in _lines(path):
            bid, ask, sym, ts = o.get("bid"), o.get("ask"), o.get("symbol"), str(o.get("ts", ""))
            if bid and ask and sym and o.get("index", "nifty") == "nifty" and len(ts) >= 19:
                sec = int(ts[11:13]) * 3600 + int(ts[14:16]) * 60 + float(ts[17:19] + (ts[19:23] if ts[19:20] == "." else ""))
                out.setdefault(sym, []).append((sec, float(bid), float(ask)))
    for v in out.values():
        v.sort()
    return out


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


class ReplayQuotes:
    """Quote source for the book at moment `at` (seconds since midnight): the first depth quote at or after it (within
    DEPTH_MAX_AGE_S) with bid/ask; else the structure tape minute stamped at or after it (last price only)."""

    def __init__(self, px: dict, feed: dict, depth: dict) -> None:
        self.px, self.feed, self.depth, self.at = px, feed, depth, 9 * 3600 + 15 * 60
        self.used = Counter()

    def get(self, sym: str):
        q = self.depth.get(sym)
        if q:
            i = bisect.bisect_left(q, (self.at,))
            if i < len(q) and q[i][0] - self.at <= DEPTH_MAX_AGE_S:
                self.used["depth"] += 1
                return {"bid": q[i][1], "ask": q[i][2], "last_price": (q[i][1] + q[i][2]) / 2}
        start = int(self.at // 60)
        for m in range(start if self.at % 60 == 0 else start + 1, start + 6):
            hm = f"{m // 60:02d}:{m % 60:02d}"
            if hm in self.px:
                v = interp(self.px[hm], sym, float(self.feed.get(hm, {}).get("spot") or 0) or None)
                if v:
                    self.used["tape"] += 1
                    return {"last_price": v}
        return None


def _minute(hm: str, delta: int) -> str:
    t = datetime.strptime(hm, "%H:%M") + timedelta(minutes=delta)
    return t.strftime("%H:%M")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("day", help="YYYY-MM-DD")
    ap.add_argument("--model", help="OpenRouter model for this replay only (default: the live model in llm_buyer.py)")
    ap.add_argument("--prompt", choices=sorted(llm_buyer.PROMPTS), help="prompt version for this replay only (default: live)")
    ap.add_argument("--reprice", help="decision log (jsonl) to refill without LLM calls")
    ap.add_argument("--rebuild", action="store_true", help="rebuild the cached day files from the recordings")
    args = ap.parse_args()
    if args.model:
        llm_buyer.MODEL_OPENROUTER = args.model  # read at call time by LlmDecider.targets()
    OUT.mkdir(parents=True, exist_ok=True)
    cache, dcache = OUT / f"day_{args.day}.pkl", OUT / f"depth_{args.day}.pkl"
    if args.rebuild or not cache.exists():
        with cache.open("wb") as fh:
            pickle.dump(build_day(args.day), fh)
    if args.rebuild or not dcache.exists():
        with dcache.open("wb") as fh:
            pickle.dump(build_depth(args.day), fh)
    with cache.open("rb") as fh:
        D = pickle.load(fh)
    with dcache.open("rb") as fh:
        depth = pickle.load(fh)
    feed, bars = D["feed"], D["bars"]
    print(f"{args.day}: feed {len(feed)} min | bars {len(bars)} | tape {len(D['px'])} min | depth symbols {len(depth)}"
          f" | prev day {D['prev_day']}")

    old = None
    if args.reprice:  # {decision minute D: logged answer}; a look-ahead log's hm decision is placed at hm+1
        old = {}
        for line in open(args.reprice):
            o = json.loads(line)
            old[_minute(o["hm"], 1)] = o
    q = ReplayQuotes(D["px"], feed, depth)
    tag = "reprice" if old else llm_buyer.MODEL_OPENROUTER.replace("/", "_") + f"_{args.prompt or llm_buyer.PROMPT_VERSION}"
    log = OUT / f"replay_{args.day}_{tag}_{datetime.now():%H%M%S}.jsonl"
    prompt = llm_buyer.render_prompt(args.prompt) if args.prompt else ""
    b = LlmBuyer(path=Path(tempfile.mkdtemp()) / "r.jsonl", decider=LlmDecider(read_llm_credentials(), prompt=prompt))
    acts, events, errs, lat = Counter(), [], 0, []
    with log.open("w") as out_fh:
        for hm in [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(60)]:
            prev = _minute(hm, -1)
            if not ("09:15" <= hm <= llm_buyer.LIVE_ORDERS_UNTIL) or prev not in feed:
                continue
            f = {**feed[prev], "bars_today": [x for x in bars if x["t"][11:16] < hm]}  # live: closed candles only
            if D["prev_day"]:
                f["prev_day"] = D["prev_day"]
            now = datetime.fromisoformat(f"{args.day}T{hm}:00+05:30")
            q.at = now.hour * 3600 + now.minute * 60
            ev = b.on_frame(now=now, feed=f, book=q)
            if ev and ev.get("event") == "close":
                events.append((hm, ev))
            if old is not None:
                if hm not in old or not (b.decision_due(now) or b.position is not None):
                    continue
                out = {k: v for k, v in old[hm].items() if k in ("action", "reason", "error", "provider", "model")}
                q.at += OLD_LOG_FILL_DELAY_S - 60  # the old decision saw hm:59 of the minute before D
                took = 0.0
            else:
                if not b.decision_due(now):
                    continue
                t0 = time.time()
                out = b.decider.decide(b.state(now, q))
                took = time.time() - t0
                lat.append(took)
                q.at += took + ORDER_LATENCY_S
            out["hm"] = hm
            b.decided.add(hm)
            acts[out.get("action") or "ERROR"] += 1
            errs += 1 if out.get("error") else 0
            ev = b.apply(now, out, q)
            out_fh.write(json.dumps({**out, "fill_at_s": round(q.at, 1)}, default=str) + "\n")
            if out.get("action") not in ("WAIT", "HOLD"):
                why = (out.get("reason") or out.get("error") or "")[:150]
                print(f"  {hm} {out.get('action') or 'ERROR':8} NIFTY {f.get('spot')} | {why}")
            if ev and ev.get("event") in ("open", "close"):
                events.append((hm, ev))
    print("\ntrades (buy at ask, sell at bid):")
    for hm, e in events:
        if e["event"] == "open":
            print(f"  {hm} BUY  {e['leg'].upper()} {e['symbol'].split(':')[-1]} @ {e['fill']}")
        else:
            print(f"  {hm} SELL {e['leg'].upper()} @ {e['exit']}  P&L ₹{e['pnl']:,.0f}  ({e['reason']})")
    print(f"\nMODEL {tag} | DAY RESULT ₹{b.day_realized:,.0f} | entries {b.entries} | stopped: {b.stopped or '-'}")
    print(f"answers {dict(acts)} | errors {errs} | quotes {dict(q.used)} (tape = no bid/ask, last +-1 pt)")
    if lat:
        print(f"latency avg {sum(lat)/len(lat):.1f}s max {max(lat):.1f}s")
    print(f"decisions: {log}")


if __name__ == "__main__":
    main()
