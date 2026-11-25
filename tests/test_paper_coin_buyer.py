"""Coin-flip control book: the LLM buyer's engine and rules, a coin deciding, profit stops armed at +5% (paper only)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from atlas_lite import llm_buyer as M
from atlas_lite import paper_coin_buyer as C
from atlas_lite.paper_coin_buyer import CoinBuyer, CoinDecider

DAY = "2026-11-11"
CE = "NFO:NIFTY26N1724000CE"
PE = "NFO:NIFTY26N1724000PE"


@pytest.fixture(autouse=True)
def _rules_off(monkeypatch):
    monkeypatch.setattr(M, "DAY_DIRECTION_RULE", False)
    monkeypatch.setattr(M, "LOSS_PAUSE_MIN", 0)


class Fixed:
    """A 'random' source that returns the given numbers in turn."""

    def __init__(self, *xs: float) -> None:
        self.xs = list(xs)

    def random(self) -> float:
        return self.xs.pop(0)


class Book:
    def __init__(self, ce: float = 100.0, pe: float = 90.0) -> None:
        self.px = {CE: ce, PE: pe}

    def get(self, s: str):
        p = self.px.get(s)
        return None if p is None else {"bid": p - 0.5, "ask": p + 0.5, "last_price": p}


def feed() -> dict:
    return {"spot": 24000.0, "atm": 24000, "ce_symbol": CE, "pe_symbol": PE, "ce": 100.0, "pe": 90.0,
            "expiry": "2026-11-17", "vix": 14.0, "iv": 13.0}


def t(hms: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hms}+05:30")


def test_coin_decider_entries_and_exits() -> None:
    assert CoinDecider(Fixed(0.05)).decide({"allowed_entry": "calls only"})["action"] == "BUY_CE"
    assert CoinDecider(Fixed(0.05)).decide({"allowed_entry": "puts only"})["action"] == "BUY_PE"
    assert CoinDecider(Fixed(0.5)).decide({"allowed_entry": "calls only"})["action"] == "WAIT"
    assert CoinDecider(Fixed(0.7, 0.05)).decide({"allowed_entry": "calls or puts"})["action"] == "BUY_PE"
    assert CoinDecider(Fixed(0.05)).decide({"allowed_entry": "no new trades"})["action"] == "WAIT"
    held = {"position": {"option": "CE"}}
    assert CoinDecider(Fixed(0.05)).decide(held)["action"] == "EXIT"
    assert CoinDecider(Fixed(0.5)).decide(held)["action"] == "HOLD"
    out = CoinDecider(Fixed(0.5)).decide(held)
    assert out["provider"] == "coin" and out["model"] == "coin"


def test_coin_book_is_paper_only_with_its_own_ledger(tmp_path: Path) -> None:
    class Flag:
        def is_enabled(self):
            return True

    b = CoinBuyer(path=tmp_path / C.LEDGER_NAME, lot_size=65, decider=CoinDecider(), strategy=C.STRATEGY,
                  name=C.NAME, order_client=object(), live_flag=Flag())
    assert not b.live_enabled()  # never live, even with a client and a ticked flag
    q = Book()
    b.on_frame(now=t("10:00:01"), feed=feed(), book=q)
    ev = b.apply(t("10:00:02"), {"hm": "10:00", "action": "BUY_CE"}, q)
    assert ev["event"] == "open" and ev["strategy"] == "coin_buyer" and ev["qty"] == 130 and not ev.get("live")
    lines = [json.loads(x) for x in (tmp_path / C.LEDGER_NAME).read_text().splitlines()]
    assert all(x["strategy"] == "coin_buyer" for x in lines)


def test_coin_book_arms_profit_stops_at_five_percent(tmp_path: Path) -> None:
    b = CoinBuyer(path=tmp_path / "c.jsonl", lot_size=65, decider=CoinDecider(), strategy=C.STRATEGY, name=C.NAME)
    q = Book()
    b.on_frame(now=t("10:00:01"), feed=feed(), book=q)
    b.apply(t("10:00:02"), {"hm": "10:00", "action": "BUY_CE"}, q)  # fill at the ask 100.5
    q.px[CE] = 106.5  # bid 106: +5.5% -> both stops armed (the LLM buyer would need +10%)
    b.on_frame(now=t("10:01:00"), feed=feed(), book=q)
    assert b.position.lot1_high == 106.0 and b.position.lot2_high == 106.0
    q.px[CE] = 104.3  # bid 103.8 <= 106 x 0.98: lot 1 sold by its trail
    ev = b.on_frame(now=t("10:01:30"), feed=feed(), book=q)
    assert ev["event"] == "partial_close" and ev["reason"] == "lot_trailing_stop" and b.position.qty == 65
    q.px[CE] = 103.8  # bid 103.3 <= 100.5 + 5.5 x 0.6 = 103.8: 40% of the best gain given back
    ev = b.on_frame(now=t("10:02:00"), feed=feed(), book=q)
    assert ev["event"] == "close" and ev["reason"] == "lot2_giveback" and ev["pnl_gross"] > 0


def test_llm_buyer_still_arms_at_ten_percent(tmp_path: Path) -> None:
    b = M.LlmBuyer(path=tmp_path / "l.jsonl", lot_size=65)
    q = Book()
    b.on_frame(now=t("10:00:01"), feed=feed(), book=q)
    b.apply(t("10:00:02"), {"hm": "10:00", "action": "BUY_CE"}, q)
    q.px[CE] = 106.5  # +5.5%: not armed in the LLM buyer
    b.on_frame(now=t("10:01:00"), feed=feed(), book=q)
    assert not b.position.lot1_high and not b.position.lot2_high
