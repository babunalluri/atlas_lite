"""Multi-day setup memory from agent closes — evidence for the scorecard.

Not an LLM diary: only decayed stats with minimum sample sizes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

MIN_SAMPLES_SOFT = 8
MIN_SAMPLES_HARD = 12
LOOKBACK_DAYS = 12
HALFLIFE_DAYS = 5.0


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _key(side: Any, style: Any) -> str | None:
    s = str(side or "").strip().lower()
    st = str(style or "long").strip().lower() or "long"
    if s not in ("ce", "pe") or st not in ("long", "short"):
        return None
    return f"{st}_{s}"


def _parse_day(raw: Any) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _weight(age_days: float, *, halflife: float = HALFLIFE_DAYS) -> float:
    if age_days <= 0:
        return 1.0
    return float(0.5 ** (age_days / max(0.5, float(halflife))))


def build_setup_memory(
    closes: list[dict[str, Any]] | None,
    *,
    as_of_day: str | date | None = None,
    lookback_days: int = LOOKBACK_DAYS,
) -> dict[str, Any]:
    """Decayed multi-day PnL by setup key (``long_ce`` / ``short_pe`` / …)."""
    closes = list(closes or [])
    if isinstance(as_of_day, date):
        today = as_of_day
    else:
        today = _parse_day(as_of_day) or datetime.now().date()
    floor = today - timedelta(days=max(1, int(lookback_days)))

    acc: dict[str, dict[str, float]] = defaultdict(
        lambda: {"n": 0.0, "wins": 0.0, "net": 0.0, "w_n": 0.0, "w_net": 0.0, "w_wins": 0.0}
    )
    used = 0
    for c in closes:
        day = _parse_day(c.get("day") or c.get("ts"))
        if day is None or day < floor or day > today:
            continue
        key = _key(c.get("side"), c.get("style") or "long")
        pnl = _f(c.get("pnl"))
        if key is None or pnl is None:
            continue
        age = float((today - day).days)
        w = _weight(age)
        row = acc[key]
        row["n"] += 1
        row["net"] += pnl
        row["w_n"] += w
        row["w_net"] += pnl * w
        if pnl > 0:
            row["wins"] += 1
            row["w_wins"] += w
        used += 1

    setups: dict[str, Any] = {}
    score_adjust: dict[str, float] = {}
    avoid: list[dict[str, Any]] = []
    prefer: list[dict[str, Any]] = []

    for key, row in sorted(acc.items()):
        n = int(row["n"])
        w_n = float(row["w_n"])
        if n <= 0 or w_n <= 0:
            continue
        style, side = key.split("_", 1)
        win_rate = round(float(row["wins"]) / n, 3)
        expectancy = round(float(row["w_net"]) / w_n, 2)
        net = round(float(row["net"]), 2)
        setups[key] = {
            "side": side,
            "style": style,
            "n": n,
            "wins": int(row["wins"]),
            "net": net,
            "win_rate": win_rate,
            "expectancy": expectancy,
            "weighted_n": round(w_n, 2),
        }
        # Sample-size gates use raw n; expectancy uses decayed weights.
        hard_avoid = n >= MIN_SAMPLES_HARD and (
            expectancy <= -80 or (win_rate < 0.35 and expectancy < 0)
        )
        hard_prefer = (
            n >= MIN_SAMPLES_HARD and expectancy >= 80 and win_rate >= 0.45
        )
        # Soft nudges only when not already hard-avoided (avoid double penalty).
        if n >= MIN_SAMPLES_SOFT and not hard_avoid:
            if expectancy <= -80:
                score_adjust[key] = -2.5
            elif expectancy <= -40:
                score_adjust[key] = -1.5
            elif expectancy >= 100 and win_rate >= 0.45:
                score_adjust[key] = 1.0
            elif expectancy >= 50 and win_rate >= 0.4:
                score_adjust[key] = 0.5
        if hard_avoid:
            avoid.append(
                {
                    "side": side,
                    "style": style,
                    "reason": (
                        f"multi-day weak {key}: n={n} wr={win_rate} "
                        f"E≈{expectancy}"
                    ),
                    "horizon": "multi_day",
                }
            )
        if hard_prefer:
            prefer.append(
                {
                    "side": side,
                    "style": style,
                    "reason": (
                        f"multi-day edge {key}: n={n} wr={win_rate} "
                        f"E≈{expectancy}"
                    ),
                    "horizon": "multi_day",
                }
            )

    rules: list[str] = []
    for a in avoid:
        rules.append(f"MEMORY AVOID {a['style']} {a['side']}: {a['reason']}")
    for p in prefer[:3]:
        rules.append(f"MEMORY PREFER {p['style']} {p['side']}: {p['reason']}")

    return {
        "ok": True,
        "as_of_day": today.isoformat(),
        "lookback_days": int(lookback_days),
        "halflife_days": HALFLIFE_DAYS,
        "min_samples_soft": MIN_SAMPLES_SOFT,
        "min_samples_hard": MIN_SAMPLES_HARD,
        "closes_used": used,
        "setups": setups,
        "score_adjust": score_adjust,
        "avoid_setups": avoid,
        "prefer_setups": prefer,
        "rules": rules[:8],
    }
