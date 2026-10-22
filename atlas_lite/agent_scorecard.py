"""Rank ATM agent candidates from live tape facts (no LLM, no broker orders)."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from atlas_lite.paper_impulse_fade import MIN_IMPULSE

IST = ZoneInfo("Asia/Kolkata")

Candidate = dict[str, Any]

# Default bar for propose_entry recommendation (raised under cautious/defensive stance).
MIN_ENTRY_SCORE = 3.0
# Morning floor (env ATLAS_LITE_AGENT_MORNING_MIN_SCORE). Default = base bar (no extra tighten).
# Set 4.0 to restore the prior morning raise; 0 disables the morning floor.
MORNING_MIN_ENTRY_SCORE = 3.0
MORNING_SCORE_UNTIL = (10, 30)
# Hard veto long premium when spot vs open clearly fights the side.
# Env ATLAS_LITE_AGENT_SPOT_AGAINST_PCT (default 0.20 — was 0.10).
SPOT_AGAINST_LONG_PCT = 0.20
# Trend-long scalper v2 (₹20k): buy with the move, morning window.
TREND_LONG_PTS = 100.0
TREND_LONG_AFTER = (9, 30)  # skip first 15m noise; catch opening drive
TREND_LONG_UNTIL = (12, 0)
TREND_LONG_SCORE = 5.0
# Naked short book (needs ~₹1.5L) — kept for capital upgrade; off by default.
TREND_SHORT_PTS = 100.0
TREND_SHORT_AFTER = (10, 30)
TREND_SHORT_UNTIL = (14, 30)
TREND_SHORT_SCORE = 5.0


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_hhmm(name: str, default: tuple[int, int]) -> tuple[int, int]:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw or ":" not in raw:
        return default
    try:
        h_s, m_s = raw.split(":", 1)
        hour, minute = int(h_s), int(m_s)
    except ValueError:
        return default
    if hour > 23 or minute > 59:
        return default
    return hour, minute


def morning_min_entry_score() -> float:
    """Morning min score floor; ≤0 disables. Env ATLAS_LITE_AGENT_MORNING_MIN_SCORE."""
    return _env_float("ATLAS_LITE_AGENT_MORNING_MIN_SCORE", MORNING_MIN_ENTRY_SCORE)


def morning_score_until() -> tuple[int, int]:
    """Morning window end (HH:MM IST). Env ATLAS_LITE_AGENT_MORNING_SCORE_UNTIL."""
    return _env_hhmm("ATLAS_LITE_AGENT_MORNING_SCORE_UNTIL", MORNING_SCORE_UNTIL)


def spot_against_long_pct() -> float:
    """Spot-vs-open hard veto threshold (%). Env ATLAS_LITE_AGENT_SPOT_AGAINST_PCT."""
    return max(0.0, _env_float("ATLAS_LITE_AGENT_SPOT_AGAINST_PCT", SPOT_AGAINST_LONG_PCT))


def agent_longs_enabled() -> bool:
    """Long CE/PE entries (env ATLAS_LITE_AGENT_ALLOW_LONG). Compose enables for scalper v2."""
    raw = str(os.environ.get("ATLAS_LITE_AGENT_ALLOW_LONG", "0") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def agent_shorts_enabled() -> bool:
    """Naked short entries (env ATLAS_LITE_AGENT_ALLOW_SHORT). Default off — needs ~₹1.5L."""
    raw = str(os.environ.get("ATLAS_LITE_AGENT_ALLOW_SHORT", "0") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def trend_long_pts() -> float:
    """NIFTY points vs open to arm buy-with-the-move. Env ATLAS_LITE_AGENT_TREND_LONG_PTS."""
    return max(0.0, _env_float("ATLAS_LITE_AGENT_TREND_LONG_PTS", TREND_LONG_PTS))


def trend_long_after() -> tuple[int, int]:
    return _env_hhmm("ATLAS_LITE_AGENT_TREND_LONG_AFTER", TREND_LONG_AFTER)


def trend_long_until() -> tuple[int, int]:
    return _env_hhmm("ATLAS_LITE_AGENT_TREND_LONG_UNTIL", TREND_LONG_UNTIL)


def trend_short_pts() -> float:
    """NIFTY points vs open to arm sell-the-move. Env ATLAS_LITE_AGENT_TREND_SHORT_PTS."""
    return max(0.0, _env_float("ATLAS_LITE_AGENT_TREND_SHORT_PTS", TREND_SHORT_PTS))


def trend_short_after() -> tuple[int, int]:
    return _env_hhmm("ATLAS_LITE_AGENT_TREND_SHORT_AFTER", TREND_SHORT_AFTER)


def trend_short_until() -> tuple[int, int]:
    return _env_hhmm("ATLAS_LITE_AGENT_TREND_SHORT_UNTIL", TREND_SHORT_UNTIL)


def _spot_pts_vs_open(context: dict[str, Any]) -> tuple[float | None, float | None]:
    spot = _num(context.get("spot"))
    if spot is None:
        raw_spot = context.get("spot")
        if isinstance(raw_spot, dict):
            spot = _num(raw_spot.get("value"))
    spot_dir = _num(context.get("spot_chg_open_pct"))
    if spot is None or spot <= 0 or spot_dir is None:
        return None, None
    pts = float(spot) * float(spot_dir) / 100.0
    return float(spot), pts


def trend_long_signal(
    context: dict[str, Any],
) -> dict[str, Any] | None:
    """09:30–12:00, |spot−open| ≥ 100 pts → buy CE (up) / buy PE (down). Never against trend."""
    if not agent_longs_enabled():
        return None
    now = _context_ist(context)
    if now is None:
        return None
    after = trend_long_after()
    until = trend_long_until()
    hm = (now.hour, now.minute)
    if hm < after or hm > until:
        return None
    thr = float(trend_long_pts())
    if thr <= 0:
        return None
    _spot, pts = _spot_pts_vs_open(context)
    if pts is None:
        return None
    if pts >= thr:
        return {
            "side": "ce",
            "style": "long",
            "score": float(TREND_LONG_SCORE),
            "spot_pts_open": round(pts, 1),
            "setup": "trend_long",
            "reasons": [
                f"trend_long: spot {pts:.0f} pts vs open (≥+{thr:.0f}) — buy CE",
            ],
        }
    if pts <= -thr:
        return {
            "side": "pe",
            "style": "long",
            "score": float(TREND_LONG_SCORE),
            "spot_pts_open": round(pts, 1),
            "setup": "trend_long",
            "reasons": [
                f"trend_long: spot {pts:.0f} pts vs open (≤−{thr:.0f}) — buy PE",
            ],
        }
    return None


def trend_short_signal(
    context: dict[str, Any],
) -> dict[str, Any] | None:
    """10:30–14:30, |spot−open| ≥ 100 pts → short CE (down) / short PE (up). Off unless ALLOW_SHORT."""
    if not agent_shorts_enabled():
        return None
    now = _context_ist(context)
    if now is None:
        return None
    after = trend_short_after()
    until = trend_short_until()
    hm = (now.hour, now.minute)
    if hm < after or hm > until:
        return None
    thr = float(trend_short_pts())
    if thr <= 0:
        return None
    _spot, pts = _spot_pts_vs_open(context)
    if pts is None:
        return None
    if pts <= -thr:
        return {
            "side": "ce",
            "style": "short",
            "score": float(TREND_SHORT_SCORE),
            "spot_pts_open": round(pts, 1),
            "setup": "trend_short",
            "reasons": [
                f"trend_short: spot {pts:.0f} pts vs open (≤−{thr:.0f}) — sell CE",
            ],
        }
    if pts >= thr:
        return {
            "side": "pe",
            "style": "short",
            "score": float(TREND_SHORT_SCORE),
            "spot_pts_open": round(pts, 1),
            "setup": "trend_short",
            "reasons": [
                f"trend_short: spot {pts:.0f} pts vs open (≥+{thr:.0f}) — sell PE",
            ],
        }
    return None


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _has_agreement(
    *,
    side: str,
    style: str,
    combo_regime: str,
    combo_signal: str,
    impulse_fade: str,
    impulse_ok: bool,
    spot_dir: float | None,
    di_bull: bool,
    di_bear: bool,
    trending: bool,
    ranging: bool,
    skew_rich: float | None,
) -> bool:
    """Confirming tape — intraday direction / DI first; combo optional.

    ``spot_dir`` must be vs session open (not yesterday). ADX alone has no side.
    """
    if style == "long":
        if side == "ce" and spot_dir is not None and spot_dir >= 0.15:
            return True
        if side == "pe" and spot_dir is not None and spot_dir <= -0.15:
            return True
        if trending and di_bull and side == "ce":
            return True
        if trending and di_bear and side == "pe":
            return True
        if combo_regime == "B" and side == "ce":
            return True
        if combo_regime == "S" and side == "pe":
            return True
        if combo_signal == "B" and side == "ce":
            return True
        if combo_signal == "S" and side == "pe":
            return True
        if impulse_ok and impulse_fade == side:
            if side == "ce" and (spot_dir is None or spot_dir >= -0.05):
                return True
            if side == "pe" and (spot_dir is None or spot_dir <= 0.05):
                return True
        return False
    if ranging:
        return True
    if skew_rich is not None:
        if side == "ce" and skew_rich >= 8:
            return True
        if side == "pe" and skew_rich <= -8:
            return True
    if combo_regime == "B" and side == "pe":
        return True
    if combo_regime == "S" and side == "ce":
        return True
    if combo_signal == "B" and side == "pe":
        return True
    if combo_signal == "S" and side == "ce":
        return True
    return False


def entry_permitted(scorecard: dict[str, Any], *, side: str, style: str) -> tuple[bool, str]:
    """Hard gate used by propose_entry — must match an eligible scorecard candidate."""
    side_n = str(side or "").strip().lower()
    style_n = str(style or "long").strip().lower() or "long"
    if side_n not in ("ce", "pe") or style_n not in ("long", "short"):
        return False, "invalid_side_or_style"
    rec = scorecard.get("recommended") if isinstance(scorecard, dict) else None
    if not isinstance(rec, dict):
        return False, "no_scorecard"
    max_entries = scorecard.get("max_entries_today")
    entries_today = scorecard.get("entries_today")
    try:
        if (
            max_entries is not None
            and entries_today is not None
            and int(entries_today) >= int(max_entries)
        ):
            return False, "day_entry_cap"
    except (TypeError, ValueError):
        pass
    if rec.get("action") != "propose_entry":
        return False, "scorecard_wait"
    if str(rec.get("side") or "") == side_n and str(rec.get("style") or "") == style_n:
        # Recommended rows are already filtered, but re-check block flags if present.
        if rec.get("blocked"):
            return False, "thesis_or_lesson_blocked"
        if rec.get("agreement") is False:
            return False, "no_agreement"
        return True, "recommended"
    # Allow only if this exact candidate is eligible (not blocked, agreement, score≥min).
    min_score = float(scorecard.get("min_entry_score") or MIN_ENTRY_SCORE)
    for row in scorecard.get("candidates") or []:
        if row.get("action") != "propose_entry":
            continue
        if str(row.get("side")) != side_n or str(row.get("style")) != style_n:
            continue
        if row.get("blocked"):
            return False, "thesis_or_lesson_blocked"
        if not row.get("agreement"):
            return False, "no_agreement"
        if float(row.get("score") or 0) < min_score:
            return False, "weak_score"
        return True, "eligible_candidate"
    return False, "not_in_scorecard"


def _context_ist(context: dict[str, Any]) -> datetime | None:
    """Best-effort IST clock from context (as_of / clock.ist)."""
    clock = context.get("clock") if isinstance(context.get("clock"), dict) else {}
    raw = str(clock.get("ist") or context.get("as_of") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _morning_score_window(now: datetime | None) -> bool:
    if now is None:
        return False
    hh, mm = morning_score_until()
    return (now.hour, now.minute) < (hh, mm)


def build_strategy_scorecard(
    context: dict[str, Any],
    *,
    agent_book: dict[str, Any] | None = None,
    thesis_block: dict[str, Any] | None = None,
    daily_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score long/short CE/PE (+ wait) from agent tape (spot/ADX/skew) + optional books.

    Combo/impulse are soft cues only — pausing those books must not strand the agent.
    Code gates (tape, cooldown, thesis_block, fee floors, weak_score) still apply.
    """
    agent_book = agent_book or {}
    thesis_block = thesis_block if thesis_block is not None else agent_book.get("thesis_block")
    daily_review = daily_review or {}
    min_entry_score = float(daily_review.get("min_entry_score_hint") or MIN_ENTRY_SCORE)
    min_entry_score = max(MIN_ENTRY_SCORE, min_entry_score)
    now_ist = _context_ist(context)
    morn_floor = float(morning_min_entry_score())
    if morn_floor > 0 and _morning_score_window(now_ist):
        min_entry_score = max(min_entry_score, morn_floor)
    max_entries_today = daily_review.get("max_entries_today")
    try:
        entries_today = int(agent_book.get("entries_today") or 0)
    except (TypeError, ValueError):
        entries_today = 0
    try:
        at_entry_cap = (
            max_entries_today is not None and entries_today >= int(max_entries_today)
        )
    except (TypeError, ValueError):
        at_entry_cap = False

    # Intraday vs open only — never fall back to day chg (that reopens CE-into-selloff).
    # Missing open → no directional credit until quote OHLC arrives.
    spot_dir = _num(context.get("spot_chg_open_pct"))
    spot_chg_day = _num(context.get("spot_chg_pct"))
    adx = _num(context.get("adx"))
    adx_regime = (_str(context.get("adx_regime")) or "").lower()
    pdi = _num(context.get("pdi"))
    if pdi is None:
        pdi = _num(context.get("plus_di"))
    mdi = _num(context.get("mdi"))
    if mdi is None:
        mdi = _num(context.get("minus_di"))
    di_bull = pdi is not None and mdi is not None and pdi > mdi
    di_bear = pdi is not None and mdi is not None and mdi > pdi
    # Prefer forward-adjusted richness; rebuild from raw if needed.
    # Parity: CE−PE ≈ F_opt−K. Never use raw monthly fut as the option forward.
    skew_rich = _num(context.get("ce_pe_skew_rich"))
    skew_raw = _num(context.get("ce_pe_skew"))
    if skew_rich is None and skew_raw is not None:
        from atlas_lite.metrics import option_carry_from_fut

        spot = _num(context.get("spot"))
        atm = _num(context.get("atm"))
        forward = _num(context.get("forward"))
        if forward is None and spot is not None:
            carry = _num(context.get("carry_pts"))
            if carry is not None:
                forward = spot + carry
            else:
                forward, _carry = option_carry_from_fut(
                    spot,
                    _num(context.get("fut")),
                    option_dte=_num(context.get("days_to_expiry")),
                    fut_dte=_num(context.get("fut_days_to_expiry")),
                )
        if forward is not None and atm is not None:
            skew_rich = round(skew_raw - (forward - atm), 2)
        else:
            skew_rich = None  # do not treat raw moneyness/carry gap as "rich"
    pcr = _num(context.get("pcr"))

    combo = context.get("combo") if isinstance(context.get("combo"), dict) else {}
    impulse = context.get("impulse") if isinstance(context.get("impulse"), dict) else {}
    # Lasting confluence regime (B/S). signal is only set on the flip minute.
    combo_regime = (_str(combo.get("side") or combo.get("regime")) or "").upper()
    combo_signal = (_str(combo.get("signal")) or "").upper()
    combo_open = combo.get("position") is not None
    impulse_delta = _num(impulse.get("delta") if "delta" in impulse else impulse.get("impulse"))
    impulse_fade = (_str(impulse.get("fade_side")) or "").lower()  # ce / pe to long for fade
    impulse_open = impulse.get("position") is not None

    trending = (adx is not None and adx >= 22) or adx_regime == "trend"
    ranging = (adx is not None and adx < 18) or adx_regime == "range"

    scores = {
        "long_ce": 0.0,
        "long_pe": 0.0,
        "short_ce": 0.0,
        "short_pe": 0.0,
        "wait": 0.0,
    }
    why: dict[str, list[str]] = {k: [] for k in scores}

    def add(key: str, pts: float, note: str) -> None:
        scores[key] = round(scores[key] + pts, 2)
        why[key].append(f"{note} ({pts:+.1f})")

    # Intraday direction (vs open). Day chg alone must not buy into a selloff.
    if spot_dir is not None:
        if spot_dir >= 0.15:
            add("long_ce", 2.0, f"spot_chg_open_pct={spot_dir} bullish intraday")
            add("short_pe", 1.0, f"spot_chg_open_pct={spot_dir} supports short puts")
            add("long_pe", -1.5, f"spot_chg_open_pct={spot_dir} against long puts")
        elif spot_dir <= -0.15:
            add("long_pe", 2.0, f"spot_chg_open_pct={spot_dir} bearish intraday")
            add("short_ce", 1.0, f"spot_chg_open_pct={spot_dir} supports short calls")
            add("long_ce", -1.5, f"spot_chg_open_pct={spot_dir} against long calls")
        else:
            add("wait", 0.5, f"spot_chg_open_pct={spot_dir} flat intraday")
    if (
        spot_chg_day is not None
        and spot_dir is not None
        and spot_chg_day >= 0.15
        and spot_dir <= -0.10
    ):
        add("long_ce", -2.0, f"day green ({spot_chg_day}) but selling vs open ({spot_dir})")
        add("wait", 0.5, "intraday selloff despite green day")
    elif (
        spot_chg_day is not None
        and spot_dir is not None
        and spot_chg_day <= -0.15
        and spot_dir >= 0.10
    ):
        add("long_pe", -2.0, f"day red ({spot_chg_day}) but buying vs open ({spot_dir})")
        add("wait", 0.5, "intraday bounce despite red day")

    # ADX = strength only; side from +DI/-DI (fallback: intraday spot).
    if trending:
        if di_bull:
            add("long_ce", 1.5, f"adx={adx} +DI>{mdi} trending up")
            add("long_pe", -1.0, "−DI weaker — against long PE")
        elif di_bear:
            add("long_pe", 1.5, f"adx={adx} −DI>{pdi} trending down")
            add("long_ce", -1.0, "+DI weaker — against long CE")
        elif spot_dir is not None and spot_dir >= 0.05:
            add("long_ce", 1.0, f"adx={adx} trending; intraday up")
        elif spot_dir is not None and spot_dir <= -0.05:
            add("long_pe", 1.0, f"adx={adx} trending; intraday down")
        add("short_ce", -1.0, "trending — avoid short premium")
        add("short_pe", -1.0, "trending — avoid short premium")
    elif ranging:
        add("short_ce", 1.5, f"adx={adx} / range — short premium ok")
        add("short_pe", 1.5, f"adx={adx} / range — short premium ok")
        add("long_ce", -0.5, "range — long premium weaker")
        add("long_pe", -0.5, "range — long premium weaker")
        add("wait", 0.5, "range — wait is valid if signals conflict")

    # Optional combo cues (book may be paused — absence is fine).
    if combo_regime == "B":
        add("long_ce", 1.5, "combo regime=B (optional)")
        add("short_pe", 0.5, "combo regime=B supports short PE")
        add("long_pe", -1.0, "combo regime=B against long PE")
    elif combo_regime == "S":
        add("long_pe", 1.5, "combo regime=S (optional)")
        add("short_ce", 0.5, "combo regime=S supports short CE")
        add("long_ce", -1.0, "combo regime=S against long CE")
    if combo_signal == "B":
        add("long_ce", 0.5, "combo fresh flip signal=B")
    elif combo_signal == "S":
        add("long_pe", 0.5, "combo fresh flip signal=S")

    # Optional impulse fade cue.
    impulse_ok = impulse_delta is not None and abs(float(impulse_delta)) >= float(MIN_IMPULSE)
    if impulse_ok and impulse_fade == "ce":
        add("long_ce", 1.0, f"impulse fade_side=ce (delta={impulse_delta})")
        add("long_pe", -0.5, "impulse fade against long PE")
    elif impulse_ok and impulse_fade == "pe":
        add("long_pe", 1.0, f"impulse fade_side=pe (delta={impulse_delta})")
        add("long_ce", -0.5, "impulse fade against long CE")

    # Skew richness (moneyness-adjusted). Raw CE−PE alone is mostly spot vs ATM.
    if skew_rich is not None:
        if skew_rich >= 8:
            add("short_ce", 1.5, f"ce_pe_skew_rich={skew_rich} calls rich")
            add("long_pe", 0.5, f"ce_pe_skew_rich={skew_rich} puts relatively cheap")
        elif skew_rich <= -8:
            add("short_pe", 1.5, f"ce_pe_skew_rich={skew_rich} puts rich")
            add("long_ce", 0.5, f"ce_pe_skew_rich={skew_rich} calls relatively cheap")

    # PCR soft cue
    if pcr is not None:
        if pcr >= 1.2:
            add("long_ce", 0.5, f"pcr={pcr} put-heavy (contrarian soft)")
        elif pcr <= 0.7:
            add("long_pe", 0.5, f"pcr={pcr} call-heavy (contrarian soft)")

    # Other books open: soft note only (user may leave them on intentionally).
    if combo_open:
        add("wait", 0.25, "combo book has open risk (informational)")
    if impulse_open:
        add("wait", 0.25, "impulse book has open risk (informational)")

    # 1m structure / trap cues (code-computed). Soft only — never invent beyond fields.
    structure = context.get("structure") if isinstance(context.get("structure"), dict) else {}
    if structure.get("ok"):
        trap = (_str(structure.get("trap")) or "").lower()
        pin = (_str(structure.get("pin")) or "").lower()
        engulf = (_str(structure.get("engulfing")) or "").lower()
        bias = (_str(structure.get("bias")) or "").lower()
        bearish_traps = {
            "bull_trap",
            "eqh_liquidity_grab",
            "vp_reject_from_hvn_bearish",
        }
        bullish_traps = {
            "bear_trap",
            "eql_liquidity_grab",
            "vp_reject_from_hvn_bullish",
        }
        # Quieter weights; in a trend, traps only veto chasing the fade side (no flip boost).
        trap_boost = 0.75 if ranging else (0.25 if trending else 0.5)
        trap_penalty = 1.0 if ranging else (0.5 if trending else 0.75)
        if trap in bearish_traps:
            if ranging or not trending:
                add("long_pe", trap_boost, f"structure {trap}")
                add("short_ce", round(trap_boost * 0.6, 2), f"structure {trap} supports short CE")
            add("long_ce", -trap_penalty, f"structure {trap} — do not chase long CE")
        elif trap in bullish_traps:
            if ranging or not trending:
                add("long_ce", trap_boost, f"structure {trap}")
                add("short_pe", round(trap_boost * 0.6, 2), f"structure {trap} supports short PE")
            add("long_pe", -trap_penalty, f"structure {trap} — do not chase long PE")
        if pin == "bullish_rejection":
            add("long_ce", 0.5, "structure pin=bullish_rejection")
            add("short_pe", 0.25, "structure pin supports short PE")
        elif pin == "bearish_rejection":
            add("long_pe", 0.5, "structure pin=bearish_rejection")
            add("short_ce", 0.25, "structure pin supports short CE")
        if engulf == "bullish_engulfing":
            add("long_ce", 0.4, "structure bullish_engulfing")
        elif engulf == "bearish_engulfing":
            add("long_pe", 0.4, "structure bearish_engulfing")
        # Multi-TF order blocks + liquidity pools (soft).
        for ob in structure.get("order_blocks") or []:
            if not isinstance(ob, dict):
                continue
            side = (_str(ob.get("side")) or "").lower()
            rel = (_str(ob.get("spot_rel")) or "").lower()
            tf = _str(ob.get("tf")) or "?"
            if side == "demand" and rel == "inside":
                add("long_ce", 0.5, f"structure demand_ob {tf} (spot inside)")
                add("long_pe", -0.25, f"structure demand_ob {tf}")
            elif side == "supply" and rel == "inside":
                add("long_pe", 0.5, f"structure supply_ob {tf} (spot inside)")
                add("long_ce", -0.25, f"structure supply_ob {tf}")
        pools = structure.get("liquidity_pools") if isinstance(structure.get("liquidity_pools"), dict) else {}
        nearest = pools.get("nearest") if isinstance(pools, dict) else None
        if isinstance(nearest, dict) and nearest.get("dist_pts") is not None:
            try:
                dist = float(nearest.get("dist_pts"))
            except (TypeError, ValueError):
                dist = None
            kind = (_str(nearest.get("kind")) or "").lower()
            if dist is not None and dist <= 15:
                if kind == "equal_highs" and nearest.get("above"):
                    add("wait", 0.25, f"near equal_highs liquidity @{nearest.get('level')}")
                elif kind == "equal_lows" and not nearest.get("above"):
                    add("wait", 0.25, f"near equal_lows liquidity @{nearest.get('level')}")
        intent = structure.get("intent_proxy") if isinstance(structure.get("intent_proxy"), dict) else {}
        intent_label = (_str(intent.get("label")) or "").lower()
        if intent_label == "initiative_up" and not trending:
            add("long_ce", 0.25, "intent_proxy initiative_up")
        elif intent_label == "initiative_down" and not trending:
            add("long_pe", 0.25, "intent_proxy initiative_down")
        elif intent_label == "absorption":
            add("wait", 0.25, "intent_proxy absorption — raise bar")
        if bias == "compression":
            add("short_ce", 0.5, "structure compression — short premium soft")
            add("short_pe", 0.5, "structure compression — short premium soft")
            add("wait", 0.5, "structure compression — wait ok")
        elif bias == "bullish_structure" and trap not in bearish_traps and not ranging:
            add("long_ce", 0.25, "structure bias=bullish_structure")
        elif bias == "bearish_structure" and trap not in bullish_traps and not ranging:
            add("long_pe", 0.25, "structure bias=bearish_structure")

    # Thesis block(s) + daily avoid list — hard-exclude those keys.
    blocked_keys: set[str] = set()
    raw_blocks: list[Any] = []
    book_blocks = agent_book.get("thesis_blocks")
    if isinstance(book_blocks, list):
        raw_blocks.extend(b for b in book_blocks if isinstance(b, dict))
    if isinstance(thesis_block, list):
        raw_blocks.extend(b for b in thesis_block if isinstance(b, dict))
    elif isinstance(thesis_block, dict) and thesis_block.get("side"):
        raw_blocks.append(thesis_block)
    for block in raw_blocks:
        block_side = (_str(block.get("side")) or "").lower()
        block_style = (_str(block.get("style")) or "long").lower()
        if block_side not in ("ce", "pe"):
            continue
        k = f"{block_style}_{block_side}"
        if k in blocked_keys:
            continue
        blocked_keys.add(k)
        add(k, -8.0, f"thesis_block {block_style} {block_side}")
    for row in daily_review.get("avoid_today") or []:
        if not isinstance(row, dict):
            continue
        k = f"{str(row.get('style') or 'long').lower()}_{str(row.get('side') or '').lower()}"
        if k in scores:
            blocked_keys.add(k)
            horizon = str(
                row.get("horizon") or row.get("severity") or row.get("lock") or "day"
            )
            add(k, -10.0, f"{horizon} avoid: {row.get('reason')}")
    # Hard veto: do not long premium against clear spot-vs-open direction.
    veto_pct = float(spot_against_long_pct())
    if spot_dir is not None and veto_pct > 0:
        if spot_dir <= -veto_pct:
            blocked_keys.add("long_ce")
            add(
                "long_ce",
                -10.0,
                f"spot_chg_open_pct={spot_dir} against long CE — hard veto",
            )
        if spot_dir >= veto_pct:
            blocked_keys.add("long_pe")
            add(
                "long_pe",
                -10.0,
                f"spot_chg_open_pct={spot_dir} against long PE — hard veto",
            )
    # Scalper v2: longs on by default. Naked shorts only when ALLOW_SHORT.
    longs_on = agent_longs_enabled()
    shorts_on = agent_shorts_enabled()
    if not longs_on:
        for key in ("long_ce", "long_pe"):
            if key not in blocked_keys:
                blocked_keys.add(key)
                add(key, -10.0, "longs disabled")
    if not shorts_on:
        for key in ("short_ce", "short_pe"):
            if key not in blocked_keys:
                blocked_keys.add(key)
                add(key, -10.0, "shorts disabled — scalper v2 long book")
    # Prefer trend-long (buy with move); trend-short kept for capital upgrade.
    trend_long = trend_long_signal(context)
    trend_short = trend_short_signal(context)
    trend = trend_long or trend_short
    if trend is not None:
        t_key = f"{trend['style']}_{trend['side']}"
        add(
            t_key,
            3.0,
            (trend.get("reasons") or [str(trend.get("setup") or "trend")])[0],
        )
    # Multi-day setup memory: soft score nudges (hard avoids already in avoid_today).
    score_adjust = daily_review.get("score_adjust") or {}
    if isinstance(score_adjust, dict):
        for key, pts in score_adjust.items():
            k = str(key or "")
            if k not in scores:
                continue
            try:
                delta = float(pts)
            except (TypeError, ValueError):
                continue
            if abs(delta) < 0.05:
                continue
            add(k, delta, f"setup_memory {k} adjust")
    for rule in daily_review.get("rules") or []:
        add("wait", 0.5, f"review: {str(rule)[:80]}")

    stance = str(daily_review.get("stance") or "normal")
    if stance in ("cautious", "defensive"):
        add("wait", 1.5 if stance == "cautious" else 2.5, f"stance={stance}")

    # Agent already open → wait / manage, don't re-rank entries as primary
    agent_pos = agent_book.get("position")
    if agent_pos:
        add("wait", 5.0, "agent already open — manage exit/hold, do not re-enter")
    if at_entry_cap:
        add(
            "wait",
            5.0,
            f"day entry cap reached ({entries_today}/{max_entries_today})",
        )

    ranked: list[Candidate] = []
    for key, score in scores.items():
        if key == "wait":
            ranked.append(
                {
                    "action": "wait",
                    "side": None,
                    "style": None,
                    "score": score,
                    "reasons": why[key][:6],
                    "blocked": False,
                    "agreement": True,
                }
            )
            continue
        style, side = key.split("_", 1)
        agreed = _has_agreement(
            side=side,
            style=style,
            combo_regime=combo_regime,
            combo_signal=combo_signal,
            impulse_fade=impulse_fade,
            impulse_ok=impulse_ok,
            spot_dir=spot_dir,
            di_bull=di_bull,
            di_bear=di_bear,
            trending=trending,
            ranging=ranging,
            skew_rich=skew_rich,
        )
        if not agreed:
            add(key, -1.0, "no confirming agreement (intraday/DI/skew/range)")
            score = scores[key]
        ranked.append(
            {
                "action": "propose_entry",
                "side": side,
                "style": style,
                "score": score,
                "reasons": why[key][:8],
                "blocked": key in blocked_keys,
                "agreement": agreed,
            }
        )
    ranked.sort(key=lambda r: float(r["score"]), reverse=True)

    wait_row = next((r for r in ranked if r["action"] == "wait"), None)
    # Eligible: not blocked, has agreement, score ≥ min_entry_score.
    top_entry = next(
        (
            r
            for r in ranked
            if r["action"] == "propose_entry"
            and not r.get("blocked")
            and r.get("agreement")
            and float(r["score"]) >= min_entry_score
        ),
        None,
    )
    if agent_pos:
        chosen = {
            "action": "manage",
            "side": agent_pos.get("side"),
            "style": agent_pos.get("style") or "long",
            "score": None,
            "reasons": [
                "position open",
                f"net={agent_pos.get('net')}",
                f"exit_allowed={agent_pos.get('exit_allowed')}",
            ],
            "exit_allowed": agent_pos.get("exit_allowed"),
        }
    elif at_entry_cap:
        chosen = {
            "action": "wait",
            "side": None,
            "style": None,
            "score": float(wait_row["score"]) if wait_row else 0.0,
            "reasons": list((wait_row or {}).get("reasons") or [])
            + [f"day entry cap {entries_today}/{max_entries_today} — no new entries"],
        }
    elif top_entry is None:
        chosen = {
            "action": "wait",
            "side": None,
            "style": None,
            "score": float(wait_row["score"]) if wait_row else 0.0,
            "reasons": list((wait_row or {}).get("reasons") or [])
            + [
                f"no eligible entry (need agreement + score≥{min_entry_score}, not blocked)"
            ],
        }
    else:
        chosen = top_entry

    # Prefer the trend trigger when armed (overrides weaker scorecard picks).
    if (
        trend is not None
        and not agent_pos
        and not at_entry_cap
        and str(trend.get("side") or "") in ("ce", "pe")
    ):
        t_side = str(trend["side"])
        t_style = str(trend.get("style") or "long")
        t_setup = str(trend.get("setup") or ("trend_long" if t_style == "long" else "trend_short"))
        t_key = f"{t_style}_{t_side}"
        if t_key not in blocked_keys:
            cand = next(
                (
                    r
                    for r in ranked
                    if r.get("side") == t_side and r.get("style") == t_style
                ),
                None,
            )
            reasons = list(trend.get("reasons") or [])
            if cand is not None:
                reasons = reasons + list(cand.get("reasons") or [])[:4]
            setup_score = (
                float(TREND_LONG_SCORE) if t_setup == "trend_long" else float(TREND_SHORT_SCORE)
            )
            chosen = {
                "action": "propose_entry",
                "side": t_side,
                "style": t_style,
                "score": max(
                    setup_score,
                    float((cand or {}).get("score") or 0.0),
                ),
                "reasons": reasons[:8],
                "blocked": False,
                "agreement": True,
                "setup": t_setup,
            }

    # Scalper / short books: only the trend setup may enter — no other scorecard picks.
    if not agent_pos:
        allowed_setup = None
        wait_msg = ""
        if longs_on and not shorts_on:
            allowed_setup = "trend_long"
            wait_msg = (
                "awaiting trend_long (≥100pt vs open, 09:30–12:00) — "
                "scorecard entries disabled"
            )
        elif shorts_on and not longs_on:
            allowed_setup = "trend_short"
            wait_msg = (
                "awaiting trend_short (≥100pt vs open, 10:30–14:30) — "
                "scorecard entries disabled"
            )
        if (
            allowed_setup
            and chosen.get("action") == "propose_entry"
            and chosen.get("setup") != allowed_setup
        ):
            chosen = {
                "action": "wait",
                "side": None,
                "style": None,
                "score": float(wait_row["score"]) if wait_row else 0.0,
                "reasons": [wait_msg],
            }

    regime = "unknown"
    if trending:
        regime = "trend"
    elif ranging:
        regime = "range"
    elif adx is not None:
        regime = "mixed"

    return {
        "regime": regime,
        "stance": stance,
        "min_entry_score": min_entry_score,
        "max_entries_today": max_entries_today,
        "entries_today": entries_today,
        "morning_min_entry_score": morn_floor if morn_floor > 0 else None,
        "spot_against_long_pct": veto_pct if veto_pct > 0 else None,
        "longs_enabled": longs_on,
        "shorts_enabled": shorts_on,
        "trend_long": trend_long,
        "trend_short": trend_short,
        "inputs": {
            "spot_chg_pct": spot_chg_day,
            "spot_chg_open_pct": spot_dir,
            "adx": adx,
            "pdi": pdi,
            "mdi": mdi,
            "adx_regime": adx_regime or None,
            "ce_pe_skew_raw": skew_raw,
            "ce_pe_skew_rich": skew_rich,
            "pcr": pcr,
            "combo_regime": combo_regime or None,
            "combo_signal": combo_signal or None,
            "impulse_delta": impulse_delta,
            "impulse_fade_side": impulse_fade or None,
            "combo_open": combo_open,
            "impulse_open": impulse_open,
            "thesis_blocks": [
                {"side": b.get("side"), "style": b.get("style") or "long"}
                for b in raw_blocks
                if (_str(b.get("side")) or "").lower() in ("ce", "pe")
            ],
            "thesis_block": (
                {
                    "side": (_str(raw_blocks[0].get("side")) or "").lower(),
                    "style": (_str(raw_blocks[0].get("style")) or "long").lower(),
                }
                if raw_blocks
                else None
            ),
            "avoid_today": daily_review.get("avoid_today") or [],
        },
        "candidates": ranked,
        "recommended": chosen,
        "playbook": {
            "trend": "prefer long premium with intraday/+DI; avoid short premium",
            "range": "prefer short premium or wait; long only on strong agree",
            "conflict": (
                f"wait unless agreement + score≥{min_entry_score}; "
                "honor avoid_today and thesis_block"
            ),
        },
    }
