"""Expiry-week selling paper books (expiry-eve strangle, expiry-day straddle)."""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path

from atlas_lite.paper_expiry_sell import PaperExpirySell, lots_for_vix, nearest

EXPIRY = date(2026, 11, 3)  # Tuesday
EVE = "2026-11-02"  # Monday: one session before expiry
F0 = 23000
STRIKES = list(range(20000, 26050, 50))


def sym(k: int, opt: str) -> str:
    return f"NFO:NIFTY26N03{k}{opt}"


def chain(F: float = F0, scale: float = 1.0, bump: dict[str, float] | None = None) -> dict[str, dict]:
    rows = {}
    for k in STRIKES:
        for opt in ("CE", "PE"):
            intrinsic = max(0.0, (F - k) if opt == "CE" else (k - F))
            px = max(0.05, round(intrinsic + scale * 120 * math.exp(-abs(k - F) / 200), 2))
            px = (bump or {}).get(sym(k, opt), px)
            rows[sym(k, opt)] = {"last_price": px, "bid": max(0.05, px - 0.5), "ask": px + 0.5}
    return rows


class Book:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def get(self, s: str):
        return self.rows.get(s)


def t(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def eve(tmp_path: Path, wing: float | None = 0.05) -> PaperExpirySell:
    return PaperExpirySell(path=tmp_path / "eve.jsonl", strategy="expiry_eve_strangle", sessions=1, dist=0.005,
                           wing_pct=wing, stop=1.0)


def day(tmp_path: Path) -> PaperExpirySell:
    return PaperExpirySell(path=tmp_path / "day.jsonl", strategy="expiry_day_straddle", sessions=0, dist=0.0,
                           wing_pct=0.05, stop=0.5, min_vix=13.0)


def frame(b: PaperExpirySell, now: datetime, rows: dict, vix: float | None = 15.0, allow: bool = True):
    return b.on_frame(now=now, book=Book(rows), expiry=EXPIRY, strikes=STRIKES, option_symbol=sym, atm=F0,
                      vix=vix, allow_entry=allow)


def ledger(b: PaperExpirySell) -> list[dict]:
    return [json.loads(x) for x in b.path.read_text().splitlines() if x.strip()]


def test_helpers() -> None:
    assert nearest([22850, 22900, 22950], 22910) == 22900
    assert lots_for_vix(16.9) == 1 and lots_for_vix(17.0) == 2 and lots_for_vix(None) == 1


def test_eve_strangle_enters_once_with_wings_first(tmp_path: Path) -> None:
    b = eve(tmp_path)
    assert frame(b, t("2026-10-30", "09:20"), chain()) is None  # two sessions before: not this book
    assert frame(b, t(EVE, "09:15"), chain()) is None  # before 09:16
    ev = frame(b, t(EVE, "09:17"), chain())
    assert ev and ev["event"] == "open" and ev["lots"] == 1 and ev["qty"] == 65
    legs = [(x["strike"], x["opt"], x["sign"]) for x in ev["legs"]]
    assert legs[:2] == [(24150, "CE", 1), (21850, "PE", 1)]  # wings first, nearest F +- 5%
    assert legs[2:] == [(23100, "CE", -1), (22900, "PE", -1)]  # nearest F +- 0.5%
    assert frame(b, t(EVE, "09:18"), chain()) is None  # one trade per expiry


def test_eve_strangle_leg_stop_then_expiry_close(tmp_path: Path) -> None:
    b = eve(tmp_path, wing=None)
    frame(b, t(EVE, "09:17"), chain())
    ce = b.position.legs[0]
    assert ce.opt == "CE" and ce.sign < 0
    rows = chain(bump={ce.symbol: ce.entry * 2.05})
    ev = frame(b, t(EVE, "11:00"), rows)
    assert ev and ev["event"] == "leg_stop" and ev["leg"] == ce.symbol
    assert frame(b, t(EVE, "15:25"), rows) is None  # carried overnight
    b2 = eve(tmp_path, wing=None)  # restart restores the stopped leg
    assert b2.position is not None and b2.position.legs[0].exit is not None
    close = frame(b2, t("2026-11-03", "15:14"), chain(scale=0.1))
    assert close and close["event"] == "close" and close["reason"] == "expiry" and close["pnl_known"]
    stop_loss = (ce.fill - ev["exit"]) * 65
    assert close["pnl_gross"] > stop_loss  # the put decayed, recovering part of the call's stop loss


def test_expiry_day_straddle_skips_low_vix_and_doubles_at_high_vix(tmp_path: Path) -> None:
    b = day(tmp_path)
    assert frame(b, t(EVE, "09:17"), chain()) is None  # not expiry day
    assert frame(b, t("2026-11-03", "09:17"), chain(), vix=12.4) is None
    assert ledger(b)[-1]["event"] == "skip" and b.last_reject.startswith("vix_12.4")
    assert frame(b, t("2026-11-03", "09:18"), chain(), vix=20.0) is None  # decided for this expiry
    b2 = day(tmp_path / "x")
    ev = frame(b2, t("2026-11-03", "09:17"), chain(), vix=18.0)
    assert ev["event"] == "open" and ev["lots"] == 2 and ev["qty"] == 130
    assert [(x["strike"], x["sign"]) for x in ev["legs"] if x["sign"] < 0] == [(23000, -1), (23000, -1)]


def test_expiry_day_straddle_closes_when_both_legs_stop(tmp_path: Path) -> None:
    b = day(tmp_path)
    frame(b, t("2026-11-03", "09:17"), chain())
    shorts = [leg for leg in b.position.legs if leg.sign < 0]
    rows = chain(bump={leg.symbol: leg.entry * 1.6 for leg in shorts})
    ev = frame(b, t("2026-11-03", "10:00"), rows)
    assert ev and ev["event"] == "close" and ev["reason"] == "all_legs_stopped"
    assert sum(1 for e in ledger(b) if e["event"] == "leg_stop") == 2
    assert ev["pnl"] < 0


def test_missed_expiry_books_unknown_pnl(tmp_path: Path) -> None:
    b = eve(tmp_path)
    frame(b, t(EVE, "09:17"), chain())
    ev = frame(b, t("2026-11-04", "09:20"), chain())
    assert ev["reason"] == "expiry_missed" and ev["pnl"] is None


def test_vix_skip_survives_restart_and_missing_vix_is_retried(tmp_path: Path) -> None:
    b = day(tmp_path)
    assert frame(b, t("2026-11-03", "09:16"), chain(), vix=None) is None
    assert b.last_reject == "no_vix" and b.entered_for == ""  # retried, not skipped
    frame(b, t("2026-11-03", "09:17"), chain(), vix=12.0)
    b2 = day(tmp_path)  # restart inside the entry window
    assert frame(b2, t("2026-11-03", "09:18"), chain(), vix=14.0) is None


def test_no_stop_outside_market_hours(tmp_path: Path) -> None:
    b = eve(tmp_path)
    frame(b, t(EVE, "09:17"), chain())
    ce = next(leg for leg in b.position.legs if leg.sign < 0 and leg.opt == "CE")
    rows = chain(bump={ce.symbol: ce.entry * 3})
    assert frame(b, t(EVE, "15:40"), rows) is None and ce.exit is None
    ev = frame(b, t("2026-11-03", "09:15"), rows)  # gap at the next open
    assert ev and ev["event"] == "leg_stop"


def test_close_reports_entry_and_exit(tmp_path: Path) -> None:
    b = day(tmp_path)
    frame(b, t("2026-11-03", "09:17"), chain())
    ev = frame(b, t("2026-11-03", "15:14"), chain(scale=0.2))
    assert ev["entry"] > 0 and ev["exit"] is not None and ev["exit"] < ev["entry"]


def test_sensex_books_use_their_own_chain_and_lot(tmp_path: Path) -> None:
    from atlas_lite.instruments import option_lot_size
    from atlas_lite.paper_expiry_sell import BOOKS

    assert {BOOKS[n]["index"] for n in BOOKS} == {"nifty", "sensex"}
    assert BOOKS["sensex_expiry_day_straddle"]["min_vix"] == BOOKS["expiry_day_straddle"]["min_vix"]
    fo_csv = ("name,instrument_type,expiry,lot_size\nSENSEX,CE,2026-11-12,20\nNIFTY,CE,2026-11-09,65\n")
    assert option_lot_size(fo_csv, "SENSEX", expiry=date(2026, 11, 12), default=1) == 20
    assert option_lot_size(fo_csv, "SENSEX", expiry=date(2026, 11, 19), default=1) == 1

    sx_exp, sx_f = date(2026, 11, 12), 76000  # Thursday expiry, 100-pt strikes
    sx_strikes = list(range(70000, 82100, 100))

    def sx_sym(k: int, opt: str) -> str:
        return f"BFO:SENSEX26N12{k}{opt}"

    rows = {}
    for k in sx_strikes:
        for opt in ("CE", "PE"):
            px = max(0.05, round(max(0.0, (sx_f - k) if opt == "CE" else (k - sx_f)) + 300 * math.exp(-abs(k - sx_f) / 600), 2))
            rows[sx_sym(k, opt)] = {"last_price": px, "bid": max(0.05, px - 1), "ask": px + 1}
    b = PaperExpirySell(path=tmp_path / "sx.jsonl", strategy="sensex_expiry_day_straddle", sessions=0, dist=0.0,
                        wing_pct=0.05, stop=0.5, min_vix=13.0, lot_size=20)
    assert b.on_frame(now=t("2026-11-11", "09:17"), book=Book(rows), expiry=sx_exp, strikes=sx_strikes,
                      option_symbol=sx_sym, atm=sx_f, vix=15.0) is None  # Wednesday: not expiry day
    ev = b.on_frame(now=t("2026-11-12", "09:17"), book=Book(rows), expiry=sx_exp, strikes=sx_strikes,
                    option_symbol=sx_sym, atm=sx_f, vix=15.0)
    assert ev["event"] == "open" and ev["qty"] == 20
    assert [(x["strike"], x["opt"], x["sign"]) for x in ev["legs"]] == [
        (79800, "CE", 1), (72200, "PE", 1), (76000, "CE", -1), (76000, "PE", -1)]


def test_missing_chain_is_reported_then_cleared(tmp_path: Path) -> None:
    b = day(tmp_path)
    assert b.on_frame(now=t("2026-11-03", "09:17"), book=Book(chain()), expiry=None, strikes=[], option_symbol=None,
                      atm=None, vix=15.0) is None
    assert b.last_reject == "no_chain"
    assert frame(b, t("2026-11-03", "09:17"), chain())["event"] == "open" and b.last_reject is None
