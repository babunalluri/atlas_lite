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


# The mechanics tests (stops, kill switch, restarts, live orders) run on a wide day so their times read
# naturally; the book's own hours (09:16-10:15, every minute) are tested below with its defaults.
WIDE = dict(first_decision="09:20", last_entry="14:30", last_decision_at="15:00", exit_at="15:05",
            live_orders_until="15:10", decide_every_min=5, max_entries_if_positive=2, path_every_min=30,
            straddle_every_min=15)


def book_at(tmp_path: Path, **kw) -> LlmBuyer:
    return LlmBuyer(path=tmp_path / "llm.jsonl", lot_size=65, **{**WIDE, **kw})


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
        return Resp(200, 'Sure: {"action": "buy_pe", "reason": "new lows", "factors": ["new day low", "PCR 0.82"]}')

    out = LlmDecider(Creds(), post=post).decide({"time": "10:40"})  # OpenRouter 402 -> OpenAI, same model
    assert out["action"] == "BUY_PE" and out["provider"] == "openai" and out["model"] == "gpt-4.1"
    assert [m for _, m in calls] == ["openai/gpt-4.1", "gpt-4.1"] and out["inputs"] == {"time": "10:40"}
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
        self.on, self.source = True, "ui"

    def is_enabled(self) -> bool:
        return self.on

    def snapshot(self) -> dict:
        return {"enabled": self.on, "allowed": True, "source": self.source}


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
    flag.on, flag.source = False, "day_end"  # the engine's 15:30 auto-off
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
    assert b.entries == 0 and b.live is None  # the slot is given back
    b._append = real
    b2 = book_at(tmp_path, order_client=kite, live_flag=Flag())  # a restart honours the abort
    assert b2.live is None


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
        strategy, live, position, live_flag = "llm_buyer", None, object(), None  # "holding": skips the weekend idle
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
        task = asyncio.create_task(fe.FeedEngine._llm_buyer_loop(eng, eng._llm_buyer))
        await asyncio.sleep(3.5)
        task.cancel()
        release.set()

    asyncio.run(run())
    assert len(asked) == 2  # asked again after abandoning the hung call
    assert applied[0]["error"] == "llm_hung"


def test_llm_errors_in_a_row_while_holding_sell(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    err = {"action": "", "error": "openai:ReadTimeout:"}
    assert b.apply(t("10:01"), {"hm": "10:01", **err}, q) is None
    assert b.apply(t("10:02"), {"hm": "10:02", "action": "HOLD"}, q) is None  # a valid answer resets the count
    for i in range(M.MAX_ERRORS_HOLDING - 1):
        hm = f"10:{3 + i:02d}"
        assert b.apply(t(hm), {"hm": hm, **err}, q) is None and b.position is not None
    ev = b.apply(t("10:30"), {"hm": "10:30", **err}, q)
    assert ev["event"] == "close" and ev["reason"] == "llm_unavailable" and b.position is None
    assert b.apply(t("10:31"), {"hm": "10:31", **err}, q) is None  # flat: errors never trade
    assert M.MAX_ERRORS_HOLDING == 10  # ~10 minutes at one question a minute

def test_paper_open_needs_its_ledger_line(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:00", q)
    b._append = lambda e: None
    assert b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q) is None
    assert b.position is None and b.entries == 0 and b.last_reject == "ledger_write_failed"


def test_every_decision_keeps_its_inputs_and_factors(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:40", q)
    st = b.state(t("10:40"), q)
    b.apply(t("10:40"), {"hm": "10:40", "action": "BUY_PE", "reason": "lows", "factors": ["new day low"], "inputs": st}, q)
    ev = [e for e in ledger(b) if e["event"] == "decision"][-1]
    assert ev["inputs"]["pcr"] == 1.05 and ev["inputs"]["atm_strike"] == 24000 and ev["factors"] == ["new day low"]
    snap = b.snapshot(q)["last_decision"]
    assert snap["factors"] == ["new day low"] and snap["inputs"]["vix"] == 14.0
    b.apply(t("10:45"), {"hm": "10:45", "action": "", "error": "openai:ReadTimeout:", "inputs": st}, q)
    assert [e for e in ledger(b) if e["event"] == "decision"][-1]["inputs"]["time"] == "10:40"  # errors keep them too


def test_untick_after_1510_still_stops_the_book(tmp_path: Path) -> None:
    flag = SwitchFlag()
    b, q = book_at(tmp_path, order_client=FakeKite(), live_flag=flag), Book()
    frame(b, "15:11", q)
    flag.on = False  # the operator unticks late (source "ui")
    frame(b, "15:12", q)
    assert b.stopped == "ui_off"


def test_errors_in_a_row_survive_a_restart(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    for i in range(M.MAX_ERRORS_HOLDING - 1):
        hm = f"10:{5 + i:02d}"
        b.apply(t(hm), {"hm": hm, "action": "", "error": "openai:ReadTimeout:"}, q)
    import atlas_lite.llm_buyer as mod
    real_now = mod.datetime

    class FakeDT(real_now):
        @classmethod
        def now(cls, tz=None):
            return t("10:14")

    mod.datetime = FakeDT
    try:
        b2 = book_at(tmp_path)
    finally:
        mod.datetime = real_now
    assert b2.errors_in_row == M.MAX_ERRORS_HOLDING - 1 and b2.position is not None
    frame(b2, "10:15", q)
    ev = b2.apply(t("10:15"), {"hm": "10:15", "action": "", "error": "openai:ReadTimeout:"}, q)
    assert ev["event"] == "close" and ev["reason"] == "llm_unavailable"  # the third error, across the restart


def test_unusable_answers_count_as_errors_while_holding(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    for i in range(M.MAX_ERRORS_HOLDING - 1):  # not allowed while holding
        hm = f"10:{1 + i:02d}"
        act = ("SELL_ALL", "BUY_PE")[i % 2]
        assert b.apply(t(hm), {"hm": hm, "action": act}, q) is None and b.position is not None
    assert b.errors_in_row == M.MAX_ERRORS_HOLDING - 1 and ledger(b)[-1]["valid"] is False
    ev = b.apply(t("10:15"), {"hm": "10:15", "action": "MAYBE"}, q)
    assert ev["event"] == "close" and ev["reason"] == "llm_unavailable"


def test_restart_with_open_live_trade_and_box_already_off_sells(tmp_path: Path, monkeypatch) -> None:
    kite, q, flag = FakeKite(), Book(), SwitchFlag()
    b = book_at(tmp_path, order_client=kite, live_flag=flag)
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_PE", "reason": "lows"}, q)
    kite.fill_all(90.0)
    frame(b, "10:01", q)
    assert b.live.phase == "holding"
    flag.on = False  # the box is unticked while the engine is down
    import atlas_lite.llm_buyer as mod
    real_now = mod.datetime

    class FakeDT(real_now):
        @classmethod
        def now(cls, tz=None):
            return t("10:05")

    monkeypatch.setattr(mod, "datetime", FakeDT)
    b2 = book_at(tmp_path, order_client=kite, live_flag=flag)  # restart: the live trade is restored
    monkeypatch.setattr(mod, "datetime", real_now)
    assert b2.live is not None and b2.live.legs == ("pe",)
    frame(b2, "10:05", q)
    assert b2.stopped == "ui_off" and [o["transaction_type"] for o in kite.orders] == ["BUY", "SELL"]
    kite.fill_all(91.0)
    ev = frame(b2, "10:06", q)
    assert ev["event"] == "close" and ev["live"] and ev["reason"] == "ui_off"



def hour_book(tmp_path: Path, **kw) -> LlmBuyer:
    return LlmBuyer(path=tmp_path / "h.jsonl", lot_size=65, **kw)  # the book's own defaults


def test_hour_book_schedule_prompt_and_tags(tmp_path: Path) -> None:
    b, q = hour_book(tmp_path), Book()
    frame(b, "09:15", q)
    assert not b.decision_due(t("09:15")) and b.decision_due(t("09:16"))
    frame(b, "09:16", q)
    assert b.decision_due(t("09:17"))  # every minute
    frame(b, "14:30", q)
    assert b.decision_due(t("14:30"))
    frame(b, "14:31", q)
    assert not b.decision_due(t("14:31"))  # flat: no new trades after 14:30
    assert "New trades only until 14:30" in M.SYSTEM_PROMPT and "held until 15:05" in M.SYSTEM_PROMPT
    assert "up to four times while today's realised P&L is positive" in M.SYSTEM_PROMPT
    kite = FakeKite()
    b2 = hour_book(tmp_path / "live", order_client=kite, live_flag=Flag())
    frame(b2, "09:20", q)
    b2.apply(t("09:20"), {"hm": "09:20", "action": "BUY_PE"}, q)
    assert kite.orders[0]["tag"].startswith("llm")  # its order tags


def test_an_open_trade_is_managed_after_the_entry_hour(tmp_path: Path) -> None:
    b, q = hour_book(tmp_path), Book()
    frame(b, "10:10", q)
    b.apply(t("10:10"), {"hm": "10:10", "action": "BUY_CE"}, q)
    assert frame(b, "10:15", q) is None and b.position is not None  # not sold at 10:15
    frame(b, "11:30", q)
    assert b.decision_due(t("11:30"))  # still asked every minute while holding
    ev = b.apply(t("11:30"), {"hm": "11:30", "action": "EXIT", "reason": "target"}, q)
    assert ev["event"] == "close" and ev["reason"] == "llm_exit"
    frame(b, "11:31", q)
    assert b.decision_due(t("11:31"))  # flat with entries left before 14:30: asked again
    b2, q2 = hour_book(tmp_path / "late"), Book()
    frame(b2, "10:05", q2)
    b2.apply(t("10:05"), {"hm": "10:05", "action": "BUY_CE"}, q2)
    for hm in ("12:00", "14:59"):
        assert frame(b2, hm, q2) is None and b2.position is not None
    ev = frame(b2, "15:05", q2)
    assert ev["event"] == "close" and ev["reason"] == "square_off"  # the last-resort exit before Zerodha's
    b3, q3 = hour_book(tmp_path / "stop"), Book()
    frame(b3, "10:05", q3)
    b3.apply(t("10:05"), {"hm": "10:05", "action": "BUY_PE"}, q3)
    q3.px[PE] = 55.0
    ev = frame(b3, "11:00", q3)
    assert ev["event"] == "close" and ev["reason"] == "day_loss_stop"  # the day stop still guards it


def test_hour_book_extra_trades_only_while_positive(tmp_path: Path) -> None:
    b, q = hour_book(tmp_path), Book()
    frame(b, "09:20", q)
    for hm in ("09:20", "09:25"):  # two winners
        frame(b, hm, q)
        b.apply(t(hm), {"hm": hm, "action": "BUY_CE"}, q)
        q.px[CE] += 20.0
        b.apply(t(hm), {"hm": hm, "action": "EXIT"}, q)
    frame(b, "09:30", q)
    assert b.entries == 2 and b.day_realized > 0 and b.decision_due(t("09:30"))  # a 3rd is allowed
    b2, q2 = hour_book(tmp_path / "loss"), Book()
    frame(b2, "09:20", q2)
    for hm in ("09:20", "09:25"):  # two losers
        frame(b2, hm, q2)
        b2.apply(t(hm), {"hm": hm, "action": "BUY_CE"}, q2)
        q2.px[CE] -= 5.0
        b2.apply(t(hm), {"hm": hm, "action": "EXIT"}, q2)
    frame(b2, "09:30", q2)
    assert b2.entries == 2 and b2.day_realized < 0 and not b2.decision_due(t("09:30"))
    assert b2.apply(t("09:30"), {"hm": "09:30", "action": "BUY_PE"}, q2) is None
    assert M.MAX_ENTRIES == 2 and M.MAX_ENTRIES_IF_POSITIVE == 4


def test_profit_lock_sells_an_extra_trade_at_half_the_days_best(tmp_path: Path) -> None:
    b, q = hour_book(tmp_path), Book()
    frame(b, "09:20", q)
    for hm in ("09:20", "09:25"):
        frame(b, hm, q)
        b.apply(t(hm), {"hm": hm, "action": "BUY_CE"}, q)
        q.px[CE] += 20.0
        b.apply(t(hm), {"hm": hm, "action": "EXIT"}, q)
    best = b.day_realized
    frame(b, "09:30", q)
    b.apply(t("09:30"), {"hm": "09:30", "action": "BUY_CE"}, q)  # the 3rd, extra trade
    fill = b.position.fill
    q.px[CE] = fill + 0.5 - (best * 0.5 / 65) + 3.0  # day P&L just above half of the best: hold
    assert frame(b, "09:31", q) is None and b.position is not None
    q.px[CE] = fill + 0.5 - (best * 0.5 / 65) - 3.0  # below half: sell and stop
    ev = frame(b, "09:32", q)
    assert ev["event"] == "close" and ev["reason"] == "profit_lock" and b.stopped == "profit_lock"
    assert b.day_realized > 0  # the winning hour stayed a winning hour


def test_previous_session_ohlc_reaches_the_llm(tmp_path: Path) -> None:
    bars = [{"t": "2026-11-06 09:15", "o": 24900.0, "h": 24950.0, "l": 24880.0, "c": 24940.0},
            {"t": "2026-11-06 15:29", "o": 25010.0, "h": 25040.0, "l": 25000.0, "c": 25030.4},
            {"t": "2026-11-09 09:15", "o": 25100.0, "h": 25120.0, "l": 25090.0, "c": 25110.0}]
    pd = M.prev_day_ohlc(bars, "2026-11-09")
    assert pd == {"date": "2026-11-06", "open": 24900.0, "high": 25040.0, "low": 24880.0, "close": 25030.4}
    assert M.prev_day_ohlc(bars[2:], "2026-11-09") is None
    b, q = hour_book(tmp_path), Book()
    b.on_frame(now=t("09:16"), feed={**feed(), "prev_day": pd}, book=q)
    st = b.state(t("09:16"), q)
    assert (st["prev_day_open"], st["prev_day_high"], st["prev_day_low"], st["prev_day_close"]) == (24900.0, 25040.0, 24880.0, 25030.4)
    assert st["day_high"] and st["day_low"]  # today's high and low were already in the view


def test_previous_session_ohlc_is_retried_until_the_bars_have_it(tmp_path: Path) -> None:
    b = hour_book(tmp_path)
    bars: list[dict] = []  # bars still loading after a restart
    calls = []

    def provider():
        calls.append(1)
        return list(bars)

    assert b.ensure_prev_day("2026-11-09", "09:15", provider) is None
    assert b.ensure_prev_day("2026-11-09", "09:15", provider) is None and len(calls) == 1  # once a minute
    bars += [{"t": "2026-11-06 09:15", "o": 24900.0, "h": 25040.0, "l": 24880.0, "c": 25030.0}]
    pd = b.ensure_prev_day("2026-11-09", "09:16", provider)
    assert pd and pd["close"] == 25030.0
    assert b.ensure_prev_day("2026-11-09", "09:17", provider) is pd and len(calls) == 2  # kept once found
    assert b.ensure_prev_day("2026-11-10", "09:15", lambda: []) is None  # a new day starts afresh


def test_no_buy_without_its_decision_line_but_exit_still_goes(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:00", q)
    real = b._append
    b._append = lambda e: None if e.get("event") == "decision" else real(e)
    assert b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q) is None
    assert b.position is None and b.last_reject == "ledger_write_failed"
    b._append = real
    frame(b, "10:05", q)
    b.apply(t("10:05"), {"hm": "10:05", "action": "BUY_CE"}, q)
    assert b.position is not None
    b._append = lambda e: None if e.get("event") == "decision" else real(e)
    ev = b.apply(t("10:10"), {"hm": "10:10", "action": "EXIT"}, q)
    assert ev["event"] == "close" and b.position is None  # getting out is never blocked
