"""G2T10 coded baseline: entry window, breakout / pullback entries, exit on a close back, 2-loss stop, restart."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from atlas_lite.paper_g2t10 import PaperG2T10

DAY = "2026-11-16"
CE, PE = "NFO:NIFTY26N1725000CE", "NFO:NIFTY26N1725000PE"


class Book:
    def __init__(self, ce: float = 100.0, pe: float = 100.0) -> None:
        self.px = {CE: ce, PE: pe}

    def get(self, s: str):
        p = self.px.get(s)
        return None if p is None else {"last_price": p, "bid": p - 0.5, "ask": p + 0.5}


def t(hm: str, sec: int = 5) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hm}:{sec:02d}+05:30")


def hm_of(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def flat_bars(until: str, px: float = 25000.0) -> list[dict]:
    """09:15 .. until (exclusive), a quiet 20-point range: opening range 24990-25010."""
    out, m = [], 9 * 60 + 15
    while hm_of(m) < until:
        out.append({"t": f"{DAY} {hm_of(m)}", "o": px, "h": px + 10, "l": px - 10, "c": px})
        m += 1
    return out


def bar(hm: str, o: float, h: float, l: float, c: float) -> dict:
    return {"t": f"{DAY} {hm}", "o": o, "h": h, "l": l, "c": c}


def feed(bars: list[dict]) -> dict:
    return {"atm": 25000, "ce_symbol": CE, "pe_symbol": PE, "bars_today": bars}


def mk(tmp_path: Path) -> PaperG2T10:
    return PaperG2T10(path=tmp_path / "g.jsonl")


def ledger(b: PaperG2T10) -> list[dict]:
    return [json.loads(x) for x in b.path.read_text().splitlines() if x.strip()]


def test_breakout_before_10_is_ignored_then_entry_and_close_back(tmp_path: Path) -> None:
    b = mk(tmp_path)
    early = flat_bars("09:50") + [bar("09:50", 25000, 25025, 25000, 25020)]  # breaks the day high at 09:50
    assert b.on_frame(now=t("09:51"), feed=feed(early), book=Book()) is None  # before 10:00: no entry
    bars = flat_bars("10:05") + [bar("10:05", 25000, 25025, 24995, 25020)]  # close above 25010, 5-min move up
    ev = b.on_frame(now=t("10:06"), feed=feed(bars), book=Book(ce=120))
    assert ev and ev["event"] == "open" and ev["leg"] == "ce" and ev["symbol"] == CE and ev["fill"] == 120.5
    assert ev["entry_level_kind"] == "day_high" and ev["entry_level"] == 25010
    assert b.on_frame(now=t("10:06", 30), feed=feed(bars), book=Book()) is None  # once a minute
    bars2 = bars + [bar("10:06", 25020, 25022, 25000, 25005)]  # closes back below 25010
    close = b.on_frame(now=t("10:07"), feed=feed(bars2), book=Book(ce=110))
    assert close and close["event"] == "close" and close["reason"] == "close_back" and close["exit"] == 109.5
    assert close["pnl"] < 0 and b.losses == 1


def test_far_break_waits_for_the_pullback(tmp_path: Path) -> None:
    b = mk(tmp_path)
    # 75 pts beyond 25010 (> 0.25%), and the candle stays above the level (a candle that dipped to it would count as
    # its own pullback, as in the backtest)
    bars = flat_bars("10:05") + [bar("10:05", 25015, 25090, 25012, 25085)]
    assert b.on_frame(now=t("10:06"), feed=feed(bars), book=Book()) is None and b.pending
    bars += [bar("10:06", 25085, 25090, 25050, 25060)]
    assert b.on_frame(now=t("10:07"), feed=feed(bars), book=Book()) is None  # no touch of 25010 yet
    bars += [bar("10:07", 25060, 25062, 25008, 25030)]  # touches the level, closes back above it
    ev = b.on_frame(now=t("10:08"), feed=feed(bars), book=Book())
    assert ev and ev["event"] == "open" and ev["leg"] == "ce" and ev["entry_level"] == 25010


def test_two_losses_stop_new_entries_and_a_restart_remembers(tmp_path: Path) -> None:
    b = mk(tmp_path)
    for i, (o, c) in enumerate(((100.0, 90.0), (100.0, 95.0))):
        b._append({"event": "open", "ts": f"{DAY}T10:1{i}:05+05:30", "day": DAY, "leg": "pe", "symbol": PE,
                   "strike": 25000, "qty": 65, "fill": o, "entry_level": 24990, "entry_level_kind": "day_low"})
        b._append({"event": "close", "ts": f"{DAY}T10:1{i}:35+05:30", "day": DAY, "pnl": (c - o) * 65})
    r = mk(tmp_path)  # restart
    bars = flat_bars("11:00") + [bar("11:00", 25000, 25025, 24995, 25020)]
    assert r.on_frame(now=t("11:01"), feed=feed(bars), book=Book()) is None
    assert r.losses == 2 and r.trades == 2 and r.last_reject == "two_losses_today"
    assert r.snapshot()["entries_stopped"] is True


def test_open_trade_is_restored_and_squared_off_at_1508(tmp_path: Path) -> None:
    b = mk(tmp_path)
    bars = flat_bars("10:05") + [bar("10:05", 25000, 25025, 24995, 25020)]
    b.on_frame(now=t("10:06"), feed=feed(bars), book=Book())
    r = mk(tmp_path)
    r.on_frame(now=t("15:07"), feed={}, book=Book())
    assert r.position is not None and r.position["leg"] == "ce"
    ev = r.on_frame(now=t("15:08", 0), feed={}, book=Book(ce=130))
    assert ev and ev["reason"] == "square_off" and ev["pnl"] > 0 and r.position is None
    assert [e["event"] for e in ledger(r)] == ["open", "close"]


def test_a_trade_left_open_from_an_earlier_day_is_closed_unpriced(tmp_path: Path) -> None:
    b = mk(tmp_path)
    b._append({"event": "open", "ts": "2026-11-13T14:59:03+05:30", "day": "2026-11-13", "leg": "ce", "symbol": CE,
               "strike": 25000, "qty": 65, "fill": 100.0, "entry_level": 25010, "entry_level_kind": "day_high"})
    r = mk(tmp_path)
    assert r.on_frame(now=t("10:06"), feed={}, book=Book()) is None and r.position is None
    last = ledger(r)[-1]
    assert last["event"] == "close" and last["reason"] == "missed_square_off" and last["pnl"] is None
    assert last["day"] == "2026-11-13" and r.losses == 0 and r.trades == 0
    r2 = mk(tmp_path)  # a second restart does not add another close
    r2.on_frame(now=t("10:07"), feed={}, book=Book())
    assert sum(e["event"] == "close" for e in ledger(r2)) == 1
