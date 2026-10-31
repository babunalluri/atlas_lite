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
