"""NSE vs BSE gap watcher reports and the /api/arb endpoint (read-only, paper only)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import atlas_lite.main as main

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nse_bse_gap_watch as W  # noqa: E402


def test_api_arb_reports_nothing_until_the_watcher_writes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    assert asyncio.run(main.api_arb()) == {"ok": True, "available": False}


def test_api_arb_prefers_the_final_summary_over_the_live_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    arb = tmp_path / "arb"
    arb.mkdir()
    (arb / "live-2026-11-02.json").write_text(json.dumps({"day": "2026-11-02", "final": False, "small": {"crossings": 3}}))
    out = asyncio.run(main.api_arb())
    assert out["available"] is True and out["final"] is False and out["small"]["crossings"] == 3
    (arb / "summary-2026-11-02.json").write_text(json.dumps({"day": "2026-11-02", "final": True, "small": {"crossings": 9}}))
    (arb / "live-2026-10-30.json").write_text(json.dumps({"day": "2026-10-30", "final": False}))
    out = asyncio.run(main.api_arb())
    assert out["final"] is True and out["small"]["crossings"] == 9 and out["day"] == "2026-11-02"


def test_watcher_writes_live_then_final_reports(tmp_path) -> None:
    universe = {"AAA": ("small", "AAA")}
    tokens = {1: ("NSE", "AAA"), 2: ("BSE", "AAA")}
    w = W.Watch(tokens, tmp_path, "2026-11-02", universe)

    def tick(tok, bid, ask):
        w.on_tick({"instrument_token": tok, "bid": bid, "ask": ask, "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]}})

    tick(1, 500.0, 500.2)
    tick(2, 497.8, 498.0)  # crossed and still open
    live = w.write_report(final=False)
    assert live["final"] is False and live["small"]["crossings"] == 0  # open crossings left out of a live snapshot
    assert json.loads((tmp_path / "live-2026-11-02.json").read_text())["small"]["stocks"] == 1
    final = w.write_report(final=True)
    assert final["final"] is True and final["small"]["crossings"] == 1
    assert (tmp_path / "summary-2026-11-02.json").is_file()
    assert list(final)[:4] == ["day", "final", "updated_at", "ticks"] and "small" in final


def _watch(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "LATENCIES_MS", (100,))
    monkeypatch.setattr(W, "NEXT_QUOTE_WAIT_S", 0.3)
    monkeypatch.setattr(W, "MIN_PAPER_NET", 0.0)  # mechanics tests; the ₹500 rule has its own tests
    monkeypatch.setattr(W, "RESEARCH_NETS", ())  # one track: research tracks have their own test
    universe = {"AAA": ("small", "AAA")}
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", universe)
    w._resume_until = 0.0  # mechanics tests trade at once; the start cooldown has its own test

    def tick(tok, bid, ask, bq=100, aq=100):
        w.on_tick({"instrument_token": tok, "bid": bid, "ask": ask, "depth": {"buy": [{"quantity": bq}], "sell": [{"quantity": aq}]}})

    return w, tick


async def _settle(w):
    while w._tasks:
        await asyncio.sleep(0.02)


def test_charges_for_the_crossing_test_equal_the_paper_fills() -> None:
    assert W.intraday_cost(1000.0, 1001.0, 100, "BSE") == W.order_charges(
        [("BSE", "buy", 1000.0, 100), ("NSE", "sell", 1001.0, 100)])


def test_size_is_capped_at_one_lakh_for_stats_and_paper() -> None:
    assert W.tradable_qty({"ask": 500.0, "ask_qty": 10_000}, {"bid": 502.0, "bid_qty": 10_000}) == 200
    assert W.tradable_qty({"ask": None, "ask_qty": 10}, {"bid": 502.0, "bid_qty": 10}) == 0


def test_both_legs_fill_on_the_next_quote_update(tmp_path, monkeypatch) -> None:
    w, tick = _watch(tmp_path, monkeypatch)

    async def go():
        tick(1, 500.0, 500.2)
        tick(2, 497.8, 498.0)  # ₹2 cross on 100 shares: clears costs, paper fires
        await asyncio.sleep(0.15)
        tick(1, 500.0, 500.2)
        tick(2, 497.8, 498.0)  # update after arrival: still crossed
        await _settle(w)

    asyncio.run(go())
    (rec,) = w.paper
    assert rec["outcome"] == "both" and rec["quotes_updated"] is True and rec["bought"] == rec["sold"] == 100
    assert rec["net"] == round(200.0 - W.order_charges([("BSE", "buy", 498.0, 100), ("NSE", "sell", 500.0, 100)]), 2)


def test_one_leg_is_unwound_at_market_and_loses(tmp_path, monkeypatch) -> None:
    w, tick = _watch(tmp_path, monkeypatch)

    async def go():
        tick(1, 500.0, 500.2)
        tick(2, 497.8, 498.0)
        await asyncio.sleep(0.15)
        tick(2, 497.8, 498.0)  # BSE ask still there: buy fills
        tick(1, 497.0, 497.2)  # NSE bid fell below the 500 limit: sell misses
        await asyncio.sleep(0.15)
        tick(2, 497.5, 497.9)  # unwind: sell the 100 shares on BSE at 497.5
        await _settle(w)

    asyncio.run(go())
    (rec,) = w.paper
    assert rec["outcome"] == "one_leg" and rec["bought"] == 100 and rec["sold"] == 0
    assert rec["unwind_pnl"] == round(100 * (497.5 - 498.0), 2) and rec["net"] < 0
    assert rec["unwind_quote_updated"] is True


def test_no_quote_update_after_arrival_is_flagged(tmp_path, monkeypatch) -> None:
    w, tick = _watch(tmp_path, monkeypatch)

    async def go():
        tick(1, 500.0, 500.2)
        tick(2, 497.8, 498.0)
        await _settle(w)  # no further ticks

    asyncio.run(go())
    (rec,) = w.paper
    assert rec["outcome"] == "unknown" and rec["quotes_updated"] is False
    assert rec["bought"] == rec["sold"] == 0 and rec["net"] is None  # a guess either way: no fills, no P&L
    paper = w.summary()["small"]["paper"]["100ms"]
    assert paper["unknown"] == 1 and paper["net"] == 0


def test_a_cross_that_does_not_clear_costs_is_not_paper_traded(tmp_path, monkeypatch) -> None:
    w, tick = _watch(tmp_path, monkeypatch)

    async def go():
        tick(1, 500.0, 500.2)
        tick(2, 499.85, 499.95)  # 5 paise on 100 shares: far below costs
        await _settle(w)

    asyncio.run(go())
    assert w.paper == [] and w.open_eps  # still recorded as a crossing


def test_a_failed_paper_attempt_is_logged_and_counted(tmp_path, monkeypatch, capsys) -> None:
    w, tick = _watch(tmp_path, monkeypatch)

    async def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(w, "paper_attempt", broken)

    async def go():
        tick(1, 500.0, 500.2)
        tick(2, 497.8, 498.0)
        await _settle(w)

    asyncio.run(go())
    assert w.paper_errors == 1 and "paper attempt failed" in capsys.readouterr().out


def test_missing_universe_warns(tmp_path, capsys) -> None:
    uni = W.load_universe(tmp_path / "missing.json")
    assert set(g for g, _ in uni.values()) == {"large"} and "WARNING" in capsys.readouterr().out


def test_api_arb_falls_back_to_live_when_the_summary_is_unreadable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    arb = tmp_path / "arb"
    arb.mkdir()
    (arb / "summary-2026-11-02.json").write_text("{not json")
    (arb / "live-2026-11-02.json").write_text(json.dumps({"day": "2026-11-02", "final": False}))
    out = asyncio.run(main.api_arb())
    assert out["available"] is True and out["final"] is False


def test_api_arb_trades_lists_the_latest_day_newest_first_by_delay(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    assert asyncio.run(main.api_arb_trades(latency_ms=300, limit=300, day=None, track=None))["count"] == 0
    arb = tmp_path / "arb"
    arb.mkdir()
    rows = [
        {"seen": "2026-11-02T09:16:02.410+05:30", "latency_ms": 300, "stock": "AAA", "net": 180.0},
        {"seen": "2026-11-02T11:42:15.090+05:30", "latency_ms": 300, "stock": "BBB", "net": -40.0},
        {"seen": "2026-11-02T11:42:15.090+05:30", "latency_ms": 100, "stock": "BBB", "net": 10.0},
    ]
    (arb / "paper-2026-11-02.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n{bad line\n")
    (arb / "paper-2026-10-30.jsonl").write_text(json.dumps(rows[0]) + "\n")
    out = asyncio.run(main.api_arb_trades(latency_ms=300, limit=300, day=None, track=None))
    assert out["day"] == "2026-11-02" and out["count"] == 2 and out["net"] == 140.0
    assert [t["stock"] for t in out["trades"]] == ["BBB", "AAA"]


def test_summary_and_trade_list_use_the_same_day(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    arb = tmp_path / "arb"
    arb.mkdir()
    (arb / "summary-2026-10-30.json").write_text(json.dumps({"day": "2026-10-30", "final": True}))
    (arb / "paper-2026-10-30.jsonl").write_text(json.dumps({"seen": "x", "latency_ms": 300, "net": 5.0}) + "\n")
    (arb / "paper-2026-11-02.jsonl").write_text(json.dumps({"seen": "y", "latency_ms": 300, "net": 7.0}) + "\n")
    summary = asyncio.run(main.api_arb())
    trades = asyncio.run(main.api_arb_trades(latency_ms=300, limit=300, day=None, track=None))
    assert summary["day"] == trades["day"] == "2026-11-02"  # newest paper day; no summary for it yet
    assert summary["available"] is False
    older = asyncio.run(main.api_arb_trades(latency_ms=300, limit=300, day="2026-10-30", track=None))
    assert older["count"] == 1 and older["net"] == 5.0


def test_trade_list_says_how_many_are_shown(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    arb = tmp_path / "arb"
    arb.mkdir()
    rows = [{"seen": f"2026-11-02T10:00:{i:02d}", "latency_ms": 300, "net": 1.0} for i in range(5)]
    (arb / "paper-2026-11-02.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = asyncio.run(main.api_arb_trades(latency_ms=300, limit=2, day=None, track=None))
    assert out["count"] == 5 and out["shown"] == 2 and len(out["trades"]) == 2 and out["net"] == 5.0


def test_paper_trades_need_the_minimum_estimated_net(tmp_path, monkeypatch) -> None:
    w, tick = _watch(tmp_path, monkeypatch)
    monkeypatch.setattr(W, "MIN_PAPER_NET", 500.0)

    async def go():
        # ₹2.50 cross on 200 shares (₹1 lakh cap): ₹500 gross, under ₹500 after costs -> no paper trade
        tick(1, 500.5, 500.7, bq=1000, aq=1000)
        tick(2, 497.8, 498.0, bq=1000, aq=1000)
        await _settle(w)
        assert w.paper == []
        # ₹4 cross on 200 shares: ₹800 gross, about ₹717 after costs -> paper trade
        tick(1, 502.0, 502.2, bq=1000, aq=1000)
        await asyncio.sleep(0.15)
        tick(1, 502.0, 502.2, bq=1000, aq=1000)
        tick(2, 497.8, 498.0, bq=1000, aq=1000)
        await _settle(w)

    asyncio.run(go())
    (rec,) = w.paper
    assert rec["est_net"] >= 500 and rec["outcome"] == "both" and rec["qty"] == 200
    s = w.summary(final=True)
    assert s["min_net"] == 500.0 and s["small"]["crossings_meeting_min_net"] >= 1


def test_research_tracks_run_beside_the_main_track(tmp_path, monkeypatch) -> None:
    w, tick = _watch(tmp_path, monkeypatch)
    monkeypatch.setattr(W, "MIN_PAPER_NET", 500.0)
    monkeypatch.setattr(W, "RESEARCH_NETS", (100.0, 200.0, 300.0, 1000.0))

    async def go():
        # ₹4 cross on 200 shares: about ₹717 estimated -> tracks 100, 200, 300 and 500; not 1000
        tick(1, 502.0, 502.2, bq=1000, aq=1000)
        tick(2, 497.8, 498.0, bq=1000, aq=1000)
        await asyncio.sleep(0.15)
        tick(1, 502.0, 502.2, bq=1000, aq=1000)
        tick(2, 497.8, 498.0, bq=1000, aq=1000)
        await _settle(w)

    asyncio.run(go())
    assert sorted(r["track"] for r in w.paper) == [100.0, 200.0, 300.0, 500.0]
    s = w.summary(final=True)["small"]
    assert s["paper"]["100ms"]["attempts"] == 1  # main (₹500) track only
    assert s["paper_research"]["100"]["100ms"]["attempts"] == 1
    assert s["paper_research"]["1000"]["100ms"]["attempts"] == 0


def test_trade_list_filters_by_track_and_defaults_to_main(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    arb = tmp_path / "arb"
    arb.mkdir()
    rows = [{"seen": "a", "latency_ms": 300, "track": 500.0, "net": 600.0},
            {"seen": "b", "latency_ms": 300, "track": 100.0, "net": 120.0},
            {"seen": "c", "latency_ms": 300, "track": 100.0, "net": -40.0}]
    (arb / "paper-2026-11-02.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    main_out = asyncio.run(main.api_arb_trades(latency_ms=300, limit=300, day=None, track=None))
    assert main_out["track"] == 500.0 and main_out["count"] == 1 and main_out["net"] == 600.0
    research = asyncio.run(main.api_arb_trades(latency_ms=300, limit=300, day=None, track=100.0))
    assert research["count"] == 2 and research["net"] == 80.0


def test_trade_list_default_is_the_main_track_even_without_main_rows(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    arb = tmp_path / "arb"
    arb.mkdir()
    (arb / "live-2026-11-02.json").write_text(json.dumps({"day": "2026-11-02", "final": False, "min_net": 500.0}))
    (arb / "paper-2026-11-02.jsonl").write_text(json.dumps({"seen": "a", "latency_ms": 300, "track": 1000.0, "net": 900.0}) + "\n")
    out = asyncio.run(main.api_arb_trades(latency_ms=300, limit=300, day=None, track=None))
    assert out["track"] == 500.0 and out["count"] == 0  # not the ₹1,000 research track


def test_restart_mid_day_reloads_the_days_records(tmp_path) -> None:
    (tmp_path / "gaps-2026-11-02.jsonl").write_text(json.dumps(
        {"stock": "AAA", "group": "small", "direction": "buy BSE sell NSE", "start": "2026-11-02T10:00:00.000+05:30",
         "duration_s": 0.7, "max_gap_pct": 0.3, "max_qty": 100, "best_net": 300.0}) + "\n")
    (tmp_path / "paper-2026-11-02.jsonl").write_text(json.dumps(
        {"stock": "AAA", "group": "small", "latency_ms": 300, "track": 500.0, "outcome": "both", "net": 250.0}) + "\n")
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", {"AAA": ("small", "AAA")})
    s = w.summary(final=False)["small"]
    assert s["crossings"] == 1 and s["paper"]["300ms"]["attempts"] == 1 and s["paper"]["300ms"]["net"] == 250.0


def test_a_second_watcher_cannot_take_the_lock(tmp_path) -> None:
    first = W.acquire_lock(tmp_path)
    assert first is not None
    assert W.acquire_lock(tmp_path) is None
    first.close()
    assert W.acquire_lock(tmp_path) is not None


def test_planned_last_day_of_a_five_day_run() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    assert W.planned_until(datetime(2026, 10, 31, 12, 0, tzinfo=ist), 5, "15:30").isoformat() == "2026-11-06"
    assert W.planned_until(datetime(2026, 11, 2, 10, 0, tzinfo=ist), 5, "15:30").isoformat() == "2026-11-06"


def test_unwind_without_a_fresh_quote_assumes_a_spread_against() -> None:
    q = {"bid": 100.0, "ask": 100.2}
    assert W.unwind_price(q, "sell", True, 101.0) == 100.0
    assert W.unwind_price(q, "sell", False, 101.0) == 99.8
    assert W.unwind_price(q, "buy", False, 99.0) == 100.4
    assert W.unwind_price(None, "sell", False, 100.0) == 99.5


def test_restart_restores_each_tracks_cooldown(tmp_path) -> None:
    (tmp_path / "paper-2026-11-02.jsonl").write_text(json.dumps(
        {"stock": "AAA", "group": "small", "latency_ms": 300, "track": 500.0, "outcome": "both", "net": 250.0,
         "seen": "2026-11-02T10:00:00.000+05:30"}) + "\n")
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", {"AAA": ("small", "AAA")})
    from datetime import datetime
    assert w._paper_last[("AAA", 300, 500.0)] == datetime.fromisoformat("2026-11-02T10:00:00.000+05:30").timestamp()


def test_a_day_without_a_final_report_is_rebuilt_from_its_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(W, "load_tokens", lambda universe: {1: ("NSE", "AAA"), 2: ("BSE", "AAA")})
    (tmp_path / "paper-2026-10-30.jsonl").write_text(json.dumps(
        {"stock": "AAA", "group": "small", "latency_ms": 300, "track": 500.0, "outcome": "both", "net": 250.0,
         "seen": "2026-10-30T10:00:00.000+05:30"}) + "\n")
    uni = tmp_path / "universe.json"
    uni.write_text(json.dumps({"small": [{"nse": "AAA", "bse": "AAA"}]}))
    W.finalize_missing(tmp_path, uni)
    report = json.loads((tmp_path / "summary-2026-10-30.json").read_text())
    assert report["recovered"] is True and report["final"] is True
    assert report["small"]["stocks"] == 1 and report["small"]["paper"]["300ms"]["net"] == 250.0
    W.finalize_missing(tmp_path, uni)  # a day with a final report is left alone
    assert json.loads((tmp_path / "summary-2026-10-30.json").read_text())["recovered"] is True


def test_until_is_the_end_even_past_the_days_count(tmp_path, monkeypatch) -> None:
    import argparse
    from datetime import date

    seen: list[str] = []
    days = iter(["2026-11-02", "2026-11-03", "2026-11-04"])

    async def fake_run_day(args, out, n):
        seen.append(next(days))
        return seen[-1]

    class Clock:  # today's date stays before --until until the last run returns it
        @staticmethod
        def now(tz=None):
            from datetime import datetime as real
            return real(2026, 11, 2, 10, 0, tzinfo=tz)

    monkeypatch.setattr(W, "run_day", fake_run_day)
    monkeypatch.setattr(W, "finalize_missing", lambda out, uni, end="15:30": None)
    monkeypatch.setattr(W, "datetime", Clock)
    monkeypatch.setattr(W, "idle", lambda: asyncio.sleep(0))
    monkeypatch.setattr(sys, "argv", ["w", "--out", str(tmp_path), "--days", "2", "--until", "2026-11-04"])
    asyncio.run(W.main())
    assert seen == ["2026-11-02", "2026-11-03", "2026-11-04"]  # 3 sessions although --days 2


def test_restart_restores_ticks_open_crossings_and_waits_before_paper(tmp_path) -> None:
    open_ep = {"stock": "AAA", "group": "small", "direction": "buy BSE sell NSE", "start": "2026-11-02T10:00:00.000+05:30",
               "end": "2026-11-02T10:00:01.000+05:30", "duration_s": 1.0, "ticks": 3, "max_gap_pct": 0.4,
               "max_qty": 200, "best_net": 600.0}
    (tmp_path / "live-2026-11-02.json").write_text(json.dumps({"day": "2026-11-02", "final": False, "ticks": 12345,
                                                                "open_crossings": [open_ep]}))
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", {"AAA": ("small", "AAA")})
    s = w.summary(final=False)
    assert s["ticks"] == 12345 and s["small"]["crossings"] == 1
    assert w.episodes[0]["closed_by"] == "restart"
    assert W.Watch({}, tmp_path, "2026-11-02", {}).episodes.__len__() == 1  # written once, not again on the next restart
    import time as _t
    assert w._resume_until > _t.time()
    w.maybe_paper("AAA", "BSE", "NSE", {"ask": 498.0, "ask_qty": 1000}, {"bid": 502.0, "bid_qty": 1000}, _t.time())
    assert not w._tasks  # no paper trade straight after a restart


def test_live_snapshot_lists_crossings_still_open(tmp_path) -> None:
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", {"AAA": ("small", "AAA")})
    w.on_tick({"instrument_token": 1, "bid": 500.0, "ask": 500.2, "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]}})
    w.on_tick({"instrument_token": 2, "bid": 497.8, "ask": 498.0, "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]}})
    live = w.write_report(final=False)
    assert len(live["open_crossings"]) == 1 and live["open_crossings"][0]["stock"] == "AAA"
    assert "open_crossings" not in w.write_report(final=True)


def test_recovery_respects_a_custom_session_end(tmp_path, monkeypatch) -> None:
    from datetime import datetime as real

    class Clock:
        @staticmethod
        def now(tz=None):
            return real(2026, 11, 2, 14, 0, tzinfo=tz)

        @staticmethod
        def fromtimestamp(*a, **k):
            return real.fromtimestamp(*a, **k)

        @staticmethod
        def fromisoformat(v):
            return real.fromisoformat(v)

    monkeypatch.setattr(W, "datetime", Clock)
    monkeypatch.setattr(W, "load_tokens", lambda universe: {})
    (tmp_path / "paper-2026-11-02.jsonl").write_text(json.dumps({"stock": "AAA", "latency_ms": 300, "net": 1.0}) + "\n")
    uni = tmp_path / "u.json"
    uni.write_text(json.dumps({"small": []}))
    W.finalize_missing(tmp_path, uni, end="15:00")  # 14:00 < 15:00: today's session not over
    assert not (tmp_path / "summary-2026-11-02.json").exists()
    W.finalize_missing(tmp_path, uni, end="13:30")  # 14:00 >= 13:30: over, rebuild
    assert (tmp_path / "summary-2026-11-02.json").exists()


def test_every_start_waits_out_the_cooldown(tmp_path) -> None:
    import time as _t

    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", {"AAA": ("small", "AAA")})
    assert w._resume_until > _t.time()  # no files at all: still guarded


def test_a_crossing_opened_after_the_last_snapshot_survives_a_crash(tmp_path) -> None:
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", {"AAA": ("small", "AAA")})
    w.on_tick({"instrument_token": 1, "bid": 500.0, "ask": 500.2, "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]}})
    w.on_tick({"instrument_token": 2, "bid": 497.8, "ask": 498.0, "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]}})
    assert w.open_eps and not w.episodes  # open, no live snapshot written: then the process dies
    again = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", {"AAA": ("small", "AAA")})
    assert len(again.episodes) == 1 and again.episodes[0]["closed_by"] == "restart"
    assert len(W.Watch({}, tmp_path, "2026-11-02", {}).episodes) == 1  # not added twice


def test_a_mangled_crossing_line_is_skipped(tmp_path) -> None:
    (tmp_path / "gaps-2026-11-02.jsonl").write_text(json.dumps({"stock": "AAA"}) + "\n" + json.dumps(
        {"stock": "AAA", "group": "small", "direction": "buy BSE sell NSE", "start": "s", "duration_s": 0.4,
         "max_gap_pct": 0.2, "max_qty": 10, "best_net": 50.0}) + "\n")
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", {"AAA": ("small", "AAA")})
    assert w.summary(final=False)["small"]["crossings"] == 1


def test_one_paper_attempt_per_crossing_even_if_it_stays_open(tmp_path, monkeypatch) -> None:
    w, tick = _watch(tmp_path, monkeypatch)
    monkeypatch.setattr(W, "PAPER_COOLDOWN_S", 0.05)

    async def go():
        for _ in range(6):  # the same crossing stays open well past the cooldown
            tick(1, 500.0, 500.2)
            tick(2, 497.8, 498.0)
            await asyncio.sleep(0.1)
        await _settle(w)
        assert len(w.paper) == 1
        tick(2, 500.0, 500.2)  # closes
        tick(2, 497.8, 498.0)  # a new crossing: one new attempt
        await asyncio.sleep(0.15)
        tick(1, 500.0, 500.2)
        tick(2, 497.8, 498.0)
        await _settle(w)

    asyncio.run(go())
    assert len(w.paper) == 2


def test_open_marker_is_refreshed_so_a_crash_keeps_the_latest_values(tmp_path, monkeypatch) -> None:
    import time as _t

    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", {"AAA": ("small", "AAA")})

    def tick(tok, bid, ask):
        w.on_tick({"instrument_token": tok, "bid": bid, "ask": ask, "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]}})

    tick(1, 500.0, 500.2)
    tick(2, 498.8, 499.0)  # opens: ₹1 cross
    _t.sleep(1.05)
    tick(1, 500.0, 500.2)  # both quotes fresh again (a pair older than 1 s is ignored)
    tick(2, 496.8, 497.0)  # widens to ₹3 after a second: marker refreshed
    assert len(w.open_eps) == 1 and not w.episodes  # then the process dies
    again = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", {"AAA": ("small", "AAA")})
    (ep,) = again.episodes
    assert ep["closed_by"] == "restart" and ep["duration_s"] >= 1.0 and ep["max_gap_pct"] > 0.5


def test_reload_keeps_the_freshest_copy_of_an_open_crossing(tmp_path) -> None:
    base = {"stock": "AAA", "group": "small", "direction": "buy BSE sell NSE", "start": "2026-11-02T10:00:00.000+05:30",
            "ticks": 1, "max_qty": 100}
    stale = {**base, "end": "2026-11-02T10:00:05.000+05:30", "duration_s": 5.0, "max_gap_pct": 0.2, "best_net": 100.0}
    fresh = {**base, "end": "2026-11-02T10:00:50.000+05:30", "duration_s": 50.0, "max_gap_pct": 0.6, "best_net": 900.0}
    (tmp_path / "live-2026-11-02.json").write_text(json.dumps({"day": "2026-11-02", "final": False, "open_crossings": [stale]}))
    (tmp_path / "gaps-2026-11-02.jsonl").write_text(json.dumps({"event": "open", **fresh}) + "\n")
    w = W.Watch({}, tmp_path, "2026-11-02", {})
    (ep,) = w.episodes
    assert ep["best_net"] == 900.0 and ep["duration_s"] == 50.0


def test_restarts_do_not_repeat_attempts_on_a_crossing_that_never_closed(tmp_path, monkeypatch) -> None:
    # Cooldowns off: only the carried root and attempts can stop a repeat.
    monkeypatch.setattr(W, "LATENCIES_MS", (100,))
    monkeypatch.setattr(W, "NEXT_QUOTE_WAIT_S", 0.3)
    monkeypatch.setattr(W, "MIN_PAPER_NET", 0.0)
    monkeypatch.setattr(W, "RESEARCH_NETS", ())
    monkeypatch.setattr(W, "PAPER_COOLDOWN_S", 0.0)
    toks, uni = {1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, {"AAA": ("small", "AAA")}

    def tick(w, tok, bid, ask):
        w.on_tick({"instrument_token": tok, "bid": bid, "ask": ask, "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]}})

    async def run(first: bool):
        w = W.Watch(toks, tmp_path, "2026-11-02", uni)
        w._resume_until = 0.0
        w._paper_last.clear()  # no cooldown either
        tick(w, 1, 500.0, 500.2)
        tick(w, 2, 497.8, 498.0)  # the gap is open at the first look after every start
        await asyncio.sleep(0.15)
        tick(w, 1, 500.0, 500.2)
        tick(w, 2, 497.8, 498.0)
        await _settle(w)
        w.write_report(final=False)  # its open crossing reaches the snapshot, then the process "dies"
        return w

    asyncio.run(run(True))
    roots = set()
    for _ in range(3):  # three restarts while the gap stays open
        w = asyncio.run(run(False))
        roots |= {ep.get("root") for ep in w.open_eps.values()}
    lines = [json.loads(x) for x in (tmp_path / "paper-2026-11-02.jsonl").read_text().splitlines()]
    assert len(lines) == 1  # without the carried root this is 4
    assert roots == {lines[0]["crossing_root"]}  # every continuation kept the first crossing's root


def test_a_gap_that_closed_after_the_restart_starts_a_new_root(tmp_path, monkeypatch) -> None:
    w, tick = _watch(tmp_path, monkeypatch)
    w._carry[("AAA", "buy BSE sell NSE")] = ("2026-11-02T10:00:00.000+05:30", {(0.0, 100)})
    tick(1, 500.0, 500.2)
    tick(2, 500.0, 500.2)  # first look: no gap -> the carry is dropped
    assert not w._carry
    tick(2, 497.8, 498.0)  # later crossing: its own root
    (ep,) = w.open_eps.values()
    assert ep["root"] != "2026-11-02T10:00:00.000+05:30"


def test_no_tradable_size_does_not_end_a_crossing_or_drop_its_carry(tmp_path, monkeypatch) -> None:
    w, tick = _watch(tmp_path, monkeypatch)
    root = "2026-11-02T10:00:00.000+05:30"
    w._carry[("AAA", "buy BSE sell NSE")] = (root, {(0.0, 100)})  # attempted before the restart
    monkeypatch.setattr(W, "MAX_NOTIONAL", 100.0)  # under one share at ₹498: no tradable size
    tick(1, 500.0, 500.2)
    tick(2, 497.8, 498.0)  # first look: crossed, size 0
    assert ("AAA", "buy BSE sell NSE") in w._carry and not w.open_eps
    monkeypatch.setattr(W, "MAX_NOTIONAL", 100_000.0)  # size returns
    tick(2, 497.8, 498.0)
    (ep,) = w.open_eps.values()
    assert ep["root"] == root  # same crossing: carried attempts apply
    assert (0.0, 100) in w._attempted[("AAA", "buy BSE sell NSE", ep["start"])]


def test_a_running_crossing_survives_a_moment_without_size(tmp_path, monkeypatch) -> None:
    w, tick = _watch(tmp_path, monkeypatch)
    tick(1, 500.0, 500.2)
    tick(2, 497.8, 498.0)
    (first,) = w.open_eps.values()
    monkeypatch.setattr(W, "MAX_NOTIONAL", 100.0)
    tick(2, 497.8, 498.0)  # still crossed, no size
    monkeypatch.setattr(W, "MAX_NOTIONAL", 100_000.0)
    tick(2, 497.8, 498.0)
    (again,) = w.open_eps.values()
    assert again is first and not w.episodes  # one crossing, not closed and reopened


def test_carry_survives_a_restart_that_died_before_seeing_the_gap_again(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(W, "LATENCIES_MS", (100,))
    monkeypatch.setattr(W, "NEXT_QUOTE_WAIT_S", 0.3)
    monkeypatch.setattr(W, "MIN_PAPER_NET", 0.0)
    monkeypatch.setattr(W, "RESEARCH_NETS", ())
    monkeypatch.setattr(W, "PAPER_COOLDOWN_S", 0.0)
    toks, uni = {1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, {"AAA": ("small", "AAA")}

    def tick(w, tok, bid, ask):
        w.on_tick({"instrument_token": tok, "bid": bid, "ask": ask, "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]}})

    async def first_session():
        w = W.Watch(toks, tmp_path, "2026-11-02", uni)
        w._resume_until = 0.0
        tick(w, 1, 500.0, 500.2)
        tick(w, 2, 497.8, 498.0)
        await asyncio.sleep(0.15)
        tick(w, 1, 500.0, 500.2)
        tick(w, 2, 497.8, 498.0)
        await _settle(w)
        w.write_report(final=False)

    asyncio.run(first_session())
    W.Watch(toks, tmp_path, "2026-11-02", uni)  # restart 1: dies before any tick

    async def restart_2():
        w = W.Watch(toks, tmp_path, "2026-11-02", uni)
        w._resume_until = 0.0
        w._paper_last.clear()
        tick(w, 1, 500.0, 500.2)
        tick(w, 2, 497.8, 498.0)  # gap still open
        await asyncio.sleep(0.15)
        await _settle(w)

    asyncio.run(restart_2())
    assert len((tmp_path / "paper-2026-11-02.jsonl").read_text().splitlines()) == 1


def test_a_carry_ended_by_a_closed_gap_is_not_carried_again(tmp_path) -> None:
    toks, uni = {1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, {"AAA": ("small", "AAA")}
    root = "2026-11-02T10:00:00.000+05:30"
    (tmp_path / "gaps-2026-11-02.jsonl").write_text(json.dumps(
        {"stock": "AAA", "group": "small", "direction": "buy BSE sell NSE", "start": root, "end": root, "root": root,
         "duration_s": 0.0, "max_gap_pct": 0.4, "max_qty": 100, "best_net": 300.0, "closed_by": "restart"}) + "\n")
    w = W.Watch(toks, tmp_path, "2026-11-02", uni)
    assert w._carry[("AAA", "buy BSE sell NSE")][0] == root  # rebuilt from disk
    for tok, bid, ask in ((1, 500.0, 500.2), (2, 500.0, 500.2)):  # first look: the gap is closed
        w.on_tick({"instrument_token": tok, "bid": bid, "ask": ask, "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]}})
    assert not w._carry
    assert not W.Watch(toks, tmp_path, "2026-11-02", uni)._carry  # the carry_end marker stops a re-carry


def _restart_closed(tmp_path, end_iso: str, root: str = "2026-11-02T10:00:00.000+05:30") -> None:
    (tmp_path / "gaps-2026-11-02.jsonl").write_text(json.dumps(
        {"stock": "AAA", "group": "small", "direction": "buy BSE sell NSE", "start": root, "end": end_iso, "root": root,
         "duration_s": 1.0, "max_gap_pct": 0.4, "max_qty": 100, "best_net": 300.0, "closed_by": "restart"}) + "\n")


def test_carry_only_after_a_quick_restart(tmp_path) -> None:
    from datetime import datetime, timedelta

    toks, uni = {1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, {"AAA": ("small", "AAA")}
    recent = (datetime.now(W.IST) - timedelta(seconds=20)).isoformat(timespec="milliseconds")
    _restart_closed(tmp_path, recent)
    assert ("AAA", "buy BSE sell NSE") in W.Watch(toks, tmp_path, "2026-11-02", uni)._carry
    old = (datetime.now(W.IST) - timedelta(minutes=10)).isoformat(timespec="milliseconds")
    _restart_closed(tmp_path, old)
    assert not W.Watch(toks, tmp_path, "2026-11-02", uni)._carry  # a long outage: the gap may have closed


def test_carry_matches_attempts_by_the_crossings_own_start_too(tmp_path) -> None:
    from datetime import datetime, timedelta

    toks, uni = {1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, {"AAA": ("small", "AAA")}
    start = "2026-11-02T10:00:00.000+05:30"
    _restart_closed(tmp_path, (datetime.now(W.IST) - timedelta(seconds=5)).isoformat(timespec="milliseconds"), root=start)
    (tmp_path / "paper-2026-11-02.jsonl").write_text(json.dumps(  # older row format: crossing_start only
        {"stock": "AAA", "direction": "buy BSE sell NSE", "latency_ms": 100, "track": 500.0, "crossing_start": start,
         "seen": start, "net": 1.0}) + "\n")
    root, done = W.Watch(toks, tmp_path, "2026-11-02", uni)._carry[("AAA", "buy BSE sell NSE")]
    assert (500.0, 100) in done


def test_close_writes_before_forgetting(tmp_path, monkeypatch) -> None:
    w = W.Watch({1: ("NSE", "AAA"), 2: ("BSE", "AAA")}, tmp_path, "2026-11-02", {"AAA": ("small", "AAA")})
    w.on_tick({"instrument_token": 1, "bid": 500.0, "ask": 500.2, "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]}})
    w.on_tick({"instrument_token": 2, "bid": 497.8, "ask": 498.0, "depth": {"buy": [{"quantity": 100}], "sell": [{"quantity": 100}]}})

    def crash(ep):
        raise RuntimeError("disk full")

    monkeypatch.setattr(w, "_append_episode", crash)
    try:
        w.close(("AAA", "buy BSE sell NSE"))
    except RuntimeError:
        pass
    assert ("AAA", "buy BSE sell NSE") in w.open_eps  # not forgotten when the write failed
