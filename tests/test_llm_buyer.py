"""LLM option buyer: paper and live flows with a scripted decider and a fake Kite client."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from atlas_lite import llm_buyer as M
from atlas_lite.llm_buyer import LlmBuyer, LlmDecider


@pytest.fixture(autouse=True)
def _day_direction_off(monkeypatch, request):
    """Most tests check other behaviour on a flat test market; the day-direction rule has its own tests below."""
    if "day_direction" not in request.node.name:
        monkeypatch.setattr(M, "DAY_DIRECTION_RULE", False)
    if "loss_pause" not in request.node.name:
        monkeypatch.setattr(M, "LOSS_PAUSE_MIN", 0)

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


def test_decider_parses_and_has_no_openai_fallback() -> None:
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

    out = LlmDecider(Creds(), post=post).decide({"time": "10:40"})  # OpenRouter 402, no OpenAI fallback
    assert out["action"] == "" and out["error"].startswith("openrouter:") and out["inputs"] == {"time": "10:40"}
    assert [m for _, m in calls] == [M.MODEL_OPENROUTER]
    bodies = []
    ok = LlmDecider(Creds(), post=lambda url, **kw: (bodies.append(kw["json"]),
                    Resp(200, '{"action": "buy_pe", "reason": "new lows"}'))[1]).decide({"time": "10:40"})
    assert ok["action"] == "BUY_PE" and ok["provider"] == "openrouter" and ok["model"] == M.MODEL_OPENROUTER
    assert bodies[0]["reasoning"] == {"effort": "low"} and bodies[0]["max_tokens"] == 1000  # answers, never empty
    assert bodies[0]["provider"] == {"sort": "latency"}  # the fastest host of the same model
    assert LlmDecider(None).decide({})["error"] == "no_llm_credentials"


def test_paper_entry_exit_and_decision_schedule(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "09:19", q)
    assert not b.decision_due(t("09:19")) and b.decision_due(t("09:20")) and not b.decision_due(t("09:21"))
    frame(b, "10:40", q)
    ev = b.apply(t("10:40"), {"hm": "10:40", "action": "BUY_CE", "reason": "breakout"}, q)
    assert ev["event"] == "open" and ev["leg"] == "ce" and ev["fill"] == 100.5 and ev["qty"] == 130  # paper: 2 lots
    assert b.apply(t("10:45"), {"hm": "10:45", "action": "BUY_PE"}, q) is None  # a buy while holding is ignored
    assert "position" in b.state(t("10:45"), q) and b.decision_due(t("10:50"))
    q.px[CE] = 130.0
    close = b.apply(t("11:00"), {"hm": "11:00", "action": "EXIT", "reason": "target"}, q)
    assert close["event"] == "close" and close["exit"] == 129.5 and close["pnl_gross"] == round(29.0 * 130, 2)  # no frame
    # between: the lot target was never checked, both lots sold by the EXIT
    assert b.position is None and b.paper_realized == close["pnl"] and b.day_realized == 0.0  # live total untouched
    assert [e["action"] for e in ledger(b) if e["event"] == "decision"] == ["BUY_CE", "BUY_PE", "EXIT"]


def test_paper_has_no_day_stop_and_no_entry_cap(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_PE", "reason": "down"}, q)
    assert M.DAY_LOSS_LIMIT == 1_500.0
    q.px[PE] = 40.0  # -51 x 130 = -6,630: far past the live day stop, but paper has none
    assert frame(b, "10:01", q) is None and b.position is not None
    b.apply(t("10:02"), {"hm": "10:02", "action": "EXIT"}, q)
    frame(b, "10:04", q)
    assert b.paper_realized < -6_000 and not b.stopped and b.decision_due(t("10:05"))
    for i, hm in enumerate(("10:05", "10:10", "10:15", "10:20", "10:25")):  # well past the old live cap
        frame(b, hm, q)
        assert b.apply(t(hm), {"hm": hm, "action": "BUY_CE"}, q)["event"] == "open"
        b.apply(t(hm), {"hm": hm, "action": "EXIT"}, q)
    assert b.paper_entries == 6 and b.entries == 0 and "entries_left_today" not in b.state(t("10:26"), q)


def test_max_entries_done_for_day_errors_and_square_off(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "09:30", q)
    assert b.apply(t("09:30"), {"hm": "09:30", "action": "", "error": "openai:ReadTimeout:"}, q) is None
    assert b.position is None and ledger(b)[-1]["action"] == "ERROR"
    for hm in ("09:35", "09:45"):
        frame(b, hm, q)
        b.apply(t(hm), {"hm": hm, "action": "BUY_CE"}, q)
        b.apply(t(hm), {"hm": hm, "action": "EXIT"}, q)
    frame(b, "09:59", q)
    assert b.paper_entries == 2 and b.decision_due(t("10:00"))  # paper: no entry cap
    b2, q2 = book_at(tmp_path / "x"), Book()
    frame(b2, "10:00", q2)
    b2.apply(t("10:00"), {"hm": "10:00", "action": "DONE_FOR_DAY"}, q2)
    assert b2.stopped == "done_for_day" and not b2.decision_due(t("10:05"))
    b3, q3 = book_at(tmp_path / "y"), Book()
    frame(b3, "14:25", q3)
    b3.apply(t("14:25"), {"hm": "14:25", "action": "BUY_CE"}, q3)
    assert b3.apply(t("14:35"), {"hm": "14:35", "action": "BUY_CE"}, q3) is None or b3.paper_entries == 1
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
    assert b2.position is not None and b2.position.leg == "pe" and b2.paper_entries == 2
    assert b2.last_decision["hm"] == "10:10" and b2.last_decision["action"] == "BUY_PE"  # the card is not blank
    assert {"10:00", "10:05", "10:10"} <= b2.decided and b2.paper_realized == b.paper_realized


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
    assert len(sells) == 1 and sells[0]["quantity"] == 130  # 2 lots, as paper
    kite.fill_all(109.0)
    ev = frame(b, "10:46", q)
    assert ev["event"] == "close" and ev["live"] and ev["pnl_gross"] == round(19.0 * 130, 2)
    assert b.live is None and b.month_live_pnl[DAY[:7]] == ev["pnl"] and b.day_realized == ev["pnl"]


def test_live_day_loss_stop_sells_through_the_live_layer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas_lite.live_straddle.STOP_CONFIRM_S", 0.0)
    kite, q = FakeKite(), Book()
    b = book_at(tmp_path, order_client=kite, live_flag=Flag())
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    kite.fill_all(100.0)
    frame(b, "10:01", q)
    q.px[CE] = 68.0  # bid 67.5: -32.5 x 65 = -2,113 -> beyond the -1,500 budget
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


def test_unticking_the_box_sells_the_live_position_and_goes_on_in_paper(tmp_path: Path) -> None:
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
    assert len(sells) == 1 and sells[0]["quantity"] == 130 and not b.stopped
    kite.fill_all(101.0)
    ev = frame(b, "10:43", q)
    assert ev["event"] == "close" and ev["live"] and ev["reason"] == "ui_off"
    frame(b, "10:44", q)
    assert b.decision_due(t("10:45"))  # still deciding, now in paper
    ev = b.apply(t("10:45"), {"hm": "10:45", "action": "BUY_PE"}, q)
    assert ev["event"] == "open" and not ev.get("live") and b.position is not None  # a paper trade, no order
    assert len(kite.orders) == 2  # the live buy and its sell only
    assert [e["why"] for e in ledger(b) if e["event"] == "live_off"] == ["ui_off"]  # logged once


def test_box_never_ticked_today_does_not_stop_paper(tmp_path: Path) -> None:
    flag = SwitchFlag()
    flag.on = False
    b, q = book_at(tmp_path, order_client=FakeKite(), live_flag=flag), Book()
    frame(b, "10:00", q)
    assert not b.stopped and b.decision_due(t("10:00"))
    assert b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)["event"] == "open"  # paper


def test_no_second_entry_without_day_loss_budget_and_live_stop_never_zero(tmp_path: Path, monkeypatch) -> None:
    b, q = book_at(tmp_path, order_client=FakeKite(), live_flag=Flag()), Book()
    frame(b, "10:00", q)
    b.day_realized = -1_450.0  # a first live trade the LLM closed near the -1,500 limit
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

        def decision_slot(self, now):
            return now.strftime("%H:%M:%S") if len(asked) < 2 else None

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


def test_mid_minute_check_while_holding(tmp_path: Path, monkeypatch) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:00", q)
    at = lambda hms: datetime.fromisoformat(f"{DAY}T{hms}+05:30")  # noqa: E731
    assert b.decision_slot(at("10:00:05")) == "10:00" and b.decision_slot(at("10:00:35")) == "10:00"  # flat: minutes only
    b.decided.add("10:00")
    assert b.decision_slot(at("10:00:35")) is None
    b.apply(at("10:00:06"), {"hm": "10:00", "action": "BUY_CE"}, q)
    assert b.decision_slot(at("10:00:20")) is None  # this minute already asked
    assert b.decision_slot(at("10:00:30")) == "10:00:30"  # holding: the mid-minute check
    b.decided.add("10:00:30")
    assert b.decision_slot(at("10:00:45")) is None and b.decision_slot(at("10:05:00")) == "10:05"
    assert b.decision_slot(at("10:01:31")) == "10:01:30"  # the :30 check runs every minute while holding
    q.px[CE] = 95.0
    ev = b.apply(at("10:00:32"), {"hm": "10:00:30", "action": "EXIT", "reason": "failed"}, q)
    assert ev["event"] == "close" and ev["reason"] == "llm_exit" and b.position is None
    frame(b, "10:05", q)
    assert b.decision_slot(at("10:05:40")) == "10:05" and b.decision_slot(at("10:06:40")) is None  # flat: minute slots
    monkeypatch.setattr(M, "HOLD_CHECK_30S", False)
    b2 = book_at(tmp_path / "off")
    frame(b2, "10:00", q)
    b2.apply(at("10:00:06"), {"hm": "10:00", "action": "BUY_CE"}, q)
    assert b2.decision_slot(at("10:00:30")) is None  # switched off: once a minute


def test_mid_minute_check_only_while_red(tmp_path: Path, monkeypatch) -> None:
    b, q = book_at(tmp_path), Book()
    at = lambda hms: datetime.fromisoformat(f"{DAY}T{hms}+05:30")  # noqa: E731
    frame(b, "10:00", q)
    b.apply(at("10:00:06"), {"hm": "10:00", "action": "BUY_CE"}, q)  # bought at the ask 100.5
    q.px[CE] = 110.0  # bid 109.5: green
    b.on_frame(now=at("10:01:31"), feed=feed(), book=q)
    assert not b.hold_red and b.decision_slot(at("10:01:31")) is None  # green: once a minute only
    q.px[CE] = 99.0  # bid 98.5: red
    b.on_frame(now=at("10:01:32"), feed=feed(), book=q)
    assert b.hold_red and b.decision_slot(at("10:01:32")) == "10:01:30"
    monkeypatch.setattr(M, "HOLD_CHECK_30S_RED_ONLY", False)
    q.px[CE] = 110.0
    b.on_frame(now=at("10:02:31"), feed=feed(), book=q)
    assert b.decision_slot(at("10:02:31")) == "10:02:30"  # switched off: every :30 while holding


def test_stretch_limit_blocks_new_entries_for_the_day(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    bar = lambda hm, o, c: {"t": f"{DAY} {hm}", "o": o, "h": max(o, c), "l": min(o, c), "c": c}  # noqa: E731
    bars = [bar("09:15", 24000.0, 24010.0), bar("09:16", 24010.0, 24200.0)]
    b.on_frame(now=t("10:00"), feed={**feed(), "bars_today": bars}, book=q)
    assert b.stretch_reached_at() is None and b.decision_due(t("10:00"))  # +0.83%: still open
    ev = b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    assert ev["event"] == "open"
    bars.append(bar("09:17", 24200.0, 24300.0))  # +1.25% from the open
    b.on_frame(now=t("10:05"), feed={**feed(), "bars_today": bars}, book=q)
    assert b.stretch_reached_at() == "09:17"
    assert b.decision_due(t("10:05")) and b.position is not None  # an open trade is still managed
    q.px[CE] = 101.0
    b.apply(t("10:05"), {"hm": "10:05", "action": "EXIT"}, q)
    bars.append(bar("09:18", 24300.0, 24100.0))  # back inside 1.2%: the limit stays on for the day
    b.on_frame(now=t("10:10"), feed={**feed(), "bars_today": bars}, book=q)
    assert not b.decision_due(t("10:10"))
    assert b.apply(t("10:10"), {"hm": "10:10", "action": "BUY_CE"}, q) is None and b.position is None
    blocked = [e for e in ledger(b) if e.get("event") == "entry_blocked"]
    assert blocked[-1]["why"] == "stretch_limit" and blocked[-1]["stretch_reached_at"] == "09:17"
    bars[:] = [bar("09:15", 24000.0, 23700.0)]  # -1.25%: either way
    b.on_frame(now=t("10:15"), feed={**feed(), "bars_today": bars}, book=q)
    assert b.stretch_reached_at() == "09:15"


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
    assert M.MAX_ERRORS_HOLDING == 5  # ~5 minutes at one question a minute

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
    assert not b.stopped and any(e["event"] == "live_off" for e in ledger(b))


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
    assert not b2.stopped and [o["transaction_type"] for o in kite.orders] == ["BUY", "SELL"]  # sold, paper goes on
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
    frame(b, "15:00", q)
    assert b.decision_due(t("15:00"))
    frame(b, "15:01", q)
    assert not b.decision_due(t("15:01"))  # flat: no new trades after 15:00
    frame(b, "15:03", q)
    assert not b.decision_due(t("15:03"))  # flat after 15:00: never asked, even before 15:08
    live_prompt = " ".join(M.SYSTEM_PROMPT.split())
    assert "New trades only until 15:00" in live_prompt and "held until 15:08" in live_prompt
    assert (M.LAST_ENTRY, M.LAST_DECISION, M.EXIT_AT, M.LIVE_ORDERS_UNTIL) == ("15:00", "15:07", "15:08", "15:12")
    assert "entries_left_today" in M.render_prompt("v3") and "realised" not in M.SYSTEM_PROMPT
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
    assert b.decision_due(t("11:31"))  # flat with entries left before 15:00: asked again
    b2, q2 = hour_book(tmp_path / "late"), Book()
    frame(b2, "10:05", q2)
    b2.apply(t("10:05"), {"hm": "10:05", "action": "BUY_CE"}, q2)
    for hm in ("12:00", "14:59", "15:07"):
        assert frame(b2, hm, q2) is None and b2.position is not None
    assert b2.decision_due(t("15:07"))  # holding: asked up to the minute before the 15:08 exit
    b2.decided.add("15:07")
    ev = frame(b2, "15:08", q2)
    assert ev["event"] == "close" and ev["reason"] == "square_off"  # the last-resort exit before Zerodha's
    b3, q3 = hour_book(tmp_path / "stop"), Book()
    frame(b3, "10:05", q3)
    b3.apply(t("10:05"), {"hm": "10:05", "action": "BUY_PE"}, q3)
    q3.px[PE] = 55.0
    assert frame(b3, "11:00", q3) is None and b3.position is not None  # paper: no day stop, the LLM decides


def _live_round_trip(b: LlmBuyer, kite: "FakeKite", q: Book, m0: int, buy: float, sell: float) -> None:
    hm = lambda k: f"09:{m0 + k:02d}"
    frame(b, hm(0), q)
    b.apply(t(hm(0)), {"hm": hm(0), "action": "BUY_CE"}, q)
    kite.fill_all(buy)
    frame(b, hm(1), q)
    b.apply(t(hm(1)), {"hm": hm(1), "action": "EXIT"}, q)
    frame(b, hm(1), q)
    kite.fill_all(sell)
    frame(b, hm(2), q)
    assert b.live is None


def test_live_has_no_trade_cap_and_no_profit_lock(tmp_path: Path) -> None:
    kite, q = FakeKite(), Book()
    b = hour_book(tmp_path, order_client=kite, live_flag=Flag())
    _live_round_trip(b, kite, q, 20, 100.0, 101.0)
    _live_round_trip(b, kite, q, 25, 100.0, 101.0)
    frame(b, "09:30", q)
    assert b.entries == 2 and b.decision_due(t("09:30"))  # a 3rd is allowed whatever the day's P&L
    _live_round_trip(b, kite, q, 30, 100.0, 101.0)
    frame(b, "09:35", q)
    assert b.entries == 3 and b.decision_due(t("09:35")) and b._profit_floor() is None and not b.stopped
    kite2, q2 = FakeKite(), Book()
    b2 = hour_book(tmp_path / "loss", order_client=kite2, live_flag=Flag())
    _live_round_trip(b2, kite2, q2, 20, 100.0, 88.0)  # -12 x 130 = -1,560: past the -1,500 day stop
    frame(b2, "09:33", q2)  # past the 10-minute loss pause, so only the day stop blocks
    assert b2.day_realized - M.EST_CHARGES <= -M.DAY_LOSS_LIMIT and not b2.decision_due(t("09:33"))
    assert b2.apply(t("09:33"), {"hm": "09:33", "action": "BUY_PE"}, q2) is None


def test_live_buys_two_lots_and_runs_papers_two_exits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas_lite.live_straddle.STOP_CONFIRM_S", 0.0)
    kite, q = FakeKite(), Book()
    b = hour_book(tmp_path, order_client=kite, live_flag=Flag())
    frame(b, "09:40", q)
    b.apply(t("09:40"), {"hm": "09:40", "action": "BUY_CE", "reason": "up"}, q)
    assert [(o["transaction_type"], o["quantity"]) for o in kite.orders] == [("BUY", 130)]  # 2 lots, as paper
    assert any(e["event"] == "live_start" and e["qty"] == 130 and e["lot_size"] == 65 for e in ledger(b))
    kite.fill_all(100.0)
    frame(b, "09:41", q)
    assert b.live.phase == "holding"
    q.px[CE] = 115.5  # bid 115: +15% -> both trails armed, high 115
    frame(b, "09:42", q)
    assert b.live_lot1_high == 115.0 and b.live_lot2_high == 115.0 and len(kite.orders) == 1
    q.px[CE] = 113.0  # bid 112.5 <= 115 x 0.98: lot 1 sold by its trailing stop
    frame(b, "09:43", q)
    assert [(o["transaction_type"], o["quantity"]) for o in kite.orders][-1] == ("SELL", 65)
    assert any(e["event"] == "live_partial" and e["reason"] == "lot_trailing_stop" for e in ledger(b))
    kite.fill_all(112.5)
    frame(b, "09:44", q)
    assert b.live.held("ce") == 65 and b.live.phase == "holding" and len(kite.orders) == 2  # only once
    q.px[CE] = 109.5  # bid 109 <= 100 + 15 x 0.6 = 109: 40% of the best gain given back
    frame(b, "09:45", q)
    frame(b, "09:46", q)
    assert [(o["transaction_type"], o["quantity"]) for o in kite.orders][-1] == ("SELL", 65)
    kite.fill_all(109.0)
    ev = frame(b, "09:47", q)
    assert ev["event"] == "close" and ev["live"] and ev["reason"] == "lot2_giveback"
    assert ev["pnl_gross"] == round(12.5 * 65 + 9.0 * 65, 2)


def _close_bars(*closes, day=DAY):
    out = []
    for i, c in enumerate(closes):
        m = 15 + i
        out.append({"t": f"{day} {9 + m // 60:02d}:{m % 60:02d}", "o": c, "h": c, "l": c, "c": c})
    return out


def test_nifty_stop_sells_a_trade_not_yet_in_profit(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    closes = [24000.0, 24010.0, 24020.0]
    b.on_frame(now=t("10:00"), feed={**feed(), "bars_today": _close_bars(*closes)}, book=q)
    ev = b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    assert ev["nifty_ref"] == 24020.0 and b.position.nifty_ref == 24020.0
    closes.append(24006.0)  # 14 pts against: kept
    assert b.on_frame(now=t("10:01"), feed={**feed(), "bars_today": _close_bars(*closes)}, book=q) is None
    closes.append(24005.0)  # 15 against: sold
    q.px[CE] = 96.0
    ev = b.on_frame(now=t("10:02"), feed={**feed(), "bars_today": _close_bars(*closes)}, book=q)
    assert ev["event"] == "close" and ev["reason"] == "nifty_stop" and b.position is None


def test_nifty_stop_put_side_and_not_after_lot_one_is_sold(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    closes = [24000.0]
    b.on_frame(now=t("10:00"), feed={**feed(), "bars_today": _close_bars(*closes)}, book=q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_PE"}, q)
    b.position.banked_gross = 500.0  # lot 1 already sold by its trail: the giveback stop owns the rest
    b.position.qty = 65
    closes.append(24030.0)  # 30 pts against a put
    ev = b.on_frame(now=t("10:01"), feed={**feed(), "bars_today": _close_bars(*closes)}, book=q)
    assert ev is None and b.position is not None
    b2, q2 = book_at(tmp_path / "b2"), Book()
    closes2 = [24000.0]
    b2.on_frame(now=t("10:00"), feed={**feed(), "bars_today": _close_bars(*closes2)}, book=q2)
    b2.apply(t("10:00"), {"hm": "10:00", "action": "BUY_PE"}, q2)
    closes2.append(24015.0)
    ev = b2.on_frame(now=t("10:01"), feed={**feed(), "bars_today": _close_bars(*closes2)}, book=q2)
    assert ev["reason"] == "nifty_stop"


def test_live_nifty_stop_sells_both_lots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas_lite.live_straddle.STOP_CONFIRM_S", 0.0)
    kite, q = FakeKite(), Book()
    b = hour_book(tmp_path, order_client=kite, live_flag=Flag())
    closes = [24000.0]
    b.on_frame(now=t("09:40"), feed={**feed(), "bars_today": _close_bars(*closes)}, book=q)
    b.apply(t("09:40"), {"hm": "09:40", "action": "BUY_CE", "reason": "up"}, q)
    assert any(e["event"] == "live_start" and e["nifty_ref"] == 24000.0 for e in ledger(b))
    kite.fill_all(100.0)
    b.on_frame(now=t("09:41"), feed={**feed(), "bars_today": _close_bars(*closes)}, book=q)
    assert b.live.phase == "holding"
    closes.append(23985.0)
    q.px[CE] = 98.0
    b.on_frame(now=t("09:42"), feed={**feed(), "bars_today": _close_bars(*closes)}, book=q)
    b.on_frame(now=t("09:43"), feed={**feed(), "bars_today": _close_bars(*closes)}, book=q)
    assert [(o["transaction_type"], o["quantity"]) for o in kite.orders][-1] == ("SELL", 130)
    kite.fill_all(97.5)
    ev = b.on_frame(now=t("09:44"), feed={**feed(), "bars_today": _close_bars(*closes)}, book=q)
    assert ev["event"] == "close" and ev["live"] and ev["reason"] == "nifty_stop"


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


def test_prompt_v2_view_has_chart_inputs_and_no_day_pnl(tmp_path: Path) -> None:
    b, q = hour_book(tmp_path), Book()
    bars = [{"t": f"2026-11-11 09:{m:02d}", "o": 24000 + m, "h": 24005 + m, "l": 23995 + m, "c": 24002 + m, "v": 1000}
            for m in range(15, 45)]
    b.on_frame(now=t("09:44"), feed={**feed(spot=24046.0), "bars_today": bars}, book=q)
    st = b.state(t("09:44"), q)
    assert "realised_pnl_today_rs" not in st and "entries_left_today" not in st  # paper: no limit to show
    assert len(st["nifty_last_15_1m_bars"]) == 15 and st["nifty_last_15_1m_bars"][-1] == "09:44 24044/24049/24039/24046"
    assert st["opening_range"] == {"high": 24034.0, "low": 24010.0}  # 09:15-09:29
    assert st["move_5m_pts"] == 5.0 and st["move_15m_pts"] == 15.0
    assert "vwap" in st and "nifty_vs_vwap_pts" in st
    assert st["atm_straddle_now"] == 190.0 and st["minutes_to_forced_exit"] == 15 * 60 + 8 - (9 * 60 + 44)
    assert st["high_before_last_candle"] == 24048.0 and st["low_before_last_candle"] == 24010.0  # the 09:44 candle excluded
    assert st["pts_below_day_high"] == 3.0 and st["pts_above_day_low"] == 36.0  # candle highs/lows count, not only spots
    assert st["day_high"] == 24049.0 and st["day_low"] == 24010.0  # the same range in every field
    b.day_realized = -747.0
    assert "realised_pnl_today_rs" not in b.state(t("09:44"), q)  # a loss never reaches the LLM's view
    live = hour_book(tmp_path / "live", order_client=FakeKite(), live_flag=Flag())
    live.on_frame(now=t("09:44"), feed={**feed(spot=24046.0), "bars_today": bars}, book=q)
    assert "entries_left_today" not in live.state(t("09:44"), q)  # no trade cap (paper = live, 23 Nov 2026)
    b2 = hour_book(tmp_path / "nobars")
    b2.on_frame(now=t("09:44"), feed=feed(), book=q)
    st2 = b2.state(t("09:44"), q)
    assert "nifty_last_15_1m_bars" not in st2 and "opening_range" not in st2  # left out when bars are missing


def test_a_call_that_never_finishes_is_cut_at_the_deadline() -> None:
    import time

    import httpx

    class Padding(httpx.SyncByteStream):  # a provider keeping the call alive with padding: no read ever times out
        closed = False

        def __iter__(self):
            while not self.closed:
                time.sleep(0.02)
                yield b" "

        def close(self):
            Padding.closed = True

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Padding()))
    t0 = time.monotonic()
    try:
        M.post_with_deadline("https://openrouter.ai/api/v1/chat/completions", json={}, timeout=0.3, headers={},
                             transport=transport)
        raise AssertionError("no timeout")
    except TimeoutError:
        pass
    assert time.monotonic() - t0 < 2 and Padding.closed  # cut at the deadline and the connection closed
    ok = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}))
    r = M.post_with_deadline("https://x/chat", json={}, timeout=5, headers={}, transport=ok)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "{}"


def test_sparse_padding_is_cut_within_the_total_budget() -> None:
    """A real socket: headers, then one padding byte every 0.5 s. With a 1 s budget the 0.4 s silence limit ends it,
    so the call never runs past the budget (only checking the clock after each byte would allow ~budget + gap)."""
    import socket
    import threading
    import time

    import httpx

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        try:
            for _ in range(20):
                conn.sendall(b"1\r\n \r\n")
                time.sleep(0.5)
        except OSError:
            pass
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    t0 = time.monotonic()
    try:
        M.post_with_deadline(f"http://127.0.0.1:{srv.getsockname()[1]}/x", json={}, timeout=1.0, headers={})
        raise AssertionError("no timeout")
    except (TimeoutError, httpx.TimeoutException):
        pass
    assert time.monotonic() - t0 < 1.2  # within the budget (+ scheduling slack), not budget + gap
    srv.close()


def test_live_prompt_is_v5_and_v3_keeps_the_book_contract() -> None:
    assert M.PROMPT_VERSION == "v8" and M.SYSTEM_PROMPT == M.render_prompt("v8")
    assert M.MODEL_OPENROUTER == "anthropic/claude-sonnet-5.5"  # from 25 Nov (Sol 20-25 Nov, Sonnet 11-18 Nov, Nemo 19 Nov)
    assert M.LlmDecider(None).prompt == M.SYSTEM_PROMPT  # the book's decider sends v5 (freehand + information)
    v3 = M.render_prompt("v3")
    assert "New trades only until 15:00" in v3 and "held until 15:08" in v3 and "{" not in v3.split("Reply with")[0]
    assert "entries_left_today" in v3 and "realised" not in v3
    assert "BUY_CE, BUY_PE, WAIT, DONE_FOR_DAY" in v3 and "HOLD, EXIT" in v3


def test_previous_session_skips_a_holiday_with_flat_bars() -> None:
    bars = [{"t": "2026-11-09 09:15", "o": 25000, "h": 25100, "l": 24900, "c": 25050},
            {"t": "2026-11-09 15:29", "o": 25050, "h": 25060, "l": 25040, "c": 25044.65}]
    bars += [{"t": f"2026-11-10 {h:02d}:00", "o": 25044.65, "h": 25044.65, "l": 25044.65, "c": 25044.65} for h in (9, 12, 15)]
    got = M.prev_day_ohlc(bars, "2026-11-11")  # 10 Nov 2026 was a holiday: its flat bars are not "yesterday"
    assert got == {"date": "2026-11-09", "open": 25000.0, "high": 25100.0, "low": 24900.0, "close": 25044.65}
    flat = [{"t": "2026-11-12 10:00", "o": 1, "h": 1, "l": 1, "c": 1}]  # a flat trading day is skipped as well
    assert M.prev_day_ohlc(bars + flat, "2026-11-13")["date"] == "2026-11-09"


def test_holding_view_names_the_level_the_trade_broke(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:40", q)
    b.apply(t("10:40"), {"hm": "10:40", "action": "BUY_CE", "reason": "closed above the opening-range high 24030"}, q)
    pos = b.state(t("10:45"), q)["position"]
    assert pos["entry_reason"] == "closed above the opening-range high 24030"  # what the exit rule refers to
    assert "position.entry_reason" in M.render_prompt("v3") and "minutes_to_forced_exit" in M.render_prompt("v3")


def _bars(last_m: int) -> list[dict]:
    return [{"t": f"2026-11-11 09:{m:02d}", "o": 24000 + m, "h": 24005 + m, "l": 23995 + m, "c": 24002 + m, "v": 1000}
            for m in range(15, last_m + 1)]


def test_opening_range_is_shown_only_once_complete(tmp_path: Path) -> None:
    b, q = hour_book(tmp_path), Book()
    b.on_frame(now=t("09:27"), feed={**feed(spot=24030.0), "bars_today": _bars(26)}, book=q)
    assert "opening_range" not in b.state(t("09:27"), q)  # still forming at 09:27: it would move after an entry
    b.on_frame(now=t("09:31"), feed={**feed(spot=24034.0), "bars_today": _bars(30)}, book=q)
    assert b.state(t("09:31"), q)["opening_range"] == {"high": 24034.0, "low": 24010.0}


def test_the_traded_level_is_fixed_at_entry_and_shown_while_holding(tmp_path: Path) -> None:
    kite = FakeKite()
    for live in (False, True):
        b = hour_book(tmp_path / str(live), **({"order_client": kite, "live_flag": Flag()} if live else {}))
        q = Book()
        b.on_frame(now=t("09:41"), feed={**feed(spot=24046.0), "bars_today": _bars(40)}, book=q)
        b.apply(t("09:41"), {"hm": "09:41", "action": "BUY_CE", "reason": "broke the day high"}, q)
        # nearest key level at or below NIFTY: the day high before the last candle (09:39 high 24044), not the
        # opening-range high (24034) or VWAP
        src = b.live_meta if live else b.position.__dict__
        assert src["entry_level"] == 24044.0 and src["entry_level_kind"] == "day_high_before_last_candle"
        if live:
            kite.fill_all(100.0)
        b.on_frame(now=t("09:45"), feed={**feed(spot=24060.0), "bars_today": _bars(44)}, book=q)
        st = b.state(t("09:45"), q)
        pos = st["position"]
        assert pos["entry_level"] == 24044.0  # unchanged although the day high has moved on
        assert "high_before_last_candle" not in st and "low_before_last_candle" not in st  # moving levels hidden
        assert st["opening_range"] == {"high": 24034.0, "low": 24010.0}  # complete, fixed: kept as context
    assert "position.entry_level" in M.render_prompt("v3") and "shown once complete" in M.render_prompt("v3")
    paper_open = [e for e in ledger(LlmBuyer(path=tmp_path / "False" / "h.jsonl", lot_size=65)) if e["event"] == "open"]
    assert paper_open[0]["entry_level"] == 24044.0  # saved, so a restart restores the position with its level
    live_start = [e for e in ledger(LlmBuyer(path=tmp_path / "True" / "h.jsonl", lot_size=65)) if e["event"] == "live_start"]
    assert live_start[0]["entry_level"] == 24044.0


def test_entry_level_falls_back_to_nifty_when_no_key_level_is_on_the_right_side(tmp_path: Path) -> None:
    b, q = hour_book(tmp_path), Book()
    b.on_frame(now=t("09:20"), feed=feed(spot=24050.0), book=q)  # no candles yet: no VWAP, no day high/low
    b.apply(t("09:20"), {"hm": "09:20", "action": "BUY_PE", "reason": "falling"}, q)
    assert (b.position.entry_level, b.position.entry_level_kind) == (24050.0, "nifty_at_entry")
    assert b.state(t("09:21"), q)["position"]["entry_level"] == 24050.0  # never empty


def test_an_old_untick_stop_in_the_ledger_no_longer_stops_paper(tmp_path: Path, monkeypatch) -> None:
    import atlas_lite.llm_buyer as mod
    path = tmp_path / "llm.jsonl"
    path.write_text(json.dumps({"event": "stop_entries", "ts": "2026-11-11T10:12:46+05:30", "day": DAY, "why": "ui_off"}) + "\n")
    real_now = mod.datetime

    class FakeDT(real_now):
        @classmethod
        def now(cls, tz=None):
            return t("10:30")

    monkeypatch.setattr(mod, "datetime", FakeDT)
    b = book_at(tmp_path)
    monkeypatch.setattr(mod, "datetime", real_now)
    assert not b.stopped  # an untick before the change stopped the day; restored now, paper carries on


def test_paper_box_off_means_no_llm_calls_no_trades_and_closes_what_is_open(tmp_path: Path) -> None:
    from atlas_lite.day_live_flag import OnOffFlag

    paper = OnOffFlag(tmp_path / "paper.json")
    assert paper.is_enabled()  # on by default (no file)
    b, q = book_at(tmp_path, paper_flag=paper), Book()
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE", "reason": "breakout"}, q)
    assert b.position is not None
    paper.set_enabled(False)
    ev = frame(b, "10:01", q)
    assert ev["event"] == "close" and ev["reason"] == "paper_off" and b.position is None  # the paper trade is closed
    frame(b, "10:05", q)
    assert not b.decision_due(t("10:05"))  # no LLM calls while off
    assert b.apply(t("10:05"), {"hm": "10:05", "action": "BUY_PE"}, q) is None and b.last_reject == "paper_off"
    paper.set_enabled(True)
    frame(b, "10:09", q)
    assert OnOffFlag(tmp_path / "paper.json").is_enabled() and b.decision_due(t("10:10"))  # back on, persisted


def test_paper_box_off_sells_a_live_position(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.day_live_flag import OnOffFlag

    monkeypatch.setattr("atlas_lite.live_straddle.STOP_CONFIRM_S", 0.0)
    paper, kite, q = OnOffFlag(tmp_path / "paper.json"), FakeKite(), Book()
    b = book_at(tmp_path, order_client=kite, live_flag=Flag(), paper_flag=paper)
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    kite.fill_all(100.0)
    frame(b, "10:01", q)
    paper.set_enabled(False)
    frame(b, "10:02", q)
    frame(b, "10:03", q)
    assert [o["transaction_type"] for o in kite.orders] == ["BUY", "SELL"]  # the live position is sold


def test_paper_off_exit_is_logged_once_as_paper_off_and_untick_marker_survives_restart(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.day_live_flag import OnOffFlag
    import atlas_lite.llm_buyer as mod

    monkeypatch.setattr("atlas_lite.live_straddle.STOP_CONFIRM_S", 0.0)
    paper, kite, q, flag = OnOffFlag(tmp_path / "paper.json"), FakeKite(), Book(), SwitchFlag()
    b = book_at(tmp_path, order_client=kite, live_flag=flag, paper_flag=paper)
    frame(b, "10:00", q)
    b.apply(t("10:00"), {"hm": "10:00", "action": "BUY_CE"}, q)
    kite.fill_all(100.0)
    frame(b, "10:01", q)
    paper.set_enabled(False)  # what the API does: Paper off, and live off with source "paper_off"
    flag.on, flag.source = False, "paper_off"
    frame(b, "10:02", q)
    kite.fill_all(99.0)
    ev = frame(b, "10:03", q)
    assert ev["event"] == "close" and ev["reason"] == "paper_off"  # one clear reason
    assert not any(e["event"] == "live_off" for e in ledger(b))  # not also an untick
    # an untick logged today is not logged again after a restart
    b.live_off_day = ""
    b._append({"event": "live_off", "ts": "x", "day": DAY, "why": "ui_off"})
    real_now = mod.datetime

    class FakeDT(real_now):
        @classmethod
        def now(cls, tz=None):
            return t("10:30")

    monkeypatch.setattr(mod, "datetime", FakeDT)
    b2 = book_at(tmp_path, order_client=kite, live_flag=flag, paper_flag=paper)
    monkeypatch.setattr(mod, "datetime", real_now)
    assert b2.live_off_day == DAY


def test_freehand_prompt_v4_has_no_trading_rules_and_keeps_the_book_contract() -> None:
    v4 = M.render_prompt("v4")
    flat = " ".join(v4.split())
    assert "New trades only until 15:00" in flat and "held until 15:08" in flat
    assert "entries_left_today" in v4  # mentioned (only present when live), not a rule
    assert "not reasons on their own" in v4 and "Waiting has a cost too" in v4  # caution bias removed in plain words
    assert "BUY_CE, BUY_PE, WAIT, DONE_FOR_DAY" in v4 and "HOLD, EXIT" in v4 and "{" not in v4.split("Reply with")[0]
    for rule_word in ("chasing", "0.25%", "opening-range", "entry_level", "careful", "not obliged"):
        assert rule_word not in v4  # freehand: no rules, no caution bias
    assert M.PROMPT_VERSION == "v8"  # v4 13-18 Nov, v5/v6 18-19 Nov, v7 19-20 Nov, v8 = v7 + day-direction rule


def test_prompt_v5_is_v4_plus_information_and_still_has_no_rules() -> None:
    v4, v5 = M.render_prompt("v4"), M.render_prompt("v5")
    assert v5.startswith(v4.split("Reply with JSON only:")[0])  # v4's text unchanged
    for field in ("max_open_pnl_rs", "nifty_gap_at_open_pct", "overnight_cues_pct", "about half the time"):
        assert field in v5
    v6 = M.render_prompt("v6")
    assert "overnight_cues_pct" not in v6 and "nifty_gap_at_open_pct" not in v6
    assert "max_open_pnl_rs" in v6 and "about half the time" in v6
    v7 = M.render_prompt("v7")
    for field in ("given_back_from_peak_rs", "share_of_peak_left_pct", "minutes_since_peak", "nifty_against_trade_since_peak_pts"):
        assert field in v7 and field not in v6
    for rule_word in ("must", "take profit", "exit when", "never"):
        assert rule_word not in v7.lower().split("reply with")[0].split("given_back_from_peak_rs")[1]  # information, not a rule
    assert "{" not in v5.split("Reply with")[0] and "BUY_CE, BUY_PE, WAIT, DONE_FOR_DAY" in v5
    for rule_word in ("chasing", "0.25%", "must", "never trade", "do not buy", "take profit"):
        assert rule_word not in v5.lower()


def test_state_shows_the_trades_best_open_profit_and_no_overnight_cues(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    pd = {"date": "2026-11-09", "open": 23900.0, "high": 24100.0, "low": 23850.0, "close": 23800.0}
    fr = lambda hm: b.on_frame(now=t(hm), feed={**feed(), "prev_day": pd}, book=q)  # the engine adds prev_day each frame
    fr("10:39")
    b.apply(t("10:40"), {"hm": "10:40", "action": "BUY_CE", "reason": "breakout"}, q)  # fill 100.5
    q.px[CE] = 130.0
    fr("10:41")  # peak: bid 129.5 -> +Rs 1,885
    q.px[CE] = 110.0
    fr("10:42")
    st = b.state(t("10:42"), q)
    pos = st["position"]
    banked = (109.5 - 100.5) * 65  # lot 1 armed at 10:41 (bid 129.5), sold at 10:42 when the bid fell to 109.5
    peak = (129.5 - 100.5) * 130  # both lots at the 10:41 bid
    now_total = banked + (109.5 - 100.5) * 65
    assert pos["open_pnl_rs"] == round(now_total) and pos["max_open_pnl_rs"] == round(peak)
    assert pos["max_open_pnl_at"] == "10:41" and pos["lots_held"] == 1 and pos["banked_rs"] == round(banked)
    assert pos["given_back_from_peak_rs"] == round(peak - now_total) and pos["share_of_peak_left_pct"] == round(now_total / peak * 100)
    assert pos["minutes_since_peak"] == 1 and pos["nifty_against_trade_since_peak_pts"] == 0.0  # NIFTY flat in the test feed
    assert "overnight_cues_pct" not in st and "nifty_gap_at_open_pct" not in st  # removed in v6 (19 Nov)
    b.apply(t("10:44"), {"hm": "10:44", "action": "EXIT"}, q)
    frame(b, "10:45", q)
    assert b.trade_peak is None  # reset once flat


def test_reasoning_field_goes_to_openrouter_only(monkeypatch) -> None:
    class Creds:
        api_key, base_url = "k1", "https://openrouter.ai/api/v1"
        openai_api_key, openai_base_url = "k2", "https://api.openai.com/v1"

    class Resp:
        status_code = 200

        def __init__(self, code=200):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("down")

        def json(self):
            return {"choices": [{"message": {"content": '{"action": "WAIT", "reason": "x"}'}}]}

    bodies = {}

    def post(url, **kw):
        name = "openrouter" if "openrouter" in url else "openai"
        bodies[name] = kw["json"]
        return Resp(500 if name == "openrouter" else 200)

    monkeypatch.setattr(M, "MODEL_OPENAI", "gpt-x")
    out = LlmDecider(Creds(), post=post).decide({})
    assert out["provider"] == "openai" and bodies["openrouter"]["reasoning"] == {"effort": "low"}
    assert "reasoning" not in bodies["openai"] and "provider" not in bodies["openai"]  # OpenAI rejects these fields


def test_replay_until_accepts_unpadded_time_and_refuses_junk(tmp_path) -> None:
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "llm_replay_day.py"
    bad = subprocess.run([sys.executable, str(script), "2026-11-13", "--until", "945"], capture_output=True, text=True)
    assert bad.returncode != 0 and "--until must be HH:MM" in bad.stderr


def test_paper_two_lots_trailing_stop_on_lot_one_and_the_close_counts_both(tmp_path: Path, monkeypatch) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:39", q)
    b.apply(t("10:40"), {"hm": "10:40", "action": "BUY_CE", "reason": "breakout"}, q)  # 2 lots at 100.5
    q.px[CE] = 110.0  # bid 109.5 < 100.5 * 1.10 = 110.55: not armed
    assert frame(b, "10:41", q) is None and b.position.lot1_high == 0
    q.px[CE] = 120.0  # bid 119.5: armed, high 119.5
    assert frame(b, "10:42", q) is None and b.position.lot1_high == 119.5
    q.px[CE] = 125.0  # bid 124.5: new high, no sale
    assert frame(b, "10:43", q) is None and b.position.lot1_high == 124.5
    q.px[CE] = 122.5  # bid 122.0 = 2.0% below 124.5 -> lot 1 sold
    ev = frame(b, "10:44", q)
    assert ev["event"] == "partial_close" and ev["qty"] == 65 and ev["exit"] == 122.0 and ev["reason"] == "lot_trailing_stop"
    assert b.position.qty == 65 and b.position.banked_gross == round(21.5 * 65, 2)
    q.px[CE] = 116.0  # bid 115.5: above the lot-2 giveback stop (100.5 + 24 * 0.6 = 114.9)
    assert frame(b, "10:45", q) is None  # lot 1 only once per trade
    assert M.PARTIAL_ARM_PCT == 0.10 and M.PARTIAL_TRAIL_PCT == 0.02 and M.PAPER_LOTS == 2
    import atlas_lite.llm_buyer as mod
    real_now = mod.datetime

    class FakeDT(real_now):
        @classmethod
        def now(cls, tz=None):
            return t("10:46")

    monkeypatch.setattr(mod, "datetime", FakeDT)
    r = book_at(tmp_path)  # restart after lot 1 was sold (restores today's trade only)
    monkeypatch.setattr(mod, "datetime", real_now)
    assert r.position.qty == 65 and r.position.banked_gross == round(21.5 * 65, 2) and r.position.banked_charges > 0
    q.px[CE] = 110.0
    close = r.apply(t("10:50"), {"hm": "10:50", "action": "EXIT", "reason": "stalled"}, q)  # bid 109.5
    assert close["qty"] == 65 and close["pnl_gross"] == round(21.5 * 65 + 9.0 * 65, 2)  # banked lot + the last lot
    assert close["banked_lot_pnl_gross"] == round(21.5 * 65, 2)
    assert close["charges"] > ev["charges"] and r.paper_realized == close["pnl"]


def _dir_bars(last_close: float) -> list[dict]:
    """Opens at 24,000 at 09:15; the last closed candle (09:40) closes at last_close."""
    return [{"t": f"2026-11-11 09:{m:02d}", "o": 24000.0, "h": 24000.0, "l": 24000.0, "c": 24000.0} for m in range(15, 40)] + \
           [{"t": "2026-11-11 09:40", "o": 24000.0, "h": max(24000.0, last_close), "l": min(24000.0, last_close), "c": last_close}]


def test_day_direction_allows_only_the_side_of_the_days_move(tmp_path: Path) -> None:
    for close, side, seen in ((24060.0, "ce", "calls only"), (23940.0, "pe", "puts only"), (24030.0, None, "no new trades")):
        b, q = book_at(tmp_path / str(close)), Book()
        b.on_frame(now=t("09:41"), feed={**feed(spot=close), "bars_today": _dir_bars(close)}, book=q)
        assert b.allowed_side() == side  # +0.25% / -0.25% / +0.125% vs the 09:15 open
        st = b.state(t("09:41"), q)
        assert st["allowed_entry"] == seen and st["nifty_vs_open_pct"] == round((close / 24000 - 1) * 100, 3)
    assert "allowed_entry" in M.SYSTEM_PROMPT and "0.25%" in M.SYSTEM_PROMPT


def test_day_direction_refuses_the_other_side_and_logs_it(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    b.on_frame(now=t("09:41"), feed={**feed(spot=24060.0), "bars_today": _dir_bars(24060.0)}, book=q)
    assert b.apply(t("09:41"), {"hm": "09:41", "action": "BUY_PE", "reason": "fade"}, q) is None
    assert b.position is None and b.last_reject == "day_direction"
    blocked = [e for e in ledger(b) if e["event"] == "entry_blocked"]
    assert blocked and blocked[0]["allowed"] == "ce" and blocked[0]["action"] == "BUY_PE"
    b.apply(t("09:41"), {"hm": "09:42", "action": "BUY_CE", "reason": "with the day"}, q)
    assert b.position is not None and b.position.leg == "ce"


def test_day_direction_skips_the_llm_while_flat_with_no_direction(tmp_path: Path) -> None:
    b, q = book_at(tmp_path, first_decision="09:16", decide_every_min=1), Book()
    b.on_frame(now=t("09:41"), feed={**feed(spot=24030.0), "bars_today": _dir_bars(24030.0)}, book=q)
    assert b.decision_due(t("09:41")) is False  # inside +-0.25%: no new trade, so no LLM call
    b.on_frame(now=t("09:41"), feed={**feed(spot=24070.0), "bars_today": _dir_bars(24070.0)}, book=q)
    assert b.decision_due(t("09:41")) is True


def test_day_direction_without_the_opening_candle_allows_no_trade(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    late = [{"t": f"2026-11-11 11:{m:02d}", "o": 24000.0, "h": 24100.0, "l": 24000.0, "c": 24100.0} for m in range(5)]
    b.on_frame(now=t("11:05"), feed={**feed(spot=24100.0), "bars_today": late}, book=q)  # restart: bars from 11:00 only
    assert b.nifty_vs_open_pct() is None and b.allowed_side() is None  # unknown open -> no trade, not a guess
    b.on_frame(now=t("11:06"), feed={**feed(spot=24100.0), "bars_today": []}, book=q)
    assert b.allowed_side() is None and b.state(t("11:06"), q)["allowed_entry"] == "no new trades"


def test_paper_lot_two_is_sold_after_giving_back_40_percent_of_its_best_profit(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:39", q)
    b.apply(t("10:40"), {"hm": "10:40", "action": "BUY_CE", "reason": "breakout"}, q)  # 2 lots at 100.5
    q.px[CE] = 105.0  # bid 104.5: below +10%, not armed
    assert frame(b, "10:41", q) is None and b.position.lot2_high == 0
    q.px[CE] = 130.5  # bid 130.0: armed, high 130 (lot 1 armed too)
    assert frame(b, "10:42", q) is None and b.position.lot2_high == 130.0
    q.px[CE] = 127.0  # bid 126.5: 2.7% off the high -> lot 1 sold by its trail
    assert frame(b, "10:43", q)["event"] == "partial_close"
    q.px[CE] = 119.0  # bid 118.5 > 100.5 + 29.5 * 0.6 = 118.2: lot 2 kept
    assert frame(b, "10:44", q) is None and b.position is not None
    q.px[CE] = 118.5  # bid 118.0 <= 118.2: 40% of the best profit given back -> lot 2 sold
    ev = frame(b, "10:45", q)
    assert ev["event"] == "close" and ev["reason"] == "lot2_giveback" and ev["exit"] == 118.0 and b.position is None
    assert M.LOT2_ARM_PCT == 0.10 and M.LOT2_GIVEBACK == 0.40 and "40% of its best profit" in M.SYSTEM_PROMPT


def test_loss_pause_blocks_new_entries_for_ten_minutes_after_a_losing_close(tmp_path: Path, monkeypatch) -> None:
    b, q = book_at(tmp_path, first_decision="09:16", decide_every_min=1), Book()
    frame(b, "10:39", q)
    b.apply(t("10:40"), {"hm": "10:40", "action": "BUY_CE", "reason": "breakout"}, q)  # at 100.5
    q.px[CE] = 95.0
    close = b.apply(t("10:42"), {"hm": "10:42", "action": "EXIT", "reason": "failed"}, q)  # bid 94.5: a loss
    assert close["pnl"] < 0 and b.in_loss_pause(t("10:51")) and not b.in_loss_pause(t("10:52"))
    frame(b, "10:45", q)
    assert b.decision_due(t("10:45")) is False  # flat in the pause: the LLM is not asked
    assert b.apply(t("10:45"), {"hm": "10:45", "action": "BUY_CE", "reason": "again"}, q) is None
    assert b.last_reject == "loss_pause" and [e for e in ledger(b) if e["event"] == "entry_blocked"][-1]["why"] == "loss_pause"
    real_now = M.datetime

    class FakeDT(real_now):
        @classmethod
        def now(cls, tz=None):
            return t("10:46")

    monkeypatch.setattr(M, "datetime", FakeDT)
    r = book_at(tmp_path, first_decision="09:16", decide_every_min=1)  # a restart keeps the pause
    monkeypatch.setattr(M, "datetime", real_now)
    assert r.last_loss_exit_at == close["ts"]
    frame(b, "10:52", q)
    assert b.decision_due(t("10:52")) is True
    b.apply(t("10:52"), {"hm": "10:52", "action": "BUY_CE", "reason": "fresh"}, q)
    assert b.position is not None
    assert M.LOSS_PAUSE_MIN == 10 and "10 minutes" in M.SYSTEM_PROMPT


def test_loss_pause_not_started_by_a_winning_close(tmp_path: Path) -> None:
    b, q = book_at(tmp_path), Book()
    frame(b, "10:39", q)
    b.apply(t("10:40"), {"hm": "10:40", "action": "BUY_CE", "reason": "breakout"}, q)
    q.px[CE] = 106.0  # bid 105.5 > 100.5: a win after charges
    close = b.apply(t("10:42"), {"hm": "10:42", "action": "EXIT", "reason": "target"}, q)
    assert close["pnl"] > 0 and not b.in_loss_pause(t("10:43"))
