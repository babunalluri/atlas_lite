"""Paper theta-cliff fence — expiry noon short iron condor."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_lite.paper_theta_cliff import (
    PaperThetaCliff,
    ceil_strike,
    fence_strikes,
    fill_buy,
    fill_sell,
    floor_strike,
    in_theta_cliff_entry_window,
    load_vix_prev,
    morning_session_stats,
    remaining_sigma_pts,
    rv_filter_ok,
    save_vix_prev,
    slip_pts,
)

IST = ZoneInfo("Asia/Kolkata")


class _Book:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def get(self, symbol: str):
        return self.rows.get(symbol)


def _now(hm: str, day: str = "2026-09-29") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def _opt(strike: int, side: str) -> str:
    return f"NFO:{int(strike)}{side.upper()}"


def test_strike_rounding_and_fence() -> None:
    assert ceil_strike(25123) == 25150
    assert floor_strike(25123) == 25100
    pe_l, pe_s, ce_s, ce_l = fence_strikes(
        spot=25000,
        morning_high=25080,
        morning_low=24940,
        sigma_pts=100,
        wing_pts=100,
    )
    # Matches tcf_bt: ceil/floor of fence level (no extra step on exact strikes).
    assert ce_s == 25100
    assert pe_s == 24900
    assert ce_l == 25200
    assert pe_l == 24800
    _, pe_s2, ce_s2, _ = fence_strikes(
        spot=25000,
        morning_high=25100,
        morning_low=24900,
        sigma_pts=10,
        wing_pts=100,
    )
    assert ce_s2 == 25100
    assert pe_s2 == 24900


def test_rv_filter_and_sigma() -> None:
    assert rv_filter_ok(10.0, 12.0) is True
    assert rv_filter_ok(11.0, 12.0) is False
    now = _now("12:00")
    sig = remaining_sigma_pts(25000, 12.0, now)
    assert 20 < sig < 200


def test_entry_window() -> None:
    assert in_theta_cliff_entry_window(_now("11:59")) is False
    assert in_theta_cliff_entry_window(_now("12:00")) is True
    assert in_theta_cliff_entry_window(_now("12:10")) is True
    assert in_theta_cliff_entry_window(_now("12:11")) is False
    assert in_theta_cliff_entry_window(_now("15:15")) is False


def _morning_bars(day: str = "2026-09-29", spot: float = 25000.0) -> list[dict]:
    bars = []
    # 09:15 → 11:59 every minute, mild grind
    px = spot
    for mins in range(9 * 60 + 15, 12 * 60):
        hh, mm = divmod(mins, 60)
        # small random-ish walk via deterministic sine-like step
        step = ((mins % 7) - 3) * 0.5
        o = px
        c = px + step
        h = max(o, c) + 2
        l = min(o, c) - 2
        bars.append(
            {
                "t": f"{day} {hh:02d}:{mm:02d}",
                "o": o,
                "h": h,
                "l": l,
                "c": c,
                "v": 100.0,
            }
        )
        px = c
    return bars


def test_morning_session_stats() -> None:
    bars = _morning_bars()
    stats = morning_session_stats(bars, day="2026-09-29", until_hm="12:00")
    assert stats is not None
    assert stats["morning_high"] > stats["morning_low"]
    assert stats["rv_pct"] > 0


def _quote_book(pe_l: int, pe_s: int, ce_s: int, ce_l: int) -> _Book:
    return _Book(
        {
            _opt(pe_l, "PE"): {"last_price": 8.0},
            _opt(pe_s, "PE"): {"last_price": 28.0},
            _opt(ce_s, "CE"): {"last_price": 30.0},
            _opt(ce_l, "CE"): {"last_price": 9.0},
        }
    )


def test_open_stop_one_side_and_square(tmp_path: Path) -> None:
    bot = PaperThetaCliff(path=tmp_path / "paper_theta_cliff.jsonl", lot_size=65, lots=1)
    bars = _morning_bars()
    stats = morning_session_stats(bars, day="2026-09-29")
    assert stats is not None
    spot = 25000.0
    sigma = remaining_sigma_pts(spot, 12.0, _now("12:00"))
    pe_l, pe_s, ce_s, ce_l = fence_strikes(
        spot, stats["morning_high"], stats["morning_low"], sigma
    )
    book = _quote_book(pe_l, pe_s, ce_s, ce_l)
    feed = {
        "expiry": date(2026, 9, 29),
        "spot": spot,
        "vix_yesterday": 12.0,
        "vix": 11.5,
    }
    opened = bot.on_frame(
        now=_now("12:00"),
        feed=feed,
        book=book,
        bars_1m=bars,
        option_symbol=_opt,
        allow_entry=True,
    )
    assert opened is not None
    assert opened["event"] == "open"
    assert opened.get("opened_at")
    assert bot.position is not None
    assert bot.position.credit > 0

    # Spot touches CE short → close call vertical only
    feed_touch = {**feed, "spot": float(bot.position.ce_short_strike)}
    closed_ce = bot.on_frame(
        now=_now("13:00"),
        feed=feed_touch,
        book=book,
        bars_1m=bars,
        option_symbol=_opt,
    )
    assert closed_ce is not None
    assert closed_ce["event"] == "close_vertical"
    assert closed_ce["side"] == "ce"
    assert bot.position is not None
    assert bot.position.ce_open is False
    assert bot.position.pe_open is True

    # Square-off remaining
    flat = bot.on_frame(
        now=_now("15:15"),
        feed=feed,
        book=book,
        bars_1m=bars,
        option_symbol=_opt,
    )
    assert flat is not None
    assert bot.position is None


def test_square_off_retries_then_force(tmp_path: Path) -> None:
    bot = PaperThetaCliff(path=tmp_path / "paper_theta_cliff.jsonl", lot_size=65)
    bars = _morning_bars()
    stats = morning_session_stats(bars, day="2026-09-29")
    assert stats is not None
    spot = 25000.0
    sigma = remaining_sigma_pts(spot, 12.0, _now("12:00"))
    pe_l, pe_s, ce_s, ce_l = fence_strikes(
        spot, stats["morning_high"], stats["morning_low"], sigma
    )
    book = _quote_book(pe_l, pe_s, ce_s, ce_l)
    feed = {
        "expiry": date(2026, 9, 29),
        "spot": spot,
        "vix_yesterday": 12.0,
    }
    assert bot.on_frame(
        now=_now("12:00"),
        feed=feed,
        book=book,
        bars_1m=bars,
        option_symbol=_opt,
    )
    # 15:15 with no quotes: stay open and retry (do not zero P&L yet).
    stuck = bot.on_frame(
        now=_now("15:15"),
        feed=feed,
        book=_Book({}),
        bars_1m=bars,
        option_symbol=_opt,
    )
    assert stuck is None
    assert bot.position is not None
    # 15:25: force-flat without recursion.
    flat = bot.on_frame(
        now=_now("15:25"),
        feed=feed,
        book=_Book({}),
        bars_1m=bars,
        option_symbol=_opt,
    )
    assert flat is not None
    assert bot.position is None


def test_sticky_stop_retries_missing_quotes(tmp_path: Path) -> None:
    bot = PaperThetaCliff(path=tmp_path / "paper_theta_cliff.jsonl", lot_size=65)
    bars = _morning_bars()
    stats = morning_session_stats(bars, day="2026-09-29")
    assert stats is not None
    spot = 25000.0
    sigma = remaining_sigma_pts(spot, 12.0, _now("12:00"))
    pe_l, pe_s, ce_s, ce_l = fence_strikes(
        spot, stats["morning_high"], stats["morning_low"], sigma
    )
    book = _quote_book(pe_l, pe_s, ce_s, ce_l)
    feed = {
        "expiry": date(2026, 9, 29),
        "spot": spot,
        "vix_yesterday": 12.0,
    }
    assert bot.on_frame(
        now=_now("12:00"),
        feed=feed,
        book=book,
        bars_1m=bars,
        option_symbol=_opt,
        allow_entry=True,
    )
    assert bot.position is not None
    # Touch CE short with empty book → arm pending, stay open.
    bot.on_frame(
        now=_now("13:00"),
        feed={**feed, "spot": float(bot.position.ce_short_strike)},
        book=_Book({}),
        bars_1m=bars,
        option_symbol=_opt,
    )
    assert bot.position is not None
    assert bot.position.ce_stop_pending is True
    # Spot retreats but pending remains → closes when quotes return.
    closed = bot.on_frame(
        now=_now("13:01"),
        feed={**feed, "spot": spot},
        book=book,
        bars_1m=bars,
        option_symbol=_opt,
    )
    assert closed is not None
    assert closed["event"] == "close_vertical"
    assert closed["side"] == "ce"


def test_vix_prev_rejects_stale_and_today(tmp_path: Path) -> None:
    path = tmp_path / "vix_prev.json"
    save_vix_prev(path, day="2026-09-28", vix=12.5)
    assert load_vix_prev(path, today="2026-09-29") == 12.5
    assert load_vix_prev(path, today="2026-09-28") is None
    save_vix_prev(path, day="2026-09-01", vix=11.0)
    assert load_vix_prev(path, today="2026-09-29") is None


def test_slip_matches_tcf_bt() -> None:
    assert slip_pts(10.0) == 0.5
    assert slip_pts(100.0) == 2.0
    assert fill_sell(30.0) == 29.4  # 30 - 0.6
    assert fill_buy(9.0) == 9.5


def test_force_then_marked_close_keeps_pnl_unknown(tmp_path: Path) -> None:
    bot = PaperThetaCliff(path=tmp_path / "t.jsonl", lot_size=65)
    bars = _morning_bars()
    stats = morning_session_stats(bars, day="2026-09-29")
    assert stats is not None
    spot = 25000.0
    sigma = remaining_sigma_pts(spot, 12.0, _now("12:00"))
    pe_l, pe_s, ce_s, ce_l = fence_strikes(
        spot, stats["morning_high"], stats["morning_low"], sigma
    )
    book = _quote_book(pe_l, pe_s, ce_s, ce_l)
    feed = {"expiry": date(2026, 9, 29), "spot": spot, "vix_yesterday": 12.0}
    assert bot.on_frame(
        now=_now("12:00"), feed=feed, book=book, bars_1m=bars, option_symbol=_opt
    )
    assert bot.position is not None
    # Force-close CE only (unknown).
    forced = bot._force_close_side(_now("15:25"), "ce", "time")
    assert forced is not None
    assert forced["pnl_known"] is False
    assert bot.position is not None
    assert bot.position.pnl_unknown is True
    # PE closes with quotes — final seal must stay unknown.
    sealed = bot._close_side(_now("15:25"), book, "pe", "time")
    assert sealed is not None
    assert sealed["event"] == "close"
    assert sealed["pnl_known"] is False


def test_skip_event_once_after_entry_window(tmp_path: Path) -> None:
    bot = PaperThetaCliff(path=tmp_path / "t.jsonl")
    bars = _morning_bars()
    feed = {
        "expiry": date(2026, 9, 29),
        "spot": 25000.0,
        "vix_yesterday": 12.0,
        "entry_block": "policy_gate",
    }
    # Inside window: remember reject, do not write skip yet.
    ev = bot.on_frame(
        now=_now("12:00"),
        feed=feed,
        book=_Book({}),
        bars_1m=bars,
        option_symbol=_opt,
        allow_entry=False,
    )
    assert ev is None
    assert bot.last_reject == "policy_gate"
    # After 12:10: one skip with latest reason.
    ev2 = bot.on_frame(
        now=_now("12:11"),
        feed=feed,
        book=_Book({}),
        bars_1m=bars,
        option_symbol=_opt,
        allow_entry=False,
    )
    assert ev2 is not None
    assert ev2["event"] == "skip"
    assert ev2["reason"] == "policy_gate"
    ev3 = bot.on_frame(
        now=_now("12:12"),
        feed=feed,
        book=_Book({}),
        bars_1m=bars,
        option_symbol=_opt,
        allow_entry=False,
    )
    assert ev3 is None
    skips = [
        json.loads(line)
        for line in (tmp_path / "t.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert sum(1 for r in skips if r.get("event") == "skip") == 1


def test_no_quotes_glitch_then_open_has_no_skip(tmp_path: Path) -> None:
    bot = PaperThetaCliff(path=tmp_path / "t.jsonl", lot_size=65)
    bars = _morning_bars()
    stats = morning_session_stats(bars, day="2026-09-29")
    assert stats is not None
    spot = 25000.0
    sigma = remaining_sigma_pts(spot, 12.0, _now("12:00"))
    pe_l, pe_s, ce_s, ce_l = fence_strikes(
        spot, stats["morning_high"], stats["morning_low"], sigma
    )
    book = _quote_book(pe_l, pe_s, ce_s, ce_l)
    feed = {"expiry": date(2026, 9, 29), "spot": spot, "vix_yesterday": 12.0}
    # 12:00:01 no quotes → reject only.
    miss = bot.on_frame(
        now=_now("12:00"),
        feed=feed,
        book=_Book({}),
        bars_1m=bars,
        option_symbol=_opt,
    )
    assert miss is None
    assert bot.last_reject == "no_quotes"
    # 12:00:05 quotes → open; must not have a skip row.
    opened = bot.on_frame(
        now=_now("12:00"),
        feed=feed,
        book=book,
        bars_1m=bars,
        option_symbol=_opt,
    )
    assert opened is not None and opened["event"] == "open"
    rows = [
        json.loads(line)
        for line in (tmp_path / "t.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert not any(r.get("event") == "skip" for r in rows)


def test_skips_non_expiry(tmp_path: Path) -> None:
    bot = PaperThetaCliff(path=tmp_path / "t.jsonl")
    bars = _morning_bars()
    ev = bot.on_frame(
        now=_now("12:00"),
        feed={
            "expiry": date(2026, 10, 6),
            "spot": 25000.0,
            "vix_yesterday": 12.0,
        },
        book=_Book({}),
        bars_1m=bars,
        option_symbol=_opt,
    )
    assert ev is None
    assert bot.last_reject == "not_expiry_day"


def test_rv_skip(tmp_path: Path) -> None:
    bot = PaperThetaCliff(path=tmp_path / "t.jsonl")
    # Violent morning so RV blows through 0.9×VIX
    bars = []
    px = 25000.0
    for mins in range(9 * 60 + 15, 12 * 60):
        hh, mm = divmod(mins, 60)
        step = 40.0 if mins % 2 == 0 else -35.0
        o, c = px, px + step
        bars.append(
            {
                "t": f"2026-09-29 {hh:02d}:{mm:02d}",
                "o": o,
                "h": max(o, c) + 5,
                "l": min(o, c) - 5,
                "c": c,
                "v": 1.0,
            }
        )
        px = c
    ev = bot.on_frame(
        now=_now("12:00"),
        feed={
            "expiry": date(2026, 9, 29),
            "spot": px,
            "vix_yesterday": 8.0,
        },
        book=_Book({}),
        bars_1m=bars,
        option_symbol=_opt,
    )
    assert ev is None
    assert bot.last_reject and bot.last_reject.startswith("rv_filter")
    finalized = bot.on_frame(
        now=_now("12:11"),
        feed={
            "expiry": date(2026, 9, 29),
            "spot": px,
            "vix_yesterday": 8.0,
        },
        book=_Book({}),
        bars_1m=bars,
        option_symbol=_opt,
    )
    assert finalized is not None and finalized["event"] == "skip"
    assert finalized["reason"].startswith("rv_filter")
