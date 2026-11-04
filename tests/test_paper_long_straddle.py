"""Long straddle: daily 09:16 ATM straddle purchase, sold at 13:00, expiry days included."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from atlas_lite.paper_long_straddle import LEDGER_NAME, PaperLongStraddle

DAY = "2026-10-29"  # Thursday
EXPIRY = date(2026, 11, 3)
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


def frame(b: PaperLongStraddle, now: datetime, rows: dict, expiry: date = EXPIRY, allow: bool = True):
    return b.on_frame(now=now, book=Book(rows), expiry=expiry, atm=22950, ce_symbol=CE, pe_symbol=PE, allow_entry=allow)


def mk(tmp_path: Path) -> PaperLongStraddle:
    return PaperLongStraddle(path=tmp_path / LEDGER_NAME)


def test_buys_at_0916_at_the_ask_once_a_day(tmp_path) -> None:
    b = mk(tmp_path)
    assert frame(b, t("09:15"), quotes(120, 120)) is None
    ev = frame(b, t("09:16"), quotes(120, 120))
    assert ev["event"] == "open" and ev["side"] == "long" and ev["debit_fill"] == 241.0  # ask: 120.5 + 120.5
    assert ev["strategy"] == "long_straddle" and ev["live_orders"] is False and ev["expiry_day"] is False
    b.position = None
    assert frame(b, t("09:17"), quotes(120, 120)) is None  # one trade a day


def test_late_first_frame_and_after_window(tmp_path) -> None:
    assert frame(mk(tmp_path / "a"), t("09:21"), quotes(120, 120)) is not None
    assert frame(mk(tmp_path / "b"), t("09:22"), quotes(120, 120)) is None


def test_trades_expiry_days_and_marks_them(tmp_path) -> None:
    ev = frame(mk(tmp_path), t("09:16", "2026-11-03"), quotes(80, 80), expiry=date(2026, 11, 3))
    assert ev is not None and ev["expiry_day"] is True


def test_skips_holidays_and_gate(tmp_path) -> None:
    assert frame(mk(tmp_path / "h"), t("09:16", "2026-11-24"), quotes(120, 120)) is None  # NSE holiday
    b = mk(tmp_path / "g")
    assert frame(b, t("09:16"), quotes(120, 120), allow=False) is None and b.last_reject == "gate_paused"


def test_holds_through_the_morning_and_sells_at_1300_at_the_bid(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))
    assert frame(b, t("11:00"), quotes(40, 300)) is None  # no target or stop: held to 13:00
    ev = frame(b, t("13:00"), quotes(60, 260))
    assert ev["event"] == "close" and ev["reason"] == "time" and ev["pnl_known"] is True
    assert ev["exit"] == 319.0  # bid: 59.5 + 259.5
    assert ev["pnl_gross"] == round((319.0 - 241.0) * 65, 2) and ev["pnl"] == round(ev["pnl_gross"] - ev["charges"], 2)
    assert b.position is None


def test_a_losing_day_is_recorded_as_a_loss(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))
    ev = frame(b, t("13:00"), quotes(90, 95))
    assert ev["pnl"] < 0 and ev["pnl_known"] is True


def test_exit_first_seen_late_is_priced_but_marked(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))
    ev = frame(b, t("13:30"), quotes(150, 150))  # engine was down at 13:00
    assert ev["reason"] == "time_late" and ev["pnl_known"] is True


def test_after_the_close_or_next_day_exit_is_unknown(tmp_path) -> None:
    b = mk(tmp_path / "a")
    frame(b, t("09:16"), quotes(120, 120))
    ev = frame(b, t("15:25"), quotes(150, 150))
    assert ev["reason"] == "square_off_late" and ev["pnl"] is None and ev["pnl_known"] is False
    b = mk(tmp_path / "b")
    frame(b, t("09:16"), quotes(120, 120))
    ev = frame(b, t("09:16", "2026-10-30"), quotes(150, 150))
    assert ev["reason"] == "session_gap" and ev["pnl"] is None


def test_a_blinked_quote_at_exit_uses_the_last_price_less_slippage(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))
    frame(b, t("12:59"), quotes(130, 140))
    ev = frame(b, t("13:00"), {PE: quotes(130, 140)[PE]})  # call quote missing this frame
    assert ev["pnl_known"] is True and ev["exit"] == round((130 - 1.0) + 139.5, 2)


def test_restart_restores_the_open_position_and_the_day(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))
    again = mk(tmp_path)
    assert again.position is not None and again.position.debit_fill == 241.0 and again.traded_day == DAY
    ev = frame(again, t("13:00"), quotes(130, 130))
    assert ev["reason"] == "time" and ev["pnl_known"] is True
    assert frame(mk(tmp_path), t("09:16"), quotes(120, 120)) is None  # already traded today


def test_no_quote_no_entry(tmp_path) -> None:
    b = mk(tmp_path)
    assert frame(b, t("09:16"), {}) is None and b.last_reject == "no_atm_quote"
    assert frame(b, t("09:17"), quotes(120, 120)) is not None  # not locked: retries within the window


def test_snapshot_open_pnl_is_net_of_charges(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))
    snap = b.snapshot(Book(quotes(150, 150)))
    pos = snap["position"]
    assert snap["book"] == "long_straddle" and pos["debit"] == 241.0 and pos["value_now"] == 299.0  # at the bids
    assert pos["open_pnl"] < (299.0 - 241.0) * 65


def test_ledger_lines_are_json_with_the_strategy(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))
    frame(b, t("13:00"), quotes(130, 130))
    rows = [json.loads(x) for x in (tmp_path / LEDGER_NAME).read_text().splitlines()]
    assert [r["event"] for r in rows] == ["open", "close"] and {r["strategy"] for r in rows} == {"long_straddle"}


def mk_tp(tmp_path: Path) -> PaperLongStraddle:
    from atlas_lite.paper_long_straddle import TP20_LEDGER_NAME, TP20_STRATEGY, TP20_TAKE_PROFIT

    return PaperLongStraddle(path=tmp_path / TP20_LEDGER_NAME, strategy=TP20_STRATEGY,
                             name="Long straddle +20% TP", take_profit=TP20_TAKE_PROFIT)


def test_tp20_sells_at_exactly_plus_20_percent_once_the_bids_reach_it(tmp_path) -> None:
    b = mk_tp(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))  # paid 241.0 at the ask; target 289.2
    assert frame(b, t("10:00"), quotes(150, 139)) is None  # bids 149.5 + 138.5 = 288 < 289.2
    ev = frame(b, t("10:05"), quotes(160, 140))  # bids 159.5 + 139.5 = 299 >= 289.2
    assert ev["reason"] == "target" and ev["pnl_known"] is True and ev["strategy"] == "long_straddle_tp20"
    assert ev["exit"] == 289.2  # a resting limit fills at its own price, not the better bid
    assert ev["pnl_gross"] == round((289.2 - 241.0) * 65, 2)


def test_tp20_without_the_target_sells_at_1300_like_the_main_book(tmp_path) -> None:
    b = mk_tp(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))
    assert frame(b, t("12:00"), quotes(125, 125)) is None
    ev = frame(b, t("13:00"), quotes(126, 126))
    assert ev["reason"] == "time" and ev["exit"] == 251.0


def test_tp20_needs_both_bids(tmp_path) -> None:
    b = mk_tp(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))
    rows = quotes(400, 140)
    rows[PE] = {"last_price": 140}  # no bid on the put: the resting order can't be judged filled
    assert frame(b, t("10:00"), rows) is None and b.position is not None


def test_the_two_books_keep_separate_ledgers_and_state(tmp_path) -> None:
    main, tp = mk(tmp_path), mk_tp(tmp_path)
    frame(main, t("09:16"), quotes(120, 120))
    frame(tp, t("09:16"), quotes(120, 120))
    frame(tp, t("10:00"), quotes(160, 140))  # tp closes on target, main stays open
    again_main, again_tp = mk(tmp_path), mk_tp(tmp_path)
    assert again_main.position is not None and again_tp.position is None
    assert again_tp.traded_day == DAY and again_tp.snapshot()["book"] == "long_straddle_tp20"
    assert again_main.snapshot()["take_profit"] is None and again_tp.snapshot()["take_profit"] == 0.20


def test_snapshot_values_the_position_at_the_bids(tmp_path) -> None:
    b = mk_tp(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))  # paid 241.0; target 289.2
    snap = b.snapshot(Book(quotes(145, 145, spread=1.0)))  # last 290 (past target), bids 288 (not yet)
    assert snap["position"]["value_now"] == 288.0 and snap["position"]["target"] == 289.2
    assert frame(b, t("10:00"), quotes(145, 145, spread=1.0)) is None  # and the take-profit has not fired


# ---- balanced strike, stop, monthly limit ----
CE50, PE50 = "NFO:NIFTY26N0323000CE", "NFO:NIFTY26N0323000PE"


def test_balanced_strike_picks_the_strike_where_ce_and_pe_are_closest(tmp_path) -> None:
    b = mk(tmp_path)
    rows = {**quotes(150, 100), CE50: {"last_price": 124, "bid": 123.5, "ask": 124.5},
            PE50: {"last_price": 126, "bid": 125.5, "ask": 126.5}}
    ev = frame(b, t("09:16"), rows)
    assert ev["atm"] == 23000 and ev["ce_symbol"] == CE50 and ev["debit_fill"] == 251.0


def test_balanced_strike_falls_back_to_atm_without_neighbour_quotes(tmp_path) -> None:
    ev = frame(mk(tmp_path), t("09:16"), quotes(150, 100))
    assert ev["atm"] == 22950 and ev["ce_symbol"] == CE


def test_emergency_stop_at_30_percent_below_the_price_paid(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))  # paid 241; stop at 168.7 on the bids
    assert frame(b, t("10:00"), quotes(85, 85)) is None  # bids 169
    ev = frame(b, t("10:01"), quotes(84.5, 84.5))  # bids 168
    assert ev["reason"] == "stop" and ev["pnl"] < 0 and ev["pnl_known"] is True


def test_monthly_loss_limit_blocks_new_entries(tmp_path) -> None:
    b = mk(tmp_path)
    b.month_pnl["2026-10"] = -15_000.0
    assert frame(b, t("09:16"), quotes(120, 120)) is None and b.last_reject == "monthly_loss_limit"
    assert frame(mk(tmp_path / "nov"), t("09:16", "2026-11-04"), quotes(120, 120)) is not None  # new month


def test_monthly_pnl_is_rebuilt_from_the_ledger(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))
    frame(b, t("13:00"), quotes(90, 95))
    assert mk(tmp_path).month_pnl["2026-10"] == b.month_pnl["2026-10"] < 0


def test_day_live_flag_counts_only_for_today(tmp_path, monkeypatch) -> None:
    import json as _j
    from atlas_lite.day_live_flag import DayLiveFlag

    f = DayLiveFlag(tmp_path / "straddle_live.json", "ATLAS_LITE_STRADDLE_LIVE_ALLOWED")
    monkeypatch.delenv("ATLAS_LITE_STRADDLE_LIVE_ALLOWED", raising=False)
    import pytest as _pt
    with _pt.raises(ValueError):
        f.set_enabled(True)
    monkeypatch.setenv("ATLAS_LITE_STRADDLE_LIVE_ALLOWED", "1")
    assert f.set_enabled(True)["enabled"] is True
    (tmp_path / "straddle_live.json").write_text(_j.dumps({"enabled": True, "day": "2026-01-01"}))
    assert f.is_enabled() is False and f.snapshot()["stale"] is True
    (tmp_path / "straddle_live.json").write_text(_j.dumps({"enabled": True}))  # no day at all
    assert f.is_enabled() is False
    assert f.turn_off_if_on(source="day_end") is True and f.is_enabled() is False


def test_straddle_live_api_reads_and_refuses_when_not_allowed(tmp_path, monkeypatch) -> None:
    import asyncio
    import atlas_lite.main as main

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.delenv("ATLAS_LITE_STRADDLE_LIVE_ALLOWED", raising=False)
    got = asyncio.run(main.api_straddle_live_get())
    assert got["live"]["allowed"] is False and got["live"]["enabled"] is False

    class Req:  # disabling never needs a login
        headers, cookies = {}, {}

    off = asyncio.run(main.api_straddle_live_set(Req(), enabled=False))
    assert off["live"]["enabled"] is False


def test_open_logs_the_entry_conditions(tmp_path) -> None:
    rows = quotes(130, 110)
    rows["NSE:NIFTY 50"] = {"last_price": 23000.0, "ohlc": {"close": 22900.0}}
    rows["NSE:INDIA VIX"] = {"last_price": 12.5, "ohlc": {"close": 12.0}}
    ctx = frame(mk(tmp_path), t("09:16"), rows)["context"]
    assert ctx["ce_pe_imbalance"] == round(20 / 240, 4)
    assert ctx["straddle_pct_spot"] == round(240 / 23000 * 100, 3) and ctx["spot"] == 23000.0
    assert ctx["gap_pct"] == round(100 / 22900 * 100, 3)
    assert ctx["vix"] == 12.5 and ctx["vix_chg"] == 0.5


def test_open_without_spot_or_vix_still_trades(tmp_path) -> None:
    ev = frame(mk(tmp_path), t("09:16"), quotes(120, 120))
    assert ev["event"] == "open" and ev["context"] == {"ce_pe_imbalance": 0.0}
