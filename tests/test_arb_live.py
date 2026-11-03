"""Arb live flag, windows, ₹2,000 day-loss stop, audit accounting."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from atlas_lite.arb_live import (
    ATTEMPT_UNTIL,
    DEFAULT_DAY_LOSS_STOP,
    ArbLiveDayState,
    ArbLiveFlag,
    day_loss_stop_rs,
    within_attempt_hours,
    within_live_window,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nse_bse_gap_watch as W  # noqa: E402


def test_live_flag_locked_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ATLAS_LITE_ARB_LIVE_ALLOWED", raising=False)
    flag = ArbLiveFlag(tmp_path / "arb_live.json")
    assert flag.snapshot()["allowed"] is False
    assert flag.is_enabled() is False
    with pytest.raises(ValueError, match="live_not_allowed"):
        flag.set_enabled(True, source="test")


def test_live_flag_needs_env_and_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_ALLOWED", "1")
    flag = ArbLiveFlag(tmp_path / "arb_live.json")
    assert flag.snapshot()["allowed"] is True
    assert flag.is_enabled() is False
    flag.set_enabled(True, source="test")
    assert flag.is_enabled() is True
    assert flag.snapshot()["day"]  # armed for today
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_ALLOWED", "0")
    assert flag.is_enabled() is False


def test_stale_live_flag_clears(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_ALLOWED", "1")
    flag = ArbLiveFlag(tmp_path / "arb_live.json")
    flag.path.write_text(
        '{"enabled": true, "day": "2026-11-01", "updated_at": "2026-11-01T15:30:00+05:30", "source": "day_end"}\n'
    )
    assert flag.snapshot()["stale"] is True
    assert flag.is_enabled() is False
    cleared = flag.clear_if_stale()
    assert cleared is not None
    assert cleared["file_enabled"] is False
    assert cleared["enabled"] is False


def test_missing_day_on_enabled_flag_is_stale(tmp_path, monkeypatch) -> None:
    """Audit A: ops_restore file without day must not arm live."""
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_ALLOWED", "1")
    flag = ArbLiveFlag(tmp_path / "arb_live.json")
    flag.path.write_text(
        '{"enabled": true, "updated_at": "2026-11-03T19:56:33+05:30", "source": "ops_restore"}\n'
    )
    assert flag.snapshot()["stale"] is True
    assert flag.is_enabled() is False
    assert flag.clear_if_stale()["enabled"] is False


def test_day_loss_stop_default_and_env(monkeypatch) -> None:
    monkeypatch.delenv("ATLAS_LITE_ARB_LIVE_DAY_STOP", raising=False)
    assert day_loss_stop_rs() == DEFAULT_DAY_LOSS_STOP
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_DAY_STOP", "2000")
    assert day_loss_stop_rs() == 2000.0


def test_windows(monkeypatch) -> None:
    monkeypatch.delenv("ATLAS_LITE_ARB_LIVE_AFTER", raising=False)
    monkeypatch.delenv("ATLAS_LITE_ARB_LIVE_UNTIL", raising=False)
    assert within_attempt_hours("15:14") is True
    assert within_attempt_hours(ATTEMPT_UNTIL) is False
    assert within_attempt_hours("15:20") is False
    assert within_live_window("09:15") is True
    assert within_live_window("09:19") is True
    assert within_live_window("09:20") is False
    assert within_live_window("10:00") is False
    assert within_live_window("15:20") is False


def test_day_state_stops_at_2000(tmp_path) -> None:
    state = ArbLiveDayState(tmp_path / "live-state.json", day="2026-11-04", stop_rs=2000.0)
    state.note_trade(-800.0)
    assert state.stopped is False
    snap = state.note_trade(-1500.0)
    assert snap["stopped"] is True
    assert snap["day_pnl"] == -2300.0
    assert "day_loss_stop" in snap["stop_reason"]
    again = ArbLiveDayState(tmp_path / "live-state.json", day="2026-11-04", stop_rs=2000.0)
    assert again.stopped is True
    assert again.day_pnl == -2300.0


def test_zero_fill_does_not_count_trade(tmp_path) -> None:
    state = ArbLiveDayState(tmp_path / "live-state.json", day="2026-11-04", stop_rs=2000.0)
    state.note_trade(None, count=False)
    assert state.trades == 0
    state.note_trade(0.0, count=False)
    assert state.trades == 0
    assert state.day_pnl == 0.0


def test_unwind_adverse_counts_and_blocks(tmp_path) -> None:
    state = ArbLiveDayState(tmp_path / "live-state.json", day="2026-11-04", stop_rs=2000.0)
    state.note_trade(-500.0, count=True)
    state.block_opens("unwind_failed:test")
    assert state.opens_blocked is True
    assert state.snapshot()["entries_allowed"] is False
    assert state.trades == 1


def test_maybe_live_respects_window(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_ALLOWED", "1")
    monkeypatch.setattr(W, "within_live_window", lambda: False)
    flag = ArbLiveFlag(tmp_path / "arb_live.json")
    flag.set_enabled(True, source="test")
    universe = {"AAA": ("small", "AAA")}
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-04", universe,
                live_flag=flag, order_client=object())  # type: ignore[arg-type]
    w._resume_until = 0.0
    w.maybe_live(
        "AAA", "BSE", "NSE",
        {"ask": 100.0, "ask_qty": 500, "bid": 99.9, "bid_qty": 500},
        {"ask": 102.0, "ask_qty": 500, "bid": 101.5, "bid_qty": 500},
        1_000_000.0,
        ("AAA", "buy BSE sell NSE", 1_000_000.0, "root-1"),
        ticks=2,
    )
    assert w._tasks == set()


def test_maybe_paper_blocked_after_1515(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(W, "within_attempt_hours", lambda: False)
    monkeypatch.setattr(W, "LATENCIES_MS", (100,))
    monkeypatch.setattr(W, "MIN_PAPER_NET", 0.0)
    monkeypatch.setattr(W, "RESEARCH_NETS", ())
    universe = {"AAA": ("small", "AAA")}
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-04", universe)
    w._resume_until = 0.0
    w.maybe_paper(
        "AAA", "BSE", "NSE",
        {"ask": 100.0, "ask_qty": 100, "bid": 99.9, "bid_qty": 100},
        {"ask": 101.0, "ask_qty": 100, "bid": 100.8, "bid_qty": 100},
        1.0,
        ("AAA", "buy BSE sell NSE", 1.0, "root"),
        ticks=1,
    )
    assert w._tasks == set()


def test_summary_includes_mid(tmp_path) -> None:
    universe = {"AAA": ("small", "AAA"), "BBB": ("mid", "BBB"), "CCC": ("large", "CCC")}
    tokens = {1: ("NSE", "AAA"), 2: ("BSE", "AAA"), 3: ("NSE", "BBB"), 4: ("BSE", "BBB"),
              5: ("NSE", "CCC"), 6: ("BSE", "CCC")}
    w = W.Watch(tokens, tmp_path, "2026-11-04", universe)
    report = w.write_report(final=True)
    assert "mid" in report and "small" in report and "large" in report
    assert report["mid"]["stocks"] == 1
    assert "paper_before_1515" in report["small"]
    assert report["attempt_until"] == "15:15"


class _FakeOrders:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def equity_available(self):
        return 500_000.0

    def basket_margin_required(self, orders):
        return 50_000.0

    def ioc_limit_fill(self, **kwargs):
        self.calls.append(("ioc", kwargs))
        side = kwargs["transaction_type"]
        px = float(kwargs["price"])
        return {
            "order_id": f"oid-{side}",
            "average_price": px,
            "quantity": int(kwargs["qty"]),
            "status": "COMPLETE",
            "transaction_type": side,
        }

    def market_fill(self, **kwargs):
        self.calls.append(("mkt", kwargs))
        return {
            "order_id": "oid-uw",
            "average_price": float(kwargs.get("price") or 100.0),
            "quantity": int(kwargs["qty"]),
            "status": "COMPLETE",
        }


def test_live_attempt_both_opens_hedge(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_ALLOWED", "1")
    monkeypatch.setattr(W, "within_live_window", lambda: True)
    flag = ArbLiveFlag(tmp_path / "arb_live.json")
    flag.set_enabled(True, source="test")
    orders = _FakeOrders()
    universe = {"AAA": ("small", "AAA", "A")}
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-04", universe,
                live_flag=flag, order_client=orders, fo_underlyings={"AAA"})  # type: ignore[arg-type]
    asyncio.run(w.live_attempt(
        "AAA", "BSE", "NSE", 100.0, 101.0, 100, 80.0, "root-1",
        crossing_ticks=2, direction="buy BSE sell NSE",
    ))
    assert w.live_orders[-1]["outcome"] == "both_open"
    assert len(w._live_open) == 1 and w._live_open[0]["kind"] == "hedge"
    assert w.live_state.trades == 0  # P&L deferred until flatten


def test_maybe_live_skips_non_fo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_ALLOWED", "1")
    monkeypatch.setattr(W, "within_live_window", lambda: True)
    monkeypatch.setattr(W, "MIN_PAPER_NET", 1.0)
    flag = ArbLiveFlag(tmp_path / "arb_live.json")
    flag.set_enabled(True, source="test")
    w = W.Watch(
        {1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-04",
        {"AAA": ("small", "AAA", "A")},
        live_flag=flag, order_client=object(), fo_underlyings=set(),  # type: ignore[arg-type]
    )
    w._resume_until = 0.0
    w.maybe_live(
        "AAA", "BSE", "NSE",
        {"ask": 100.0, "ask_qty": 500, "bid": 99.5, "bid_qty": 500},
        {"ask": 102.0, "ask_qty": 500, "bid": 101.5, "bid_qty": 500},
        1.0, ("AAA", "buy BSE sell NSE", 1.0, "root"), ticks=2,
    )
    assert w._tasks == set()


def test_round_trip_est_below_entry_only() -> None:
    buy_q = {"ask": 100.0, "bid": 99.8}
    sell_q = {"ask": 101.2, "bid": 101.0}
    entry_only = (101.0 - 100.0) * 100 - W.intraday_cost(100.0, 101.0, 100, "BSE")
    rt = W.round_trip_est_net(100.0, 101.0, 100, "BSE", buy_q, sell_q)
    assert rt < entry_only


def test_flatten_backoff_and_max_attempts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_ALLOWED", "1")
    flag = ArbLiveFlag(tmp_path / "arb_live.json")
    flag.set_enabled(True, source="test")
    w = W.Watch(
        {1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-04",
        {"AAA": ("small", "AAA", "A")},
        live_flag=flag, order_client=object(), fo_underlyings={"AAA"},  # type: ignore[arg-type]
    )
    w._live_open = [{
        "kind": "hedge", "stock": "AAA", "buy_exchange": "BSE", "sell_exchange": "NSE",
        "buy_symbol": "BSE:AAA", "sell_symbol": "NSE:AAA",
        "buy_px": 100.0, "sell_px": 101.0, "qty": 10,
        "flatten_attempts": 1, "flatten_last_fail_at": __import__("time").time(),
    }]
    w.maybe_flatten_live(force=True)  # within 5s backoff
    assert w._tasks == set()
    w._live_open[0]["flatten_attempts"] = W.FLATTEN_MAX_ATTEMPTS
    w.maybe_flatten_live(force=True)
    assert w._live_open[0].get("manual_needed") is True
    assert w._tasks == set()


def test_flatten_exits_only_open_qty(tmp_path, monkeypatch) -> None:
    """Audit C: zero-qty leg must not get an exit order."""
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_ALLOWED", "1")
    monkeypatch.setattr(W, "within_live_window", lambda: True)

    class PartialPos(_FakeOrders):
        def __init__(self) -> None:
            super().__init__()
            self.qtys = {"BSE:AAA": 50, "NSE:AAA": 0}  # sell leg already flat

        def net_position_qty(self, symbol, product="MIS"):
            return int(self.qtys.get(symbol, 0))

        def market_fill(self, **kwargs):
            self.calls.append(("mkt", kwargs))
            return {
                "order_id": "oid-uw",
                "average_price": 100.0,
                "quantity": int(kwargs["qty"]),
                "status": "COMPLETE",
            }

    flag = ArbLiveFlag(tmp_path / "arb_live.json")
    flag.set_enabled(True, source="test")
    orders = PartialPos()
    w = W.Watch(
        {1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-04",
        {"AAA": ("small", "AAA", "A")},
        live_flag=flag, order_client=orders, fo_underlyings={"AAA"},  # type: ignore[arg-type]
    )
    pos = {
        "kind": "hedge", "stock": "AAA",
        "buy_exchange": "BSE", "sell_exchange": "NSE",
        "buy_symbol": "BSE:AAA", "sell_symbol": "NSE:AAA",
        "buy_px": 100.0, "sell_px": 101.0, "qty": 50,
        "entry_charges": 0.0, "entry_unwind_pnl": 0.0,
    }
    w._live_open = [pos]
    asyncio.run(w._flatten_open(pos, reason="test"))
    assert len(orders.calls) == 1
    assert orders.calls[0][1]["transaction_type"] == "SELL"
    assert orders.calls[0][1]["qty"] == 50
    assert orders.calls[0][1]["symbol"] == "BSE:AAA"
    assert w.live_orders[-1]["outcome"] == "hedge_closed"
    assert w.live_orders[-1]["long_exit"] == 50
    assert w.live_orders[-1]["short_exit"] == 0


def test_margin_api_fail_skips(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_ALLOWED", "1")
    monkeypatch.setattr(W, "within_live_window", lambda: True)

    class BadMargin(_FakeOrders):
        def basket_margin_required(self, orders):
            from atlas_lite.kite_orders import KiteOrderError
            raise KiteOrderError("http_400:not shortable")

    flag = ArbLiveFlag(tmp_path / "arb_live.json")
    flag.set_enabled(True, source="test")
    w = W.Watch(
        {1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-04",
        {"AAA": ("small", "AAA", "A")},
        live_flag=flag, order_client=BadMargin(), fo_underlyings={"AAA"},  # type: ignore[arg-type]
    )
    asyncio.run(w.live_attempt(
        "AAA", "BSE", "NSE", 100.0, 101.0, 100, 80.0, "root-1",
        crossing_ticks=2, direction="buy BSE sell NSE",
    ))
    assert w.live_orders[-1]["outcome"] == "skipped"
    assert "basket_margin_failed" in w.live_orders[-1]["reason"]


def test_live_attempt_none_does_not_count(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_ARB_LIVE_ALLOWED", "1")
    monkeypatch.setattr(W, "LIVE_LATENCY_MS", 0)
    monkeypatch.setattr(W, "within_live_window", lambda: True)

    class Empty(_FakeOrders):
        def ioc_limit_fill(self, **kwargs):
            return {"order_id": "x", "average_price": None, "quantity": 0, "status": "CANCELLED"}

    flag = ArbLiveFlag(tmp_path / "arb_live.json")
    flag.set_enabled(True, source="test")
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-04",
                {"AAA": ("small", "AAA", "A")}, live_flag=flag, order_client=Empty(),
                fo_underlyings={"AAA"})  # type: ignore[arg-type]
    asyncio.run(w.live_attempt(
        "AAA", "BSE", "NSE", 100.0, 101.0, 100, 80.0, "root-1",
        crossing_ticks=2, direction="buy BSE sell NSE",
    ))
    assert w.live_orders[-1]["outcome"] == "none"
    assert w.live_state.trades == 0
