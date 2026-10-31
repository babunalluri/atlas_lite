"""Usage: python3 scripts/nse_bse_gap_watch.py [--universe data/arb_universe.json] [--out data/arb] [--min-net 500]
       [--research-nets 1,100,200,300,1000] [--days 5] [--until YYYY-MM-DD] [--start 09:15] [--end 15:30]

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

--universe is {"small": [...], "mid": [...], "large": [...]}, each entry {"nse": symbol, "bse": symbol,
...} (matched by ISIN; ranked by NSE traded value; BSE groups A/B/X only). The summary is reported
per group, small caps first. Without the file, STOCKS below is watched as "large".

Paper trading (no real orders), in independent tracks: the main track (--min-net, ₹500) and research
tracks (--research-nets, ₹1/100/200/300/1000; ₹1 = anything that beats costs) kept for later study.
Runs --days trading days in a row (5 = a week), skipping weekends and NSE holidays, one set of files
per day; --until is the last day. It runs as the `arb-watch` service in docker-compose.yml
(restart: unless-stopped), so Docker restarts it after crashes, reboots and deploys; a restart
reloads the day's files. When the run is over, or ATLAS_LITE_ARB_WATCH=0, it idles instead of
exiting, so Docker does not restart it in a loop. If another watcher holds the lock, it waits. A crossing whose estimated net after all
costs reaches a track's threshold at the moment it is seen fires, in that track,
two IOC limit orders, buy at the cheaper exchange's ask and sell at the dearer one's bid, for the
shares shown at both best prices (capped at ₹1 lakh). Each order "arrives" after a delay, simulated
at 100, 300 and 600 ms in parallel; it fills only if its price is still at or better than the
limit, up to the shares shown at that best price. A leg that filled more than the other is closed
at market on its exchange after another delay (there is no all-or-none order across NSE and BSE).
Full Zerodha intraday charges on every order, unwinds included. One attempt per stock per delay
at a time, 2 s cooldown (both directions share it: they can never be crossed at the same moment,
since that would need one exchange's bid above its own ask). If either leg gets no quote update
within NEXT_QUOTE_WAIT_S of arriving, the attempt is "unknown": no fills, no P&L, counted apart.

Writes <out>/gaps-<day>.jsonl (one line per crossing episode), <out>/paper-<day>.jsonl (one line
per paper attempt), <out>/live-<day>.json every 60 s (crossings still open are left out) and
<out>/summary-<day>.json at --end, which it also prints. The dashboard reads them via /api/arb.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import statistics as st
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.agent_gates import _is_nse_trading_day  # noqa: E402
from atlas_lite.config import load_settings  # noqa: E402
from atlas_lite.kite_charges import order_charges as kite_order_charges  # noqa: E402
from atlas_lite.kite_ws import KITE_WS_URL, WS_MODE_FULL, parse_binary_ticks  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
STOCKS = (
    "RELIANCE HDFCBANK ICICIBANK INFY TCS ITC LT SBIN BHARTIARTL KOTAKBANK AXISBANK HINDUNILVR "
    "BAJFINANCE MARUTI SUNPHARMA M&M NTPC POWERGRID ULTRACEMCO TITAN ASIANPAINT HCLTECH WIPRO ONGC "
    "ADANIENT JSWSTEEL TATASTEEL COALINDIA BAJAJFINSV TECHM ETERNAL TRENT"
).split()
FRESH_S = 1.0
LATENCIES_MS = (100, 300, 600)
# A leg's fill uses the first quote update at or after its arrival; none within this long counts as stale.
NEXT_QUOTE_WAIT_S = 2.0
MAX_NOTIONAL = 100_000.0
PAPER_COOLDOWN_S = 2.0
MIN_PAPER_NET = 500.0  # main track: estimated net after costs a crossing needs (--min-net)
RESEARCH_NETS: tuple[float, ...] = (1.0, 100.0, 200.0, 300.0, 1000.0)  # research tracks (--research-nets)


def paper_tracks() -> tuple[float, ...]:
    """Thresholds, main first. Each track has its own locks, so research trades never block main ones."""
    return tuple(dict.fromkeys((MIN_PAPER_NET, *sorted(RESEARCH_NETS))))
TXN_RATE = {"NSE": 0.0000297, "BSE": 0.0000375}


def order_charges(fills: list[tuple[str, str, float, int]]) -> float:
    """Zerodha equity intraday charges for executed orders (exchange, buy|sell, price, qty).

    atlas_lite.kite_charges.order_charges does the rounding and GST base; equity intraday rates:
    brokerage 0.03% capped at ₹20, STT 0.025% on sells, exchange fees per exchange.
    """
    total = 0.0
    for exch, side, px, qty in fills:
        value = px * qty
        total += kite_order_charges(value, side=side, brokerage=round(min(20.0, 0.0003 * value), 2),
                                    txn_rate=TXN_RATE[exch], stt_sell_rate=0.00025)["total"]
    return total


def intraday_cost(buy_px: float, sell_px: float, qty: int, buy_exch: str) -> float:
    """Zerodha equity intraday round trip, rupees: the same charges the paper fills pay."""
    sell_exch = "BSE" if buy_exch == "NSE" else "NSE"
    return order_charges([(buy_exch, "buy", buy_px, qty), (sell_exch, "sell", sell_px, qty)])


GROUP_ORDER = ("small", "mid", "large")


def load_universe(path: Path) -> dict[str, tuple[str, str]]:
    """NSE symbol -> (group, BSE symbol)."""
    if path.is_file():
        groups = json.loads(path.read_text(encoding="utf-8"))
        return {e["nse"]: (g, e["bse"]) for g, entries in groups.items() for e in entries}
    print(f"WARNING: {path} not found: watching only {len(STOCKS)} built-in large caps, no small or mid caps",
          flush=True)
    return {s: ("large", s) for s in STOCKS}


def load_tokens(universe: dict[str, tuple[str, str]]) -> dict[int, tuple[str, str]]:
    """instrument_token -> (exchange, NSE symbol) for universe stocks found on both exchanges."""
    want = {"NSE": set(universe), "BSE": {bse for _, bse in universe.values()}}
    found: dict[str, dict[str, int]] = {"NSE": {}, "BSE": {}}
    for exch, name in (("NSE", "nse_instruments.csv"), ("BSE", "bse_instruments.csv")):
        with (ROOT / "data" / name).open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row["exchange"] == exch and row["instrument_type"] == "EQ" and row["tradingsymbol"] in want[exch]:
                    found[exch][row["tradingsymbol"]] = int(row["instrument_token"])
    both = [s for s, (_, bse) in universe.items() if s in found["NSE"] and bse in found["BSE"]]
    return {**{found["NSE"][s]: ("NSE", s) for s in both}, **{found["BSE"][universe[s][1]]: ("BSE", s) for s in both}}


def unwind_price(q: dict | None, side: str, fresh: bool, ref_px: float) -> float:
    """Market order closing a leg left over. Fresh quote: its bid (sell) or ask (buy). No fresh quote:
    that price moved one spread against us. No price at all: 0.5% against the leg's own fill."""
    q = q or {}
    bid, ask = q.get("bid"), q.get("ask")
    if side == "sell":
        if not bid:
            return round(ref_px * 0.995, 2)
        return bid if fresh else round(max(0.05, bid - ((ask - bid) if ask else 0.0)), 2)
    if not ask:
        return round(ref_px * 1.005, 2)
    return ask if fresh else round(ask + ((ask - bid) if bid else 0.0), 2)


def tradable_qty(buy_q: dict, sell_q: dict) -> int:
    """Shares at both best prices, capped at MAX_NOTIONAL: the same size for crossing stats and paper."""
    if not buy_q.get("ask") or not sell_q.get("bid"):
        return 0
    return int(min(sell_q["bid_qty"], buy_q["ask_qty"], MAX_NOTIONAL // buy_q["ask"]))


class Watch:
    def __init__(self, tokens: dict[int, tuple[str, str]], out: Path, day: str,
                 universe: dict[str, tuple[str, str]] | None = None) -> None:
        self.tokens = tokens
        self.groups = {s: g for s, (g, _) in (universe or {}).items()}
        self.quotes: dict[tuple[str, str], dict] = {}
        self.open_eps: dict[tuple[str, str], dict] = {}
        self.episodes: list[dict] = []
        self.mid_gaps: dict[str, list[float]] = {}
        self.spreads: dict[str, dict[str, list[float]]] = {}
        self.ticks = 0
        self.samples_since = datetime.now(IST).isoformat(timespec="seconds")
        self.path = out / f"gaps-{day}.jsonl"
        self.summary_path = out / f"summary-{day}.json"
        self._last_sample: dict[str, float] = {}
        self.paper: list[dict] = []
        self.paper_path = out / f"paper-{day}.jsonl"
        self._paper_last: dict[tuple[str, int, float], float] = {}
        self._paper_busy: set[tuple[str, int, float]] = set()
        self._tasks: set[asyncio.Task] = set()
        self.paper_errors = 0
        self.cancelled_at_close = 0
        self.day = day
        self.live_path = out / f"live-{day}.json"
        self._live_at = 0.0
        self._reload()

    def _reload(self) -> None:
        """A restart mid-day picks up the day's crossings and paper trades already written, so the
        reports keep covering the whole day, and restores each track's cooldown from the last attempt.
        Quote samples (typical gap, spreads) restart empty: the report says when they start."""
        for path, bucket in ((self.path, self.episodes), (self.paper_path, self.paper)):
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    bucket.append(row)
        for rec in self.paper:
            try:
                at = datetime.fromisoformat(str(rec["seen"])).timestamp()
                key = (rec["stock"], int(rec["latency_ms"]), float(rec.get("track", MIN_PAPER_NET)))
            except (KeyError, TypeError, ValueError):
                continue
            self._paper_last[key] = max(self._paper_last.get(key, 0.0), at)

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
            qty = tradable_qty(buy_q, sell_q)
            if gap > 0 and qty >= 1:
                net = gap * qty - intraday_cost(buy_q["ask"], sell_q["bid"], qty, buy_exch)
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
                self.maybe_paper(stock, buy_exch, "NSE" if buy_exch == "BSE" else "BSE", buy_q, sell_q, now)
            elif k in self.open_eps:
                self.close(k)

    def maybe_paper(self, stock: str, buy_exch: str, sell_exch: str, buy_q: dict, sell_q: dict, now: float) -> None:
        qty = tradable_qty(buy_q, sell_q)
        if qty < 1:
            return
        est_net = (sell_q["bid"] - buy_q["ask"]) * qty - intraday_cost(buy_q["ask"], sell_q["bid"], qty, buy_exch)
        tracks = [t for t in paper_tracks() if est_net >= t]
        if not tracks:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for track in tracks:
            for lat in LATENCIES_MS:
                key = (stock, lat, track)
                if key in self._paper_busy or now - self._paper_last.get(key, 0.0) < PAPER_COOLDOWN_S:
                    continue
                self._paper_busy.add(key)
                self._paper_last[key] = now
                task = loop.create_task(self.paper_attempt(
                    stock, buy_exch, sell_exch, buy_q["ask"], sell_q["bid"], qty, lat, est_net, track))
                self._tasks.add(task)
                task.add_done_callback(self._paper_done)

    def _paper_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.paper_errors += 1
            print(f"{datetime.now(IST):%H:%M:%S} paper attempt failed: {task.exception()!r}", flush=True)

    async def _quote_after(self, key: tuple[str, str], t_min: float) -> tuple[dict | None, bool]:
        """First quote update at or after t_min; (last quote, False) if none comes within NEXT_QUOTE_WAIT_S."""
        deadline = t_min + NEXT_QUOTE_WAIT_S
        while True:
            q = self.quotes.get(key)
            if q is not None and q["at"] >= t_min:
                return q, True
            if time.time() >= deadline:
                return q, False
            await asyncio.sleep(0.02)

    async def paper_attempt(self, stock: str, buy_exch: str, sell_exch: str, buy_lim: float, sell_lim: float,
                            qty: int, lat: int, est_net: float = 0.0, track: float | None = None) -> None:
        track = MIN_PAPER_NET if track is None else track
        try:
            seen = time.time()
            arrive = seen + lat / 1000
            await asyncio.sleep(lat / 1000)
            # Kite streams quote snapshots, not every trade: a leg fills against the first update at or
            # after its arrival, not the quote that triggered it.
            (bq, b_fresh), (sq, s_fresh) = await asyncio.gather(
                self._quote_after((buy_exch, stock), arrive), self._quote_after((sell_exch, stock), arrive))
            base = {"stock": stock, "group": self.groups.get(stock, "large"), "latency_ms": lat,
                    "seen": datetime.fromtimestamp(seen, IST).isoformat(timespec="milliseconds"),
                    "direction": f"buy {buy_exch} sell {sell_exch}", "buy_limit": buy_lim, "sell_limit": sell_lim,
                    "qty": qty, "est_net": round(est_net, 2), "track": track}
            if bq is None or sq is None or not (b_fresh and s_fresh):
                # No price seen after the orders arrived: the fill is a guess either way, so no P&L.
                self._record({**base, "bought": 0, "sold": 0, "outcome": "unknown", "quotes_updated": False,
                              "gross": None, "unwind_pnl": None, "net": None})
                return
            bought = int(min(qty, bq["ask_qty"])) if bq["ask"] and bq["ask"] <= buy_lim else 0
            sold = int(min(qty, sq["bid_qty"])) if sq["bid"] and sq["bid"] >= sell_lim else 0
            buy_px, sell_px = bq["ask"], sq["bid"]
            fills: list[tuple[str, str, float, int]] = []
            if bought:
                fills.append((buy_exch, "buy", buy_px, bought))
            if sold:
                fills.append((sell_exch, "sell", sell_px, sold))
            matched = min(bought, sold)
            gross = matched * (sell_px - buy_px) if matched else 0.0
            unwind_pnl = 0.0
            unwind_updated = None
            if bought != sold:  # one leg filled more: close the extra at market after another delay
                t_unwind = time.time() + lat / 1000
                await asyncio.sleep(lat / 1000)
                if bought > sold:
                    q, unwind_updated = await self._quote_after((buy_exch, stock), t_unwind)
                    px = unwind_price(q, "sell", unwind_updated, buy_px)
                    extra = bought - sold
                    unwind_pnl = extra * (px - buy_px)
                    fills.append((buy_exch, "sell", px, extra))
                else:
                    q, unwind_updated = await self._quote_after((sell_exch, stock), t_unwind)
                    px = unwind_price(q, "buy", unwind_updated, sell_px)
                    extra = sold - bought
                    unwind_pnl = extra * (sell_px - px)
                    fills.append((sell_exch, "buy", px, extra))
            outcome = ("none" if not bought and not sold else "both" if bought == sold
                       else "one_leg" if not bought or not sold else "partial")
            net = gross + unwind_pnl - order_charges(fills)
            # unwind_quote_updated: False means the unwind used the last price seen (a market order hits
            # whatever is standing); None means no unwind was needed.
            self._record({**base, "bought": bought, "sold": sold, "outcome": outcome, "quotes_updated": True,
                          "unwind_quote_updated": unwind_updated,
                          "gross": round(gross, 2), "unwind_pnl": round(unwind_pnl, 2), "net": round(net, 2)})
        finally:
            self._paper_busy.discard((stock, lat, track))

    def _record(self, rec: dict) -> None:
        self.paper.append(rec)
        with self.paper_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

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

    def write_report(self, final: bool) -> dict:
        report = {"day": self.day, "final": final,
                  "updated_at": datetime.now(IST).isoformat(timespec="seconds"), **self.summary(final)}
        path = self.summary_path if final else self.live_path
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
        tmp.replace(path)
        self._live_at = time.time()
        return report

    def summary(self, final: bool = True) -> dict:
        if final:  # a live snapshot leaves crossings still open out
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
                "crossings_meeting_min_net": sum(e["best_net"] >= MIN_PAPER_NET for e in eps),
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

        def paper_block(recs: list[dict]) -> dict:
            out_b = {}
            for lat in LATENCIES_MS:
                r = [x for x in recs if x["latency_ms"] == lat]
                counts = {o: sum(x["outcome"] == o for x in r) for o in ("both", "none", "one_leg", "partial", "unknown")}
                out_b[f"{lat}ms"] = {
                    "attempts": len(r), **counts,
                    "net": round(sum(x["net"] or 0 for x in r), 2),  # unknown attempts carry no P&L
                    "net_both_filled": round(sum(x["net"] for x in r if x["outcome"] == "both"), 2),
                    "net_failed_legs": round(sum(x["net"] for x in r if x["outcome"] in ("one_leg", "partial")), 2),
                }
            return out_b

        out: dict = {"ticks": self.ticks, "quote_samples_since": self.samples_since, "paper_errors": self.paper_errors,
                     "paper_cancelled_at_close": self.cancelled_at_close, "min_net": MIN_PAPER_NET,
                     "research_nets": sorted(RESEARCH_NETS)}

        def main_track(recs: list[dict]) -> list[dict]:
            return [x for x in recs if x.get("track", MIN_PAPER_NET) == MIN_PAPER_NET]

        def research(recs: list[dict]) -> dict:
            """₹ threshold -> paper results, every research track."""
            return {f"{t:g}": paper_block([x for x in recs if x.get("track") == t]) for t in sorted(RESEARCH_NETS)}
        present = set(self.groups.values()) or {"large"}
        for g in [x for x in GROUP_ORDER if x in present]:  # small caps first
            out[g] = block([e for e in self.episodes if e.get("group") == g], g)
            in_group = [x for x in self.paper if x["group"] == g]
            out[g]["paper"] = paper_block(main_track(in_group))
            out[g]["paper_research"] = research(in_group)
        out["all"] = block(self.episodes, None)
        out["all"]["paper"] = paper_block(main_track(self.paper))
        out["all"]["paper_research"] = research(self.paper)
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
            if time.time() - watch._live_at >= 60:
                watch.write_report(final=False)


async def main() -> None:
    global MIN_PAPER_NET, RESEARCH_NETS
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=str(ROOT / "data" / "arb_universe.json"))
    ap.add_argument("--min-net", type=float, default=MIN_PAPER_NET, help="₹ estimated net, main paper track")
    ap.add_argument("--research-nets", default=",".join(f"{t:g}" for t in RESEARCH_NETS),
                    help="comma-separated ₹ thresholds for research tracks")
    ap.add_argument("--out", default=str(ROOT / "data" / "arb"))
    ap.add_argument("--days", type=int, default=5, help="trading days to record (5 = a week)")
    ap.add_argument("--until", default=None, help="YYYY-MM-DD: last day to record")
    ap.add_argument("--start", default="09:15")
    ap.add_argument("--end", default="15:30")
    args = ap.parse_args()
    MIN_PAPER_NET = args.min_net
    RESEARCH_NETS = tuple(float(x) for x in args.research_nets.split(",") if x.strip())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ATLAS_LITE_ARB_WATCH", "1").strip().lower() in ("0", "false", "no", "off"):
        print(f"{datetime.now(IST):%Y-%m-%d %H:%M}: ATLAS_LITE_ARB_WATCH is off; idling", flush=True)
        await idle()
    lock = acquire_lock(out)  # held for the process lifetime
    while lock is None:  # another watcher (a manual run) has it: wait rather than exit and be restarted
        print(f"{datetime.now(IST):%H:%M:%S} another gap watcher holds {out / LOCK_NAME}; waiting", flush=True)
        await asyncio.sleep(60)
        lock = acquire_lock(out)
    if not args.until:  # the last of --days trading sessions, so a long run never spills past it
        args.until = planned_until(datetime.now(IST), args.days, args.end).isoformat()
    print(f"{datetime.now(IST):%Y-%m-%d %H:%M}: recording up to {args.days} trading day(s), last {args.until}", flush=True)
    finalize_missing(out, Path(args.universe))
    n = 0
    while datetime.now(IST).strftime("%Y-%m-%d") <= args.until:  # --until is the end; --days only sets it
        n += 1
        day = await run_day(args, out, n)
        if day >= args.until:
            break
    print(f"{datetime.now(IST):%Y-%m-%d %H:%M}: recording finished (last day {args.until}); idling", flush=True)
    await idle()


async def idle() -> None:
    """Stay up doing nothing, so a restart: unless-stopped service does not loop. Stop the service to end it."""
    await asyncio.Event().wait()


LOCK_NAME = "watcher.lock"


def acquire_lock(out: Path):
    """Exclusive, non-blocking lock so two watchers never write the same day's files."""
    import fcntl

    fh = (out / LOCK_NAME).open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def planned_until(now: datetime, days: int, end: str):
    """Date of the days-th trading session from now (today counts if it has not ended)."""
    from datetime import timedelta

    d = now.date()
    if not (_is_nse_trading_day(d) and now.strftime("%H:%M") < end):
        d += timedelta(days=1)
    count = 0
    while True:
        if _is_nse_trading_day(d):
            count += 1
            if count >= days:
                return d
        d += timedelta(days=1)


def finalize_missing(out: Path, universe_path: Path) -> None:
    """A day whose watcher died after the close (or before writing it) gets its final report rebuilt
    from its files, marked recovered. Today counts only once its session is over."""
    today, now_hm = datetime.now(IST).strftime("%Y-%m-%d"), hm_now()
    days = {p.stem.split("-", 1)[1] for p in [*out.glob("gaps-*.jsonl"), *out.glob("paper-*.jsonl")]}
    for day in sorted(days):
        if (out / f"summary-{day}.json").is_file() or (day == today and now_hm < "15:30") or day > today:
            continue
        universe = load_universe(universe_path)
        watch = Watch(load_tokens(universe), out, day, universe)
        report = watch.write_report(final=True)
        report["recovered"] = True
        (out / f"summary-{day}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"{day}: final report rebuilt from its files (watcher stopped before writing it)", flush=True)


async def run_day(args: argparse.Namespace, out: Path, n: int) -> str:
    """Wait for the next trading session (weekdays, not NSE holidays), record it, write the final report."""
    while (not _is_nse_trading_day(datetime.now(IST).date()) or hm_now() < args.start or hm_now() >= args.end
           or (out / f"summary-{datetime.now(IST):%Y-%m-%d}.json").is_file()):  # a finished day is not redone
        if args.until and datetime.now(IST).strftime("%Y-%m-%d") > args.until:
            return datetime.now(IST).strftime("%Y-%m-%d")
        await asyncio.sleep(20)
    day = datetime.now(IST).strftime("%Y-%m-%d")
    universe = load_universe(Path(args.universe))
    watch = Watch(load_tokens(universe), out, day, universe)
    print(f"{day} (day {n}, last {args.until}): watching {len(watch.tokens) // 2} stocks on NSE and BSE until {args.end}; "
          f"paper tracks ₹{', ₹'.join(f'{t:g}' for t in paper_tracks())}", flush=True)
    backoff = 5.0
    while hm_now() < args.end:
        try:
            await session(watch, args.end)
        except Exception as exc:  # noqa: BLE001 - reconnect (token refresh, network)
            print(f"{datetime.now(IST):%H:%M:%S} websocket: {exc}; retrying in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    if watch._tasks:  # let paper attempts in flight finish: worst case 2 x (600 ms + 2 s quote wait)
        _, pending = await asyncio.wait(set(watch._tasks), timeout=15)
        for task in pending:  # anything still running is dropped, not written after the final report
            task.cancel()
        watch.cancelled_at_close = len(pending)
    report = watch.write_report(final=True)
    print(json.dumps(report, indent=2), flush=True)
    return day


if __name__ == "__main__":
    asyncio.run(main())
