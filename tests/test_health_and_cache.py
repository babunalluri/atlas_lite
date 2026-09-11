"""Health, ADX reseed gate, candle deltas, fo_bhav prune."""

from __future__ import annotations

import asyncio
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from atlas_lite.feed_engine import HEALTH_TICK_MAX_AGE_S, FeedEngine
from atlas_lite.kite_rest import KiteRest
from atlas_lite.nse_fo_bhav import prune_fo_bhav_cache


def _engine() -> FeedEngine:
    from atlas_lite.notebook import get_notebook
    from atlas_lite.notebook_runtime import NotebookRuntime
    from atlas_lite.minute_bars import MinuteBarBuilder
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    rest = MagicMock(spec=KiteRest)
    eng = FeedEngine(rest=rest, data_dir=Path("data"))
    # Initialize nifty notebook for backward compatibility with tests
    nifty_cfg = get_notebook("nifty")
    nifty_nb = NotebookRuntime(config=nifty_cfg)
    nifty_nb.bar_builder = MinuteBarBuilder(symbol=nifty_cfg.symbol)
    eng.notebooks["nifty"] = nifty_nb
    return eng


def test_health_ok_when_ws_live_despite_auth_error() -> None:
    eng = _engine()
    eng.book.connected = True
    eng.book.last_tick_at = time.time()
    eng._kite_auth_error = (
        "Kite access token expired — run: python3 scripts/kite_get_access_token.py"
    )
    body = eng.health_status()
    assert body["ok"] is True
    assert body.get("degraded") is True
    assert "auth_error" in body
    assert "error" not in body


def test_health_false_when_auth_error_and_stale_ticks() -> None:
    eng = _engine()
    eng.book.connected = True
    eng.book.last_tick_at = time.time() - (HEALTH_TICK_MAX_AGE_S + 5)
    eng._kite_auth_error = "Kite access token expired"
    body = eng.health_status()
    assert body["ok"] is False
    assert body.get("error")


def test_maybe_reseed_skips_when_seeded_today_without_today_bar() -> None:
    eng = _engine()
    nifty_nb = eng.notebooks["nifty"]
    nifty_nb.adx_kite_seed_day = eng._today()
    nifty_nb.bar_builder.bars = [
        {"t": "2020-01-01 15:29", "o": 1, "h": 1, "l": 1, "c": 1},
    ]
    assert eng._adx_bars_fresh(nifty_nb) is False
    asyncio.get_event_loop().run_until_complete(eng._maybe_reseed_adx_bars())
    eng.rest.historical_minute.assert_not_called()


def test_maybe_reseed_skips_kite_outside_session() -> None:
    eng = _engine()
    nifty_nb = eng.notebooks["nifty"]
    nifty_nb.adx_kite_seed_day = ""
    # Enough bars so this is not a cold-start (cold-start may fetch off-session).
    nifty_nb.bar_builder.bars = [
        {"t": f"2026-09-10 10:{i:02d}", "o": 1, "h": 1, "l": 1, "c": 1} for i in range(30)
    ]
    nifty_nb.kite_adx_bars = list(nifty_nb.bar_builder.bars)
    eng._token_index = {"NSE:NIFTY 50": 256265}
    with patch("atlas_lite.feed_engine.in_adx_seed_window", return_value=False):
        asyncio.get_event_loop().run_until_complete(eng._maybe_reseed_adx_bars())
    eng.rest.historical_minute.assert_not_called()


def test_maybe_reseed_cold_start_fetches_outside_session() -> None:
    eng = _engine()
    nifty_nb = eng.notebooks["nifty"]
    nifty_nb.adx_kite_seed_day = ""
    nifty_nb.bar_builder.bars = []
    nifty_nb.kite_adx_bars = []
    eng._token_index = {"NSE:NIFTY 50": 256265}

    async def _candles(*_a, **_k):
        return [["2026-09-10 10:00:00", 1, 2, 1, 1.5, 100]]

    eng.rest.historical_minute.side_effect = _candles
    with patch("atlas_lite.feed_engine.in_adx_seed_window", return_value=False):
        asyncio.get_event_loop().run_until_complete(eng._maybe_reseed_adx_bars())
    eng.rest.historical_minute.assert_called()


def test_empty_seed_during_session_does_not_latch() -> None:
    async def _empty(*_a, **_k):
        return []

    eng = _engine()
    nifty_nb = eng.notebooks["nifty"]
    eng._token_index = {"NSE:NIFTY 50": 256265}
    eng.rest.historical_minute.side_effect = _empty
    asyncio.get_event_loop().run_until_complete(eng._seed_adx_bars_from_kite(nifty_nb))
    assert nifty_nb.adx_kite_seed_day == ""


def test_nifty_candles_since_returns_delta() -> None:
    eng = _engine()
    nifty_nb = eng.notebooks["nifty"]
    bars = [
        {"t": "2026-09-04 10:00", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "oi": 1},
        {"t": "2026-09-04 10:01", "o": 1.5, "h": 2.5, "l": 1, "c": 2, "v": 20, "oi": 2},
        {"t": "2026-09-04 10:02", "o": 2, "h": 3, "l": 1.5, "c": 2.5, "v": 30, "oi": 3},
    ]
    nifty_nb.kite_adx_bars = list(bars)
    full = eng.nifty_candles(limit=800)
    assert full["delta"] is False
    assert len(full["bars"]) == 3
    mid = full["bars"][1]["time"]
    delta = eng.nifty_candles(limit=800, since=mid)
    assert delta["delta"] is True
    assert len(delta["bars"]) == 2
    assert all(b["time"] >= mid for b in delta["bars"])


def test_nifty_candles_prefers_kite_bars_over_ws() -> None:
    eng = _engine()
    nifty_nb = eng.notebooks["nifty"]
    nifty_nb.bar_builder.bars = [
        {"t": "2026-09-04 10:00", "o": 1, "h": 2, "l": 0.5, "c": 99, "v": 10, "oi": 1},
    ]
    nifty_nb.kite_adx_bars = [
        {"t": "2026-09-04 10:00", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "oi": 1},
    ]
    out = eng.nifty_candles(limit=800)
    assert out["bars"][-1]["close"] == 99.0


def test_kite_adx_series_merges_ws_forming_minute() -> None:
    from atlas_lite.minute_bars import MinuteBarBuilder
    from atlas_lite.specs import NIFTY_SYMBOL
    from unittest.mock import patch

    eng = _engine()
    nifty_nb = eng.notebooks["nifty"]
    nifty_nb.kite_adx_bars = [
        {"t": "2026-09-10 09:43", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "oi": 1},
        {"t": "2026-09-10 09:44", "o": 2, "h": 2.1, "l": 1.9, "c": 2.0, "v": 0, "oi": 0},
    ]
    b = MinuteBarBuilder(symbol=NIFTY_SYMBOL)
    b.bars = list(nifty_nb.kite_adx_bars[:1])
    with patch("atlas_lite.minute_bars._minute_key", return_value="2026-09-10 09:44"):
        b.ingest(99.0)
    nifty_nb.bar_builder = b
    merged = eng._kite_adx_series_bars(nifty_nb)
    assert merged[-1]["t"] == "2026-09-10 09:44"
    assert merged[-1]["c"] == 99.0


def test_live_chart_bars_returns_tail() -> None:
    eng = _engine()
    nifty_nb = eng.notebooks["nifty"]
    bars = [
        {"t": "2026-09-04 10:00", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "oi": 1},
        {"t": "2026-09-04 10:01", "o": 1.5, "h": 2.5, "l": 1, "c": 2, "v": 20, "oi": 2},
        {"t": "2026-09-04 10:02", "o": 2, "h": 3, "l": 1.5, "c": 2.5, "v": 30, "oi": 3},
    ]
    nifty_nb.kite_adx_bars = list(bars)
    tail = eng.live_chart_bars("nifty", 2)
    assert len(tail) == 2
    assert tail[-1]["close"] == 2.5


def test_nifty_tick_refreshes_adx_on_forming_bar() -> None:
    from atlas_lite.minute_bars import MinuteBarBuilder
    from atlas_lite.specs import NIFTY_SYMBOL
    from unittest.mock import patch

    eng = _engine()
    nifty_nb = eng.notebooks["nifty"]
    b = MinuteBarBuilder(symbol=NIFTY_SYMBOL)
    for i in range(40):
        px = 24000.0 + i
        b.bars.append(
            {
                "t": f"2026-09-07 10:{i:02d}",
                "o": px,
                "h": px + 5,
                "l": px - 5,
                "c": px + 1,
            }
        )
    nifty_nb.bar_builder = b
    nifty_nb.kite_adx_bars = list(b.bars)
    eng._refresh_adx_from_bars(nifty_nb)
    assert nifty_nb.adx is not None
    assert 0 <= nifty_nb.adx <= 100
    nifty_nb.kite_adx_bars[-1]["c"] = 24080.0
    nifty_nb.kite_adx_bars[-1]["h"] = max(float(nifty_nb.kite_adx_bars[-1]["h"]), 24080.0)
    live = eng.live_chart_bars("nifty", 1)
    assert live
    assert live[-1]["close"] == 24080.0
    candles = eng.nifty_candles(limit=50)
    assert candles["bars"]
    assert candles["bars"][-1].get("adx") == eng.adx


def test_frame_revision_includes_live_bars() -> None:
    from atlas_lite.frame_util import frame_revision

    a = frame_revision(
        {"feed": {}, "live_bars": [{"time": 1, "open": 1, "high": 2, "low": 1, "close": 1.5}]}
    )
    b = frame_revision(
        {"feed": {}, "live_bars": [{"time": 1, "open": 1, "high": 2, "low": 1, "close": 1.6}]}
    )
    assert a != b


def test_prune_fo_bhav_cache(tmp_path: Path) -> None:
    cache = tmp_path / "fo_bhav"
    cache.mkdir()
    for day in (date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)):
        (cache / f"{day.strftime('%Y%m%d')}.csv").write_text("x", encoding="utf-8")
    assert prune_fo_bhav_cache(cache, keep_days=1) == 2
    assert list(cache.glob("*.csv")) == [cache / "20260103.csv"]
    assert prune_fo_bhav_cache(cache, keep_days=0) == 1
    assert list(cache.glob("*.csv")) == []


def test_safe_recording_path_rejects_bad_names(tmp_path: Path) -> None:
    from atlas_lite.recorder import safe_recording_path

    try:
        safe_recording_path(tmp_path, "../secret.jsonl")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_write_day_archive_zips_slots(tmp_path: Path) -> None:
    import zipfile

    from atlas_lite.recorder import write_day_archive

    (tmp_path / "2026-09-07_09-00.jsonl").write_text('{"ts":"a"}\n', encoding="utf-8")
    (tmp_path / "2026-09-07_09-30.jsonl").write_text('{"ts":"b"}\n', encoding="utf-8")
    (tmp_path / "2026-09-06_09-00.jsonl").write_text('{"ts":"skip"}\n', encoding="utf-8")
    zip_path, filename = write_day_archive(tmp_path, "2026-09-07")
    try:
        assert filename == "atlas-recordings-2026-09-07.zip"
        with zipfile.ZipFile(zip_path) as zf:
            assert set(zf.namelist()) == {
                "2026-09-07_09-00.jsonl",
                "2026-09-07_09-30.jsonl",
            }
            info = zf.getinfo("2026-09-07_09-00.jsonl")
            assert info.compress_type == zipfile.ZIP_DEFLATED
    finally:
        zip_path.unlink(missing_ok=True)


def test_write_day_archive_missing_day(tmp_path: Path) -> None:
    from atlas_lite.recorder import write_day_archive

    try:
        write_day_archive(tmp_path, "2026-09-07")
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass
    try:
        write_day_archive(tmp_path, "not-a-day")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
