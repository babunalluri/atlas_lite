"""LLM option buyer: paper and live flows with a scripted decider and a fake Kite client."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite import llm_buyer as M
from atlas_lite.llm_buyer import LlmBuyer, LlmDecider

IST = ZoneInfo("Asia/Kolkata")
DAY = "2026-11-11"  # Wednesday, a trading day
CE, PE = "NFO:NIFTY26N1724000CE", "NFO:NIFTY26N1724000PE"


def t(hm: str, day: str = DAY) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


class Book:
    def __init__(self, ce: float = 100.0, pe: float = 90.0) -> None:
        self.px = {CE: ce, PE: pe}

    def get(self, s: str):
        p = self.px.get(s)
        return None if p is None else {"last_price": p, "bid": p - 0.5, "ask": p + 0.5}


def feed(spot: float = 24000.0) -> dict:
    return {"spot": spot, "atm": 24000, "ce_symbol": CE, "pe_symbol": PE, "ce": 100.0, "pe": 90.0, "expiry": "2026-11-17",
            "vix": 14.0, "vix_chg": 0.2, "iv": 13.0, "iv_day_high": 13.5, "iv_day_low": 12.8, "ivp": 40, "pcr": 1.05,
            "oi_vs_day_high": 99.0, "index_nifty_chg": 0.2}


def book_at(tmp_path: Path, **kw) -> LlmBuyer:
    return LlmBuyer(path=tmp_path / "llm.jsonl", lot_size=65, **kw)


def frame(b: LlmBuyer, hm: str, q: Book, day: str = DAY):
    return b.on_frame(now=t(hm, day), feed=feed(), book=q)


def ledger(b: LlmBuyer) -> list[dict]:
    return [json.loads(x) for x in b.path.read_text().splitlines() if x.strip()]


def test_decider_parses_and_fails_over_to_openai() -> None:
    class Creds:
        api_key, base_url = "k1", "https://openrouter.ai/api/v1"
        openai_api_key, openai_base_url = "k2", "https://api.openai.com/v1"

    calls = []

    class Resp:
        def __init__(self, code, content):
            self.status_code, self.content = code, content

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"http {self.status_code}")

        def json(self):
            return {"choices": [{"message": {"content": self.content}}]}

    def post(url, **kw):
        calls.append((url, kw["json"]["model"]))
        if "openrouter" in url:
            return Resp(402, "")
        return Resp(200, 'Sure: {"action": "buy_pe", "reason": "new lows"}')

    out = LlmDecider(Creds(), post=post).decide({"time": "10:40"})
    assert out == {"action": "BUY_PE", "reason": "new lows", "provider": "openai", "model": "gpt-4.1"}
    assert [m for _, m in calls] == ["openai/gpt-4.1", "gpt-4.1"]
    assert LlmDecider(None).decide({})["error"] == "no_llm_credentials"


def test_paper_entry_exit_and_decision_schedule(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "09:19", q)
    assert not b.decision_due(t("09:19")) and b.decision_due(t("09:20")) and not b.decision_due(t("09:21"))
    frame(b, "10:40", q)
    ev = b.apply(t("10:40"), {"hm": "10:40", "action": "BUY_CE", "reason": "breakout"}, q)
    assert ev["event"] == "open" and ev["leg"] == "ce" and ev["fill"] == 100.5 and ev["qty"] == 65
    assert b.apply(t("10:45"), {"hm": "10:45", "action": "BUY_PE"}, q) is None  # a buy while holding is ignored
    assert "position" in b.state(t("10:45"), q) and b.decision_due(t("10:50"))
    q.px[CE] = 130.0
    close = b.apply(t("11:00"), {"hm": "11:00", "action": "EXIT", "reason": "target"}, q)
    assert close["event"] == "close" and close["exit"] == 129.5 and close["pnl_gross"] == round(29.0 * 65, 2)
    assert b.position is None and b.day_realized == close["pnl"]
    assert [e["action"] for e in ledger(b) if e["event"] == "decision"] == ["BUY_CE", "BUY_PE", "EXIT"]


def test_day_loss_stop_is_code_enforced_and_ends_entries(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_PE", "reason": "down"}, q)
    q.px[PE] = 70.0  # bid 69.5 vs fill 90.5: -21 x 65 = -1,365 -> above the stop
    assert frame(b, "10:01", q) is None and b.position is not None
    q.px[PE] = 60.0  # -31 x 65 = -2,015 less charges -> stop
    ev = frame(b, "10:02", q)
    assert ev["event"] == "close" and ev["reason"] == "day_loss_stop" and b.stopped == "day_loss"
    assert not b.decision_due(t("10:05"))
    assert b.apply(t("10:05"), {"hm": "10:05", "action": "BUY_CE"}, q) is None and b.position is None


def test_max_entries_done_for_day_errors_and_square_off(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "09:30", q)
    assert b.apply(t("09:30"), {"hm": "09:30", "action": "", "error": "openai:ReadTimeout:"}, q) is None
    assert b.position is None and ledger(b)[-1]["action"] == "ERROR"
    for hm in ("09:35", "09:45"):
        frame(b, hm, q)
        b.apply(t(hm), {"hm": hm, "action": "BUY_CE"}, q)
        b.apply(t(hm), {"hm": hm, "action": "EXIT"}, q)
    assert b.entries == 2 and not b.decision_due(t("10:00"))
    b2, q2 = book_at(tmp_path / "x"), Book()
    frame(b2, "10:00", q2)
    b2.apply(t("10:00"), {"hm": "10:00", "action": "DONE_FOR_DAY"}, q2)
    assert b2.stopped == "done_for_day" and not b2.decision_due(t("10:05"))
    b3, q3 = book_at(tmp_path / "y"), Book()
    frame(b3, "14:25", q3)
    b3.apply(t("14:25"), {"hm": "14:25", "action": "BUY_CE"}, q3)
    assert b3.apply(t("14:35"), {"hm": "14:35", "action": "BUY_CE"}, q3) is None or b3.entries == 1
    ev = frame(b3, "15:05", q3)
    assert ev["event"] == "close" and ev["reason"] == "square_off"


def test_restart_restores_position_decisions_and_day_total(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    b.apply(t("10:05"), {"hm": "10:05", "action": "EXIT"}, q)
    frame(b, "10:10", q)
    b.apply(t("10:10"), {"hm": "10:10", "action": "BUY_PE"}, q)
    import atlas_lite.llm_buyer as mod
    real_now = mod.datetime

    class FakeDT(real_now):
        @classmethod
        def now(cls, tz=None):
            return t("10:12")

    mod.datetime = FakeDT
    try:
        b2 = book_at(tmp_path)
    finally:
        mod.datetime = real_now
    assert b2.position is not None and b2.position.leg == "pe" and b2.entries == 2
    assert {"10:00", "10:05", "10:10"} <= b2.decided and b2.day_realized == b.day_realized


class FakeKite:
    def __init__(self) -> None:
        self.orders: list[dict] = []

    def equity_available(self) -> float:
        return 50_000.0

    def place_market(self, *, symbol, transaction_type, qty, tag):
        oid = f"o{len(self.orders) + 1}"
        self.orders.append({"order_id": oid, "tag": tag, "tradingsymbol": symbol.split(":", 1)[1], "product": "MIS",
                            "transaction_type": transaction_type, "quantity": qty, "status": "OPEN",
                            "filled_quantity": 0, "average_price": 0})
        return oid

    def fill_all(self, px: float) -> None:
        for o in self.orders:
            if o["status"] == "OPEN":
                o.update(status="COMPLETE", filled_quantity=o["quantity"], average_price=px)

    def list_orders(self):
        return [dict(o) for o in self.orders]

    def cancel(self, oid):
        pass


class Flag:
    def is_enabled(self) -> bool:
        return True

    def snapshot(self) -> dict:
        return {"enabled": True, "allowed": True}


def test_live_single_leg_buy_llm_exit_and_close(tmp_path: Path) -> None:
    kite, q = FakeKite(), Book()
    b = book_at(tmp_path, order_client=kite, live_flag=Flag())
    frame(b, "10:40", q)
    start = b.apply(t("10:40"), {"hm": "10:40", "action": "BUY_PE", "reason": "new lows"}, q)
    assert start["event"] == "live_start" and start["legs"] == ["pe"] and b.entries == 1
    assert len(kite.orders) == 1 and kite.orders[0]["tag"].startswith("llm") and kite.orders[0]["tradingsymbol"].endswith("PE")
    kite.fill_all(90.0)
    frame(b, "10:41", q)
    assert b.live.phase == "holding" and any(e["event"] == "open" and e.get("live") for e in ledger(b))
    assert b.decision_due(t("10:45"))
    q.px[PE] = 110.0
    b.apply(t("10:45"), {"hm": "10:45", "action": "EXIT", "reason": "bounce"}, q)
    frame(b, "10:45", q)
    sells = [o for o in kite.orders if o["transaction_type"] == "SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == 65
    kite.fill_all(109.0)
    ev = frame(b, "10:46", q)
    assert ev["event"] == "close" and ev["live"] and ev["pnl_gross"] == round(19.0 * 65, 2)
    assert b.live is None and b.month_live_pnl[DAY[:7]] == ev["pnl"] and b.day_realized == ev["pnl"]


def test_live_day_loss_stop_sells_through_the_live_layer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas_lite.live_straddle.STOP_CONFIRM_S", 0.0)
    kite, q = FakeKite(), Book()
    b = book_at(tmp_path, order_client=kite, live_flag=Flag())
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    kite.fill_all(100.0)
    frame(b, "10:01", q)
    q.px[CE] = 68.0  # bid 67.5: -32.5 x 65 = -2,113 -> beyond the -2,000 budget
    frame(b, "10:02", q)
    frame(b, "10:03", q)
    assert [o["transaction_type"] for o in kite.orders] == ["BUY", "SELL"]
    kite.fill_all(67.5)
    ev = frame(b, "10:04", q)
    assert ev["reason"] == "day_loss_stop" and b.stopped == "day_loss" and not b.decision_due(t("10:05"))


def test_live_month_limit_and_funds_skip(tmp_path: Path) -> None:
    kite, q = FakeKite(), Book()
    b = book_at(tmp_path, order_client=kite, live_flag=Flag())
    b.month_live_pnl[DAY[:7]] = -M.MONTH_LOSS_LIMIT
    frame(b, "10:00", q)
    ev = b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    assert ev["event"] == "live_skip" and ev["why"] == "month_loss_limit" and not kite.orders
    kite.equity_available = lambda: 1_000.0
    b.month_live_pnl.clear()
    frame(b, "10:05", q)
    ev = b.apply(t("10:05"), {"hm": "10:05", "action": "BUY_CE"}, q)
    assert ev["event"] == "live_skip" and ev["why"].startswith("insufficient_funds") and not kite.orders


class SwitchFlag:
    def __init__(self) -> None:
        self.on = True

    def is_enabled(self) -> bool:
        return self.on

    def snapshot(self) -> dict:
        return {"enabled": self.on, "allowed": True}


def test_unticking_the_box_sells_the_live_position_and_stops_for_the_day(tmp_path: Path) -> None:
    kite, q, flag = FakeKite(), Book(), SwitchFlag()
    b = book_at(tmp_path, order_client=kite, live_flag=flag)
    frame(b, "10:40", q)
    b.apply(t("10:40"), {"hm": "10:40", "action": "BUY_CE", "reason": "breakout"}, q)
    kite.fill_all(100.0)
    frame(b, "10:41", q)
    assert b.live.phase == "holding"
    flag.on = False  # operator unticks "Live LLM buyer"
    frame(b, "10:42", q)
    sells = [o for o in kite.orders if o["transaction_type"] == "SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == 65 and b.stopped == "ui_off"
    kite.fill_all(101.0)
    ev = frame(b, "10:43", q)
    assert ev["event"] == "close" and ev["live"] and ev["reason"] == "ui_off"
    assert not b.decision_due(t("10:45"))  # no more trades today, paper included
    assert b.apply(t("10:45"), {"hm": "10:45", "action": "BUY_PE"}, q) is None and not b.position
    assert any(e["event"] == "stop_entries" and e["why"] == "ui_off" for e in ledger(b))


def test_box_never_ticked_today_does_not_stop_paper(tmp_path: Path) -> None:
    flag = SwitchFlag()
    flag.on = False
    b, q = book_at(tmp_path, order_client=FakeKite(), live_flag=flag), Book()
    frame(b, "10:00", q)
    assert not b.stopped and b.decision_due(t("10:00"))
    assert b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)["event"] == "open"  # paper


def test_no_second_entry_without_day_loss_budget_and_live_stop_never_zero(tmp_path: Path, monkeypatch) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:00", q)
    b.day_realized = -1_950.0  # a first trade the LLM closed near the limit
    assert b.apply(t("10:05"), {"hm": "10:05", "action": "BUY_CE"}, q) is None and b.stopped == "day_loss"
    monkeypatch.setattr("atlas_lite.live_straddle.STOP_CONFIRM_S", 0.0)
    kite = FakeKite()
    b2 = book_at(tmp_path / "live", order_client=kite, live_flag=Flag())
    frame(b2, "10:00", q)
    b2.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    kite.fill_all(100.0)
    frame(b2, "10:01", q)
    b2.day_realized = -2_500.0  # budget already spent (e.g. restored totals): the next lower bid must sell
    q.px[CE] = 99.0
    frame(b2, "10:02", q)
    frame(b2, "10:03", q)
    assert [o["transaction_type"] for o in kite.orders] == ["BUY", "SELL"]


def test_session_end_auto_off_is_not_a_kill(tmp_path: Path) -> None:
    flag = SwitchFlag()
    b, q = book_at(tmp_path, order_client=FakeKite(), live_flag=flag), Book()
    frame(b, "15:20", q)
    flag.on = False  # the engine's 15:30 auto-off
    frame(b, "15:30", q)
    assert b.stopped == "" and not any(e.get("why") == "ui_off" for e in (ledger(b) if b.path.exists() else []))


def test_restore_with_no_orders_yet_and_revives_every_written_off_buy() -> None:
    from atlas_lite.live_straddle import LiveStraddle

    start = {"day": DAY, "atm": 24000, "pe_symbol": PE, "qty": 65, "legs": ["pe"], "tag_prefix": "llm"}
    ls = LiveStraddle.restore(start, [], lambda e: e)  # crash right after live_start: used to raise
    assert ls.intents == [] and ls.legs == ("pe",) and ls.tag_prefix == "llm"
    ev = []
    for n in (1, 2):
        tag = f"llm1111{n:02d}peb"
        ev += [{"event": "order_intent", "tag": tag, "leg": "pe", "side": "BUY", "qty": 65, "symbol": PE, "seq": n},
               {"event": "order_sent", "tag": tag, "order_id": "", "send": "uncertain", "returned_at": 1.0},
               {"event": "order_final", "tag": tag, "status": "NOT_PLACED", "filled": 0, "avg": 0.0}]
    ls = LiveStraddle.restore(start, ev, lambda e: e)
    assert [i.status for i in ls.intents] == ["SENT", "SENT"]  # both looked for again, not just the last


def test_new_day_never_uses_yesterdays_feed(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "15:00", q, day="2026-11-09")
    assert b.last_feed.get("ce_symbol") == CE
    b.on_frame(now=t("09:25"), feed={"error": "Notebook nifty not enabled"}, book=q)  # next day, no feed yet
    assert b.last_feed == {} and not b.decision_due(t("09:25"))
    assert b.apply(t("09:25"), {"hm": "09:25", "action": "BUY_CE"}, q) is None and b.last_reject == "stale_feed"
    frame(b, "09:29", q)
    assert b.decision_due(t("09:30"))  # fresh feed again (1 minute old)
    assert not b.decision_due(t("09:35"))  # 6 minutes without a feed: stale


def test_live_buy_not_sent_when_ledger_write_fails(tmp_path: Path) -> None:
    kite, q = FakeKite(), Book()
    b = book_at(tmp_path, order_client=kite, live_flag=Flag())
    frame(b, "10:00", q)
    real = b._append
    b._append = lambda e: None if e.get("event") == "order_intent" else real(e)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    assert not kite.orders and b.last_reject == "ledger_write_failed"


def test_engine_abandons_a_hung_llm_call_and_asks_again(monkeypatch) -> None:
    import asyncio
    import threading
    from types import SimpleNamespace

    from atlas_lite import feed_engine as fe

    monkeypatch.setattr(fe, "LLM_BUYER_HUNG_S", 0.5)
    release = threading.Event()
    asked, applied = [], []

    class Decider:
        def decide(self, state):
            asked.append(state)
            if len(asked) == 1:
                release.wait(5)  # the first call hangs
            return {"action": "WAIT", "reason": "ok"}

    class FakeBook:
        strategy, live, position = "llm_buyer", None, object()  # "holding": skips the weekend idle path
        decider, decided = Decider(), set()

        def on_frame(self, **kw):
            return None

        def decision_due(self, now):
            return len(asked) < 2

        def state(self, now, book):
            return {}

        def apply(self, now, out, book, allow_entry=True):
            applied.append(out)

    eng = SimpleNamespace(_llm_buyer=FakeBook(), notebooks={}, book=None, _agent_gates=None, _llm_buyer_live=None,
                          _log=SimpleNamespace(warning=lambda *a: None, info=lambda *a: None),
                          build_feed=lambda nb: {})

    async def run():
        task = asyncio.create_task(fe.FeedEngine._llm_buyer_loop(eng))
        await asyncio.sleep(3.5)
        task.cancel()
        release.set()

    asyncio.run(run())
    assert len(asked) == 2  # asked again after abandoning the hung call
    assert applied[0]["error"] == "llm_hung"


def test_three_llm_errors_in_a_row_while_holding_sell(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    err = {"action": "", "error": "openai:ReadTimeout:"}
    assert b.apply(t("10:05"), {"hm": "10:05", **err}, q) is None
    assert b.apply(t("10:10"), {"hm": "10:10", "action": "HOLD"}, q) is None  # a valid answer resets the count
    assert b.apply(t("10:15"), {"hm": "10:15", **err}, q) is None
    assert b.apply(t("10:20"), {"hm": "10:20", **err}, q) is None and b.position is not None
    ev = b.apply(t("10:25"), {"hm": "10:25", **err}, q)
    assert ev["event"] == "close" and ev["reason"] == "llm_unavailable" and b.position is None
    assert b.apply(t("10:30"), {"hm": "10:30", **err}, q) is None  # flat: errors never trade
