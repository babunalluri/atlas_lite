"""Pre-open cash–futures dislocation paper watcher (09:15–10:00 research)."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import preopen_basis_watch as P  # noqa: E402

UNI = {
    "ABC": {
        "cash_token": 1,
        "fut_token": 2,
        "fut_symbol": "ABC26NOVFUT",
        "expiry": date(2026, 11, 23),
        "lot": 500,
    }
}


PREV_BASIS = 0.40  # ABC's closing basis on the previous trading day (Fri 30 Oct), % of cash
REF = PREV_BASIS - P.fair_pct(3)  # aged by the 3 calendar days to Mon 2 Nov: ~0.347%


def write_prev_baselines(ref_dir, points=40, value=PREV_BASIS, day="2026-10-30"):
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / f"baselines-{day}.json").write_text(
        json.dumps({"ABC": [[f"15:{i:02d}", value] for i in range(points)]}))


def mk(tmp_path, monkeypatch, capital=250_000.0):
    monkeypatch.setattr(P, "LATENCY_MS", 50)
    monkeypatch.setattr(P, "NEXT_QUOTE_WAIT_S", 0.3)
    monkeypatch.setattr(P, "CONFIRM_MAX_S", 5.0)
    write_prev_baselines(tmp_path / "cf")
    return P.POB(UNI, tmp_path, "2026-11-02", capital, ref_dir=tmp_path / "cf", prev_universe=lambda d: UNI)


def tick(w, tok, bid, ask, bq=1000, aq=1000):
    w.on_tick({
        "instrument_token": tok,
        "bid": bid,
        "ask": ask,
        "depth": {
            "buy": [{"price": bid, "quantity": bq}],
            "sell": [{"price": ask, "quantity": aq}],
        },
    })


async def confirm(w, tok=2, bid=1010.0, ask=1010.1):
    await asyncio.sleep(0.01)
    tick(w, tok, bid, ask)


async def settle(w):
    while w._tasks:
        await asyncio.sleep(0.02)


def at(monkeypatch, hm):
    real = P.datetime

    class Clock(real):
        @classmethod
        def fromtimestamp(cls, ts, tz=None):
            d = real.fromtimestamp(ts, tz)
            return d.replace(hour=int(hm[:2]), minute=int(hm[3:]))

    monkeypatch.setattr(P, "datetime", Clock)


def test_fair_pct_scales_with_dte() -> None:
    assert P.fair_pct(0) == 0.0
    assert abs(P.fair_pct(365) - P.FAIR_RATE * 100) < 1e-9
    assert 0 < P.fair_pct(21) < 1.0


def test_rich_open_window_confirms_opens_and_reverts(tmp_path, monkeypatch) -> None:
    """Fut ~1% over cash vs a ~0.35% reference (previous close): rich enough to trade in the open window."""
    w = mk(tmp_path, monkeypatch)
    at(monkeypatch, "09:20")

    async def go():
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1010.0, 1010.1)
        await confirm(w)
        await asyncio.sleep(0.1)
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1010.0, 1010.1)
        await settle(w)
        assert w.position is not None
        # Full-lot unwind spread (1003.1 - 999.9)/999.95 = 0.32%: back under the reference, exit.
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1003.0, 1003.1)
        await asyncio.sleep(0.1)
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1003.0, 1003.1)
        await settle(w)

    asyncio.run(go())
    assert w.position is None
    (close,) = [e for e in w.events if e["event"] == "close"]
    assert close["reason"] == "reverted" and close["pnl"] is not None
    rep = w.report(final=True)
    assert rep["trades"] == 1 and rep["entry_probes"]["confirmed"]["both"] >= 1


def test_no_entry_after_window(tmp_path, monkeypatch) -> None:
    w = mk(tmp_path, monkeypatch)
    at(monkeypatch, "10:05")

    async def go():
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1010.0, 1010.1)
        await confirm(w)
        await settle(w)

    asyncio.run(go())
    assert not [e for e in w.events if e["event"] == "signal"]


def test_force_exit_at_1000(tmp_path, monkeypatch) -> None:
    (tmp_path / "pob-2026-11-02.jsonl").write_text(
        json.dumps({
            "ts": "2026-11-02T09:20:00+05:30",
            "event": "open",
            "stock": "ABC",
            "lot": 500,
            "cash_buy": 1000.0,
            "fut_sell": 1010.0,
            "fair_pct": 0.37,
            "est_net": 2000.0,
        })
        + "\n"
    )
    w = mk(tmp_path, monkeypatch)
    assert w.position is not None
    at(monkeypatch, "10:00")

    async def go():
        tick(w, 1, 1000.0, 1000.1)
        tick(w, 2, 1012.0, 1012.1)
        await asyncio.sleep(0.1)
        tick(w, 1, 1000.0, 1000.1)
        tick(w, 2, 1012.0, 1012.1)
        await settle(w)

    asyncio.run(go())
    (close,) = [e for e in w.events if e["event"] == "close"]
    assert close["reason"] == "time" and w.position is None


def test_cheap_futures_logged_not_traded(tmp_path, monkeypatch) -> None:
    w = mk(tmp_path, monkeypatch)
    at(monkeypatch, "09:25")

    async def go():
        # Fut bid well below cash ask: cheap, not short-spread rich.
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 990.0, 990.1)
        await settle(w)

    asyncio.run(go())
    assert [e["event"] for e in w.events] == ["cheap_sighting"]
    assert w.position is None


def test_one_leg_unwound(tmp_path, monkeypatch) -> None:
    w = mk(tmp_path, monkeypatch)
    at(monkeypatch, "09:30")

    async def go():
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1010.0, 1010.1)
        await confirm(w)
        await asyncio.sleep(0.1)
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1008.0, 1008.1)  # fut bid slipped: sell misses
        await asyncio.sleep(0.1)
        tick(w, 1, 999.5, 999.7)
        await settle(w)

    asyncio.run(go())
    (u,) = [e for e in w.events if e["event"] == "unwind"]
    assert u["leg"] == "cash" and w.position is None


def test_cf_host_shares_quotes_into_pob(tmp_path, monkeypatch) -> None:
    import cash_futures_watch as C

    monkeypatch.setattr(C, "LATENCY_MS", 50)
    monkeypatch.setenv("ATLAS_LITE_POB_WATCH", "1")
    cf_out = tmp_path / "cf"
    cf_out.mkdir()
    # CF needs a warm baseline for its own book; POB needs the previous day's baselines file.
    write_prev_baselines(cf_out)
    monkeypatch.setattr(C, "load_universe", lambda data_dir, d: UNI)
    w = C.CF(UNI, cf_out, "2026-11-02")
    assert abs(w.pob.refs["ABC"] - REF) < 1e-9
    assert w.pob is not None
    assert w.pob.quotes is w.quotes
    real = C.datetime

    class Clock(real):
        @classmethod
        def fromtimestamp(cls, ts, tz=None):
            d = real.fromtimestamp(ts, tz)
            return d.replace(hour=9, minute=20)

    monkeypatch.setattr(C, "datetime", Clock)
    monkeypatch.setattr(P, "datetime", Clock)
    monkeypatch.setattr(P, "LATENCY_MS", 50)
    monkeypatch.setattr(P, "NEXT_QUOTE_WAIT_S", 0.3)

    async def go():
        w.on_tick({
            "instrument_token": 1,
            "bid": 999.9,
            "ask": 1000.0,
            "depth": {"buy": [{"price": 999.9, "quantity": 1000}], "sell": [{"price": 1000.0, "quantity": 1000}]},
        })
        w.on_tick({
            "instrument_token": 2,
            "bid": 1010.0,
            "ask": 1010.1,
            "depth": {"buy": [{"price": 1010.0, "quantity": 1000}], "sell": [{"price": 1010.1, "quantity": 1000}]},
        })
        await asyncio.sleep(0.01)
        w.on_tick({
            "instrument_token": 2,
            "bid": 1010.0,
            "ask": 1010.1,
            "depth": {"buy": [{"price": 1010.0, "quantity": 1000}], "sell": [{"price": 1010.1, "quantity": 1000}]},
        })
        await asyncio.sleep(0.1)
        w.on_tick({
            "instrument_token": 1,
            "bid": 999.9,
            "ask": 1000.0,
            "depth": {"buy": [{"price": 999.9, "quantity": 1000}], "sell": [{"price": 1000.0, "quantity": 1000}]},
        })
        w.on_tick({
            "instrument_token": 2,
            "bid": 1010.0,
            "ask": 1010.1,
            "depth": {"buy": [{"price": 1010.0, "quantity": 1000}], "sell": [{"price": 1010.1, "quantity": 1000}]},
        })
        while w.pob._tasks:
            await asyncio.sleep(0.02)

    asyncio.run(go())
    assert w.pob.position is not None
    assert (tmp_path / "pob" / "pob-2026-11-02.jsonl").is_file()


def test_reference_is_the_previous_close_aged_by_carry(tmp_path) -> None:
    write_prev_baselines(tmp_path, points=40, value=0.9)
    write_prev_baselines(tmp_path, points=40, value=0.1, day="2026-10-29")  # older file: not used
    day, refs = P.load_refs(tmp_path, "2026-11-02", UNI, prev_universe=lambda d: UNI)
    assert day == "2026-10-30" and abs(refs["ABC"] - (0.9 - P.fair_pct(3))) < 1e-9


def test_no_reference_without_enough_minutes_or_after_a_contract_roll(tmp_path) -> None:
    write_prev_baselines(tmp_path, points=P.REF_MIN_POINTS - 1)
    assert P.load_refs(tmp_path, "2026-11-02", UNI, prev_universe=lambda d: UNI)[1] == {}
    write_prev_baselines(tmp_path, points=40)
    rolled = {"ABC": {**UNI["ABC"], "fut_symbol": "ABC26OCTFUT"}}  # the previous day traded another contract
    assert P.load_refs(tmp_path, "2026-11-02", UNI, prev_universe=lambda d: rolled)[1] == {}
    assert P.load_refs(tmp_path / "missing", "2026-11-02", UNI)[1] == {}


def test_a_stock_without_a_reference_is_not_traded(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(P, "LATENCY_MS", 50)
    w = P.POB(UNI, tmp_path, "2026-11-02")  # no reference directory
    at(monkeypatch, "09:20")

    async def go():
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1010.0, 1010.1)
        await confirm(w)
        await settle(w)

    asyncio.run(go())
    assert w.events == [] and w.report(final=False)["rules"]["stocks_with_ref"] == 0


def test_exit_waits_for_a_full_lot_reversion(tmp_path, monkeypatch) -> None:
    w = mk(tmp_path, monkeypatch)
    spawned = []
    monkeypatch.setattr(w, "_spawn", lambda coro: (spawned.append(coro), coro.close()))
    w.position = {"stock": "ABC", "lot": 500, "cash_buy": 1000.0, "fut_sell": 1010.0, "ref_pct": REF}
    now = __import__("time").time()
    # Top of book is back under the reference, but its 100 shares are not a lot; the full lot is not.
    w.quotes[("cash", "ABC")] = {"bid": 999.9, "ask": 1000.0, "bids": [(999.9, 100), (998.0, 1000)],
                                 "asks": [(1000.0, 1000)], "at": now}
    w.quotes[("fut", "ABC")] = {"bid": 1003.0, "ask": 1003.1, "bids": [(1003.0, 1000)],
                                "asks": [(1003.1, 1000)], "at": now}
    w.manage_position("09:30")
    assert spawned == []  # full lot: (1003.1 - 998.38)/999.95 = 0.47%, still rich
    w.quotes[("cash", "ABC")]["bids"] = [(999.9, 1000)]
    w.manage_position("09:30")
    assert len(spawned) == 1  # the full lot reverts too: exit


def test_no_entry_while_the_host_book_holds_the_capital(tmp_path, monkeypatch) -> None:
    w = mk(tmp_path, monkeypatch)
    w.capital_busy = lambda: True
    at(monkeypatch, "09:20")

    async def go():
        tick(w, 1, 999.9, 1000.0)
        tick(w, 2, 1010.0, 1010.1)
        await confirm(w)
        await settle(w)

    asyncio.run(go())
    assert w.events == [] and w.position is None


def test_recovery_closes_an_open_position_with_pnl_unknown(tmp_path) -> None:
    (tmp_path / "pob-2026-10-30.jsonl").write_text(json.dumps(
        {"ts": "2026-10-30T09:20:00+05:30", "event": "open", "stock": "ABC", "lot": 500, "cash_buy": 1000.0,
         "fut_sell": 1010.0, "ref_pct": 0.35, "est_net": 2000.0}) + "\n")
    P.finalize_missing(tmp_path)
    rows = [json.loads(x) for x in (tmp_path / "pob-2026-10-30.jsonl").read_text().splitlines()]
    assert rows[-1]["event"] == "close" and rows[-1]["pnl"] is None and rows[-1]["reason"] == "no_final_report"
    assert json.loads((tmp_path / "summary-2026-10-30.json").read_text())["recovered"] is True
