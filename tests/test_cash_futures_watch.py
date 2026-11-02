"""Cash-futures spread paper watcher (read-only research, never places orders)."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cash_futures_watch as C  # noqa: E402

UNI = {"ABC": {"cash_token": 1, "fut_token": 2, "fut_symbol": "ABC26NOVFUT", "expiry": date(2026, 11, 23), "lot": 500}}


def mk(tmp_path, monkeypatch, capital=250_000.0):
    monkeypatch.setattr(C, "LATENCY_MS", 50)
    monkeypatch.setattr(C, "NEXT_QUOTE_WAIT_S", 0.3)
    w = C.CF(UNI, tmp_path, "2026-11-02", capital)
    w.minute_mid["ABC"] = __import__("collections").deque((f"10:{i:02d}", 0.40) for i in range(45))  # baseline 0.40%
    return w


def tick(w, tok, bid, ask, bq=1000, aq=1000):
    w.on_tick({"instrument_token": tok, "bid": bid, "ask": ask,
               "depth": {"buy": [{"price": bid, "quantity": bq}], "sell": [{"price": ask, "quantity": aq}]}})


async def settle(w):
    while w._tasks:
        await asyncio.sleep(0.02)


def at(monkeypatch, hm):
    """Freeze the hour:minute the watcher sees in check()."""
    real = C.datetime

    class Clock(real):
        @classmethod
        def fromtimestamp(cls, ts, tz=None):
            d = real.fromtimestamp(ts, tz)
            return d.replace(hour=int(hm[:2]), minute=int(hm[3:]))

    monkeypatch.setattr(C, "datetime", Clock)


def test_costs_for_a_reliance_sized_lot_are_in_the_expected_range() -> None:
    cost = C.round_trip_cost(1400.0, 1410.0, 500)  # ~₹7 lakh per leg
    assert 450 < cost < 650


def test_wide_spread_opens_and_reversion_closes_with_bid_ask_fills(tmp_path, monkeypatch) -> None:
    w = mk(tmp_path, monkeypatch)
    at(monkeypatch, "11:00")

    async def go():
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1010.0, 1010.1)  # executable spread (1010.0 - 1000.0)/1000 = 1.0% vs 0.40%: +0.60%
        await asyncio.sleep(0.1)
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1010.0, 1010.1)  # still there when the orders arrive: both legs fill
        await settle(w)
        assert w.position is not None and w.position["cash_buy"] == 1000.0 and w.position["fut_sell"] == 1010.0
        tick(w, 1, 1004.9, 1005.0)
        tick(w, 2, 1008.9, 1009.0)  # exit spread (1009.0 - 1004.9)/~1005 = 0.41%: not yet back to 0.40%
        await settle(w)
        assert w.position is not None
        tick(w, 2, 1008.8, 1008.9)  # (1008.9 - 1004.9)/~1005 = 0.398%: back at the baseline
        await asyncio.sleep(0.1)
        tick(w, 1, 1004.9, 1005.0)
        tick(w, 2, 1008.8, 1008.9)
        await settle(w)

    asyncio.run(go())
    (close,) = [e for e in w.events if e["event"] == "close"]
    assert close["reason"] == "reverted" and close["cash_sell"] == 1004.9 and close["fut_buy"] == 1008.9
    assert close["gross"] == round((1010.0 - 1008.9) * 500 + (1004.9 - 1000.0) * 500, 2)
    assert close["pnl"] == round(close["gross"] - close["charges"], 2) and close["pnl"] > 0
    rep = w.report(final=True)
    assert rep["signals"] == 1 and rep["trades"] == 1 and rep["wins"] == 1 and rep["net"] == close["pnl"]


def test_a_lot_larger_than_the_shown_size_does_not_fill(tmp_path, monkeypatch) -> None:
    w = mk(tmp_path, monkeypatch)
    at(monkeypatch, "11:00")

    async def go():
        tick(w, 1, 999.9, 1000.0, aq=100)  # only 100 shares on the ask, lot is 500
        tick(w, 2, 1010.0, 1010.1, bq=100)
        await asyncio.sleep(0.1)
        tick(w, 1, 999.9, 1000.0, aq=100)
        tick(w, 2, 1010.0, 1010.1, bq=100)
        await settle(w)

    asyncio.run(go())
    assert w.position is None and [e["why"] for e in w.events if e["event"] == "skip"] == ["neither leg filled"]


def test_one_leg_is_unwound_at_market(tmp_path, monkeypatch) -> None:
    w = mk(tmp_path, monkeypatch)
    at(monkeypatch, "11:00")

    async def go():
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1010.0, 1010.1)
        await asyncio.sleep(0.1)
        tick(w, 1, 999.9, 1000.0)  # cash ask still there: buy fills
        tick(w, 2, 1008.0, 1008.1)  # futures bid fell below 1010: sell misses
        await asyncio.sleep(0.1)
        tick(w, 1, 999.5, 999.7)  # unwind: sell the 500 shares at 999.5
        await settle(w)

    asyncio.run(go())
    (u,) = [e for e in w.events if e["event"] == "unwind"]
    assert u["leg"] == "cash" and u["pnl"] < round((999.5 - 1000.0) * 500, 2) and w.position is None


def test_signals_outside_capital_are_logged_but_not_traded(tmp_path, monkeypatch) -> None:
    w = mk(tmp_path, monkeypatch, capital=50_000.0)  # one lot (~₹5 lakh) needs ~₹1.9 lakh margin
    at(monkeypatch, "11:00")

    async def go():
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1010.0, 1010.1)
        await settle(w)

    asyncio.run(go())
    (s,) = [e for e in w.events if e["event"] == "signal"]
    assert s["fits_capital"] is False and s["traded"] is False and w.position is None


def test_no_entry_before_1015_or_without_a_baseline(tmp_path, monkeypatch) -> None:
    w = mk(tmp_path, monkeypatch)
    at(monkeypatch, "10:05")

    async def go():
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1010.0, 1010.1)
        await settle(w)

    asyncio.run(go())
    assert not [e for e in w.events if e["event"] == "signal"]
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert C.CF(UNI, fresh, "2026-11-02").baseline("ABC") is None  # fewer than 30 minutes seen


def test_restart_resumes_the_open_position_and_exits_it(tmp_path, monkeypatch) -> None:
    (tmp_path / "cf-2026-11-02.jsonl").write_text(json.dumps(
        {"ts": "2026-11-02T11:00:00", "event": "open", "stock": "ABC", "lot": 500, "cash_buy": 1000.0,
         "fut_sell": 1010.0, "baseline": 0.40, "est_net": 2500.0}) + "\n")
    w = mk(tmp_path, monkeypatch)
    assert w.position is not None and w.position["stock"] == "ABC"
    at(monkeypatch, "15:15")

    async def go():
        tick(w, 1, 1004.9, 1005.0)
        tick(w, 2, 1015.0, 1015.1)  # not reverted, but 15:15: forced exit
        await asyncio.sleep(0.1)
        tick(w, 1, 1004.9, 1005.0)
        tick(w, 2, 1015.0, 1015.1)
        await settle(w)

    asyncio.run(go())
    (close,) = [e for e in w.events if e["event"] == "close"]
    assert close["reason"] == "time" and w.position is None
    assert C.CF(UNI, tmp_path, "2026-11-02").position is None  # the close is in the log too


def test_universe_picks_the_nearest_future_at_least_three_days_out(tmp_path) -> None:
    (tmp_path / "nse_instruments.csv").write_text(
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
        "11,1,ABC,ABC,0,,0,0.05,1,EQ,NSE,NSE\n")
    (tmp_path / "nfo_instruments.csv").write_text(
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
        "21,1,ABC26NOVFUT,ABC,0,2026-11-23,0,0.05,500,FUT,NFO-FUT,NFO\n"
        "22,1,ABC26DECFUT,ABC,0,2026-12-28,0,0.05,500,FUT,NFO-FUT,NFO\n"
        "23,1,NIFTY26NOVFUT,NIFTY,0,2026-11-23,0,0.1,65,FUT,NFO-FUT,NFO\n")
    u = C.load_universe(tmp_path, date(2026, 11, 2))
    assert u == {"ABC": {"cash_token": 11, "fut_token": 21, "fut_symbol": "ABC26NOVFUT", "expiry": date(2026, 11, 23), "lot": 500}}
    assert C.load_universe(tmp_path, date(2026, 11, 21))["ABC"]["fut_symbol"] == "ABC26DECFUT"  # 2 days out: next month


def test_limit_fill_uses_levels_within_the_limit() -> None:
    asks = [(100.0, 200), (100.1, 200), (100.5, 1000)]
    assert C.limit_fill(asks, 400, 100.1, buying=True) == round((200 * 100.0 + 200 * 100.1) / 400, 4)
    assert C.limit_fill(asks, 500, 100.1, buying=True) is None  # the 100.5 level is beyond the limit
    bids = [(99.9, 300), (99.8, 300)]
    assert C.limit_fill(bids, 500, 99.8, buying=False) == round((300 * 99.9 + 200 * 99.8) / 500, 4)


def test_market_fill_walks_the_book_and_penalises_beyond_it() -> None:
    bids = [(99.9, 100), (99.8, 100)]
    assert C.market_fill(bids, 200, buying=False, ref=100.0) == 99.85
    thin = C.market_fill(bids, 400, buying=False, ref=100.0)  # 200 more than shown: 0.5% below 99.8
    assert thin == round((100 * 99.9 + 100 * 99.8 + 200 * 99.8 * 0.995) / 400, 4)
    assert C.market_fill([], 100, buying=True, ref=100.0) == 100.5


def test_exit_walks_a_thin_book(tmp_path, monkeypatch) -> None:
    (tmp_path / "cf-2026-11-02.jsonl").write_text(json.dumps(
        {"ts": "t", "event": "open", "stock": "ABC", "lot": 500, "cash_buy": 1000.0, "fut_sell": 1010.0,
         "baseline": 0.40, "est_net": 1.0}) + "\n")
    w = mk(tmp_path, monkeypatch)
    w.quotes[("cash", "ABC")] = {"bid": 1004.9, "ask": 1005.0, "bids": [(1004.9, 200), (1004.8, 300)], "asks": [(1005.0, 500)], "at": 0}
    w.quotes[("fut", "ABC")] = {"bid": 1008.8, "ask": 1008.9, "bids": [(1008.8, 500)], "asks": [(1008.9, 100), (1009.0, 400)], "at": 0}
    asyncio.run(w.exit("reverted"))
    (close,) = [e for e in w.events if e["event"] == "close"]
    assert close["cash_sell"] == round((200 * 1004.9 + 300 * 1004.8) / 500, 4)
    assert close["fut_buy"] == round((100 * 1008.9 + 400 * 1009.0) / 500, 4)


def test_a_day_without_a_final_report_is_rebuilt_and_its_open_position_closed(tmp_path) -> None:
    (tmp_path / "cf-2026-10-30.jsonl").write_text(json.dumps(
        {"ts": "t", "event": "open", "stock": "ABC", "lot": 500, "cash_buy": 1000.0, "fut_sell": 1010.0,
         "baseline": 0.40, "est_net": 1.0}) + "\n")
    C.finalize_missing(tmp_path, "15:30")
    rep = json.loads((tmp_path / "summary-2026-10-30.json").read_text())
    assert rep["recovered"] is True and rep["final"] is True and rep["open_position"] is None
    lines = [json.loads(x) for x in (tmp_path / "cf-2026-10-30.jsonl").read_text().splitlines()]
    assert lines[-1]["event"] == "close" and lines[-1]["reason"] == "no_final_report" and lines[-1]["pnl"] is None
    C.finalize_missing(tmp_path, "15:30")  # already final: left alone
    assert len((tmp_path / "cf-2026-10-30.jsonl").read_text().splitlines()) == 2


def test_quiet_stock_is_still_force_exited_at_1515(tmp_path, monkeypatch) -> None:
    (tmp_path / "cf-2026-11-02.jsonl").write_text(json.dumps(
        {"ts": "t", "event": "open", "stock": "ABC", "lot": 500, "cash_buy": 1000.0, "fut_sell": 1010.0,
         "baseline": 0.40, "est_net": 1.0}) + "\n")
    w = mk(tmp_path, monkeypatch)
    old = 0.0  # quotes far older than FRESH_S: check() would never act on them
    w.quotes[("cash", "ABC")] = {"bid": 1004.9, "ask": 1005.0, "bids": [(1004.9, 500)], "asks": [(1005.0, 500)], "at": old}
    w.quotes[("fut", "ABC")] = {"bid": 1012.0, "ask": 1012.1, "bids": [(1012.0, 500)], "asks": [(1012.1, 500)], "at": old}
    monkeypatch.setattr(C, "hm_now", lambda: "15:16")

    async def loop_pass():  # what the session loop does each pass
        w.manage_position(C.hm_now())
        await settle(w)

    asyncio.run(loop_pass())
    (close,) = [e for e in w.events if e["event"] == "close"]
    assert close["reason"] == "time" and w.position is None


def _open_log(tmp_path):
    (tmp_path / "cf-2026-11-02.jsonl").write_text(json.dumps(
        {"ts": "2026-11-02T11:00:00+05:30", "event": "open", "stock": "ABC", "lot": 500, "cash_buy": 1000.0,
         "fut_sell": 1010.0, "baseline": 0.40, "est_net": 1.0}) + "\n")


def test_session_end_closes_at_the_last_known_book(tmp_path, monkeypatch) -> None:
    _open_log(tmp_path)
    w = mk(tmp_path, monkeypatch)
    w.quotes[("cash", "ABC")] = {"bid": 1004.9, "ask": 1005.0, "bids": [(1004.9, 500)], "asks": [(1005.0, 500)], "at": 0.0}
    w.quotes[("fut", "ABC")] = {"bid": 1012.0, "ask": 1012.1, "bids": [(1012.0, 500)], "asks": [(1012.1, 500)], "at": 0.0}
    w.close_now("session_end")
    (close,) = [e for e in w.events if e["event"] == "close"]
    assert close["pnl"] is not None and close["last_known_book"] is True and close["fut_buy"] == 1012.1
    other = tmp_path / "no_book"
    other.mkdir()
    _open_log(other)
    w2 = mk(other, monkeypatch)
    w2.close_now("session_end")  # no book at all: unknown
    assert [e for e in w2.events if e["event"] == "close"][0]["pnl"] is None


def test_restart_restores_baselines_and_signal_cooldown(tmp_path, monkeypatch) -> None:
    w = mk(tmp_path, monkeypatch)
    w._log({"event": "signal", "stock": "ABC", "est_net": 500.0})
    w.write_report(final=False)
    again = C.CF(UNI, tmp_path, "2026-11-02")
    assert again.baseline("ABC") == 0.40  # no warm-up needed
    assert "ABC" in again.last_signal


def test_skip_reason_says_why_a_signal_was_not_traded(tmp_path, monkeypatch) -> None:
    _open_log(tmp_path)
    w = mk(tmp_path, monkeypatch)
    w.universe = {**UNI, "XYZ": {**UNI["ABC"], "cash_token": 3, "fut_token": 4}}
    w.tokens.update({3: ("cash", "XYZ"), 4: ("fut", "XYZ")})
    w.minute_mid["XYZ"] = w.minute_mid["ABC"]
    at(monkeypatch, "11:00")

    async def go():
        tick(w, 3, 999.9, 1000.0)
        tick(w, 4, 1010.0, 1010.1)
        await settle(w)

    asyncio.run(go())
    (sig,) = [e for e in w.events if e["event"] == "signal"]
    assert sig["traded"] is False and sig["skip_reason"] == "position_open" and sig["fits_capital"] is True


def test_quiet_stock_exits_on_reversion_from_last_known_prices(tmp_path, monkeypatch) -> None:
    _open_log(tmp_path)
    w = mk(tmp_path, monkeypatch)
    stale = 0.0  # older than FRESH_S: check() ignores this pair, the loop pass does not
    w.quotes[("cash", "ABC")] = {"bid": 1004.9, "ask": 1005.0, "bids": [(1004.9, 500)], "asks": [(1005.0, 500)], "at": stale}
    w.quotes[("fut", "ABC")] = {"bid": 1008.8, "ask": 1008.9, "bids": [(1008.8, 500)], "asks": [(1008.9, 500)], "at": stale}

    async def loop_pass():
        w.manage_position("11:30")  # (1008.9 - 1004.9) / ~1005 = 0.398% <= 0.40%: reverted
        await settle(w)

    asyncio.run(loop_pass())
    (close,) = [e for e in w.events if e["event"] == "close"]
    assert close["reason"] == "reverted" and w.position is None


def test_no_reversion_exit_while_the_spread_is_still_wide(tmp_path, monkeypatch) -> None:
    _open_log(tmp_path)
    w = mk(tmp_path, monkeypatch)
    w.quotes[("cash", "ABC")] = {"bid": 1004.9, "ask": 1005.0, "bids": [(1004.9, 500)], "asks": [(1005.0, 500)], "at": 0.0}
    w.quotes[("fut", "ABC")] = {"bid": 1012.0, "ask": 1012.1, "bids": [(1012.0, 500)], "asks": [(1012.1, 500)], "at": 0.0}
    w.manage_position("11:30")
    assert not w._tasks and w.position is not None
