"""Single entry bell — same gates as the sheet (notebook 17/8/26).

Heuristic starter only — not trading advice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from atlas_lite.metrics import evaluate_sheet
from atlas_lite.specs import CE_PE_BALANCE_PCT, IV_NEAR_DAY_LOW_PCT, OI_NEAR_DAY_HIGH_PCT

IST = ZoneInfo("Asia/Kolkata")


def _f(feed: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        raw = feed.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _hm_minutes(hour: int, minute: int) -> int:
    return hour * 60 + minute


def ce_pe_parity(ce: Optional[float], pe: Optional[float]) -> Tuple[str, Optional[float]]:
    """Return (tight|near|away|unknown, diff_pct of avg premium)."""
    if ce is None or pe is None:
        return "unknown", None
    if ce <= 0 or pe <= 0:
        return "unknown", None
    avg = (abs(ce) + abs(pe)) / 2.0
    if avg <= 0:
        return "unknown", None
    diff_pct = abs(ce - pe) / avg * 100.0
    if diff_pct <= CE_PE_BALANCE_PCT:
        return "tight", round(diff_pct, 3)
    if diff_pct <= 25.0:
        return "near", round(diff_pct, 3)
    return "away", round(diff_pct, 3)


def suggest_strategy(
    feed: Optional[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Bell state from the same notebook gates as ENTRY READY."""
    when = now or datetime.now(IST)
    if when.tzinfo is None:
        when = when.replace(tzinfo=IST)
    else:
        when = when.astimezone(IST)

    feed = feed if isinstance(feed, dict) else {}
    hm = when.strftime("%H:%M")
    mins = _hm_minutes(when.hour, when.minute)
    weekday = when.weekday()
    ev = evaluate_sheet(feed)
    ce = _f(feed, "ce")
    pe = _f(feed, "pe")
    parity, parity_diff = ce_pe_parity(ce, pe)

    base: Dict[str, Any] = {
        "metrics": {
            "hm": hm,
            "ce": ce,
            "pe": pe,
            "parity": parity,
            "parity_diff_pct": parity_diff,
            "adx": _f(feed, "adx"),
            "pcr": _f(feed, "pcr"),
            "ivp": _f(feed, "ivp"),
            "oi_pct_chg": _f(feed, "oi_pct_chg"),
            "oi_vs_day_high": _f(feed, "oi_vs_day_high"),
            "iv_vs_day_low": _f(feed, "iv_vs_day_low"),
        },
        "paper": True,
        "as_of": when.isoformat(timespec="seconds"),
        "strategy": "NOTEBOOK_ENTRY",
        "parity": parity,
        "bell": "idle",
        "passed": ev.get("passed"),
        "gates_total": ev.get("gates_total"),
        "failing_gates": ev.get("failing_gates") or [],
        "missing_gates": ev.get("missing_gates") or [],
    }

    if weekday >= 5:
        return {
            **base,
            "action": "SIT_OUT",
            "confidence": "high",
            "reason": "Weekend — market closed",
            "rules": [],
            "strategy": None,
        }

    if mins < _hm_minutes(9, 15) or mins > _hm_minutes(15, 30):
        return {
            **base,
            "action": "SIT_OUT",
            "confidence": "high",
            "reason": "Outside cash session (%s IST)" % hm,
            "rules": [],
            "strategy": None,
        }

    rules = [
        "ADX < 25",
        "VIX ±2.99 pts",
        "PCR 1.0–1.3",
        "IVP < 70",
        "CE ≈ PE (Δ≤%.0f%%)" % CE_PE_BALANCE_PCT,
        "IV near session low (≥%.1f%%)" % IV_NEAR_DAY_LOW_PCT,
        "FUT OI ≥ %.1f%% of day high" % OI_NEAR_DAY_HIGH_PCT,
    ]

    if ev.get("entry_ready"):
        return {
            **base,
            "action": "ENTER",
            "confidence": "medium",
            "reason": "All notebook gates passing",
            "rules": rules,
            "bell": "enter",
        }

    failing = ev.get("failing_gates") or []
    missing = ev.get("missing_gates") or []
    bits = []
    if failing:
        bits.append("failing: " + ", ".join(failing))
    if missing:
        bits.append("missing: " + ", ".join(missing))
    return {
        **base,
        "action": "SIT_OUT",
        "confidence": "medium",
        "reason": "Gates not ready · " + (" · ".join(bits) if bits else "waiting"),
        "rules": rules,
        "bell": "idle",
    }
