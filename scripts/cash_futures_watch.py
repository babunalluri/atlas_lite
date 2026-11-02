"""Usage: python3 scripts/cash_futures_watch.py [--out data/cf] [--until YYYY-MM-DD] [--capital 250000]

Intraday cash-futures spread paper trading for NSE F&O stocks. Read-only research: never places
an order.

Watches every F&O stock's cash quote and near-month future (own Kite websocket, full mode: best
bid/ask with quantities). The spread is futures minus cash, as % of the cash price. A stock's
baseline is the mean of its per-minute mid spread over the last 60 minutes.

Entry ("short the spread"): sell the future at its bid and buy the stock at its ask, when that
executable spread is more than ENTRY_PCT above the baseline AND the estimated net per lot after
both legs' costs is at least MIN_NET. 10:15-14:45 only. Exit: buy the future at its ask and sell
the stock at its bid, when that executable spread is back at the entry baseline, or at 15:15.

Paper fills: each leg "arrives" after LATENCY_MS and fills against the first quote update after
that. Entries are IOC limit orders: they fill only if the book levels within the limit hold a full
lot (average price over those levels). Exits and the unwind of a leg that filled alone are market
orders that walk the shown book; beyond its 5 levels they assume 0.5% worse. Costs: Zerodha
equity-intraday and futures charges on every order.

Capital: one paper position at a time, and only stocks whose estimated margin for one lot (cash
MIS ~20% plus futures ~18% of the lot value) fits --capital. Every qualifying signal is logged for
research whether or not it was traded.

Design for restarts, decided up front: one append-only event log per day (cf-<day>.jsonl: signal,
open, close, unwind, skip). A start replays it; an open position is restored and managed to its
exit. live-<day>.json every 60 s and summary-<day>.json at the close are derived from the log.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from collections import deque
from datetime import date, datetime, timedelta
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
ENTRY_PCT = 0.15  # executable spread above the 60-min baseline, % of price
MIN_NET = 300.0  # estimated ₹ per lot after costs
BASELINE_MIN = 60
BASELINE_MIN_POINTS = 30
FRESH_S = 1.0
LATENCY_MS = 300
NEXT_QUOTE_WAIT_S = 2.0
ENTRY_FROM, ENTRY_UNTIL, FORCE_EXIT = "10:15", "14:45", "15:15"
MARGIN_FRACTION = 0.38  # cash MIS ~20% + futures ~18% of one lot's value
LOCK_NAME = "cf_watcher.lock"
EQ_TXN = 0.0000297  # NSE cash
FUT_TXN = 0.0000173  # NSE futures


def hm_now() -> str:
    return datetime.now(IST).strftime("%H:%M")


def leg_charges(kind: str, side: str, px: float, qty: int) -> float:
    """Zerodha charges for one executed order: kind 'cash' (equity intraday) or 'fut' (futures)."""
    value = px * qty
    brokerage = round(min(20.0, 0.0003 * value), 2)
    if kind == "cash":
        return kite_order_charges(value, side=side, brokerage=brokerage, txn_rate=EQ_TXN, stt_sell_rate=0.00025)["total"]
    return kite_order_charges(value, side=side, brokerage=brokerage, txn_rate=FUT_TXN, stt_sell_rate=0.0002,
                              stamp_buy_rate=0.00002)["total"]


def round_trip_cost(cash_px: float, fut_px: float, qty: int) -> float:
    """Both legs in and out: buy cash, sell cash, sell future, buy future."""
    return (leg_charges("cash", "buy", cash_px, qty) + leg_charges("cash", "sell", cash_px, qty)
            + leg_charges("fut", "sell", fut_px, qty) + leg_charges("fut", "buy", fut_px, qty))


def limit_fill(levels: list[tuple[float, int]], qty: int, limit: float, buying: bool) -> float | None:
    """IOC limit order for qty: average price over the book levels within the limit, if they hold qty."""
    got, cost = 0, 0.0
    for px, q in levels:
        if (buying and px > limit) or (not buying and px < limit):
            break
        take = min(q, qty - got)
        got += take
        cost += take * px
        if got >= qty:
            return round(cost / qty, 4)
    return None


def market_fill(levels: list[tuple[float, int]], qty: int, buying: bool, ref: float) -> float:
    """Market order for qty: walks the shown book; anything beyond the 5 shown levels fills 0.5% past
    the last of them (or past ref, with no book at all)."""
    got, cost, last = 0, 0.0, None
    for px, q in levels:
        take = min(q, qty - got)
        got += take
        cost += take * px
        last = px
        if got >= qty:
            return round(cost / qty, 4)
    worse = (last if last is not None else ref) * (1.005 if buying else 0.995)
    return round((cost + (qty - got) * worse) / qty, 4)


def load_universe(data_dir: Path, today: date) -> dict[str, dict]:
    """stock -> {cash_token, fut_token, fut_symbol, expiry, lot}: the nearest future at least 3 days out."""
    cash: dict[str, int] = {}
    with (data_dir / "nse_instruments.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["exchange"] == "NSE" and r["instrument_type"] == "EQ":
                cash[r["tradingsymbol"]] = int(r["instrument_token"])
    out: dict[str, dict] = {}
    with (data_dir / "nfo_instruments.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["instrument_type"] != "FUT" or r["segment"] != "NFO-FUT" or r["name"] not in cash:
                continue
            exp = date.fromisoformat(r["expiry"][:10])
            if (exp - today).days < 3:
                continue
            cur = out.get(r["name"])
            if cur is None or exp < cur["expiry"]:
                out[r["name"]] = {"cash_token": cash[r["name"]], "fut_token": int(r["instrument_token"]),
                                  "fut_symbol": r["tradingsymbol"], "expiry": exp, "lot": int(float(r["lot_size"]))}
    return out


class CF:
    def __init__(self, universe: dict[str, dict], out: Path, day: str, capital: float = 250_000.0) -> None:
        self.universe = universe
        self.capital = capital
        self.tokens: dict[int, tuple[str, str]] = {}
        for s, u in universe.items():
            self.tokens[u["cash_token"]] = ("cash", s)
            self.tokens[u["fut_token"]] = ("fut", s)
        self.quotes: dict[tuple[str, str], dict] = {}
        self.minute_mid: dict[str, deque] = {}  # stock -> (minute, mid spread %), last BASELINE_MIN minutes
        self.day = day
        self.log_path = out / f"cf-{day}.jsonl"
        self.live_path = out / f"live-{day}.json"
        self.summary_path = out / f"summary-{day}.json"
        self.baselines_path = out / f"baselines-{day}.json"  # per-minute mid spreads, so a restart needs no warm-up
        self.events: list[dict] = []
        self._exit_wait_logged = False
        self.position: dict | None = None  # the one paper position
        self.busy = False  # an entry or exit is in flight
        self.last_signal: dict[str, float] = {}  # before _replay(), which restores it
        self.ticks = 0
        self._live_at = 0.0
        self._tasks: set[asyncio.Task] = set()
        self.errors = 0
        self._replay()

    # ---- the event log: the only state that survives a restart ----
    def _log(self, ev: dict) -> None:
        ev = {"ts": datetime.now(IST).isoformat(timespec="milliseconds"), **ev}
        self.events.append(ev)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")

    def _replay(self) -> None:
        if not self.log_path.is_file():
            return
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            self.events.append(ev)
            if ev.get("event") == "open":
                self.position = ev
            elif ev.get("event") == "close":
                self.position = None
            elif ev.get("event") == "signal":  # one signal per stock a minute, across restarts too
                try:
                    self.last_signal[ev["stock"]] = datetime.fromisoformat(ev["ts"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    pass
        try:
            saved = json.loads(self.baselines_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        for stock, pts in saved.items():
            self.minute_mid[stock] = deque((str(m), float(v)) for m, v in pts[-BASELINE_MIN:])

    # ---- quotes and the baseline ----
    def on_tick(self, tick: dict) -> None:
        key = self.tokens.get(int(tick.get("instrument_token") or 0))
        if key is None:
            return
        depth = tick.get("depth") or {}
        levels = {side: [(float(lv["price"]), int(lv["quantity"])) for lv in depth.get(side) or []
                         if lv.get("price") and (lv.get("quantity") or 0) > 0] for side in ("buy", "sell")}
        self.quotes[key] = {"bid": tick.get("bid"), "ask": tick.get("ask"), "bids": levels["buy"],
                            "asks": levels["sell"], "at": time.time()}
        self.ticks += 1
        self.check(key[1], time.time())

    def pair(self, stock: str, now: float) -> tuple[dict, dict] | None:
        c, f = self.quotes.get(("cash", stock)), self.quotes.get(("fut", stock))
        if not c or not f or now - c["at"] > FRESH_S or now - f["at"] > FRESH_S:
            return None
        if not all((c["bid"], c["ask"], f["bid"], f["ask"])):
            return None
        return c, f

    def baseline(self, stock: str) -> float | None:
        pts = self.minute_mid.get(stock)
        if not pts or len(pts) < BASELINE_MIN_POINTS:
            return None
        return sum(v for _, v in pts) / len(pts)

    def _note_minute(self, stock: str, minute: str, mid_spread: float) -> None:
        pts = self.minute_mid.setdefault(stock, deque())
        if pts and pts[-1][0] == minute:
            pts[-1] = (minute, mid_spread)
        else:
            pts.append((minute, mid_spread))
        while len(pts) > BASELINE_MIN:
            pts.popleft()

    def check(self, stock: str, now: float) -> None:
        p = self.pair(stock, now)
        if p is None:
            return
        c, f = p
        cash_mid = (c["bid"] + c["ask"]) / 2
        mid_spread = ((f["bid"] + f["ask"]) / 2 - cash_mid) / cash_mid * 100
        hm = datetime.fromtimestamp(now, IST).strftime("%H:%M")
        base = self.baseline(stock)
        self._note_minute(stock, hm, mid_spread)  # after reading: the baseline is the past 60 minutes only
        if self.position is not None and self.position["stock"] == stock:
            self.manage_position(hm)
            return
        if base is None or not (ENTRY_FROM <= hm <= ENTRY_UNTIL):
            return
        entry_spread = (f["bid"] - c["ask"]) / cash_mid * 100
        dev = entry_spread - base
        if dev <= ENTRY_PCT:
            return
        u = self.universe[stock]
        lot = u["lot"]
        est_net = dev / 100 * cash_mid * lot - round_trip_cost(c["ask"], f["bid"], lot)
        if est_net < MIN_NET or now - self.last_signal.get(stock, 0.0) < 60:  # one signal per stock a minute
            return
        self.last_signal[stock] = now
        lot_value = cash_mid * lot
        fits = lot_value * MARGIN_FRACTION <= self.capital
        skip_reason = ("capital" if not fits else "position_open" if self.position is not None
                       else "order_in_flight" if self.busy else None)
        traded = skip_reason is None
        self._log({"event": "signal", "stock": stock, "entry_spread_pct": round(entry_spread, 4),
                   "baseline_pct": round(base, 4), "dev_pct": round(dev, 4), "est_net": round(est_net, 2),
                   "lot": lot, "lot_value": round(lot_value), "fits_capital": fits, "traded": traded,
                   "skip_reason": skip_reason,
                   "cash_ask": c["ask"], "fut_bid": f["bid"]})
        if traded:
            self._spawn(self.enter(stock, c["ask"], f["bid"], base, est_net))

    def manage_position(self, hm: str) -> None:
        """Exit the open position at 15:15, or when its spread is back at the entry baseline. Uses the last
        known quotes even if older than FRESH_S, so a quiet stock is not left open: called on every fresh
        tick for the stock and on every pass of the session loop. The exit itself fills against the first
        update after the orders arrive (or the last known book)."""
        pos = self.position
        if pos is None or self.busy:
            return
        if hm >= FORCE_EXIT:
            self._spawn(self.exit(reason="time"))
            return
        c, f = self.quotes.get(("cash", pos["stock"])), self.quotes.get(("fut", pos["stock"]))
        if not c or not f or not c.get("bid") or not c.get("ask") or not f.get("ask"):
            return
        cash_mid = (c["bid"] + c["ask"]) / 2
        if (f["ask"] - c["bid"]) / cash_mid * 100 <= pos["baseline"]:
            self._spawn(self.exit(reason="reverted"))

    # ---- paper execution ----
    def _spawn(self, coro) -> None:
        self.busy = True
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self.busy = False
        if not task.cancelled() and task.exception() is not None:
            self.errors += 1
            print(f"{datetime.now(IST):%H:%M:%S} paper step failed: {task.exception()!r}", flush=True)

    async def _quote_after(self, key: tuple[str, str], t_min: float) -> dict | None:
        deadline = t_min + NEXT_QUOTE_WAIT_S
        while True:
            q = self.quotes.get(key)
            if q is not None and q["at"] >= t_min:
                return q
            if time.time() >= deadline:
                return None
            await asyncio.sleep(0.02)

    async def enter(self, stock: str, cash_lim: float, fut_lim: float, base: float, est_net: float) -> None:
        lot = self.universe[stock]["lot"]
        arrive = time.time() + LATENCY_MS / 1000
        await asyncio.sleep(LATENCY_MS / 1000)
        c, f = await asyncio.gather(self._quote_after(("cash", stock), arrive), self._quote_after(("fut", stock), arrive))
        if c is None or f is None:
            self._log({"event": "skip", "stock": stock, "why": "no quote update after the orders arrived"})
            return
        cash_px = limit_fill(c.get("asks") or [], lot, cash_lim, buying=True)
        fut_px = limit_fill(f.get("bids") or [], lot, fut_lim, buying=False)
        bought, sold = cash_px is not None, fut_px is not None
        if bought and sold:
            self._log({"event": "open", "stock": stock, "lot": lot, "cash_buy": cash_px, "fut_sell": fut_px,
                       "baseline": base, "est_net": round(est_net, 2)})
            self.position = self.events[-1]  # the logged event is the position, as a replay rebuilds it
            return
        if not bought and not sold:
            self._log({"event": "skip", "stock": stock, "why": "neither leg filled"})
            return
        # One leg alone: close it at market after another delay.
        t_unwind = time.time() + LATENCY_MS / 1000
        await asyncio.sleep(LATENCY_MS / 1000)
        if bought:
            q = await self._quote_after(("cash", stock), t_unwind) or self.quotes.get(("cash", stock)) or {}
            px = market_fill(q.get("bids") or [], lot, buying=False, ref=cash_px)
            pnl = (px - cash_px) * lot - leg_charges("cash", "buy", cash_px, lot) - leg_charges("cash", "sell", px, lot)
        else:
            q = await self._quote_after(("fut", stock), t_unwind) or self.quotes.get(("fut", stock)) or {}
            px = market_fill(q.get("asks") or [], lot, buying=True, ref=fut_px)
            pnl = (fut_px - px) * lot - leg_charges("fut", "sell", fut_px, lot) - leg_charges("fut", "buy", px, lot)
        self._log({"event": "unwind", "stock": stock, "leg": "cash" if bought else "futures", "pnl": round(pnl, 2)})

    async def exit(self, reason: str) -> None:
        pos = self.position
        if pos is None:
            return
        stock, lot = pos["stock"], int(pos["lot"])
        arrive = time.time() + LATENCY_MS / 1000
        await asyncio.sleep(LATENCY_MS / 1000)
        c = await self._quote_after(("cash", stock), arrive) or self.quotes.get(("cash", stock)) or {}
        f = await self._quote_after(("fut", stock), arrive) or self.quotes.get(("fut", stock)) or {}
        if not c.get("bids") or not f.get("asks"):
            if not self._exit_wait_logged:  # visible once; the next tick or loop pass tries again
                self._log({"event": "exit_waiting", "stock": stock, "reason": reason, "why": "no order book yet"})
                self._exit_wait_logged = True
            return
        self._close_at(c, f, reason)

    def close_now(self, reason: str) -> None:
        """Close the position at the last known books (end of session); P&L unknown only with no book at all."""
        pos = self.position
        if pos is None:
            return
        c = self.quotes.get(("cash", pos["stock"])) or {}
        f = self.quotes.get(("fut", pos["stock"])) or {}
        if c.get("bids") and f.get("asks"):
            self._close_at(c, f, reason, last_known=True)
        else:
            self._log({"event": "close", "stock": pos["stock"], "reason": reason + "_no_price", "pnl": None})
            self.position = None

    def _close_at(self, c: dict, f: dict, reason: str, last_known: bool = False) -> None:
        pos = self.position
        stock, lot = pos["stock"], int(pos["lot"])
        cash_sell = market_fill(c["bids"], lot, buying=False, ref=pos["cash_buy"])
        fut_buy = market_fill(f["asks"], lot, buying=True, ref=pos["fut_sell"])
        gross = (pos["fut_sell"] - fut_buy) * lot + (cash_sell - pos["cash_buy"]) * lot
        charges = (leg_charges("cash", "buy", pos["cash_buy"], lot) + leg_charges("cash", "sell", cash_sell, lot)
                   + leg_charges("fut", "sell", pos["fut_sell"], lot) + leg_charges("fut", "buy", fut_buy, lot))
        self._log({"event": "close", "stock": stock, "reason": reason, "cash_sell": cash_sell, "fut_buy": fut_buy,
                   "gross": round(gross, 2), "charges": round(charges, 2), "pnl": round(gross - charges, 2),
                   "opened": pos.get("ts"), "last_known_book": last_known})
        self.position = None
        self._exit_wait_logged = False

    # ---- reports, derived from the log ----
    def report(self, final: bool) -> dict:
        ev = self.events
        signals = [e for e in ev if e.get("event") == "signal"]
        closes = [e for e in ev if e.get("event") == "close"]
        unwinds = [e for e in ev if e.get("event") == "unwind"]
        by_stock: dict[str, int] = {}
        for s in signals:
            by_stock[s["stock"]] = by_stock.get(s["stock"], 0) + 1
        net = sum(e.get("pnl") or 0 for e in closes) + sum(e.get("pnl") or 0 for e in unwinds)
        return {
            "day": self.day, "final": final, "updated_at": datetime.now(IST).isoformat(timespec="seconds"),
            "stocks": len(self.universe), "ticks": self.ticks, "errors": self.errors,
            "rules": {"entry_pct": ENTRY_PCT, "min_net": MIN_NET, "latency_ms": LATENCY_MS, "capital": self.capital},
            "signals": len(signals), "signals_fit_capital": sum(1 for s in signals if s.get("fits_capital")),
            "signals_est_net_sum": round(sum(s.get("est_net") or 0 for s in signals), 2),
            "trades": len(closes), "wins": sum(1 for e in closes if (e.get("pnl") or 0) > 0),
            "unwinds": len(unwinds), "unwind_pnl": round(sum(e.get("pnl") or 0 for e in unwinds), 2),
            "skips": sum(1 for e in ev if e.get("event") == "skip"),
            "net": round(net, 2),
            "open_position": self.position,
            "top_signal_stocks": dict(sorted(by_stock.items(), key=lambda kv: -kv[1])[:10]),
        }

    def write_report(self, final: bool) -> dict:
        tmp = self.baselines_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: list(v) for k, v in self.minute_mid.items()}), encoding="utf-8")
        tmp.replace(self.baselines_path)
        rep = self.report(final)
        path = self.summary_path if final else self.live_path
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        tmp.replace(path)
        self._live_at = time.time()
        return rep


async def session(cf: CF, end: str) -> None:
    import websockets

    settings = load_settings()  # re-read each connect: picks up a refreshed token
    url = f"{KITE_WS_URL}?{urlencode({'api_key': settings.api_key, 'access_token': settings.access_token})}"
    tokens = sorted(cf.tokens)
    async with websockets.connect(url, ping_interval=30, ping_timeout=60, max_size=2**22) as ws:
        for i in range(0, len(tokens), 200):
            chunk = tokens[i:i + 200]
            await ws.send(json.dumps({"a": "subscribe", "v": chunk}))
            await ws.send(json.dumps({"a": "mode", "v": [WS_MODE_FULL, chunk]}))
        print(f"{datetime.now(IST):%H:%M:%S} connected, {len(tokens)} instruments in full mode", flush=True)
        while hm_now() < end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                msg = None
            if isinstance(msg, bytes):
                for tick in parse_binary_ticks(msg):
                    cf.on_tick(tick)
            cf.manage_position(hm_now())  # every pass: a quiet stock still exits on reversion or at 15:15
            if time.time() - cf._live_at >= 60:
                cf.write_report(final=False)


def acquire_lock(out: Path):
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


def finalize_missing(out: Path, end: str) -> None:
    """A past day (or today after the close) with a log but no final report gets one from its log;
    a position still open in it is closed with P&L unknown."""
    today, now_hm = datetime.now(IST).strftime("%Y-%m-%d"), hm_now()
    for log in sorted(out.glob("cf-*.jsonl")):
        day = log.stem.split("-", 1)[1]
        if (out / f"summary-{day}.json").is_file() or day > today or (day == today and now_hm < end):
            continue
        cf = CF({}, out, day)
        if cf.position is not None:
            cf._log({"event": "close", "stock": cf.position["stock"], "reason": "no_final_report", "pnl": None})
            cf.position = None
        rep = cf.write_report(final=True)
        rep["recovered"] = True
        (out / f"summary-{day}.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print(f"{day}: final report rebuilt from its log", flush=True)


def planned_until(now: datetime, days: int, end: str) -> date:
    """Date of the days-th trading session from now (today counts if it has not ended)."""
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


async def idle() -> None:
    await asyncio.Event().wait()


async def run_day(args: argparse.Namespace, out: Path) -> str:
    while (not _is_nse_trading_day(datetime.now(IST).date()) or hm_now() < args.start or hm_now() >= args.end
           or (out / f"summary-{datetime.now(IST):%Y-%m-%d}.json").is_file()):
        if args.until and datetime.now(IST).strftime("%Y-%m-%d") > args.until:
            return datetime.now(IST).strftime("%Y-%m-%d")
        await asyncio.sleep(20)
    today = datetime.now(IST).date()
    cf = CF(load_universe(ROOT / "data", today), out, today.isoformat(), args.capital)
    cf.write_report(final=False)
    print(f"{today} : {len(cf.universe)} F&O stocks (cash + near-month future) until {args.end}"
          + (f"; resumed open position in {cf.position['stock']}" if cf.position else ""), flush=True)
    backoff = 5.0
    while hm_now() < args.end:
        started = time.time()
        try:
            await session(cf, args.end)
        except Exception as exc:  # noqa: BLE001 - reconnect (token refresh, network)
            if time.time() - started > 60:  # the connection had held: start again from a short wait
                backoff = 5.0
            print(f"{datetime.now(IST):%H:%M:%S} websocket: {exc}; retrying in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    if cf._tasks:
        _, pending = await asyncio.wait(set(cf._tasks), timeout=15)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)  # stopped before the close below
    if cf.position is not None:  # not exited in time: close at the last known books
        cf.close_now("session_end")
    print(json.dumps(cf.write_report(final=True), indent=2), flush=True)
    return today.isoformat()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "cf"))
    ap.add_argument("--until", default=None, help="YYYY-MM-DD: last day to record (default: 5 trading days)")
    ap.add_argument("--capital", type=float, default=250_000.0)
    ap.add_argument("--start", default="09:15")
    ap.add_argument("--end", default="15:30")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ATLAS_LITE_CF_WATCH", "1").strip().lower() in ("0", "false", "no", "off"):
        print("ATLAS_LITE_CF_WATCH is off; idling", flush=True)
        await idle()
    lock = acquire_lock(out)
    while lock is None:
        print(f"{datetime.now(IST):%H:%M:%S} another cash-futures watcher holds the lock; waiting", flush=True)
        await asyncio.sleep(60)
        lock = acquire_lock(out)
    if not args.until:
        args.until = planned_until(datetime.now(IST), 5, args.end).isoformat()
    print(f"{datetime.now(IST):%Y-%m-%d %H:%M}: recording trading days until {args.until}", flush=True)
    finalize_missing(out, args.end)
    while datetime.now(IST).strftime("%Y-%m-%d") <= args.until:
        day = await run_day(args, out)
        if day >= args.until:
            break
    lock.close()
    print("recording finished; idling", flush=True)
    await idle()


if __name__ == "__main__":
    asyncio.run(main())
