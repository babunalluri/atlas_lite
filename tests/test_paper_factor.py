"""factor_paper: trend entries on closed minutes, VIX filter, exits, and book rules."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from atlas_lite.feed_engine import FeedEngine
from atlas_lite.kite_rest import KiteRest
from atlas_lite.paper_factor import LEDGER_NAME, PaperFactor, paper_factor_enabled

DAY = "2026-10-28"  # Wednesday
CE, PE = "NFO:NIFTY26N0322600CE", "NFO:NIFTY26N0322600PE"
OPEN = 22600.0


class Book:
    def __init__(self, **px: float) -> None:
        self.px = dict(px)

    def get(self, symbol: str):
        value = self.px.get(symbol)
        return None if value is None else {"last_price": value}


def _t(hm: str, sec: int = 0, day: str = DAY) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:{sec:02d}+05:30")


def _book(tmp_path: Path, **kw) -> PaperFactor:
    return PaperFactor(path=tmp_path / LEDGER_NAME, lot_size=65, **kw)


def _frame(pf: PaperFactor, now: datetime, book: Book, spot: float, **kw):
    args = dict(
        now=now,
        book=book,
        spot=spot,
        spot_open=OPEN,
        vix=kw.pop("vix", 15.0),
        vix_open=kw.pop("vix_open", 15.0),
        fut_oi=kw.pop("fut_oi", 1_000_000.0),
        ce_symbol=CE,
        pe_symbol=PE,
        atm=22600,
    )
    args.update(kw)
    return pf.on_frame(**args)


def _enter(pf: PaperFactor, book: Book, hm: str, spot: float, **kw):
    """Spot sits at ``spot`` during minute ``hm``; the decision fires on the next minute."""
    _frame(pf, _t(hm, 30), book, spot, **kw)
    nxt = (_t(hm) + timedelta(minutes=1)).strftime("%H:%M")
    return _frame(pf, _t(nxt, 1), book, spot, **kw)


def _ledger(tmp_path: Path) -> list[dict]:
    path = tmp_path / LEDGER_NAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ATLAS_LITE_PAPER_FACTOR", raising=False)
    assert paper_factor_enabled() is False
    monkeypatch.setenv("ATLAS_LITE_PAPER_FACTOR", "1")
    assert paper_factor_enabled() is True


def test_spot_above_open_buys_ce_on_next_minute(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 120.0, PE: 100.0})
    assert _frame(pf, _t("10:00", 30), book, OPEN + 60) is None  # minute still open
    ev = _frame(pf, _t("10:01", 1), book, OPEN + 60)
    assert ev is not None and ev["event"] == "open"
    assert ev["side"] == "ce" and ev["symbol"] == CE
    assert ev["entry"] == 120.0 and ev["target"] == 180.0 and ev["stop"] == 90.0
    assert ev["bar"] == "10:00" and ev["move"] == 60.0
    assert ev["hold_until"].startswith(f"{DAY}T11:30:59")
    assert ev["strategy"] == "factor_paper" and ev["live_orders"] is False


def test_spot_below_open_buys_pe(tmp_path) -> None:
    pf = _book(tmp_path)
    ev = _enter(pf, Book(**{CE: 100.0, PE: 140.0}), "11:00", OPEN - 75)
    assert ev is not None and ev["side"] == "pe" and ev["entry"] == 140.0


def test_small_move_does_nothing(tmp_path) -> None:
    pf = _book(tmp_path)
    assert _enter(pf, Book(**{CE: 120.0, PE: 100.0}), "10:00", OPEN + 49) is None
    assert pf.position is None and _ledger(tmp_path) == []


def test_minute_close_not_intraminute_spike(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 120.0, PE: 100.0})
    _frame(pf, _t("10:00", 10), book, OPEN + 80)  # spike inside the minute
    _frame(pf, _t("10:00", 50), book, OPEN + 20)  # closes back near the open
    assert _frame(pf, _t("10:01", 1), book, OPEN + 20) is None


def test_entry_window(tmp_path) -> None:
    book = Book(**{CE: 120.0, PE: 100.0})
    assert _enter(_book(tmp_path / "a"), book, "09:29", OPEN + 60) is None
    assert _enter(_book(tmp_path / "b"), book, "09:30", OPEN + 60) is not None
    assert _enter(_book(tmp_path / "c"), book, "14:44", OPEN + 60) is not None
    assert _enter(_book(tmp_path / "d"), book, "14:45", OPEN + 60) is None


def test_vix_rising_skips(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 120.0, PE: 100.0})
    assert _enter(pf, book, "10:00", OPEN + 60, vix=15.8, vix_open=15.0) is None
    assert pf.last_reject == "vix_rising"
    assert _enter(pf, book, "10:05", OPEN + 60, vix=15.7, vix_open=15.0) is not None


def test_vix_base_falls_back_to_first_print(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 120.0, PE: 100.0})
    _frame(pf, _t("09:16"), book, OPEN, vix=15.0, vix_open=None)
    assert _enter(pf, book, "10:00", OPEN + 60, vix=16.0, vix_open=None) is None
    assert pf.last_reject == "vix_rising"


def test_oi_change_logged_not_gated(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 120.0, PE: 100.0})
    _frame(pf, _t("09:16"), book, OPEN, fut_oi=1_000_000.0)
    ev = _enter(pf, book, "10:00", OPEN + 60, fut_oi=980_000.0)
    assert ev is not None and ev["oi_chg_open"] == -0.02


def test_target_stop_and_time_exits(tmp_path) -> None:
    book = Book(**{CE: 100.0, PE: 100.0})
    pf = _book(tmp_path / "t")
    _enter(pf, book, "10:00", OPEN + 60)
    book.px[CE] = 150.0
    ev = _frame(pf, _t("10:05"), book, OPEN + 60)
    assert ev["event"] == "close" and ev["reason"] == "target" and ev["exit"] == 150.0
    assert ev["pnl"] < 50 * 65 and ev["pnl"] > 0

    book.px[CE] = 100.0
    pf = _book(tmp_path / "s")
    _enter(pf, book, "10:00", OPEN + 60)
    book.px[CE] = 75.0
    assert _frame(pf, _t("10:05"), book, OPEN + 60)["reason"] == "stop"

    book.px[CE] = 100.0
    pf = _book(tmp_path / "h")
    _enter(pf, book, "10:00", OPEN + 60)
    book.px[CE] = 110.0
    assert _frame(pf, _t("11:30", 58), book, OPEN + 60) is None
    assert _frame(pf, _t("11:30", 59), book, OPEN + 60)["reason"] == "time"


def test_square_off_at_1514(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 100.0, PE: 100.0})
    _enter(pf, book, "14:30", OPEN + 60)
    assert _frame(pf, _t("15:13", 59), book, OPEN + 60) is None
    assert _frame(pf, _t("15:14"), book, OPEN + 60)["reason"] == "square_off"


def test_stop_blocks_side_15m_and_small_stop_does_not_lock(tmp_path) -> None:
    pf = _book(tmp_path, stop_pct=0.02)  # small stop -> loss above -400
    book = Book(**{CE: 100.0, PE: 100.0})
    _enter(pf, book, "10:00", OPEN + 60)
    book.px[CE] = 98.0
    close = _frame(pf, _t("10:05"), book, OPEN + 60)
    assert close["reason"] == "stop" and close["pnl"] > -400
    book.px[CE] = 100.0
    assert _enter(pf, book, "10:10", OPEN + 60) is None
    assert pf.last_reject == "ce_blocked"
    assert _enter(pf, book, "10:20", OPEN + 60) is not None


def test_big_stop_locks_side_for_the_day(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 100.0, PE: 100.0})
    _enter(pf, book, "10:00", OPEN + 60)
    book.px[CE] = 74.0
    assert _frame(pf, _t("10:05"), book, OPEN + 60)["pnl"] <= -400
    book.px[CE] = 100.0
    assert _enter(pf, book, "11:00", OPEN + 60) is None
    assert pf.last_reject == "ce_locked"
    assert _enter(pf, book, "11:10", OPEN - 60) is not None  # PE still allowed


def test_exit_gap_after_win(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 100.0, PE: 100.0})
    _enter(pf, book, "10:00", OPEN + 60)
    book.px[CE] = 160.0
    _frame(pf, _t("10:05"), book, OPEN + 60)
    assert _enter(pf, book, "10:06", OPEN - 60) is None
    assert pf.last_reject == "exit_gap"
    assert _enter(pf, book, "10:10", OPEN - 60) is not None


def test_max_three_entries_a_day(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 100.0, PE: 100.0})
    for i, hm in enumerate(("10:00", "10:30", "11:00")):
        assert _enter(pf, book, hm, OPEN + 60) is not None
        book.px[CE] = 160.0
        _frame(pf, _t(hm[:3] + "15"), book, OPEN + 60)
        book.px[CE] = 100.0
    assert _enter(pf, book, "12:00", OPEN + 60) is None
    assert pf.last_reject == "max_trades_day"


def test_daily_loss_stop(tmp_path) -> None:
    pf = _book(tmp_path)
    pf.day_pnl = -2000.0
    pf.traded_day = DAY
    assert _enter(pf, Book(**{CE: 100.0, PE: 100.0}), "10:00", OPEN + 60) is None
    assert pf.last_reject == "daily_loss_stop"


def test_new_day_resets_counters(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 100.0, PE: 100.0})
    _enter(pf, book, "10:00", OPEN + 60)
    book.px[CE] = 74.0
    _frame(pf, _t("10:05"), book, OPEN + 60)
    assert "ce" in pf.side_locked and pf.day_pnl < 0
    book.px[CE] = 100.0
    nxt = "2026-10-29"
    _frame(pf, _t("10:00", 30, nxt), book, OPEN + 60)
    ev = _frame(pf, _t("10:01", 1, nxt), book, OPEN + 60)
    assert ev is not None and ev["side"] == "ce" and pf.entries_today == 1


def test_restore_open_position_and_day_state(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 100.0, PE: 100.0})
    _enter(pf, book, "10:00", OPEN + 60)
    book.px[CE] = 74.0
    _frame(pf, _t("10:05"), book, OPEN + 60)
    book.px[PE] = 100.0
    _enter(pf, book, "10:30", OPEN - 60)

    again = PaperFactor(path=tmp_path / LEDGER_NAME, lot_size=65)
    # The open position pins the restored day, whatever today's wall clock says.
    assert again.position is not None and again.position.side == "pe"
    assert again.entries_today == 2
    assert "ce" in again.side_locked
    assert again.day_pnl == pf.day_pnl
    book.px[PE] = 150.0
    assert _frame(again, _t("10:40"), book, OPEN - 60)["reason"] == "target"


def test_weekend_and_session_gap_close(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 100.0, PE: 100.0})
    _enter(pf, book, "14:40", OPEN + 60)
    ev = _frame(pf, _t("09:20", day="2026-10-29"), book, OPEN + 60)
    assert ev["reason"] == "session_gap"


def test_trades_api_lists_factor_book(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 100.0, PE: 100.0})
    _enter(pf, book, "10:00", OPEN + 60)
    book.px[CE] = 150.0
    _frame(pf, _t("10:05"), book, OPEN + 60)
    eng = FeedEngine(rest=MagicMock(spec=KiteRest), data_dir=tmp_path)
    out = eng.list_paper_trades(limit=10)
    assert "factor_paper" in out["books"]
    assert out["closed_count"] == 1 and out["pnl_total"] is not None and out["pnl_total"] > 0
    assert "50 pt" in FeedEngine._paper_book_legend("factor_paper")


def test_time_exit_uses_last_seen_price_when_quote_drops(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 100.0, PE: 100.0})
    _enter(pf, book, "10:00", OPEN + 60)
    book.px[CE] = 112.0
    assert _frame(pf, _t("10:30"), book, OPEN + 60) is None
    del book.px[CE]  # strike left the subscribed chain
    assert _frame(pf, _t("11:00"), book, OPEN + 60) is None
    ev = _frame(pf, _t("11:30", 59), book, OPEN + 60)
    assert ev["reason"] == "time" and ev["exit"] == 112.0 and ev["pnl"] is not None
    assert pf.day_pnl == ev["pnl"]


def test_no_signals_on_nse_holiday(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 120.0, PE: 100.0})
    holiday = "2026-11-24"  # Tuesday, Guru Nanak Jayanti
    _frame(pf, _t("10:00", 30, holiday), book, OPEN + 60)
    assert _frame(pf, _t("10:01", 1, holiday), book, OPEN + 60) is None
    assert _ledger(tmp_path) == []


def test_session_gap_closes_at_last_seen_price_not_next_day_quote(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 100.0, PE: 100.0})
    _enter(pf, book, "14:40", OPEN + 60)
    book.px[CE] = 110.0
    _frame(pf, _t("14:50"), book, OPEN + 60)
    book.px[CE] = 400.0  # next morning after a gap
    ev = _frame(pf, _t("09:20", day="2026-10-29"), book, OPEN + 60)
    assert ev["reason"] == "session_gap" and ev["exit_last_seen"] == 110.0
    assert ev["pnl"] is None and ev["pnl_known"] is False


def test_late_restart_logs_no_oi_change_and_uses_vix_open(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 120.0, PE: 100.0})
    _frame(pf, _t("12:00"), book, OPEN, vix=16.0, vix_open=None, fut_oi=900_000.0)
    ev = _enter(pf, book, "12:10", OPEN + 60, vix=16.5, vix_open=None, fut_oi=880_000.0)
    assert ev is not None and ev["oi_chg_open"] is None and ev["vix_rise"] is None
    pf2 = _book(tmp_path / "b")
    _frame(pf2, _t("12:00"), book, OPEN, vix=16.0, vix_open=15.0)
    assert _enter(pf2, book, "12:10", OPEN + 60, vix=16.0, vix_open=15.0) is None
    assert pf2.last_reject == "vix_rising"


def test_daily_loss_stop_zero_disables(tmp_path) -> None:
    pf = _book(tmp_path, daily_loss_stop=0.0)
    assert _enter(pf, Book(**{CE: 100.0, PE: 100.0}), "10:00", OPEN + 60) is not None


def test_new_day_clears_last_reject(tmp_path) -> None:
    pf = _book(tmp_path)
    pf.traded_day = DAY
    pf.last_reject = "max_trades_day"
    _frame(pf, _t("09:20", day="2026-10-29"), Book(), OPEN)
    assert pf.last_reject is None


def test_unparsable_hold_falls_back_to_opened_at(tmp_path) -> None:
    pf = _book(tmp_path)
    book = Book(**{CE: 100.0, PE: 100.0})
    _enter(pf, book, "10:00", OPEN + 60)
    pf.position.hold_until = ""
    assert _frame(pf, _t("10:30"), book, OPEN + 60) is None
