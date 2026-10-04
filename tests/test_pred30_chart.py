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


def test_slope_agrees_with_up_vote() -> None:
    path = _forecast(23140.0, 37.5, 21.0, 1.2, 5, side="up", up=3, down=1)
    assert path["drift"] == 7.2
    assert path["target"] == 23147.2
    assert path["conflict"] is False
    assert path["text"] == "↑ 23147 · 23110–23185"


def test_opposing_slope_uses_vote_lean() -> None:
    path = _forecast(23140.0, 37.5, 21.0, -2.0, 5, side="up", up=3, down=1)
    assert path["conflict"] is True
    assert path["drift"] == 9.38
    assert path["text"] == "↑ 23149 · 23112–23187"


def test_vote_only_when_slope_missing() -> None:
    path = _forecast(100.0, 40.0, 20.0, None, 5, side="down", up=0, down=4)
    assert path["conflict"] is False
    assert path["drift"] == -20.0
    assert path["tlo"] == 60.0
    assert path["thi"] == 100.0
    assert path["text"] == "↓ 80 · 40–120"


def test_flat_vote_keeps_slope_path() -> None:
    path = _forecast(23140.0, 37.5, 21.0, 1.2, 5, side="flat", up=2, down=2)
    assert path["drift"] == 7.2
    assert path["text"] == "→ 23147 · 23110–23185"
