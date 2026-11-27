"""Model vote buyer: three trained models vote on NIFTY's direction over the next 30 minutes; paper only, never live,
no LLM. 1 lot ATM NIFTY CE or PE.

Operator's call, 27 Nov 2026: built to watch on paper although the offline test FAILED the pass bar (scratchpad
train_vote_book.py; 2026 unseen: 3.6 trades a day, +1.7 NIFTY points a trade, t 1.1, hit 56%; a buy needs about +10
points to cover the option's spread and charges, so the expected result is a small loss per trade).

Models (atlas_lite/model_vote_buyer.json, pure Python, no numpy): LightGBM, CatBoost and logistic regression trained on
NIFTY 1-minute bars 2019-2024, early-stopped on 2025. Inputs come only from today's closed 1-minute NIFTY candles:
returns over 1/5/15/30/60 minutes, realised volatility over 15/60 minutes, position in the day's range, distance to the
day's high and low, opening-range (09:15-09:29) break, time of day, move from the open.

Every minute, on the candles closed at 09:15, 09:16 ... 14:59 (decided in the next minute, from second 3; trained on
10:15-14:30 only, the window is the operator's call). Operator's
call, 27 Nov 2026: the models were trained on 5-minute samples; voting every minute did worse offline (2026: 4.4 trades
a day, +0.2 NIFTY points a trade, t 0.2, vs 3.6 a day, +1.7 points every 5 minutes; scratchpad vote_1min_check.py):
- each model votes UP if its score is above its own 2025 median, else DOWN;
- all three UP and the average score lead over the medians at least ENTRY_CONF_CUT (top 10% of 2025's readings;
  top 20% = 0.0288 until 27 Nov 2026) -> buy the ATM CE at the ask; all three DOWN and as confident -> buy the ATM PE;
  otherwise no trade. The same cut makes a confident opposite vote (the reverse).
- Exits by vote too (operator's call, 27 Nov 2026, "vote for all decisions"): while holding, the vote keeps running on
  every candle to 15:13; a confident vote for the other side sells the trade at the bid and buys the other side in the
  same minute (a reverse; no new trades after the 14:59 candle). Anything else holds. Square-off 15:14.
  Offline (scratchpad vote_exit_test.py, 2025 / 2026): 1.7 / 1.9 trades a day, +6.1 / -0.3 NIFTY points a trade, about
  -7 / -19 points a day after costs, against -47 / -57 for the 30-minute exit it replaced.
- Every vote is written to the ledger as a 'vote' line (candle, NIFTY, scores, confidence, decision). One trade at a
  time. No stop, no day limit (paper).
- Trailing SL (operator's call 27 Nov 2026: a shadow in the morning, switched to real for this paper book the same
  evening): once NIFTY has moved TRAIL_ARM_PTS the trade's way (1-minute closes), the trade is sold at the bid when
  NIFTY gives back TRAIL_GIVEBACK of its best move (close reason 'trail_stop'), and the book stays flat for the rest of
  the day. Offline (scratchpad vote_trail2.py) the only trail no worse in both 2025 and 2026 (Rs -304 / -327 a day vs
  -314 / -540); on the 46 recorded days 24 Sep-26 Nov (replay_summary.py) -Rs 7,248 vs -Rs 17,233 without it.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from atlas_lite.paper_g2t10 import PaperG2T10, QuoteSource, _mins

STRATEGY = "model_vote"
LEDGER_NAME = "paper_model_vote.jsonl"
NAME = "Model vote buyer · no LLM"
# Operator's call, 27 Nov 2026: entries 09:16-15:00 (candles 09:15-14:59). Training covered only the 10:15-14:30
# candles; before 10:14 the 60-minute inputs use the candles there are so far (outside what the models saw).
FIRST_BAR, LAST_BAR, EXIT_AT = "09:15", "14:59", "15:14"  # square-off 15:14: operator's call
LAST_VOTE = "15:13"  # votes keep running for exits after the last entry candle
STEP_MIN = 1  # operator's call 27 Nov 2026: every minute (trained on 5-minute samples)
TRAIL_ARM_PTS = 80.0  # trailing SL: armed after this NIFTY move the trade's way
TRAIL_GIVEBACK = 0.5  # ... and sells after giving back this share of the best move
TRAIL_RULE = "NIFTY trail: armed at +80 pts, sells after giving back 50% of the best move, then flat for the day"
OR_BARS = 15  # 09:15-09:29
MODEL_FILE = Path(__file__).with_name("model_vote_buyer.json")
# Entry / reverse confidence (operator's call, 27 Nov 2026 evening): top 10% of 2025's readings, was the model file's
# top 20% (0.0288). Option-priced 2025 / 2026: -Rs 167 / -205 a day vs -340 / -289; 46 recorded days with real bid/ask
# and the trailing SL: +Rs 29,563 vs -Rs 7,248 (t 0.9, not significant; scratchpad vote_aggr.py, replays_c10).
ENTRY_CONF_CUT = 0.0370


def paper_model_vote_enabled() -> bool:
    return os.environ.get("ATLAS_LITE_PAPER_MODEL_VOTE", "1").strip().lower() in ("1", "true", "yes")


def _sig(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x)) if x >= -700 else 0.0


def _pstd(xs: list[float]) -> float:
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def features(bars: list[dict[str, Any]]) -> list[float] | None:
    """The 13 model inputs at the last candle of `bars` (today's closed 1-minute candles from 09:15, in order), exactly
    as in training (scratchpad rich_features.py) from 60 candles on. With fewer (before 10:14) the 15- and 60-minute
    inputs use the candles there are, and before 09:30 the opening range is still forming (break = 0). None without
    candles from 09:15."""
    if not bars or str(bars[0].get("t") or "")[11:16] != "09:15":
        return None
    o = [float(b["o"]) for b in bars]
    h = [float(b["h"]) for b in bars]
    lo_ = [float(b["l"]) for b in bars]
    c = [float(b["c"]) for b in bars]
    i = len(c) - 1
    prev = [o[0]] + c[:-1]
    r = [math.log(c[k] / prev[k]) for k in range(len(c))]
    ret = lambda n: (c[i] / c[max(0, i - n)] - 1) * 100  # noqa: E731
    hi, lo = max(h), min(lo_)
    or_h, or_l = max(h[:OR_BARS]), min(lo_[:OR_BARS])
    hm = str(bars[-1]["t"])[11:16]
    return [ret(1), ret(5), ret(15), ret(30), ret(60), _pstd(r[max(0, i - 14):]) * 100, _pstd(r[max(0, i - 59):]) * 100,
            (c[i] - lo) / (hi - lo) if hi > lo else 0.5, (hi / c[i] - 1) * 100, (c[i] / lo - 1) * 100,
            1.0 if c[i] > or_h else (-1.0 if c[i] < or_l else 0.0), (_mins(hm) - 555) / 375.0, (c[i] / o[0] - 1) * 100]


class VoteModels:
    """The exported LightGBM / CatBoost / logistic models and the vote rule."""

    def __init__(self, path: Path = MODEL_FILE) -> None:
        m = json.loads(Path(path).read_text(encoding="utf-8"))
        self.version = str(m.get("version") or "")
        self.lgb, self.cb, self.lr = m["lightgbm"], m["catboost"], m["logistic"]
        self.median = {k: float(v) for k, v in m["median"].items()}
        self.conf_cut = ENTRY_CONF_CUT if ENTRY_CONF_CUT else float(m["conf_cut"])
        self.n_features = len(m["features"])

    def scores(self, x: list[float]) -> dict[str, float]:
        raw = 0.0
        for node in self.lgb:
            while isinstance(node, list):
                node = node[2] if x[node[0]] <= node[1] else node[3]
            raw += node
        cbr = 0.0
        for splits, leaves in self.cb["trees"]:
            idx = 0
            for bit, (f, border) in enumerate(splits):
                if x[f] > border:
                    idx |= 1 << bit
            cbr += leaves[idx]
        lr = self.lr
        z = lr["intercept"] + sum(w * (v - m) / s for w, v, m, s in zip(lr["coef"], x, lr["mean"], lr["std"]))
        return {"lightgbm": _sig(raw), "catboost": _sig(cbr * self.cb["scale"] + self.cb["bias"]), "logistic": _sig(z)}

    def vote(self, x: list[float]) -> dict[str, Any]:
        s = self.scores(x)
        ups = {k: s[k] > self.median[k] for k in s}
        conf = sum(s[k] - self.median[k] for k in s) / len(s)
        leg = None
        if all(ups.values()) and conf >= self.conf_cut:
            leg = "ce"
        elif not any(ups.values()) and conf <= -self.conf_cut:
            leg = "pe"
        return {"scores": {k: round(v, 4) for k, v in s.items()}, "up": ups, "confidence": round(conf, 4),
                "needed": round(self.conf_cut, 4), "leg": leg}


@dataclass
class PaperModelVote(PaperG2T10):
    """G2T10's ledger, restart, fills and square-off; the model vote decides. Paper only (never Kite orders)."""

    strategy: str = STRATEGY
    name: str = NAME
    models: VoteModels | None = None
    last_vote: dict[str, Any] | None = None
    trail: dict[str, Any] | None = None  # trailing SL of the open trade
    trail_done: dict[str, Any] | None = None  # today's trailing-SL sale (the book is flat for the rest of the day)
    trail_day: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.models is None:
            self.models = VoteModels()

    def decision_due(self, now: datetime) -> bool:
        """Once a minute from second 3, 09:16 until this book's square-off (G2T10's own stops at 15:08)."""
        hm = now.strftime("%H:%M")
        return hm != self.done_minute and now.second >= 3 and "09:16" <= hm < EXIT_AT

    def on_frame(self, *, now: datetime, feed: dict[str, Any], book: QuoteSource) -> dict[str, Any] | None:
        today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        self._new_day(today)
        if self.trail_day != today:
            self._rebuild_trail(today)
        if self.position is not None and hm >= EXIT_AT:
            self.trail = None
            return self._close(now, book, "square_off")
        if not self.decision_due(now) or not feed or feed.get("error"):
            return None
        self.done_minute = hm
        bars = [b for b in (feed.get("bars_today") or [])
                if isinstance(b, dict) and b.get("c") is not None and str(b.get("t") or "").startswith(today)
                and str(b["t"])[11:16] < hm]  # closed candles only
        if not bars:
            return None
        last = str(bars[-1]["t"])[11:16]
        m = _mins(last)
        if not (FIRST_BAR <= last <= LAST_VOTE and (m - _mins(FIRST_BAR)) % STEP_MIN == 0 and _mins(hm) - m == 1):
            return None
        x = features(bars)
        if x is None:
            self.last_reject = "too_few_candles"
            return None
        assert self.models is not None
        v = self.models.vote(x)
        self.last_vote = {"at": hm, "candle": last, **v}
        nifty = float(bars[-1]["c"])
        trail_note = self._trail_step(now, book, last, nifty)
        closed = trail_note.get("sold") if trail_note else None
        if self.position is not None and v["leg"] is not None and v["leg"] != self.position.get("leg"):
            closed = self._close(now, book, "vote_reverse")
            if self.position is not None:  # no quote to sell: try again on the next second of this minute
                self.done_minute = ""
                return None
            self.trail = None
        opened = None
        if v["leg"] is not None and self.position is None and last <= LAST_BAR and self.trail_done is None:
            opened = self._open_vote(now, feed, book, v, nifty)
        self._log_vote(now, last, nifty, v, opened, closed,
                       {k: x for k, x in trail_note.items() if k != "sold"} if trail_note else None)
        return opened or closed

    # ---- trailing SL ----

    def _trail_step(self, now: datetime, book: QuoteSource, candle: str, nifty: float) -> dict[str, Any] | None:
        """Update the open trade's trailing SL on this candle; sell the trade when it triggers (then flat for the day)."""
        tr, pos = self.trail, self.position
        if tr is None or pos is None or self.trail_done is not None or tr.get("opened_at") != pos.get("opened_at"):
            return None
        move = (nifty - tr["c0"]) * (1 if tr["leg"] == "ce" else -1)
        tr["best"] = max(tr["best"], move)
        note: dict[str, Any] = {"move": round(move, 2), "best": round(tr["best"], 2), "armed": tr["best"] >= TRAIL_ARM_PTS}
        if not note["armed"] or move > tr["best"] * (1 - TRAIL_GIVEBACK):
            return note
        ev = self._close(now, book, "trail_stop")
        if ev is None:  # no quote to sell: try again next candle
            return note
        self.trail_done = {**ev, "best_move": round(tr["best"], 2), "move": round(move, 2)}
        self.trail = None
        note["trail_stop"] = True
        note["sold"] = ev
        return note

    def _rebuild_trail(self, today: str) -> None:
        """After a restart (or on a new day): today's trailing-SL sale, and the open trade's trail rebuilt from its open
        line and the NIFTY closes in the vote lines since."""
        self.trail_day, self.trail, self.trail_done = today, None, None
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.is_file() else []
        except OSError:
            return
        tr: dict[str, Any] | None = None
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("day") != today:
                continue
            kind = ev.get("event")
            if kind == "close" and ev.get("reason") == "trail_stop":
                self.trail_done = ev
                tr = None
            elif kind == "open":
                c0 = ev.get("nifty")  # older open lines lack it: taken from the buy's own vote line below
                tr = {"opened_at": ev.get("ts"), "leg": ev.get("leg"), "c0": float(c0) if c0 is not None else None,
                      "best": 0.0}
            elif kind == "close":
                tr = None
            elif kind == "vote" and tr is not None and ev.get("nifty") is not None:
                ts = str(ev.get("ts") or "")
                if tr["c0"] is None and ts[:16] == str(tr["opened_at"])[:16] and "buy" in str(ev.get("action") or ""):
                    tr["c0"] = float(ev["nifty"])
                elif tr["c0"] is not None and ts > str(tr["opened_at"]):
                    tr["best"] = max(tr["best"], (float(ev["nifty"]) - tr["c0"]) * (1 if tr["leg"] == "ce" else -1))
        if (self.position is not None and tr is not None and tr["c0"] is not None and self.trail_done is None
                and tr["opened_at"] == self.position.get("opened_at")):
            self.trail = tr

    def _log_vote(self, now: datetime, candle: str, nifty: float, v: dict[str, Any], opened: dict[str, Any] | None,
                  closed: dict[str, Any] | None = None, trail: dict[str, Any] | None = None) -> None:
        """One 'vote' line per minute in the ledger (operator's call, 27 Nov 2026), so every decision can be reviewed."""
        n_up = sum(1 for u in v["up"].values() if u)
        held = (self.position or {}).get("leg")
        if opened is not None:
            action = (f"sell {str(closed.get('leg') or '').upper()}, " if closed else "") + f"buy {v['leg'].upper()}"
        elif closed is not None and closed.get("reason") == "trail_stop":
            action = f"sell {str(closed.get('leg') or '').upper()} (trailing SL), flat for the rest of the day"
        elif closed is not None:
            why = f"no new entries after {LAST_BAR}" if candle > LAST_BAR else f"{v['leg'].upper()} not filled: {self.last_reject}"
            action = f"sell {str(closed.get('leg') or '').upper()} ({why})"
        elif held:
            action = f"hold {str(held).upper()}" + (": vote agrees" if v["leg"] == held else ": no opposite signal")
        elif self.trail_done is not None:
            action = "no trade: trailing SL hit today"
        elif v["leg"] is not None and candle > LAST_BAR:
            action = f"no trade: after {LAST_BAR}"
        elif v["leg"] is not None:
            action = f"signal, not filled ({self.last_reject})"
        elif n_up in (0, len(v["up"])):
            action = "no trade: not confident"
        else:
            action = "no trade: models split"
        last_event = self.last_event
        self._append({"event": "vote", "ts": now.isoformat(), "day": self.day, "candle": candle, "nifty": round(nifty, 2),
                      "scores": v["scores"], "up": n_up, "confidence": v["confidence"], "needed": v["needed"],
                      "leg": v["leg"], "action": action, **({"trail": trail} if trail else {})})
        self.last_event = last_event  # the card's last event stays the last trade

    def _open_vote(self, now: datetime, feed: dict[str, Any], book: QuoteSource, v: dict[str, Any],
                   nifty: float | None = None) -> dict[str, Any] | None:
        leg = v["leg"]
        symbol, strike = feed.get(f"{leg}_symbol"), int(feed.get("atm") or 0)
        if not symbol or not strike:
            self.last_reject = "no_atm_symbol"
            return None
        px = self._buy_px(book, str(symbol))
        if not px:
            self.last_reject = "no_quote"
            return None
        qty = int(self.lot_size)
        side = "UP" if leg == "ce" else "DOWN"
        ev = self._append({"event": "open", "ts": now.isoformat(), "day": self.day, "leg": leg, "symbol": symbol,
                           "strike": strike, "qty": qty, "fill": round(px, 2), "entry": round(px, 2), "side": "long",
                           "scores": v["scores"], "confidence": v["confidence"],
                           "nifty": round(nifty, 2) if nifty is not None else None,
                           "reason": f"3 models agree {side}, confidence {v['confidence']:+.4f} (needs ±{v['needed']:.4f})"})
        if ev is None:
            return None
        self.trades += 1
        self.position = {"leg": leg, "symbol": symbol, "strike": strike, "qty": qty, "fill": round(px, 2),
                         "opened_at": now.isoformat()}
        self.trail = ({"opened_at": now.isoformat(), "leg": leg, "c0": round(nifty, 2), "best": 0.0}
                      if nifty is not None and self.trail_done is None else None)
        self.last_reject = None
        return ev

    def snapshot(self, book: QuoteSource | None = None) -> dict[str, Any]:
        snap = super().snapshot(book)
        snap.pop("entries_stopped", None)
        snap.pop("losses_today", None)
        if snap.get("position"):
            other = "PE" if snap["position"].get("leg") == "ce" else "CE"
            snap["position"]["exit_by"] = f"{other} vote or {EXIT_AT}"
        snap["last_vote"] = self.last_vote
        snap["trail"] = {"rule": TRAIL_RULE, "arm_pts": TRAIL_ARM_PTS,
                         "open": ({"best": round(self.trail["best"], 2), "armed": self.trail["best"] >= TRAIL_ARM_PTS}
                                  if self.trail is not None and self.trail_done is None else None),
                         "done": ({k: self.trail_done.get(k) for k in ("ts", "leg", "exit", "pnl", "best_move", "move")}
                                  if self.trail_done else None)}
        snap["models"] = self.models.version if self.models else None
        snap["window"] = (f"votes every minute on the {FIRST_BAR}-{LAST_BAR} candles · 1 lot ATM CE/PE when all 3 models "
                          f"agree and are confident (top 10%, ±{ENTRY_CONF_CUT}) · sold (and reversed) on a confident opposite vote · trailing SL from "
                          f"+{TRAIL_ARM_PTS:.0f} NIFTY pts, sold after giving back half, then flat for the day · else {EXIT_AT}")
        return snap

