"""Price overlays live in static/levels.js (pivots, auto fib, S/R, S/D)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "levels.js"


def _js(expr: str, payload: dict) -> dict | list:
    script = (
        "const m = require(process.argv[1]);"
        "const c = JSON.parse(process.argv[2]);"
        f"const out = {expr};"
        "process.stdout.write(JSON.stringify(out));"
    )
    proc = subprocess.run(
        ["node", "-e", script, str(JS), json.dumps(payload)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def _ts(day: str, hm: str) -> int:
    return int(datetime.fromisoformat(f"{day}T{hm}:00+05:30").timestamp() * 1000)


def _bar(ts_ms: int, high: float, low: float, close: float, open_: float | None = None) -> dict:
    o = close if open_ is None else open_
    return {
        "timestamp": ts_ms,
        "open": o,
        "high": high,
        "low": low,
        "close": close,
        "volume": 100,
    }


def test_classic_floor_pivots() -> None:
    p = _js("m.lvlClassicPivots(c.h, c.l, c.c)", {"h": 110, "l": 90, "c": 100})
    assert p["p"] == 100
    assert p["r1"] == 110
    assert p["s1"] == 90
    assert p["r2"] == 120
    assert p["s2"] == 80
    assert p["r3"] == 130
    assert p["s3"] == 70


def test_pivot_rows_use_prior_ist_session() -> None:
    bars = []
    for i in range(3):
        ts = _ts("2026-09-24", f"10:{i:02d}")
        bars.append(_bar(ts, 110, 90, 100, 95))
    for i in range(3):
        ts = _ts("2026-09-25", f"10:{i:02d}")
        bars.append(_bar(ts, 105, 95, 102, 100))
    rows = _js("m.calcPivotRows(c)", bars)
    assert "p" not in rows[0]
    assert rows[-1]["p"] == 100
    assert rows[-1]["r1"] == 110
    assert rows[-1]["day"] == "2026-09-25"


def test_williams_fractal_wing_two() -> None:
    t0 = _ts("2026-09-25", "10:00")
    highs = [10, 11, 15, 12, 11]
    lows = [8, 9, 5, 8, 9]
    bars = []
    for i, (h, l) in enumerate(zip(highs, lows)):
        bars.append(_bar(t0 + i * 60_000, h, l, (h + l) / 2))
    out = _js("m.lvlFractals(c, 2)", bars)
    assert out["highs"] == [{"i": 2, "price": 15, "kind": "h"}]
    assert out["lows"] == [{"i": 2, "price": 5, "kind": "l"}]


def test_auto_fib_zero_at_later_swing() -> None:
    t0 = _ts("2026-09-25", "10:00")
    path = [
        (10, 8),
        (11, 9),
        (12, 5),
        (11, 8),
        (12, 9),
        (13, 10),
        (18, 12),
        (15, 11),
        (14, 10),
    ]
    bars = [_bar(t0 + i * 60_000, h, l, (h + l) / 2) for i, (h, l) in enumerate(path)]
    pair = _js("m.lvlLastSwingPair(c.bars, c.opts)", {"bars": bars, "opts": {"wing": 2, "minAtrMult": 0}})
    assert pair["earlier"]["kind"] == "l"
    assert pair["later"]["kind"] == "h"
    assert pair["later"]["price"] == 18
    levels = _js("m.lvlFibLevels(c)", pair)
    by_label = {lv["label"]: lv["price"] for lv in levels}
    assert by_label["0"] == 18
    assert by_label["100"] == 5
    assert abs(by_label["50"] - 11.5) < 1e-9
    assert abs(by_label["61.8"] - (18 + (5 - 18) * 0.618)) < 1e-9


def test_sr_polarity_uses_price_vs_last_close() -> None:
    """Levels above last close are R; below are S — not fractal-kind majority."""
    t0 = _ts("2026-09-25", "10:00")
    # Cluster of lows around 100, then price rallies to 120 so that level is support.
    highs = [102, 103, 101, 102, 103, 110, 115, 118, 121, 122, 121]
    lows = [98, 99, 97, 98, 99, 105, 110, 114, 117, 118, 119]
    bars = [
        _bar(t0 + i * 60_000, h, l, (h + l) / 2)
        for i, (h, l) in enumerate(zip(highs, lows))
    ]
    # Force last close clearly above the early cluster.
    bars[-1] = _bar(t0 + (len(bars) - 1) * 60_000, 122, 119, 120.5)
    rows = _js("m.calcSrRows(c.bars, c.opts)", {"bars": bars, "opts": {"wing": 2, "maxLevels": 8}})
    levels = rows[-1]["sr"]["levels"]
    for lv in levels:
        if lv["price"] >= 120.5:
            assert lv["type"] == "R"
        else:
            assert lv["type"] == "S"


def test_sr_clusters_repeated_fractal_high() -> None:
    t0 = _ts("2026-09-25", "10:00")
    highs = [10, 11, 16, 12, 11, 12, 13, 16.1, 13, 12, 11, 12, 13]
    lows = [8, 9, 10, 9, 8, 9, 10, 11, 10, 9, 8, 9, 10]
    bars = [
        _bar(t0 + i * 60_000, h, l, (h + l) / 2)
        for i, (h, l) in enumerate(zip(highs, lows))
    ]
    rows = _js("m.calcSrRows(c.bars, c.opts)", {"bars": bars, "opts": {"wing": 2, "maxLevels": 8}})
    levels = rows[-1]["sr"]["levels"]
    resist = [lv for lv in levels if lv["type"] == "R"]
    assert resist
    assert any(abs(lv["price"] - 16) < 0.2 and lv["touches"] >= 2 for lv in resist)


def test_rbr_marks_unbroken_demand_zone() -> None:
    t0 = _ts("2026-09-25", "10:00")
    bars = []
    for i in range(18):
        bars.append(_bar(t0 + i * 60_000, 100.4, 99.8, 100.1, 100.0))
    # Two impulse-up bars, three tight base bars, two impulse-up bars.
    seq = [
        (108, 100, 107.5, 100.5),
        (116, 107, 115.5, 108),
        (116.3, 115.9, 116.1, 116.0),
        (116.4, 116.0, 116.2, 116.1),
        (116.5, 116.1, 116.3, 116.2),
        (124, 116, 123.5, 116.5),
        (132, 123, 131.5, 124),
    ]
    for j, (h, l, c, o) in enumerate(seq):
        bars.append(_bar(t0 + (18 + j) * 60_000, h, l, c, o))
    zones = _js("m.lvlSupplyDemandZones(c)", bars)
    demand = [z for z in zones if z["type"] == "demand" and z["pattern"] == "RBR"]
    assert demand
    z = demand[-1]
    assert z["broken"] is None
    assert 115.8 <= z["lo"] <= 116.2
    assert 116.2 <= z["hi"] <= 116.6


def test_empty_series_does_not_throw() -> None:
    assert _js("m.calcPivotRows(c)", []) == []
    assert _js("m.calcFibRows(c)", []) == []
    assert _js("m.calcSrRows(c)", []) == []
    assert _js("m.calcSdRows(c)", []) == []
    assert _js("m.lvlSupplyDemandZones(c)", []) == []


def test_same_bar_high_and_low_is_not_a_fib_pair() -> None:
    t0 = _ts("2026-09-25", "10:00")
    highs = [10, 11, 20, 12, 11]
    lows = [8, 9, 4, 8, 9]
    bars = [_bar(t0 + i * 60_000, h, l, (h + l) / 2) for i, (h, l) in enumerate(zip(highs, lows))]
    pair = _js("m.lvlLastSwingPair(c.bars, c.opts)", {"bars": bars, "opts": {"wing": 2, "minAtrMult": 0}})
    assert pair is None


def test_cluster_width_does_not_drift() -> None:
    swings = [
        {"i": 0, "price": 10, "kind": "h"},
        {"i": 1, "price": 12, "kind": "h"},
        {"i": 2, "price": 14, "kind": "h"},
        {"i": 3, "price": 16, "kind": "h"},
    ]
    groups = _js("m.lvlClusterSwings(c.swings, c.thresh)", {"swings": swings, "thresh": 3})
    assert len(groups) == 2
    assert groups[0]["touches"] == 2
    assert groups[1]["touches"] == 2


def test_inside_zone_prefers_fresh_over_later_broken() -> None:
    zones = [
        {"type": "demand", "from": 2, "lo": 100, "hi": 110, "broken": None},
        {"type": "supply", "from": 8, "lo": 105, "hi": 120, "broken": 9},
    ]
    hit = _js("m.lvlZoneAt(c.close, c.zones)", {"close": 108, "zones": zones})
    assert hit["type"] == "demand"
    assert hit["broken"] is None


def test_demand_zone_breaks_when_close_trades_through() -> None:
    t0 = _ts("2026-09-25", "10:00")
    bars = []
    for i in range(18):
        bars.append(_bar(t0 + i * 60_000, 100.4, 99.8, 100.1, 100.0))
    seq = [
        (108, 100, 107.5, 100.5),
        (116, 107, 115.5, 108),
        (116.3, 115.9, 116.1, 116.0),
        (116.4, 116.0, 116.2, 116.1),
        (116.5, 116.1, 116.3, 116.2),
        (124, 116, 123.5, 116.5),
        (132, 123, 131.5, 124),
        (116.2, 114.0, 114.5, 116.0),
    ]
    for j, (h, l, c, o) in enumerate(seq):
        bars.append(_bar(t0 + (18 + j) * 60_000, h, l, c, o))
    zones = _js("m.lvlSupplyDemandZones(c)", bars)
    demand = [z for z in zones if z["pattern"] == "RBR"]
    assert demand
    assert demand[-1]["broken"] is not None


def test_sd_mix_bars_still_form_rbr() -> None:
    t0 = _ts("2026-09-25", "10:00")
    bars = []
    for i in range(18):
        bars.append(_bar(t0 + i * 60_000, 100.5, 99.5, 100.1, 100.0))
    seq = [
        (108, 100, 107.5, 100.5),
        (116, 107, 115.5, 108),
        (116.9, 115.8, 116.2, 116.0),
        (116.5, 116.0, 116.2, 116.1),
        (124, 116, 123.5, 116.5),
        (132, 123, 131.5, 124),
    ]
    for j, (h, l, c, o) in enumerate(seq):
        bars.append(_bar(t0 + (18 + j) * 60_000, h, l, c, o))
    zones = _js("m.lvlSupplyDemandZones(c)", bars)
    assert any(z["pattern"] == "RBR" and z["broken"] is None for z in zones)


def test_sd_typical_minute_ranges_make_a_zone() -> None:
    t0 = _ts("2026-09-25", "10:00")
    bars = []
    px = 22780.0
    for i in range(40):
        bars.append(_bar(t0 + i * 60_000, px + 6, px - 6, px + 1, px - 1))
        px += 0.4
    seq = [
        (px + 14, px - 1, px + 12, px),
        (px + 18, px + 8, px + 16, px + 12),
        (px + 17, px + 14, px + 15, px + 16),
        (px + 16.5, px + 14.2, px + 15.2, px + 15),
        (px + 17.2, px + 14.5, px + 16.8, px + 15.2),
        (px + 28, px + 15, px + 26, px + 17),
        (px + 34, px + 24, px + 32, px + 26),
    ]
    for j, (h, l, c, o) in enumerate(seq):
        bars.append(_bar(t0 + (40 + j) * 60_000, h, l, c, o))
    zones = _js("m.lvlSupplyDemandZones(c)", bars)
    assert zones
    assert any(z["type"] in ("demand", "supply") for z in zones)


def test_chart_loads_levels_script() -> None:
    html = (ROOT / "static" / "index.html").read_text()
    main = (ROOT / "atlas_lite" / "main.py").read_text()
    assert 'src="/levels.js"' in html
    assert '@app.get("/levels.js")' in main
    assert '"PIVOT"' in html and '"FIB"' in html and '"SR"' in html and '"SD"' in html
    assert "function calcConfluenceRows" in html
    assert "function calcPivotRows" not in html
    assert "function calcConfluenceRows" not in (ROOT / "static" / "levels.js").read_text()
