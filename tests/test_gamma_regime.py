"""Dealer-gamma reading from NSE open interest (logged with Scalper v3 entries)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from atlas_lite.gamma_regime import GammaOi, gamma_reading, parse_nifty_oi, previous_trading_day

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 30, 13, 0, tzinfo=IST)

CSV = "\n".join(
    [
        "TckrSymb,FinInstrmTp,XpryDt,StrkPric,OptnTp,OpnIntrst",
        "NIFTY,IDO,2026-11-03,23000,CE,1000",
        "NIFTY,IDO,2026-11-03,23000,PE,4000",
        "NIFTY,IDO,2027-03-30,23000,CE,9999",  # beyond 40 days: dropped
        "BANKNIFTY,IDO,2026-11-03,50000,CE,500",  # other index: dropped
        "NIFTY,IDO,2026-11-03,23100,CE,0",  # no open interest: dropped
    ]
)


def test_parse_keeps_near_nifty_options_with_open_interest() -> None:
    rows = parse_nifty_oi(CSV, date(2026, 10, 29))
    assert rows == [("2026-11-03", 23000, "CE", 1000.0), ("2026-11-03", 23000, "PE", 4000.0)]


def test_regime_sign_follows_calls_minus_puts() -> None:
    calls = [("2026-11-03", 23000, "CE", 1000.0)]
    puts = [("2026-11-03", 23000, "PE", 1000.0)]
    long_ = gamma_reading(calls, spot=23000, vix=14, now=NOW, oi_day="2026-10-29")
    short = gamma_reading(puts, spot=23000, vix=14, now=NOW, oi_day="2026-10-29")
    assert long_["regime"] == "long" and long_["share"] == 1.0
    assert short["regime"] == "short" and short["share"] == -1.0
    mixed = gamma_reading(parse_nifty_oi(CSV, date(2026, 10, 29)), spot=23000, vix=14, now=NOW, oi_day="2026-10-29")
    assert mixed["regime"] == "short" and -1 < mixed["share"] < 0
    assert gamma_reading([], spot=23000, vix=14, now=NOW, oi_day="x") is None


def test_previous_trading_day_skips_weekends() -> None:
    assert previous_trading_day(date(2026, 11, 2)) == date(2026, 10, 30)  # Monday -> Friday
    assert previous_trading_day(date(2026, 10, 30)) == date(2026, 10, 29)


def test_cached_open_interest_loads_without_a_download(tmp_path) -> None:
    store = GammaOi(tmp_path)
    store.dir.mkdir(parents=True)
    (store.dir / "2026-10-30.json").write_text(json.dumps([["2026-11-03", 23000, "CE", 1000.0]]))

    class NoNetwork:
        async def get(self, *a, **k):
            raise AssertionError("must not download when the day is cached")

    assert asyncio.run(store.ensure(date(2026, 11, 2), client=NoNetwork())) is True
    assert store.day == "2026-10-30"
    assert store.reading(spot=23000, vix=14, now=NOW)["regime"] == "long"
    assert store.reading(spot=None, vix=14, now=NOW) is None
