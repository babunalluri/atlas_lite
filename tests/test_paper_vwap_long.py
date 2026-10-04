"""Paper Session-VWAP long book — separate from iron fly, no broker."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from atlas_lite.paper_vwap_long import (
    MAX_ENTRIES_PER_DAY,
    MIN_EDGE_BUFFER_PTS,
    PAPER_BAR_MINUTES,
    PaperVwapLong,
    aggregate_bars,
    bucket_floor_ts,
    in_vwap_entry_window,
    scan_vwap_signals,
    spot_order_charges,
    supertrend_series,
)

IST = ZoneInfo("Asia/Kolkata")


def _now(hm: str, day: str = "2026-09-18") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def _bar(t: str, o: float, h: float, l: float, c: float, v: float = 1000.0) -> dict:
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def _pullback_long_bars(day: str = "2026-09-18") -> list[dict]:
    """Session that prints long B (pullback to −0.5σ) then S (through +1σ)."""
    bars: list[dict] = []
    for i in range(20):
        mm = 15 + i
        hour, minute = divmod(mm, 60)
        t = f"{day} {9 + hour:02d}:{minute:02d}"
        c = 25100.0 + (i % 5) * 10.0
        bars.append(_bar(t, c - 5, c + 15, c - 15, c, v=5000.0))
    rows = scan_vwap_signals(bars)
    last = rows[-1]
    vwap = float(last["vwap"])
    pull = float(last["pull_long"])
    up1 = float(last["up1"])
    # Tag −0.5σ (not full −1σ) so milder pullback still fires B.
    bars.append(_bar(f"{day} 09:35", vwap + 5, vwap + 6, pull - 1, vwap + 1, v=100.0))
    bars.append(_bar(f"{day} 09:36", vwap + 1, up1 + 5, vwap, up1 + 2, v=100.0))
    return bars


def _bot(path: Path, **kwargs) -> PaperVwapLong:
    # Geometry tests stay on 1m bars; production default is PAPER_BAR_MINUTES (5).
    kwargs.setdefault("require_st", False)
    kwargs.setdefault("bar_minutes", 1)
    kwargs.setdefault("require_edge", False)
    return PaperVwapLong(path=path, **kwargs)


def test_entry_window() -> None:
    assert in_vwap_entry_window(_now("09:14")) is False
    assert in_vwap_entry_window(_now("09:15")) is False
    assert in_vwap_entry_window(_now("09:29")) is False
    assert in_vwap_entry_window(_now("09:30")) is True
    assert in_vwap_entry_window(_now("13:59")) is True
    assert in_vwap_entry_window(_now("14:00")) is False
    assert in_vwap_entry_window(_now("15:14")) is False
    assert in_vwap_entry_window(_now("15:15")) is False


def test_scan_long_entry_then_exit() -> None:
    rows = scan_vwap_signals(_pullback_long_bars())
    entries = [r for r in rows if r.get("long_entry")]
    exits = [r for r in rows if r.get("long_exit")]
    assert entries, "expected long B signal"
    assert exits, "expected S exit after long"
    assert entries[0]["t"] < exits[0]["t"]


def test_charges_buy_and_sell() -> None:
    buy = spot_order_charges(25000.0, 1, "buy")
    sell = spot_order_charges(25050.0, 1, "sell")
    # Zerodha: ₹20 or 0.03% — at qty=1 the rate wins (~₹7.5).
    assert buy["brokerage"] == min(20.0, round(25000.0 * 0.0003, 2))
    assert buy["stt"] == 0.0
    assert buy["stamp"] > 0
    assert sell["stt"] > 0
    assert sell["total"] > buy["brokerage"]


def test_opens_on_b_and_closes_on_s(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl", qty=1)
    bars = _pullback_long_bars()
    opened = None
    closed = None
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        ev = bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
        if ev and ev.get("event") == "open":
            opened = ev
        if ev and ev.get("event") == "close":
            closed = ev
    assert opened is not None
    assert opened["qty"] == 1
    assert closed is not None
    assert closed["reason"] in ("signal_s", "stop")
    assert "pnl" in closed
    assert bot.position is None
    ledger = (tmp_path / "vwap.jsonl").read_text(encoding="utf-8").strip().splitlines()
    kinds = [json.loads(line)["event"] for line in ledger]
    assert "open" in kinds and "close" in kinds


def test_no_short_entries(tmp_path: Path) -> None:
    """Short S marks from scan must not open a paper short."""
    bot = _bot(tmp_path / "vwap.jsonl")
    day = "2026-09-18"
    bars: list[dict] = []
    base = 25000.0
    for i in range(12):
        mm = 15 + i
        c = base - i * 8
        bars.append(_bar(f"{day} 09:{mm:02d}", c + 2, c + 3, c - 3, c, v=2000))
    # Push into short B/S geometry (high tags +1σ while below VWAP).
    last_c = bars[-1]["c"]
    bars.append(_bar(f"{day} 09:27", last_c, last_c + 40, last_c - 1, last_c + 5, v=2500))
    rows = scan_vwap_signals(bars)
    assert any(r.get("signal") == "S" and not r.get("long_exit") for r in rows) or any(
        r.get("bias") == "short" for r in rows
    )
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
    assert bot.position is None
    assert bot.entries_today == 0


def test_square_off_at_1515(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    day = "2026-09-18"
    bars = _pullback_long_bars(day)
    # Force open via first B bar.
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
        if bot.position is not None:
            break
    assert bot.position is not None
    closed = bot.on_bars(
        now=_now("15:15", day),
        bars=bars + [_bar(f"{day} 15:15", 25020, 25025, 25015, 25020)],
        spot=25020.0,
    )
    assert closed is not None
    assert closed["event"] == "close"
    assert closed["reason"] == "time"
    assert bot.position is None


def test_net_pnl_subtracts_charges(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    day = "2026-09-18"
    bars = _pullback_long_bars(day)
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        ev = bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
        if ev and ev.get("event") == "close":
            gross = float(ev["pnl_gross"])
            charges = float(ev["charges"])
            assert ev["pnl"] == round(gross - charges, 2)
            assert charges > 0
            return
    raise AssertionError("expected close event")


def test_short_entry_is_not_long_exit() -> None:
    day = "2026-09-18"
    bars: list[dict] = []
    base = 25000.0
    for i in range(20):
        mm = 15 + i
        hour, minute = divmod(mm, 60)
        t = f"{day} {9 + hour:02d}:{minute:02d}"
        c = base - (i % 5) * 10.0
        bars.append(_bar(t, c + 5, c + 15, c - 15, c, v=5000.0))
    rows = scan_vwap_signals(bars)
    last = rows[-1]
    vwap = float(last["vwap"])
    up1 = float(last["up1"])
    bars.append(_bar(f"{day} 09:35", vwap - 5, up1 + 5, vwap - 6, vwap - 1, v=100.0))
    rows2 = scan_vwap_signals(bars)
    short_entries = [
        r for r in rows2 if r.get("signal") == "S" and not r.get("exit") and not r.get("stop")
    ]
    assert short_entries, "expected short entry mark"
    assert all(not r.get("long_exit") for r in short_entries)


def test_no_exit_on_closed_entry_bar_high(tmp_path: Path) -> None:
    """A +1σ wick printed *before* the fill must not take once the bar closes."""
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    rows = scan_vwap_signals(prefix)
    entry = dict(prefix[-1])
    entry["h"] = float(rows[-1]["up1"]) + 50.0
    prefix = prefix[:-1] + [entry]
    opened = bot.on_bars(now=_now("09:35"), bars=prefix, spot=float(entry["c"]))
    assert opened and opened["event"] == "open"
    held = bot.on_bars(now=_now("09:36"), bars=prefix, spot=float(entry["c"]))
    assert held is None
    assert bot.position is not None
    # Next bar can still take through +1σ.
    full = prefix + [bars[entry_i + 1]]
    now2 = datetime.strptime(full[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    closed = bot.on_bars(now=now2, bars=full, spot=float(full[-1]["c"]))
    assert closed is not None
    assert closed["event"] == "close"
    assert closed["reason"] in ("signal_s", "stop")
    assert bot.position is None


def test_forming_take_on_entry_bucket_banks_up1(tmp_path: Path) -> None:
    """Live +1σ inside the entry bucket must bank (forming path)."""
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    opened = bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert opened and opened["event"] == "open"
    rows = scan_vwap_signals(prefix)
    up1 = float(rows[-1]["up1"])
    assert up1 > float(opened["entry"])
    closed = bot.on_bars(now=now, bars=prefix, spot=up1 + 5.0)
    assert closed is not None
    assert closed["reason"] == "signal_s"
    assert closed["exit"] == round(up1 + 5.0, 4)
    assert bot.position is None


def test_prior_session_open_flattens_next_day(tmp_path: Path) -> None:
    path = tmp_path / "vwap.jsonl"
    bot = _bot(path)
    bars = _pullback_long_bars("2026-09-17")
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
        if bot.position is not None:
            break
    assert bot.position is not None
    assert bot.position.day == "2026-09-17"
    bot2 = _bot(path)
    assert bot2.position is not None
    # No prior-day bars in the feed → history-gap path books onto calendar day.
    closed = bot2.on_bars(
        now=_now("09:20", "2026-09-18"),
        bars=[_bar("2026-09-18 09:20", 25000, 25010, 24990, 25000)],
        spot=25000.0,
    )
    assert closed is not None
    assert closed["reason"] == "session"
    assert closed["day"] == "2026-09-18"
    assert closed["exit"] == 25000.0
    assert bot2.position is None
    assert bot2.traded_day == "2026-09-18"


def test_gap_flatten_seals_prior_day_pnl_while_open(tmp_path: Path) -> None:
    """History-gap path must seal Day-D day_pnl before rolling (allow_open)."""
    path = tmp_path / "vwap.jsonl"
    bot = _bot(path)
    bars = _pullback_long_bars("2026-09-17")
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
        if bot.position is not None:
            break
    assert bot.position is not None
    bot.day_pnl = 72.1
    bot.entries_today = 2
    bot.eod_written = False
    closed = bot.on_bars(
        now=_now("09:20", "2026-09-18"),
        bars=[_bar("2026-09-18 09:20", 25000, 25010, 24990, 25000)],
        spot=25000.0,
    )
    assert closed is not None
    assert closed["day"] == "2026-09-18"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    seals = [
        ev
        for ev in lines
        if ev.get("event") == "day_pnl" and ev.get("day") == "2026-09-17"
    ]
    assert len(seals) == 1
    assert seals[0]["day_pnl"] == 72.1


def test_idle_eod_skips_zero_trade_row(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "vwap.jsonl"
    day = "2026-09-18"  # Thursday
    monkeypatch.setattr(
        "atlas_lite.paper_vwap_long.ist_now",
        lambda: f"{day} 15:20:00.000",
    )
    bot = _bot(path)
    bot.traded_day = day
    bot.entries_today = 0
    bot.day_pnl = 0.0
    first = bot.on_bars(now=_now("15:15", day), bars=[], spot=25000.0)
    assert first is None
    assert bot.eod_written is True
    second = bot.on_bars(now=_now("15:16", day), bars=[], spot=25000.0)
    assert second is None
    assert (not path.exists()) or path.read_text(encoding="utf-8").strip() == ""


def test_eod_after_trades_not_duplicated(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "vwap.jsonl"
    day = "2026-09-18"
    monkeypatch.setattr(
        "atlas_lite.paper_vwap_long.ist_now",
        lambda: f"{day} 15:20:00.000",
    )
    bot = _bot(path)
    bars = _pullback_long_bars(day)
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
    assert bot.entries_today >= 1
    first = bot.on_bars(now=_now("15:15", day), bars=bars, spot=25020.0)
    assert first is not None
    # May be close-at-time or day_pnl depending on whether still open.
    bot2 = _bot(path)
    assert bot2.eod_written is True or bot2.position is None
    # Force another square-off tick — must not add a second day_pnl.
    bot2.on_bars(now=_now("15:16", day), bars=bars, spot=25020.0)
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert sum(1 for ev in lines if ev.get("event") == "day_pnl") == 1


def test_restore_keeps_zero_day_pnl(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "vwap.jsonl"
    day = "2026-09-18"
    monkeypatch.setattr(
        "atlas_lite.paper_vwap_long.ist_now",
        lambda: f"{day} 14:00:00.000",
    )
    path.write_text(
        json.dumps(
            {
                "event": "close",
                "strategy": "nifty_vwap_long",
                "day": day,
                "pnl": -40.0,
                "day_pnl": 0.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bot = _bot(path)
    assert bot.day_pnl == 0.0


def test_snapshot_shows_position_without_spot(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
        if bot.position is not None:
            break
    assert bot.position is not None
    snap = bot.snapshot(spot=None)
    assert snap["position"] is not None
    assert snap["spot_missing"] is True
    assert snap["open_pnl"] == 0.0


def test_take_profit_is_plus_one_sigma_not_vwap(tmp_path: Path) -> None:
    """Touching VWAP alone must not flatten; +1σ is the take."""
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = scan_vwap_signals(prefix)
    vwap = float(rows[-1]["vwap"])
    up1 = float(rows[-1]["up1"])
    assert up1 > vwap + 0.5
    # Next minute: high clears VWAP but stays below +1σ → stay long.
    mid = dict(prefix[-1])
    mid["t"] = "2026-09-18 09:36"
    mid["o"] = vwap
    mid["h"] = vwap + 0.25
    mid["l"] = vwap - 0.25
    mid["c"] = vwap + 0.1
    held = bot.on_bars(now=_now("09:36"), bars=prefix + [mid], spot=float(mid["c"]))
    assert held is None
    assert bot.position is not None
    # Then tag +1σ on the forming minute — fill at max(spot, up1) (no free upgrade).
    win = dict(mid)
    win["h"] = up1 + 1.0
    win["c"] = up1 + 0.5
    closed = bot.on_bars(now=_now("09:36"), bars=prefix + [win], spot=float(win["c"]))
    assert closed and closed["event"] == "close"
    assert closed["reason"] == "signal_s"
    refreshed = dict(win)
    refreshed["c"] = float(win["c"])
    refreshed["h"] = max(float(win["h"]), float(win["c"]))
    refreshed["l"] = min(float(win["l"]), float(win["c"]))
    fill_up1 = float(scan_vwap_signals(prefix + [refreshed])[-1]["up1"])
    assert closed["exit"] == round(max(float(win["c"]), fill_up1), 4)


def test_stop_fills_at_minus_one_sigma_not_spot(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = scan_vwap_signals(prefix)
    dn1 = float(rows[-1]["dn1"])
    stop_bar = {
        "t": "2026-09-18 09:36",
        "o": float(prefix[-1]["c"]),
        "h": float(prefix[-1]["c"]),
        "l": dn1 - 5.0,
        "c": dn1 + 10.0,  # snaps back — spot would be optimistic vs stop
        "v": 100.0,
    }
    # Closed-bar path (clock past the minute) uses H/L vs bands, not live spot.
    closed = bot.on_bars(
        now=_now("09:37"), bars=prefix + [stop_bar], spot=None
    )
    assert closed and closed["reason"] == "stop"
    fill_dn1 = float(scan_vwap_signals(prefix + [stop_bar])[-1]["dn1"])
    assert closed["exit"] == round(fill_dn1, 4)
    assert closed["exit"] != round(float(stop_bar["c"]), 4)


def test_weekend_flattens_open_book(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars("2026-09-18")  # Thursday
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
        if bot.position is not None:
            break
    assert bot.position is not None
    # Saturday — weekend path must flatten (not early-return).
    closed = bot.on_bars(
        now=_now("11:00", "2026-09-20"),
        bars=bars,
        spot=25000.0,
    )
    assert closed is not None
    assert closed["reason"] == "session"
    assert closed["day"] == "2026-09-18"
    assert bot.position is None


def test_no_signals_while_sigma_collapsed() -> None:
    day = "2026-09-18"
    # Identical bars → σ≈0 even with volume.
    bars = [_bar(f"{day} 09:{15 + i:02d}", 25000, 25000, 25000, 25000, v=1000) for i in range(8)]
    rows = scan_vwap_signals(bars)
    assert all(not r.get("bands_ready") for r in rows)
    assert all(r.get("signal") is None for r in rows)


def test_square_off_uses_last_close_when_spot_missing(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
        if bot.position is not None:
            break
    assert bot.position is not None
    closed = bot.on_bars(
        now=_now("15:15"),
        bars=bars + [_bar("2026-09-18 15:14", 25010, 25012, 25008, 25011)],
        spot=None,
    )
    assert closed is not None
    assert closed["event"] == "close"
    assert closed["reason"] == "time"
    assert closed["exit"] == 25011.0


def test_half_sigma_pullback_fires_without_full_minus_one() -> None:
    """low tags −0.5σ but stays above −1σ → still long_entry."""
    day = "2026-09-18"
    bars: list[dict] = []
    for i in range(20):
        mm = 15 + i
        hour, minute = divmod(mm, 60)
        t = f"{day} {9 + hour:02d}:{minute:02d}"
        c = 25100.0 + (i % 5) * 10.0
        bars.append(_bar(t, c - 5, c + 15, c - 15, c, v=5000.0))
    rows = scan_vwap_signals(bars)
    last = rows[-1]
    vwap = float(last["vwap"])
    pull = float(last["pull_long"])
    dn1 = float(last["dn1"])
    assert pull > dn1
    # Tag pull_long but stay clearly above dn1.
    mid = (pull + dn1) / 2.0
    assert mid < pull and mid > dn1
    bars.append(_bar(f"{day} 09:35", vwap + 5, vwap + 6, mid, vwap + 1, v=100.0))
    rows2 = scan_vwap_signals(bars)
    assert any(r.get("long_setup") for r in rows2)
    assert any(r.get("long_entry") for r in rows2)


def test_st_reject_does_not_block_later_setup(tmp_path: Path, monkeypatch) -> None:
    """ST-filtered B must not leave scan phantom long_open that skips later entries."""
    bot = _bot(tmp_path / "vwap.jsonl", require_st=True)
    day = "2026-09-18"
    bars: list[dict] = []
    for i in range(20):
        mm = 15 + i
        hour, minute = divmod(mm, 60)
        t = f"{day} {9 + hour:02d}:{minute:02d}"
        c = 25100.0 + (i % 5) * 10.0
        bars.append(_bar(t, c - 5, c + 15, c - 15, c, v=5000.0))
    rows = scan_vwap_signals(bars)
    last = rows[-1]
    vwap = float(last["vwap"])
    pull = float(last["pull_long"])
    bars.append(_bar(f"{day} 09:35", vwap + 5, vwap + 6, pull - 1, vwap + 1, v=100.0))

    st_dir = {"v": -1}
    monkeypatch.setattr(
        PaperVwapLong, "_st_dir_for_bar", lambda self, **_kw: st_dir["v"]
    )
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
    assert bot.position is None

    # Later pullback with ST now up — must still be able to open (geometry, not scan long_entry).
    st_dir["v"] = 1
    rows2 = scan_vwap_signals(bars)
    vwap2 = float(rows2[-1]["vwap"])
    pull2 = float(rows2[-1]["pull_long"])
    bars.append(_bar(f"{day} 09:40", vwap2 + 5, vwap2 + 6, pull2 - 1, vwap2 + 1, v=100.0))
    opened = bot.on_bars(
        now=_now("09:40", day),
        bars=bars,
        spot=float(bars[-1]["c"]),
    )
    assert opened is not None
    assert opened["event"] == "open"


def test_st_gate_blocks_when_downtrend(tmp_path: Path, monkeypatch) -> None:
    bot = _bot(tmp_path / "vwap.jsonl", require_st=True)
    bars = _pullback_long_bars()
    monkeypatch.setattr(PaperVwapLong, "_st_dir_for_bar", lambda self, **_kw: -1)
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
    assert bot.position is None
    assert bot.entries_today == 0


def test_st_gate_allows_when_uptrend(tmp_path: Path, monkeypatch) -> None:
    bot = _bot(tmp_path / "vwap.jsonl", require_st=True)
    bars = _pullback_long_bars()
    monkeypatch.setattr(PaperVwapLong, "_st_dir_for_bar", lambda self, **_kw: 1)
    opened = None
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        ev = bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
        if ev and ev.get("event") == "open":
            opened = ev
            break
    assert opened is not None
    assert opened.get("st_required") is True
    assert opened.get("pull_long") is not None


def test_supertrend_series_warms_up() -> None:
    day = "2026-09-18"
    bars = [
        _bar(f"{day} 09:{15 + i:02d}", 25000 + i, 25010 + i, 24990 + i, 25005 + i, v=1000)
        for i in range(15)
    ]
    rows = supertrend_series(bars)
    assert any(r.get("ready") for r in rows)
    dirs = [int(r["dir"]) for r in rows if r.get("ready") and r.get("dir") is not None]
    assert dirs
    assert all(d in (1, -1) for d in dirs)


def test_no_same_bar_reentry_after_exit(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    # Open on entry bar.
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    assert bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))["event"] == "open"
    # Exit on next bar through +1σ.
    full = bars[: entry_i + 2]
    now2 = datetime.strptime(full[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    closed = bot.on_bars(now=now2, bars=full, spot=float(full[-1]["c"]))
    assert closed and closed["event"] == "close"
    exit_t = closed.get("signal_t")
    assert exit_t
    # Same minute still looks like long_setup after flat — must not re-buy.
    again = bot.on_bars(now=now2, bars=full, spot=float(full[-1]["c"]))
    assert again is None
    assert bot.position is None
    assert bot._last_exit_t == exit_t


def test_no_duplicate_day_pnl_on_monday_restart(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "vwap.jsonl"
    fri = "2026-09-18"
    # Friday sealed ledger.
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "close",
                        "strategy": "nifty_vwap_long",
                        "day": fri,
                        "pnl": 10.0,
                        "day_pnl": 10.0,
                    }
                ),
                json.dumps(
                    {
                        "event": "day_pnl",
                        "strategy": "nifty_vwap_long",
                        "day": fri,
                        "day_pnl": 10.0,
                        "trades": 1,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "atlas_lite.paper_vwap_long.ist_now",
        lambda: "2026-09-21 09:20:00.000",  # Monday
    )
    for _ in range(3):
        bot = _bot(path)
        assert bot.eod_written is True
        bot.on_bars(
            now=_now("09:20", "2026-09-21"),
            bars=[_bar("2026-09-21 09:20", 25000, 25010, 24990, 25000)],
            spot=25000.0,
        )
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert sum(1 for ev in lines if ev.get("event") == "day_pnl") == 1


def test_stop_honored_without_spot(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = scan_vwap_signals(prefix)
    dn1 = float(rows[-1]["dn1"])
    stop_bar = _bar("2026-09-18 09:36", dn1 + 5, dn1 + 5, dn1 - 5, dn1 + 2, v=100)
    # Evaluate as a *closed* bar (clock past 09:36) so H/L stop works without spot.
    closed = bot.on_bars(now=_now("09:37"), bars=prefix + [stop_bar], spot=None)
    assert closed is not None
    assert closed["reason"] == "stop"


def test_stop_catches_gap_bar_not_only_latest(tmp_path: Path) -> None:
    """A stop wick on an intermediate bar must fire even if the last bar recovered."""
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = scan_vwap_signals(prefix)
    dn1 = float(rows[-1]["dn1"])
    vwap = float(rows[-1]["vwap"])
    stop_bar = _bar("2026-09-18 09:36", vwap, vwap + 1, dn1 - 5, vwap, v=100)
    recover = _bar("2026-09-18 09:37", vwap, vwap + 2, vwap - 1, vwap + 1, v=100)
    # Simulate gap: jump straight to recovered last bar (stop wick only in history).
    closed = bot.on_bars(
        now=_now("09:37"), bars=prefix + [stop_bar, recover], spot=float(recover["c"])
    )
    assert closed is not None
    assert closed["reason"] == "stop"
    assert closed["signal_t"] == "2026-09-18 09:36"


def test_take_skipped_when_up1_below_entry(tmp_path: Path, monkeypatch) -> None:
    """+1σ pierce is not a take if VWAP has fallen so up1 sits under entry."""
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    entry = float(bot.position.entry)
    fake_up1 = entry - 20.0
    fake_dn1 = entry - 80.0
    loser = _bar(
        "2026-09-18 09:36",
        entry - 5,
        fake_up1 + 2,
        entry - 10,
        entry - 5,
        v=100,
    )

    def _fake_scan(session):
        rows = scan_vwap_signals(session)
        if not rows:
            return rows
        last = dict(rows[-1])
        last["up1"] = fake_up1
        last["dn1"] = fake_dn1
        last["vwap"] = fake_up1 - 10.0
        return rows[:-1] + [last]

    monkeypatch.setattr("atlas_lite.paper_vwap_long.scan_vwap_signals", _fake_scan)
    held = bot.on_bars(now=_now("09:37"), bars=prefix + [loser], spot=None)
    assert held is None
    assert bot.position is not None


def test_close_append_failure_keeps_position(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    day_pnl_before = bot.day_pnl

    def _boom(_event):
        raise OSError("disk full")

    bot._append = _boom  # type: ignore[method-assign]
    rows = scan_vwap_signals(prefix)
    up1 = float(rows[-1]["up1"])
    win = _bar("2026-09-18 09:36", up1, up1 + 5, up1 - 1, up1 + 2, v=100)
    try:
        bot.on_bars(now=_now("09:37"), bars=prefix + [win], spot=None)
        raise AssertionError("expected OSError")
    except OSError:
        pass
    assert bot.position is not None
    assert bot.day_pnl == day_pnl_before


def test_forming_take_uses_spot_not_stale_high(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = scan_vwap_signals(prefix)
    up1 = float(rows[-1]["up1"])
    # Forming bar wick tagged up1 earlier; live spot has snapped back below.
    forming = _bar("2026-09-18 09:36", up1 - 5, up1 + 5, up1 - 10, up1 - 3, v=100)
    held = bot.on_bars(
        now=_now("09:36"), bars=prefix + [forming], spot=up1 - 3.0
    )
    assert held is None
    assert bot.position is not None


def test_spot_does_not_contaminate_prior_minute(tmp_path: Path) -> None:
    """Wall clock on T must not paint spot into a cached T-1 last bar."""
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = scan_vwap_signals(prefix)
    dn1 = float(rows[-1]["dn1"])
    prior = _bar("2026-09-18 09:36", dn1 + 20, dn1 + 25, dn1 + 15, dn1 + 20, v=100)
    # Clock is 09:37 but series still ends at 09:36 — must not treat as forming via spot.
    held = bot.on_bars(
        now=_now("09:37"),
        bars=prefix + [prior],
        spot=dn1 - 5.0,  # would false-stop if merged into prior as forming H/L
    )
    assert held is None
    assert bot.position is not None


def test_no_entry_when_spot_at_or_above_up1(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[:entry_i]  # warm bars only
    rows = scan_vwap_signals(prefix)
    last = rows[-1]
    vwap = float(last["vwap"])
    pull = float(last["pull_long"])
    up1 = float(last["up1"])
    setup = _bar("2026-09-18 09:35", vwap + 5, up1 + 10, pull - 1, vwap + 1, v=100)
    # Live fill would be above +1σ — refuse so the book keeps a real take target.
    ev = bot.on_bars(
        now=_now("09:35"), bars=prefix + [setup], spot=up1 + 2.0
    )
    assert ev is None
    assert bot.position is None


def test_close_append_failure_does_not_arm_exit_guard(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None

    def _boom(_event):
        raise OSError("disk full")

    bot._append = _boom  # type: ignore[method-assign]
    rows = scan_vwap_signals(prefix)
    up1 = float(rows[-1]["up1"])
    win = _bar("2026-09-18 09:36", up1, up1 + 5, up1 - 1, up1 + 2, v=100)
    try:
        bot.on_bars(now=_now("09:37"), bars=prefix + [win], spot=None)
        raise AssertionError("expected OSError")
    except OSError:
        pass
    assert bot.position is not None
    assert bot._last_exit_t is None


def test_paper_default_bar_minutes_is_five() -> None:
    assert PAPER_BAR_MINUTES == 5


def test_aggregate_bars_five_minute_buckets() -> None:
    day = "2026-09-18"
    bars = [
        _bar(f"{day} 09:15", 100, 101, 99, 100.5, v=10),
        _bar(f"{day} 09:16", 100.5, 102, 100, 101, v=10),
        _bar(f"{day} 09:17", 101, 103, 100.5, 102, v=10),
        _bar(f"{day} 09:18", 102, 102.5, 101, 101.5, v=10),
        _bar(f"{day} 09:19", 101.5, 102, 101, 101.8, v=10),
        _bar(f"{day} 09:20", 101.8, 104, 101, 103, v=10),
    ]
    out = aggregate_bars(bars, 5)
    assert [b["t"] for b in out] == [f"{day} 09:15", f"{day} 09:20"]
    assert out[0]["o"] == 100
    assert out[0]["h"] == 103
    assert out[0]["l"] == 99
    assert out[0]["c"] == 101.8
    assert out[0]["v"] == 50
    assert bucket_floor_ts(_now("09:17"), 5) == f"{day} 09:15"
    assert bucket_floor_ts(_now("09:20"), 5) == f"{day} 09:20"


def test_paper_opens_on_aggregated_five_minute_bars(tmp_path: Path) -> None:
    """Production TF: 1m feed rolled to 5m before scan/open."""
    bot = _bot(tmp_path / "vwap.jsonl", bar_minutes=5)
    day = "2026-09-18"
    bars: list[dict] = []
    # Five 5m buckets of noise so σ is ready.
    for bi in range(5):
        base_mm = 15 + bi * 5
        for j in range(5):
            mm = base_mm + j
            hour, minute = divmod(mm, 60)
            t = f"{day} {9 + hour:02d}:{minute:02d}"
            c = 25100.0 + bi * 8 + (j % 3) * 4
            bars.append(_bar(t, c - 3, c + 8, c - 8, c, v=2000))
    # Next 5m bucket: pullback geometry on the rolled bar.
    warm = aggregate_bars(bars, 5)
    last = scan_vwap_signals(warm)[-1]
    vwap = float(last["vwap"])
    pull = float(last["pull_long"])
    up1 = float(last["up1"])
    assert up1 > vwap
    # 09:40–09:44 → bucket 09:40
    for j, (o, h, l, c) in enumerate(
        [
            (vwap + 4, vwap + 5, vwap + 2, vwap + 3),
            (vwap + 3, vwap + 4, pull - 2, vwap + 1),
            (vwap + 1, vwap + 2, vwap, vwap + 1.5),
            (vwap + 1.5, vwap + 2, vwap + 0.5, vwap + 1),
            (vwap + 1, vwap + 1.5, vwap + 0.5, vwap + 1.2),
        ]
    ):
        bars.append(_bar(f"{day} 09:{40 + j:02d}", o, h, l, c, v=100))
    opened = None
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        # Spot below up1 so entry guard passes.
        spot = min(float(b["c"]), up1 - 1.0)
        ev = bot.on_bars(now=now, bars=bars[: i + 1], spot=spot)
        if ev and ev.get("event") == "open":
            opened = ev
            break
    assert opened is not None
    assert opened["signal_t"] == f"{day} 09:40"
    assert bot.snapshot(spot=None)["bar_minutes"] == 5


def test_shipped_defaults_open_with_st_and_edge(tmp_path: Path, monkeypatch) -> None:
    """Production gates together: 5m + Supertrend + +1σ headroom (not _bot overrides)."""
    path = tmp_path / "vwap.jsonl"
    bot = PaperVwapLong(path=path)
    assert bot.bar_minutes == PAPER_BAR_MINUTES == 5
    assert bot.require_st is True
    assert bot.require_edge is True
    monkeypatch.setattr(
        "atlas_lite.paper_vwap_long.supertrend_series",
        lambda bars, **_kw: [
            {"t": str(b["t"]), "dir": 1, "up": None, "dn": 1.0, "ready": True}
            for b in bars
        ],
    )
    day = "2026-09-18"
    amp = 80.0
    bars: list[dict] = []
    for bi in range(8):
        base_mm = 15 + bi * 5
        for j in range(5):
            mm = base_mm + j
            hour, minute = divmod(mm, 60)
            t = f"{day} {9 + hour:02d}:{minute:02d}"
            c = 25000.0 + bi * amp + ((j % 2) * 2 - 1) * amp * 0.3
            bars.append(
                _bar(t, c - amp * 0.4, c + amp * 0.5, c - amp * 0.5, c, v=8000)
            )
    warm = aggregate_bars(bars, 5)
    last = scan_vwap_signals(warm)[-1]
    vwap = float(last["vwap"])
    pull = float(last["pull_long"])
    up1 = float(last["up1"])
    need = MIN_EDGE_BUFFER_PTS + 1.0
    assert (up1 - vwap) > need + 5.0
    # Next 5m bucket: tag −0.5σ pullback while close stays above VWAP.
    entry_mm = 15 + 8 * 5  # 09:55
    for j in range(5):
        mm = entry_mm + j
        hour, minute = divmod(mm, 60)
        t = f"{day} {9 + hour:02d}:{minute:02d}"
        if j == 1:
            bars.append(
                _bar(t, vwap + 5, vwap + 10, pull - 2, vwap + 8, v=8000)
            )
        else:
            bars.append(
                _bar(t, vwap + 8, vwap + 12, vwap + 4, vwap + 9, v=8000)
            )
    opened = None
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        # Paint live spot with charge headroom but keep long bias vs VWAP.
        spot = min(float(b["c"]), up1 - need)
        spot = max(spot, vwap + 2.0)
        ev = bot.on_bars(now=now, bars=bars[: i + 1], spot=spot)
        if ev and ev.get("event") == "open":
            opened = ev
            break
    assert opened is not None
    assert opened["event"] == "open"
    assert opened["signal_t"].endswith("09:55")


def test_forming_stop_fills_at_worse_of_spot_and_dn1(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = scan_vwap_signals(prefix)
    dn1 = float(rows[-1]["dn1"])
    gap_spot = dn1 - 15.0
    forming = _bar("2026-09-18 09:36", dn1 + 5, dn1 + 5, gap_spot, gap_spot, v=100)
    closed = bot.on_bars(
        now=_now("09:36"), bars=prefix + [forming], spot=gap_spot
    )
    assert closed and closed["reason"] == "stop"
    assert closed["exit"] == round(gap_spot, 4)


def _pre_fill_wick_prefix() -> list[dict]:
    """Session up to an entry bar whose pullback wick already pierced −1σ."""
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    rows = scan_vwap_signals(prefix)
    entry = dict(prefix[-1])
    entry["l"] = float(rows[-1]["dn1"]) - 20.0
    return prefix[:-1] + [entry]


def test_closed_entry_bar_ignores_pre_fill_wick_stop(tmp_path: Path) -> None:
    """Bucket low before the fill must not stop once the entry bar is closed."""
    bot = _bot(tmp_path / "vwap.jsonl")
    prefix = _pre_fill_wick_prefix()
    bot.on_bars(now=_now("09:35"), bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    assert bot.position.fill_bar_l == float(prefix[-1]["l"])
    # Bucket closes with the same low — nothing printed below the fill-time low.
    held = bot.on_bars(now=_now("09:36"), bars=prefix, spot=float(prefix[-1]["c"]))
    assert held is None
    assert bot.position is not None


def test_closed_entry_bar_takes_on_new_post_fill_high(tmp_path: Path) -> None:
    """A high above the fill-time high that reaches +1σ is a post-fill take."""
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    bot.on_bars(now=_now("09:35"), bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = scan_vwap_signals(prefix)
    later = dict(prefix[-1])
    later["h"] = float(rows[-1]["up1"]) + 5.0
    closed = bot.on_bars(
        now=_now("09:36"), bars=prefix[:-1] + [later], spot=float(later["c"])
    )
    assert closed is not None
    assert closed["reason"] == "signal_s"
    assert bot.position is None


def test_restart_keeps_pre_fill_wick_rule(tmp_path: Path) -> None:
    """Fill extremes persist on the open row — no phantom stop after a restart."""
    path = tmp_path / "vwap.jsonl"
    prefix = _pre_fill_wick_prefix()
    bot = _bot(path)
    bot.on_bars(now=_now("09:35"), bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert rows[-1]["event"] == "open"
    assert rows[-1]["fill_bar_l"] == bot.position.fill_bar_l

    bot2 = _bot(path)
    assert bot2.position is not None
    assert bot2.position.fill_bar_l == bot.position.fill_bar_l
    held = bot2.on_bars(now=_now("09:36"), bars=prefix, spot=float(prefix[-1]["c"]))
    assert held is None
    assert bot2.position is not None

    # A genuinely new post-fill low through −1σ still stops after the restart.
    entry = dict(prefix[-1])
    entry["l"] = float(prefix[-1]["l"]) - 30.0
    closed = bot2.on_bars(
        now=_now("09:36"), bars=prefix[:-1] + [entry], spot=float(entry["c"])
    )
    assert closed is not None
    assert closed["reason"] == "stop"
    assert bot2.position is None


def test_entry_bucket_stop_without_spot_uses_bar_low(tmp_path: Path) -> None:
    """LTP outage on the entry bucket must still stop from printed bar low."""
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = scan_vwap_signals(prefix)
    dn1 = float(rows[-1]["dn1"])
    entry = dict(prefix[-1])
    entry["l"] = dn1 - 25.0
    entry["c"] = float(entry["c"])
    closed = bot.on_bars(
        now=now,
        bars=prefix[:-1] + [entry],
        spot=None,
    )
    assert closed is not None
    assert closed["reason"] == "stop"
    rows_after = scan_vwap_signals(prefix[:-1] + [entry])
    assert closed["exit"] == round(float(rows_after[-1]["dn1"]), 4)
    assert bot.position is None


def test_forming_stop_uses_current_spot_not_stale_low(tmp_path: Path) -> None:
    """A prior dip above −1σ must not stop later when spot is still above bands."""
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    now = datetime.strptime(prefix[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    bot.on_bars(now=now, bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = scan_vwap_signals(prefix)
    dn1 = float(rows[-1]["dn1"])
    up1 = float(rows[-1]["up1"])
    entry = float(bot.position.entry)
    dip = dn1 + 5.0
    bot.on_bars(
        now=_now("09:36"),
        bars=prefix
        + [_bar("2026-09-18 09:36", dip, dip + 2, dip, dip + 1, v=100)],
        spot=dip,
    )
    assert bot.position is not None
    # Stay between stop and take so only a phantom stale-min would have fired.
    later_c = min(entry + 5.0, up1 - 5.0)
    assert later_c > dn1
    held = bot.on_bars(
        now=_now("09:37"),
        bars=prefix
        + [
            _bar("2026-09-18 09:36", dip, dip + 2, dip, dip + 1, v=100),
            _bar(
                "2026-09-18 09:37",
                later_c,
                later_c + 1,
                later_c - 1,
                later_c,
                v=100,
            ),
        ],
        spot=later_c,
    )
    assert held is None
    assert bot.position is not None


def test_vwap_is_equal_weight_even_with_fut_volume() -> None:
    day = "2026-09-18"
    bars = [
        _bar(f"{day} 09:{15 + i:02d}", 25000 + i, 25010 + i, 24990 + i, 25005 + i, v=5000)
        for i in range(8)
    ]
    # Heavy volume on a far print must not yank session VWAP (equal-weight only).
    bars.append(_bar(f"{day} 09:23", 26000, 26010, 25990, 26000, v=1_000_000))
    rows = scan_vwap_signals(bars)
    last = rows[-1]
    vwap = float(last["vwap"])
    assert vwap < 25250
    assert vwap > 24950


def test_prior_session_flatten_prefers_prior_close(tmp_path: Path) -> None:
    path = tmp_path / "vwap.jsonl"
    bot = _bot(path)
    bars = _pullback_long_bars("2026-09-17")
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
        if bot.position is not None:
            break
    assert bot.position is not None
    open_day_bars = [b for b in bars if b["t"].startswith("2026-09-17")]
    prior_close = float(open_day_bars[-1]["c"])
    bot2 = _bot(path)
    closed = bot2.on_bars(
        now=_now("09:20", "2026-09-18"),
        bars=open_day_bars + [_bar("2026-09-18 09:20", 99999, 99999, 99999, 99999)],
        spot=99999.0,
    )
    assert closed is not None
    assert closed["reason"] == "session"
    assert closed["exit"] == round(prior_close, 4)


def test_edge_guard_blocks_thin_target(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl", require_edge=True)
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    rows = scan_vwap_signals(prefix)
    up1 = float(rows[-1]["up1"])
    # Spot almost at +1σ — thinner than MIN_EDGE_BUFFER_PTS.
    ev = bot.on_bars(
        now=_now("09:35"), bars=prefix, spot=up1 - 0.5
    )
    assert ev is None
    assert bot.position is None


def test_gap_flatten_retries_when_close_append_fails(tmp_path: Path) -> None:
    """A failed close write must leave the stale book on its own day for a retry."""
    path = tmp_path / "vwap.jsonl"
    bot = _bot(path)
    bars = _pullback_long_bars("2026-09-17")
    for i, b in enumerate(bars):
        now = datetime.strptime(b["t"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        bot.on_bars(now=now, bars=bars[: i + 1], spot=float(b["c"]))
        if bot.position is not None:
            break
    assert bot.position is not None
    bot.day_pnl = 72.1
    bot.entries_today = 2
    bot.eod_written = False

    real_append = bot._append

    def flaky(event: dict) -> None:
        if event.get("event") == "close":
            raise OSError("disk full")
        real_append(event)

    bot._append = flaky  # type: ignore[method-assign]
    gap_bars = [_bar("2026-09-18 09:20", 25000, 25010, 24990, 25000)]
    with pytest.raises(OSError):
        bot.on_bars(now=_now("09:20", "2026-09-18"), bars=gap_bars, spot=25000.0)
    # Nothing rolled: still yesterday's book, so the next tick retries the flatten.
    assert bot.position is not None
    assert bot.position.day == "2026-09-17"
    assert bot.traded_day == "2026-09-17"
    assert bot.day_pnl == 72.1

    bot._append = real_append  # type: ignore[method-assign]
    closed = bot.on_bars(now=_now("09:21", "2026-09-18"), bars=gap_bars, spot=25000.0)
    assert closed is not None
    assert closed["day"] == "2026-09-18"
    assert bot.position is None
    assert bot.traded_day == "2026-09-18"
    assert bot.entries_today == 0
    assert bot.day_pnl == closed["pnl"]
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    seals = [ev for ev in lines if ev.get("event") == "day_pnl" and ev.get("day") == "2026-09-17"]
    assert len(seals) == 1
    assert seals[0]["day_pnl"] == 72.1
    assert sum(1 for ev in lines if ev.get("event") == "close") == 1


def test_defaults_entry_window_and_max_two(tmp_path: Path) -> None:
    bot = PaperVwapLong(path=tmp_path / "vwap.jsonl")
    assert MAX_ENTRIES_PER_DAY == 2
    assert bot.max_entries_per_day == 2
    snap = bot.snapshot(spot=None)
    assert snap["entry_window"] == "09:30-14:00"
    assert snap["max_entries_per_day"] == 2


def test_third_entry_blocked(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl")
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    bot.traded_day = "2026-09-18"
    bot.entries_today = 2
    ev = bot.on_bars(
        now=_now("09:35"), bars=prefix, spot=float(prefix[-1]["c"])
    )
    assert ev is None
    assert bot.position is None


def test_st_flip_exits_open_book(tmp_path: Path) -> None:
    bot = _bot(tmp_path / "vwap.jsonl", require_st=True)
    bot._st_dir_for_bar = lambda **_k: 1  # type: ignore[method-assign]
    bars = _pullback_long_bars()
    entry_i = next(i for i, b in enumerate(bars) if "09:35" in b["t"])
    prefix = bars[: entry_i + 1]
    bot.on_bars(now=_now("09:35"), bars=prefix, spot=float(prefix[-1]["c"]))
    assert bot.position is not None
    rows = scan_vwap_signals(prefix)
    vwap = float(rows[-1]["vwap"])
    dn1 = float(rows[-1]["dn1"])
    up1 = float(rows[-1]["up1"])
    hold = _bar("2026-09-18 09:36", vwap + 2, vwap + 4, vwap + 1, vwap + 3, v=100)
    assert float(hold["l"]) > dn1
    assert float(hold["h"]) < up1
    bot._st_dir_for_bar = lambda **_k: -1  # type: ignore[method-assign]
    closed = bot.on_bars(now=_now("09:36"), bars=prefix + [hold], spot=float(hold["c"]))
    assert closed is not None
    assert closed["reason"] == "st_flip"
    assert closed["exit"] == round(float(hold["c"]), 4)
    assert bot.position is None


def test_supertrend_incremental_matches_full_scan(tmp_path: Path) -> None:
    """Cached closed-prefix + one forming push must equal a full recompute."""
    bot = _bot(tmp_path / "vwap.jsonl")
    up = _pullback_long_bars()
    down = [
        _bar(f"2026-09-18 {9 + (15 + i) // 60:02d}:{(15 + i) % 60:02d}",
             25100 - 30 * i, 25110 - 30 * i, 25080 - 30 * i, 25085 - 30 * i)
        for i in range(18)
    ]
    for bars in (up, down):
        full = supertrend_series(bars)
        for n in range(1, len(bars) + 1):
            prefix = bars[:n]
            expect = int(full[n - 1]["dir"]) if full[n - 1]["ready"] else None
            got = bot._st_dir_for_bar(bars=prefix, session=prefix, sig_t=prefix[-1]["t"])
            assert got == expect, (n, got, expect)
    assert any(r["ready"] and r["dir"] == -1 for r in supertrend_series(down))
    # Same length / same tail, mid-series Kite correction → cache must not serve stale state.
    fixed = [dict(b) for b in down]
    for b in fixed[3:15]:
        b["c"] += 900.0
        b["h"] += 900.0
    expect = int(supertrend_series(fixed)[-1]["dir"])
    got = bot._st_dir_for_bar(bars=fixed, session=fixed, sig_t=fixed[-1]["t"])
    assert got == expect
