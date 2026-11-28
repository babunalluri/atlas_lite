"""LLM Buy V2: an LLM reads the day like a discretionary trader and decides a few times a day. Paper only, never live.

Operator's call, 28 Nov 2026: a brand-new LLM buyer, built fresh (the first LLM buyer is not touched). Model: Claude
Sonnet 5.5 via OpenRouter. Straight to paper, no replay test first (operator's choice): there is no evidence it works.
What the first book taught (paper ledger 13-25 Nov: 143 trades, -Rs 14,735): asked every minute it churned (up to 46
trades a day); its winners were closed by code stops and most losers were its own quick EXITs. So V2:

- Asks rarely. At set checks (CHECK_TIMES) and when code sees a setup a trader would look at (TRIGGERS: opening-range
  break, new day high / low, a 0.75% stretch from the open, a gap filling, a VIX jump), at most MAX_CALLS_DAY a day,
  never two asks within ASK_COOLDOWN_MIN. Only while flat, entries ENTRY_FROM-LAST_ENTRY, at most MAX_TRADES a day.
- Reads the day first. The answer names the day type (trend, range, gap-and-go, gap-fill, expiry, event / VIX spike,
  late stretch, reversal) before BUY_CE / BUY_PE / WAIT / DONE_FOR_DAY, with a one-line thesis, the NIFTY level that
  proves it wrong (invalidation) and how long the idea needs (horizon). Low confidence is treated as WAIT.
- Sees evidence, not only candles: a history card of NIFTY 2019-2026 base rates for the same time and move from the
  open (llm_buy_v2_history.json: what NIFTY did next 30 / 60 min and to 15:10; gap-fill odds), its own recent trades
  (a journal), today's 5-min candles, the previous day, VIX, the ATM option prices, days to expiry and the overnight /
  macro snapshot (US futures, crude, USD/INR; headlines only when the host's news feed is on).
- Code owns every exit (the LLM is never asked while holding): a closed 1-min NIFTY candle beyond the invalidation
  level (kept 20-60 pts from entry); the horizon passed with the option not in profit; a trailing stop (armed at +20%,
  sold once half the best gain is given back); a hard stop at -35% of the price paid; 15:10.
- On/off switch on its card (operator's call 28 Nov 2026; llm_buy_v2_paper.json, ON when missing, stays as set across
  days): off = no LLM calls and no new trades; an open trade is sold at once ("switched_off") and an answer still
  on its way is not traded.
- 1 lot ATM, bought at the ask and sold at the bid; Kite charges. Ledger paper_llm_buy_v2.jsonl: every ask with the
  full input, every answer, opens and closes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from atlas_lite.paper_g2t10 import PaperG2T10, QuoteSource

STRATEGY = "llm_buy_v2"
LEDGER_NAME = "paper_llm_buy_v2.jsonl"
PAPER_FLAG_NAME = "llm_buy_v2_paper.json"  # the card's on/off switch: off = no LLM calls, no trades (ON if missing)
NAME = "LLM Buy V2 · Sonnet 5.5"
MODEL = "anthropic/claude-sonnet-5.5"
HISTORY_FILE = Path(__file__).with_name("llm_buy_v2_history.json")

ENTRY_FROM, LAST_ENTRY, EXIT_AT = "09:30", "14:30", "15:10"
CHECK_TIMES = ("09:30", "10:00", "10:45", "11:30", "12:30", "13:30", "14:01")
MAX_CALLS_DAY = 14
ASK_COOLDOWN_MIN = 5
MAX_TRADES = 2
LOSS_PAUSE_MIN = 15
MAX_ERRORS_DAY = 3  # LLM errors in a row -> no more asks today (credits out, provider down)
ANSWER_MAX_AGE_S = 120.0  # an answer that comes later than this is not traded
STRETCH_PCT = 0.75
GAP_PCT = 0.3
VIX_JUMP_PTS = 1.0
NEW_EXTREME_GAP_MIN = 45  # a new day high / low asks again only after this long
INVALIDATION_MIN_PTS, INVALIDATION_MAX_PTS = 20.0, 60.0
HORIZON_MIN, HORIZON_MAX, HORIZON_DEFAULT = 15, 120, 45
TRAIL_ARM_PCT, TRAIL_KEEP = 0.20, 0.5
HARD_STOP_PCT = 0.35
LLM_TIMEOUT_S = 45.0
MACRO_WAIT_S = 8.0  # the macro snapshot is skipped if Yahoo is slower than this (the ask goes on without it)
LLM_MAX_TOKENS = 1500
DAY_TYPES = ("trend_up", "trend_down", "range", "gap_and_go", "gap_fill", "expiry_day", "event_or_vix_spike",
             "late_stretch", "reversal", "unclear")

SYSTEM_PROMPT = """You are a careful, experienced NIFTY intraday trader who only BUYS options: one lot (65) of the at-the-money CALL
when you expect NIFTY to rise, or the at-the-money PUT when you expect it to fall. You are not asked every minute:
only at a few set times and when a setup a trader would look at has just happened (the "trigger" field says which).
Your job at each ask: read the day like a professional, then decide whether this is one of the few moments worth
paying for an option, or not.

Facts you must respect (from years of NIFTY data and this desk's own records):
- An option buy needs NIFTY to move about 10-15 points your way just to pay the spread, charges and time decay.
- After the first 15-30 minutes, NIFTY's next hour is close to a coin flip in most situations. history_card shows,
  for this time of day and this move from the open, what NIFTY did next (up_share / down_share = share of days it
  moved more than move_threshold_pct up / down; mean_pct = average move). Treat odds near 50/50 as "no edge".
- Big gaps (0.75%+) rarely fill the same day; small gaps fill more often, mostly early.
- A desk test of this exact idea failed when trades were taken often: churning many small trades lost money. Most
  days the right answer is WAIT, and on dull range days DONE_FOR_DAY.
- recent_trades is your own journal: learn from what lost before.

Read the day first and name its type (day_type):
- trend_up / trend_down: range expanding, price holding one side of the average price, higher highs (or lower
  lows); buy pullbacks that hold, in the trend's direction, not spikes.
- range: price crossing back and forth inside the opening range or yesterday's range, small candles. Buying loses
  to time decay here: WAIT or DONE_FOR_DAY.
- gap_and_go: the gap holds after the first 30 minutes and the opening range breaks the gap's way.
- gap_fill: the gap is failing and price is moving back toward yesterday's close (target: that close).
- expiry_day: options lose value very fast; only a clear, strong move is worth it, and it must come quickly.
- event_or_vix_spike: wide swings and expensive options; a buy needs a bigger move to pay off.
- late_stretch: NIFTY far (0.75%+) from the open in the afternoon; strong days have often carried on to the close.
- reversal: a break of the day high or low that closed back inside, a trap for breakout buyers.
- unclear: you cannot tell; that is a WAIT.

How to decide:
- Buy only if you can say in one sentence why NIFTY should move your way enough, soon (within horizon_min), AND
  name the NIFTY level that would prove you wrong (invalidation_nifty: below the current price for a CALL, above it
  for a PUT). Code sells the trade if a 1-minute candle closes beyond that level, if it is not in profit after
  horizon_min minutes, at a trailing stop once it is up 20%, at -35%, and at 15:10. You will not be asked again
  while holding, so choose the level and horizon with care.
- Do not chase: if the move already happened in the last few candles, it is usually in the price.
- confidence: "high" only for a clean setup with the odds on your side; "low" means you would not risk money
  (code treats low as WAIT).

Reply with JSON only, no other text:
{"day_type": "<one of: trend_up, trend_down, range, gap_and_go, gap_fill, expiry_day, event_or_vix_spike, late_stretch, reversal, unclear>",
 "action": "<BUY_CE | BUY_PE | WAIT | DONE_FOR_DAY>",
 "confidence": "<low | medium | high>",
 "thesis": "<one short sentence>",
 "invalidation_nifty": <number or null>,
 "horizon_min": <minutes, 15-120, or null>}"""


def paper_llm_buy_v2_enabled() -> bool:
    return os.environ.get("ATLAS_LITE_PAPER_LLM_BUY_V2", "1").strip().lower() in ("1", "true", "yes")


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _mins(hm: str) -> int:
    return int(hm[:2]) * 60 + int(hm[3:5])


def load_history(path: Path = HISTORY_FILE) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def history_card(hist: dict[str, Any], hm: str, move_pct: float, gap_pct: float | None, gap_open: bool,
                 nifty: float) -> dict[str, Any]:
    """The base rates for this half hour and this move from the open (plus gap-fill odds while a gap is open)."""
    if not hist or hm < "09:30":
        return {}
    slot = f"{hm[:2]}:{'30' if int(hm[3:5]) >= 30 else '00'}"
    trend = hist.get("trend", {})
    if slot not in trend:
        slot = max((s for s in trend if s <= slot), default="")
    buckets = [(-99, -1.0, "<= -1%"), (-1.0, -0.5, "-1% to -0.5%"), (-0.5, -0.25, "-0.5% to -0.25%"),
               (-0.25, 0.25, "flat (+-0.25%)"), (0.25, 0.5, "+0.25% to +0.5%"), (0.5, 1.0, "+0.5% to +1%"),
               (1.0, 99, ">= +1%")]
    name = next((n for lo, hi, n in buckets if lo <= move_pct < hi), None)
    row = trend.get(slot, {}).get(name or "")
    th = float(hist.get("move_threshold_pct") or 0.06)
    card: dict[str, Any] = {"source": hist.get("source"), "slot": slot, "move_from_open_bucket": name,
                            "move_threshold_pct": th, "move_threshold_pts_now": round(nifty * th / 100),
                            "typical_full_day_range_pct": hist.get("median_day_range_pct_2024_26")}
    if row:
        card["days"] = row.get("n")
        for k in ("next_30m", "next_60m", "to_1510"):
            v = row.get(k)
            if v:
                card[k] = {**v, "mean_pts_now": round(nifty * float(v["mean_pct"]) / 100, 1)}
    if gap_pct is not None and gap_open and abs(gap_pct) >= GAP_PCT:
        g = ("gap down >= 0.75%" if gap_pct <= -0.75 else "gap down 0.3-0.75%" if gap_pct < 0
             else "gap up 0.3-0.75%" if gap_pct < 0.75 else "gap up >= 0.75%")
        gf = hist.get("gap_fill", {})
        gs = max((s for s in gf if s <= slot), default="")
        if gs and g in gf[gs]:
            card["gap_not_filled_yet"] = {"bucket": g, "days": gf[gs][g]["n"],
                                          "share_filled_by_1510": gf[gs][g]["filled_by_1510"]}
    return card


def candles_5m(bars: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    grp: list[dict[str, Any]] = []
    key = ""
    for b in bars:
        hm = str(b["t"])[11:16]
        k = f"{hm[:3]}{int(hm[3:5]) // 5 * 5:02d}"
        if k != key and grp:
            out.append(_fmt(key, grp))
            grp = []
        key = k
        grp.append(b)
    if grp:
        out.append(_fmt(key, grp))
    return out


def _fmt(key: str, grp: list[dict[str, Any]]) -> str:
    o, c = float(grp[0]["o"]), float(grp[-1]["c"])
    h, lo = max(float(b["h"]) for b in grp), min(float(b["l"]) for b in grp)
    return f"{key} {o:.0f}/{h:.0f}/{lo:.0f}/{c:.0f}"


def parse_answer(txt: str) -> dict[str, Any]:
    out = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    act = str(out.get("action") or "").upper().strip()
    dt = str(out.get("day_type") or "").strip().lower()
    conf = str(out.get("confidence") or "").strip().lower()
    return {"action": act, "day_type": dt if dt in DAY_TYPES else "unclear",
            "confidence": conf if conf in ("low", "medium", "high") else "low",
            "thesis": str(out.get("thesis") or "")[:300], "invalidation_nifty": _f(out.get("invalidation_nifty")),
            "horizon_min": _f(out.get("horizon_min"))}


class V2Decider:
    """One OpenRouter chat call with the V2 prompt. Never raises: an error comes back as {"error": ...}."""

    def __init__(self, credentials: Any, *, post: Callable[..., Any] | None = None, model: str = MODEL) -> None:
        self.credentials = credentials
        self.model = model
        if post is None:
            from atlas_lite.llm_buyer import post_with_deadline
            post = post_with_deadline
        self._post = post

    def decide(self, state: dict[str, Any]) -> dict[str, Any]:
        c = self.credentials
        key, base = getattr(c, "api_key", ""), str(getattr(c, "base_url", "") or "")
        if not key or "openrouter" not in base:
            return {"error": "no_openrouter_credentials"}
        body = {"model": self.model, "temperature": 0.2, "max_tokens": LLM_MAX_TOKENS, "reasoning": {"effort": "low"},
                "provider": {"sort": "latency"},
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": json.dumps(state, separators=(",", ":"))}]}
        try:
            r = self._post(f"{base.rstrip('/')}/chat/completions", json=body, timeout=LLM_TIMEOUT_S,
                           headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                    "HTTP-Referer": "https://atlas-lite.local", "X-Title": "Atlas Lite LLM Buy V2"})
            r.raise_for_status()
            j = r.json()
            out = parse_answer(str(j["choices"][0]["message"]["content"] or ""))
            out["model"] = self.model
            out["usage"] = j.get("usage")
            return out
        except Exception as exc:  # noqa: BLE001 - an error is a WAIT
            code = getattr(getattr(exc, "response", None), "status_code", "")
            return {"error": f"{type(exc).__name__}:{code}"}


def macro_context() -> dict[str, Any]:
    """US futures, crude and USD/INR % moves (and headlines when the host's news feed is on). Never raises."""
    try:
        from atlas_lite.macro_sentiment import fetch_macro_snapshot

        snap = fetch_macro_snapshot()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Any] = {}
    for k in ("es_futures", "nq_futures", "crude_brent", "usdinr", "gold"):
        v = snap.get(k) or {}
        if isinstance(v, dict) and v.get("day_pct") is not None and not v.get("error"):
            out[f"{k}_pct"] = round(float(v["day_pct"]), 2)
    heads = [str(h.get("title") or "")[:140] for h in (snap.get("headlines") or []) if isinstance(h, dict)]
    if heads:
        out["headlines"] = heads[:6]
    return out


@dataclass
class PaperLlmBuyV2(PaperG2T10):
    """G2T10's ledger, fills and charges; an LLM decides entries at a few asks a day; code exits. Paper only."""

    strategy: str = STRATEGY
    name: str = NAME
    decider: Any = None
    paper_flag: Any = None  # OnOffFlag; None = always on
    macro: Callable[[], dict[str, Any]] = field(default=macro_context, repr=False)
    history: dict[str, Any] = field(default_factory=load_history, repr=False)
    wants_prev_day: bool = True  # the engine adds yesterday's OHLC to the feed
    calls: int = 0
    errors_in_row: int = 0
    done_for_day: bool = False
    fired: set[str] = field(default_factory=set)
    last_ask_min: int = -999
    last_extreme: dict[str, int] = field(default_factory=dict)
    last_loss_min: int = -999
    asking: dict[str, Any] | None = None  # {"id", "hm", "trigger", "at", "feed", "nifty"} while a call runs
    _answer: tuple[int, dict[str, Any]] | None = field(default=None, repr=False)  # (ask id, answer)
    _ask_id: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    last_decision: dict[str, Any] | None = None
    peak_bid: float = 0.0
    _flag_cache: tuple[float, bool] = (0.0, True)

    def paper_enabled(self) -> bool:
        """The card's switch (re-read at most every 2 s: on_frame runs every second)."""
        if self.paper_flag is None:
            return True
        at, on = self._flag_cache
        if time.monotonic() - at >= 2.0:
            try:
                on = bool(self.paper_flag.is_enabled())
            except Exception:  # noqa: BLE001 - an unreadable flag keeps the last state
                pass
            self._flag_cache = (time.monotonic(), on)
        return on

    # ---- day state (restart-safe) ----

    def _new_day(self, today: str) -> None:
        fresh = self.day != today
        super()._new_day(today)
        if not fresh:
            return
        self.calls, self.errors_in_row, self.done_for_day = 0, 0, False
        self.fired, self.last_ask_min, self.last_extreme, self.last_loss_min = set(), -999, {}, -999
        self.asking, self._answer, self.peak_bid = None, None, 0.0
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            if f'"day":"{today}"' not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind, hm = ev.get("event"), str(ev.get("ts") or "")[11:16]
            if kind == "ask":
                self.calls += 1
                self.fired.update(ev.get("fired") or [])
                if hm:
                    self.last_ask_min = _mins(hm)
            elif kind == "decision":
                self.errors_in_row = self.errors_in_row + 1 if ev.get("error") else 0
                if ev.get("action") == "DONE_FOR_DAY":
                    self.done_for_day = True
            elif kind == "open" and self.position is not None:
                for k in ("invalidation", "horizon_min", "thesis", "day_type", "trigger"):
                    self.position[k] = ev.get(k)
            elif kind == "close" and float(ev.get("pnl") or 0) <= 0 and hm:
                self.last_loss_min = _mins(hm)
        if self.position is not None:
            self.peak_bid = float(self.position.get("fill") or 0.0)

    # ---- the engine asks once a minute for a feed ----

    def decision_due(self, now: datetime) -> bool:
        hm = now.strftime("%H:%M")
        return hm != self.done_minute and now.second >= 3 and "09:16" <= hm < EXIT_AT

    def on_frame(self, *, now: datetime, feed: dict[str, Any], book: QuoteSource) -> dict[str, Any] | None:
        today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        self._new_day(today)
        if not self.paper_enabled():
            if self.asking is not None:  # an answer on its way is dropped
                self.asking = None
                with self._lock:
                    self._answer = None
            self.last_reject = "switched off"
            if self.position is not None:
                return self._close(now, book, "switched_off")
            return None
        ev = self._take_answer(now, book)
        if ev is not None:
            return ev
        if self.position is not None:
            ev = self._code_exits(now, hm, book)
            if ev is not None or self.position is None:
                return ev
        if not self.decision_due(now) or not feed or feed.get("error"):
            return None
        self.done_minute = hm
        bars = [b for b in (feed.get("bars_today") or [])
                if isinstance(b, dict) and b.get("c") is not None and str(b.get("t") or "").startswith(today)
                and str(b["t"])[11:16] < hm]  # closed candles only
        if not bars:
            return None  # NIFTY has not traded today (holiday): never trade at stale quotes
        if self.position is not None:
            return self._thesis_exits(now, hm, bars, book)
        why = self._triggers(hm, bars, feed)
        if not why or not self._may_ask(hm):
            return None
        self._ask(now, hm, why, bars, feed, book)
        return None

    # ---- when to ask ----

    def _triggers(self, hm: str, bars: list[dict[str, Any]], feed: dict[str, Any]) -> list[str]:
        """Setups that fire this minute (each once a day; new extremes again after NEW_EXTREME_GAP_MIN)."""
        out: list[str] = []
        if hm in CHECK_TIMES:
            out.append(f"check_{hm}")
        if len(bars) < 2:
            return out
        o, c, pc = float(bars[0]["o"]), float(bars[-1]["c"]), float(bars[-2]["c"])
        orb = [b for b in bars if str(b["t"])[11:16] <= "09:29"]
        if hm >= "09:31" and len(orb) >= 10:
            orh, orl = max(float(b["h"]) for b in orb), min(float(b["l"]) for b in orb)
            if c > orh >= pc:
                out.append("or_high_break")
            if c < orl <= pc:
                out.append("or_low_break")
        if hm >= "10:00" and len(bars) >= 3:
            prior = bars[:-1]
            now_m = _mins(hm)
            if c > max(float(b["h"]) for b in prior) and now_m - self.last_extreme.get("hi", -999) >= NEW_EXTREME_GAP_MIN:
                out.append("new_day_high")
            if c < min(float(b["l"]) for b in prior) and now_m - self.last_extreme.get("lo", -999) >= NEW_EXTREME_GAP_MIN:
                out.append("new_day_low")
        mv = (c / o - 1) * 100
        if mv >= STRETCH_PCT:
            out.append("stretch_up_0.75pct")
        if mv <= -STRETCH_PCT:
            out.append("stretch_down_0.75pct")
        prev = feed.get("prev_day") if isinstance(feed.get("prev_day"), dict) else None
        pcl = _f((prev or {}).get("close"))
        if pcl and abs(o / pcl - 1) * 100 >= GAP_PCT:
            last = bars[-1]
            if float(last["l"]) <= pcl <= float(last["h"]):
                out.append("gap_fill_touch")
        vchg = _f(feed.get("vix_chg"))
        if vchg is not None and vchg >= VIX_JUMP_PTS:
            out.append("vix_jump")
        once = [t for t in out if t not in ("new_day_high", "new_day_low")]
        fresh = [t for t in once if t not in self.fired]
        fresh += [t for t in out if t in ("new_day_high", "new_day_low")]
        return fresh

    def _may_ask(self, hm: str) -> bool:
        if self.asking is not None or self.decider is None:
            return False
        if not (ENTRY_FROM <= hm <= LAST_ENTRY):
            self.last_reject = "outside the entry window"
            return False
        if self.done_for_day:
            self.last_reject = "LLM said DONE_FOR_DAY"
            return False
        if self.trades >= MAX_TRADES:
            self.last_reject = f"{MAX_TRADES} trades today"
            return False
        if self.calls >= MAX_CALLS_DAY:
            self.last_reject = f"{MAX_CALLS_DAY} asks today"
            return False
        if self.errors_in_row >= MAX_ERRORS_DAY:
            self.last_reject = "LLM unavailable today"
            return False
        now_m = _mins(hm)
        if now_m - self.last_ask_min < ASK_COOLDOWN_MIN:
            return False
        if now_m - self.last_loss_min < LOSS_PAUSE_MIN:
            self.last_reject = "pause after a loss"
            return False
        return True

    def _ask(self, now: datetime, hm: str, why: list[str], bars: list[dict[str, Any]], feed: dict[str, Any],
             book: QuoteSource) -> None:
        state = self.state(now, bars, feed, book, why)
        for t in why:
            if t == "new_day_high":
                self.last_extreme["hi"] = _mins(hm)
            elif t == "new_day_low":
                self.last_extreme["lo"] = _mins(hm)
        self.fired.update(t for t in why if t not in ("new_day_high", "new_day_low"))
        self.calls += 1
        self.last_ask_min = _mins(hm)
        self._append({"event": "ask", "ts": now.isoformat(), "day": self.day, "hm": hm, "trigger": why,
                      "fired": [t for t in why if t not in ("new_day_high", "new_day_low")], "inputs": state})
        self._ask_id += 1
        ask_id = self._ask_id
        self.asking = {"id": ask_id, "hm": hm, "trigger": why, "at": time.monotonic(), "feed": dict(feed),
                       "nifty": state["nifty"]["last"]}
        decider, macro = self.decider, self.macro

        def work() -> None:
            got: dict[str, Any] = {}

            def fetch() -> None:
                try:
                    got.update(macro() or {})
                except Exception:  # noqa: BLE001
                    pass

            mt = threading.Thread(target=fetch, name="llm-buy-v2-macro", daemon=True)
            mt.start()
            mt.join(MACRO_WAIT_S)
            if got and not mt.is_alive():
                state["overnight_and_macro"] = dict(got)
            out = decider.decide(state)
            with self._lock:
                self._answer = (ask_id, out)  # tagged: a late answer to an abandoned ask is never used for the next

        threading.Thread(target=work, name="llm-buy-v2", daemon=True).start()

    # ---- the answer ----

    def _take_answer(self, now: datetime, book: QuoteSource) -> dict[str, Any] | None:
        if self.asking is None:
            return None
        with self._lock:
            got, self._answer = self._answer, None
        out = got[1] if got is not None and got[0] == self.asking["id"] else None
        if out is None:
            if time.monotonic() - self.asking["at"] > LLM_TIMEOUT_S + 30:
                out = {"error": "llm_hung"}
            else:
                return None
        ask, self.asking = self.asking, None
        age = time.monotonic() - ask["at"]
        act = str(out.get("action") or "")
        self.errors_in_row = self.errors_in_row + 1 if out.get("error") else 0
        self.last_decision = {"hm": ask["hm"], "trigger": ask["trigger"], **{k: v for k, v in out.items() if k != "usage"}}
        self._append({"event": "decision", "ts": now.isoformat(), "day": self.day, "hm": ask["hm"],
                      "trigger": ask["trigger"], "action": act or "ERROR", "answer_s": round(age, 1), **out})
        if act == "DONE_FOR_DAY":
            self.done_for_day = True
            return None
        if act not in ("BUY_CE", "BUY_PE"):
            return None
        if out.get("confidence") == "low":
            self.last_reject = "low confidence: treated as WAIT"
            return None
        if age > ANSWER_MAX_AGE_S or now.strftime("%H:%M") > LAST_ENTRY or self.position is not None:
            self.last_reject = "answer too late"
            return None
        return self._enter(now, ask, out, book)

    def _enter(self, now: datetime, ask: dict[str, Any], out: dict[str, Any], book: QuoteSource) -> dict[str, Any] | None:
        leg = "ce" if out["action"] == "BUY_CE" else "pe"
        feed = ask["feed"]
        symbol, strike = feed.get(f"{leg}_symbol"), int(feed.get("atm") or 0)
        px = self._buy_px(book, str(symbol)) if symbol else None
        if not symbol or not strike or not px:
            self.last_reject = "no quote"
            return None
        ref = float(ask["nifty"])
        inv = _f(out.get("invalidation_nifty"))
        dist = (ref - inv) if (inv is not None and leg == "ce") else (inv - ref) if inv is not None else None
        if dist is None or dist <= 0:  # missing or on the wrong side: the default distance
            dist = 30.0
        dist = min(max(dist, INVALIDATION_MIN_PTS), INVALIDATION_MAX_PTS)
        level = round(ref - dist if leg == "ce" else ref + dist, 1)
        hz = _f(out.get("horizon_min"))
        horizon = int(min(max(hz if hz else HORIZON_DEFAULT, HORIZON_MIN), HORIZON_MAX))
        qty = int(self.lot_size)
        ev = self._append({"event": "open", "ts": now.isoformat(), "day": self.day, "leg": leg, "symbol": symbol,
                           "strike": strike, "qty": qty, "fill": round(px, 2), "entry": round(px, 2), "side": "long",
                           "nifty_at_ask": ref, "invalidation": level, "invalidation_asked": inv,
                           "horizon_min": horizon, "thesis": out.get("thesis"), "day_type": out.get("day_type"),
                           "confidence": out.get("confidence"), "trigger": ask["trigger"],
                           "reason": f"{out.get('day_type')}: {out.get('thesis')}"})
        if ev is None:
            return None
        self.trades += 1
        self.peak_bid = round(px, 2)
        self.position = {"leg": leg, "symbol": symbol, "strike": strike, "qty": qty, "fill": round(px, 2),
                         "opened_at": now.isoformat(), "invalidation": level, "horizon_min": horizon,
                         "thesis": out.get("thesis"), "day_type": out.get("day_type"), "trigger": ask["trigger"]}
        self.last_reject = None
        return ev

    # ---- code exits ----

    def _close(self, now: datetime, book: QuoteSource, why: str) -> dict[str, Any] | None:
        ev = super()._close(now, book, why)
        if ev is not None and float(ev.get("pnl") or 0) <= 0:
            self.last_loss_min = _mins(now.strftime("%H:%M"))
        if ev is not None:
            self.peak_bid = 0.0
        return ev

    def _code_exits(self, now: datetime, hm: str, book: QuoteSource) -> dict[str, Any] | None:
        """Every second: 15:10, the hard stop and the trailing stop (option bid)."""
        if hm >= EXIT_AT:
            return self._close(now, book, "exit_1510")
        pos = self.position
        bid = self._sell_px(book, str(pos["symbol"]))
        if bid is None:
            return None
        fill = float(pos["fill"])
        self.peak_bid = max(self.peak_bid, bid)
        if bid <= fill * (1 - HARD_STOP_PCT):
            return self._close(now, book, "hard_stop")
        if self.peak_bid >= fill * (1 + TRAIL_ARM_PCT) and bid <= fill + (self.peak_bid - fill) * (1 - TRAIL_KEEP):
            return self._close(now, book, "trail_stop")
        return None

    def _thesis_exits(self, now: datetime, hm: str, bars: list[dict[str, Any]], book: QuoteSource) -> dict[str, Any] | None:
        """Once a minute: a closed candle beyond the invalidation level; the horizon passed while not in profit."""
        pos = self.position
        c = float(bars[-1]["c"])
        lvl = _f(pos.get("invalidation"))
        if lvl is not None and (c < lvl if pos["leg"] == "ce" else c > lvl):
            return self._close(now, book, "invalidated")
        try:
            held = (now - datetime.fromisoformat(str(pos["opened_at"]))).total_seconds() / 60
        except (TypeError, ValueError):
            held = 0
        bid = self._sell_px(book, str(pos["symbol"]))
        if held >= float(pos.get("horizon_min") or HORIZON_DEFAULT) and bid is not None and bid <= float(pos["fill"]):
            return self._close(now, book, "thesis_timeout")
        return None

    # ---- what the LLM sees ----

    def _journal(self, limit: int = 8) -> list[dict[str, Any]]:
        """Its own last closed trades (before today), newest last."""
        if not self.path.is_file():
            return []
        opens: dict[str, dict[str, Any]] = {}
        out: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if '"event":"open"' not in line and '"event":"close"' not in line:
                    continue
                ev = json.loads(line)
                if ev.get("day") == self.day:
                    continue
                if ev.get("event") == "open":
                    opens[str(ev.get("symbol"))] = ev
                elif ev.get("event") == "close" and ev.get("pnl") is not None:
                    o = opens.pop(str(ev.get("symbol")), {})
                    out.append({"day": ev.get("day"), "time": str(o.get("ts") or "")[11:16], "option": str(ev.get("leg") or "").upper(),
                                "day_type": o.get("day_type"), "trigger": o.get("trigger"), "thesis": o.get("thesis"),
                                "exit": ev.get("reason"), "pnl_rs": round(float(ev["pnl"]))})
        except (OSError, json.JSONDecodeError, ValueError):
            return out[-limit:]
        return out[-limit:]

    def state(self, now: datetime, bars: list[dict[str, Any]], feed: dict[str, Any], book: QuoteSource,
              why: list[str]) -> dict[str, Any]:
        hm = now.strftime("%H:%M")
        o, c = float(bars[0]["o"]), float(bars[-1]["c"])
        hi_b = max(bars, key=lambda b: float(b["h"]))
        lo_b = min(bars, key=lambda b: float(b["l"]))
        hi, lo = float(hi_b["h"]), float(lo_b["l"])
        closes = [float(b["c"]) for b in bars]

        def back(k: int) -> float | None:
            return round(c - closes[-1 - k], 1) if len(closes) > k else None

        avg = sum((float(b["h"]) + float(b["l"]) + float(b["c"])) / 3 for b in bars) / len(bars)
        orb = [b for b in bars if str(b["t"])[11:16] <= "09:29"]
        prev = feed.get("prev_day") if isinstance(feed.get("prev_day"), dict) else None
        pcl = _f((prev or {}).get("close"))
        gap = round((o / pcl - 1) * 100, 2) if pcl else None
        gap_open = bool(pcl) and not (lo <= pcl <= hi)
        exp = str(feed.get("expiry") or "")[:10]
        try:
            dte = (date.fromisoformat(exp) - now.date()).days if exp else None
        except ValueError:
            dte = None
        ce, pe = _f(feed.get("ce")), _f(feed.get("pe"))
        ce_sym, pe_sym = feed.get("ce_symbol"), feed.get("pe_symbol")
        ce_ask = self._buy_px(book, str(ce_sym)) if ce_sym else None
        pe_ask = self._buy_px(book, str(pe_sym)) if pe_sym else None
        move = round((c / o - 1) * 100, 2)
        s: dict[str, Any] = {
            "time": hm, "weekday": now.strftime("%A"), "trigger": why,
            "minutes_to_forced_exit": _mins(EXIT_AT) - _mins(hm),
            "calendar": {"expiry": exp or None, "days_to_expiry": dte, "expiry_day": dte == 0},
            "nifty": {"last": round(c, 1), "open_0915": round(o, 1), "move_from_open_pct": move,
                      "gap_at_open_pct": gap, "gap_still_open": gap_open if gap is not None else None,
                      "day_high": round(hi, 1), "day_high_at": str(hi_b["t"])[11:16],
                      "day_low": round(lo, 1), "day_low_at": str(lo_b["t"])[11:16],
                      "day_range_pct": round((hi - lo) / o * 100, 2), "average_price_today": round(avg, 1),
                      "move_5m_pts": back(5), "move_15m_pts": back(15), "move_30m_pts": back(30)},
            "candles_5m_today": candles_5m(bars)[-78:],
            "last_10_candles_1m": [f"{str(b['t'])[11:16]} {float(b['o']):.0f}/{float(b['h']):.0f}/{float(b['l']):.0f}/{float(b['c']):.0f}"
                                   for b in bars[-10:]],
            "vix": {"now": feed.get("vix"), "change_since_open_pts": feed.get("vix_chg")},
            "options": {k: v for k, v in {
                "atm_strike": feed.get("atm"), "atm_call_ask": ce_ask or ce, "atm_put_ask": pe_ask or pe,
                "atm_straddle": round(ce + pe, 1) if ce and pe else None, "atm_iv": feed.get("iv"),
                "iv_percentile_1y": feed.get("ivp"), "put_call_ratio": feed.get("pcr")}.items() if v is not None},
            "history_card": history_card(self.history, hm, move, gap, gap_open, c),
            "today": {"asks_so_far": self.calls, "asks_left": MAX_CALLS_DAY - self.calls - 1,
                      "trades": self.trades, "trades_left": MAX_TRADES - self.trades,
                      "realized_rs": round(self.realized)},
            "recent_trades": self._journal(),
        }
        if len(orb) >= 10:
            s["nifty"]["opening_range_0915_0929"] = {"high": round(max(float(b["h"]) for b in orb), 1),
                                                     "low": round(min(float(b["l"]) for b in orb), 1)}
        if prev:
            s["previous_day"] = {k: round(float(prev[k]), 1) for k in ("open", "high", "low", "close") if prev.get(k) is not None}
        return s

    # ---- dashboard ----

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        snap = super().snapshot(book)
        snap.pop("entries_stopped", None)
        snap.pop("losses_today", None)
        snap["model"] = MODEL
        snap["paper_on"] = self.paper_enabled()
        snap["asks_today"] = self.calls
        snap["max_asks"] = MAX_CALLS_DAY
        snap["asking"] = bool(self.asking)
        snap["done_for_day"] = self.done_for_day
        snap["last_decision"] = self.last_decision
        if snap.get("position") is not None:
            p = self.position or {}
            snap["position"].update({k: p.get(k) for k in ("invalidation", "horizon_min", "thesis", "day_type")})
        snap["window"] = (f"asks at {', '.join(CHECK_TIMES)} and on setups (max {MAX_CALLS_DAY}/day) · entries "
                          f"{ENTRY_FROM}-{LAST_ENTRY}, max {MAX_TRADES} · code exits: invalidation level, horizon, "
                          f"trail +{int(TRAIL_ARM_PCT * 100)}%/half back, -{int(HARD_STOP_PCT * 100)}%, {EXIT_AT}")
        return snap
