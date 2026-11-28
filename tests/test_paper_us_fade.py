"""US-fade watch book: parsing the S&P 500 move, 09:16 entry against a >= 1% move, skips, 15:14 exit, restart."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from atlas_lite.paper_us_fade import PaperUsFade, last_us_move

DAY = "2026-11-30"
CE, PE = "NFO:NIFTY26D0125600CE", "NFO:NIFTY26D0125600PE"


class Book:
    def get(self, s: str):
        p = {CE: 100.0, PE: 90.0}.get(s)
        return None if p is None else {"last_price": p, "bid": p - 0.5, "ask": p + 0.5}


def yahoo(*closes_by_date: tuple[str, float]) -> dict:
    ts = [int(datetime.fromisoformat(f"{d}T14:30:00").replace(tzinfo=timezone.utc).timestamp()) for d, _ in closes_by_date]
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": [c for _, c in closes_by_date]}]}}]}}


def t(hm: str, sec: int = 5) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hm}:{sec:02d}+05:30")


FEED = {"atm": 25600, "ce_symbol": CE, "pe_symbol": PE, "bars_today": [{"t": f"{DAY} 09:15", "c": 25600.0}]}


def mk(tmp_path: Path, payload: dict) -> PaperUsFade:
    return PaperUsFade(path=tmp_path / "u.jsonl", fetch=lambda: payload)


def warm(b: PaperUsFade) -> None:
    b.on_frame(now=t("09:05"), feed={}, book=Book())  # starts the fetch thread
    for _ in range(50):
        if b.us:
            return
        time.sleep(0.01)


def ledger(b: PaperUsFade) -> list[dict]:
    return [json.loads(x) for x in b.path.read_text().splitlines() if x.strip()]


def test_last_us_move_uses_the_session_before_the_india_day() -> None:
    p = yahoo(("2026-11-25", 6000.0), ("2026-11-26", 6060.0), ("2026-11-30", 5000.0))
    assert last_us_move(p, DAY) == ("2026-11-26", 1.0)  # 30 Nov's own US session is later: ignored
    assert last_us_move({"chart": {}}, DAY) is None


def test_us_up_buys_the_pe_at_0916_and_sells_at_1514(tmp_path: Path) -> None:
    b = mk(tmp_path, yahoo(("2026-11-26", 6000.0), ("2026-11-27", 6090.0)))  # +1.5%
    warm(b)
    ev = b.on_frame(now=t("09:16"), feed=FEED, book=Book())
    assert ev and ev["event"] == "open" and ev["leg"] == "pe" and ev["fill"] == 90.5 and ev["us_move_pct"] == 1.5
    assert b.on_frame(now=t("10:00"), feed=FEED, book=Book()) is None  # one trade a day
    ev = b.on_frame(now=t("15:14"), feed={}, book=Book())
    assert ev and ev["reason"] == "square_off" and ev["exit"] == 89.5


def test_us_down_buys_the_ce_and_small_moves_skip(tmp_path: Path) -> None:
    b = mk(tmp_path, yahoo(("2026-11-26", 6000.0), ("2026-11-27", 5880.0)))  # -2%
    warm(b)
    assert b.on_frame(now=t("09:16"), feed=FEED, book=Book())["leg"] == "ce"
    q = PaperUsFade(path=tmp_path / "q.jsonl", fetch=lambda: yahoo(("2026-11-26", 6000.0), ("2026-11-27", 6030.0)))
    warm(q)
    assert q.on_frame(now=t("09:16"), feed=FEED, book=Book()) is None
    assert ledger(q)[-1]["event"] == "skip" and "under 1.0%" in ledger(q)[-1]["reason"]


def test_no_us_figure_skips_and_a_late_restart_never_enters(tmp_path: Path) -> None:
    def boom():
        raise RuntimeError("blocked")
    b = PaperUsFade(path=tmp_path / "u.jsonl", fetch=boom)
    warm(b)
    assert b.on_frame(now=t("09:16"), feed=FEED, book=Book()) is None
    assert ledger(b)[-1]["event"] == "skip" and "blocked" in ledger(b)[-1]["reason"]
    late = mk(tmp_path / "x", yahoo(("2026-11-26", 6000.0), ("2026-11-27", 6200.0)))
    (tmp_path / "x").mkdir()
    warm(late)
    assert late.on_frame(now=t("11:00"), feed=FEED, book=Book()) is None  # restart after 09:20: no late entry
    assert ledger(late)[-1]["reason"] == "entry window 09:16-09:20 missed"
    again = mk(tmp_path / "x", yahoo(("2026-11-26", 6000.0), ("2026-11-27", 6200.0)))
    warm(again)
    assert again.on_frame(now=t("09:16"), feed=FEED, book=Book()) is None and again.decided == DAY  # remembers the skip


def test_restart_restores_the_open_trade(tmp_path: Path) -> None:
    b = mk(tmp_path, yahoo(("2026-11-26", 6000.0), ("2026-11-27", 6090.0)))
    warm(b)
    b.on_frame(now=t("09:16"), feed=FEED, book=Book())
    again = mk(tmp_path, yahoo(("2026-11-26", 6000.0), ("2026-11-27", 6090.0)))
    again._new_day(DAY)
    assert again.position is not None and again.position["leg"] == "pe"
    assert again.on_frame(now=t("15:14"), feed={}, book=Book())["reason"] == "square_off"


def test_guards_holiday_stale_us_session_and_a_late_us_figure(tmp_path: Path) -> None:
    b = mk(tmp_path, yahoo(("2026-11-25", 6000.0), ("2026-11-26", 6090.0)))
    warm(b)
    holiday = {**FEED, "bars_today": [{"t": "2026-11-27 15:29", "c": 25600.0}]}  # only yesterday's candles
    assert b.on_frame(now=t("09:16"), feed=holiday, book=Book()) is None and b.position is None
    # US session dated before India's previous session (27 Nov, a Friday): already traded -> skip
    s_ = mk(tmp_path / "s", yahoo(("2026-11-24", 6000.0), ("2026-11-25", 6090.0)))
    (tmp_path / "s").mkdir()
    warm(s_)
    assert s_.on_frame(now=t("09:16"), feed=FEED, book=Book()) is None
    assert "already traded" in ledger(s_)[-1]["reason"]
    # day 2 with yesterday's decision still in memory: a US figure arriving at 09:25 must not trade
    late = mk(tmp_path / "l", yahoo(("2026-11-25", 6000.0), ("2026-11-26", 6200.0)))
    (tmp_path / "l").mkdir()
    late.decided = "2026-11-27"
    warm(late)
    assert late.on_frame(now=t("09:25"), feed=FEED, book=Book()) is None and late.position is None
    assert ledger(late)[-1]["reason"] == "entry window 09:16-09:20 missed"
