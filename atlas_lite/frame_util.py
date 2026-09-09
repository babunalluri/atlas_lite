"""Shared frame fingerprint for SSE dedupe and change-driven recording."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def frame_revision(frame: dict[str, Any]) -> tuple[Any, ...]:
    """Hash of every UI-visible value (feed, indices, gates, warnings)."""
    parts: list[Any] = []
    feed = frame.get("feed") if isinstance(frame.get("feed"), dict) else {}
    for key in sorted(feed.keys()):
        val = feed[key]
        parts.append(round(float(val), 4) if isinstance(val, (int, float)) else val)

    indices = frame.get("indices")
    if isinstance(indices, list):
        for item in indices:
            if not isinstance(item, dict):
                continue
            for key in ("id", "ltp", "chg_pct", "chg_pts"):
                val = item.get(key)
                parts.append(round(float(val), 4) if isinstance(val, (int, float)) else val)

    evaluation = frame.get("evaluation")
    if isinstance(evaluation, dict):
        parts.append(evaluation.get("entry_ready"))
        parts.append(evaluation.get("passed"))
        parts.append(evaluation.get("evaluable"))
        parts.append(evaluation.get("gates_total"))
        missing = evaluation.get("missing_gates")
        failing = evaluation.get("failing_gates")
        if missing:
            parts.append(tuple(missing))
        if failing:
            parts.append(tuple(failing))
        for row in evaluation.get("rows") or []:
            if not isinstance(row, dict):
                continue
            parts.extend([row.get("id"), row.get("value"), row.get("passed")])

    warnings = frame.get("live_warnings")
    if isinstance(warnings, list) and warnings:
        parts.append(tuple(warnings))

    live_bars = frame.get("live_bars")
    if isinstance(live_bars, list):
        for bar in live_bars[-2:]:
            if not isinstance(bar, dict):
                continue
            for key in ("time", "open", "high", "low", "close"):
                val = bar.get(key)
                parts.append(round(float(val), 4) if isinstance(val, (int, float)) else val)

    hint = frame.get("strategy_hint")
    if isinstance(hint, dict):
        parts.extend(
            [
                hint.get("action"),
                hint.get("strategy"),
                hint.get("confidence"),
                hint.get("reason"),
                hint.get("bell"),
                hint.get("parity"),
            ]
        )

    digest = hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:16]
    return (digest,)
