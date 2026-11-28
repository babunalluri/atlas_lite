"""LLM Buy V2: when it asks, what it sees, how answers become trades, code exits, limits and restart."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from atlas_lite.paper_llm_buy_v2 import (
    MAX_CALLS_DAY,
    PaperLlmBuyV2,
    candles_5m,
    history_card,
    load_history,
    parse_answer,
)

DAY = "2026-11-30"
ATM = 25600
CE, PE = f"NFO:NIFTY26D01{ATM}CE", f"NFO:NIFTY26D01{ATM}PE"


class Book:
    def __init__(self, ce: float = 100.0, pe: float = 95.0) -> None:
        self.px = {CE: ce, PE: pe}

    def get(self, s: str):
        p = self.px.get(s)
        return None if p is None else {"last_price": p, "bid": p - 0.5, "ask": p + 0.5}


class Decider:
    def __init__(self, *answers: dict) -> None:
        self.answers, self.seen = list(answers), []

    def decide(self, state: dict) -> dict:
        self.seen.append(state)
        return self.answers.pop(0) if self.answers else {"action": "WAIT", "day_type": "range", "confidence": "low"}


def t(hm: str, sec: int = 5) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hm}:{sec:02d}+05:30")


def bars_until(hm: str, path=None, day: str = DAY) -> list[dict]:
    """Closed 1-min candles 09:15 .. hm (exclusive). path(minute index) -> close; flat 25600 by default."""
    out, m = [], 9 * 60 + 15
    i = 0
    while f"{m // 60:02d}:{m % 60:02d}" < hm:
        c = path(i) if path else 25600.0
        out.append({"t": f"{day} {m // 60:02d}:{m % 60:02d}", "o": c, "h": c + 2, "l": c - 2, "c": c})
        m += 1
        i += 1
    return out


def feed(hm: str, path=None, **kw) -> dict:
    return {"atm": ATM, "ce_symbol": CE, "pe_symbol": PE, "ce": 100.0, "pe": 95.0, "vix": 13.0, "vix_chg": 0.1,
            "expiry": "2026-12-01", "bars_today": bars_until(hm, path),
            "prev_day": {"open": 25500.0, "high": 25650.0, "low": 25450.0, "close": 25590.0}, **kw}


def mk(tmp_path: Path, *answers: dict, name: str = "v2.jsonl") -> PaperLlmBuyV2:
    return PaperLlmBuyV2(path=tmp_path / name, decider=Decider(*answers), macro=lambda: {"es_futures_pct": 0.4})


def settle(b: PaperLlmBuyV2, hm: str, f: dict, book: Book) -> dict | None:
    """Run frames in the same minute until the threaded answer is applied."""
    for _ in range(200):
        if b.asking is None:
            return None
        ev = b.on_frame(now=t(hm, 30), feed={}, book=book)
        if ev is not None:
            return ev
        time.sleep(0.005)
    raise AssertionError("no answer")


def ledger(b: PaperLlmBuyV2) -> list[dict]:
    return [json.loads(x) for x in b.path.read_text().splitlines() if x.strip()]


BUY_CE = {"action": "BUY_CE", "day_type": "trend_up", "confidence": "high", "thesis": "holding above VWAP",
          "invalidation_nifty": 25570, "horizon_min": 40}


def test_asks_at_a_check_time_and_buys_with_code_owned_levels(tmp_path: Path) -> None:
    b = mk(tmp_path, BUY_CE)
    assert b.on_frame(now=t("09:29"), feed=feed("09:29"), book=Book()) is None and b.asking is None  # before 09:30
    assert b.on_frame(now=t("09:30"), feed=feed("09:30"), book=Book()) is None and b.asking is not None
    ev = settle(b, "09:30", feed("09:30"), Book())
    assert ev and ev["event"] == "open" and ev["leg"] == "ce" and ev["symbol"] == CE and ev["fill"] == 100.5
    assert ev["invalidation"] == 25570.0 and ev["horizon_min"] == 40 and ev["day_type"] == "trend_up"
    kinds = [e["event"] for e in ledger(b)]
    assert kinds == ["ask", "decision", "open"]
    seen = b.decider.seen[0]
    assert seen["trigger"] == ["check_09:30"] and seen["nifty"]["opening_range_0915_0929"]
    assert seen["previous_day"]["close"] == 25590.0 and seen["overnight_and_macro"] == {"es_futures_pct": 0.4}
    assert seen["history_card"]["slot"] == "09:30" and "next_60m" in seen["history_card"]


def test_invalidation_is_clamped_and_wrong_side_gets_the_default(tmp_path: Path) -> None:
    near = mk(tmp_path, {**BUY_CE, "invalidation_nifty": 25595}, name="a.jsonl")  # 5 pts: widened to 20
    near.on_frame(now=t("09:30"), feed=feed("09:30"), book=Book())
    assert settle(near, "09:30", {}, Book())["invalidation"] == 25580.0
    wrong = mk(tmp_path, {**BUY_CE, "invalidation_nifty": 25700, "horizon_min": 500}, name="b.jsonl")
    wrong.on_frame(now=t("09:30"), feed=feed("09:30"), book=Book())
    ev = settle(wrong, "09:30", {}, Book())
    assert ev["invalidation"] == 25570.0 and ev["horizon_min"] == 120  # 30-pt default; horizon capped


def test_low_confidence_waits_and_done_for_day_stops_asking(tmp_path: Path) -> None:
    b = mk(tmp_path, {**BUY_CE, "confidence": "low"}, {"action": "DONE_FOR_DAY", "day_type": "range", "confidence": "high"})
    b.on_frame(now=t("09:30"), feed=feed("09:30"), book=Book())
    assert settle(b, "09:30", {}, Book()) is None and b.position is None
    b.on_frame(now=t("10:00"), feed=feed("10:00"), book=Book())
    settle(b, "10:00", {}, Book())
    assert b.done_for_day
    b.on_frame(now=t("10:45"), feed=feed("10:45"), book=Book())
    assert b.asking is None and b.calls == 2


def test_setup_triggers_fire_once_and_respect_the_cooldown(tmp_path: Path) -> None:
    b = mk(tmp_path)
    up = lambda i: 25600.0 + (60.0 if i >= 20 else 0.0)  # 09:35 candle breaks the opening range high
    b.on_frame(now=t("09:36"), feed=feed("09:36", up), book=Book())
    settle(b, "09:36", {}, Book())
    assert b.decider.seen[0]["trigger"] == ["or_high_break"]
    b.on_frame(now=t("09:38"), feed=feed("09:38", up), book=Book())  # inside the 5-min cooldown
    assert b.asking is None and b.calls == 1


def test_code_exits_invalidation_trail_hard_stop_timeout_and_1510(tmp_path: Path) -> None:
    def opened(name: str) -> PaperLlmBuyV2:
        b = mk(tmp_path, BUY_CE, name=name)
        b.on_frame(now=t("09:30"), feed=feed("09:30"), book=Book())
        settle(b, "09:30", {}, Book())
        assert b.position is not None
        return b

    inv = opened("i.jsonl")
    down = lambda i: 25600.0 if i < 20 else 25560.0
    assert inv.on_frame(now=t("09:36"), feed=feed("09:36", down), book=Book())["reason"] == "invalidated"

    tr = opened("t.jsonl")
    assert tr.on_frame(now=t("09:31", 10), feed={}, book=Book(ce=140.0)) is None  # bid 139.5: armed (+39%)
    ev = tr.on_frame(now=t("09:31", 20), feed={}, book=Book(ce=119.0))  # half of the gain given back
    assert ev["reason"] == "trail_stop" and ev["exit"] == 118.5

    hs = opened("h.jsonl")
    assert hs.on_frame(now=t("09:31", 10), feed={}, book=Book(ce=65.0))["reason"] == "hard_stop"

    to = opened("o.jsonl")
    assert to.on_frame(now=t("10:05"), feed=feed("10:05"), book=Book(ce=100.0)) is None  # 35 min: horizon not yet
    assert to.on_frame(now=t("10:11"), feed=feed("10:11"), book=Book(ce=100.0))["reason"] == "thesis_timeout"

    eod = opened("e.jsonl")
    assert eod.on_frame(now=t("15:10", 0), feed={}, book=Book(ce=104.0))["reason"] == "exit_1510"


def test_limits_holiday_errors_and_ask_cap(tmp_path: Path) -> None:
    hol = mk(tmp_path, name="hol.jsonl")
    old = feed("09:30")
    old["bars_today"] = bars_until("15:30", day="2026-11-27")
    hol.on_frame(now=t("09:30"), feed=old, book=Book())
    assert hol.asking is None and not hol.path.exists()

    err = mk(tmp_path, {"error": "HTTPStatusError:402"}, {"error": "x"}, {"error": "y"}, name="err.jsonl")
    for hm in ("09:30", "10:00", "10:45"):
        err.on_frame(now=t(hm), feed=feed(hm), book=Book())
        settle(err, hm, {}, Book())
    err.on_frame(now=t("11:30"), feed=feed("11:30"), book=Book())
    assert err.asking is None and err.last_reject == "LLM unavailable today"

    cap = mk(tmp_path, name="cap.jsonl")
    cap._new_day(DAY)
    cap.calls = MAX_CALLS_DAY
    cap.on_frame(now=t("09:30"), feed=feed("09:30"), book=Book())
    assert cap.asking is None


def test_restart_restores_asks_fired_triggers_and_the_open_trade(tmp_path: Path) -> None:
    b = mk(tmp_path, BUY_CE)
    b.on_frame(now=t("09:30"), feed=feed("09:30"), book=Book())
    settle(b, "09:30", {}, Book())
    again = mk(tmp_path)
    again._new_day(DAY)
    assert again.calls == 1 and "check_09:30" in again.fired and again.trades == 1
    assert again.position is not None and again.position["invalidation"] == 25570.0 and again.position["horizon_min"] == 40
    down = lambda i: 25600.0 if i < 20 else 25560.0
    assert again.on_frame(now=t("09:36"), feed=feed("09:36", down), book=Book())["reason"] == "invalidated"


def test_helpers_parse_history_and_candles() -> None:
    a = parse_answer('sure: {"action": "buy_pe", "day_type": "nonsense", "confidence": "HIGH", "invalidation_nifty": "25650"}')
    assert a["action"] == "BUY_PE" and a["day_type"] == "unclear" and a["confidence"] == "high" and a["invalidation_nifty"] == 25650.0
    h = load_history()
    card = history_card(h, "14:10", 1.2, None, False, 26000.0)
    assert card["slot"] == "14:00" and card["move_from_open_bucket"] == ">= +1%" and card["days"] >= 20
    assert card["move_threshold_pts_now"] == 16
    gap = history_card(h, "10:05", -0.1, 0.9, True, 26000.0)
    assert 0 < gap["gap_not_filled_yet"]["share_filled_by_1510"] < 1
    assert candles_5m(bars_until("09:25"))[:2] == ["09:15 25600/25602/25598/25600", "09:20 25600/25602/25598/25600"]


def test_a_late_answer_to_an_abandoned_ask_is_never_used_for_the_next_one(tmp_path: Path) -> None:
    b = mk(tmp_path)
    b.on_frame(now=t("09:30"), feed=feed("09:30"), book=Book())
    settle(b, "09:30", {}, Book())  # first ask answered (WAIT)
    b.on_frame(now=t("10:00"), feed=feed("10:00"), book=Book())
    first_id = b.asking["id"]
    for _ in range(200):  # let the thread finish, then drop its answer and fake a late one from the old ask
        if b._answer is not None:
            break
        time.sleep(0.005)
    b._answer = (first_id - 1, dict(BUY_CE))  # an answer that belongs to an earlier ask
    assert b.on_frame(now=t("10:00", 30), feed={}, book=Book()) is None and b.position is None and b.asking is not None
    b._answer = (first_id, {"action": "WAIT", "day_type": "range", "confidence": "high"})
    assert b.on_frame(now=t("10:00", 40), feed={}, book=Book()) is None and b.asking is None


def test_a_slow_macro_fetch_does_not_hold_the_ask(tmp_path: Path, monkeypatch) -> None:
    import atlas_lite.paper_llm_buy_v2 as v2
    monkeypatch.setattr(v2, "MACRO_WAIT_S", 0.05)
    b = PaperLlmBuyV2(path=tmp_path / "m.jsonl", decider=Decider(BUY_CE), macro=lambda: time.sleep(1) or {"x": 1})
    b.on_frame(now=t("09:30"), feed=feed("09:30"), book=Book())
    ev = settle(b, "09:30", {}, Book())
    assert ev and ev["event"] == "open" and "overnight_and_macro" not in b.decider.seen[0]


class Flag:
    def __init__(self, on: bool = True) -> None:
        self.on = on

    def is_enabled(self) -> bool:
        return self.on


def test_switch_off_stops_asks_drops_a_pending_answer_and_sells_the_open_trade(tmp_path: Path) -> None:
    flag = Flag(True)
    b = PaperLlmBuyV2(path=tmp_path / "s.jsonl", decider=Decider(BUY_CE, BUY_CE), macro=dict, paper_flag=flag)
    b.on_frame(now=t("09:30"), feed=feed("09:30"), book=Book())
    settle(b, "09:30", {}, Book())
    assert b.position is not None and b.snapshot(Book())["paper_on"] is True
    flag.on, b._flag_cache = False, (0.0, True)  # switched off on the card
    ev = b.on_frame(now=t("09:40"), feed={}, book=Book(ce=104.0))
    assert ev and ev["reason"] == "switched_off" and b.position is None
    b.on_frame(now=t("10:00"), feed=feed("10:00"), book=Book())
    assert b.asking is None and b.last_reject == "switched off" and b.snapshot(Book())["paper_on"] is False
    flag.on, b._flag_cache = True, (0.0, False)
    b.on_frame(now=t("10:45"), feed=feed("10:45"), book=Book())  # back on: asks again
    assert b.asking is not None
    flag.on, b._flag_cache = False, (0.0, True)  # off while the answer is on its way: never traded
    time.sleep(0.05)
    assert b.on_frame(now=t("10:45", 40), feed={}, book=Book()) is None and b.asking is None and b.position is None


def test_switch_api_needs_sign_in_to_turn_on_and_never_to_turn_off(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import atlas_lite.main as main

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    client = TestClient(main.app)
    assert client.get("/api/llm_buy_v2/paper").json()["paper"]["enabled"] is True  # on when never set
    r = client.post("/api/llm_buy_v2/paper?enabled=false")
    assert r.status_code == 200 and r.json()["paper"]["enabled"] is False
    assert (tmp_path / "llm_buy_v2_paper.json").is_file()
    assert client.post("/api/llm_buy_v2/paper?enabled=true").status_code == 401
    assert client.get("/api/llm_buy_v2/paper").json()["paper"]["enabled"] is False
