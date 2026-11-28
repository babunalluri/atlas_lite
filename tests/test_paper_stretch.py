"""14:00 stretch watch book: the ITM symbol, entry at 14:01 both ways, no trade under 0.75%, 15:00 exit, guards."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from atlas_lite.paper_stretch import PaperStretch, itm_symbol

DAY = "2026-11-30"
ATM = 25600
CE, PE = f"NFO:NIFTY26D01{ATM}CE", f"NFO:NIFTY26D01{ATM}PE"
CE_ITM, PE_ITM = f"NFO:NIFTY26D01{ATM - 100}CE", f"NFO:NIFTY26D01{ATM + 100}PE"


class Book:
    def __init__(self, px: dict | None = None) -> None:
        self.px = px if px is not None else {CE_ITM: 160.0, PE_ITM: 150.0, CE: 100.0, PE: 95.0}

    def get(self, s: str):
        p = self.px.get(s)
        return None if p is None else {"last_price": p, "bid": p - 0.5, "ask": p + 0.5}


def t(hm: str, sec: int = 5) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hm}:{sec:02d}+05:30")


def feed(move_pct: float, day: str = DAY, upto: str = "14:00") -> dict:
    o = 25000.0
    bars = [{"t": f"{day} 09:15", "o": o, "h": o, "l": o, "c": o}]
    bars.append({"t": f"{day} 13:59", "o": o, "h": o, "l": o, "c": o})
    if upto >= "14:00":
        bars.append({"t": f"{day} 14:00", "o": o, "h": o, "l": o, "c": o * (1 + move_pct / 100)})
    return {"atm": ATM, "ce_symbol": CE, "pe_symbol": PE, "bars_today": bars}


def ledger(b: PaperStretch) -> list[dict]:
    return [json.loads(x) for x in b.path.read_text().splitlines() if x.strip()]


def test_itm_symbol_from_the_atm_symbol() -> None:
    assert itm_symbol(CE, ATM, "ce") == (CE_ITM, ATM - 100)
    assert itm_symbol(PE, ATM, "pe") == (PE_ITM, ATM + 100)
    assert itm_symbol("NFO:NIFTY26DEC25600CE", 25600, "ce") == ("NFO:NIFTY26DEC25500CE", 25500)  # monthly format
    assert itm_symbol(CE, 25650, "ce") is None  # symbol does not match the ATM: refuse


def test_up_stretch_buys_the_itm_ce_at_1401_and_sells_at_1500(tmp_path: Path) -> None:
    b = PaperStretch(path=tmp_path / "s.jsonl")
    assert b.on_frame(now=t("14:00"), feed=feed(0.9), book=Book()) is None  # before 14:01
    ev = b.on_frame(now=t("14:01"), feed=feed(0.9), book=Book())
    assert ev and ev["leg"] == "ce" and ev["symbol"] == CE_ITM and ev["strike"] == ATM - 100 and ev["fill"] == 160.5
    assert ev["move_pct"] == 0.9
    assert b.on_frame(now=t("14:30"), feed=feed(0.9), book=Book()) is None  # one trade a day, holding
    ev = b.on_frame(now=t("15:00"), feed={}, book=Book({CE_ITM: 180.0}))
    assert ev and ev["event"] == "close" and ev["reason"] == "exit_1500" and ev["exit"] == 179.5


def test_down_stretch_buys_the_itm_pe_and_a_small_move_skips(tmp_path: Path) -> None:
    b = PaperStretch(path=tmp_path / "s.jsonl")
    ev = b.on_frame(now=t("14:01"), feed=feed(-1.2), book=Book())
    assert ev and ev["leg"] == "pe" and ev["symbol"] == PE_ITM
    q = PaperStretch(path=tmp_path / "q.jsonl")
    assert q.on_frame(now=t("14:01"), feed=feed(0.5), book=Book()) is None
    assert ledger(q)[-1]["event"] == "skip" and "needs" in ledger(q)[-1]["reason"]
    assert q.on_frame(now=t("14:02"), feed=feed(0.9), book=Book()) is None  # decided for the day


def test_guards_holiday_missing_quote_and_late_restart(tmp_path: Path) -> None:
    b = PaperStretch(path=tmp_path / "s.jsonl")
    old = feed(1.0, day="2026-11-27")  # only yesterday's candles: a holiday
    assert b.on_frame(now=t("14:01"), feed=old, book=Book()) is None and b.position is None
    assert not b.path.exists()
    nq = PaperStretch(path=tmp_path / "n.jsonl")
    assert nq.on_frame(now=t("14:01"), feed=feed(1.0), book=Book({})) is None  # no ITM quote yet: retry
    assert nq.on_frame(now=t("14:03"), feed=feed(1.0), book=Book())["symbol"] == CE_ITM
    late = PaperStretch(path=tmp_path / "l.jsonl")
    assert late.on_frame(now=t("14:20"), feed=feed(1.5), book=Book()) is None  # restart after 14:05
    assert ledger(late)[-1]["reason"] == "entry window 14:01-14:05 missed"
    again = PaperStretch(path=tmp_path / "l.jsonl")
    again._new_day(DAY)
    assert again.decided == DAY  # remembers the skip
    no_quote = PaperStretch(path=tmp_path / "z.jsonl")
    for hm in ("14:01", "14:02", "14:03", "14:04"):
        assert no_quote.on_frame(now=t(hm), feed=feed(1.0), book=Book({})) is None
    assert no_quote.on_frame(now=t("14:05"), feed=feed(1.0), book=Book({})) is None
    assert ledger(no_quote)[-1]["reason"] == "no ITM quote by 14:05"


def test_restart_restores_the_open_trade_and_exits_at_1500(tmp_path: Path) -> None:
    b = PaperStretch(path=tmp_path / "s.jsonl")
    b.on_frame(now=t("14:01"), feed=feed(0.9), book=Book())
    again = PaperStretch(path=tmp_path / "s.jsonl")
    again._new_day(DAY)
    assert again.position is not None and again.position["symbol"] == CE_ITM
    assert again.on_frame(now=t("14:59"), feed=feed(0.9), book=Book()) is None
    assert again.on_frame(now=t("15:00"), feed={}, book=Book())["reason"] == "exit_1500"
