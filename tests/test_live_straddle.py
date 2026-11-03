"""Live long straddle: tagged orders, holdings only from the book's own fills in Kite's order book."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import atlas_lite.live_straddle as L
from atlas_lite.paper_long_straddle import LEDGER_NAME, PaperLongStraddle

DAY = "2026-10-29"
CE, PE = "NFO:NIFTY26N0322950CE", "NFO:NIFTY26N0322950PE"


def t(hm: str, day: str = DAY) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def quotes(ce: float, pe: float, spread: float = 0.5) -> dict:
    return {CE: {"last_price": ce, "bid": ce - spread, "ask": ce + spread},
            PE: {"last_price": pe, "bid": pe - spread, "ask": pe + spread}}


class Book:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def get(self, s: str):
        return self.rows.get(s)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(L.time, "time", c)
    return c


class FakeKite:
    """Order book keyed by order id. plan[(symbol, side)] is a list of behaviours, used in order:
    fill | open | refuse | lost_placed | lost_not_placed | http5xx_placed | partial:<n>."""

    def __init__(self, funds: float = 1_000_000.0) -> None:
        self.book: dict[str, dict] = {}
        self.plan: dict[tuple[str, str], list[str]] = {}
        self.sent: list[tuple[str, str, int]] = []
        self.funds = funds
        self.unreadable = False
        self.cancel_lands = True
        self.px = {"BUY": 121.0, "SELL": 130.0}

    def equity_available(self):
        return self.funds

    def basket_margin_required(self, orders):
        return 0.0

    def _row(self, symbol, side, qty, tag, status, filled):
        oid = f"o{len(self.book) + 1}"
        self.book[oid] = {"order_id": oid, "tag": tag, "product": "MIS", "tradingsymbol": symbol.split(":")[1],
                          "transaction_type": side,
                          "quantity": qty, "status": status, "filled_quantity": filled,
                          "average_price": self.px[side] if filled else 0.0}
        return oid

    def place_market(self, *, symbol, transaction_type, qty, tag, product="MIS", market_protection=-1):
        self.sent.append((symbol, transaction_type, qty))
        todo = self.plan.get((symbol, transaction_type)) or ["fill"]
        how = todo.pop(0) if len(todo) > 1 else todo[0]
        if how == "refuse":
            raise RuntimeError("http_400:Insufficient funds")
        if how == "lost_not_placed":
            raise RuntimeError("transport:ReadTimeout")
        if how == "lost_placed":
            self._row(symbol, transaction_type, qty, tag, "COMPLETE", qty)
            raise RuntimeError("transport:ReadTimeout")
        if how == "http5xx_placed":
            self._row(symbol, transaction_type, qty, tag, "COMPLETE", qty)
            raise RuntimeError("http_502:Bad gateway")
        if how == "open":
            return self._row(symbol, transaction_type, qty, tag, "OPEN", 0)
        if how.startswith("partial:"):
            return self._row(symbol, transaction_type, qty, tag, "CANCELLED", int(how.split(":")[1]))
        return self._row(symbol, transaction_type, qty, tag, "COMPLETE", qty)

    def net_qty_strict(self, symbol):
        return self.net(symbol)

    def list_orders(self):
        if self.unreadable:
            raise RuntimeError("transport:ConnectError")
        return [dict(r) for r in self.book.values()]

    def cancel(self, oid):
        if self.cancel_lands and self.book[oid]["status"] == "OPEN":
            self.book[oid]["status"] = "CANCELLED"

    def fill_late(self, oid, qty=None):
        r = self.book[oid]
        r["status"], r["filled_quantity"] = "COMPLETE", qty or r["quantity"]
        r["average_price"] = self.px[r["transaction_type"]]

    def net(self, symbol):  # what the account really holds in this option (all orders, ours or not)
        ts = symbol.split(":")[1]
        return sum((r["filled_quantity"] if r["transaction_type"] == "BUY" else -r["filled_quantity"])
                   for r in self.book.values() if r["tradingsymbol"] == ts)

    def sells(self, symbol):
        return sum(1 for s, side, _ in self.sent if s == symbol and side == "SELL")


class Flag:
    def __init__(self, on: bool = True) -> None:
        self.on = on

    def is_enabled(self) -> bool:
        return self.on


def mk(tmp_path: Path, kite: FakeKite, on: bool = True) -> PaperLongStraddle:
    return PaperLongStraddle(path=tmp_path / LEDGER_NAME, order_client=kite, live_flag=Flag(on))


def frame(b: PaperLongStraddle, now: datetime, rows: dict):
    from datetime import date

    return b.on_frame(now=now, book=Book(rows), expiry=date(2026, 11, 3), atm=22950, ce_symbol=CE, pe_symbol=PE)


def run(b, clock, hm, rows=None, secs=1.0):
    clock.now += secs
    return frame(b, t(hm), rows or quotes(120, 120))


# ---- normal path ----
def test_buys_both_legs_holds_and_sells_at_1300(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    assert frame(b, t("09:16"), quotes(120, 120))["event"] == "live_start"
    assert k.sent == [(CE, "BUY", 65), (PE, "BUY", 65)]
    ev = run(b, clock, "09:17")
    assert b.live.phase == "holding" and ev is None
    ev = run(b, clock, "13:00", quotes(131, 131))  # sends both sells
    ev = ev or run(b, clock, "13:00", quotes(131, 131))
    assert ev["event"] == "close" and ev["live"] is True and ev["pnl_known"] is True
    assert ev["pnl_gross"] == round((130 + 130 - 121 - 121) * 65, 2) and k.net(CE) == 0 and k.net(PE) == 0
    assert b.live is None and b.month_pnl["2026-10"] == ev["pnl"]


def test_live_off_is_paper_with_no_orders(tmp_path, clock) -> None:
    k = FakeKite()
    ev = frame(mk(tmp_path, k, on=False), t("09:16"), quotes(120, 120))
    assert ev["event"] == "open" and ev["live"] is False and k.sent == []


def test_no_funds_no_orders(tmp_path, clock) -> None:
    k = FakeKite(funds=5_000)
    ev = frame(mk(tmp_path, k), t("09:16"), quotes(120, 120))
    assert ev["event"] == "live_skip" and k.sent == []


def test_stop_sells_when_bids_fall_30_percent(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    run(b, clock, "10:00", quotes(84, 84))  # bids 83.5 vs cost 121: -31%
    run(b, clock, "10:00", quotes(84, 84), secs=6)
    assert b.live.phase == "holding" and k.sells(CE) == 0  # 6 s below: not yet
    run(b, clock, "10:00", quotes(84, 84), secs=6)  # 12 s below with live prices: stop
    assert b.live.phase == "exiting" and b.live.exit_reason == "stop" and k.sells(CE) == 1


# ---- entry failures ----
def test_refused_call_means_the_put_alone_is_sold_back(tmp_path, clock) -> None:
    k = FakeKite()
    k.plan[(CE, "BUY")] = ["refuse"]
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    ev = run(b, clock, "09:17")  # a clean refusal is final at once: the put is sold back straight away
    ev = ev or run(b, clock, "09:17")
    assert k.net(CE) == 0 and k.net(PE) == 0 and k.sells(PE) == 1
    assert ev["event"] == "close" and ev["reason"] == "entry_incomplete"


def test_a_lost_reply_for_a_placed_buy_is_found_by_its_tag(tmp_path, clock) -> None:
    k = FakeKite()
    k.plan[(CE, "BUY")] = ["lost_placed"]
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    assert b.live.phase == "holding" and b.live.held("ce") == 65 and len(k.sent) == 2  # no repeat buy


def test_a_5xx_on_a_buy_that_went_through_is_not_called_never_placed(tmp_path, clock) -> None:
    k = FakeKite()
    k.plan[(CE, "BUY")] = ["http5xx_placed"]
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    assert b.live.held("ce") == 65 and b.live.phase == "holding"


def test_a_partial_buy_exits_both_legs_by_their_own_fills(tmp_path, clock) -> None:
    k = FakeKite()
    k.plan[(CE, "BUY")] = ["partial:40"]
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    assert b.live.phase == "exiting" and b.live.exit_reason == "entry_incomplete"
    assert (CE, "SELL", 40) in k.sent and (PE, "SELL", 65) in k.sent


def test_a_buy_still_working_is_cancelled_and_never_doubled(tmp_path, clock) -> None:
    k = FakeKite()
    k.plan[(PE, "BUY")] = ["open"]
    k.cancel_lands = False
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:16", secs=L.CANCEL_AFTER_S + 1)
    assert b.live.phase == "entering" and k.sent.count((PE, "BUY", 65)) == 1  # waits; never re-buys
    k.fill_late("o2")  # it fills after all
    run(b, clock, "09:17")
    assert b.live.phase == "holding" and b.live.held("pe") == 65


# ---- exits ----
def test_a_sell_still_working_is_never_doubled_and_retried_after_it_dies(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    k.plan[(PE, "SELL")] = ["open", "fill"]
    k.cancel_lands = False
    run(b, clock, "13:00", quotes(131, 131))
    for _ in range(5):
        run(b, clock, "13:00", quotes(131, 131), secs=10)
    assert k.sells(PE) == 1 and k.net(PE) == 65  # one working sell: no second one on top
    k.cancel_lands = True
    run(b, clock, "13:01", quotes(131, 131), secs=31)  # cancel lands: dead with 0 filled
    for _ in range(4):
        ev = run(b, clock, "13:02", quotes(131, 131), secs=20)
        if ev:
            break
    assert k.sells(PE) == 2 and k.net(PE) == 0 and ev["event"] == "close"


def test_a_lost_sell_reply_that_filled_is_not_sold_again(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    k.plan[(CE, "SELL")] = ["lost_placed"]
    run(b, clock, "13:00", quotes(131, 131))
    ev = run(b, clock, "13:00", quotes(131, 131))
    assert k.sells(CE) == 1 and k.net(CE) == 0 and ev["event"] == "close"


def test_manual_orders_in_the_same_option_are_never_touched(tmp_path, clock) -> None:
    k = FakeKite()
    k._row(CE, "BUY", 100, "", "COMPLETE", 100)  # bought by hand, no tag
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    run(b, clock, "13:00", quotes(131, 131))
    run(b, clock, "13:00", quotes(131, 131))
    assert b.live is None and k.net(CE) == 100  # the manual 100 are still there


def test_an_unreadable_order_book_means_no_decisions(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    k.unreadable = True
    run(b, clock, "13:00", quotes(131, 131))
    assert k.sells(CE) == 0 and b.last_reject.startswith("order_book_unreadable")
    k.unreadable = False
    run(b, clock, "13:00", quotes(131, 131))  # entering -> holding (both buys final)
    run(b, clock, "13:00", quotes(131, 131))  # holding -> exiting at 13:00
    assert k.sells(CE) == 1 and k.sells(PE) == 1


def test_sells_back_off_then_flag_manual_and_keep_trying(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    k.plan[(PE, "SELL")] = ["refuse"]
    for _ in range(12):
        run(b, clock, "13:00", quotes(131, 131), secs=61)
    assert b.live.manual_needed and k.net(PE) == 65 and k.sells(PE) >= L.MAX_SELLS
    k.plan[(PE, "SELL")] = ["fill"]
    for _ in range(4):
        ev = run(b, clock, "13:10", quotes(131, 131), secs=L.MANUAL_RETRY_S + 21)
        if ev:
            break
    assert ev["event"] == "close" and k.net(PE) == 0


# ---- restarts, end of day ----
def test_restart_while_holding_finds_its_orders_by_tag(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    again = mk(tmp_path, k)
    assert again.live is not None and again.live.phase == "holding"
    run(again, clock, "13:00", quotes(131, 131))
    ev = run(again, clock, "13:00", quotes(131, 131))
    assert ev["event"] == "close" and ev["pnl_gross"] == round((130 + 130 - 242) * 65, 2) and k.sent.count((CE, "BUY", 65)) == 1


def test_a_crash_after_the_intent_but_before_sending_resolves_as_never_placed(tmp_path, clock, monkeypatch) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    real = k.place_market

    def crash(**kw):
        if kw["symbol"] == PE:
            raise SystemExit("died")
        return real(**kw)

    monkeypatch.setattr(k, "place_market", crash)
    with pytest.raises(SystemExit):
        frame(b, t("09:16"), quotes(120, 120))
    monkeypatch.setattr(k, "place_market", real)
    again = mk(tmp_path, k)
    run(again, clock, "09:17", secs=1)
    assert again.live.phase == "entering" and k.net(CE) == 65  # outcome unknown: waits, sells nothing yet
    run(again, clock, "09:19", secs=L.UNCERTAIN_LOST_S + 1)  # the put's tag never appeared: written off
    run(again, clock, "09:19")
    assert k.net(CE) == 0 and (PE, "BUY", 65) not in k.sent  # the call is sold back at once
    assert again.live is not None and again.live.manual_needed  # but it keeps watching, alarm on
    ev = run(again, clock, "15:10")
    assert ev["event"] == "close" and ev["reason"] == "broker_square_off" and k.net(CE) == 0


def test_next_day_restart_sends_no_orders_and_uses_recorded_fills(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17", quotes(120, 120))
    k.book.clear()  # the next day the order book is empty
    n = len(k.sent)
    again = mk(tmp_path, k)
    ev = run(again, clock, "09:10", quotes(10, 10)) if False else frame(again, t("09:10", "2026-10-30"), quotes(10, 10))
    assert ev["event"] == "close" and ev["reason"] == "broker_square_off" and ev["pnl_estimated"] is True
    assert len(k.sent) == n  # no orders on a later day
    assert ev["pnl"] < 0  # valued at the last bids seen (119.5 each) vs 121 paid: a loss, and it counts
    assert again.month_pnl["2026-10"] == ev["pnl"]


def test_from_1510_working_orders_are_cancelled_and_nothing_new_is_sent(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    k.plan[(CE, "SELL")] = ["open"]
    run(b, clock, "13:00", quotes(131, 131))
    n = len(k.sent)
    ev = run(b, clock, "15:10", quotes(131, 131))
    assert ev["reason"] == "broker_square_off" and len(k.sent) == n
    assert k.book["o3"]["status"] == "CANCELLED"


def test_no_order_is_sent_if_its_intent_cannot_be_written(tmp_path, clock, monkeypatch) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    real = b._append
    monkeypatch.setattr(b, "_append", lambda ev: None if ev.get("event") == "order_intent" else real(ev))
    frame(b, t("09:16"), quotes(120, 120))
    assert k.sent == []


def test_tags_are_unique_and_short_enough_for_kite(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    tags = [json.loads(x).get("tag") for x in (tmp_path / LEDGER_NAME).read_text().splitlines()
            if json.loads(x).get("event") == "order_intent"]
    assert len(set(tags)) == 2 and all(len(x) <= 20 and x.isalnum() for x in tags)


def test_a_cancel_that_does_not_land_is_asked_again(tmp_path, clock) -> None:
    k = FakeKite()
    k.plan[(PE, "BUY")] = ["open"]
    k.cancel_lands = False
    calls = []
    real = k.cancel
    k.cancel = lambda oid: (calls.append(oid), real(oid))
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    for _ in range(3):
        run(b, clock, "09:17", secs=L.CANCEL_AFTER_S + 1)
    assert len(calls) == 3  # asked again every 30 s while still working


# ---- second independent review ----
def test_a_slow_listed_sell_that_went_through_is_never_sold_again(tmp_path, clock) -> None:
    """Review 2, #1: a sell whose reply was lost and which Kite lists late must not be doubled into a short."""
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    hidden = {}
    real_list = k.list_orders

    def place(**kw):
        if kw["symbol"] == CE and kw["transaction_type"] == "SELL":
            k.sent.append((CE, "SELL", kw["qty"]))
            oid = k._row(CE, "SELL", kw["qty"], kw["tag"], "COMPLETE", kw["qty"])
            hidden[oid] = k.book.pop(oid)  # placed and filled, but not listed yet
            clock.now += 25  # the POST hung for 25 s
            raise RuntimeError("transport:ReadTimeout")
        return FakeKite.place_market(k, **kw)

    k.place_market = place
    k.net_qty_strict = lambda sym: k.net(sym) - sum(r["filled_quantity"] for r in hidden.values()
                                                    if r["tradingsymbol"] == sym.split(":")[1])
    run(b, clock, "13:00", quotes(131, 131))
    for _ in range(3):  # Kite still hasn't listed it, but the account already shows it sold
        run(b, clock, "13:00", quotes(131, 131), secs=10)
    assert k.sells(CE) == 1  # never a second sell
    k.book.update(hidden)  # listed at last
    for _ in range(3):
        ev = run(b, clock, "13:02", quotes(131, 131), secs=10)
        if ev:
            break
    assert k.net(CE) == 0 and k.sells(CE) == 1 and ev["event"] == "close"


def test_a_slow_listed_buy_is_waited_for_not_left_untracked(tmp_path, clock) -> None:
    """Review 2, #2: an entry buy listed late must be found, not called never placed."""
    k = FakeKite()
    hidden = {}

    def place(**kw):
        if kw["symbol"] == CE and kw["transaction_type"] == "BUY":
            k.sent.append((CE, "BUY", kw["qty"]))
            oid = k._row(CE, "BUY", kw["qty"], kw["tag"], "COMPLETE", kw["qty"])
            hidden[oid] = k.book.pop(oid)
            clock.now += 25
            raise RuntimeError("transport:ReadTimeout")
        return FakeKite.place_market(k, **kw)

    k.place_market = place
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17", secs=30)
    assert b.live.phase == "entering"  # not decided: the call may have gone through
    k.book.update(hidden)  # listed 30 s later
    run(b, clock, "09:17", secs=1)
    assert b.live.phase == "holding" and b.live.held("ce") == 65


def test_hand_sold_shares_are_never_sold_again(tmp_path, clock) -> None:
    """Review 2, #3: if the call was sold by hand, the book must not sell it again into a short."""
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    k._row(CE, "SELL", 65, "", "COMPLETE", 65)  # sold by hand at 11:00
    run(b, clock, "13:00", quotes(131, 131))
    ev = run(b, clock, "13:00", quotes(131, 131))
    assert k.net(CE) == 0 and k.sells(CE) == 0  # the hand sale closed it; the book sent no call sell
    assert ev["event"] == "close" and ev["pnl_estimated"] is True


def test_an_order_book_dead_for_three_minutes_raises_the_alarm(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    k.unreadable = True
    for _ in range(4):
        run(b, clock, "10:00", secs=60)
    assert b.live.manual_needed and b.last_reject == "manual_needed"


def test_a_late_fill_after_never_placed_is_written_again(tmp_path, clock) -> None:
    k = FakeKite()
    k.plan[(CE, "BUY")] = ["refuse"]
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:16")
    b.live.intents[0].status = "NOT_PLACED"
    tag = b.live.intents[0].tag
    k._row(CE, "BUY", 65, tag, "COMPLETE", 65)  # it shows up after all
    run(b, clock, "09:17")
    finals = [json.loads(x) for x in (tmp_path / LEDGER_NAME).read_text().splitlines()
              if json.loads(x).get("event") == "order_final" and json.loads(x).get("tag") == tag]
    assert finals[-1]["status"] == "COMPLETE" and finals[-1]["filled"] == 65


def test_the_close_line_alone_ends_the_trade_and_counts_the_month(tmp_path, clock, monkeypatch) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    real = b._append
    monkeypatch.setattr(b, "_append", lambda ev: None if ev.get("event") == "close" else real(ev))
    run(b, clock, "13:00", quotes(131, 131))
    run(b, clock, "13:00", quotes(131, 131))
    assert b.pending_close is not None and b.month_pnl.get("2026-10", 0.0) == 0.0  # not counted until written
    monkeypatch.setattr(b, "_append", real)
    ev = run(b, clock, "13:01", quotes(131, 131))
    assert ev["event"] == "close" and b.month_pnl["2026-10"] == ev["pnl"]
    assert mk(tmp_path, k).month_pnl["2026-10"] == ev["pnl"] and mk(tmp_path, k).live is None


def test_a_later_day_never_uses_todays_prices(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17", quotes(120, 120), secs=61)  # marks the trade day's bids (119.5)
    k.book.clear()
    again = mk(tmp_path, k)
    ev = frame(again, t("09:20", "2026-10-30"), quotes(300, 300))  # today's prices are far higher
    assert ev["reason"] == "broker_square_off" and ev["pnl_gross"] == round((119.5 * 2 - 242) * 65, 2)


# ---- third independent review ----
def test_a_one_leg_entry_is_sold_without_any_positions_read(tmp_path, clock) -> None:
    """Review 3, #1: a refused put must not leave the filled call untracked, however Kite positions lag."""
    k = FakeKite()
    k.plan[(PE, "BUY")] = ["refuse"]
    k.net_qty_strict = lambda sym: 0  # positions lagging: would read "flat" - must not matter
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:16")
    ev = run(b, clock, "09:17")
    assert k.sells(CE) == 1 and k.net(CE) == 0 and ev["event"] == "close" and ev["reason"] == "entry_incomplete"


def test_a_hand_buy_in_the_same_option_is_never_sold(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    k._row(CE, "BUY", 100, "", "COMPLETE", 100)  # bought by hand during the trade
    run(b, clock, "13:00", quotes(131, 131))
    run(b, clock, "13:00", quotes(131, 131))
    assert (CE, "SELL", 65) in k.sent and k.net(CE) == 100 and b.live is None  # only its own 65


def test_a_late_listing_after_the_window_is_still_caught(tmp_path, clock) -> None:
    """Review 3, #2: the trade keeps watching after writing off an uncertain order."""
    k = FakeKite()
    hidden = {}

    def place(**kw):
        if kw["symbol"] == CE and kw["transaction_type"] == "BUY":
            k.sent.append((CE, "BUY", kw["qty"]))
            oid = k._row(CE, "BUY", kw["qty"], kw["tag"], "COMPLETE", kw["qty"])
            hidden[oid] = k.book.pop(oid)
            raise RuntimeError("transport:ReadTimeout")
        return FakeKite.place_market(k, **kw)

    k.place_market = place
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:19", secs=L.UNCERTAIN_LOST_S + 1)  # written off; the put alone is sold
    run(b, clock, "09:19")
    assert k.net(PE) == 0 and b.live is not None and b.live.manual_needed
    k.book.update(hidden)  # listed after 2+ minutes
    run(b, clock, "09:20")
    ev = run(b, clock, "09:20")
    assert k.net(CE) == 0 and ev["event"] == "close"  # caught and sold, nothing left untracked


def test_an_order_working_too_long_raises_the_alarm_and_the_other_leg_stays_protected(tmp_path, clock) -> None:
    k = FakeKite()
    k.plan[(PE, "BUY")] = ["open"]
    k.cancel_lands = False
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:18", secs=L.WORKING_ALARM_S + 1)
    assert b.live.manual_needed
    ev = run(b, clock, "13:00", quotes(131, 131))  # the call is still sold at 13:00 while the put is stuck
    assert k.sells(CE) == 1 and k.net(CE) == 0


def test_an_unfinished_order_at_close_marks_the_pnl_estimated(tmp_path, clock) -> None:
    k = FakeKite()
    k.plan[(PE, "BUY")] = ["open"]
    k.cancel_lands = False
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    ev = run(b, clock, "15:10")
    assert ev["reason"] == "broker_square_off" and ev["pnl_estimated"] is True


def test_a_stale_book_never_moves_a_final_order_backwards(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    k.book["o1"].update({"status": "OPEN", "filled_quantity": 0})  # a stale snapshot
    run(b, clock, "09:18")
    it = b.live.intents[0]
    assert it.status == "COMPLETE" and it.filled == 65 and b.live.phase == "holding"


# ---- fourth independent review ----
def test_a_working_hand_sell_counts_as_sold(tmp_path, clock) -> None:
    """Review 4, #1: a resting hand sell must stop the book from selling into a short."""
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    oid = k._row(CE, "SELL", 65, "", "OPEN", 0)  # your limit sell resting in Kite
    run(b, clock, "13:00", quotes(131, 131))
    run(b, clock, "13:00", quotes(131, 131))
    assert k.sells(CE) == 0  # the book leaves the call to your order
    k.fill_late(oid)
    assert k.net(CE) == 0  # never short


def test_one_bad_tick_or_missing_prices_never_trigger_the_stop(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    run(b, clock, "10:00", quotes(84, 84))  # one bad tick
    run(b, clock, "10:00", {}, secs=6)  # then no prices at all
    run(b, clock, "10:00", {}, secs=6)
    assert b.live.phase == "holding" and k.sells(CE) == 0


def test_a_sell_listed_very_late_is_never_doubled(tmp_path, clock) -> None:
    """Review 4, #3: a sell is never written off, however late Kite lists it."""
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    hidden = {}

    def place(**kw):
        if kw["symbol"] == CE and kw["transaction_type"] == "SELL":
            k.sent.append((CE, "SELL", kw["qty"]))
            oid = k._row(CE, "SELL", kw["qty"], kw["tag"], "COMPLETE", kw["qty"])
            hidden[oid] = k.book.pop(oid)
            raise RuntimeError("transport:ReadTimeout")
        return FakeKite.place_market(k, **kw)

    k.place_market = place
    run(b, clock, "13:00", quotes(131, 131))
    for _ in range(4):
        run(b, clock, "13:05", quotes(131, 131), secs=L.UNCERTAIN_LOST_S)
    assert k.sells(CE) == 1 and b.live.manual_needed  # waiting, alarm on, no second sell
    k.book.update(hidden)
    ev = run(b, clock, "13:10", quotes(131, 131))
    assert k.net(CE) == 0 and ev["event"] == "close"


def test_an_earlier_days_trade_closes_without_the_order_book(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    k.unreadable = True  # before the morning login: every read fails
    again = mk(tmp_path, k)
    ev = frame(again, t("09:16", "2026-10-30"), quotes(120, 120))
    assert ev["event"] == "close" and ev["reason"] == "broker_square_off" and again.live is None


def test_a_torn_last_ledger_line_does_not_swallow_the_next(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    with (tmp_path / LEDGER_NAME).open("a", encoding="utf-8") as fh:
        fh.write('{"event":"order_fin')  # a crash mid-write
    again = mk(tmp_path, k)
    run(again, clock, "09:17")
    rows = [x for x in (tmp_path / LEDGER_NAME).read_text().splitlines()]
    assert rows[-1].startswith("{") and json.loads(rows[-1])  # the next line is intact
    assert len(mk(tmp_path, k).live.intents) == 2


# ---- audit on the earlier snapshot ----
def test_a_crash_after_finishing_but_before_the_close_line_sells_nothing_again(tmp_path, clock, monkeypatch) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    real = b._append
    monkeypatch.setattr(b, "_append", lambda ev: None if ev.get("event") == "close" else real(ev))
    run(b, clock, "13:00", quotes(131, 131))
    run(b, clock, "13:00", quotes(131, 131))
    assert b.live is not None and b.pending_close is not None  # kept until the close is on disk
    n = len(k.sent)
    again = mk(tmp_path, k)  # crash: the close line was never written
    ev = run(again, clock, "13:01", quotes(131, 131))
    ev = ev or run(again, clock, "13:01", quotes(131, 131))
    assert ev["event"] == "close" and len(k.sent) == n and k.net(CE) == 0 and k.net(PE) == 0  # no new orders


def test_a_restart_keeps_the_uncertain_window_and_the_sell_backoff(tmp_path, clock) -> None:
    k = FakeKite()
    k.plan[(PE, "BUY")] = ["lost_not_placed"]
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    returned = [i.returned_at for i in b.live.intents if i.leg == "pe"][0]
    again = mk(tmp_path, k)
    assert [i.returned_at for i in again.live.intents if i.leg == "pe"][0] == returned  # not restarted
    k2 = FakeKite()
    b2 = mk(tmp_path / "s", k2)
    frame(b2, t("09:16"), quotes(120, 120))
    run(b2, clock, "09:17")
    k2.plan[(CE, "SELL")] = ["refuse"]
    run(b2, clock, "13:00", quotes(131, 131))
    again2 = mk(tmp_path / "s", k2)
    assert again2.live.next_sell_at["ce"] > clock.now  # the backoff continues after the restart


def test_no_live_order_after_1510_because_zerodha_squares_off_at_1514(tmp_path, clock) -> None:
    k = FakeKite()
    b = mk(tmp_path, k)
    frame(b, t("09:16"), quotes(120, 120))
    run(b, clock, "09:17")
    k.plan[(CE, "SELL")] = ["refuse"]
    run(b, clock, "13:00", quotes(131, 131))  # the call's sell keeps failing
    n = len(k.sent)
    assert run(b, clock, "15:09", quotes(131, 131), secs=120) is None
    ev = run(b, clock, "15:10", quotes(131, 131), secs=120)
    assert ev["reason"] == "broker_square_off" and ev["pnl_estimated"] is True
    assert all(side != "SELL" or sym != CE for sym, side, _ in k.sent[n + 1:])  # nothing sent from 15:10
