"""Paper long iron condor — 09:15 200/200, 2-min validate, red first."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite.paper_long_iron_condor import (
    PaperLongIronCondor,
    condor_debit,
    fit_long_iron_condor,
    in_long_ic_entry_window,
    long_ic_pop,
    long_iron_condor_strikes,
    vertical_mtm_pts,
)

IST = ZoneInfo("Asia/Kolkata")

SYMS = {
    "pe_short": "NFO:PES",
    "pe_long": "NFO:PEL",
    "ce_long": "NFO:CEL",
    "ce_short": "NFO:CES",
}


class _Book:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def get(self, symbol: str):
        return self.rows.get(symbol)


def _now(hm: str, day: str = "2026-09-25") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def _bot(path: Path, **kwargs) -> PaperLongIronCondor:
    kwargs.setdefault("long_otm", 200)
    kwargs.setdefault("wing_pts", 200)
    return PaperLongIronCondor(path=path / "lic.jsonl", lot_size=65, lots=1, **kwargs)


def _book(ce_l: float, ce_s: float, pe_l: float, pe_s: float) -> _Book:
    return _Book(
        {
            "NFO:CEL": {"last_price": ce_l},
            "NFO:CES": {"last_price": ce_s},
            "NFO:PEL": {"last_price": pe_l},
            "NFO:PES": {"last_price": pe_s},
        }
    )


def _frame(
    bot: PaperLongIronCondor,
    hm: str,
    ce_l: float = 61.8,
    ce_s: float = 21.55,
    pe_l: float = 45.95,
    pe_s: float = 20.90,
    *,
    day: str = "2026-09-25",
    atm: int = 23050,
) -> dict | None:
    return bot.on_frame(
        now=_now(hm, day=day),
        feed={"index_nifty_chg": 0.9},
        book=_book(ce_l, ce_s, pe_l, pe_s),
        atm=atm,
        symbols=SYMS,
    )


def test_strikes_match_sensibull() -> None:
    assert long_iron_condor_strikes(23050) == (22800, 23050, 23050, 23300)
    assert long_iron_condor_strikes(23050, long_otm=200, wing=200) == (
        22650,
        22850,
        23250,
        23450,
    )
    assert condor_debit(152.55, 48.15, 106.50, 37.25) == 173.65
    assert vertical_mtm_pts(80.0, 30.0, 61.8, 21.55) == 9.75


def test_pop_matches_sensibull_ballpark() -> None:
    pop = long_ic_pop(0, 173.65, 241)
    assert 0.45 <= pop <= 0.52


def _opt(strike: int, side: str) -> str:
    return f"NFO:{int(strike)}{side.upper()}"


def _decay_book(atm: int = 23050) -> _Book:
    rows: dict = {}
    for otm in range(0, 451, 50):
        ce = max(4.0, round(155.0 * (2.718281828 ** (-otm / 280.0)), 2))
        pe = max(4.0, round(110.0 * (2.718281828 ** (-otm / 280.0)), 2))
        rows[_opt(atm + otm, "CE")] = {"last_price": ce}
        rows[_opt(atm - otm, "PE")] = {"last_price": pe}
    # Pin the screenshot butterfly (ATM longs / 250 hedge).
    rows[_opt(23050, "CE")] = {"last_price": 152.55}
    rows[_opt(23050, "PE")] = {"last_price": 106.50}
    rows[_opt(23300, "CE")] = {"last_price": 48.15}
    rows[_opt(22800, "PE")] = {"last_price": 37.25}
    return _Book(rows)


def test_fit_midweek_picks_butterfly_hedge() -> None:
    now = _now("09:15")
    fit = fit_long_iron_condor(
        23050,
        book=_decay_book(),
        option_symbol=_opt,
        now=now,
        spot=23063.0,
        iv_pct=12.0,
        expiry=date(2026, 9, 29),
    )
    assert fit is not None
    assert fit.long_otm == 0
    assert fit.wing_pts in (200, 250, 300)
    assert fit.pop >= 0.45
    assert fit.pop <= 0.68
    assert fit.ce_long_strike == 23050
    assert fit.pe_long_strike == 23050


def test_fit_expiry_morning_skips_dead_pop() -> None:
    now = datetime.fromisoformat("2026-09-29T09:15:00+05:30")
    fit = fit_long_iron_condor(
        23050,
        book=_decay_book(),
        option_symbol=_opt,
        now=now,
        spot=23063.0,
        iv_pct=12.0,
        expiry=date(2026, 9, 29),
    )
    assert fit is None


def test_entry_window() -> None:
    assert in_long_ic_entry_window(_now("08:59")) is False
    assert in_long_ic_entry_window(_now("09:14")) is False
    assert in_long_ic_entry_window(_now("09:15")) is True
    assert in_long_ic_entry_window(_now("09:25")) is True
    assert in_long_ic_entry_window(_now("09:26")) is False
    assert in_long_ic_entry_window(_now("15:14")) is False


def test_opens_at_first_nfo_print(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = _frame(bot, "09:15")
    assert ev is not None
    assert ev["event"] == "open"
    assert ev["debit"] == 65.3
    assert ev["lots"] == 1
    assert ev["qty"] == 65
    assert ev["pe_long_strike"] == 22850
    assert ev["pe_short_strike"] == 22650
    assert ev["ce_long_strike"] == 23250
    assert ev["ce_short_strike"] == 23450
    assert bot.position is not None
    assert bot.position.call_open is True
    assert bot.position.put_open is True
    assert bot.entries_today == 1


def test_blocks_before_nfo_open(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:00") is None
    assert bot.position is None


def test_blocks_cheap_or_fat_debit(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15", ce_l=12.0, ce_s=10.0, pe_l=11.0, pe_s=10.0) is None
    assert _frame(bot, "09:15", ce_l=180.0, ce_s=5.0, pe_l=40.0, pe_s=5.0) is None


def test_trend_day_still_opens(tmp_path: Path) -> None:
    """Long IC wants expansion — do not apply the 0.75% short-vol cap."""
    bot = _bot(tmp_path)
    ev = _frame(bot, "09:15")
    assert ev is not None
    assert ev["event"] == "open"


def test_waits_two_minutes_before_cutting_red(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    mid = _frame(bot, "09:16", ce_l=30.0, ce_s=15.0, pe_l=70.0, pe_s=25.0)
    assert mid is None
    assert bot.position is not None
    assert bot.position.call_open is True
    assert bot.position.put_open is True


def test_closes_red_call_first_keeps_green_put(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    ev = _frame(bot, "09:17", ce_l=30.0, ce_s=15.0, pe_l=80.0, pe_s=28.0)
    assert ev is not None
    assert ev["event"] == "close_vertical"
    assert ev["side"] == "call"
    assert ev["reason"] == "red"
    assert bot.position is not None
    assert bot.position.call_open is False
    assert bot.position.put_open is True
    assert ev["pnl"] is not None


def test_exits_green_when_it_turns_red(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    assert _frame(bot, "09:17", ce_l=30.0, ce_s=15.0, pe_l=80.0, pe_s=28.0) is not None
    assert bot.position is not None and bot.position.put_open is True
    closed = _frame(bot, "09:40", ce_l=20.0, ce_s=10.0, pe_l=40.0, pe_s=18.0)
    assert closed is not None
    assert closed["event"] == "close"
    assert closed["side"] == "put"
    assert closed["reason"] == "turn_red"
    assert bot.position is None


def test_giveback_exits_green_after_profit(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    # Call red, put +34.15 pts (80-28 vs 45.95-20.90)
    assert _frame(bot, "09:17", ce_l=30.0, ce_s=15.0, pe_l=80.0, pe_s=28.0) is not None
    # Put still green but gave back >40% from peak
    ev = _frame(bot, "09:35", ce_l=20.0, ce_s=10.0, pe_l=60.0, pe_s=24.0)
    assert ev is not None
    assert ev["reason"] == "giveback"
    assert ev["event"] == "close"
    assert bot.position is None


def test_both_red_at_validate_flattens(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    ev = _frame(bot, "09:17", ce_l=30.0, ce_s=18.0, pe_l=25.0, pe_s=16.0)
    assert ev is not None
    assert ev["event"] == "close"
    assert ev["reason"] == "validate_fail"
    assert bot.position is None


def test_one_entry_per_day(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    bot.position = None
    assert _frame(bot, "09:20") is None


def test_max_hold_flattens_when_not_green(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    closed = _frame(bot, "09:45")
    assert closed is not None
    assert closed["reason"] == "max_hold"
    assert bot.position is None


def test_max_hold_drops_flat_sibling_keeps_green(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    ev = _frame(bot, "09:45", ce_l=80.0, ce_s=25.0, pe_l=45.95, pe_s=20.90)
    assert ev is not None
    assert ev["event"] == "close_vertical"
    assert ev["side"] == "put"
    assert ev["reason"] == "max_hold"
    assert bot.position is not None
    assert bot.position.call_open is True
    assert bot.position.put_open is False


def test_live_scan_reject_does_not_fallback(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = bot.on_frame(
        now=_now("09:15", day="2026-09-29"),
        feed={
            "spot": 23063.0,
            "iv": 12.0,
            "expiry": date(2026, 9, 29),
            "ce": 152.55,
            "pe": 106.50,
        },
        book=_decay_book(),
        atm=23050,
        symbols=SYMS,
        option_symbol=_opt,
    )
    assert ev is None
    assert bot.position is None


def test_restores_zero_long_otm(tmp_path: Path) -> None:
    path = tmp_path / "lic.jsonl"
    path.write_text(
        '{"event":"open","strategy":"long_iron_condor","day":"2026-09-25",'
        '"atm":23050,"qty":65,"lots":1,"long_otm":0,"wing_pts":250,'
        '"pe_short_strike":22800,"pe_long_strike":23050,'
        '"ce_long_strike":23050,"ce_short_strike":23300,'
        '"pe_short_symbol":"NFO:PES","pe_long_symbol":"NFO:PEL",'
        '"ce_long_symbol":"NFO:CEL","ce_short_symbol":"NFO:CES",'
        '"pe_short_entry":37.25,"pe_long_entry":106.5,'
        '"ce_long_entry":152.55,"ce_short_entry":48.15,'
        '"debit":173.65,"charges":0}\n'
    )
    bot = PaperLongIronCondor(path=path, lot_size=65, lots=1, long_otm=200, wing_pts=200)
    assert bot.position is not None
    assert bot.position.long_otm == 0
    assert bot.position.wing_pts == 250


def test_max_hold_waits_when_green_is_paying(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    still = _frame(bot, "09:45", ce_l=80.0, ce_s=25.0, pe_l=70.0, pe_s=25.0)
    assert still is None
    assert bot.position is not None
    later = _frame(bot, "10:10", ce_l=85.0, ce_s=26.0, pe_l=72.0, pe_s=26.0)
    assert later is None
    assert bot.position is not None
    closed = _frame(bot, "15:14", ce_l=85.0, ce_s=26.0, pe_l=72.0, pe_s=26.0)
    assert closed is not None
    assert closed["reason"] == "time"


def test_session_gap_marks_when_quotes_live(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    closed = _frame(bot, "09:20", day="2026-09-28")
    assert closed is not None
    assert closed["reason"] == "session_gap"
    assert closed["day"] == "2026-09-28"
    assert closed["pnl"] is not None
    assert bot.traded_day == "2026-09-28"
    assert bot.entries_today == 1
    blocked = _frame(bot, "09:15", day="2026-09-28")
    assert blocked is None
    assert bot.position is None


def test_held_quotes_ignore_current_atm(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    mid = bot.on_frame(
        now=_now("09:30"),
        feed={},
        book=_Book({}),
        atm=24000,
        symbols={k: v + "X" for k, v in SYMS.items()},
    )
    assert mid is None
    assert bot.position is not None
    stale = bot.on_frame(
        now=_now("09:45"),
        feed={},
        book=_Book({}),
        atm=24000,
        symbols={k: v + "X" for k, v in SYMS.items()},
    )
    assert stale is None
    assert bot.position is not None
    closed = bot.on_frame(
        now=_now("15:14"),
        feed={},
        book=_Book({}),
        atm=24000,
        symbols={k: v + "X" for k, v in SYMS.items()},
    )
    assert closed is not None
    assert closed["reason"] == "time_flat"
    assert closed["pnl_known"] is False
    assert bot.day_pnl == 0.0


def test_stale_quote_at_max_hold_retries_then_marks(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    stale = bot.on_frame(
        now=_now("09:45"),
        feed={},
        book=_Book({}),
        atm=23050,
        symbols=SYMS,
    )
    assert stale is None
    assert bot.position is not None
    closed = _frame(bot, "09:46")
    assert closed is not None
    assert closed["reason"] == "max_hold"
    assert closed["pnl_known"] is True
    assert bot.position is None


def test_close_write_fail_keeps_position(tmp_path: Path, monkeypatch) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    monkeypatch.setattr(bot, "_append", lambda ev: None)
    closed = _frame(bot, "09:45")
    assert closed is None
    assert bot.position is not None
    assert bot.day_pnl == 0.0


def test_seals_day_pnl_before_roll(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    closed = _frame(bot, "09:17", ce_l=30.0, ce_s=18.0, pe_l=25.0, pe_s=16.0)
    assert closed is not None
    loss = closed["pnl"]
    assert bot.eod_written is False
    sealed = _frame(bot, "09:16", day="2026-09-28")
    assert sealed is not None
    assert sealed["event"] == "day_pnl"
    assert sealed["day"] == "2026-09-25"
    assert sealed["day_pnl"] == loss
    assert bot.traded_day == "2026-09-28"
    assert bot.day_pnl == 0.0


def test_idle_day_skips_zero_eod(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    ev = bot.on_frame(
        now=_now("15:14"),
        feed={},
        book=_book(61.8, 21.55, 45.95, 20.90),
        atm=23050,
        symbols=SYMS,
    )
    assert ev is None
    assert bot.eod_written is True
    assert (not bot.path.exists()) or bot.path.read_text() == ""


def test_weekend_flatten_reason(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    closed = _frame(bot, "10:00", day="2026-09-26")
    assert closed is not None
    assert closed["reason"] == "weekend"
    assert closed["day"] == "2026-09-26"
    assert bot.traded_day == "2026-09-26"


def test_restores_open_and_partial(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    assert _frame(bot, "09:17", ce_l=30.0, ce_s=15.0, pe_l=80.0, pe_s=28.0) is not None
    again = PaperLongIronCondor(path=tmp_path / "lic.jsonl", lot_size=65, lots=1)
    assert again.position is not None
    assert again.position.call_open is False
    assert again.position.put_open is True
    assert again.entries_today == 1
    assert again.position.debit == 65.3


def test_restart_keeps_leftover_flatten_cap(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    closed = _frame(bot, "09:20", day="2026-09-28")
    assert closed is not None
    assert bot.entries_today == 1
    again = PaperLongIronCondor(path=tmp_path / "lic.jsonl", lot_size=65, lots=1)
    assert again.position is None
    assert again.traded_day == "2026-09-28"
    assert again.entries_today == 1
    blocked = _frame(again, "09:15", day="2026-09-28")
    assert blocked is None
    assert again.position is None


def test_default_is_one_lot(tmp_path: Path) -> None:
    bot = PaperLongIronCondor(path=tmp_path / "lic.jsonl", lot_size=65)
    ev = _frame(bot, "09:15")
    assert ev is not None
    assert ev["lots"] == 1
    assert ev["qty"] == 65


def test_snapshot_after_red_close_does_not_double_charges(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    red = _frame(bot, "09:17", ce_l=30.0, ce_s=15.0, pe_l=80.0, pe_s=28.0)
    assert red is not None
    snap = bot.snapshot(book=_book(30.0, 15.0, 80.0, 28.0))
    put_mtm = vertical_mtm_pts(80.0, 28.0, 45.95, 20.90) * 65
    assert snap["open_pnl_gross"] == round(put_mtm, 2)
    assert snap["mtm_pnl"] == round(red["pnl"] + snap["open_pnl"], 2)
    # Remaining charges are the still-open put only, not the closed call again.
    assert bot.position is not None
    assert snap["charges"] < bot.position.charges_open + 80.0


def test_green_exit_without_closed_side_quotes(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert _frame(bot, "09:15") is not None
    assert _frame(bot, "09:17", ce_l=30.0, ce_s=15.0, pe_l=80.0, pe_s=28.0) is not None
    closed = bot.on_frame(
        now=_now("09:40"),
        feed={},
        book=_Book(
            {
                "NFO:PEL": {"last_price": 40.0},
                "NFO:PES": {"last_price": 18.0},
            }
        ),
        atm=23050,
        symbols=SYMS,
    )
    assert closed is not None
    assert closed["reason"] == "turn_red"
    assert closed["pnl_known"] is True
