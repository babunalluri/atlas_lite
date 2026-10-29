"""Scalper v3: daily 13:00 ATM straddle sale, TP 30% / SL 30% of credit, flat 15:14."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

from atlas_lite.paper_scalper_v3 import LEDGER_NAME, PaperScalperV3

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


def frame(b: PaperScalperV3, now: datetime, rows: dict, expiry: date = EXPIRY, allow: bool = True):
    return b.on_frame(now=now, book=Book(rows), expiry=expiry, atm=22950, ce_symbol=CE, pe_symbol=PE, allow_entry=allow)


def mk(tmp_path: Path) -> PaperScalperV3:
    return PaperScalperV3(path=tmp_path / LEDGER_NAME)


def test_opens_at_1300_once_a_day(tmp_path) -> None:
    b = mk(tmp_path)
    assert frame(b, t("12:59"), quotes(100, 100)) is None
    ev = frame(b, t("13:00"), quotes(100, 100))
    assert ev["event"] == "open" and ev["credit_ltp"] == 200 and ev["credit_fill"] == 199.0
    assert ev["target"] == 140.0 and ev["stop"] == 260.0
    b.position = None
    assert frame(b, t("13:01"), quotes(100, 100)) is None  # one trade a day


def test_late_first_frame_and_after_window(tmp_path) -> None:
    assert frame(mk(tmp_path / "a"), t("13:05"), quotes(100, 100)) is not None
    assert frame(mk(tmp_path / "b"), t("13:06"), quotes(100, 100)) is None


def test_skips_expiry_day_and_holidays(tmp_path) -> None:
    b = mk(tmp_path / "e")
    assert frame(b, t("13:00", "2026-11-03"), quotes(100, 100), expiry=date(2026, 11, 3)) is None
    assert b.last_reject == "expiry_day"
    assert frame(mk(tmp_path / "h"), t("13:00", "2026-11-24"), quotes(100, 100), expiry=date(2026, 11, 26)) is None


def test_gate_blocks_entry(tmp_path) -> None:
    b = mk(tmp_path)
    assert frame(b, t("13:00"), quotes(100, 100), allow=False) is None and b.last_reject == "gate_paused"


def test_take_profit_at_30_percent_of_credit(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    assert frame(b, t("13:30"), quotes(75, 66)) is None  # cost 141 > 140
    ev = frame(b, t("13:31"), quotes(75, 65))  # cost 140 = 70% of credit
    assert ev["reason"] == "target" and ev["pnl"] > 0 and ev["pnl_known"] is True


def test_stop_at_30_percent_loss(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    ev = frame(b, t("13:40"), quotes(220, 40))  # cost 260 = 130% of credit
    assert ev["reason"] == "stop" and ev["pnl"] < 0


def test_square_off_at_1514(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    assert frame(b, t("15:13"), quotes(95, 95)) is None
    ev = frame(b, t("15:14"), quotes(95, 95))
    assert ev["reason"] == "square_off" and ev["exit"] == 191.0  # bought back at ask


def test_restart_same_day_keeps_position(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    again = mk(tmp_path)
    assert again.position is not None and again.position.credit_ltp == 200 and again.traded_day == DAY
    assert frame(again, t("14:00"), quotes(70, 70))["reason"] == "target"


def test_missed_square_off_books_unknown_pnl(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    again = mk(tmp_path)
    ev = frame(again, t("09:20", "2026-10-30"), quotes(90, 90))
    assert ev["reason"] == "session_gap" and ev["pnl"] is None and ev["pnl_known"] is False


def test_trades_api_lists_v3(tmp_path) -> None:
    from atlas_lite.feed_engine import FeedEngine
    from atlas_lite.kite_rest import KiteRest

    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    frame(b, t("13:31"), quotes(70, 65))
    out = FeedEngine(rest=MagicMock(spec=KiteRest), data_dir=tmp_path).list_paper_trades(limit=10)
    assert "scalper_v3" in out["books"] and out["closed_count"] == 1 and out["pnl_total"] > 0
    assert "13:00" in FeedEngine._paper_book_legend("scalper_v3")


def test_ledger_rows_are_tagged(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    rows = [json.loads(x) for x in b.path.read_text().splitlines()]
    assert rows[0]["strategy"] == "scalper_v3" and rows[0]["live_orders"] is False


def test_quote_blink_at_close_uses_last_price_plus_slippage(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    frame(b, t("15:13"), quotes(95, 95))
    ev = frame(b, t("15:14"), {CE: {"last_price": 95, "bid": 94.5, "ask": 95.5}})  # PE quote missing this frame
    assert ev["reason"] == "square_off" and ev["pnl_known"] is True and ev["exit"] == 95.5 + 96.0


def test_stop_fires_on_last_price_when_a_quote_blinks(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    frame(b, t("13:30"), quotes(110, 90))
    ev = frame(b, t("13:31"), {CE: {"last_price": 175, "bid": 174.5, "ask": 175.5}})  # PE missing; 175 + 90 >= 260
    assert ev is not None and ev["reason"] == "stop"


def test_late_close_after_downtime_is_unknown(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    ev = frame(b, t("16:00"), quotes(80, 80))  # engine was down at 15:14; post-close quotes
    assert ev["reason"] == "square_off_late" and ev["pnl"] is None and ev["pnl_known"] is False


def test_snapshot_uses_last_price_when_a_quote_blinks(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    snap = b.snapshot(Book({CE: {"last_price": 100}}))
    assert snap["position"]["cost_now"] == 200


def test_wrong_expiry_reading_does_not_lock_the_day(tmp_path) -> None:
    b = mk(tmp_path)
    assert frame(b, t("13:00"), quotes(100, 100), expiry=date(2026, 10, 29)) is None  # stale universe says today
    assert b.last_reject == "expiry_day"
    ev = frame(b, t("13:01"), quotes(100, 100), expiry=EXPIRY)  # corrected on the next frame
    assert ev is not None and ev["event"] == "open"


def test_open_pnl_is_net_of_charges(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t("13:00"), quotes(100, 100))
    snap = b.snapshot(Book(quotes(100, 100)))
    gross = (b.position.credit_fill - 200) * b.position.qty
    assert snap["position"]["open_pnl"] < gross - b.position.charges_open
