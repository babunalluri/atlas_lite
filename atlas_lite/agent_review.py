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

    # Hard avoid list for the rest of the day.
    avoid: list[dict[str, Any]] = []
    for key, n_stops in by_key_stops.items():
        style, side = key.split("_", 1)
        if n_stops >= 2:
            avoid.append(
                {
                    "side": side,
                    "style": style,
                    "reason": f"{n_stops} stops today on {key}",
                    "severity": "day",
                }
            )
        elif n_stops >= 1 and by_key_pnl.get(key, 0.0) <= -400.0:
            avoid.append(
                {
                    "side": side,
                    "style": style,
                    "reason": f"stop + day pnl {round(by_key_pnl[key], 2)} on {key}",
                    "severity": "day",
                }
            )
    for key, pnl_sum in by_key_pnl.items():
        if any(a["side"] == key.split("_")[1] and a["style"] == key.split("_")[0] for a in avoid):
            continue
        if pnl_sum <= -600.0 and by_key_n[key] >= 2:
            style, side = key.split("_", 1)
            avoid.append(
                {
                    "side": side,
                    "style": style,
                    "reason": f"repeated losses ({round(pnl_sum, 2)}) on {key}",
                    "severity": "day",
                }
            )

    rules: list[str] = []
    if stops >= 2:
        rules.append(f"{stops} stops today — raise bar; prefer wait unless score is dominant")
    if scratches >= 2:
        rules.append(f"{scratches} scratch/micro exits — stop flipping; hold for trail/target")
    if net <= -1000:
        rules.append(f"day net {net} — defensive: only highest-conviction entries")
    if win_rate is not None and n >= 4 and win_rate < 0.35:
        rules.append(f"win rate {win_rate} on {n} closes — cut frequency")
    for a in avoid:
        rules.append(f"AVOID {a['style']} {a['side']}: {a['reason']}")
    for les in lessons[-5:]:
        kind = str(les.get("kind") or "")
        if kind in ("stop", "loss"):
            rules.append(
                f"lesson {kind} {les.get('style') or 'long'} {les.get('side')}: "
                f"{str(les.get('lesson') or '')[:120]}"
            )

    # Stance must not flip the whole day to "cautious" on one stop / any red print.
    # Thesis avoid + 15m stop block already handle single-setup pain.
    if net <= -1500 or stops >= 3:
        stance = "defensive"
    elif stops >= 2 or scratches >= 2 or net <= -500.0:
        stance = "cautious"
    elif net > 300 and wins >= 2:
        stance = "selective"
    else:
        stance = "normal"

    # Hard day caps — stop the bleed (absolute; does not grow with more closes).
    if stance == "defensive" or net <= -1000:
        max_entries_today: int | None = 8
        min_score = 5.0
    elif stance == "cautious":
        max_entries_today = 12
        min_score = 4.0
    else:
        max_entries_today = None
        min_score = 3.0
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
        "win_rate": win_rate,
        "by_thesis": {
            k: {"n": by_key_n[k], "pnl": round(by_key_pnl[k], 2), "stops": by_key_stops[k]}
            for k in sorted(set(by_key_n) | set(by_key_stops))
        },
        "avoid_today": avoid,
        "rules": rules[:14],
        "stance": stance,
        "min_entry_score_hint": min_score,
        "max_entries_today": max_entries_today,
    }
