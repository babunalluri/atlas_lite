"""The chart forecast has one implementation: static/pred30.js."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "pred30.js"


def _forecast(
    close: float,
    median: float,
    tight: float,
    slope: float | None,
    bar_minutes: float,
    *,
    side: str,
    up: int,
    down: int,
) -> dict:
    payload = {
        "close": close,
        "median": median,
        "tight": tight,
        "slope": slope,
        "barMinutes": bar_minutes,
        "votes": {"side": side, "up": up, "down": down},
    }
    script = (
        "const m = require(process.argv[1]);"
        "const c = JSON.parse(process.argv[2]);"
        "const out = m.pred30Forecast(c.close, c.median, c.tight, c.slope, c.barMinutes, c.votes);"
        "process.stdout.write(JSON.stringify(out));"
    )
    proc = subprocess.run(
        ["node", "-e", script, str(JS), json.dumps(payload)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def test_chart_script_is_the_only_forecast_path() -> None:
    text = (ROOT / "atlas_lite" / "pred30.py").read_text()
    assert "def forecast_path" not in text
    assert "def blend" not in text
    html = (ROOT / "static" / "index.html").read_text()
    assert 'src="/pred30.js"' in html
    assert "function pred30Forecast" not in html
    assert "function pred30IvWidth" not in html
    assert "function pred30Ensemble" not in html
    assert "function pred30Linreg" not in html
    js = (ROOT / "static" / "pred30.js").read_text()
    assert "function pred30IvWidth" in js
    assert "function pred30Ensemble" in js
    assert "function pred30Forecast" in js


def test_path_is_a_range_around_the_last_price() -> None:
    path = _forecast(23140.0, 37.5, 21.0, 1.2, 5, side="up", up=4, down=0)
    assert path["drift"] == 0
    assert path["target"] == 23140.0
    assert path["conflict"] is False
    assert path["lo"] == 23102.5
    assert path["hi"] == 23177.5
    assert path["tlo"] == 23119.0
    assert path["thi"] == 23161.0
    assert path["text"] == "→ 23140 · 23103–23178"


def test_opposing_votes_do_not_shift_the_path() -> None:
    path = _forecast(23140.0, 37.5, 21.0, -2.0, 5, side="up", up=3, down=1)
    assert path["drift"] == 0
    assert path["target"] == 23140.0
    assert path["text"] == "→ 23140 · 23103–23178"


def test_missing_slope_stays_on_the_last_price() -> None:
    path = _forecast(100.0, 40.0, 20.0, None, 5, side="down", up=0, down=4)
    assert path["drift"] == 0
    assert path["target"] == 100.0
    assert path["tlo"] == 80.0
    assert path["thi"] == 120.0
    assert path["text"] == "→ 100 · 60–140"
