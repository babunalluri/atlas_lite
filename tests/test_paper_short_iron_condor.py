"""Paper short iron condor — credit 4–5/side, ₹2k book TP, hold to expiry."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite.paper_short_iron_condor import (
    WING_CHOICES,
    PaperShortIronCondor,
    credit_in_band,
    fit_credit_vertical,
    fit_short_iron_condor,
    in_short_ic_entry_window,
    intrinsic_set_mark,
    set_credit,
)
from atlas_lite.paper_short_iron_condor import SetState

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = date(2026, 10, 9)  # hold across sessions to this weekly settle


class _Book:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def get(self, symbol: str):
        return self.rows.get(symbol)


def _sym(strike: int, side: str) -> str:
    return f"NFO:{strike}{side}"


def _now(hm: str, day: str = "2026-10-06") -> datetime:
    parts = hm.split(":")
    if len(parts) == 2:
        hm = f"{hm}:00"
    return datetime.fromisoformat(f"{day}T{hm}+05:30")


def _feed(spot: float = 22420.0, expiry: date = EXPIRY) -> dict:
    return {"spot": spot, "expiry": expiry}


def _book_from_map(px: dict[str, float]) -> _Book:
    # Bid=ask=ltp so fill marks match tests; live path uses real TOB when present.
    return _Book(
        {
            k: {"last_price": v, "bid": v, "ask": v}
            for k, v in px.items()
        }
    )


def _confirm_stop(
    bot,
    *,
    day: str,
    hm: str,
    feed: dict,
    book,
    atm: int,
    allow_entry: bool = True,
    block_reason: str | None = None,
):
    """Arm mid/LTP stop then confirm after STOP_CONFIRM_S."""
    from datetime import timedelta

    t0 = _now(hm, day)
    bot.on_frame(
        now=t0,
        feed=feed,
        book=book,
        atm=atm,
        option_symbol=_sym,
        allow_entry=allow_entry,
        block_reason=block_reason,
    )
    return bot.on_frame(
        now=t0 + timedelta(seconds=3),
        feed=feed,
        book=book,
        atm=atm,
        option_symbol=_sym,
        allow_entry=allow_entry,
        block_reason=block_reason,
    )


def test_credit_band_and_window() -> None:
    assert credit_in_band(4.0)
    assert credit_in_band(5.0)
    assert not credit_in_band(3.9)
    assert not credit_in_band(5.1)
    assert set_credit(10.75, 4.35) == 6.4
    assert in_short_ic_entry_window(_now("10:00"))
    assert not in_short_ic_entry_window(_now("09:10"))
    assert not in_short_ic_entry_window(_now("15:00"))
    assert 300 in WING_CHOICES and 400 in WING_CHOICES


def test_fit_finds_4_to_5_credit_sides() -> None:
    atm = 22400
    px = {
        _sym(23000, "CE"): 10.75,
        _sym(23300, "CE"): 6.0,  # credit 4.75, wing 300 (Sensibull-style)
        _sym(21850, "PE"): 9.5,
        _sym(21450, "PE"): 5.0,  # credit 4.5, wing 400
        # noise outside band
        _sym(22900, "CE"): 20.0,
        _sym(23200, "CE"): 12.0,
        _sym(21900, "PE"): 15.0,
        _sym(21600, "PE"): 8.0,
    }
    book = _book_from_map(px)
    fitted = fit_short_iron_condor(book, _sym, atm=atm)
    assert fitted is not None
    ce, pe = fitted
    assert credit_in_band(ce.credit)
    assert credit_in_band(pe.credit)
    assert abs(ce.long_strike - ce.short_strike) in WING_CHOICES
    assert abs(pe.short_strike - pe.long_strike) in WING_CHOICES
    # Prefer Sensibull-wide wings when both fit.
    assert abs(ce.long_strike - ce.short_strike) >= 300
    assert abs(pe.short_strike - pe.long_strike) >= 300


def _band_book() -> dict[str, float]:
    """Single CE + PE pair in the 4–5 credit band (Sensibull 300pt wings)."""
    return {
        _sym(23000, "CE"): 9.0,
        _sym(23300, "CE"): 4.5,  # 4.5, wing 300
        _sym(21850, "PE"): 9.0,
        _sym(21550, "PE"): 4.5,  # 4.5, wing 300
    }


def test_open_set_stop_keeps_other_leg(tmp_path: Path) -> None:
    bot = PaperShortIronCondor(path=tmp_path / "sic.jsonl", lot_size=65, lots=1, capital=200_000)
    atm = 22400
    px = _band_book()
    ev = bot.on_frame(
        now=_now("10:00"),
        feed=_feed(),
        book=_book_from_map(px),
        atm=atm,
        option_symbol=_sym,
        allow_entry=True,
    )
    assert ev is not None and ev["event"] == "open"
    assert bot.position is not None
    assert bot.position.expiry == EXPIRY.isoformat()
    ce_s = bot.position.ce.short_symbol
    ce_l = bot.position.ce.long_symbol
    pe_s = bot.position.pe.short_symbol
    pe_l = bot.position.pe.long_symbol
    credit = bot.position.ce.credit

    # Blow the live CE symbols to 4× entry credit — PE stays calm
    px2 = {
        ce_s: credit * 4 + 4.0,
        ce_l: 4.0,  # diff = 4× credit
        pe_s: 8.0,
        pe_l: 4.0,
    }
    ev2 = _confirm_stop(
        bot,
        day="2026-10-06",
        hm="11:00:00",
        feed=_feed(22800.0),
        book=_book_from_map(px2),
        atm=atm,
    )
    assert ev2 is not None
    assert ev2["event"] in ("close_set", "reentry")
    assert bot.position is not None
    assert bot.position.ce.open is False or bot.position.ce.is_reentry
    assert bot.position.pe.open is True


def test_target_1pct_closes_book(tmp_path: Path) -> None:
    bot = PaperShortIronCondor(
        path=tmp_path / "sic.jsonl",
        lot_size=65,
        lots=1,
        capital=200_000,
        target_pct=0.01,
    )
    atm = 22400
    px = _band_book()
    bot.on_frame(
        now=_now("10:00"),
        feed=_feed(),
        book=_book_from_map(px),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    ce_s, ce_l = bot.position.ce.short_symbol, bot.position.ce.long_symbol
    pe_s, pe_l = bot.position.pe.short_symbol, bot.position.pe.long_symbol
    # Force tiny capital so 1% is reachable on this mark.
    bot.capital = 1_000.0  # 1% = ₹10
    px2 = {
        ce_s: 2.0,
        ce_l: 1.5,
        pe_s: 2.0,
        pe_l: 1.5,
    }
    ev = bot.on_frame(
        now=_now("12:00"),
        feed=_feed(),
        book=_book_from_map(px2),
        atm=atm,
        option_symbol=_sym,
    )
    assert ev is not None
    assert bot.position is None
    assert bot.day_pnl > 0


def test_reentry_after_stop(tmp_path: Path) -> None:
    # 6 lots (Sensibull qty 390) → 4.5×390 ≥ ₹1,000 re-entry gate on default ₹20L book.
    bot = PaperShortIronCondor(path=tmp_path / "sic.jsonl", lot_size=65, lots=6)
    atm = 22400
    bot.on_frame(
        now=_now("10:00"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    ce_s = bot.position.ce.short_symbol
    ce_l = bot.position.ce.long_symbol
    pe_s = bot.position.pe.short_symbol
    pe_l = bot.position.pe.long_symbol
    credit = bot.position.ce.credit
    opened_at = bot.position.opened_at
    # Stop the live CE set; introduce a fresh in-band CE vertical for re-entry.
    px2 = {
        ce_s: credit * 4 + 4.0,
        ce_l: 4.0,
        pe_s: 8.0,
        pe_l: 4.0,
        _sym(23150, "CE"): 8.5,
        _sym(23450, "CE"): 4.0,  # wing 300
    }
    ev = _confirm_stop(
        bot,
        day="2026-10-06",
        hm="11:00:00",
        feed=_feed(22850.0),
        book=_book_from_map(px2),
        atm=atm,
    )
    assert ev is not None
    assert bot.position is not None
    assert bot.position.ce.open and bot.position.ce.is_reentry
    assert bot.position.ce.reentries >= 1
    assert bot.position.ce.short_symbol == _sym(23150, "CE")
    assert ev.get("credit") == bot.position.ce.credit
    assert bot.position.opened_at == opened_at
    assert ev.get("opened_at") == opened_at


def test_policy_gate_blocks_reentry_after_stop(tmp_path: Path) -> None:
    """Kill switch / skip_entries must block re-entry, not only fresh condors."""
    bot = PaperShortIronCondor(path=tmp_path / "sic.jsonl", lot_size=65, lots=6)
    atm = 22400
    bot.on_frame(
        now=_now("10:00"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    ce_s = bot.position.ce.short_symbol
    ce_l = bot.position.ce.long_symbol
    pe_s = bot.position.pe.short_symbol
    pe_l = bot.position.pe.long_symbol
    credit = bot.position.ce.credit
    px2 = {
        ce_s: credit * 4 + 4.0,
        ce_l: 4.0,
        pe_s: 8.0,
        pe_l: 4.0,
        _sym(23150, "CE"): 8.5,
        _sym(23450, "CE"): 4.0,
    }
    ev = _confirm_stop(
        bot,
        day="2026-10-06",
        hm="11:00:00",
        feed=_feed(22850.0),
        book=_book_from_map(px2),
        atm=atm,
        allow_entry=False,
        block_reason="policy_gate",
    )
    assert ev is not None
    assert ev.get("event") == "close_set"
    assert bot.position is not None
    assert bot.position.ce.open is False
    assert bot.position.ce.awaiting_reentry is True
    assert bot.position.ce.is_reentry is False
    assert bot.last_reject == "policy_gate"

    # Still blocked on later frames while awaiting_reentry.
    bot.on_frame(
        now=_now("11:01"),
        feed=_feed(22850.0),
        book=_book_from_map(px2),
        atm=atm,
        option_symbol=_sym,
        allow_entry=False,
        block_reason="policy_gate",
    )
    assert bot.position.ce.open is False
    assert bot.last_reject == "policy_gate"


def test_same_frame_ledger_kill_blocks_reentry(tmp_path: Path) -> None:
    """After 2 morning stop losses, same-frame re-entry must die even if allow_entry is stale True."""
    bot = PaperShortIronCondor(path=tmp_path / "sic.jsonl", lot_size=65, lots=6)
    atm = 22400
    bot.on_frame(
        now=_now("09:30"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None

    def _stop_book(live_short: str, live_long: str, next_short: int, next_long: int) -> dict:
        credit = 4.5
        return {
            live_short: credit * 4 + 4.0,
            live_long: 4.0,
            _sym(21850, "PE"): 8.0,
            _sym(21550, "PE"): 4.0,
            _sym(next_short, "CE"): 8.5,
            _sym(next_long, "CE"): 4.0,
        }

    # Loss #1 + re-entry (morning kill needs 2).
    ce_s = bot.position.ce.short_symbol
    ce_l = bot.position.ce.long_symbol
    ev1 = _confirm_stop(
        bot,
        day="2026-10-06",
        hm="10:00:00",
        feed=_feed(22850.0),
        book=_book_from_map(_stop_book(ce_s, ce_l, 23150, 23450)),
        atm=atm,
        allow_entry=True,
    )
    assert ev1 is not None
    assert bot.position is not None and bot.position.ce.open and bot.position.ce.is_reentry

    # Loss #2 — allow_entry still True (policy loop lag); ledger kill must block re-entry.
    ce_s2 = bot.position.ce.short_symbol
    ce_l2 = bot.position.ce.long_symbol
    ev2 = _confirm_stop(
        bot,
        day="2026-10-06",
        hm="10:05:00",
        feed=_feed(22900.0),
        book=_book_from_map(_stop_book(ce_s2, ce_l2, 23200, 23500)),
        atm=atm,
        allow_entry=True,
    )
    assert ev2 is not None
    assert ev2.get("event") == "close_set"
    assert bot.position is not None
    assert bot.position.ce.open is False
    assert bot.position.ce.awaiting_reentry is True
    assert bot.position.ce.reentries == 1  # only the first re-entry filled
    assert bot.last_reject is not None
    assert str(bot.last_reject).startswith("policy_kill:")
    assert "morning_losses=" in str(bot.last_reject)


def test_restore_does_not_double_count_flatten_pnl(tmp_path: Path) -> None:
    path = tmp_path / "sic.jsonl"
    bot = PaperShortIronCondor(path=path, lot_size=65, lots=1, capital=1_000)
    atm = 22400
    bot.on_frame(
        now=_now("10:00"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    ce_s, ce_l = bot.position.ce.short_symbol, bot.position.ce.long_symbol
    pe_s, pe_l = bot.position.pe.short_symbol, bot.position.pe.long_symbol
    bot.on_frame(
        now=_now("12:00"),
        feed=_feed(),
        book=_book_from_map({ce_s: 2.0, ce_l: 1.5, pe_s: 2.0, pe_l: 1.5}),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is None
    booked = bot.day_pnl
    again = PaperShortIronCondor(path=path, lot_size=65, lots=1, capital=1_000)
    assert again.position is None
    assert again.day_pnl == booked


def test_one_sided_stop_keeps_remaining_open_charges(tmp_path: Path) -> None:
    bot = PaperShortIronCondor(path=tmp_path / "sic.jsonl", lot_size=65, lots=1)
    atm = 22400
    bot.on_frame(
        now=_now("10:00"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    open_ch = bot.position.charges_open
    ce_s = bot.position.ce.short_symbol
    ce_l = bot.position.ce.long_symbol
    pe_s = bot.position.pe.short_symbol
    pe_l = bot.position.pe.long_symbol
    credit = bot.position.ce.credit
    _confirm_stop(
        bot,
        day="2026-10-06",
        hm="11:00:00",
        feed=_feed(22800.0),
        book=_book_from_map(
            {
                ce_s: credit * 4 + 4.0,
                ce_l: 4.0,
                pe_s: 8.0,
                pe_l: 4.0,
            }
        ),
        atm=atm,
    )
    assert bot.position is not None
    assert bot.position.pe.open is True
    # Remaining PE still carries ~half the original open charges (not wiped to 0).
    assert bot.position.charges_open == round(open_ch * 0.5, 2)
    half = bot.position.charges_open
    # Restart must restore the remaining half, not the full open charges field.
    again = PaperShortIronCondor(path=bot.path, lot_size=65, lots=1)
    assert again.position is not None
    assert again.position.pe.open is True
    assert again.position.charges_open == half


def test_holds_overnight_until_expiry(tmp_path: Path) -> None:
    bot = PaperShortIronCondor(path=tmp_path / "sic.jsonl", lot_size=65, lots=1)
    atm = 22400
    bot.on_frame(
        now=_now("10:00", "2026-10-06"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    ce_s, ce_l = bot.position.ce.short_symbol, bot.position.ce.long_symbol
    pe_s, pe_l = bot.position.pe.short_symbol, bot.position.pe.long_symbol
    # Next session morning — still open (no day_roll / square_off).
    bot.on_frame(
        now=_now("10:00", "2026-10-07"),
        feed=_feed(),
        book=_book_from_map({ce_s: 8.0, ce_l: 4.0, pe_s: 8.0, pe_l: 4.0}),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    assert bot.position.ce.open and bot.position.pe.open
    # Expiry flatten at 15:20.
    ev = bot.on_frame(
        now=_now("15:20", "2026-10-09"),
        feed=_feed(),
        book=_book_from_map({ce_s: 2.0, ce_l: 1.0, pe_s: 2.0, pe_l: 1.0}),
        atm=atm,
        option_symbol=_sym,
    )
    assert ev is not None
    assert bot.position is None


def test_no_reentry_after_reentry_target(tmp_path: Path) -> None:
    bot = PaperShortIronCondor(path=tmp_path / "sic.jsonl", lot_size=65, lots=6, capital=200_000)
    atm = 22400
    bot.on_frame(
        now=_now("10:00"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    ce_s = bot.position.ce.short_symbol
    ce_l = bot.position.ce.long_symbol
    pe_s = bot.position.pe.short_symbol
    pe_l = bot.position.pe.long_symbol
    credit = bot.position.ce.credit
    # Stop CE → re-enter on alternate strikes.
    _confirm_stop(
        bot,
        day="2026-10-06",
        hm="11:00:00",
        feed=_feed(22850.0),
        book=_book_from_map(
            {
                ce_s: credit * 4 + 4.0,
                ce_l: 4.0,
                pe_s: 8.0,
                pe_l: 4.0,
                _sym(23150, "CE"): 8.5,
                _sym(23450, "CE"): 4.0,
            }
        ),
        atm=atm,
    )
    assert bot.position is not None and bot.position.ce.is_reentry
    re_s = bot.position.ce.short_symbol
    re_l = bot.position.ce.long_symbol
    # Hit re-entry TP (credit collapses) — must not open another CE set.
    bot.position.ce.target_rupees = 10.0
    bot.on_frame(
        now=_now("12:00"),
        feed=_feed(),
        book=_book_from_map(
            {
                re_s: 2.0,
                re_l: 1.5,
                pe_s: 8.0,
                pe_l: 4.0,
                _sym(23200, "CE"): 8.5,
                _sym(23500, "CE"): 4.0,
            }
        ),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    assert bot.position.ce.open is False
    assert bot.position.ce.awaiting_reentry is False
    assert bot.position.pe.open is True


def test_expiry_force_settles_without_quotes(tmp_path: Path) -> None:
    bot = PaperShortIronCondor(path=tmp_path / "sic.jsonl", lot_size=65, lots=1)
    atm = 22400
    bot.on_frame(
        now=_now("10:00", "2026-10-06"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    # Record expiry-day spot, then miss the 15:20 quote flatten.
    bot.on_frame(
        now=_now("15:00", "2026-10-09"),
        feed=_feed(spot=22500.0, expiry=EXPIRY),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    assert bot.position.expiry_spot == 22500.0
    # Day after expiry, empty book — settle from expiry_spot, not next-day gap.
    ev = bot.on_frame(
        now=_now("10:00", "2026-10-10"),
        feed=_feed(spot=23000.0, expiry=date(2026, 10, 16)),
        book=_Book({}),
        atm=atm,
        option_symbol=_sym,
    )
    assert ev is not None
    assert bot.position is None
    assert ev.get("settle") == "intrinsic"
    assert ev.get("settle_spot") == 22500.0
    assert ev.get("pnl_known") is False


def test_expiry_spot_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "sic.jsonl"
    bot = PaperShortIronCondor(path=path, lot_size=65, lots=1)
    atm = 22400
    bot.on_frame(
        now=_now("10:00", "2026-10-06"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    bot.on_frame(
        now=_now("14:00", "2026-10-09"),
        feed=_feed(spot=22550.0, expiry=EXPIRY),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    assert bot.position.expiry_spot == 22550.0
    again = PaperShortIronCondor(path=path, lot_size=65, lots=1)
    assert again.position is not None
    assert again.position.expiry_spot == 22550.0


def test_target_uses_fill_mtm_not_optimistic_mid(tmp_path: Path) -> None:
    bot = PaperShortIronCondor(
        path=tmp_path / "sic.jsonl",
        lot_size=65,
        lots=1,
        capital=1_000.0,  # 1% = ₹10
    )
    atm = 22400
    bot.on_frame(
        now=_now("10:00"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    ce_s = bot.position.ce.short_symbol
    ce_l = bot.position.ce.long_symbol
    pe_s = bot.position.pe.short_symbol
    pe_l = bot.position.pe.long_symbol
    # Mid looks like a big win; ask/bid fill MTM stays small — must NOT take 1% TP.
    wide = {
        ce_s: {"last_price": 2.0, "bid": 1.5, "ask": 8.0},
        ce_l: {"last_price": 1.0, "bid": 0.05, "ask": 1.0},
        pe_s: {"last_price": 2.0, "bid": 1.5, "ask": 8.0},
        pe_l: {"last_price": 1.0, "bid": 0.05, "ask": 1.0},
    }
    bot.on_frame(
        now=_now("12:00"),
        feed=_feed(),
        book=_Book(wide),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None


def test_missed_expiry_without_expiry_spot_is_unknown(tmp_path: Path) -> None:
    bot = PaperShortIronCondor(path=tmp_path / "sic.jsonl", lot_size=65, lots=1)
    atm = 22400
    bot.on_frame(
        now=_now("10:00", "2026-10-06"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    # Jump past expiry with no expiry-day spot recorded — must not use entry spot.
    ev = bot.on_frame(
        now=_now("10:00", "2026-10-10"),
        feed=_feed(spot=23000.0, expiry=date(2026, 10, 16)),
        book=_Book({}),
        atm=atm,
        option_symbol=_sym,
    )
    assert ev is not None
    assert bot.position is None
    assert ev.get("settle") == "unknown"
    assert ev.get("pnl_known") is False


def test_wide_ask_at_open_does_not_false_stop(tmp_path: Path) -> None:
    bot = PaperShortIronCondor(path=tmp_path / "sic.jsonl", lot_size=65, lots=1)
    atm = 22400
    bot.on_frame(
        now=_now("10:00", "2026-10-06"),
        feed=_feed(),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    ce_s = bot.position.ce.short_symbol
    ce_l = bot.position.ce.long_symbol
    pe_s = bot.position.pe.short_symbol
    pe_l = bot.position.pe.long_symbol
    credit = bot.position.ce.credit
    # Auction: ask 3× fair on short, mid still calm — must not stop before 09:20 or on mid.
    wide = {
        ce_s: {"last_price": credit, "bid": credit, "ask": credit * 4 + 4.0},
        ce_l: {"last_price": 1.0, "bid": 0.5, "ask": 1.0},
        pe_s: {"last_price": 4.0, "bid": 4.0, "ask": 4.0},
        pe_l: {"last_price": 1.0, "bid": 1.0, "ask": 1.0},
    }
    bot.on_frame(
        now=_now("09:15:00", "2026-10-07"),
        feed=_feed(),
        book=_Book(wide),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    assert bot.position.ce.open is True
    # After 09:20, mid still under 4× — still open even if ask is wild.
    bot.on_frame(
        now=_now("09:20:00", "2026-10-07"),
        feed=_feed(),
        book=_Book(wide),
        atm=atm,
        option_symbol=_sym,
    )
    bot.on_frame(
        now=_now("09:20:03", "2026-10-07"),
        feed=_feed(),
        book=_Book(wide),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    assert bot.position.ce.open is True


def test_intrinsic_set_mark() -> None:
    st = SetState(
        side="ce",
        short_strike=23000,
        long_strike=23300,
        short_symbol="x",
        long_symbol="y",
        short_entry=9.0,
        long_entry=4.5,
        credit=4.5,
    )
    s, l, cur = intrinsic_set_mark(st, 23100.0)
    assert s == 100.0
    assert l == 0.0
    assert cur == 100.0


def test_no_reentry_after_expiry_noon(tmp_path: Path) -> None:
    bot = PaperShortIronCondor(path=tmp_path / "sic.jsonl", lot_size=65, lots=6)
    atm = 22400
    # Open on expiry morning.
    bot.on_frame(
        now=_now("10:00", "2026-10-09"),
        feed=_feed(expiry=date(2026, 10, 9)),
        book=_book_from_map(_band_book()),
        atm=atm,
        option_symbol=_sym,
    )
    assert bot.position is not None
    ce_s = bot.position.ce.short_symbol
    ce_l = bot.position.ce.long_symbol
    pe_s = bot.position.pe.short_symbol
    pe_l = bot.position.pe.long_symbol
    credit = bot.position.ce.credit
    _confirm_stop(
        bot,
        day="2026-10-09",
        hm="12:30:00",
        feed=_feed(22850.0, expiry=date(2026, 10, 9)),
        book=_book_from_map(
            {
                ce_s: credit * 4 + 4.0,
                ce_l: 4.0,
                pe_s: 8.0,
                pe_l: 4.0,
                _sym(23150, "CE"): 8.5,
                _sym(23450, "CE"): 4.0,
            }
        ),
        atm=atm,
    )
    assert bot.position is not None
    assert bot.position.ce.open is False
    assert bot.position.ce.is_reentry is False
    assert bot.position.ce.awaiting_reentry is False


def test_fit_credit_vertical_none_when_band_miss() -> None:
    book = _book_from_map(
        {
            _sym(23000, "CE"): 20.0,
            _sym(23300, "CE"): 5.0,  # credit 15
        }
    )
    assert fit_credit_vertical(book, _sym, side="ce", atm=22400) is None
