"""Offline learner for the AI agent paper book.

Fits one linear score per action (long/short call and put) on finished minute
rows. Walk-forward trains on earlier days and scores only later days. The
scorecard is replayed on those same later days. ``promoted`` is true only when
the learned book makes money, after charges, on the days it did not train on,
and its choice of call or put beats picking the side at random at the same
minutes (a sale at a random side earns most of what a learned sale does).
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from atlas_lite.agent_scorecard import build_strategy_scorecard
from atlas_lite.instruments import round_atm_strike
from atlas_lite.kite_charges import kite_nfo_charges
from atlas_lite.paper_agent import (
    COOLDOWN_MIN,
    DAILY_LOSS_STOP,
    DEFAULT_LOT_SIZE,
    SQUARE_OFF,
    agent_exit_profile,
    agent_hold_until,
    initial_target_stop,
    trail_gap_px,
)

IST_OFFSET = "+05:30"
ACTIONS = ("long_ce", "long_pe", "short_ce", "short_pe")
FEATURE_NAMES = (
    "bias",
    "spot_open_pct",
    "adx",
    "pcr",
    "skew",
    "atr",
    "minute",
    "nifty_chg",
    # The option itself: where spot sits versus the strike, time left, and the last 15 minutes of premium.
    "strike_gap",
    "days_left",
    "ce_chg",
    "pe_chg",
)
MODEL_NAME = "ai_agent_model.json"
RIDGE = 1.0
# Last fraction of sessions is the unseen test. Need enough days on each side.
MIN_DAYS = 6
# More than three unseen sessions, and enough fills that one lucky day cannot promote.
MIN_TEST_DAYS = 4
MIN_TEST_TRADES = 20
# Each fill is half a point worse than the printed premium. One lot pays about ₹65 a round trip.
SLIPPAGE_PTS = 0.5
# A prediction must clear charges plus a buffer, and the book cannot refill all day.
MIN_PREDICTED_NET = 150.0
# The fit learns whether a normal trade paid. A freak print does not own the weights.
TARGET_CLIP = 1300.0
PREMIUM_LOOKBACK_MIN = 15
# The side choice must beat a random side by this many standard errors of the difference.
MIN_SIDE_EDGE_SE = 2.0
MAX_ENTRIES_PER_DAY = 8
SESSION_OPEN_HM = "09:15"


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hm_to_min(hm: str) -> int:
    hour, minute = hm.split(":", 1)
    return int(hour) * 60 + int(minute)


def _min_to_hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _dt(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00{IST_OFFSET}")


def _weekly_expiry(session: date) -> date:
    """Nifty weekly expiry: Tuesday, or the previous session when Tuesday is shut."""
    from atlas_lite.agent_gates import NSE_HOLIDAYS

    expiry = session + timedelta(days=(1 - session.weekday()) % 7)
    while expiry.weekday() >= 5 or expiry.isoformat() in NSE_HOLIDAYS:
        expiry -= timedelta(days=1)
    return expiry


def _days_left(day: str, hm: str, expiry: str | None) -> float:
    """Calendar days until 15:30 on expiry. A blank expiry is the Tuesday of that week."""
    if not day or not hm:
        return 0.0
    exp_iso = str(expiry or "")[:10]
    if not exp_iso:
        try:
            exp_iso = _weekly_expiry(date.fromisoformat(day)).isoformat()
        except ValueError:
            return 0.0
    try:
        exp = datetime.fromisoformat(f"{exp_iso}T15:30:00{IST_OFFSET}")
        now = _dt(day, hm)
    except ValueError:
        return 0.0
    hours = (exp - now).total_seconds() / 3600.0
    if hours <= 0:
        return 0.0
    return hours / 24.0


def _train_target(pnl: float) -> float:
    """Direction of a normal one-lot result. Larger than about 20 points is clipped."""
    if pnl > TARGET_CLIP:
        return TARGET_CLIP
    if pnl < -TARGET_CLIP:
        return -TARGET_CLIP
    return pnl


def features_from_values(
    *,
    spot_open_pct: float | None,
    adx: float | None,
    pcr: float | None,
    skew: float | None,
    atr: float | None,
    hm: str,
    nifty_chg: float | None,
    spot: float | None = None,
    atm: float | None = None,
    days_left: float = 0.0,
    ce_chg: float | None = None,
    pe_chg: float | None = None,
) -> list[float]:
    """Fixed-length inputs. Missing numbers become 0 so a thin feed still scores."""
    minute = _hm_to_min(hm) if hm and ":" in hm else 0
    strike_gap = 0.0 if spot is None or atm is None else (float(spot) - float(atm)) / 50.0
    return [
        1.0,
        float(spot_open_pct or 0.0),
        float(adx or 0.0) / 25.0,
        (float(pcr) - 1.0) if pcr is not None else 0.0,
        float(skew or 0.0) / 20.0,
        float(atr or 0.0) / 10.0,
        minute / 360.0,
        float(nifty_chg or 0.0),
        strike_gap,
        float(days_left or 0.0),
        float(ce_chg or 0.0) / 10.0,
        float(pe_chg or 0.0) / 10.0,
    ]


def _row_skew(row: dict[str, Any]) -> float | None:
    ce = _f(row.get("ce"))
    pe = _f(row.get("pe"))
    if ce is None or pe is None:
        return None
    return ce - pe


def features_from_row(row: dict[str, Any], *, day_open: float | None) -> list[float]:
    spot = _f(row.get("spot"))
    open_pct = _f(row.get("spot_chg_open_pct"))
    if open_pct is None and spot is not None and day_open not in (None, 0):
        open_pct = (spot - float(day_open)) / float(day_open) * 100.0
    hm = str(row.get("hm") or "")
    return features_from_values(
        spot_open_pct=open_pct,
        adx=_f(row.get("adx")),
        pcr=_f(row.get("pcr")),
        skew=_f(row.get("skew")) if row.get("skew") is not None else _row_skew(row),
        atr=_f(row.get("atr")),
        hm=hm,
        nifty_chg=_f(row.get("nifty_chg") if row.get("nifty_chg") is not None else row.get("index_nifty_chg")),
        spot=spot,
        atm=_f(row.get("atm")),
        days_left=_days_left(str(row.get("date") or ""), hm, str(row.get("expiry") or "") or None),
        ce_chg=_f(row.get("ce_chg_15")),
        pe_chg=_f(row.get("pe_chg_15")),
    )


def _attach_premium_change(days: dict[str, dict[str, dict[str, Any]]]) -> None:
    """Last 15 minutes of the same strike. A strike change leaves the change blank."""
    for minutes in days.values():
        for hm, row in minutes.items():
            prev = minutes.get(_min_to_hm(_hm_to_min(hm) - PREMIUM_LOOKBACK_MIN))
            row["ce_chg_15"] = None
            row["pe_chg_15"] = None
            if prev is None:
                continue
            if row.get("atm") is not None and prev.get("atm") is not None and int(row["atm"]) != int(prev["atm"]):
                continue
            ce, prev_ce = _f(row.get("ce")), _f(prev.get("ce"))
            pe, prev_pe = _f(row.get("pe")), _f(prev.get("pe"))
            if ce is not None and prev_ce is not None:
                row["ce_chg_15"] = ce - prev_ce
            if pe is not None and prev_pe is not None:
                row["pe_chg_15"] = pe - prev_pe


def _in_entry_window(hm: str) -> bool:
    return "09:45" <= hm <= "15:00" and hm < "15:14"


def _round_trip_charges(style: str, entry: float, exit_px: float, qty: int) -> float:
    open_side = "sell" if style == "short" else "buy"
    close_side = "buy" if style == "short" else "sell"
    total = kite_nfo_charges(
        [(float(entry), qty, open_side), (float(exit_px), qty, close_side)]
    )["total"]
    return float(total)


def _slipped(style: str, quote: float, *, opening: bool) -> float:
    """Buy fills pay the extra half point. Sell fills receive it less."""
    buy = opening if style == "long" else not opening
    slipped = quote + SLIPPAGE_PTS if buy else quote - SLIPPAGE_PTS
    return max(slipped, 0.05)


def _gross(style: str, entry: float, exit_px: float, qty: int) -> float:
    if style == "short":
        return (float(entry) - float(exit_px)) * int(qty)
    return (float(exit_px) - float(entry)) * int(qty)


def _row_strike(row: dict[str, Any]) -> int | None:
    """The contract these premiums belong to.

    The tape's call and put are the feed's sticky strike. Kite rows have no
    stored strike, and those premiums were taken at the spot rounded to 50.
    """
    raw = row.get("atm")
    if raw in (None, ""):
        pass
    else:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    spot = _f(row.get("spot"))
    if spot is None:
        return None
    return round_atm_strike(spot)


def _dead_session(minutes: dict[str, dict[str, Any]]) -> bool:
    """A holiday tape: the spot and the call never move."""
    spots: list[float] = []
    calls: list[float] = []
    for row in minutes.values():
        spot = _f(row.get("spot"))
        call = _f(row.get("ce"))
        if spot is not None:
            spots.append(spot)
        if call is not None:
            calls.append(call)
    if len(spots) < 2 or len(calls) < 2:
        return False
    return max(spots) == min(spots) and max(calls) == min(calls)


def _model_premium(row: dict[str, Any], side: str, strike: int) -> float | None:
    """A strike's premium from this minute's ATM pair: the forward from put-call parity,
    the expected move from the straddle, then a normal (Bachelier) price."""
    row_strike = _row_strike(row)
    if row_strike is None:
        return None
    ce, pe = _f(row.get("ce")), _f(row.get("pe"))
    if ce is None or pe is None or ce <= 0 or pe <= 0:
        return None
    forward = row_strike + ce - pe
    # ATM straddle = 2 * s * phi(0) for the expected move s.
    move = (ce + pe) * math.sqrt(math.pi / 2.0)
    d = (forward - strike) / move
    call = (forward - strike) * 0.5 * (1.0 + math.erf(d / math.sqrt(2.0))) + move * math.exp(-d * d / 2.0) / math.sqrt(2.0 * math.pi)
    return call if side == "ce" else call - (forward - strike)


def _held_premium(row: dict[str, Any], side: str, entry_strike: int | None, offset: float = 0.0) -> float | None:
    """The held contract's premium on this minute.

    ce/pe on a row are the feed's current strike. Once that strike moves, the held
    strike is the model price plus ``offset``, the gap between the last real print
    and the model on that minute, so the mark does not jump at the roll. A straight
    shift by delta underprices every held option and flatters sales. No later minute is used.
    """
    px = _f(row.get(side))
    if px is None or px <= 0:
        return None
    if entry_strike is None:
        return px
    row_strike = _row_strike(row)
    if row_strike == entry_strike:
        return px
    model = _model_premium(row, side, entry_strike)
    return None if model is None else max(0.05, model + offset)


def simulate_action(
    minutes: dict[str, dict[str, Any]],
    entry: dict[str, Any],
    action: str,
    *,
    qty: int = DEFAULT_LOT_SIZE,
) -> tuple[float, str] | None:
    """Net rupees and exit minute for one ATM action, using the agent exit profile.

    Returns None when the premium path after the entry minute is missing.
    """
    if action not in ACTIONS:
        return None
    style, side = action.split("_", 1)
    entry_px = _f(entry.get(side))
    if entry_px is None or entry_px <= 0:
        return None
    start = _hm_to_min(str(entry["hm"]))
    opened = _dt(str(entry["date"]), str(entry["hm"]))
    target_pct, stop_pct, arm_pct, trail_pct, trail_pts = agent_exit_profile(
        opened, style
    )
    target, stop = initial_target_stop(
        entry_px, style, target_pct=target_pct, stop_pct=stop_pct  # type: ignore[arg-type]
    )
    gap = trail_gap_px(entry_px, trail_pct=trail_pct, trail_pts=trail_pts)
    best = entry_px
    armed = False
    square_min = _hm_to_min(f"{SQUARE_OFF[0]:02d}:{SQUARE_OFF[1]:02d}")
    # The live book's hold: shorts to square-off, longs 20 or 30 minutes (Scalper v2 env).
    held_until = agent_hold_until(opened, style)
    hold_end = min(held_until.hour * 60 + held_until.minute, square_min)
    entry_strike = _row_strike(entry)
    offset = 0.0
    if entry_strike is not None:
        model = _model_premium(entry, side, entry_strike)
        if model is not None:
            offset = entry_px - model
    exit_px: float | None = None
    exit_hm: str | None = None
    for minute in range(start + 1, hold_end + 1):
        hm = _min_to_hm(minute)
        row = minutes.get(hm)
        if row is None:
            continue
        px = _held_premium(row, side, entry_strike, offset)
        if px is None:
            continue
        if entry_strike is not None and _row_strike(row) == entry_strike:
            model = _model_premium(row, side, entry_strike)
            if model is not None:
                offset = px - model
        if style == "short":
            # Match agent_paper short profile: stop or square-off only.
            if px >= stop or minute >= hold_end:
                exit_px = px
                exit_hm = hm
                break
        else:
            best = max(best, px)
            if not armed and px >= entry_px * (1.0 + arm_pct):
                armed = True
            if armed:
                stop = max(stop, round(best - gap, 2))
            if px >= target or px <= stop or minute >= hold_end:
                exit_px = px
                exit_hm = hm
                break
    if exit_px is None or exit_hm is None:
        return None
    filled_entry = _slipped(style, entry_px, opening=True)
    filled_exit = _slipped(style, exit_px, opening=False)
    charges = _round_trip_charges(style, filled_entry, filled_exit, qty)
    net = round(_gross(style, filled_entry, filled_exit, qty) - charges, 2)
    return net, exit_hm


def _dot(weights: list[float], features: list[float]) -> float:
    return sum(w * x for w, x in zip(weights, features))


def _solve(matrix: list[list[float]], target: list[float]) -> list[float] | None:
    size = len(target)
    rows = [row[:] + [target[i]] for i, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-10:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        scale = rows[col][col]
        for j in range(col, size + 1):
            rows[col][j] /= scale
        for r in range(size):
            if r == col:
                continue
            factor = rows[r][col]
            if factor == 0.0:
                continue
            for j in range(col, size + 1):
                rows[r][j] -= factor * rows[col][j]
    return [rows[i][size] for i in range(size)]


def _fit_ridge(samples: list[list[float]], targets: list[float], *, lam: float = RIDGE) -> list[float]:
    width = len(FEATURE_NAMES)
    if not samples:
        return [0.0] * width
    xtx = [[0.0] * width for _ in range(width)]
    xty = [0.0] * width
    for features, y in zip(samples, targets):
        for i in range(width):
            xty[i] += features[i] * y
            for j in range(width):
                xtx[i][j] += features[i] * features[j]
    for i in range(1, width):
        xtx[i][i] += lam
    solved = _solve(xtx, xty)
    return solved if solved is not None else [0.0] * width


def _index_days(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, dict[str, dict[str, Any]]], dict[str, float | None]]:
    days: dict[str, dict[str, dict[str, Any]]] = {}
    opens: dict[str, float | None] = {}
    for row in rows:
        day = str(row.get("date") or "")
        hm = str(row.get("hm") or "")
        if not day or not hm:
            continue
        days.setdefault(day, {})[hm] = row
        spot = _f(row.get("spot"))
        # 09:00 pre-open is near yesterday's close. The live book uses the 09:15 session open.
        if spot is not None and hm >= SESSION_OPEN_HM and day not in opens:
            opens[day] = spot
    ordered = sorted(days)
    return ordered, days, opens


def _scorecard_action(row: dict[str, Any], *, day_open: float | None) -> str:
    spot = _f(row.get("spot"))
    open_pct = _f(row.get("spot_chg_open_pct"))
    if open_pct is None and spot is not None and day_open not in (None, 0):
        open_pct = round((spot - float(day_open)) / float(day_open) * 100.0, 3)
    context = {
        "spot_chg_open_pct": open_pct,
        "adx": _f(row.get("adx")),
        "pcr": _f(row.get("pcr")),
        "ce_pe_skew": _row_skew(row),
        "spot": spot,
    }
    card = build_strategy_scorecard(context)
    rec = card.get("recommended") if isinstance(card.get("recommended"), dict) else {}
    if rec.get("action") != "propose_entry":
        return "wait"
    style = str(rec.get("style") or "")
    side = str(rec.get("side") or "")
    action = f"{style}_{side}"
    return action if action in ACTIONS else "wait"


def _replay(
    day_names: list[str],
    days: dict[str, dict[str, dict[str, Any]]],
    opens: dict[str, float | None],
    decide: Callable[[dict[str, Any], float | None], str],
    *,
    qty: int = DEFAULT_LOT_SIZE,
    max_entries: int | None = None,
    entries_out: list[tuple[str, dict[str, Any], str, float]] | None = None,
) -> dict[str, Any]:
    """One position at a time, the book's cooldown and day stop. Same path as the book.

    ``entries_out`` collects (day, row, action, net) for every trade taken.
    """
    net = 0.0
    trades = 0
    by_day: dict[str, float] = {}
    for day in day_names:
        minutes = days[day]
        day_net = 0.0
        entries = 0
        cursor = _hm_to_min("09:45")
        end = _hm_to_min("15:00")
        while cursor <= end:
            hm = _min_to_hm(cursor)
            row = minutes.get(hm)
            if row is None or not _in_entry_window(hm) or day_net <= DAILY_LOSS_STOP:
                cursor += 1
                continue
            if max_entries is not None and entries >= max_entries:
                cursor += 1
                continue
            action = decide(row, opens.get(day))
            if action == "wait":
                cursor += 1
                continue
            outcome = simulate_action(minutes, row, action, qty=qty)
            if outcome is None:
                cursor += 1
                continue
            pnl, exit_hm = outcome
            if entries_out is not None:
                entries_out.append((day, row, action, pnl))
            day_net = round(day_net + pnl, 2)
            net = round(net + pnl, 2)
            trades += 1
            entries += 1
            cursor = _hm_to_min(exit_hm) + COOLDOWN_MIN
        by_day[day] = round(day_net, 2)
    return {"net": round(net, 2), "trades": trades, "by_day": by_day}


def side_edge(
    days: dict[str, dict[str, dict[str, Any]]],
    entries: list[tuple[str, dict[str, Any], str, float]],
    *,
    qty: int = DEFAULT_LOT_SIZE,
) -> dict[str, Any]:
    """The learned side against a random side at the same minutes, same buy or sell.

    A random side earns the average of the call and the put. Per trade the difference
    is the learned net minus that average; the edge counts only when its mean clears
    ``MIN_SIDE_EDGE_SE`` standard errors. Every learned trade counts: one whose other side
    cannot be priced scores as no edge, so the edge is measured on the same trades as the net.
    """
    diffs: list[float] = []
    random_net = 0.0
    for day, row, action, pnl in entries:
        style = action.split("_", 1)[0]
        outs = [simulate_action(days[day], row, f"{style}_{side}", qty=qty) for side in ("ce", "pe")]
        avg = pnl if outs[0] is None or outs[1] is None else (outs[0][0] + outs[1][0]) / 2.0
        random_net += avg
        diffs.append(pnl - avg)
    n = len(diffs)
    mean = sum(diffs) / n if n else 0.0
    sd = (sum((d - mean) ** 2 for d in diffs) / (n - 1)) ** 0.5 if n > 1 else 0.0
    se = sd / n**0.5 if n > 1 else 0.0
    return {
        "trades": n,
        "random_net": round(random_net, 2),
        "edge_per_trade": round(mean, 2),
        "edge_se": round(se, 2),
        "beats_random": n > 1 and mean > 0 and mean > MIN_SIDE_EDGE_SE * se,
    }


def skip_rising_short(action: str, ce_chg: float | None, pe_chg: float | None) -> str:
    """Wait instead of shorting an option that is already higher than 15 minutes ago.

    A blank change means the strike changed, so that short is still allowed.
    """
    if action == "short_ce" and ce_chg is not None and ce_chg > 0:
        return "wait"
    if action == "short_pe" and pe_chg is not None and pe_chg > 0:
        return "wait"
    return action


def _model_decide(weights: dict[str, list[float]]) -> Callable[[dict[str, Any], float | None], str]:
    def decide(row: dict[str, Any], day_open: float | None) -> str:
        action = predict_action(weights, features_from_row(row, day_open=day_open))[0]
        return skip_rising_short(action, _f(row.get("ce_chg_15")), _f(row.get("pe_chg_15")))

    return decide


def predict_action(weights: dict[str, list[float]], features: list[float]) -> tuple[str, float]:
    """Highest predicted net. Wait unless it clears charges plus the buffer."""
    best_action = "wait"
    best_net = 0.0
    for action in ACTIONS:
        vector = weights.get(action) or []
        if len(vector) != len(features):
            continue
        predicted = _dot(vector, features)
        if predicted > best_net:
            best_action = action
            best_net = predicted
    if best_net < MIN_PREDICTED_NET:
        return "wait", round(best_net, 2)
    return best_action, round(best_net, 2)


def walk_forward(rows: list[dict[str, Any]], *, qty: int = DEFAULT_LOT_SIZE) -> dict[str, Any]:
    """Train on earlier days. Promote only if unseen days beat the scorecard and finish green."""
    ordered, days, opens = _index_days(rows)
    ordered = [day for day in ordered if not _dead_session(days[day])]
    _attach_premium_change(days)
    width = len(FEATURE_NAMES)
    blank = {action: [0.0] * width for action in ACTIONS}
    if len(ordered) < MIN_DAYS:
        return {
            "promoted": False,
            "reason": "not_enough_days",
            "feature_names": list(FEATURE_NAMES),
            "weights": blank,
            "train_days": ordered,
            "test_days": [],
            "test_net": 0.0,
            "scorecard_test_net": 0.0,
            "test_trades": 0,
            "scorecard_test_trades": 0,
        }
    n_test = max(MIN_TEST_DAYS, len(ordered) // 3)
    n_test = min(n_test, len(ordered) - 3)
    train_days = ordered[:-n_test]
    test_days = ordered[-n_test:]
    samples: dict[str, list[list[float]]] = {action: [] for action in ACTIONS}
    targets: dict[str, list[float]] = {action: [] for action in ACTIONS}
    for day in train_days:
        minutes = days[day]
        day_open = opens.get(day)
        for hm in sorted(minutes):
            if not _in_entry_window(hm):
                continue
            row = minutes[hm]
            features = features_from_row(row, day_open=day_open)
            priced: dict[str, float] = {}
            for action in ACTIONS:
                outcome = simulate_action(minutes, row, action, qty=qty)
                if outcome is None:
                    priced = {}
                    break
                priced[action] = outcome[0]
            if len(priced) != len(ACTIONS):
                continue
            for action, pnl in priced.items():
                samples[action].append(features)
                targets[action].append(_train_target(pnl))
    weights = {
        action: _fit_ridge(samples[action], targets[action]) for action in ACTIONS
    }
    entries: list[tuple[str, dict[str, Any], str, float]] = []
    learned = _replay(
        test_days,
        days,
        opens,
        _model_decide(weights),
        qty=qty,
        max_entries=MAX_ENTRIES_PER_DAY,
        entries_out=entries,
    )
    edge = side_edge(days, entries, qty=qty)
    baseline = _replay(
        test_days,
        days,
        opens,
        lambda row, day_open: _scorecard_action(row, day_open=day_open),
        qty=qty,
        max_entries=MAX_ENTRIES_PER_DAY,
    )
    promoted = (
        len(test_days) >= MIN_TEST_DAYS
        and learned["trades"] >= MIN_TEST_TRADES
        and learned["net"] > 0
        and learned["net"] > baseline["net"]
        and edge["beats_random"]
    )
    if promoted:
        reason = "beats_random_side_on_unseen_days"
    elif len(test_days) < MIN_TEST_DAYS:
        reason = "not_enough_test_days"
    elif learned["trades"] < MIN_TEST_TRADES:
        reason = "not_enough_test_trades"
    elif learned["net"] <= 0 or learned["net"] <= baseline["net"]:
        reason = "does_not_beat_scorecard"
    else:
        reason = "side_no_better_than_random"
    return {
        "promoted": promoted,
        "reason": reason,
        "feature_names": list(FEATURE_NAMES),
        "weights": {action: [round(w, 6) for w in weights[action]] for action in ACTIONS},
        "train_days": train_days,
        "test_days": test_days,
        "test_net": learned["net"],
        "test_trades": learned["trades"],
        "test_by_day": learned["by_day"],
        "scorecard_test_net": baseline["net"],
        "scorecard_test_trades": baseline["trades"],
        "scorecard_by_day": baseline["by_day"],
        "random_side_test_net": edge["random_net"],
        "side_edge_per_trade": edge["edge_per_trade"],
        "side_edge_se": edge["edge_se"],
        "train_samples": len(samples["long_ce"]),
    }


def save_model(path: Path, report: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "book": "ai_agent",
        "version": 1,
        "promoted": bool(report.get("promoted")),
        "reason": report.get("reason"),
        "feature_names": list(report.get("feature_names") or FEATURE_NAMES),
        "weights": report.get("weights") or {},
        "train_days": report.get("train_days") or [],
        "test_days": report.get("test_days") or [],
        "test_net": report.get("test_net"),
        "test_trades": report.get("test_trades"),
        "scorecard_test_net": report.get("scorecard_test_net"),
        "scorecard_test_trades": report.get("scorecard_test_trades"),
        "random_side_test_net": report.get("random_side_test_net"),
        "side_edge_per_trade": report.get("side_edge_per_trade"),
        "side_edge_se": report.get("side_edge_se"),
        "trained_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def load_model(path: Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    weights = raw.get("weights")
    if not isinstance(weights, dict):
        return None
    return raw
