"""Usage: python3 scripts/nse_bse_gap_watch.py [--universe data/arb_universe.json] [--out data/arb] [--start 09:15] [--end 15:30]

Watch NSE vs BSE best bid/ask for liquid stocks and record every moment one exchange's
bid is above the other's ask. Read-only: never places an order.

Opens its own Kite websocket in full mode (best bid/ask with quantities; the engine's
ticker uses quote mode for stocks, which has no depth). Kite allows 3 websocket
connections per key; the engine uses one. Credentials are re-read from the engine's file
on every reconnect, so a token refreshed after start is picked up.

A crossing counts only when both quotes are under 1 s old. For each crossing it records the
gap, the shares available at both best prices, and whether buying on one exchange and
selling on the other would clear Zerodha intraday costs (brokerage, STT, exchange and SEBI
fees, GST, stamp duty). Episodes (same stock and direction, back to back) get a duration.

--universe is {"large": [...], "mid": [...], "small": [...]} NSE symbols (ranked by NSE traded
value); the summary is reported per group. Without the file, STOCKS below is watched as "large".

Writes <out>/gaps-<day>.jsonl (one line per crossing episode) and <out>/summary-<day>.json,
and prints the summary at --end.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics as st
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.config import load_settings  # noqa: E402
from atlas_lite.kite_ws import KITE_WS_URL, WS_MODE_FULL, parse_binary_ticks  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
STOCKS = (
    "RELIANCE HDFCBANK ICICIBANK INFY TCS ITC LT SBIN BHARTIARTL KOTAKBANK AXISBANK HINDUNILVR "
    "BAJFINANCE MARUTI SUNPHARMA M&M NTPC POWERGRID ULTRACEMCO TITAN ASIANPAINT HCLTECH WIPRO ONGC "
    "ADANIENT JSWSTEEL TATASTEEL COALINDIA BAJAJFINSV TECHM ETERNAL TRENT"
).split()
FRESH_S = 1.0


def intraday_cost(buy_px: float, sell_px: float, qty: int, buy_exch: str) -> float:
    """Zerodha equity intraday round trip, rupees (rates as of 2026)."""
    buy_val, sell_val = buy_px * qty, sell_px * qty
    brokerage = min(20.0, 0.0003 * buy_val) + min(20.0, 0.0003 * sell_val)
    stt = 0.00025 * sell_val
    txn_rate = {"NSE": 0.0000297, "BSE": 0.0000375}
    sell_exch = "BSE" if buy_exch == "NSE" else "NSE"
    txn = txn_rate[buy_exch] * buy_val + txn_rate[sell_exch] * sell_val
    sebi = 0.000001 * (buy_val + sell_val)
    gst = 0.18 * (brokerage + txn + sebi)
    stamp = 0.00003 * buy_val
    return brokerage + stt + txn + sebi + gst + stamp


def load_universe(path: Path) -> dict[str, str]:
    """stock -> group."""
    if path.is_file():
        groups = json.loads(path.read_text(encoding="utf-8"))
        return {s: g for g, names in groups.items() for s in names}
    return {s: "large" for s in STOCKS}


def load_tokens(universe: dict[str, str]) -> dict[int, tuple[str, str]]:
    """instrument_token -> (exchange, stock) for universe stocks listed on both exchanges."""
    found: dict[str, dict[str, int]] = {"NSE": {}, "BSE": {}}
    for exch, name in (("NSE", "nse_instruments.csv"), ("BSE", "bse_instruments.csv")):
        with (ROOT / "data" / name).open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row["exchange"] == exch and row["instrument_type"] == "EQ" and row["tradingsymbol"] in universe:
                    found[exch][row["tradingsymbol"]] = int(row["instrument_token"])
    both = [s for s in universe if s in found["NSE"] and s in found["BSE"]]
    return {**{found["NSE"][s]: ("NSE", s) for s in both}, **{found["BSE"][s]: ("BSE", s) for s in both}}


class Watch:
    def __init__(self, tokens: dict[int, tuple[str, str]], out: Path, day: str, groups: dict[str, str] | None = None) -> None:
        self.tokens = tokens
        self.groups = groups or {}
        self.quotes: dict[tuple[str, str], dict] = {}
        self.open_eps: dict[tuple[str, str], dict] = {}
        self.episodes: list[dict] = []
        self.mid_gaps: dict[str, list[float]] = {}
        self.spreads: dict[str, dict[str, list[float]]] = {}
        self.ticks = 0
        self.path = out / f"gaps-{day}.jsonl"
        self.summary_path = out / f"summary-{day}.json"
        self._last_sample: dict[str, float] = {}

    def on_tick(self, tick: dict) -> None:
        key = self.tokens.get(int(tick.get("instrument_token") or 0))
        if key is None:
            return
        exch, stock = key
        depth = tick.get("depth") or {}
        buy, sell = (depth.get("buy") or [{}])[0], (depth.get("sell") or [{}])[0]
        self.quotes[key] = {"bid": tick.get("bid"), "ask": tick.get("ask"), "bid_qty": buy.get("quantity") or 0,
                            "ask_qty": sell.get("quantity") or 0, "at": time.time()}
        self.ticks += 1
        self.check(stock)

    def check(self, stock: str) -> None:
        n, b = self.quotes.get(("NSE", stock)), self.quotes.get(("BSE", stock))
        now = time.time()
        if not n or not b or now - n["at"] > FRESH_S or now - b["at"] > FRESH_S:
            return
        if not all((n["bid"], n["ask"], b["bid"], b["ask"])):
            return
        mid = (n["bid"] + n["ask"] + b["bid"] + b["ask"]) / 4
        if now - self._last_sample.get(stock, 0) >= 1.0:  # once a second per stock
            self._last_sample[stock] = now
            g = self.groups.get(stock, "large")
            self.mid_gaps.setdefault(g, []).append(((n["bid"] + n["ask"]) - (b["bid"] + b["ask"])) / 2 / mid)
            sp = self.spreads.setdefault(g, {"NSE": [], "BSE": []})
            sp["NSE"].append((n["ask"] - n["bid"]) / mid)
            sp["BSE"].append((b["ask"] - b["bid"]) / mid)
        for direction, sell_q, buy_q, buy_exch in (("buy BSE sell NSE", n, b, "BSE"), ("buy NSE sell BSE", b, n, "NSE")):
            k = (stock, direction)
            gap = sell_q["bid"] - buy_q["ask"]
            if gap > 0:
                qty = int(min(sell_q["bid_qty"], buy_q["ask_qty"]))
                net = gap * qty - intraday_cost(buy_q["ask"], sell_q["bid"], qty, buy_exch) if qty > 0 else 0.0
                ep = self.open_eps.get(k)
                if ep is None:
                    ep = self.open_eps[k] = {"stock": stock, "group": self.groups.get(stock, "large"),
                                             "direction": direction, "start": now, "ticks": 0,
                                             "max_gap_pct": 0.0, "max_qty": 0, "best_net": float("-inf")}
                ep["ticks"] += 1
                ep["end"] = now
                ep["max_gap_pct"] = max(ep["max_gap_pct"], gap / mid * 100)
                ep["max_qty"] = max(ep["max_qty"], qty)
                ep["best_net"] = max(ep["best_net"], net)
            elif k in self.open_eps:
                self.close(k)

    def close(self, k: tuple[str, str]) -> None:
        ep = self.open_eps.pop(k)
        ep["duration_s"] = round(ep["end"] - ep["start"], 3)
        ep["start"] = datetime.fromtimestamp(ep["start"], IST).isoformat(timespec="milliseconds")
        ep["end"] = datetime.fromtimestamp(ep["end"], IST).isoformat(timespec="milliseconds")
        ep["max_gap_pct"] = round(ep["max_gap_pct"], 4)
        ep["best_net"] = round(ep["best_net"], 2)
        self.episodes.append(ep)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ep) + "\n")

    def summary(self) -> dict:
        for k in list(self.open_eps):
            self.close(k)

        def pct(values: list[float], q: float) -> float | None:
            return round(sorted(values)[int(q * (len(values) - 1))] * 100, 4) if values else None

        def block(eps: list[dict], group: str | None) -> dict:
            profitable = [e for e in eps if e["best_net"] > 0]
            lasting = [e for e in profitable if e["duration_s"] >= 0.5]
            by_stock: dict[str, int] = {}
            for e in profitable:
                by_stock[e["stock"]] = by_stock.get(e["stock"], 0) + 1
            gaps = self.mid_gaps.get(group, []) if group else [x for v in self.mid_gaps.values() for x in v]
            spreads = self.spreads.get(group, {"NSE": [], "BSE": []}) if group else {
                ex: [x for v in self.spreads.values() for x in v[ex]] for ex in ("NSE", "BSE")}
            return {
                "stocks": len({s for _, s in self.tokens.values() if group in (None, self.groups.get(s, "large"))}),
                "crossings": len(eps),
                "crossings_profitable_after_costs": len(profitable),
                "profitable_lasting_0_5s_or_more": len(lasting),
                "sum_best_net_if_every_profitable_one_were_caught": round(sum(e["best_net"] for e in profitable), 2),
                "sum_best_net_lasting_0_5s_or_more": round(sum(e["best_net"] for e in lasting), 2),
                "median_crossing_duration_s": round(st.median([e["duration_s"] for e in eps]), 3) if eps else None,
                "median_crossing_gap_pct": round(st.median([e["max_gap_pct"] for e in eps]), 4) if eps else None,
                "mid_gap_pct_p50_abs": pct([abs(x) for x in gaps], 0.5),
                "mid_gap_pct_p99_abs": pct([abs(x) for x in gaps], 0.99),
                "nse_spread_pct_median": pct(spreads["NSE"], 0.5),
                "bse_spread_pct_median": pct(spreads["BSE"], 0.5),
                "top_profitable_stocks": dict(sorted(by_stock.items(), key=lambda kv: -kv[1])[:10]),
            }

        out = {"ticks": self.ticks, "all": block(self.episodes, None)}
        for g in sorted({*self.groups.values()} or {"large"}):
            out[g] = block([e for e in self.episodes if e.get("group") == g], g)
        return out


def hm_now() -> str:
    return datetime.now(IST).strftime("%H:%M")


async def session(watch: Watch, end: str) -> None:
    import websockets

    settings = load_settings()  # re-read each connect: picks up a token refreshed after start
    url = f"{KITE_WS_URL}?{urlencode({'api_key': settings.api_key, 'access_token': settings.access_token})}"
    tokens = sorted(watch.tokens)
    async with websockets.connect(url, ping_interval=30, ping_timeout=60, max_size=2**22) as ws:
        await ws.send(json.dumps({"a": "subscribe", "v": tokens}))
        await ws.send(json.dumps({"a": "mode", "v": [WS_MODE_FULL, tokens]}))
        print(f"{datetime.now(IST):%H:%M:%S} connected, {len(tokens)} instruments in full mode", flush=True)
        while hm_now() < end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            if isinstance(msg, bytes):
                for tick in parse_binary_ticks(msg):
                    watch.on_tick(tick)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=str(ROOT / "data" / "arb_universe.json"))
    ap.add_argument("--out", default=str(ROOT / "data" / "arb"))
    ap.add_argument("--start", default="09:15")
    ap.add_argument("--end", default="15:30")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    while datetime.now(IST).weekday() >= 5 or hm_now() < args.start or hm_now() >= args.end:
        await asyncio.sleep(20)
    day = datetime.now(IST).strftime("%Y-%m-%d")
    universe = load_universe(Path(args.universe))
    watch = Watch(load_tokens(universe), out, day, universe)
    print(f"{day}: watching {len(watch.tokens) // 2} stocks on NSE and BSE until {args.end}", flush=True)
    backoff = 5.0
    while hm_now() < args.end:
        try:
            await session(watch, args.end)
        except Exception as exc:  # noqa: BLE001 - reconnect (token refresh, network)
            print(f"{datetime.now(IST):%H:%M:%S} websocket: {exc}; retrying in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    report = watch.summary()
    watch.summary_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
