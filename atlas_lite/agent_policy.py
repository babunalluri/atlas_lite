"""Lean regime → books policy: allocation + kill switches (not entry curve-fit).

Rules (evidence / hygiene, not Oct-1 time cutoffs):
- Classify ADX regime (same bands as scorecard).
- Prefer books that fit the regime; skip mismatched books until session end.
- Kill-switch: pause a bleeding book after repeated morning losses or a loss streak.
- Manual gates always win. Never auto-restrict book=agent or impulse_fade.
"""

from __future__ import annotations

import json
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Literal

from zoneinfo import ZoneInfo

from atlas_lite.agent_gates import AgentGateStore, GateMode, normalize_book, session_end_until

IST = ZoneInfo("Asia/Kolkata")
Regime = Literal["trend", "range", "mixed"]


def _as_ist(now: datetime | None = None) -> datetime:
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)

# Match agent_scorecard trending / ranging bands.
ADX_TREND = 22.0
ADX_RANGE = 18.0

MORNING_UNTIL = dt_time(12, 0)
MORNING_LOSS_PAUSE = 2
LOSS_STREAK_PAUSE = 3

# Books policy may restrict. impulse_fade + agent stay allow (impulse earns seat; agent waits alone).
POLICY_BOOKS = (
    "iron_fly",
    "short_straddle",
    "skew_fade",
    "long_iron_condor",
    "short_iron_condor",
    "theta_cliff",
    "combo",
)

KILL_SWITCH_BOOKS = frozenset(
    {"combo", "short_straddle", "skew_fade", "theta_cliff", "short_iron_condor"}
)

BOOK_LEDGERS: dict[str, str] = {
    "iron_fly": "paper_trades.jsonl",
    "short_straddle": "paper_short_straddle.jsonl",
    "skew_fade": "paper_skew_fade.jsonl",
    "long_iron_condor": "paper_long_iron_condor.jsonl",
    "short_iron_condor": "paper_short_iron_condor.jsonl",
    "theta_cliff": "paper_theta_cliff.jsonl",
    "combo": "paper_combo.jsonl",
    "impulse_fade": "paper_impulse_fade.jsonl",
    "agent": "paper_agent.jsonl",
}

REASON_REGIME = "policy:regime:"
REASON_KILL = "policy:kill:"


def classify_regime(
    adx: float | None,
    adx_regime: str | None = None,
) -> Regime:
    """Map ADX / hint into trend | range | mixed."""
    hint = (adx_regime or "").strip().lower()
    if hint in ("trend", "range", "mixed"):
        if hint == "trend":
            return "trend"
        if hint == "range":
            return "range"
        return "mixed"
    if adx is not None:
        if adx >= ADX_TREND:
            return "trend"
        if adx < ADX_RANGE:
            return "range"
    return "mixed"


def regime_book_modes(regime: Regime) -> dict[str, GateMode]:
    """Capital allocation by regime — skip mismatched books, allow fits.

    iron_fly stays allow in every regime (multi-day evidence book).
    skew_fade stays skipped (dead sample). impulse/agent are not managed here.
    """
    if regime == "trend":
        # Combo OK when directional; plain short-premium sits out.
        return {
            "combo": "allow",
            "iron_fly": "allow",
            "short_straddle": "skip_entries",
            "long_iron_condor": "skip_entries",
            "short_iron_condor": "skip_entries",
            "theta_cliff": "skip_entries",
            "skew_fade": "skip_entries",
        }
    if regime == "range":
        # Short premium OK; combo letter-churn sits out.
        return {
            "iron_fly": "allow",
            "short_straddle": "allow",
            "long_iron_condor": "allow",
            "short_iron_condor": "allow",
            "theta_cliff": "allow",
            "combo": "skip_entries",
            "skew_fade": "skip_entries",
        }
    # mixed / chop — prefer evidence books; park weak / twitchy ones.
    # Expiry fence stays allow (only fires on expiry noon + RV filter).
    return {
        "iron_fly": "allow",
        "theta_cliff": "allow",
        "short_iron_condor": "allow",
        "combo": "skip_entries",
        "short_straddle": "skip_entries",
        "long_iron_condor": "skip_entries",
        "skew_fade": "skip_entries",
    }


# Ledger events that book a realized close PnL (trades API + kill switch).
# Short IC uses close_set; theta_cliff / long IC use close_vertical; most books use close.
# Flatten ``close`` rows with pnl=null are markers only — skip those.
_CLOSE_PNL_EVENTS = frozenset({"close", "close_set", "close_vertical"})


def day_close_stats(
    path: Path,
    *,
    day: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Today's closed trades: loss streak (from end), morning losses, day_pnl.

    Counts any ledger event in ``_CLOSE_PNL_EVENTS`` with a non-null ``pnl``.
    Skips seal/flatten ``close`` rows that only carry ``pnl: null``.
    """
    now = _as_ist(now)
    pnls: list[float] = []
    morning_losses = 0
    day_pnl = 0.0
    if not path.is_file():
        return {
            "day": day,
            "n_closes": 0,
            "n_losses": 0,
            "loss_streak": 0,
            "morning_losses": 0,
            "day_pnl": 0.0,
        }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {
            "day": day,
            "n_closes": 0,
            "n_losses": 0,
            "loss_streak": 0,
            "morning_losses": 0,
            "day_pnl": 0.0,
        }
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(ev.get("day") or "") != day:
            continue
        kind = ev.get("event")
        if kind in _CLOSE_PNL_EVENTS and ev.get("pnl") is not None:
            try:
                pnl = float(ev["pnl"])
            except (TypeError, ValueError):
                continue
            pnls.append(pnl)
            if ev.get("day_pnl") is not None:
                try:
                    day_pnl = float(ev["day_pnl"])
                except (TypeError, ValueError):
                    day_pnl = day_pnl + pnl
            else:
                day_pnl = day_pnl + pnl
            ts = str(ev.get("ts") or "")
            try:
                closed_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if closed_at.tzinfo is None:
                    closed_at = closed_at.replace(tzinfo=IST)
                else:
                    closed_at = closed_at.astimezone(IST)
            except ValueError:
                closed_at = now
            if closed_at.time() < MORNING_UNTIL and pnl < 0:
                morning_losses += 1
        elif kind == "close" and ev.get("pnl") is None and ev.get("day_pnl") is not None:
            # Seal/flatten marker (theta / SIC) — refresh day total, not a streak close.
            try:
                day_pnl = float(ev["day_pnl"])
            except (TypeError, ValueError):
                pass
        elif kind == "day_pnl" and ev.get("day_pnl") is not None:
            try:
                day_pnl = float(ev["day_pnl"])
            except (TypeError, ValueError):
                pass

    streak = 0
    for pnl in reversed(pnls):
        if pnl < 0:
            streak += 1
        else:
            break
    return {
        "day": day,
        "n_closes": len(pnls),
        "n_losses": sum(1 for p in pnls if p < 0),
        "loss_streak": streak,
        "morning_losses": morning_losses,
        "day_pnl": round(day_pnl, 2),
    }


def kill_switch_hit(stats: dict[str, Any]) -> str | None:
    """Return reason suffix if kill switch fires, else None."""
    streak = int(stats.get("loss_streak") or 0)
    morning = int(stats.get("morning_losses") or 0)
    day_pnl = float(stats.get("day_pnl") or 0.0)
    if streak >= LOSS_STREAK_PAUSE:
        return f"streak={streak}"
    if morning >= MORNING_LOSS_PAUSE and day_pnl < 0:
        return f"morning_losses={morning} day_pnl={day_pnl}"
    return None


def desired_book_modes(
    *,
    regime: Regime,
    data_dir: Path,
    now: datetime | None = None,
) -> dict[str, tuple[GateMode, str]]:
    """Compute desired mode + reason per managed book (kill > regime)."""
    now = _as_ist(now)
    day = now.date().isoformat()
    out: dict[str, tuple[GateMode, str]] = {}
    base = regime_book_modes(regime)
    for book, mode in base.items():
        out[book] = (mode, f"{REASON_REGIME}{regime}:{mode}")

    for book in KILL_SWITCH_BOOKS:
        ledger = BOOK_LEDGERS.get(book)
        if not ledger:
            continue
        stats = day_close_stats(Path(data_dir) / ledger, day=day, now=now)
        hit = kill_switch_hit(stats)
        if hit:
            out[book] = ("skip_entries", f"{REASON_KILL}{hit}")
    return out


def _is_policy_reason(reason: str | None) -> bool:
    text = str(reason or "")
    return text.startswith(REASON_REGIME) or text.startswith(REASON_KILL)


def apply_book_policy(
    gates: AgentGateStore,
    *,
    data_dir: Path,
    adx: float | None = None,
    adx_regime: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply regime allocation + kill switches. Manual wins; never touch agent/impulse."""
    now = _as_ist(now)
    regime = classify_regime(adx, adx_regime)
    desired = desired_book_modes(regime=regime, data_dir=Path(data_dir), now=now)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for book in POLICY_BOOKS:
        want_mode, want_reason = desired.get(book, ("allow", f"{REASON_REGIME}{regime}:allow"))
        key = normalize_book(book)
        cur = gates.get(key, now=now)
        if str(cur.get("source") or "") == "manual":
            skipped.append({"book": key, "why": "manual_override"})
            continue
        cur_mode = str(cur.get("mode") or "allow").lower()
        cur_reason = str(cur.get("reason") or "")
        # Active kill must not be cleared by a softer regime allow.
        if (
            cur_reason.startswith(REASON_KILL)
            and cur_mode in ("pause", "skip_entries")
            and not want_reason.startswith(REASON_KILL)
        ):
            skipped.append({"book": key, "why": "kill_active"})
            continue
        if cur_mode == want_mode and cur_reason == want_reason:
            skipped.append({"book": key, "why": "unchanged"})
            continue
        if want_mode == "allow" and cur.get("source") == "default" and cur_mode == "allow":
            skipped.append({"book": key, "why": "already_allow"})
            continue
        # Same mode from policy — only rewrite to promote kill.
        if (
            cur_mode == want_mode
            and _is_policy_reason(cur_reason)
            and not want_reason.startswith(REASON_KILL)
        ):
            skipped.append({"book": key, "why": "unchanged"})
            continue

        until = session_end_until(now) if want_mode != "allow" else None
        row = gates.set_gate(
            key,
            want_mode,
            until=until,
            reason=want_reason,
            source="policy",
            now=now,
        )
        applied.append({"book": key, "mode": want_mode, "reason": want_reason, "gate": row})

    return {
        "ok": True,
        "regime": regime,
        "adx": adx,
        "adx_regime": adx_regime,
        "applied": applied,
        "skipped": skipped,
        "desired": {b: {"mode": m, "reason": r} for b, (m, r) in desired.items()},
        "ts": now.isoformat(),
    }
