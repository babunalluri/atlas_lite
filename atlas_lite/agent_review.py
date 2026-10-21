"""Daily self-review from agent fills — facts the expert must obey next cycle."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


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


def _close_sort_ts(c: dict[str, Any]) -> str:
    return str(c.get("ts") or c.get("closed_at") or "")


def _consecutive_same_setup_stop_keys(closes: list[dict[str, Any]]) -> set[str]:
    """Setups that printed ≥2 stops back-to-back (same side/style) today.

    Tape policy: after that streak, day-lock the failed setup. Raised bar stays
    for other setups — do not auto-lower after later wins / shadow validations.
    """
    locked: set[str] = set()
    ordered = sorted(closes, key=_close_sort_ts)
    streak_key: str | None = None
    streak_n = 0
    for c in ordered:
        if str(c.get("reason") or "") != "stop":
            streak_key = None
            streak_n = 0
            continue
        key = _key(c.get("side"), c.get("style") or "long")
        if key is None:
            streak_key = None
            streak_n = 0
            continue
        if key == streak_key:
            streak_n += 1
        else:
            streak_key = key
            streak_n = 1
        if streak_n >= 2:
            locked.add(key)
    return locked


def _worst_prefix_net(closes: list[dict[str, Any]]) -> float:
    """Most negative running day PnL (so a later recovery cannot relax the bar)."""
    running = 0.0
    worst = 0.0
    saw = False
    for c in sorted(closes, key=_close_sort_ts):
        pnl = _f(c.get("pnl"))
        if pnl is None:
            continue
        saw = True
        running += pnl
        if running < worst:
            worst = running
    return worst if saw else 0.0


def _avoid_has(avoid: list[dict[str, Any]], *, side: str, style: str) -> bool:
    return any(
        str(a.get("side") or "").lower() == side and str(a.get("style") or "long").lower() == style
        for a in avoid
    )


def _append_day_avoid(
    avoid: list[dict[str, Any]],
    *,
    key: str,
    reason: str,
    lock: str = "day",
) -> None:
    style, side = key.split("_", 1)
    if _avoid_has(avoid, side=side, style=style):
        # Prefer the stronger consecutive day-lock reason when present.
        if "consecutive" in reason:
            for a in avoid:
                if (
                    str(a.get("side") or "").lower() == side
                    and str(a.get("style") or "long").lower() == style
                ):
                    a["reason"] = reason
                    a["severity"] = "day"
                    a["horizon"] = "day"
                    a["lock"] = lock
                    break
        return
    avoid.append(
        {
            "side": side,
            "style": style,
            "reason": reason,
            "severity": "day",
            "horizon": "day",
            "lock": lock,
        }
    )


def build_daily_review(
    closes: list[dict[str, Any]] | None,
    *,
    lessons: list[dict[str, Any]] | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """Summarize today's agent closes into hard lessons and a trading stance."""
    closes = list(closes or [])
    lessons = list(lessons or [])
    if day:
        # Strict day match only — do not pull undated / other-day rows into today's review.
        closes = [c for c in closes if str(c.get("day") or "") == day]
        lessons = [les for les in lessons if str(les.get("day") or "") in ("", day)]

    pnls: list[float] = []
    stops = 0
    targets = 0
    trails = 0
    agent_exits = 0
    scratches = 0  # micro exits that barely clear / lose after fees
    by_key_pnl: dict[str, float] = defaultdict(float)
    by_key_stops: Counter[str] = Counter()
    by_key_n: Counter[str] = Counter()

    for c in closes:
        key = _key(c.get("side"), c.get("style") or "long")
        pnl = _f(c.get("pnl"))
        reason = str(c.get("reason") or "")
        if pnl is not None:
            pnls.append(pnl)
            if key:
                by_key_pnl[key] += pnl
                by_key_n[key] += 1
        if reason == "stop":
            stops += 1
            if key:
                by_key_stops[key] += 1
        elif reason == "target":
            targets += 1
        elif reason == "trail":
            trails += 1
        elif reason == "agent_exit":
            agent_exits += 1
        # Scratch = small net after fees (not a real winner). Do not count solid +PnL.
        if pnl is not None and -100.0 <= pnl < 40.0 and reason in ("agent_exit", "time"):
            scratches += 1

    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    net = round(sum(pnls), 2) if pnls else 0.0
    win_rate = round(wins / n, 3) if n else None
    worst_net = round(_worst_prefix_net(closes), 2)

    # Day-lock after ≥2 consecutive same-setup stops (tape-backed; no auto-unlock).
    consec_locks = _consecutive_same_setup_stop_keys(closes)

    # Hard avoid list for the rest of the day.
    avoid: list[dict[str, Any]] = []
    for key in sorted(consec_locks):
        _append_day_avoid(
            avoid,
            key=key,
            reason=f"2 consecutive stops on {key} — day-lock (no re-entry)",
            lock="day",
        )
    for key, n_stops in by_key_stops.items():
        if n_stops >= 2:
            _append_day_avoid(
                avoid,
                key=key,
                reason=f"{n_stops} stops today on {key}",
                lock="day",
            )
        elif n_stops >= 1 and by_key_pnl.get(key, 0.0) <= -400.0:
            _append_day_avoid(
                avoid,
                key=key,
                reason=f"stop + day pnl {round(by_key_pnl[key], 2)} on {key}",
                lock="day",
            )
    for key, pnl_sum in by_key_pnl.items():
        style, side = key.split("_", 1)
        if _avoid_has(avoid, side=side, style=style):
            continue
        if pnl_sum <= -600.0 and by_key_n[key] >= 2:
            _append_day_avoid(
                avoid,
                key=key,
                reason=f"repeated losses ({round(pnl_sum, 2)}) on {key}",
                lock="day",
            )

    day_locked = bool(consec_locks) or any(
        str(a.get("lock") or "") == "day" and "stops today" in str(a.get("reason") or "")
        for a in avoid
    )

    rules: list[str] = []
    if consec_locks:
        for key in sorted(consec_locks):
            style, side = key.split("_", 1)
            rules.append(
                f"DAY-LOCK {style} {side}: 2 consecutive stops — "
                "no re-entry today; other setups need raised bar"
            )
    if stops >= 2:
        rules.append(
            f"{stops} stops today — raise bar; prefer wait unless score is dominant "
            "(bar stays raised for the day; no auto-lower after later wins)"
        )
    if scratches >= 2:
        rules.append(f"{scratches} scratch/micro exits — stop flipping; hold for trail/target")
    if net <= -1000:
        rules.append(f"day net {net} — defensive: only highest-conviction entries")
    if win_rate is not None and n >= 4 and win_rate < 0.35:
        rules.append(f"win rate {win_rate} on {n} closes — cut frequency")
    for a in avoid:
        # DAY-LOCK rules already cover consecutive locks; keep other avoids.
        if "consecutive stops" in str(a.get("reason") or ""):
            continue
        rules.append(f"AVOID {a['style']} {a['side']}: {a['reason']}")
    for les in lessons[-5:]:
        kind = str(les.get("kind") or "")
        if kind in ("stop", "loss"):
            rules.append(
                f"lesson {kind} {les.get('style') or 'long'} {les.get('side')}: "
                f"{str(les.get('lesson') or '')[:120]}"
            )

    # One stop must not raise the bar for every other setup, even when that
    # loss is past -500. The day avoid list already blocks the setup that lost.
    # Day-lock / ≥2 stops keep cautious+ for the rest of the session even if
    # later flips print winners (no shadow-validate → lower-bar path).
    # Use worst prefix net so a mid-day bleed cannot be erased by later wins.
    if worst_net <= -1500 or net <= -1500 or stops >= 3:
        stance = "defensive"
    elif stops >= 2 or scratches >= 2 or day_locked:
        stance = "cautious"
    elif net > 300 and wins >= 2:
        stance = "selective"
    else:
        stance = "normal"

    # Hard day caps — ratchet up only (absolute; later wins must not relax).
    if stance == "defensive" or net <= -1000 or worst_net <= -1000:
        max_entries_today: int | None = 8
        min_score = 5.0
    elif stance == "cautious":
        max_entries_today = 12
        min_score = 4.0
    else:
        max_entries_today = None
        min_score = 3.0
    # Session floors after day-lock / multi-stop: never drop back to 3.0.
    if day_locked or stops >= 2:
        min_score = max(min_score, 4.0)
        if max_entries_today is None:
            max_entries_today = 12
        else:
            max_entries_today = min(max_entries_today, 12)
    if worst_net <= -1000 or net <= -1000 or stops >= 3:
        min_score = max(min_score, 5.0)
        max_entries_today = 8 if max_entries_today is None else min(max_entries_today, 8)
    if max_entries_today is not None:
        rules.append(
            f"stance={stance}: max {max_entries_today} entries today "
            f"(score≥{min_score})"
        )

    return {
        "day": day,
        "closes": n,
        "wins": wins,
        "losses": n - wins,
        "stops": stops,
        "targets": targets,
        "trails": trails,
        "agent_exits": agent_exits,
        "scratches": scratches,
        "net_pnl": net,
        "worst_prefix_net": worst_net,
        "win_rate": win_rate,
        "by_thesis": {
            k: {"n": by_key_n[k], "pnl": round(by_key_pnl[k], 2), "stops": by_key_stops[k]}
            for k in sorted(set(by_key_n) | set(by_key_stops))
        },
        "avoid_today": avoid,
        "day_locked_setups": sorted(consec_locks),
        "rules": rules[:16],
        "stance": stance,
        "min_entry_score_hint": min_score,
        "max_entries_today": max_entries_today,
    }
