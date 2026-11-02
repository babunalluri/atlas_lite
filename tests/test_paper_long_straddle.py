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
    assert snap["book"] == "long_straddle" and pos["debit"] == 241.0 and pos["value_now"] == 300.0
    assert pos["open_pnl"] < (300.0 - 241.0) * 65


def test_ledger_lines_are_json_with_the_strategy(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("09:16"), quotes(120, 120))
    frame(b, t("13:00"), quotes(130, 130))
    rows = [json.loads(x) for x in (tmp_path / LEDGER_NAME).read_text().splitlines()]
    assert [r["event"] for r in rows] == ["open", "close"] and {r["strategy"] for r in rows} == {"long_straddle"}
