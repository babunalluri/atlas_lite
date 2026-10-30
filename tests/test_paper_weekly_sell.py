"""Weekly NIFTY strangle / 5%-wing condor paper books."""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

from atlas_lite.paper_weekly_sell import PaperWeeklySell, select_strikes, sessions_until

EXPIRY = date(2026, 11, 3)  # Tuesday
ENTRY_DAY = "2026-10-29"  # Thursday: Fri, Mon, Tue = 3 sessions
F0 = 23000


def sym(k: int, opt: str) -> str:
    return f"NFO:NIFTY26N03{k}{opt}"


def chain(F: float = F0, scale: float = 1.0) -> dict[str, dict]:
    """Smooth synthetic chain around F; bid/ask 0.5 either side of last price."""
    rows = {}
    for k in range(20000, 26050, 50):
        for opt in ("CE", "PE"):
            intrinsic = max(0.0, (F - k) if opt == "CE" else (k - F))
            px = round(intrinsic + scale * 150 * math.exp(-abs(k - F) / 260), 2)
            px = max(px, 0.05)
            rows[sym(k, opt)] = {"last_price": px, "bid": max(0.05, px - 0.5), "ask": px + 0.5}
    return rows


class Book:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def get(self, s: str):
        return self.rows.get(s)


def t(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+05:30")


def mk(tmp_path: Path, name: str = "weekly_condor5", wing: float | None = 0.05) -> PaperWeeklySell:
    return PaperWeeklySell(path=tmp_path / f"{name}.jsonl", strategy=name, wing_pct=wing)


def frame(b: PaperWeeklySell, now: datetime, rows: dict, expiry: date = EXPIRY, allow: bool = True):
    return b.on_frame(now=now, book=Book(rows), expiry=expiry, strikes=list(range(20000, 26050, 50)),
                      option_symbol=sym, atm=F0, allow_entry=allow)


def ledger(b: PaperWeeklySell) -> list[dict]:
    return [json.loads(x) for x in b.path.read_text().splitlines() if x.strip()]


def test_sessions_until_counts_trading_days_and_skips_holidays() -> None:
    assert sessions_until(date(2026, 10, 29), EXPIRY) == 3
    # Mon 9 Nov 2026 is an NSE holiday: the 10 Nov expiry is entered on Wednesday 4 Nov.
    assert sessions_until(date(2026, 11, 4), date(2026, 11, 10)) == 3
    assert sessions_until(date(2026, 11, 5), date(2026, 11, 10)) == 2


def test_select_strikes_numbers() -> None:
    strikes = list(range(20000, 26050, 50))
    assert select_strikes(23000, 0.14, strikes, strikes) == (22450, 23550, None, None)
    assert select_strikes(23000, 0.14, strikes, strikes, wing_pct=0.05) == (22450, 23550, 21300, 24700)
    # A target beyond the listed strikes means no trade, as in the backtest.
    assert select_strikes(23000, 1.2, [22000, 22050], strikes) is None


def test_opens_once_at_1525_three_sessions_before_expiry(tmp_path) -> None:
    b = mk(tmp_path)
    assert frame(b, t(ENTRY_DAY, "15:20"), chain()) is None
    assert frame(b, t("2026-10-28", "15:25"), chain()) is None  # 4 sessions before
    ev = frame(b, t(ENTRY_DAY, "15:25"), chain())
    assert ev is not None and ev["event"] == "open" and ev["expiry"] == "2026-11-03"
    legs = {(leg["strike"], leg["opt"], leg["sign"]) for leg in ev["legs"]}
    sp = next(k for k, o, s in legs if o == "PE" and s < 0)
    sc = next(k for k, o, s in legs if o == "CE" and s < 0)
    lp = next(k for k, o, s in legs if o == "PE" and s > 0)
    lc = next(k for k, o, s in legs if o == "CE" and s > 0)
    assert lp < sp < F0 < sc < lc and sp - lp >= 0.05 * F0 - 50 and lc - sc >= 0.05 * F0 - 50
    assert ev["credit_fill"] < ev["credit_ltp"]  # sold at bid, bought at ask
    assert ev["max_loss"] is not None
    b.position = None  # a later frame for the same expiry must not reopen
    assert frame(b, t(ENTRY_DAY, "15:26"), chain()) is None


def test_strangle_has_two_legs_and_uncapped_loss(tmp_path) -> None:
    b = mk(tmp_path, "weekly_strangle", None)
    ev = frame(b, t(ENTRY_DAY, "15:25"), chain())
    assert len(ev["legs"]) == 2 and ev["max_loss"] is None


def test_gate_blocks_entry(tmp_path) -> None:
    b = mk(tmp_path)
    assert frame(b, t(ENTRY_DAY, "15:25"), chain(), allow=False) is None and b.last_reject == "gate_paused"


def test_stop_fires_at_next_daily_check_not_intraday(tmp_path) -> None:
    b = mk(tmp_path, "weekly_strangle", None)
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    crash = chain(F=22300)  # short put deep in trouble
    assert frame(b, t("2026-10-30", "11:00"), crash) is None  # checked only at 15:25
    ev = frame(b, t("2026-10-30", "15:25"), crash)
    assert ev is not None and ev["event"] == "close" and ev["reason"] == "stop" and ev["pnl"] < 0


def test_quiet_day_logs_stop_check_and_holds(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    assert frame(b, t("2026-10-30", "15:25"), chain(scale=0.8)) is None
    checks = [e for e in ledger(b) if e["event"] == "stop_check"]
    assert len(checks) == 1 and checks[0]["day"] == "2026-10-30"
    assert frame(b, t("2026-10-30", "15:27"), chain(F=22000)) is None  # one check per day


def test_no_stop_on_expiry_day_then_close_at_1529(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    crash = chain(F=22300)
    assert frame(b, t("2026-11-03", "15:25"), crash) is None
    ev = frame(b, t("2026-11-03", "15:29"), crash)
    assert ev["reason"] == "expiry" and ev["pnl_known"] is True


def test_win_when_premium_decays(tmp_path) -> None:
    b = mk(tmp_path, "weekly_strangle", None)
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    ev = frame(b, t("2026-11-03", "15:29"), chain(scale=0.02))
    assert ev["reason"] == "expiry" and ev["pnl"] > 0


def test_restore_overnight_position_and_todays_stop_check(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    frame(b, t("2026-10-30", "15:25"), chain(scale=0.9))
    again = mk(tmp_path)
    assert again.position is not None and again.position.expiry == "2026-11-03"
    assert again.position.stop_checked == "2026-10-30" and again.entered_for == "2026-11-03"
    assert len(again.position.legs) == 4
    ev = frame(again, t("2026-11-03", "15:29"), chain(scale=0.05))
    assert ev["reason"] == "expiry"


def test_missed_expiry_closes_at_last_marks(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    frame(b, t("2026-11-02", "10:00"), chain(scale=0.3))
    ev = frame(b, t("2026-11-04", "09:20"), {})
    assert ev["reason"] == "expiry_missed" and ev["pnl_known"] is False


def test_weekend_no_action(tmp_path) -> None:
    b = mk(tmp_path)
    assert frame(b, t("2026-10-31", "15:25"), chain(), expiry=date(2026, 11, 3)) is None


def test_trades_api_pairs_overnight_trade(tmp_path) -> None:
    from atlas_lite.feed_engine import FeedEngine
    from atlas_lite.kite_rest import KiteRest

    b = PaperWeeklySell(path=tmp_path / "paper_weekly_strangle.jsonl", strategy="weekly_strangle")
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    frame(b, t("2026-11-03", "15:29"), chain(scale=0.02))
    out = FeedEngine(rest=MagicMock(spec=KiteRest), data_dir=tmp_path).list_paper_trades(limit=10)
    assert "weekly_strangle" in out["books"] and out["closed_count"] == 1 and out["pnl_total"] > 0


def test_expiry_close_uses_last_price_without_slippage(tmp_path) -> None:
    b = mk(tmp_path, "weekly_strangle", None)
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    rows = {s: {"last_price": 0.05} for s in chain()}  # worthless at expiry, no offers
    ev = frame(b, t("2026-11-03", "15:29"), rows)
    assert ev["reason"] == "expiry" and ev["exit"] == 0.1  # 2 legs x 0.05, no +1 pt buyback
    assert ev["pnl"] > 0


def test_missed_expiry_after_restart_books_unknown_pnl(tmp_path) -> None:
    b = mk(tmp_path)
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    again = mk(tmp_path)  # restart: no marks yet, contracts expired
    ev = frame(again, t("2026-11-04", "09:20"), {})
    assert ev["reason"] == "expiry_missed" and ev["pnl"] is None and ev["pnl_known"] is False


def test_close_uses_the_quantity_it_opened_with(tmp_path) -> None:
    b = mk(tmp_path)
    b.lot_size = 75
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    again = PaperWeeklySell(path=b.path, strategy="weekly_condor5", wing_pct=0.05, lot_size=65)
    assert again.position.qty == 75
    ev = frame(again, t("2026-11-03", "15:29"), chain(scale=0.02))
    assert ev["qty"] == 75


def test_restart_on_the_entry_evening_does_not_check_the_stop(tmp_path) -> None:
    """The entry day counts as checked; a restart that evening must not add a check."""
    b = mk(tmp_path)
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    again = mk(tmp_path)
    assert again.position is not None and again.position.stop_checked == ENTRY_DAY
    assert frame(again, t(ENTRY_DAY, "19:37"), chain(F=22300)) is None
    assert again.position is not None
    assert [e for e in ledger(again) if e["event"] == "stop_check"] == []


def test_snapshot_open_pnl_is_net_of_entry_and_exit_charges(tmp_path) -> None:
    from atlas_lite.kite_charges import kite_nfo_charges

    b = mk(tmp_path)
    frame(b, t(ENTRY_DAY, "15:25"), chain())
    rows = chain(scale=0.8)
    snap = b.snapshot(Book(rows))["position"]
    pos = b.position
    marks = [rows[leg.symbol]["last_price"] for leg in pos.legs]
    cost = sum(-leg.sign * m for leg, m in zip(pos.legs, marks))
    close_ch = kite_nfo_charges([(m, pos.qty, "buy" if leg.sign < 0 else "sell") for leg, m in zip(pos.legs, marks)])["total"]
    gross = (pos.credit_fill - cost) * pos.qty
    assert snap["open_pnl"] == round(gross - pos.charges_open - close_ch, 2)
    assert snap["open_pnl"] < gross
