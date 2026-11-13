"""LLM option buyer: one lot of the ATM NIFTY call OR put, side and timing chosen by an LLM. Paper by default.

Every minute from 09:16 the LLM sees what a discretionary trader would see at that minute (new trades until
15:00; a trade still open is managed every minute until it closes, at the latest 15:08) (NIFTY
path and range, % change, VIX and its change, ATM IV against today's high/low, IV percentile, PCR, futures OI
vs day high, ATM call/put prices and the straddle path, days to expiry, its own position and P&L) and answers
BUY_CE / BUY_PE / WAIT / DONE_FOR_DAY when flat, HOLD / EXIT when holding. It also sees the previous session's
open, high, low and close (from the engine's 1-minute bars). Evidence is thin: the 8 Nov 2026 replays (gpt-4.1,
dates hidden) were 5-minute, full-day, on four hand-picked days (+Rs 6,581 / -Rs 1,296 / +Rs 3,449 / -Rs 2,444);
this all-day, 1-minute setup has not been replayed.

Code, not the LLM, owns the risk:
- Day loss stop DAY_LOSS_LIMIT: today's closed P&L plus the open position at bids, after estimated charges,
  checked every frame. Hit -> exit at once and no more entries today.
- At most MAX_ENTRIES a day (a book may allow more while the day's closed P&L is positive, with a profit lock:
  an extra trade is sold once the day's P&L falls to PROFIT_LOCK of its best closed level); no new entry after
  the book's last entry time; everything is sold at its exit time.
- An LLM error, timeout or unusable answer (not an allowed action) is treated as WAIT / HOLD;
  MAX_ERRORS_HOLDING of them in a row while holding sell the position ("llm_unavailable"). A decision that no longer fits the position (a buy while holding, an
  exit while flat) is ignored.

Live (intraday MIS, 1 lot): decided at each entry, on a day the dashboard checkbox "Live LLM buyer" is ticked
and the host allows it (ATLAS_LITE_LLM_BUYER_LIVE_ALLOWED=1). Before buying, the option's ask x qty plus 10%
must fit the funds Kite shows available, and live stops for the month at MONTH_LOSS_LIMIT. Orders are run by
atlas_lite/live_straddle.py with a single leg (tagged orders, holdings from this book's own fills).
Unticking the box (after it was on today) sells an open live position at once (reason "ui_off") and the book
carries on in paper for the rest of the day (operator's choice, 12 Nov 2026; until then it stopped the book). The
15:30 auto-off (source "day_end") does nothing. No order is sent after LIVE_ORDERS_UNTIL (Zerodha squares MIS off itself at 15:14). A live trade still open from a ticked
day is also sold if the box is found off after a restart.

Paper fills: buy at ask, sell at bid when quotes have them, else last price +/- SLIP_PTS. Kite charges.
Ledger: paper_llm_buyer.jsonl (every decision with its reason, opens, closes, stops, live order events).
"""

from __future__ import annotations

import json
import os
import time
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
PAPER_FLAG_NAME = "llm_buyer_paper.json"  # the "Paper" box: off = no LLM calls, no trades
LIVE_ENV = "ATLAS_LITE_LLM_BUYER_LIVE_ALLOWED"
DEFAULT_LOT_SIZE = 65
# All day, one decision a minute (operator's choice, 8 Nov 2026): new trades 09:16-15:00 (14:30 until 11 Nov 2026;
# moved at the operator's call after 11 Nov's 14:44-15:08 fall came after the cutoff); an open trade is managed every
# minute until it closes, at the latest at EXIT_AT, with sell retries until LIVE_ORDERS_UNTIL, before Zerodha's own MIS
# square-off at 15:14. The operator stops it any time by unticking the live box (sells and stops for the day).
FIRST_DECISION, LAST_ENTRY, LAST_DECISION = "09:16", "15:00", "15:07"  # an open trade is asked until the 15:08 exit
EXIT_AT, LIVE_ORDERS_UNTIL = "15:08", "15:12"  # Zerodha squares F&O MIS off at 15:14
DECIDE_EVERY_MIN = 1
MAX_ENTRIES = 2
MAX_ENTRIES_IF_POSITIVE = 4  # more entries while today's closed P&L is positive (profit lock applies)
MAX_ERRORS_HOLDING = 5  # LLM errors in a row while holding -> sell (~5 minutes without a working LLM at 1/min)
DAY_LOSS_LIMIT = 1_500.0  # was 2,000 until 11 Nov 2026 (operator's call with prompt v3)
PROFIT_LOCK = 0.5  # extra trades on a winning day: keep at least half of the day's best closed P&L
MONTH_LOSS_LIMIT = 10_000.0
EST_CHARGES = 80.0  # a buy and a sell of one ATM lot, for the running day-loss check
SLIP_PTS = 1.0
FUNDS_HEADROOM = 1.10
TAG_PREFIX = "llm"
# The trader: Claude Sonnet 5.5 via OpenRouter, reasoning effort low (operator's choice for live from 11 Nov 2026). The
# 9 Nov replays (Opus +Rs 1,853, Sonnet +Rs 326) had a one-minute look-ahead in their fills; at the real ask/bid both
# lost (Opus -Rs 2,835, Sonnet -Rs 769; GPT-4.1 live -Rs 747). No OpenAI fallback: OpenAI can't serve this model and a
# different model is a different trader; if OpenRouter fails the book waits / holds (and sells after
# MAX_ERRORS_HOLDING failures while holding).
MODEL_OPENROUTER = "anthropic/claude-sonnet-5.5"
MODEL_OPENAI: str | None = None
LLM_TIMEOUT_S = 25.0  # a whole call, start to answer (OpenRouter's keep-alive padding defeats httpx's per-read timeout)
LLM_MAX_TOKENS = 600
LIVE_EVENTS = frozenset({"order_intent", "order_sent", "order_send_error", "order_cancel_sent", "order_final",
                         "live_phase", "manual_needed", "live_mark"})

SYSTEM_PROMPT_V2 = """You are an experienced discretionary NIFTY options trader who BUYS options to trade direction. The only
positions you may take: buy one lot (65) of the at-the-money CALL (if you expect NIFTY to rise) or the at-the-money
PUT (if you expect it to fall). You decide when to buy and which side, whether to skip the day, and when to exit.
Time decay and falling IV work against an option buyer, so the move must come soon and be big enough.
Round-trip costs are about Rs 60-80 per option plus roughly 1 point of slippage. entries_left_today shows how many
new trades you may still take. New trades only until {last_entry}; an open trade may be held until {close} at the
latest. Judge each setup on its own merits, like a careful professional: you are not obliged to trade. You see only
information available at the current minute.
How to read the data: nifty_last_15_1m_bars are the latest one-minute candles ("HH:MM open/high/low/close").
opening_range is the 09:15-09:29 high and low. atm_straddle_now (ATM call + put) is roughly the move the market
prices until expiry; on expiry day that is the move expected for the rest of the day. move_5m_pts and move_15m_pts
are NIFTY's change over the last 5 and 15 minutes.
Before buying, check that the move is happening now (recent candles), that price is still at or beyond the level it
is breaking rather than already reversing, and that the move still to come can beat time decay. Common traps:
buying a breakout after it has already reversed, chasing a spike after a big move, and expiry-day chop with falling
IV. When holding, exit once the reason for the trade is gone.
Reply with JSON only: {"action": "...", "reason": "<one short sentence>"}
When flat the action is one of BUY_CE, BUY_PE, WAIT, DONE_FOR_DAY. When holding: HOLD, EXIT."""
# v3 (written 11 Nov 2026): v2 waited for "a clean break" before a level broke and called it "chasing" after,
# so it never had an entry window (11 Nov: watched a 230-pt fall from 11:40 to 13:53), and it exited at the first flat
# minute. v3 defines the entry window and exits on structure. Generic rules only, one round threshold fixed before any
# result; not re-tuned on 9 and 11 Nov (where the problem was found). Live from 12 Nov at the operator's call.
SYSTEM_PROMPT_V3 = """You are an experienced discretionary NIFTY options trader who BUYS options to trade direction. The only
positions you may take: buy one lot (65) of the at-the-money CALL (if you expect NIFTY to rise) or the at-the-money
PUT (if you expect it to fall). You decide when to buy and which side, whether to skip the day, and when to exit.
Round-trip costs are about Rs 60-80 per option plus roughly 1 point of slippage. entries_left_today, when present,
shows how many new trades you may still take.
New trades only until {last_entry}; an open trade may be held until {close} at the latest. You see only information
available at the current minute.
How to read the data: nifty_last_15_1m_bars are the latest one-minute candles ("HH:MM open/high/low/close").
opening_range is the 09:15-09:29 high and low, shown once complete (from 09:30). move_5m_pts and move_15m_pts are
NIFTY's change over the last 5 and 15 minutes. atm_straddle_now and IV describe how expensive options are; they are
context, not a reason on their own to stay out (they are always high enough days before expiry).
minutes_to_forced_exit is how long a new trade could still run before the code sells it.
Entries: a setup is a one-minute candle closing beyond a key level (the opening-range high or low, VWAP, or the day's
high or low before that candle: high_before_last_candle / low_before_last_candle) with move_5m_pts pointing the same way: buy the CALL on an upside break, the PUT on a downside break.
The first pullback after such a break that holds the broken level is also a setup. A break is "chasing" only when
price is already more than 0.25% of NIFTY beyond the broken level; then wait for a pullback instead of skipping the
move. Most breaks fail and cost a little, while the ones that run pay several times that, so take clean breaks and
cut failed ones; do not skip a break because an earlier one failed. Inside a range with no break, WAIT.
Exits: the level you traded is position.entry_level (fixed when you bought; position.entry_reason has your reason).
Hold while price stays on the breakout side of it; a flat or slightly red minute is not a reason to exit. EXIT when a
one-minute candle closes back through that level, or when the move has clearly reversed.
After a failed break you may trade the next clean break in either direction.
Reply with JSON only: {"action": "...", "reason": "<one short sentence>"}
When flat the action is one of BUY_CE, BUY_PE, WAIT, DONE_FOR_DAY. When holding: HOLD, EXIT."""
# v4 "freehand" (13 Nov 2026, operator's call): no trading rules and no caution bias, only the data, the goal and the
# costs. Runs as a second paper book (llm_buyer_free) beside v3 on the same live data, to see which approach earns more.
SYSTEM_PROMPT_V4 = """You are a NIFTY options trader. Each minute you may buy one lot (65) of the at-the-money CALL or
PUT, wait, or exit what you hold. You see what a trader sees at this minute: NIFTY's candles and levels, VWAP, the
option prices, IV, VIX, PCR and your own position. New trades only until {last_entry}; an open trade may be held until
{close} at the latest, when it is sold for you. Round-trip costs are about Rs 60-80 per option plus roughly 1 point of
slippage. You are judged only on net profit at real bid/ask after costs over many days. Small losses on ideas that do
not work are normal; profits come from the moves you catch and hold. Decide entirely on your own judgement: whether
to trade, when, which side, how long to hold and when to get out.
Reply with JSON only: {"action": "...", "reason": "<one short sentence>"}
When flat the action is one of BUY_CE, BUY_PE, WAIT, DONE_FOR_DAY. When holding: HOLD, EXIT."""
PROMPTS = {"v2": SYSTEM_PROMPT_V2, "v3": SYSTEM_PROMPT_V3, "v4": SYSTEM_PROMPT_V4}
FREE_STRATEGY, FREE_LEDGER_NAME, FREE_PROMPT_VERSION = "llm_buyer_free", "paper_llm_buyer_free.jsonl", "v4"
PROMPT_VERSION = "v3"  # live from 12 Nov 2026 at the operator's call, without a replay test (v2 kept for comparison)


def render_prompt(version: str) -> str:
    return PROMPTS[version].replace("{close}", EXIT_AT).replace("{last_entry}", LAST_ENTRY)


SYSTEM_PROMPT_TEMPLATE = PROMPTS[PROMPT_VERSION]
SYSTEM_PROMPT = render_prompt(PROMPT_VERSION)


def system_prompt(close: str, last_entry: str = LAST_ENTRY) -> str:
    return SYSTEM_PROMPT_TEMPLATE.replace("{close}", close).replace("{last_entry}", last_entry)


def prev_day_ohlc(bars: list[dict[str, Any]], today: str) -> dict[str, float] | None:
    """The last trading session before ``today`` from 1-minute bars ({"t": "YYYY-MM-DD HH:MM", o/h/l/c}): its open,
    high, low and close. Holidays and weekends are skipped, and so is a flat session (high == low): the engine keeps
    building bars from the last price on a holiday (10 Nov 2026 gave open = high = low = close). None if no session."""
    days = sorted({str(b.get("t") or "")[:10] for b in bars if str(b.get("t") or "")[:10] < today}, reverse=True)
    for d in days:
        try:
            if not _is_nse_trading_day(date.fromisoformat(d)):
                continue
            day = [b for b in bars if str(b.get("t") or "")[:10] == d]
            hi, lo = max(float(b["h"]) for b in day), min(float(b["l"]) for b in day)
            if hi <= lo:
                continue
            return {"date": d, "open": float(day[0]["o"]), "high": hi, "low": lo, "close": float(day[-1]["c"])}
        except (KeyError, TypeError, ValueError):
            continue
    return None


def paper_llm_buyer_enabled() -> bool:
    return os.environ.get("ATLAS_LITE_PAPER_LLM_BUYER", "1").strip().lower() in ("1", "true", "yes")


class QuoteSource(Protocol):
    def get(self, symbol: str) -> dict[str, Any] | None: ...


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def post_with_deadline(url: str, *, json: dict[str, Any], timeout: float, headers: dict[str, str],
                       transport: httpx.BaseTransport | None = None) -> httpx.Response:
    """POST that never takes longer than `timeout` in total. httpx's timeout is per read, and OpenRouter keeps a slow
    call alive with padding bytes, so a call could run for minutes (686 s in a 9 Nov replay). The budget is split: any
    silence longer than `gap` (40%) ends the call, and once `timeout - gap` has passed the next chunk ends it. So the
    worst case is (timeout - gap) + gap = timeout, and the connection is closed: nothing is left running. OpenRouter
    sends its headers in 1-3 s (measured 10 Nov), so the first wait is short too."""
    gap = timeout * 0.4
    deadline = time.monotonic() + timeout - gap
    with httpx.Client(timeout=httpx.Timeout(gap), transport=transport) as client:
        with client.stream("POST", url, json=json, headers=headers) as r:
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if time.monotonic() > deadline:
                    raise TimeoutError(f"no answer within {timeout:.0f}s")
            return httpx.Response(r.status_code, content=bytes(body), request=r.request)


class LlmDecider:
    """One chat call: OpenRouter, then the OpenAI key only if MODEL_OPENAI is set (None now). Never raises."""

    def __init__(self, credentials: Any, *, post: Callable[..., Any] | None = None, prompt: str = "") -> None:
        self.credentials = credentials
        self._post = post or post_with_deadline
        self.prompt = prompt or SYSTEM_PROMPT

    def targets(self) -> list[tuple[str, str, str, str]]:
        c = self.credentials
        out = []
        if c is not None and getattr(c, "api_key", "") and "openrouter" in str(getattr(c, "base_url", "")):
            out.append(("openrouter", c.api_key, c.base_url, MODEL_OPENROUTER))
        if MODEL_OPENAI and c is not None and getattr(c, "openai_api_key", ""):
            out.append(("openai", c.openai_api_key, c.openai_base_url or "https://api.openai.com/v1", MODEL_OPENAI))
        return out

    def decide(self, state: dict[str, Any]) -> dict[str, Any]:
        errors = []
        for name, key, base, model in self.targets():
            body = {"model": model, "temperature": 0.1, "max_tokens": LLM_MAX_TOKENS,
                    "messages": [{"role": "system", "content": self.prompt},
                                 {"role": "user", "content": json.dumps(state, separators=(",", ":"))}]}
            if model.startswith("anthropic/"):
                # Claude models always reason on OpenRouter; at default effort the thinking could use the whole
                # token budget and return no answer. Low effort answers directly.
                body["reasoning"] = {"effort": "low"}
            if name == "openrouter":
                body["provider"] = {"sort": "latency"}  # fastest host of the same model; OpenRouter still falls back
            try:
                r = self._post(f"{base.rstrip('/')}/chat/completions", json=body, timeout=LLM_TIMEOUT_S,
                               headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                        "HTTP-Referer": "https://atlas-lite.local", "X-Title": "Atlas Lite LLM buyer"})
                r.raise_for_status()
                txt = str(r.json()["choices"][0]["message"]["content"] or "")
                out = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
                # Factors are not requested (asking for them changed decisions in a test); kept only if offered.
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
    entry_level: float | None = None  # the price level the trade broke (code-picked at entry); the exit refers to it
    entry_level_kind: str = ""


@dataclass
class LlmBuyer:
    path: Path
    lot_size: int = DEFAULT_LOT_SIZE
    order_client: Any = None
    live_flag: Any = None
    paper_flag: Any = None  # OnOffFlag; None = always on
    decider: Any = None
    strategy: str = STRATEGY
    # Timing (defaults: all day, one decision a minute); fields so a window can be set per book.
    first_decision: str = FIRST_DECISION
    last_entry: str = LAST_ENTRY
    last_decision_at: str = LAST_DECISION
    exit_at: str = EXIT_AT
    live_orders_until: str = LIVE_ORDERS_UNTIL
    decide_every_min: int = DECIDE_EVERY_MIN
    path_every_min: int = 5  # NIFTY path sample in the LLM's view
    straddle_every_min: int = 5  # straddle path sample
    tag_prefix: str = TAG_PREFIX
    name: str = "LLM buyer · every minute"
    max_entries: int = MAX_ENTRIES
    # Entries allowed beyond max_entries while today's closed P&L is positive (the hour book: up to 4 in all). An
    # extra trade is sold, and entries end, once the day's P&L falls to PROFIT_LOCK of its best closed level.
    max_entries_if_positive: int = MAX_ENTRIES_IF_POSITIVE
    # The day limits (entries, day stop, profit lock) count LIVE trades only; paper trades have none (operator's
    # choice, 12 Nov 2026): paper keeps recording all day, and paper results never use up live's budget.
    day_peak: float = 0.0  # best closed live P&L today
    day: str = ""
    entries: int = 0  # live entries today
    day_realized: float = 0.0  # closed live P&L today
    paper_entries: int = 0
    paper_realized: float = 0.0
    stopped: str = ""  # why entries are over for today ("day_loss", "profit_lock" end live; "done_for_day" ends both)
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
    live_on_day: str = ""  # last day the live box was seen ticked (the untick sell arms on it)
    live_off_day: str = ""  # last day an untick was handled (logged once)
    last_feed_at: str = ""  # minute of the last feed recorded today
    prev_day: dict[str, float] | None = None  # yesterday's open/high/low/close (see ensure_prev_day)
    prev_day_for: str = ""
    prev_day_tried: str = ""
    errors_in_row: int = 0  # LLM answers in a row that failed (reset by any valid answer)
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.llm_buyer"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._restore()

    @property
    def qty(self) -> int:
        return int(self.lot_size)

    def paper_enabled(self) -> bool:
        if self.paper_flag is None:
            return True
        try:
            return bool(self.paper_flag.is_enabled())
        except Exception:  # noqa: BLE001 - an unreadable switch keeps the book as it was (on)
            return True

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
                    self.paper_entries += 1
                    self.position = Position(day=d, leg=ev["leg"], symbol=ev["symbol"], strike=int(ev.get("strike") or 0),
                                             qty=int(ev["qty"]), fill=float(ev["fill"]), opened_at=str(ev.get("ts")),
                                             reason=str(ev.get("reason") or ""), entry_level=_f(ev.get("entry_level")),
                                             entry_level_kind=str(ev.get("entry_level_kind") or ""))
            elif kind == "close":
                if ev.get("live"):
                    self.day_realized = round(self.day_realized + float(ev.get("pnl") or 0.0), 2)
                    self.day_peak = max(self.day_peak, self.day_realized)
                else:
                    self.paper_realized = round(self.paper_realized + float(ev.get("pnl") or 0.0), 2)
                    self.position = None
            elif kind == "live_off" and d == today:
                self.live_off_day = d  # already handled today: a restart does not log it again
            elif kind == "stop_entries" and ev.get("why") == "ui_off":
                pass  # an untick before 12 Nov 2026 stopped the book; now it only ends live trading (paper goes on)
            elif kind == "stop_entries":
                self.stopped = str(ev.get("why") or "stopped")
        if live_start is not None:  # a live trade without its close line: finish it on the broker
            self.live_meta = dict(live_start)
            self.live = LiveStraddle.restore(live_start, live_events, self._live_log)

    # ---- day bookkeeping ----
    def _new_day(self, today: str) -> None:
        if self.day != today:
            self.day, self.entries, self.day_realized, self.stopped, self.day_peak = today, 0, 0.0, "", 0.0
            self.paper_entries, self.paper_realized = 0, 0.0
            self.decided, self.history, self.last_decision = set(), {}, None
            self.last_feed, self.last_feed_at = {}, ""  # never trade on yesterday's ATM symbols

    def ensure_prev_day(self, today: str, hm: str, bars: Callable[[], list[dict[str, Any]]]) -> dict[str, float] | None:
        """Yesterday's OHLC for today's view: looked up once a minute until the bars have it, then kept for the day
        (bars can still be loading at the first frames after a restart)."""
        if self.prev_day_for != today:
            self.prev_day, self.prev_day_for, self.prev_day_tried = None, today, ""
        if self.prev_day is None and self.prev_day_tried != hm:
            self.prev_day_tried = hm
            self.prev_day = prev_day_ohlc(bars(), today)
        return self.prev_day

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
        if not self.paper_enabled():
            return False
        if not _is_nse_trading_day(now.date()) or not (self.first_decision <= hm <= self.last_decision_at):
            return False
        if now.minute % self.decide_every_min or hm in self.decided or self.day != now.strftime("%Y-%m-%d"):
            return False
        holding = self.position is not None or self.live is not None
        if holding:
            return self.live is None or self.live.phase == "holding"
        return self._entry_open() and hm <= self.last_entry and self._feed_fresh(now)

    def _entry_open(self) -> bool:
        """May a new trade start? Paper: always (until the LLM says DONE_FOR_DAY). Live: the day limits on live trades."""
        if self.stopped == "done_for_day":
            return False
        if not self.live_enabled():
            return True
        return not self.stopped and self._can_enter() and self.day_realized - EST_CHARGES > -DAY_LOSS_LIMIT

    def _entries_left(self) -> int | None:
        """Live: entries still allowed today. Paper: None (no limit; left out of the LLM's view)."""
        if not self.live_enabled():
            return None
        cap = self.max_entries_if_positive if self.day_realized > 0 else self.max_entries
        return max(0, cap - self.entries) if not self.stopped else 0

    @staticmethod
    def _today_bars(f: dict[str, Any]) -> list[dict[str, Any]]:
        return [b for b in (f.get("bars_today") or []) if isinstance(b, dict) and b.get("c") is not None]

    @staticmethod
    def _day_range(bars: list[dict[str, Any]], spots: list[float]) -> tuple[float, float]:
        """The day's high and low from the candles' highs/lows and the per-minute spots, so every field agrees."""
        return (max([float(b["h"]) for b in bars if b.get("h") is not None] + spots),
                min([float(b["l"]) for b in bars if b.get("l") is not None] + spots))

    @staticmethod
    def _price_action(f: dict[str, Any], spots: list[float], now: datetime) -> dict[str, Any]:
        """Chart-reader inputs from today's 1-minute bars (feed "bars_today": [{"t", o/h/l/c[, v]}]): the last 15
        candles, the opening range, VWAP when volume exists, recent moves, distance to the day's extremes, the ATM
        straddle and minutes to the close. Fields that can't be computed are left out."""
        out: dict[str, Any] = {}
        bars = LlmBuyer._today_bars(f)
        if bars:
            out["nifty_last_15_1m_bars"] = [
                f"{str(b['t'])[11:16]} {float(b['o']):.0f}/{float(b['h']):.0f}/{float(b['l']):.0f}/{float(b['c']):.0f}"
                for b in bars[-15:]]
            orb = [b for b in bars if "09:15" <= str(b["t"])[11:16] <= "09:29"]
            # Only once complete (the 09:29 candle closed): a still-forming range moved after a 12 Nov 09:27 entry
            # (25,313.8 -> 25,342) and the exit then used the moved level.
            if orb and any(str(b["t"])[11:16] >= "09:29" for b in bars):
                out["opening_range"] = {"high": round(max(float(b["h"]) for b in orb), 1),
                                        "low": round(min(float(b["l"]) for b in orb), 1)}
            vol = sum(float(b.get("v") or 0) for b in bars)
            if vol > 0:
                vwap = sum((float(b["h"]) + float(b["l"]) + float(b["c"])) / 3 * float(b.get("v") or 0) for b in bars) / vol
                out["vwap"] = round(vwap, 1)
                if spots:
                    out["nifty_vs_vwap_pts"] = round(spots[-1] - vwap, 1)
            closes = [float(b["c"]) for b in bars]
            if len(closes) > 5:
                out["move_5m_pts"] = round(closes[-1] - closes[-6], 1)
            if len(closes) > 15:
                out["move_15m_pts"] = round(closes[-1] - closes[-16], 1)
            if len(bars) > 1:  # the levels the last candle may have broken (day_high/low already include it)
                out["high_before_last_candle"] = round(max(float(b["h"]) for b in bars[:-1]), 1)
                out["low_before_last_candle"] = round(min(float(b["l"]) for b in bars[:-1]), 1)
        if spots:
            hi, lo = LlmBuyer._day_range(bars, spots)
            out["pts_below_day_high"] = round(hi - spots[-1], 1)
            out["pts_above_day_low"] = round(spots[-1] - lo, 1)
        ce, pe = _f(f.get("ce")), _f(f.get("pe"))
        if ce and pe:
            out["atm_straddle_now"] = round(ce + pe, 1)
        exit_m = int(EXIT_AT[:2]) * 60 + int(EXIT_AT[3:])
        out["minutes_to_forced_exit"] = max(0, exit_m - (now.hour * 60 + now.minute))  # how long a trade can still run
        return out

    def _can_enter(self) -> bool:
        if self.entries < self.max_entries:
            return True
        return self.entries < self.max_entries_if_positive and self.day_realized > 0

    def _in_extra_trade(self) -> bool:
        return self.entries > self.max_entries and self.live is not None

    def _profit_floor(self) -> float | None:
        """Day P&L an extra trade may not fall below: PROFIT_LOCK of today's best closed P&L."""
        return self.day_peak * PROFIT_LOCK if self._in_extra_trade() and self.day_peak > 0 else None

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
        day_hi, day_lo = self._day_range(self._today_bars(f), spots) if spots else (None, None)
        exp = str(f.get("expiry") or "")[:10]
        try:
            dte = (date.fromisoformat(exp) - now.date()).days if exp else None
        except ValueError:
            dte = None
        s: dict[str, Any] = {
            "time": hm, "weekday": now.strftime("%A"), "calendar_days_to_expiry": dte,
            "nifty": round(spots[-1], 1) if spots else _f(f.get("spot")),
            "nifty_change_vs_prev_close_pct": f.get("index_nifty_chg"),
            **({f"prev_day_{k}": round(v, 1) for k, v in pd.items() if k != "date"}
               if isinstance(pd := f.get("prev_day"), dict) else {}),
            "day_open": round(spots[0], 1) if spots else None,
            "day_high": round(day_hi, 1) if spots else None, "day_low": round(day_lo, 1) if spots else None,
            "nifty_path": [f"{m} {self.history[m]['spot']:.0f}" for m in mins if int(m[3:]) % self.path_every_min == 0 or m == mins[-1]][-14:],
            "vix": f.get("vix"), "vix_change_since_open_pts": f.get("vix_chg"),
            "atm_iv": f.get("iv"), "atm_iv_day_high": f.get("iv_day_high"), "atm_iv_day_low": f.get("iv_day_low"),
            "iv_percentile_1y": f.get("ivp"), "pcr": f.get("pcr"), "futures_oi_vs_day_high_pct": f.get("oi_vs_day_high"),
            "atm_strike": f.get("atm"), "atm_ce": f.get("ce"), "atm_pe": f.get("pe"),
            "atm_straddle_path_since_open": [f"{m} {self.history[m]['straddle']:.1f}" for m in mins
                                             if "straddle" in self.history[m] and (int(m[3:]) % self.straddle_every_min == 0 or m == mins[-1])][-14:],
            **({"entries_left_today": left} if (left := self._entries_left()) is not None else {}),
            **self._price_action(f, spots, now),
        }
        if self.position is not None or self.live is not None:
            # While holding, the levels that keep moving are left out: the exit refers to position.entry_level, fixed at
            # entry (a moving level is what made 12 Nov's 09:48 exit use the wrong number).
            s.pop("high_before_last_candle", None)
            s.pop("low_before_last_candle", None)
            if self.live is not None:
                leg = self.live.legs[0]
                sym, fill = self.live.symbol(leg), self.live.cost(leg)[0]
                opened = str(self.live_meta.get("ts") or "")[11:16]
            else:
                leg, sym, fill, opened = self.position.leg, self.position.symbol, self.position.fill, self.position.opened_at[11:16]
            now_px = quote_bid(book.get(sym)) or quote_ltp(book.get(sym))
            o = self._open_pnl(book)
            why = self.live_meta.get("reason") if self.live else self.position.reason
            lvl = self.live_meta.get("entry_level") if self.live else self.position.entry_level
            kind = self.live_meta.get("entry_level_kind") if self.live else self.position.entry_level_kind
            s["position"] = {"option": leg.upper(), "strike": self.live_meta.get("strike") if self.live else self.position.strike,
                             "entered_at": opened, "bought_at": round(fill, 2), "now": now_px,
                             "open_pnl_rs": round(o) if o is not None else None,
                             "entry_reason": str(why or "")[:300],
                             "entry_level": round(float(lvl), 1) if lvl is not None else None,  # fixed at entry
                             "entry_level_kind": kind or None}
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
        logged = self._append({"event": "decision", "ts": now.isoformat(), "day": self.day, "hm": hm, "action": act or "ERROR",
                      "reason": out.get("reason"), "factors": out.get("factors") or [], "error": out.get("error"),
                      "provider": out.get("provider"), "model": out.get("model"), "holding": holding,
                      "valid": act in (("HOLD", "EXIT") if holding else ("BUY_CE", "BUY_PE", "WAIT", "DONE_FOR_DAY")),
                      "inputs": out.get("inputs") or {}})
        valid = act in (("HOLD", "EXIT") if holding else ("BUY_CE", "BUY_PE", "WAIT", "DONE_FOR_DAY"))
        self.errors_in_row = 0 if valid else self.errors_in_row + 1  # an unusable answer counts as an error
        if holding and not valid and self.errors_in_row >= MAX_ERRORS_HOLDING:
            self._log.warning("llm_buyer: %d LLM errors in a row while holding -> selling", self.errors_in_row)
            return self._exit(now, book, "llm_unavailable")
        if not holding and act in ("BUY_CE", "BUY_PE") and logged is None:
            self.last_reject = "ledger_write_failed"  # no trade without its audit line (exits still go ahead)
            return None
        if not holding and act in ("BUY_CE", "BUY_PE"):
            if not self.paper_enabled():
                self.last_reject = "paper_off"
                return None
            if not allow_entry:
                self.last_reject = "gate_blocked"
                return None
            if self.live_enabled() and self.day_realized - EST_CHARGES <= -DAY_LOSS_LIMIT:  # no live budget left
                self._stop_entries(now, "day_loss")
                return None
            if not self._entry_open() or now.strftime("%H:%M") > self.last_entry:
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
        level, kind = self._traded_level(leg, now)
        if self.live_enabled():
            return self._open_live(now, leg, str(symbol), strike, px, reason, level, kind)
        ev = self._append({"event": "open", "ts": now.isoformat(), "day": self.day, "leg": leg, "symbol": symbol,
                           "strike": strike, "qty": self.qty, "fill": round(px, 2), "entry": round(px, 2),
                           "side": "long", "reason": reason, "entry_level": level, "entry_level_kind": kind})
        if ev is None:  # no ledger line, no position (a restart could not know about it)
            self.last_reject = "ledger_write_failed"
            return None
        self.paper_entries += 1
        self.position = Position(day=self.day, leg=leg, symbol=str(symbol), strike=strike, qty=self.qty, fill=round(px, 2),
                                 opened_at=now.isoformat(), reason=reason, entry_level=level, entry_level_kind=kind)
        self.last_reject = None
        return ev

    def _traded_level(self, leg: str, now: datetime) -> tuple[float | None, str]:
        """The level a buy breaks, from the view the LLM just saw: for a call the nearest key level at or below NIFTY
        (opening-range high once complete, the day's high before the last candle, VWAP), for a put the nearest at or
        above it (opening-range low, day's low before the last candle, VWAP); NIFTY itself when there is none."""
        mins = sorted(m for m in self.history if m <= now.strftime("%H:%M"))
        spots = [self.history[m]["spot"] for m in mins]
        if not spots:
            return None, ""
        pa, spot = self._price_action(self.last_feed, spots, now), spots[-1]
        orr = pa.get("opening_range") or {}
        if leg == "ce":
            cands = [("opening_range_high", orr.get("high")), ("day_high_before_last_candle", pa.get("high_before_last_candle")),
                     ("vwap", pa.get("vwap"))]
            ok = [(v, k) for k, v in cands if v is not None and v <= spot]
            best = max(ok) if ok else None
        else:
            cands = [("opening_range_low", orr.get("low")), ("day_low_before_last_candle", pa.get("low_before_last_candle")),
                     ("vwap", pa.get("vwap"))]
            ok = [(v, k) for k, v in cands if v is not None and v >= spot]
            best = min(ok) if ok else None
        # No key level on the right side (e.g. the first minutes, no VWAP yet): NIFTY at entry, so the exit always has one
        return (best[0], best[1]) if best else (round(float(spot), 1), "nifty_at_entry")

    def _open_live(self, now: datetime, leg: str, symbol: str, strike: int, ask: float, reason: str,
                   level: float | None = None, kind: str = "") -> dict[str, Any] | None:
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
                 "strike": strike, "legs": [leg], "tag_prefix": self.tag_prefix, "qty": self.qty, "expiry_day": False,
                 "ce_symbol": symbol if leg == "ce" else "", "pe_symbol": symbol if leg == "pe" else "",
                 "reason": reason, "entry_level": level, "entry_level_kind": kind}
        if self._append(dict(start)) is None:  # no durable record: no real orders
            self.last_reject = "ledger_write_failed"
            return None
        self.entries += 1
        self.live_meta = start
        self.live = LiveStraddle(day=self.day, strike=strike, ce_symbol=start["ce_symbol"], pe_symbol=start["pe_symbol"],
                                 qty=self.qty, expiry_day=False, log=self._live_log, legs=(leg,), tag_prefix=self.tag_prefix)
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
        self.paper_realized = round(self.paper_realized + ev["pnl"], 2)
        return ev

    # ---- every frame ----
    def on_frame(self, *, now: datetime, feed: dict[str, Any], book: QuoteSource) -> dict[str, Any] | None:
        today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        self._new_day(today)
        self._kill_switch(now, today)
        if feed and not feed.get("error") and "09:15" <= hm <= "15:30":
            self._record(hm, feed)
        if not self.paper_enabled():  # "Paper" box off: close whatever is open, then stay idle (no LLM calls)
            if self.live is not None and self.live.phase in ("entering", "holding"):
                self.live.request_exit("paper_off")
            elif self.live is None and self.position is not None and self.pending_close is None:
                return self._exit(now, book, "paper_off")
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
        if hm >= self.exit_at:
            return self._exit(now, book, "square_off")
        return None  # a paper trade has no day stop or profit lock: the LLM's exit or the forced exit closes it

    def _kill_switch(self, now: datetime, today: str) -> None:
        """Box unticked after being on today (or found off with a live trade of today open): sell the live position;
        the book carries on in paper (new entries are paper because live_enabled() is now false)."""
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
        if source in ("day_end", "paper_off"):  # 15:30 auto-off; the Paper box's own exit (on_frame) handles paper_off
            return
        armed = self.live_on_day == today or (self.live is not None and self.live.day == today)
        if not armed:
            return
        if self.live_off_day != today:
            self.live_off_day = today
            self._append({"event": "live_off", "ts": now.isoformat(), "day": self.day, "why": "ui_off"})
            self._log.warning("llm_buyer: live box unticked -> selling any live position, paper from now on today")
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
            floor = self._profit_floor()
            if floor is not None:  # an extra trade on a winning day may only give back down to the floor
                budget = min(budget, self.day_realized - EST_CHARGES - floor)
            # Never 0: the live layer reads 0 as "no stop". A spent budget means sell at the first lower bid.
            stop = max(1e-6, min(1.0, budget / (avg * q)))
        before = ls.phase
        result = ls.step(self.order_client, bids={leg: quote_bid(book.get(ls.symbol(leg)))},
                         hm=now.strftime("%H:%M"), today=now.strftime("%Y-%m-%d"), stop_loss=stop,
                         exit_at=self.exit_at, square_off_late=self.live_orders_until)
        if before == "entering" and ls.phase in ("holding", "exiting"):
            avg, q = ls.cost(leg)  # the fills this frame's order-book read just brought in
            if q:
                self._append({"event": "open", "ts": now.isoformat(), "day": ls.day, "leg": leg, "symbol": ls.symbol(leg),
                              "strike": ls.strike, "qty": q, "fill": round(avg, 2), "entry": round(avg, 2),
                              "side": "long", "live": True, "reason": self.live_meta.get("reason"),
                              "entry_level": self.live_meta.get("entry_level"),
                              "entry_level_kind": self.live_meta.get("entry_level_kind")})
        if result is None:
            if ls.manual_needed:
                self.last_reject = "manual_needed"
            return None
        if result["reason"] == "stop":
            self._stop_entries(now, "profit_lock" if self._profit_floor() is not None else "day_loss")
        self.pending_close = {"event": "close", "ts": now.isoformat(), "day": ls.day, "leg": leg, "symbol": ls.symbol(leg),
                              "strike": ls.strike, "qty": ls.qty, "entry": result["debit"], "exit": result["exit"],
                              "pnl_gross": result["pnl_gross"], "charges": result["charges"], "pnl": result["pnl"],
                              "pnl_known": not result["estimated"], "pnl_estimated": result["estimated"],
                              "reason": (("profit_lock" if self._profit_floor() is not None else "day_loss_stop")
                                         if result["reason"] == "stop" else result["reason"]), "live": True}
        return self._write_pending_close()

    def _write_pending_close(self) -> dict[str, Any] | None:
        ev = self._append(dict(self.pending_close or {}))
        if ev is None:  # keep trying: the day and month totals move only when the line is on disk
            self.last_reject = "ledger_write_failed_close_pending"
            return None
        self.pending_close, self.live, self.live_meta = None, None, {}
        self.day_realized = round(self.day_realized + float(ev["pnl"]), 2)
        self.day_peak = max(self.day_peak, self.day_realized)
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
            "ok": True, "mode": "paper", "enabled": True, "book": self.strategy, "name": self.name,
            "window": f"entries {self.first_decision}–{self.last_entry} · open trade managed to {self.exit_at} "
                      f"· every {self.decide_every_min} min",
            "live_orders": self.live is not None, "live": self.live_flag.snapshot() if self.live_flag else None,
            "day": self.day, "entries": self.entries, "max_entries": self.max_entries,
            "max_entries_if_positive": self.max_entries_if_positive, "day_realized": round(self.day_realized),
            "paper_entries": self.paper_entries, "paper_realized": round(self.paper_realized),
            "day_loss_limit": DAY_LOSS_LIMIT, "stopped": self.stopped or None, "position": pos,
            "last_decision": self.last_decision, "last_reject": self.last_reject,
            "month_live_pnl": self.month_live_pnl.get(datetime.now(IST).strftime("%Y-%m"), 0.0),
        }
