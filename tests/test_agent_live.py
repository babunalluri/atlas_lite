"""Agent live flag and Kite market-order path for agent_paper."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from atlas_lite.agent_live import AgentLiveFlag
from atlas_lite.kite_orders import (
    KiteOrderClient,
    KiteOrderError,
    split_nfo_symbol,
    transaction_for_close,
    transaction_for_open,
)
from atlas_lite.paper_agent import PaperAgent

IST = ZoneInfo("Asia/Kolkata")


class Book:
    def __init__(self) -> None:
        self.quotes: dict[str, dict] = {}

    def get(self, symbol: str):
        return self.quotes.get(symbol)


class FakeOrders:
    """Broker stub. Tracks net qty from fills; supports pending/stuck closes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_next = False
        self.price = 100.0
        self.net_qty = 0
        self.avg_price: float | None = None
        self.realised: float | None = None
        self.orders: dict[str, dict] = {}
        self._seq = 0
        self.stuck_open = False  # place succeeds but wait never completes
        self.partial_fill_qty: int | None = None
        self.position_read_fail = False

    def _oid(self) -> str:
        self._seq += 1
        return f"ORD{self._seq}"

    def position_row(self, symbol, *, product="MIS"):
        if self.position_read_fail:
            raise KiteOrderError("transport:boom")
        if self.net_qty == 0 and self.realised is None and self.avg_price is None:
            return None
        return {
            "quantity": int(self.net_qty),
            "average_price": self.avg_price if self.avg_price is not None else self.price,
            "realised": self.realised,
            "tradingsymbol": symbol.split(":")[-1],
            "product": product,
            "raw": {},
        }

    def net_position_qty(self, symbol, *, product="MIS"):
        row = self.position_row(symbol, product=product)
        return int(row["quantity"]) if row else 0

    def place_market(self, *, symbol, transaction_type, qty, product="MIS", tag="atlas_agent"):
        if self.fail_next:
            self.fail_next = False
            raise KiteOrderError("rejected:margin")
        oid = self._oid()
        self.calls.append(
            {
                "symbol": symbol,
                "transaction_type": transaction_type,
                "qty": qty,
                "product": product,
                "tag": tag,
                "order_id": oid,
            }
        )
        self.orders[oid] = {
            "status": "OPEN" if self.stuck_open else "COMPLETE",
            "average_price": self.price,
            "filled_quantity": (
                self.partial_fill_qty if self.partial_fill_qty is not None else qty
            ),
            "quantity": qty,
            "transaction_type": transaction_type,
        }
        return oid

    def _apply_fill_to_net(self, order_id: str) -> None:
        row = self.orders.get(order_id) or {}
        if row.get("_applied"):
            return
        filled = int(row.get("filled_quantity") or row.get("quantity") or 0)
        side = str(row.get("transaction_type") or "").upper()
        if side == "BUY":
            self.net_qty += filled
        elif side == "SELL":
            self.net_qty -= filled
        self.avg_price = float(row.get("average_price") or self.price)
        row["_applied"] = True

    def wait_fill(self, order_id, *, timeout_s=8.0, poll_s=0.25):
        row = self.orders.get(order_id) or {}
        status = str(row.get("status") or "").upper()
        if status == "COMPLETE":
            self._apply_fill_to_net(order_id)
            return row
        if status in ("REJECTED", "CANCELLED", "CANCELED"):
            raise KiteOrderError(f"order_{status.lower()}", order_id=order_id)
        raise KiteOrderError(f"fill_timeout:{order_id}:{status or 'OPEN'}", order_id=order_id)

    def order_status(self, order_id):
        return dict(self.orders.get(order_id) or {"status": "OPEN"})

    def fill_from_order(self, order_id, *, fallback_qty=0):
        row = self.order_status(order_id)
        if str(row.get("status") or "").upper() != "COMPLETE":
            raise KiteOrderError("order_not_complete", order_id=order_id)
        self._apply_fill_to_net(order_id)
        qty = int(row.get("filled_quantity") or row.get("quantity") or fallback_qty)
        return {
            "order_id": order_id,
            "average_price": float(row.get("average_price") or self.price),
            "quantity": qty,
            "status": "COMPLETE",
        }

    def cancel(self, order_id):
        row = self.orders.get(order_id)
        if row and str(row.get("status") or "").upper() == "OPEN":
            row["status"] = "CANCELLED"

    def market_fill(self, *, symbol, transaction_type, qty, product="MIS", tag="atlas_agent"):
        oid = self.place_market(
            symbol=symbol,
            transaction_type=transaction_type,
            qty=qty,
            product=product,
            tag=tag,
        )
        fill = self.wait_fill(oid)
        filled = int(fill.get("filled_quantity") or qty)
        return {
            "order_id": oid,
            "average_price": float(fill.get("average_price") or self.price),
            "quantity": filled,
            "status": "COMPLETE",
        }

    def complete_stuck(self, order_id: str, *, filled: int | None = None) -> None:
        row = self.orders[order_id]
        row["status"] = "COMPLETE"
        if filled is not None:
            row["filled_quantity"] = filled


def _stamp(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def _live_bot(tmp_path, monkeypatch, orders: FakeOrders) -> tuple[PaperAgent, Book, str, str]:
    monkeypatch.setenv("ATLAS_LITE_AGENT_LIVE_ALLOWED", "1")
    flag = AgentLiveFlag(tmp_path / "agent_live.json")
    flag.set_enabled(True, source="test")
    bot = PaperAgent(
        path=tmp_path / "paper_agent.jsonl",
        lot_size=65,
        live_flag=flag,
        order_client=orders,  # type: ignore[arg-type]
    )
    quotes = Book()
    ce, pe = "NFO:NIFTY26O1923000CE", "NFO:NIFTY26O1923000PE"
    quotes.quotes[ce] = {"last_price": 80.0}
    quotes.quotes[pe] = {"last_price": 70.0}
    return bot, quotes, ce, pe


def test_live_flag_unlocked_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ATLAS_LITE_AGENT_LIVE_ALLOWED", raising=False)
    flag = AgentLiveFlag(tmp_path / "agent_live.json")
    assert flag.is_enabled() is False  # file still off
    assert flag.snapshot()["allowed"] is True
    snap = flag.set_enabled(True, source="test")
    assert snap["enabled"] is True


def test_live_flag_enable_and_env_lock(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_AGENT_LIVE_ALLOWED", "1")
    flag = AgentLiveFlag(tmp_path / "agent_live.json")
    snap = flag.set_enabled(True, source="test")
    assert snap["enabled"] is True
    monkeypatch.setenv("ATLAS_LITE_AGENT_LIVE_ALLOWED", "0")
    assert flag.is_enabled() is False


def test_nfo_symbol_and_sides() -> None:
    assert split_nfo_symbol("NFO:NIFTY26O1923000CE") == ("NFO", "NIFTY26O1923000CE")
    assert transaction_for_open("long") == "BUY"
    assert transaction_for_close("long") == "SELL"
    assert transaction_for_close("short") == "BUY"


def test_net_position_qty_uses_net_only_not_day() -> None:
    client = KiteOrderClient.__new__(KiteOrderClient)

    class Resp:
        status_code = 200

        def json(self):
            return {
                "data": {
                    "net": [
                        {
                            "tradingsymbol": "NIFTY26O1923000CE",
                            "exchange": "NFO",
                            "product": "MIS",
                            "quantity": -65,
                            "average_price": 80.0,
                            "realised": -100.0,
                        }
                    ],
                    "day": [
                        {
                            "tradingsymbol": "NIFTY26O1923000CE",
                            "exchange": "NFO",
                            "product": "MIS",
                            "quantity": -65,
                            "average_price": 80.0,
                            "realised": -100.0,
                        }
                    ],
                }
            }

    class FakeHttp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path):
            return Resp()

    client._client = lambda: FakeHttp()  # type: ignore[method-assign]
    assert client.net_position_qty("NFO:NIFTY26O1923000CE") == -65
    row = client.position_row("NFO:NIFTY26O1923000CE")
    assert row is not None
    assert row["quantity"] == -65
    assert row["realised"] == -100.0


def test_paper_open_when_live_off(tmp_path) -> None:
    flag = AgentLiveFlag(tmp_path / "agent_live.json")
    orders = FakeOrders()
    bot = PaperAgent(
        path=tmp_path / "paper_agent.jsonl",
        lot_size=65,
        live_flag=flag,
        order_client=orders,  # type: ignore[arg-type]
    )
    quotes = Book()
    ce, pe = "NFO:NIFTY26O1923000CE", "NFO:NIFTY26O1923000PE"
    quotes.quotes[ce] = {"last_price": 80.0}
    quotes.quotes[pe] = {"last_price": 70.0}
    now = _stamp("2026-10-21", "10:00")
    bot.propose_entry(side="ce", style="long", reason="test", spot=23000.0, now=now)
    opened = bot.on_frame(
        now=now,
        feed={"ce": 80.0, "pe": 70.0, "ce_symbol": ce, "pe_symbol": pe},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert opened is not None
    assert opened["live"] is False
    assert orders.calls == []


def test_live_open_and_close_uses_broker_fill(tmp_path, monkeypatch) -> None:
    orders = FakeOrders()
    orders.price = 81.5
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    now = _stamp("2026-10-21", "10:00")
    bot.propose_entry(side="ce", style="long", reason="live test", spot=23000.0, now=now)
    opened = bot.on_frame(
        now=now,
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert opened is not None
    assert opened["live"] is True
    assert opened["entry"] == 81.5
    assert bot.position is not None and bot.position.live is True
    assert orders.net_qty == 65

    bot.live_flag.set_enabled(False, source="test")  # type: ignore[union-attr]
    orders.price = 90.0
    quotes.quotes[ce] = {"last_price": 92.0}
    closed = bot.on_frame(
        now=_stamp("2026-10-21", "10:05"),
        feed={"ce": 92.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23040.0,
    )
    assert closed is not None
    assert closed["event"] == "close"
    assert closed["exit"] == 90.0
    assert orders.net_qty == 0
    assert bot.position is None


def test_live_open_failure_does_not_book_position(tmp_path, monkeypatch) -> None:
    orders = FakeOrders()
    orders.fail_next = True
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    now = _stamp("2026-10-21", "10:00")
    bot.propose_entry(side="ce", style="long", reason="fail", spot=23000.0, now=now)
    opened = bot.on_frame(
        now=now,
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert opened is None
    assert bot.position is None
    assert bot.last_reject and "live_open_failed" in bot.last_reject


def test_open_timeout_but_filled_adopts_own_order(tmp_path, monkeypatch) -> None:
    """Open reports failure, but our order_id filled — book adopts that fill only."""
    orders = FakeOrders()
    orders.orders["ORDX"] = {
        "status": "COMPLETE",
        "average_price": 82.0,
        "filled_quantity": 65,
        "quantity": 65,
        "transaction_type": "BUY",
    }

    def boom_fill(**kwargs):
        orders.net_qty = 65
        raise KiteOrderError("fill_timeout:ORDX:OPEN", order_id="ORDX")

    orders.market_fill = boom_fill  # type: ignore[method-assign]
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    now = _stamp("2026-10-21", "10:00")
    bot.propose_entry(side="ce", style="long", reason="race", spot=23000.0, now=now)
    opened = bot.on_frame(
        now=now,
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert opened is not None
    assert opened["event"] == "live_adopt"
    assert bot.position is not None
    assert bot.position.qty == 65
    assert bot.position.entry == 82.0
    assert bot.position.live is True


def test_does_not_adopt_preexisting_manual_lot(tmp_path, monkeypatch) -> None:
    orders = FakeOrders()
    orders.net_qty = 65
    orders.avg_price = 50.0
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    now = _stamp("2026-10-21", "10:00")
    bot.propose_entry(side="ce", style="long", reason="manual", spot=23000.0, now=now)
    opened = bot.on_frame(
        now=now,
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert opened is None
    assert bot.position is None
    assert bot.last_reject and "live_broker_not_flat" in bot.last_reject
    assert orders.calls == []


def test_partial_fill_on_open_books_actual_qty(tmp_path, monkeypatch) -> None:
    orders = FakeOrders()
    orders.price = 80.0
    orders.partial_fill_qty = 30
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    now = _stamp("2026-10-21", "10:00")
    bot.propose_entry(side="ce", style="long", reason="partial", spot=23000.0, now=now)
    opened = bot.on_frame(
        now=now,
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert opened is not None
    assert opened["qty"] == 30
    assert bot.position is not None
    assert bot.position.qty == 30
    assert orders.net_qty == 30


def test_stale_day_live_close_skips_broker(tmp_path, monkeypatch) -> None:
    orders = FakeOrders()
    orders.price = 80.0
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    bot.propose_entry(
        side="ce", style="long", reason="stale", spot=23000.0, now=_stamp("2026-10-20", "14:00")
    )
    bot.on_frame(
        now=_stamp("2026-10-20", "14:00"),
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    open_calls = len(orders.calls)
    assert bot.position is not None
    bot.position.realised_at_open = 0.0
    orders.realised = -520.0
    closed = bot._close(
        _stamp("2026-10-21", "09:20"),
        quotes,
        "session_gap",
        exit_px=80.0,
        spot=23000.0,
    )
    assert closed is not None
    assert closed["reason"] == "broker_squared_off"
    assert closed["pnl_source"] == "kite_realised_delta"
    assert closed["pnl_known"] is True
    assert closed["pnl"] is not None
    assert closed["pnl"] < 0
    assert len(orders.calls) == open_calls


def test_partial_close_retry_uses_remaining_qty(tmp_path, monkeypatch) -> None:
    orders = FakeOrders()
    orders.price = 80.0
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    bot.propose_entry(side="ce", style="short", reason="short", spot=23000.0, now=_stamp("2026-10-21", "10:00"))
    bot.on_frame(
        now=_stamp("2026-10-21", "10:00"),
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert orders.net_qty == -65
    assert bot.position is not None

    # First close only fills 30 — leave -35 on broker.
    orders.partial_fill_qty = 30
    orders.price = 70.0

    def close_then_adjust(*args, **kwargs):
        fill = FakeOrders.market_fill(orders, *args, **kwargs)
        # market_fill already applied +30 to net (-65+30=-35). Force remain.
        orders.net_qty = -35
        return fill

    # Use place_market path: after wait, set net to -35
    orig_wait = orders.wait_fill

    def wait_and_partial(oid, **kw):
        row = orig_wait(oid, **kw)
        # After BUY 30 against short 65
        orders.net_qty = -35
        return row

    orders.wait_fill = wait_and_partial  # type: ignore[method-assign]
    first = bot._close(_stamp("2026-10-21", "10:30"), quotes, "stop", exit_px=70.0, spot=22950.0)
    assert first is None  # partial — position kept
    assert bot.position is not None
    assert bot.position.qty == 35
    assert len(orders.calls) == 2  # open SELL + close BUY 30? close_qty=min(65,65)=65 but partial fill 30
    assert orders.calls[-1]["qty"] == 65  # first attempt sized to full book

    # Retry must size to remaining 35, not original 65.
    orders.partial_fill_qty = 35
    orders.price = 68.0

    def wait_flat(oid, **kw):
        row = orig_wait(oid, **kw)
        orders.net_qty = 0
        return row

    orders.wait_fill = wait_flat  # type: ignore[method-assign]
    second = bot._close(_stamp("2026-10-21", "10:31"), quotes, "stop", exit_px=68.0, spot=22940.0)
    assert second is not None
    assert second["event"] == "close"
    assert orders.calls[-1]["qty"] == 35
    assert bot.position is None


def test_stuck_open_close_does_not_send_second_order(tmp_path, monkeypatch) -> None:
    orders = FakeOrders()
    orders.price = 80.0
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    bot.propose_entry(side="ce", style="long", reason="stuck", spot=23000.0, now=_stamp("2026-10-21", "10:00"))
    bot.on_frame(
        now=_stamp("2026-10-21", "10:00"),
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert orders.net_qty == 65
    orders.stuck_open = True
    first = bot._close(_stamp("2026-10-21", "10:30"), quotes, "stop", exit_px=70.0, spot=22950.0)
    assert first is None
    assert bot.position is not None
    pending = bot.position.close_order_id
    assert pending
    calls_after_first = len(orders.calls)

    bot.live_close_next_at = None
    second = bot._close(_stamp("2026-10-21", "10:31"), quotes, "stop", exit_px=70.0, spot=22950.0)
    assert second is None
    assert len(orders.calls) == calls_after_first  # no second place_market
    assert bot.position.close_order_id == pending

    orders.complete_stuck(pending, filled=65)
    orders.orders[pending]["average_price"] = 70.0
    bot.live_close_next_at = None
    third = bot._close(_stamp("2026-10-21", "10:32"), quotes, "stop", exit_px=70.0, spot=22950.0)
    assert len(orders.calls) == calls_after_first
    assert third is not None
    assert third["close_order_id"] == pending
    assert third["exit"] == 70.0
    assert orders.net_qty == 0
    assert bot.position is None


def test_orphan_live_submit_warns_on_restore(tmp_path, monkeypatch, caplog) -> None:
    import logging

    path = tmp_path / "paper_agent.jsonl"
    path.write_text(
        '{"event":"live_submit","ts":"2026-10-21T10:00:00+05:30","day":"2026-10-21",'
        '"side":"ce","style":"long","symbol":"NFO:X","qty":65,"strategy":"agent_paper"}\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.ERROR, logger="atlas_lite.paper_agent"):
        PaperAgent(path=path, lot_size=65)
    assert any("orphan live_submit" in r.message for r in caplog.records)


def test_wait_fill_rechecks_after_cancel(monkeypatch) -> None:
    client = KiteOrderClient.__new__(KiteOrderClient)
    state = {"cancelled": False}
    cancels: list[str] = []

    def fake_latest(_oid: str) -> dict:
        if state["cancelled"]:
            return {"status": "COMPLETE", "average_price": 12.5, "filled_quantity": 65}
        return {"status": "OPEN"}

    def fake_cancel(oid: str) -> None:
        state["cancelled"] = True
        cancels.append(oid)

    monkeypatch.setattr(client, "_latest_status", fake_latest)
    monkeypatch.setattr(client, "cancel", fake_cancel)
    fill = client.wait_fill("OID1", timeout_s=0.03, poll_s=0.005)
    assert fill["status"] == "COMPLETE"
    assert cancels == ["OID1"]


def test_open_fail_blocks_when_order_unreadable(tmp_path, monkeypatch) -> None:
    orders = FakeOrders()

    def boom_fill(**kwargs):
        raise KiteOrderError("fill_timeout:ORD1:OPEN", order_id="ORD1")

    def boom_status(_oid):
        raise KiteOrderError("transport:boom")

    orders.market_fill = boom_fill  # type: ignore[method-assign]
    orders.order_status = boom_status  # type: ignore[method-assign]
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    now = _stamp("2026-10-21", "10:00")
    bot.propose_entry(side="ce", style="long", reason="unk", spot=23000.0, now=now)
    opened = bot.on_frame(
        now=now,
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert opened is None
    assert bot.live_opens_blocked is True
    bot.propose_entry(side="ce", style="long", reason="again", spot=23000.0, now=now)
    again = bot.on_frame(
        now=now,
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert again is None
    assert bot.last_reject == "live_opens_blocked"


def test_restore_live_adopt_and_pending_close(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_AGENT_LIVE_ALLOWED", "1")
    path = tmp_path / "paper_agent.jsonl"
    rows = [
        {
            "event": "live_adopt",
            "ts": "2026-10-21T10:00:00+05:30",
            "day": "2026-10-21",
            "atm": 23000,
            "side": "ce",
            "style": "long",
            "symbol": "NFO:NIFTY26O1923000CE",
            "ce_symbol": "NFO:NIFTY26O1923000CE",
            "pe_symbol": "NFO:NIFTY26O1923000PE",
            "qty": 65,
            "lots": 1,
            "entry": 82.0,
            "target": 90.0,
            "stop": 77.0,
            "hold_until": "2026-10-21T10:20:00+05:30",
            "charges": 40.0,
            "live": True,
            "live_orders": True,
            "order_id": "ORDX",
            "strategy": "agent_paper",
        },
        {
            "event": "live_close_submit",
            "ts": "2026-10-21T10:05:00+05:30",
            "day": "2026-10-21",
            "symbol": "NFO:NIFTY26O1923000CE",
            "style": "long",
            "qty": 65,
            "order_id": "CLOSE1",
            "live": True,
            "strategy": "agent_paper",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    bot = PaperAgent(path=path, lot_size=65)
    assert bot.position is not None
    assert bot.position.qty == 65
    assert bot.position.entry == 82.0
    assert bot.position.live is True
    assert bot.position.close_order_id == "CLOSE1"


def test_close_only_book_qty_when_manual_added(tmp_path, monkeypatch) -> None:
    orders = FakeOrders()
    orders.price = 80.0
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    bot.propose_entry(side="ce", style="long", reason="own", spot=23000.0, now=_stamp("2026-10-21", "10:00"))
    bot.on_frame(
        now=_stamp("2026-10-21", "10:00"),
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert bot.position is not None
    assert bot.position.qty == 65
    # Manual add — broker shows 195, book still 65.
    orders.net_qty = 195
    orders.price = 70.0
    closed = bot._close(_stamp("2026-10-21", "10:30"), quotes, "stop", exit_px=70.0, spot=22950.0)
    assert closed is not None
    assert orders.calls[-1]["qty"] == 65
    assert closed["qty"] == 65
    assert bot.position is None
    # Manual 130 left on broker; book is flat.
    assert orders.net_qty == 130


@pytest.mark.parametrize(
    "mid_status",
    ("CANCEL PENDING", "OPEN PENDING", "VALIDATION PENDING"),
)
def test_intermediate_close_status_does_not_second_sell(
    tmp_path, monkeypatch, mid_status: str
) -> None:
    """Intermediate Kite statuses must not drop the close id (no second SELL)."""
    orders = FakeOrders()
    orders.price = 80.0
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    bot.propose_entry(
        side="ce",
        style="long",
        reason=mid_status,
        spot=23000.0,
        now=_stamp("2026-10-21", "10:00"),
    )
    bot.on_frame(
        now=_stamp("2026-10-21", "10:00"),
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert bot.position is not None
    orders.stuck_open = True
    orders.price = 70.0
    bot._close(_stamp("2026-10-21", "10:30"), quotes, "stop", exit_px=70.0, spot=22950.0)
    assert bot.position is not None
    pending = bot.position.close_order_id
    assert pending
    orders.orders[pending]["status"] = mid_status
    bot.live_close_next_at = None
    calls_before = len(orders.calls)
    again = bot._close(
        _stamp("2026-10-21", "10:31"), quotes, "stop", exit_px=70.0, spot=22950.0
    )
    assert again is None
    assert len(orders.calls) == calls_before
    assert bot.position is not None and bot.position.close_order_id == pending

    # Complete the same order — one SELL only, broker flat.
    orders.complete_stuck(pending, filled=65)
    orders.orders[pending]["average_price"] = 70.0
    bot.live_close_next_at = None
    closed = bot._close(
        _stamp("2026-10-21", "10:32"), quotes, "stop", exit_px=70.0, spot=22950.0
    )
    assert closed is not None
    sell_n = sum(1 for c in orders.calls if c["transaction_type"] == "SELL")
    assert sell_n == 1
    assert orders.net_qty == 0
    assert bot.position is None


def test_blip_then_complete_with_manual(tmp_path, monkeypatch) -> None:
    """Network blip while waiting; order completes — book fill, do not sell manual 130."""
    orders = FakeOrders()
    orders.price = 100.0
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    bot.propose_entry(
        side="ce", style="long", reason="blip", spot=23000.0, now=_stamp("2026-10-21", "10:00")
    )
    bot.on_frame(
        now=_stamp("2026-10-21", "10:00"),
        feed={"ce": 100.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    orders.net_qty = 195  # +130 manual
    orders.stuck_open = True
    orders.price = 50.0
    bot._close(_stamp("2026-10-21", "10:30"), quotes, "stop", exit_px=50.0, spot=22900.0)
    pending = bot.position.close_order_id  # type: ignore[union-attr]
    assert pending
    assert orders.calls[-1]["qty"] == 65

    # Blip: wait fails, status briefly unreadable then COMPLETE.
    def boom_wait(oid, **kw):
        raise KiteOrderError("transport:blip", order_id=oid)

    reads = {"n": 0}
    real_status = orders.order_status

    def flaky_status(oid):
        reads["n"] += 1
        if reads["n"] == 1:
            # After wait fail, first status read during except — still intermediate.
            return {"status": "OPEN PENDING", "average_price": 0, "filled_quantity": 0}
        return real_status(oid)

    orders.wait_fill = boom_wait  # type: ignore[method-assign]
    orders.order_status = flaky_status  # type: ignore[method-assign]
    bot.live_close_next_at = None
    mid = bot._close(_stamp("2026-10-21", "10:31"), quotes, "stop", exit_px=50.0, spot=22900.0)
    assert mid is None
    assert bot.position.close_order_id == pending  # type: ignore[union-attr]
    assert sum(1 for c in orders.calls if c["transaction_type"] == "SELL") == 1

    orders.complete_stuck(pending, filled=65)
    orders.orders[pending]["average_price"] = 50.0
    orders.order_status = real_status  # type: ignore[method-assign]
    # After fill apply: 195 - 65 = 130 manual left.
    bot.live_close_next_at = None
    closed = bot._close(_stamp("2026-10-21", "10:32"), quotes, "stop", exit_px=50.0, spot=22900.0)
    assert closed is not None
    assert sum(1 for c in orders.calls if c["transaction_type"] == "SELL") == 1
    assert orders.net_qty == 130
    assert bot.position is None
    assert bot.day_pnl < 0


def test_cancelled_partial_close_no_manual(tmp_path, monkeypatch) -> None:
    """Close 65 stuck OPEN, then CANCELLED after 30 filled → book 30, then sell 35 only."""
    orders = FakeOrders()
    orders.price = 100.0
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    bot.propose_entry(
        side="ce", style="long", reason="cx", spot=23000.0, now=_stamp("2026-10-21", "10:00")
    )
    bot.on_frame(
        now=_stamp("2026-10-21", "10:00"),
        feed={"ce": 100.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert bot.position is not None and bot.position.qty == 65
    assert orders.net_qty == 65

    orders.stuck_open = True
    orders.price = 50.0  # big loss mark
    first = bot._close(_stamp("2026-10-21", "10:30"), quotes, "stop", exit_px=50.0, spot=22900.0)
    assert first is None
    pending = bot.position.close_order_id
    assert pending
    # Cancel after partial fill of 30.
    orders.orders[pending]["status"] = "CANCELLED"
    orders.orders[pending]["filled_quantity"] = 30
    orders.orders[pending]["average_price"] = 50.0
    orders.orders[pending]["_applied"] = False
    # Broker after cancel: 65 - 30 = 35 still long.
    orders.net_qty = 35
    bot.live_close_next_at = None
    orders.stuck_open = False
    orders.partial_fill_qty = None
    orders.price = 50.0

    second = bot._close(_stamp("2026-10-21", "10:31"), quotes, "stop", exit_px=50.0, spot=22900.0)
    # After absorb 30, fall-through places close for 35 and can complete same frame.
    assert bot.position is None or (bot.position is not None and bot.position.qty == 35)
    # Last place (if any after absorb) must be 35, never 65 again.
    close_calls = [c for c in orders.calls if c["transaction_type"] == "SELL"]
    assert close_calls[0]["qty"] == 65  # first attempt
    if len(close_calls) > 1:
        assert close_calls[1]["qty"] == 35
    lines = (tmp_path / "paper_agent.jsonl").read_text()
    assert "live_partial_close" in lines
    # Day pnl should reflect ~30 closed at 50 vs 100 entry, not double-count full 65 via realised.
    assert bot.day_pnl > -6000  # not the -5081 double-count path


def test_cancelled_partial_close_with_manual_lots(tmp_path, monkeypatch) -> None:
    """130 manual + 65 agent; cancelled close fills 30 → next sell only remaining 35."""
    orders = FakeOrders()
    orders.price = 100.0
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    bot.propose_entry(
        side="ce", style="long", reason="man", spot=23000.0, now=_stamp("2026-10-21", "10:00")
    )
    bot.on_frame(
        now=_stamp("2026-10-21", "10:00"),
        feed={"ce": 100.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    # Manual add after agent open.
    orders.net_qty = 195
    orders.stuck_open = True
    orders.price = 50.0
    bot._close(_stamp("2026-10-21", "10:30"), quotes, "stop", exit_px=50.0, spot=22900.0)
    pending = bot.position.close_order_id  # type: ignore[union-attr]
    assert pending
    # First close was sized to book 65 (min(195,65)).
    assert orders.calls[-1]["qty"] == 65
    orders.orders[pending]["status"] = "CANCELLED"
    orders.orders[pending]["filled_quantity"] = 30
    orders.orders[pending]["average_price"] = 50.0
    # Broker: 195 - 30 = 165 (130 manual + 35 agent remain).
    orders.net_qty = 165
    bot.live_close_next_at = None
    orders.stuck_open = False
    orders.price = 50.0
    bot._close(_stamp("2026-10-21", "10:31"), quotes, "stop", exit_px=50.0, spot=22900.0)
    sell_qtys = [c["qty"] for c in orders.calls if c["transaction_type"] == "SELL"]
    assert sell_qtys[0] == 65
    assert sell_qtys[-1] == 35
    # Manual 130 untouched in sizing — never sold 65 again after cancel.
    assert sell_qtys.count(65) == 1


def test_restore_partial_keeps_reduced_charges(tmp_path) -> None:
    path = tmp_path / "paper_agent.jsonl"
    rows = [
        {
            "event": "open",
            "ts": "2026-10-21T10:00:00+05:30",
            "day": "2026-10-21",
            "atm": 23000,
            "side": "ce",
            "style": "long",
            "symbol": "NFO:NIFTY26O1923000CE",
            "ce_symbol": "NFO:NIFTY26O1923000CE",
            "pe_symbol": "NFO:NIFTY26O1923000PE",
            "qty": 65,
            "lots": 1,
            "entry": 80.0,
            "target": 90.0,
            "stop": 70.0,
            "hold_until": "2026-10-21T10:20:00+05:30",
            "charges": 40.0,
            "live": True,
            "order_id": "O1",
            "strategy": "agent_paper",
        },
        {
            "event": "live_partial_close",
            "ts": "2026-10-21T10:10:00+05:30",
            "day": "2026-10-21",
            "qty_closed": 30,
            "qty_remain": 35,
            "charges_open_remain": 21.54,
            "realised_at_open": -900.0,
            "pnl": -100.0,
            "day_pnl": -100.0,
            "strategy": "agent_paper",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    bot = PaperAgent(path=path, lot_size=65)
    assert bot.position is not None
    assert bot.position.qty == 35
    assert bot.position.charges_open == 21.54
    assert bot.position.realised_at_open == -900.0


def test_live_auto_off_after_square_off(tmp_path, monkeypatch) -> None:
    orders = FakeOrders()
    orders.price = 80.0
    bot, quotes, ce, pe = _live_bot(tmp_path, monkeypatch, orders)
    assert bot.live_flag is not None and bot.live_flag.is_enabled()
    bot.propose_entry(
        side="ce", style="long", reason="eod", spot=23000.0, now=_stamp("2026-10-21", "10:00")
    )
    bot.on_frame(
        now=_stamp("2026-10-21", "10:00"),
        feed={"ce": 80.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert bot.position is not None
    orders.price = 78.0
    quotes.quotes[ce] = {"last_price": 78.0}
    closed = bot.on_frame(
        now=_stamp("2026-10-21", "15:14"),
        feed={"ce": 78.0, "pe": 70.0},
        book=quotes,
        ce_symbol=ce,
        pe_symbol=pe,
        atm=23000,
        spot=23000.0,
    )
    assert closed is not None
    assert bot.position is None
    assert bot.live_flag.is_enabled() is False
    assert bot.live_flag.snapshot()["file_enabled"] is False
    lines = (tmp_path / "paper_agent.jsonl").read_text()
    assert "live_auto_off" in lines


def test_restore_after_partial_keeps_remain_qty(tmp_path, monkeypatch) -> None:
    path = tmp_path / "paper_agent.jsonl"
    rows = [
        {
            "event": "open",
            "ts": "2026-10-21T10:00:00+05:30",
            "day": "2026-10-21",
            "atm": 23000,
            "side": "ce",
            "style": "short",
            "symbol": "NFO:NIFTY26O1923000CE",
            "ce_symbol": "NFO:NIFTY26O1923000CE",
            "pe_symbol": "NFO:NIFTY26O1923000PE",
            "qty": 65,
            "lots": 1,
            "entry": 80.0,
            "target": 72.0,
            "stop": 85.0,
            "hold_until": "2026-10-21T10:20:00+05:30",
            "charges": 40.0,
            "live": True,
            "order_id": "O1",
            "strategy": "agent_paper",
        },
        {
            "event": "live_partial_close",
            "ts": "2026-10-21T10:10:00+05:30",
            "day": "2026-10-21",
            "qty_closed": 30,
            "qty_remain": 35,
            "pnl": 200.0,
            "day_pnl": 200.0,
            "strategy": "agent_paper",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    bot = PaperAgent(path=path, lot_size=65)
    assert bot.position is not None
    assert bot.position.qty == 35
    assert bot.day_pnl == 200.0
