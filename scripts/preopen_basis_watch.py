"""Usage: python3 scripts/preopen_basis_watch.py [--out data/pob] [--until YYYY-MM-DD] [--capital 250000]

Pre-open → continuous cash–futures dislocation paper research (09:15–10:00 IST).

Why: cash equities leave the NSE call auction at 09:15 while single-stock futures are already
in continuous trading. The basis can spike; this watcher measures whether that dislocation is
sticky (two quote updates ≤ CONFIRM_MAX_S) and both-fills at LATENCY_MS with est. net ≥ MIN_NET.

Read-only: never places an order. Short-the-rich-future only (buy cash, sell the future, a full lot
walked through both books) when the executable premium is more than ENTRY_PCT above the stock's own
reference: its closing basis on the previous trading day (mean mid spread of the last REF_MINUTES
minutes, from the cash-futures watcher's baselines file), less the carry of the days in between.
A flat cost-of-carry rate is not used: many stocks' futures sit above it every day, which would look
"rich" every morning and never revert. A stock with no reference (no previous file, too few minutes,
or a different futures contract then) is not traded. Exit when the full-lot unwind spread is back at
the reference, or at 10:00. Cheap futures (would need short stock) are logged as research sightings
only, not traded.

Hosted by default inside the cash-futures watcher (same Kite websocket — Kite allows 3 per key:
engine, arb-watch, cf-watch). Can also run standalone for tests. Event log pob-<day>.jsonl;
live-/summary- reports every 60 s / at 10:00.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from atlas_lite.agent_gates import _is_nse_trading_day  # noqa: E402
from atlas_lite.config import load_settings  # noqa: E402
from atlas_lite.kite_ws import KITE_WS_URL, WS_MODE_FULL, parse_binary_ticks  # noqa: E402
import cash_futures_watch as CFW  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
FAIR_RATE = 0.065  # annual carry: only to age the previous day's basis by the days in between
ENTRY_PCT = 0.15  # executable premium above the reference, % of cash mid
REF_MINUTES = 30  # previous day's closing basis: mean of its last this-many minutes
REF_MIN_POINTS = 10
MIN_NET = 500.0
LATENCY_MS = 300
CONFIRM_MAX_S = 5.0
NEXT_QUOTE_WAIT_S = 2.0
FRESH_S = 1.0
WINDOW_FROM, WINDOW_UNTIL = "09:15", "10:00"
FORCE_EXIT = "10:00"
MARGIN_FRACTION = 0.38
LOCK_NAME = "pob_watcher.lock"


def fair_pct(dte_days: int) -> float:
    """Fair futures premium as % of spot for dte calendar days."""
    return FAIR_RATE * max(dte_days, 0) / 365.0 * 100.0


def load_refs(ref_dir: Path | None, day: str, universe: dict[str, dict],
              prev_universe=None) -> tuple[str | None, dict[str, float]]:
    """(previous day, stock -> reference basis %) from the latest cash-futures baselines file before day.
    prev_universe(date) gives that day's contracts (default: CFW.load_universe); a stock whose futures
    contract differs (expiry roll) gets no reference, since its basis is a different contract's."""
    if ref_dir is None or not universe:
        return None, {}
    files = sorted(p for p in ref_dir.glob("baselines-*.json") if p.stem.split("-", 1)[1] < day)
    if not files:
        return None, {}
    prev = files[-1].stem.split("-", 1)[1]
    try:
        saved = json.loads(files[-1].read_text(encoding="utf-8"))
        then = (prev_universe or (lambda d: CFW.load_universe(ROOT / "data", d)))(date.fromisoformat(prev))
    except (OSError, ValueError):
        return prev, {}
    # Carry accrues by calendar day (futures fair value uses calendar time to expiry), so Friday's
    # basis is three days' carry richer than Monday's fair one: aging by calendar days is deliberate.
    aged = fair_pct((date.fromisoformat(day) - date.fromisoformat(prev)).days)
    refs: dict[str, float] = {}
    for stock, u in universe.items():
        pts = [float(v) for _, v in (saved.get(stock) or [])[-REF_MINUTES:]]
        if len(pts) < REF_MIN_POINTS or (then.get(stock) or {}).get("fut_symbol") != u.get("fut_symbol"):
            continue
        refs[stock] = sum(pts) / len(pts) - aged
    return prev, refs


def hm_now() -> str:
    return datetime.now(IST).strftime("%H:%M")


class POB:
    """Pre-open basis paper book. quotes/tokens may be shared with the CF watcher."""

    def __init__(
        self,
        universe: dict[str, dict],
        out: Path,
        day: str,
        capital: float = 250_000.0,
        *,
        quotes: dict | None = None,
        tokens: dict | None = None,
        ref_dir: Path | None = None,
        prev_universe=None,
        capital_busy=None,
    ) -> None:
        self.universe = universe
        self.capital = capital
        self.day = day
        out.mkdir(parents=True, exist_ok=True)
        self.log_path = out / f"pob-{day}.jsonl"
        self.live_path = out / f"live-{day}.json"
        self.summary_path = out / f"summary-{day}.json"
        if tokens is None:
            self.tokens: dict[int, tuple[str, str]] = {}
            for s, u in universe.items():
                self.tokens[u["cash_token"]] = ("cash", s)
                self.tokens[u["fut_token"]] = ("fut", s)
        else:
            self.tokens = tokens
        self.quotes: dict[tuple[str, str], dict] = quotes if quotes is not None else {}
        self.events: list[dict] = []
        self.position: dict | None = None
        self.busy = False
        self.last_signal: dict[str, float] = {}
        self.first_seen: dict[str, float] = {}
        self.last_first_log: dict[str, float] = {}
        self.ticks = 0
        self.errors = 0
        self._live_at = 0.0
        self._tasks: set[asyncio.Task] = set()
        self._exit_wait_logged = False
        self.ref_day, self.refs = load_refs(ref_dir, day, universe, prev_universe)
        self.capital_busy = capital_busy  # host callback: True while its own book holds a position
        self._replay()

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
            elif ev.get("event") == "signal" and ev.get("confirmed") is not False:
                try:
                    self.last_signal[ev["stock"]] = datetime.fromisoformat(ev["ts"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    pass

    def on_tick(self, tick: dict) -> None:
        """Standalone path: parse a Kite tick into quotes then check."""
        key = self.tokens.get(int(tick.get("instrument_token") or 0))
        if key is None:
            return
        depth = tick.get("depth") or {}
        levels = {
            side: [
                (float(lv["price"]), int(lv["quantity"]))
                for lv in depth.get(side) or []
                if lv.get("price") and (lv.get("quantity") or 0) > 0
            ]
            for side in ("buy", "sell")
        }
        self.quotes[key] = {
            "bid": tick.get("bid"),
            "ask": tick.get("ask"),
            "bids": levels["buy"],
            "asks": levels["sell"],
            "at": time.time(),
        }
        self.ticks += 1
        self.check(key[1], time.time())

    def on_host_tick(self, stock: str, now: float) -> None:
        """CF host already updated shared quotes; just evaluate this stock."""
        self.ticks += 1
        self.check(stock, now)

    def pair(self, stock: str, now: float) -> tuple[dict, dict] | None:
        c, f = self.quotes.get(("cash", stock)), self.quotes.get(("fut", stock))
        if not c or not f or now - c["at"] > FRESH_S or now - f["at"] > FRESH_S:
            return None
        if not all((c["bid"], c["ask"], f["bid"], f["ask"])):
            return None
        return c, f

    def check(self, stock: str, now: float) -> None:
        p = self.pair(stock, now)
        if p is None:  # no fresh pair: an unconfirmed sighting cannot be confirmed across the gap
            self.first_seen.pop(stock, None)
            return
        c, f = p
        hm = datetime.fromtimestamp(now, IST).strftime("%H:%M")
        if self.position is not None and self.position["stock"] == stock:
            self.manage_position(hm)
            return
        if not (WINDOW_FROM <= hm < WINDOW_UNTIL):
            self.first_seen.pop(stock, None)
            return
        if self.position is None and self.capital_busy is not None and self.capital_busy():
            return  # the host's book holds the capital (one pool for both)
        u = self.universe[stock]
        ref = self.refs.get(stock)
        if ref is None:  # no previous-day reference for this stock and contract: nothing to measure against
            return
        dte = (u["expiry"] - date.fromisoformat(self.day)).days
        cash_mid = (c["bid"] + c["ask"]) / 2
        if cash_mid <= 0:
            return
        lot = u["lot"]
        top_spread = (f["bid"] - c["ask"]) / cash_mid * 100
        # Cheap futures (top-of-book): research log only; no short-stock trade.
        if top_spread - ref < -ENTRY_PCT and now - self.last_first_log.get(f"cheap:{stock}", 0.0) >= 60:
            self.last_first_log[f"cheap:{stock}"] = now
            self._log({
                "event": "cheap_sighting",
                "stock": stock,
                "entry_spread_pct": round(top_spread, 4),
                "ref_pct": round(ref, 4),
                "rich_pct": round(top_spread - ref, 4),
                "dte": dte,
            })
        walked = CFW.full_lot_entry(c.get("asks") or [], f.get("bids") or [], lot)
        if walked is None:
            self.first_seen.pop(stock, None)
            return
        cash_vwap, cash_lim, fut_vwap, fut_lim = walked
        entry_spread = (fut_vwap - cash_vwap) / cash_mid * 100
        rich = entry_spread - ref
        if rich <= ENTRY_PCT:
            self.first_seen.pop(stock, None)
            return
        est_net = rich / 100 * cash_mid * lot - CFW.round_trip_cost(cash_vwap, fut_vwap, lot)
        if est_net < MIN_NET:
            self.first_seen.pop(stock, None)
            return
        if now - self.last_signal.get(stock, 0.0) < 60:
            return
        lot_value = cash_mid * lot
        fits = lot_value * MARGIN_FRACTION <= self.capital
        sig = {
            "event": "signal",
            "stock": stock,
            "entry_spread_pct": round(entry_spread, 4),
            "ref_pct": round(ref, 4),
            "rich_pct": round(rich, 4),
            "dte": dte,
            "est_net": round(est_net, 2),
            "lot": lot,
            "lot_value": round(lot_value),
            "fits_capital": fits,
            "cash_ask": cash_vwap,
            "fut_bid": fut_vwap,
            "cash_limit": cash_lim,
            "fut_limit": fut_lim,
            "top_cash_ask": c["ask"],
            "top_fut_bid": f["bid"],
        }
        first = self.first_seen.get(stock)
        if first is None or now - first > CONFIRM_MAX_S:
            self.first_seen[stock] = now
            if now - self.last_first_log.get(stock, 0.0) >= 60:
                self.last_first_log[stock] = now
                self._log({**sig, "confirmed": False, "traded": False, "skip_reason": "unconfirmed"})
                self._probe(stock, cash_lim, fut_lim, confirmed=False)
            return
        del self.first_seen[stock]
        self.last_signal[stock] = now
        skip_reason = (
            "capital" if not fits else "position_open" if self.position is not None
            else "order_in_flight" if self.busy else None
        )
        traded = skip_reason is None
        self._log({**sig, "confirmed": True, "traded": traded, "skip_reason": skip_reason})
        if traded:
            self._spawn(self.enter(stock, cash_lim, fut_lim, ref, est_net))
        else:
            self._probe(stock, cash_lim, fut_lim, confirmed=True)

    def manage_position(self, hm: str) -> None:
        pos = self.position
        if pos is None or self.busy:
            return
        if hm >= FORCE_EXIT:
            self._spawn(self.exit(reason="time"))
            return
        c, f = self.quotes.get(("cash", pos["stock"])), self.quotes.get(("fut", pos["stock"]))
        if not c or not f:
            return
        # Full-lot unwind spread (what the exit fill pays) back at the entry reference.
        exit_spread = CFW.full_lot_exit_spread(c, f, int(pos["lot"]))
        ref = pos.get("ref_pct", pos.get("fair_pct"))  # fair_pct: positions opened by the first version
        if exit_spread is not None and ref is not None and exit_spread <= ref:
            self._spawn(self.exit(reason="reverted"))

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
            print(f"{datetime.now(IST):%H:%M:%S} pob paper step failed: {task.exception()!r}", flush=True)

    async def _quote_after(self, key: tuple[str, str], t_min: float) -> dict | None:
        deadline = t_min + NEXT_QUOTE_WAIT_S
        while True:
            q = self.quotes.get(key)
            if q is not None and q["at"] >= t_min:
                return q
            if time.time() >= deadline:
                return None
            await asyncio.sleep(0.02)

    async def _entry_fill(self, stock: str, cash_lim: float, fut_lim: float):
        lot = self.universe[stock]["lot"]
        arrive = time.time() + LATENCY_MS / 1000
        await asyncio.sleep(LATENCY_MS / 1000)
        c, f = await asyncio.gather(
            self._quote_after(("cash", stock), arrive),
            self._quote_after(("fut", stock), arrive),
        )
        if c is None or f is None:
            return None
        return (
            CFW.limit_fill(c.get("asks") or [], lot, cash_lim, buying=True),
            CFW.limit_fill(f.get("bids") or [], lot, fut_lim, buying=False),
        )

    def _probe(self, stock: str, cash_lim: float, fut_lim: float, confirmed: bool) -> None:
        task = asyncio.get_running_loop().create_task(self.probe(stock, cash_lim, fut_lim, confirmed))
        self._tasks.add(task)
        task.add_done_callback(self._probe_done)

    def _probe_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.errors += 1
            print(f"{datetime.now(IST):%H:%M:%S} pob probe failed: {task.exception()!r}", flush=True)

    async def probe(self, stock: str, cash_lim: float, fut_lim: float, confirmed: bool) -> None:
        self._log_probe(stock, confirmed, await self._entry_fill(stock, cash_lim, fut_lim))

    def _log_probe(self, stock: str, confirmed: bool, fill) -> None:
        if fill is None:
            outcome = "unknown"
        else:
            bought, sold = fill[0] is not None, fill[1] is not None
            outcome = "both" if bought and sold else "none" if not bought and not sold else "one_leg"
        self._log({"event": "probe", "stock": stock, "confirmed": confirmed, "outcome": outcome})

    async def enter(self, stock: str, cash_lim: float, fut_lim: float, ref: float, est_net: float) -> None:
        lot = self.universe[stock]["lot"]
        fill = await self._entry_fill(stock, cash_lim, fut_lim)
        self._log_probe(stock, True, fill)
        if fill is None:
            self._log({"event": "skip", "stock": stock, "why": "no quote update after the orders arrived"})
            return
        cash_px, fut_px = fill
        bought, sold = cash_px is not None, fut_px is not None
        if bought and sold:
            self._log({
                "event": "open",
                "stock": stock,
                "lot": lot,
                "cash_buy": cash_px,
                "fut_sell": fut_px,
                "ref_pct": ref,
                "est_net": round(est_net, 2),
            })
            self.position = self.events[-1]
            return
        if not bought and not sold:
            self._log({"event": "skip", "stock": stock, "why": "neither leg filled"})
            return
        t_unwind = time.time() + LATENCY_MS / 1000
        await asyncio.sleep(LATENCY_MS / 1000)
        if bought:
            q = await self._quote_after(("cash", stock), t_unwind) or self.quotes.get(("cash", stock)) or {}
            px = CFW.market_fill(q.get("bids") or [], lot, buying=False, ref=cash_px)
            pnl = (
                (px - cash_px) * lot
                - CFW.leg_charges("cash", "buy", cash_px, lot)
                - CFW.leg_charges("cash", "sell", px, lot)
            )
        else:
            q = await self._quote_after(("fut", stock), t_unwind) or self.quotes.get(("fut", stock)) or {}
            px = CFW.market_fill(q.get("asks") or [], lot, buying=True, ref=fut_px)
            pnl = (
                (fut_px - px) * lot
                - CFW.leg_charges("fut", "sell", fut_px, lot)
                - CFW.leg_charges("fut", "buy", px, lot)
            )
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
            if not self._exit_wait_logged:
                self._log({"event": "exit_waiting", "stock": stock, "reason": reason, "why": "no order book yet"})
                self._exit_wait_logged = True
            return
        self._close_at(c, f, reason)

    def close_now(self, reason: str) -> None:
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
        cash_sell = CFW.market_fill(c["bids"], lot, buying=False, ref=pos["cash_buy"])
        fut_buy = CFW.market_fill(f["asks"], lot, buying=True, ref=pos["fut_sell"])
        gross = (pos["fut_sell"] - fut_buy) * lot + (cash_sell - pos["cash_buy"]) * lot
        charges = (
            CFW.leg_charges("cash", "buy", pos["cash_buy"], lot)
            + CFW.leg_charges("cash", "sell", cash_sell, lot)
            + CFW.leg_charges("fut", "sell", pos["fut_sell"], lot)
            + CFW.leg_charges("fut", "buy", fut_buy, lot)
        )
        self._log({
            "event": "close",
            "stock": stock,
            "reason": reason,
            "cash_sell": cash_sell,
            "fut_buy": fut_buy,
            "gross": round(gross, 2),
            "charges": round(charges, 2),
            "pnl": round(gross - charges, 2),
            "opened": pos.get("ts"),
            "last_known_book": last_known,
        })
        self.position = None
        self._exit_wait_logged = False

    def report(self, final: bool) -> dict:
        ev = self.events
        signals = [e for e in ev if e.get("event") == "signal" and e.get("confirmed") is not False]
        probes = {
            name: {
                o: sum(
                    1
                    for e in ev
                    if e.get("event") == "probe" and e.get("confirmed") is conf and e.get("outcome") == o
                )
                for o in ("both", "one_leg", "none", "unknown")
            }
            for name, conf in (("first_sighting", False), ("confirmed", True))
        }
        closes = [e for e in ev if e.get("event") == "close"]
        unwinds = [e for e in ev if e.get("event") == "unwind"]
        by_stock: dict[str, int] = {}
        for s in signals:
            by_stock[s["stock"]] = by_stock.get(s["stock"], 0) + 1
        net = sum(e.get("pnl") or 0 for e in closes) + sum(e.get("pnl") or 0 for e in unwinds)
        return {
            "day": self.day,
            "final": final,
            "updated_at": datetime.now(IST).isoformat(timespec="seconds"),
            "book": "preopen_basis",
            "stocks": len(self.universe),
            "ticks": self.ticks,
            "errors": self.errors,
            "rules": {
                "window": f"{WINDOW_FROM}-{WINDOW_UNTIL}",
                "entry_pct_above_ref": ENTRY_PCT,
                "reference": f"previous day's closing basis (last {REF_MINUTES} min), aged at {FAIR_RATE:.1%}/yr",
                "ref_day": self.ref_day,
                "stocks_with_ref": len(self.refs),
                "min_net": MIN_NET,
                "latency_ms": LATENCY_MS,
                "confirm_max_s": CONFIRM_MAX_S,
                "capital": self.capital,
            },
            "unconfirmed_sightings": sum(
                1 for e in ev if e.get("event") == "signal" and e.get("confirmed") is False
            ),
            "cheap_sightings": sum(1 for e in ev if e.get("event") == "cheap_sighting"),
            "entry_probes": probes,
            "signals": len(signals),
            "signals_fit_capital": sum(1 for s in signals if s.get("fits_capital")),
            "signals_est_net_sum": round(sum(s.get("est_net") or 0 for s in signals), 2),
            "trades": len(closes),
            "wins": sum(1 for e in closes if (e.get("pnl") or 0) > 0),
            "unwinds": len(unwinds),
            "unwind_pnl": round(sum(e.get("pnl") or 0 for e in unwinds), 2),
            "skips": sum(1 for e in ev if e.get("event") == "skip"),
            "net": round(net, 2),
            "open_position": self.position,
            "top_signal_stocks": dict(sorted(by_stock.items(), key=lambda kv: -kv[1])[:10]),
        }

    def write_report(self, final: bool) -> dict:
        rep = self.report(final)
        path = self.summary_path if final else self.live_path
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        tmp.replace(path)
        self._live_at = time.time()
        return rep

    async def drain(self, timeout: float = 15.0) -> None:
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=timeout)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def finalize_missing(out: Path) -> None:
    today, now_hm = datetime.now(IST).strftime("%Y-%m-%d"), hm_now()
    for log in sorted(out.glob("pob-*.jsonl")):
        day = log.stem.split("-", 1)[1]
        if (out / f"summary-{day}.json").is_file() or day > today or (day == today and now_hm < FORCE_EXIT):
            continue
        pob = POB({}, out, day)
        if pob.position is not None:  # as the cash-futures watcher does: no book at recovery, P&L unknown
            pob._log({"event": "close", "stock": pob.position["stock"], "reason": "no_final_report", "pnl": None})
            pob.position = None
        rep = pob.write_report(final=True)
        rep["recovered"] = True
        (out / f"summary-{day}.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print(f"{day}: pob final report rebuilt from its log", flush=True)


# ---- optional standalone runner (uses its own websocket; prefer CF host in production) ----

async def idle() -> None:
    await asyncio.Event().wait()


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


def planned_until(now: datetime, days: int, end: str) -> date:
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


async def session(pob: POB, end: str) -> None:
    import websockets

    settings = load_settings()
    url = f"{KITE_WS_URL}?{urlencode({'api_key': settings.api_key, 'access_token': settings.access_token})}"
    tokens = sorted(pob.tokens)
    async with websockets.connect(url, ping_interval=30, ping_timeout=60, max_size=2**22) as ws:
        for i in range(0, len(tokens), 200):
            chunk = tokens[i : i + 200]
            await ws.send(json.dumps({"a": "subscribe", "v": chunk}))
            await ws.send(json.dumps({"a": "mode", "v": [WS_MODE_FULL, chunk]}))
        print(f"{datetime.now(IST):%H:%M:%S} pob connected, {len(tokens)} instruments", flush=True)
        while hm_now() < end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                msg = None
            if isinstance(msg, bytes):
                for tick in parse_binary_ticks(msg):
                    pob.on_tick(tick)
            pob.manage_position(hm_now())
            if time.time() - pob._live_at >= 60:
                pob.write_report(final=False)


async def run_day(args: argparse.Namespace, out: Path) -> str:
    while (
        not _is_nse_trading_day(datetime.now(IST).date())
        or hm_now() < args.start
        or hm_now() >= args.end
        or (out / f"summary-{datetime.now(IST):%Y-%m-%d}.json").is_file()
    ):
        if args.until and datetime.now(IST).strftime("%Y-%m-%d") > args.until:
            return datetime.now(IST).strftime("%Y-%m-%d")
        await asyncio.sleep(20)
    today = datetime.now(IST).date()
    pob = POB(CFW.load_universe(ROOT / "data", today), out, today.isoformat(), args.capital,
              ref_dir=ROOT / "data" / "cf")
    pob.write_report(final=False)
    print(f"{today} pob: {len(pob.universe)} stocks until {args.end}", flush=True)
    backoff = 5.0
    while hm_now() < args.end:
        started = time.time()
        try:
            await session(pob, args.end)
        except Exception as exc:  # noqa: BLE001
            if time.time() - started > 60:
                backoff = 5.0
            print(f"{datetime.now(IST):%H:%M:%S} pob websocket: {exc}; retrying in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    await pob.drain()
    if pob.position is not None:
        pob.close_now("session_end")
    print(json.dumps(pob.write_report(final=True), indent=2), flush=True)
    return today.isoformat()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "pob"))
    ap.add_argument("--until", default=None)
    ap.add_argument("--capital", type=float, default=250_000.0)
    ap.add_argument("--start", default=WINDOW_FROM)
    ap.add_argument("--end", default=FORCE_EXIT)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ATLAS_LITE_POB_WATCH", "1").strip().lower() in ("0", "false", "no", "off"):
        print("ATLAS_LITE_POB_WATCH is off; idling", flush=True)
        await idle()
    lock = acquire_lock(out)
    while lock is None:
        print(f"{datetime.now(IST):%H:%M:%S} another pob watcher holds the lock; waiting", flush=True)
        await asyncio.sleep(60)
        lock = acquire_lock(out)
    if not args.until:
        args.until = planned_until(datetime.now(IST), 5, "15:30").isoformat()
    print(f"{datetime.now(IST):%Y-%m-%d %H:%M}: pob recording until {args.until}", flush=True)
    finalize_missing(out)
    while datetime.now(IST).strftime("%Y-%m-%d") <= args.until:
        day = await run_day(args, out)
        if day >= args.until:
            break
    lock.close()
    print("pob recording finished; idling", flush=True)
    await idle()


if __name__ == "__main__":
    asyncio.run(main())
