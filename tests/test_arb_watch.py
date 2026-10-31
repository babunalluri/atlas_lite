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
