"""Order flow lives in static/orderflow.js (1m FUT Δ, not 5m CLV)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "orderflow.js"


def _js(expr: str, payload: dict | list) -> dict | list:
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


def test_close_at_high_is_full_buy_delta() -> None:
    d = _js("m.ofBarDelta(c)", {"high": 10, "low": 0, "close": 10, "volume": 100})
    assert d == 100


def test_close_at_low_is_full_sell_delta() -> None:
    d = _js("m.ofBarDelta(c)", {"high": 10, "low": 0, "close": 0, "volume": 100})
    assert d == -100


def test_five_minute_bar_uses_one_minute_sum_not_clv() -> None:
    raw = []
    for i in range(4):
        raw.append({"time": i * 60, "high": 10, "low": 0, "close": 0, "volume": 100})
    raw.append({"time": 240, "high": 10, "low": 0, "close": 10, "volume": 100})
    five = {
        "timestamp": 0,
        "high": 10,
        "low": 0,
        "close": 10,
        "volume": 500,
    }
    clv = _js("m.ofBarDelta(c)", five)
    assert clv == 500
    summed = _js(
        "m.calcOrderFlowSeries(c.dataList, c.opts)[0]",
        {"dataList": [five], "opts": {"period": 5, "impulseMult": 1.5, "interval": 5, "rawBars": raw}},
    )
    assert summed["source"] == "1m"
    assert summed["delta"] == -300


def _ts(day: str, hm: str) -> int:
    return int(datetime.fromisoformat(f"{day}T{hm}:00+05:30").timestamp() * 1000)


def _bar(ts_ms: int, high: float, low: float, close: float, volume: float = 100) -> dict:
    return {
        "timestamp": ts_ms,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def test_divergence_does_not_fire_at_session_open() -> None:
    bars = []
    for i in range(25):
        bars.append(_bar(_ts("2026-09-24", "10:{:02d}".format(i)), 10 + i, 0, 10 + i))
    for i in range(25):
        bars.append(_bar(_ts("2026-09-25", "10:{:02d}".format(i)), 40 + i, 30, 40 + i))
    rows = _js(
        "m.calcOrderFlowSeries(c.dataList, c.opts)",
        {"dataList": bars, "opts": {"period": 20, "impulseMult": 1.5, "interval": 1}},
    )
    day2 = rows[25:45]
    assert all(r.get("div") is None for r in day2)


def test_divergence_still_fires_inside_a_session() -> None:
    bars = []
    for i in range(16):
        close = 10.0 + i
        bars.append(_bar(_ts("2026-09-25", f"10:{i:02d}"), close + 5, close, close))
    rows = _js(
        "m.calcOrderFlowSeries(c.dataList, c.opts)",
        {"dataList": bars, "opts": {"period": 5, "impulseMult": 1.5, "interval": 1}},
    )
    marks = [r.get("div") for r in rows[5:] if r.get("div")]
    assert marks[0] == "S"
    assert marks.count("S") == 1


def test_one_minute_mid_close_can_absorb() -> None:
    bars = []
    t0 = _ts("2026-09-25", "10:00")
    for i in range(6):
        bars.append(_bar(t0 + i * 60_000, 10, 0, 10, 100))
    bars.append(_bar(t0 + 6 * 60_000, 10, 0, 5, 200))
    rows = _js(
        "m.calcOrderFlowSeries(c.dataList, c.opts)",
        {"dataList": bars, "opts": {"period": 5, "impulseMult": 1.5, "interval": 1}},
    )
    assert rows[-1]["absorb"] is True
    assert rows[-1]["source"] == "bar"


def test_five_minute_absorb_when_one_minute_delta_disagrees_with_close() -> None:
    raw = []
    data = []
    t0 = 1_200_000  # epoch-aligned 5m
    for b in range(6):
        start = t0 + b * 300
        data.append(
            {
                "timestamp": start * 1000,
                "high": 10,
                "low": 0,
                "close": 10,
                "volume": 500,
            }
        )
        for j in range(5):
            raw.append(
                {
                    "time": start + j * 60,
                    "high": 10,
                    "low": 0,
                    "close": 10,
                    "volume": 100,
                }
            )
    last_start = t0 + 6 * 300
    for j in range(4):
        raw.append(
            {
                "time": last_start + j * 60,
                "high": 10,
                "low": 0,
                "close": 10,
                "volume": 300,
            }
        )
    raw.append(
        {
            "time": last_start + 240,
            "high": 10,
            "low": 0,
            "close": 0,
            "volume": 300,
        }
    )
    data.append(
        {
            "timestamp": last_start * 1000,
            "high": 10,
            "low": 0,
            "close": 0,
            "volume": 1500,
        }
    )
    rows = _js(
        "m.calcOrderFlowSeries(c.dataList, c.opts)",
        {
            "dataList": data,
            "opts": {"period": 5, "impulseMult": 1.5, "interval": 5, "rawBars": raw},
        },
    )
    last = rows[-1]
    assert last["source"] == "1m"
    assert last["delta"] > 0
    assert last["absorb"] is True


def test_chart_loads_orderflow_script() -> None:
    html = (ROOT / "static" / "index.html").read_text()
    main = (ROOT / "atlas_lite" / "main.py").read_text()
    assert 'src="/orderflow.js"' in html
    assert "function calcOrderFlowSeries" not in html
    assert '@app.get("/orderflow.js")' in main
