"""LLM option buyer: one lot of the ATM NIFTY call OR put, side and timing chosen by an LLM. Paper by default.

Every 5 minutes from 09:20 to 15:00 the LLM sees what a discretionary trader would see at that minute (NIFTY
path and range, % change, VIX and its change, ATM IV against today's high/low, IV percentile, PCR, futures OI
vs day high, ATM call/put prices and the straddle path, days to expiry, its own position and P&L) and answers
BUY_CE / BUY_PE / WAIT / DONE_FOR_DAY when flat, HOLD / EXIT when holding. Same prompt and inputs as the
8 Nov 2026 replay (scratchpad llm_replay.py, gpt-4.1, dates hidden): 7 Oct -Rs 1,296, 8 Oct +Rs 6,581,
13 Oct +Rs 3,449. Three hand-picked days only: this book is the forward test.

Code, not the LLM, owns the risk:
- Day loss stop DAY_LOSS_LIMIT: today's closed P&L plus the open position at bids, after estimated charges,
  checked every frame. Hit -> exit at once and no more entries today.
- At most MAX_ENTRIES a day; no new entry after LAST_ENTRY; everything is sold at EXIT_AT.
- An LLM error, timeout or unusable answer (not an allowed action) is treated as WAIT / HOLD;
  MAX_ERRORS_HOLDING of them in a row while holding sell the position ("llm_unavailable"). A decision that no longer fits the position (a buy while holding, an
  exit while flat) is ignored.

Live (intraday MIS, 1 lot): decided at each entry, on a day the dashboard checkbox "Live LLM buyer" is ticked
and the host allows it (ATLAS_LITE_LLM_BUYER_LIVE_ALLOWED=1). Before buying, the option's ask x qty plus 10%
must fit the funds Kite shows available, and live stops for the month at MONTH_LOSS_LIMIT. Orders are run by
atlas_lite/live_straddle.py with a single leg (tagged orders, holdings from this book's own fills).
Kill switch: unticking the box (after it was on today) sells an open live position at once (reason "ui_off")
and stops the book for the rest of the day, paper entries included. The 15:30 auto-off (source "day_end") is
not a kill. No order is sent after LIVE_ORDERS_UNTIL (Zerodha squares MIS off itself at 15:14). A live trade still open from a ticked
day is also sold if the box is found off after a restart.

Paper fills: buy at ask, sell at bid when quotes have them, else last price +/- SLIP_PTS. Kite charges.
Ledger: paper_llm_buyer.jsonl (every decision with its reason, opens, closes, stops, live order events).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

import httpx

from atlas_lite.agent_gates import _is_nse_trading_day
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.live_straddle import LiveStraddle
from atlas_lite.log_util import get_logger
from atlas_lite.metrics import quote_ask, quote_bid, quote_ltp

IST = ZoneInfo("Asia/Kolkata")
STRATEGY = "llm_buyer"
LEDGER_NAME = "paper_llm_buyer.jsonl"
LIVE_FLAG_NAME = "llm_buyer_live.json"
LIVE_ENV = "ATLAS_LITE_LLM_BUYER_LIVE_ALLOWED"
DEFAULT_LOT_SIZE = 65
FIRST_DECISION, LAST_ENTRY, LAST_DECISION = "09:20", "14:30", "15:00"
EXIT_AT, LIVE_ORDERS_UNTIL = "15:05", "15:10"  # Zerodha squares F&O MIS off at 15:14
DECIDE_EVERY_MIN = 5
MAX_ENTRIES = 2
MAX_ERRORS_HOLDING = 3  # LLM errors in a row while holding -> sell (about 15 minutes without a working LLM)
DAY_LOSS_LIMIT = 2_000.0
MONTH_LOSS_LIMIT = 10_000.0
EST_CHARGES = 80.0  # a buy and a sell of one ATM lot, for the running day-loss check
SLIP_PTS = 1.0
FUNDS_HEADROOM = 1.10
TAG_PREFIX = "llm"
MODEL_OPENROUTER, MODEL_OPENAI = "openai/gpt-4.1", "gpt-4.1"
LLM_TIMEOUT_S = 25.0
LIVE_EVENTS = frozenset({"order_intent", "order_sent", "order_send_error", "order_cancel_sent", "order_final",
                         "live_phase", "manual_needed", "live_mark"})

SYSTEM_PROMPT = """You are an experienced discretionary NIFTY options trader who BUYS options to trade direction. The only
positions you may take: buy one lot (65) of the at-the-money CALL (if you expect NIFTY to rise) or the at-the-money
PUT (if you expect it to fall). You decide when to buy and which side, whether to skip the day, and when to exit.
Time decay and falling IV work against an option buyer, so the move must come soon and be big enough.
Round-trip costs are about Rs 60-80 per option plus roughly 1 point of slippage. You may enter at most twice a day
and everything is closed at 15:15. Judge like a careful professional: you are not obliged to trade. You see only
information available at the current minute.
Reply with JSON only: {"action": "...", "reason": "<one short sentence>"}
When flat the action is one of BUY_CE, BUY_PE, WAIT, DONE_FOR_DAY. When holding: HOLD, EXIT."""


def paper_llm_buyer_enabled() -> bool:
    return os.environ.get("ATLAS_LITE_PAPER_LLM_BUYER", "1").strip().lower() in ("1", "true", "yes")


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


class LlmDecider:
    """One chat call: OpenRouter first (if configured), then the OpenAI key. Never raises."""

    def __init__(self, credentials: Any, *, post: Callable[..., Any] | None = None) -> None:
        self.credentials = credentials
        self._post = post or httpx.post

    def targets(self) -> list[tuple[str, str, str, str]]:
        c = self.credentials
        out = []
        if c is not None and getattr(c, "api_key", "") and "openrouter" in str(getattr(c, "base_url", "")):
            out.append(("openrouter", c.api_key, c.base_url, MODEL_OPENROUTER))
        if c is not None and getattr(c, "openai_api_key", ""):
            out.append(("openai", c.openai_api_key, c.openai_base_url or "https://api.openai.com/v1", MODEL_OPENAI))
        return out

    def decide(self, state: dict[str, Any]) -> dict[str, Any]:
        errors = []
        for name, key, base, model in self.targets():
            body = {"model": model, "temperature": 0.1, "max_tokens": 120,
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                 {"role": "user", "content": json.dumps(state, separators=(",", ":"))}]}
            try:
                r = self._post(f"{base.rstrip('/')}/chat/completions", json=body, timeout=LLM_TIMEOUT_S,
                               headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                        "HTTP-Referer": "https://atlas-lite.local", "X-Title": "Atlas Lite LLM buyer"})
                r.raise_for_status()
                txt = str(r.json()["choices"][0]["message"]["content"] or "")
                out = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
                # The prompt is the replayed one (no factors requested: asking changed decisions); kept if offered.
                factors = out.get("factors") if isinstance(out.get("factors"), list) else []
                return {"action": str(out.get("action") or "").upper(), "reason": str(out.get("reason") or "")[:300],
                        "factors": [str(f)[:60] for f in factors[:4]], "provider": name, "model": model,
                        "inputs": state}
            except Exception as exc:  # noqa: BLE001 - try the next provider; the book treats errors as WAIT/HOLD
                errors.append(f"{name}:{type(exc).__name__}:{getattr(getattr(exc, 'response', None), 'status_code', '')}")
        return {"action": "", "reason": "", "error": ";".join(errors) or "no_llm_credentials", "inputs": state}


@dataclass
class Position:
    day: str
    leg: str  # "ce" | "pe"
    symbol: str
    strike: int
    qty: int
    fill: float
    opened_at: str
    reason: str = ""


@dataclass
class LlmBuyer:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    order_client: Any = None
    live_flag: Any = None
    decider: Any = None
    strategy: str = STRATEGY
    day: str = ""
    entries: int = 0
    day_realized: float = 0.0
    stopped: str = ""  # why entries are over for today ("day_loss", "done_for_day")
    decided: set = field(default_factory=set)
    position: Position | None = None
    live: LiveStraddle | None = None
    live_meta: dict[str, Any] = field(default_factory=dict)
    pending_close: dict[str, Any] | None = None
    month_live_pnl: dict[str, float] = field(default_factory=dict)
    history: dict[str, dict[str, float]] = field(default_factory=dict)  # minute -> spot / straddle samples
    last_feed: dict[str, Any] = field(default_factory=dict)
    last_decision: dict[str, Any] | None = None
    last_reject: str | None = None
    live_on_day: str = ""  # last day the live box was seen ticked (the kill switch arms on it)
    last_feed_at: str = ""  # minute of the last feed recorded today
    errors_in_row: int = 0  # LLM answers in a row that failed (reset by any valid answer)
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.llm_buyer"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._restore()

    @property
    def qty(self) -> int:
        return int(self.lot_size)

    def live_enabled(self) -> bool:
        return bool(self.order_client is not None and self.live_flag is not None and self.live_flag.is_enabled())

    # ---- ledger ----
    def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event.setdefault("strategy", self.strategy)
        event.setdefault("live_orders", bool(event.get("live")))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
                if event.get("live"):  # real-money lines survive a host crash too
                    fh.flush()
                    os.fsync(fh.fileno())
        except OSError as exc:
            self._log.warning("llm_buyer ledger write failed: %s", exc)
            return None
        return event

    def _live_log(self, event: dict[str, Any]) -> dict[str, Any] | None:
        return self._append({"ts": datetime.now(IST).isoformat(), "day": self.live.day if self.live else self.day,
                             "live": True, **event})

    def _restore(self) -> None:
        if not self.path.is_file():
            return
        today = datetime.now(IST).strftime("%Y-%m-%d")
        live_start: dict[str, Any] | None = None
        live_events: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict) or ev.get("strategy") != self.strategy:
                continue
            kind, d = ev.get("event"), str(ev.get("day") or "")
            if kind == "close" and ev.get("live") and ev.get("pnl") is not None:
                m = d[:7]
                self.month_live_pnl[m] = round(self.month_live_pnl.get(m, 0.0) + float(ev["pnl"]), 2)
            if kind == "live_start":
                live_start, live_events = ev, []
            elif kind in LIVE_EVENTS and live_start is not None:
                live_events.append(ev)
            elif kind == "close" and ev.get("live"):
                live_start = None
            elif kind == "live_abort":
                live_start = None  # nothing was sent for it
            if d != today:
                continue
            self.day = today
            if kind == "decision":
                self.decided.add(str(ev.get("hm")))
                ok = ev.get("valid") if "valid" in ev else ev.get("action") != "ERROR"
                self.errors_in_row = 0 if ok else self.errors_in_row + 1
            elif kind == "live_start":
                self.entries += 1  # a live entry counts from its start line (its "open" comes once filled)
            elif kind == "live_abort":
                self.entries -= 1
            elif kind == "open":
                if not ev.get("live"):
                    self.entries += 1
                    self.position = Position(day=d, leg=ev["leg"], symbol=ev["symbol"], strike=int(ev.get("strike") or 0),
                                             qty=int(ev["qty"]), fill=float(ev["fill"]), opened_at=str(ev.get("ts")),
                                             reason=str(ev.get("reason") or ""))
            elif kind == "close":
                self.day_realized = round(self.day_realized + float(ev.get("pnl") or 0.0), 2)
                if not ev.get("live"):
                    self.position = None
            elif kind == "stop_entries":
                self.stopped = str(ev.get("why") or "stopped")
        if live_start is not None:  # a live trade without its close line: finish it on the broker
            self.live_meta = dict(live_start)
            self.live = LiveStraddle.restore(live_start, live_events, self._live_log)

    # ---- day bookkeeping ----
    def _new_day(self, today: str) -> None:
        if self.day != today:
            self.day, self.entries, self.day_realized, self.stopped = today, 0, 0.0, ""
            self.decided, self.history, self.last_decision = set(), {}, None
            self.last_feed, self.last_feed_at = {}, ""  # never trade on yesterday's ATM symbols

    def _record(self, hm: str, feed: dict[str, Any]) -> None:
        spot, ce, pe = _f(feed.get("spot")), _f(feed.get("ce")), _f(feed.get("pe"))
        if spot:
            row = self.history.setdefault(hm, {})
            row["spot"] = spot
            if ce and pe:
                row["straddle"] = ce + pe
        self.last_feed = dict(feed)
        self.last_feed_at = hm

    def _open_pnl(self, book: QuoteSource) -> float | None:
        """Open P&L in Rs at bids (live: from the real fills), before charges."""
        if self.live is not None:
            leg = self.live.legs[0]
            avg, q = self.live.cost(leg)
            held = self.live.held(leg)
            bid = quote_bid(book.get(self.live.symbol(leg))) or quote_ltp(book.get(self.live.symbol(leg)))
            if not held or not avg or not bid:
                return 0.0 if not held else None
            return (bid - avg) * held
        if self.position is None:
            return 0.0
        bid = self._sell_px(book, self.position.symbol)
        return None if bid is None else (bid - self.position.fill) * self.position.qty

    def day_pnl(self, book: QuoteSource) -> float | None:
        o = self._open_pnl(book)
        held = self.position is not None or self.live is not None
        return None if o is None else self.day_realized + o - (EST_CHARGES if held else 0.0)

    # ---- the LLM's view ----
    def decision_due(self, now: datetime) -> bool:
        hm = now.strftime("%H:%M")
        if not _is_nse_trading_day(now.date()) or not (FIRST_DECISION <= hm <= LAST_DECISION):
            return False
        if now.minute % DECIDE_EVERY_MIN or hm in self.decided or self.day != now.strftime("%Y-%m-%d"):
            return False
        holding = self.position is not None or self.live is not None
        if holding:
            return self.live is None or self.live.phase == "holding"
        return not self.stopped and self.entries < MAX_ENTRIES and hm <= LAST_ENTRY and self._feed_fresh(now)

    def _feed_fresh(self, now: datetime) -> bool:
        """Today's feed, recorded this minute or within the 2 before (a stale feed means stale ATM symbols)."""
        if not self.last_feed_at or not self.last_feed.get("ce_symbol") or not self.last_feed.get("pe_symbol"):
            return False
        a, b = now.strftime("%H:%M"), self.last_feed_at
        return (int(a[:2]) * 60 + int(a[3:])) - (int(b[:2]) * 60 + int(b[3:])) <= 2

    def state(self, now: datetime, book: QuoteSource) -> dict[str, Any]:
        f, hm = self.last_feed, now.strftime("%H:%M")
        mins = sorted(m for m in self.history if m <= hm)
        spots = [self.history[m]["spot"] for m in mins]
        exp = str(f.get("expiry") or "")[:10]
        try:
            dte = (date.fromisoformat(exp) - now.date()).days if exp else None
        except ValueError:
            dte = None
        s: dict[str, Any] = {
            "time": hm, "weekday": now.strftime("%A"), "calendar_days_to_expiry": dte,
            "nifty": round(spots[-1], 1) if spots else _f(f.get("spot")),
            "nifty_change_vs_prev_close_pct": f.get("index_nifty_chg"),
            "day_open": round(spots[0], 1) if spots else None, "day_high": round(max(spots), 1) if spots else None,
            "day_low": round(min(spots), 1) if spots else None,
            "nifty_path": [f"{m} {self.history[m]['spot']:.0f}" for m in mins if m[3:] in ("00", "30") or m == mins[-1]][-14:],
            "vix": f.get("vix"), "vix_change_since_open_pts": f.get("vix_chg"),
            "atm_iv": f.get("iv"), "atm_iv_day_high": f.get("iv_day_high"), "atm_iv_day_low": f.get("iv_day_low"),
            "iv_percentile_1y": f.get("ivp"), "pcr": f.get("pcr"), "futures_oi_vs_day_high_pct": f.get("oi_vs_day_high"),
            "atm_strike": f.get("atm"), "atm_ce": f.get("ce"), "atm_pe": f.get("pe"),
            "atm_straddle_path_since_open": [f"{m} {self.history[m]['straddle']:.1f}" for m in mins
                                             if "straddle" in self.history[m] and (m[3:] in ("00", "15", "30", "45") or m == mins[-1])][-14:],
            "entries_used_today": self.entries, "realised_pnl_today_rs": round(self.day_realized),
        }
        if self.position is not None or self.live is not None:
            if self.live is not None:
                leg = self.live.legs[0]
                sym, fill = self.live.symbol(leg), self.live.cost(leg)[0]
                opened = str(self.live_meta.get("ts") or "")[11:16]
            else:
                leg, sym, fill, opened = self.position.leg, self.position.symbol, self.position.fill, self.position.opened_at[11:16]
            now_px = quote_bid(book.get(sym)) or quote_ltp(book.get(sym))
            o = self._open_pnl(book)
            s["position"] = {"option": leg.upper(), "strike": self.live_meta.get("strike") if self.live else self.position.strike,
                             "entered_at": opened, "bought_at": round(fill, 2), "now": now_px,
                             "open_pnl_rs": round(o) if o is not None else None}
        return s

    def apply(self, now: datetime, out: dict[str, Any], book: QuoteSource, *, allow_entry: bool = True) -> dict[str, Any] | None:
        """Act on one LLM answer (asked for the minute `out['hm']`)."""
        hm = str(out.get("hm") or now.strftime("%H:%M"))
        act = str(out.get("action") or "")
        holding = self.position is not None or self.live is not None
        self.decided.add(hm)
        self.last_decision = {"hm": hm, "action": act or "ERROR", "reason": out.get("reason") or out.get("error"),
                              "factors": out.get("factors") or [], "provider": out.get("provider"),
                              "inputs": out.get("inputs") or {}}
        # The full input the LLM saw is kept with its answer, so every decision can be audited later.
        self._append({"event": "decision", "ts": now.isoformat(), "day": self.day, "hm": hm, "action": act or "ERROR",
                      "reason": out.get("reason"), "factors": out.get("factors") or [], "error": out.get("error"),
                      "provider": out.get("provider"), "model": out.get("model"), "holding": holding,
                      "valid": act in (("HOLD", "EXIT") if holding else ("BUY_CE", "BUY_PE", "WAIT", "DONE_FOR_DAY")),
                      "inputs": out.get("inputs") or {}})
        valid = act in (("HOLD", "EXIT") if holding else ("BUY_CE", "BUY_PE", "WAIT", "DONE_FOR_DAY"))
        self.errors_in_row = 0 if valid else self.errors_in_row + 1  # an unusable answer counts as an error
        if holding and not valid and self.errors_in_row >= MAX_ERRORS_HOLDING:
            self._log.warning("llm_buyer: %d LLM errors in a row while holding -> selling", self.errors_in_row)
            return self._exit(now, book, "llm_unavailable")
        if not holding and act in ("BUY_CE", "BUY_PE"):
            if not allow_entry:
                self.last_reject = "gate_blocked"
                return None
            if self.stopped or self.entries >= MAX_ENTRIES or now.strftime("%H:%M") > LAST_ENTRY:
                return None
            if self.day_realized - EST_CHARGES <= -DAY_LOSS_LIMIT:  # no day-loss budget left for another trade
                self._stop_entries(now, "day_loss")
                return None
            return self._open(now, "ce" if act == "BUY_CE" else "pe", book, str(out.get("reason") or ""))
        if not holding and act == "DONE_FOR_DAY":
            self._stop_entries(now, "done_for_day")
        elif holding and act == "EXIT":
            return self._exit(now, book, "llm_exit")
        return None

    # ---- entries / exits ----
    @staticmethod
    def _buy_px(book: QuoteSource, symbol: str) -> float | None:
        row = book.get(symbol)
        ask, ltp = quote_ask(row), quote_ltp(row)
        return float(ask) if ask else (float(ltp) + SLIP_PTS if ltp else None)

    @staticmethod
    def _sell_px(book: QuoteSource, symbol: str) -> float | None:
        row = book.get(symbol)
        bid, ltp = quote_bid(row), quote_ltp(row)
        return float(bid) if bid else (max(0.05, float(ltp) - SLIP_PTS) if ltp else None)

    def _stop_entries(self, now: datetime, why: str) -> None:
        if not self.stopped:
            self.stopped = why
            self._append({"event": "stop_entries", "ts": now.isoformat(), "day": self.day, "why": why})

    def _open(self, now: datetime, leg: str, book: QuoteSource, reason: str) -> dict[str, Any] | None:
        f = self.last_feed
        symbol, strike = f.get(f"{leg}_symbol"), int(f.get("atm") or 0)
        if not self._feed_fresh(now):
            self.last_reject = "stale_feed"
            return None
        if not symbol:
            self.last_reject = "no_atm_symbol"
            return None
        px = self._buy_px(book, str(symbol))
        if not px:
            self.last_reject = "no_quote"
            return None
        if self.live_enabled():
            return self._open_live(now, leg, str(symbol), strike, px, reason)
        ev = self._append({"event": "open", "ts": now.isoformat(), "day": self.day, "leg": leg, "symbol": symbol,
                           "strike": strike, "qty": self.qty, "fill": round(px, 2), "entry": round(px, 2),
                           "side": "long", "reason": reason})
        if ev is None:  # no ledger line, no position (a restart could not know about it)
            self.last_reject = "ledger_write_failed"
            return None
        self.entries += 1
        self.position = Position(day=self.day, leg=leg, symbol=str(symbol), strike=strike, qty=self.qty, fill=round(px, 2),
                                 opened_at=now.isoformat(), reason=reason)
        self.last_reject = None
        return ev

    def _open_live(self, now: datetime, leg: str, symbol: str, strike: int, ask: float, reason: str) -> dict[str, Any] | None:
        m = self.day[:7]
        if self.month_live_pnl.get(m, 0.0) <= -MONTH_LOSS_LIMIT:
            self.last_reject = "month_loss_limit"
            return self._append({"event": "live_skip", "ts": now.isoformat(), "day": self.day, "live": True,
                                 "why": "month_loss_limit"})
        need = ask * self.qty * FUNDS_HEADROOM
        try:
            available = float(self.order_client.equity_available())
        except Exception as exc:  # noqa: BLE001 - no funds reading, no order
            self.last_reject = f"funds_unavailable:{exc}"
            return self._append({"event": "live_skip", "ts": now.isoformat(), "day": self.day, "live": True,
                                 "why": self.last_reject})
        if available < need:
            self.last_reject = f"insufficient_funds:avail={available:.0f}:need={need:.0f}"
            return self._append({"event": "live_skip", "ts": now.isoformat(), "day": self.day, "live": True,
                                 "why": self.last_reject})
        start = {"event": "live_start", "ts": now.isoformat(), "day": self.day, "live": True, "atm": strike,
                 "strike": strike, "legs": [leg], "tag_prefix": TAG_PREFIX, "qty": self.qty, "expiry_day": False,
                 "ce_symbol": symbol if leg == "ce" else "", "pe_symbol": symbol if leg == "pe" else "",
                 "reason": reason}
        if self._append(dict(start)) is None:  # no durable record: no real orders
            self.last_reject = "ledger_write_failed"
            return None
        self.entries += 1
        self.live_meta = start
        self.live = LiveStraddle(day=self.day, strike=strike, ce_symbol=start["ce_symbol"], pe_symbol=start["pe_symbol"],
                                 qty=self.qty, expiry_day=False, log=self._live_log, legs=(leg,), tag_prefix=TAG_PREFIX)
        if not self.live.place(self.order_client, leg, "BUY", self.qty):
            # The order intent could not be written, so nothing was sent: drop the trade and give the slot back.
            self.entries -= 1
            self.live, self.live_meta = None, {}
            self._append({"event": "live_abort", "ts": now.isoformat(), "day": self.day, "live": True,
                          "why": "ledger_write_failed"})
            self.last_reject = "ledger_write_failed"
            self._log.warning("LIVE llm_buyer buy NOT sent (ledger write failed) %s", symbol)
            return None
        self.last_reject = None
        self._log.warning("LIVE llm_buyer bought %s x%d (%s)", symbol, self.qty, reason)
        return start

    def _exit(self, now: datetime, book: QuoteSource, why: str) -> dict[str, Any] | None:
        if self.live is not None:
            self.live.request_exit(why)
            return None
        pos = self.position
        if pos is None:
            return None
        px = self._sell_px(book, pos.symbol)
        if px is None:
            self.last_reject = "no_quote_for_exit"
            return None
        charges = round(float(kite_nfo_charges([(pos.fill, pos.qty, "buy"), (px, pos.qty, "sell")])["total"]), 2)
        gross = round((px - pos.fill) * pos.qty, 2)
        ev = self._append({"event": "close", "ts": now.isoformat(), "day": pos.day, "leg": pos.leg, "symbol": pos.symbol,
                           "strike": pos.strike, "qty": pos.qty, "entry": pos.fill, "exit": round(px, 2),
                           "pnl_gross": gross, "charges": charges, "pnl": round(gross - charges, 2), "reason": why})
        if ev is None:
            return None
        self.position = None
        self.day_realized = round(self.day_realized + ev["pnl"], 2)
        return ev

    # ---- every frame ----
    def on_frame(self, *, now: datetime, feed: dict[str, Any], book: QuoteSource) -> dict[str, Any] | None:
        today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        self._new_day(today)
        self._kill_switch(now, today)
        if feed and not feed.get("error") and "09:15" <= hm <= "15:30":
            self._record(hm, feed)
        if self.pending_close is not None:
            return self._write_pending_close()
        if self.live is not None:
            return self._live_step(now, book)
        if self.position is None:
            return None
        if self.position.day != today:  # never priced at another day's quotes
            ev = self._append({"event": "close", "ts": now.isoformat(), "day": self.position.day, "leg": self.position.leg,
                               "symbol": self.position.symbol, "qty": self.position.qty, "entry": self.position.fill,
                               "exit": None, "pnl": None, "pnl_known": False, "reason": "missed_square_off"})
            self.position = None
            return ev
        if hm >= EXIT_AT:
            return self._exit(now, book, "square_off")
        pnl = self.day_pnl(book)
        if pnl is not None and pnl <= -DAY_LOSS_LIMIT:
            self._stop_entries(now, "day_loss")
            return self._exit(now, book, "day_loss_stop")
        return None

    def _kill_switch(self, now: datetime, today: str) -> None:
        """Box unticked after being on today (or found off with a live trade of today open): sell, stop for the day."""
        if self.live_flag is None:
            return
        try:
            on = bool(self.live_flag.is_enabled())
            source = str((self.live_flag.snapshot() or {}).get("source") or "")
        except Exception:  # noqa: BLE001 - an unreadable switch never sells anything by itself
            return
        if on:
            self.live_on_day = today
            return
        if source == "day_end":  # the engine's 15:30 auto-off is not an operator stop
            return
        armed = self.live_on_day == today or (self.live is not None and self.live.day == today)
        if not armed:
            return
        if self.stopped != "ui_off":
            self.stopped = "ui_off"
            self._append({"event": "stop_entries", "ts": now.isoformat(), "day": self.day, "why": "ui_off"})
            self._log.warning("llm_buyer: live box unticked -> selling any live position, no more trades today")
        if self.live is not None and self.live.phase in ("entering", "holding"):
            self.live.request_exit("ui_off")

    def _live_step(self, now: datetime, book: QuoteSource) -> dict[str, Any] | None:
        ls = self.live
        assert ls is not None
        leg = ls.legs[0]
        avg, q = ls.cost(leg)
        stop = None
        if avg > 0 and q > 0:  # the day-loss budget left, as a fraction of what the open lot cost
            budget = DAY_LOSS_LIMIT + self.day_realized - EST_CHARGES
            # Never 0: the live layer reads 0 as "no stop". A spent budget means sell at the first lower bid.
            stop = max(1e-6, min(1.0, budget / (avg * q)))
        before = ls.phase
        result = ls.step(self.order_client, bids={leg: quote_bid(book.get(ls.symbol(leg)))},
                         hm=now.strftime("%H:%M"), today=now.strftime("%Y-%m-%d"), stop_loss=stop,
                         exit_at=EXIT_AT, square_off_late=LIVE_ORDERS_UNTIL)
        if before == "entering" and ls.phase in ("holding", "exiting"):
            avg, q = ls.cost(leg)  # the fills this frame's order-book read just brought in
            if q:
                self._append({"event": "open", "ts": now.isoformat(), "day": ls.day, "leg": leg, "symbol": ls.symbol(leg),
                              "strike": ls.strike, "qty": q, "fill": round(avg, 2), "entry": round(avg, 2),
                              "side": "long", "live": True, "reason": self.live_meta.get("reason")})
        if result is None:
            if ls.manual_needed:
                self.last_reject = "manual_needed"
            return None
        if result["reason"] == "stop":
            self._stop_entries(now, "day_loss")
        self.pending_close = {"event": "close", "ts": now.isoformat(), "day": ls.day, "leg": leg, "symbol": ls.symbol(leg),
                              "strike": ls.strike, "qty": ls.qty, "entry": result["debit"], "exit": result["exit"],
                              "pnl_gross": result["pnl_gross"], "charges": result["charges"], "pnl": result["pnl"],
                              "pnl_known": not result["estimated"], "pnl_estimated": result["estimated"],
                              "reason": "day_loss_stop" if result["reason"] == "stop" else result["reason"], "live": True}
        return self._write_pending_close()

    def _write_pending_close(self) -> dict[str, Any] | None:
        ev = self._append(dict(self.pending_close or {}))
        if ev is None:  # keep trying: the day and month totals move only when the line is on disk
            self.last_reject = "ledger_write_failed_close_pending"
            return None
        self.pending_close, self.live, self.live_meta = None, None, {}
        self.day_realized = round(self.day_realized + float(ev["pnl"]), 2)
        m = str(ev["day"])[:7]
        self.month_live_pnl[m] = round(self.month_live_pnl.get(m, 0.0) + float(ev["pnl"]), 2)
        return ev

    # ---- dashboard ----
    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        pos = None
        if self.live is not None:
            leg = self.live.legs[0]
            avg, q = self.live.cost(leg)
            pos = {"option": leg.upper(), "symbol": self.live.symbol(leg), "qty": q, "entry": round(avg, 2),
                   "live": True, "phase": self.live.phase, "manual_needed": self.live.manual_needed}
        elif self.position is not None:
            pos = {"option": self.position.leg.upper(), "symbol": self.position.symbol, "qty": self.position.qty,
                   "entry": self.position.fill, "live": False}
        if pos is not None and book is not None:
            o = self._open_pnl(book)
            pos["open_pnl"] = round(o) if o is not None else None
        return {
            "ok": True, "mode": "paper", "enabled": True, "book": self.strategy, "name": "LLM option buyer",
            "live_orders": self.live is not None, "live": self.live_flag.snapshot() if self.live_flag else None,
            "day": self.day, "entries": self.entries, "max_entries": MAX_ENTRIES, "day_realized": round(self.day_realized),
            "day_loss_limit": DAY_LOSS_LIMIT, "stopped": self.stopped or None, "position": pos,
            "last_decision": self.last_decision, "last_reject": self.last_reject,
            "month_live_pnl": self.month_live_pnl.get(datetime.now(IST).strftime("%Y-%m"), 0.0),
        }
