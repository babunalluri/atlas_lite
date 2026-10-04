"""Candle / liquidity / volume-structure cues for the paper agent (code, not lore).

Built from session 1m OHLC (+ FUT volume/OI when present):
- 1m pin / engulf / failed sweeps (bull/bear trap)
- Multi-TF (5m/15m) order blocks
- Equal highs/lows liquidity pools
- Session volume-profile nodes + rejection traps
- Volume/OI ``intent_proxy`` (not true institutional intent — labeled as proxy)
"""

from __future__ import annotations

from typing import Any


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bar_ohlc(bar: dict[str, Any]) -> tuple[float, float, float, float] | None:
    o = _f(bar.get("o") if "o" in bar else bar.get("open"))
    h = _f(bar.get("h") if "h" in bar else bar.get("high"))
    l = _f(bar.get("l") if "l" in bar else bar.get("low"))
    c = _f(bar.get("c") if "c" in bar else bar.get("close"))
    if None in (o, h, l, c):
        return None
    if h < l or h < max(o, c) or l > min(o, c):  # type: ignore[arg-type]
        return None
    return float(o), float(h), float(l), float(c)  # type: ignore[return-value]


def _geom(o: float, h: float, l: float, c: float) -> dict[str, float]:
    rng = max(1e-9, h - l)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return {
        "range": round(rng, 4),
        "body": round(body, 4),
        "upper_wick": round(max(0.0, upper), 4),
        "lower_wick": round(max(0.0, lower), 4),
        "body_frac": round(body / rng, 3),
        "close_loc": round((c - l) / rng, 3),
        "dir": 1.0 if c > o else (-1.0 if c < o else 0.0),
    }


def _ts_day(ts: str) -> str:
    return str(ts).replace("T", " ")[:10]


def _ts_hm(ts: str) -> tuple[int, int] | None:
    raw = str(ts).replace("T", " ")
    if len(raw) < 16:
        return None
    try:
        return int(raw[11:13]), int(raw[14:16])
    except ValueError:
        return None


def _closed_bars(
    bars: list[dict[str, Any]] | None, *, exclude_forming_key: str | None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        t = str(bar.get("t") or bar.get("time") or "")
        if exclude_forming_key and t[:16] == exclude_forming_key[:16]:
            continue
        if _bar_ohlc(bar) is None:
            continue
        out.append(bar)
    return out


def _aggregate(bars: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    """Aggregate 1m bars into N-minute OHLCV buckets (session clock aligned)."""
    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for bar in bars:
        t = str(bar.get("t") or "").replace("T", " ")[:16]
        hm = _ts_hm(t)
        if hm is None:
            continue
        ohlc = _bar_ohlc(bar)
        if ohlc is None:
            continue
        minute_of_day = hm[0] * 60 + hm[1]
        # Align to session 09:15.
        sess0 = 9 * 60 + 15
        if minute_of_day < sess0:
            continue
        slot = sess0 + ((minute_of_day - sess0) // minutes) * minutes
        sh, sm = divmod(slot, 60)
        key = f"{t[:10]} {sh:02d}:{sm:02d}"
        o, h, l, c = ohlc
        v = _f(bar.get("v")) or 0.0
        oi = _f(bar.get("oi"))
        if key not in buckets:
            buckets[key] = {
                "t": key,
                "o": o,
                "h": h,
                "l": l,
                "c": c,
                "v": v,
                "oi": oi,
            }
            order.append(key)
        else:
            b = buckets[key]
            b["h"] = max(float(b["h"]), h)
            b["l"] = min(float(b["l"]), l)
            b["c"] = c
            b["v"] = float(b.get("v") or 0) + v
            if oi is not None:
                b["oi"] = oi
    return [buckets[k] for k in order]


def _atr(parsed: list[tuple[Any, tuple[float, float, float, float], dict]], n: int = 14) -> float:
    if len(parsed) < 2:
        return 1.0
    trs: list[float] = []
    prev_c = parsed[0][1][3]
    for _, (_o, h, l, c), _ in parsed[1:]:
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        prev_c = c
    window = trs[-n:] if trs else [1.0]
    return max(1e-6, sum(window) / len(window))


def _find_order_block(
    ht_bars: list[dict[str, Any]], *, tf: str, impulse_mult: float = 1.6
) -> dict[str, Any] | None:
    """Last opposite candle before an impulsive continuation (retail OB proxy)."""
    if len(ht_bars) < 4:
        return None
    parsed = []
    for bar in ht_bars:
        ohlc = _bar_ohlc(bar)
        if ohlc is None:
            continue
        parsed.append((bar, ohlc, _geom(*ohlc)))
    if len(parsed) < 4:
        return None
    atr = _atr(parsed, n=min(14, len(parsed) - 1))
    # Scan from newest impulse backward.
    for i in range(len(parsed) - 1, 2, -1):
        bar, (o, h, l, c), g = parsed[i]
        if g["range"] < impulse_mult * atr:
            continue
        # Bullish impulse: up bar → look for prior down bar (demand OB).
        if c > o:
            for j in range(i - 1, max(-1, i - 6), -1):
                pb, (po, ph, pl, pc), pg = parsed[j]
                if pc < po:
                    mid = round((pl + max(po, pc)) / 2.0, 2)
                    return {
                        "tf": tf,
                        "side": "demand",
                        "low": round(pl, 2),
                        "high": round(max(po, pc), 2),
                        "mid": mid,
                        "t": str(pb.get("t") or "")[:16],
                        "impulse_t": str(bar.get("t") or "")[:16],
                    }
        # Bearish impulse: down bar → prior up bar (supply OB).
        if c < o:
            for j in range(i - 1, max(-1, i - 6), -1):
                pb, (po, ph, pl, pc), pg = parsed[j]
                if pc > po:
                    mid = round((min(po, pc) + ph) / 2.0, 2)
                    return {
                        "tf": tf,
                        "side": "supply",
                        "low": round(min(po, pc), 2),
                        "high": round(ph, 2),
                        "mid": mid,
                        "t": str(pb.get("t") or "")[:16],
                        "impulse_t": str(bar.get("t") or "")[:16],
                    }
    return None


def _swing_points(
    parsed: list[tuple[Any, tuple[float, float, float, float], dict]],
    *,
    kind: str,
    left: int = 2,
    right: int = 2,
) -> list[tuple[int, float]]:
    """Strict local swing highs/lows (flat tape must not mark every bar)."""
    out: list[tuple[int, float]] = []
    n = len(parsed)
    if n < left + right + 1:
        return out
    for i in range(left, n - right):
        if kind == "high":
            px = parsed[i][1][1]
            left_ext = max(parsed[j][1][1] for j in range(i - left, i))
            right_ext = max(parsed[j][1][1] for j in range(i + 1, i + right + 1))
            if px > left_ext and px > right_ext:
                out.append((i, px))
        else:
            px = parsed[i][1][2]
            left_ext = min(parsed[j][1][2] for j in range(i - left, i))
            right_ext = min(parsed[j][1][2] for j in range(i + 1, i + right + 1))
            if px < left_ext and px < right_ext:
                out.append((i, px))
    return out


def _equal_pools(
    parsed: list[tuple[Any, tuple[float, float, float, float], dict]],
    *,
    atr: float,
    tol_frac: float = 0.15,
) -> dict[str, Any]:
    """Cluster swing highs/lows within tol → equal-high / equal-low liquidity pools."""
    if len(parsed) < 8:
        return {"equal_highs": [], "equal_lows": [], "nearest": None}
    tol = max(0.5, atr * tol_frac)
    highs = _swing_points(parsed, kind="high")
    lows = _swing_points(parsed, kind="low")

    def clusters(points: list[tuple[int, float]], kind: str) -> list[dict[str, Any]]:
        if len(points) < 2:
            return []
        pts = sorted(points, key=lambda x: x[1])
        groups: list[list[tuple[int, float]]] = []
        cur: list[tuple[int, float]] = []
        for p in pts:
            if not cur or abs(p[1] - cur[-1][1]) <= tol:
                cur.append(p)
            else:
                if len(cur) >= 2:
                    groups.append(cur)
                cur = [p]
        if len(cur) >= 2:
            groups.append(cur)
        out = []
        for g in groups:
            level = round(sum(x[1] for x in g) / len(g), 2)
            out.append(
                {
                    "kind": kind,
                    "level": level,
                    "touches": len(g),
                    "tol": round(tol, 2),
                }
            )
        last_c = parsed[-1][1][3]
        out.sort(key=lambda r: (-int(r["touches"]), abs(float(r["level"]) - last_c)))
        return out[:3]

    eq_h = clusters(highs, "equal_highs")
    eq_l = clusters(lows, "equal_lows")
    last_c = parsed[-1][1][3]
    nearest = None
    candidates = eq_h + eq_l
    if candidates:
        nearest = min(candidates, key=lambda r: abs(float(r["level"]) - last_c))
        nearest = {
            **nearest,
            "dist_pts": round(abs(float(nearest["level"]) - last_c), 2),
            "above": float(nearest["level"]) > last_c,
        }
    return {"equal_highs": eq_h, "equal_lows": eq_l, "nearest": nearest}


def _volume_profile(
    day_bars: list[dict[str, Any]],
    *,
    bins: int = 24,
) -> dict[str, Any]:
    """Session volume-by-price; HVN/LVN and rejection-from-node trap proxy."""
    rows: list[tuple[float, float, float, float, float]] = []
    for bar in day_bars:
        ohlc = _bar_ohlc(bar)
        if ohlc is None:
            continue
        v = _f(bar.get("v")) or 0.0
        rows.append((*ohlc, max(0.0, v)))
    if len(rows) < 8:
        return {"ok": False, "reason": "need_more_session_bars"}
    lo = min(r[2] for r in rows)
    hi = max(r[1] for r in rows)
    if hi <= lo:
        return {"ok": False, "reason": "flat_session"}
    width = (hi - lo) / bins
    vol_bins = [0.0] * bins
    total_v = 0.0
    for _o, h, l, c, v in rows:
        # Attribute bar volume to typical price bin.
        typical = (h + l + c) / 3.0
        idx = int((typical - lo) / width)
        idx = max(0, min(bins - 1, idx))
        # If no volume on index bars, use range as weak proxy weight.
        w = v if v > 0 else max(h - l, 0.01)
        vol_bins[idx] += w
        total_v += w
    if total_v <= 0:
        return {"ok": False, "reason": "no_volume_weight"}
    hvn_i = max(range(bins), key=lambda i: vol_bins[i])
    # LVN: least volume in interior bins (ignore edges).
    interior = list(range(1, bins - 1)) or list(range(bins))
    lvn_i = min(interior, key=lambda i: vol_bins[i])
    hvn_px = round(lo + (hvn_i + 0.5) * width, 2)
    lvn_px = round(lo + (lvn_i + 0.5) * width, 2)
    last_c = rows[-1][3]
    last_h, last_l = rows[-1][1], rows[-1][2]
    trap = None
    # Rejection through HVN: wicked through node, closed back on opposite side.
    if last_h >= hvn_px >= last_l:
        if last_c < hvn_px and (last_h - hvn_px) >= 0.25 * max(last_h - last_l, 1e-9):
            trap = "vp_reject_from_hvn_bearish"
        elif last_c > hvn_px and (hvn_px - last_l) >= 0.25 * max(last_h - last_l, 1e-9):
            trap = "vp_reject_from_hvn_bullish"
    poc_share = round(vol_bins[hvn_i] / total_v, 3)
    return {
        "ok": True,
        "hvn": hvn_px,
        "lvn": lvn_px,
        "poc_share": poc_share,
        "session_low": round(lo, 2),
        "session_high": round(hi, 2),
        "trap": trap,
        "volume_known": any((_f(b.get("v")) or 0) > 0 for b in day_bars),
    }


def _intent_proxy(
    parsed: list[tuple[Any, tuple[float, float, float, float], dict]],
    day_bars: list[dict[str, Any]],
) -> dict[str, Any]:
    """Large-range + volume / OI expansion proxy — NOT true institutional intent."""
    if len(parsed) < 5:
        return {
            "label": "unknown",
            "note": "proxy_only_not_true_institutional_intent",
            "signals": [],
        }
    atr = _atr(parsed)
    last_bar, (o, h, l, c), g = parsed[-1]
    v = _f(last_bar.get("v")) or 0.0
    vols = [_f(b.get("v")) or 0.0 for b, _, _ in parsed[-20:]]
    avg_v = sum(vols) / max(1, len(vols))
    signals: list[str] = []
    label = "neutral"
    # Absorption: high volume, small body/range.
    if avg_v > 0 and v >= 1.8 * avg_v and g["range"] <= 0.7 * atr:
        label = "absorption"
        signals.append(f"high_vol_small_range v={v:.0f} vs avg={avg_v:.0f}")
    elif g["range"] >= 1.8 * atr and (avg_v <= 0 or v >= 1.2 * avg_v):
        label = "initiative_up" if c > o else "initiative_down"
        signals.append(f"impulse_range={g['range']:.2f} atr={atr:.2f}")
    # OI expansion on last few bars if present.
    ois = [_f(b.get("oi")) for b, _, _ in parsed[-6:] if _f(b.get("oi")) is not None]
    if len(ois) >= 3 and ois[-1] is not None and ois[0] is not None:
        if ois[-1] > ois[0] * 1.002 and c > o:
            signals.append("oi_up_with_price_up")
            if label == "neutral":
                label = "oi_long_build"
        elif ois[-1] > ois[0] * 1.002 and c < o:
            signals.append("oi_up_with_price_down")
            if label == "neutral":
                label = "oi_short_build"
    return {
        "label": label,
        "note": "proxy_only_not_true_institutional_intent",
        "signals": signals[:6],
        "last_volume": round(v, 2),
        "avg_volume_20": round(avg_v, 2),
    }


def _ob_relation(spot: float | None, ob: dict[str, Any] | None) -> str | None:
    if spot is None or not ob:
        return None
    lo, hi = float(ob["low"]), float(ob["high"])
    if lo <= spot <= hi:
        return "inside"
    if spot < lo:
        return "below"
    return "above"


def build_structure_expert(
    bars: list[dict[str, Any]] | None,
    *,
    spot: float | None = None,
    forming_key: str | None = None,
    sweep_lookback: int = 12,
) -> dict[str, Any]:
    """Return structure snapshot from session 1m bars (closed only)."""
    closed = _closed_bars(bars, exclude_forming_key=forming_key)
    if len(closed) < 3:
        return {
            "ok": False,
            "reason": "need_≥3_closed_1m_bars",
            "bars_used": len(closed),
            "bias": "unknown",
            "trap": None,
            "cues": [],
            "recent_bars": [],
        }

    parsed: list[tuple[dict[str, Any], tuple[float, float, float, float], dict[str, float]]] = []
    for bar in closed:
        ohlc = _bar_ohlc(bar)
        if ohlc is None:
            continue
        parsed.append((bar, ohlc, _geom(*ohlc)))
    if len(parsed) < 3:
        return {
            "ok": False,
            "reason": "need_≥3_valid_ohlc",
            "bars_used": len(parsed),
            "bias": "unknown",
            "trap": None,
            "cues": [],
            "recent_bars": [],
        }

    last_day = _ts_day(str(parsed[-1][0].get("t") or ""))
    day_parsed = [p for p in parsed if _ts_day(str(p[0].get("t") or "")) == last_day]
    # Do not bleed prior-day bars into today's structure (avoids wrong traps at open).
    if len(day_parsed) < 3:
        return {
            "ok": False,
            "reason": "need_≥3_bars_today",
            "bars_used": len(parsed),
            "session_bars": len(day_parsed),
            "bias": "unknown",
            "trap": None,
            "cues": [],
            "recent_bars": [],
            "session": {"day": last_day},
        }
    day_bars = [p[0] for p in day_parsed]

    last_bar, (o, h, l, c), g = day_parsed[-1]
    _prev_bar, (po, ph, pl, pc), _pg = day_parsed[-2]
    cues: list[str] = []
    pin: str | None = None
    engulf: str | None = None
    sweep: str | None = None
    trap: str | None = None

    if g["lower_wick"] >= 2.0 * max(g["body"], 1e-9) and g["close_loc"] >= 0.65:
        pin = "bullish_rejection"
        cues.append(
            f"pin_bullish: lower_wick={g['lower_wick']} body={g['body']} close_loc={g['close_loc']}"
        )
    elif g["upper_wick"] >= 2.0 * max(g["body"], 1e-9) and g["close_loc"] <= 0.35:
        pin = "bearish_rejection"
        cues.append(
            f"pin_bearish: upper_wick={g['upper_wick']} body={g['body']} close_loc={g['close_loc']}"
        )

    curr_lo, curr_hi = min(o, c), max(o, c)
    prev_lo, prev_hi = min(po, pc), max(po, pc)
    if c > o and pc < po and curr_lo <= prev_lo and curr_hi >= prev_hi:
        engulf = "bullish_engulfing"
        cues.append("engulfing=bullish")
    elif c < o and pc > po and curr_lo <= prev_lo and curr_hi >= prev_hi:
        engulf = "bearish_engulfing"
        cues.append("engulfing=bearish")

    atr1 = _atr(day_parsed)
    min_overshoot = max(0.5, 0.3 * atr1)
    look = day_parsed[-(sweep_lookback + 1) : -1]
    if look:
        prior_high = max(b[1][1] for b in look)
        prior_low = min(b[1][2] for b in look)
        # Require meaningful overshoot + close back in the rejecting half of the bar.
        if (
            h > prior_high + min_overshoot
            and c < prior_high
            and g["close_loc"] <= 0.45
        ):
            sweep = "sweep_highs_fail"
            trap = "bull_trap"
            cues.append(
                f"sweep_highs_fail: overshoot={h - prior_high:.2f}≥{min_overshoot:.2f} "
                f"close_loc={g['close_loc']} (bull trap)"
            )
        elif (
            l < prior_low - min_overshoot
            and c > prior_low
            and g["close_loc"] >= 0.55
        ):
            sweep = "sweep_lows_fail"
            trap = "bear_trap"
            cues.append(
                f"sweep_lows_fail: overshoot={prior_low - l:.2f}≥{min_overshoot:.2f} "
                f"close_loc={g['close_loc']} (bear trap)"
            )

    sess_high = max(b[1][1] for b in day_parsed)
    sess_low = min(b[1][2] for b in day_parsed)
    sess_range = max(1e-9, sess_high - sess_low)
    near_high = (sess_high - c) / sess_range <= 0.08
    near_low = (c - sess_low) / sess_range <= 0.08
    if near_high:
        cues.append(f"near_session_high ({c} vs {sess_high})")
    if near_low:
        cues.append(f"near_session_low ({c} vs {sess_low})")

    ranges = [b[2]["range"] for b in day_parsed]
    compress = None
    if len(ranges) >= 15:
        recent = sum(ranges[-5:]) / 5.0
        prior = sum(ranges[-15:-5]) / 10.0
        if prior > 0 and recent / prior <= 0.55:
            compress = True
            cues.append(f"compression: recent_rng={recent:.2f} vs prior={prior:.2f}")
        else:
            compress = False

    spot_f = _f(spot) if spot is not None else c

    # Multi-TF order blocks (1m fallback + 5m / 15m).
    bars_5 = _aggregate(day_bars, 5)
    bars_15 = _aggregate(day_bars, 15)
    ob_1 = _find_order_block(day_bars[-40:], tf="1m")
    ob_5 = _find_order_block(bars_5, tf="5m")
    ob_15 = _find_order_block(bars_15, tf="15m")
    order_blocks = [x for x in (ob_15, ob_5, ob_1) if x]
    # Dedupe similar zones (same side, overlapping).
    deduped: list[dict[str, Any]] = []
    for ob in order_blocks:
        if any(
            d.get("side") == ob.get("side")
            and abs(float(d["mid"]) - float(ob["mid"])) <= max(1.0, atr1 * 0.25)
            for d in deduped
        ):
            continue
        deduped.append(ob)
    order_blocks = deduped[:3]
    for ob in order_blocks:
        rel = _ob_relation(spot_f, ob)
        ob["spot_rel"] = rel
        cues.append(f"ob_{ob['tf']}_{ob['side']} [{ob['low']}-{ob['high']}] spot={rel}")

    pools = _equal_pools(day_parsed, atr=atr1)
    if pools.get("nearest"):
        n = pools["nearest"]
        cues.append(
            f"liquidity_{n['kind']}@{n['level']} touches={n['touches']} dist={n['dist_pts']}"
        )

    vp = _volume_profile(day_bars)
    # Index NIFTY often has v=0 → range-weighted VP; never promote those to traps.
    if vp.get("ok") and vp.get("trap") and vp.get("volume_known"):
        if trap is None:
            trap = str(vp["trap"])
        cues.append(f"vp_trap={vp['trap']} hvn={vp.get('hvn')}")
    elif vp.get("ok"):
        if not vp.get("volume_known"):
            vp = {**vp, "trap": None, "trap_suppressed": "no_real_volume"}
        cues.append(
            f"vp_hvn={vp.get('hvn')} lvn={vp.get('lvn')} "
            f"poc_share={vp.get('poc_share')} vol_known={vp.get('volume_known')}"
        )

    intent = _intent_proxy(day_parsed, day_bars)
    for s in intent.get("signals") or []:
        cues.append(f"intent_proxy:{s}")

    # Liquidity pool sweep: same overshoot + close-half gate as generic sweeps.
    nearest = pools.get("nearest")
    if nearest:
        lvl = float(nearest["level"])
        eqh_grab = (
            nearest["kind"] == "equal_highs"
            and h > lvl + min_overshoot
            and c < lvl
            and g["close_loc"] <= 0.45
        )
        eql_grab = (
            nearest["kind"] == "equal_lows"
            and l < lvl - min_overshoot
            and c > lvl
            and g["close_loc"] >= 0.55
        )
        if eqh_grab:
            trap = "eqh_liquidity_grab"
            sweep = sweep or "sweep_equal_highs_fail"
            cues.append(
                f"eqh_liquidity_grab @{lvl} overshoot={h - lvl:.2f}≥{min_overshoot:.2f}"
            )
        elif eql_grab:
            trap = "eql_liquidity_grab"
            sweep = sweep or "sweep_equal_lows_fail"
            cues.append(
                f"eql_liquidity_grab @{lvl} overshoot={lvl - l:.2f}≥{min_overshoot:.2f}"
            )

    bull_pts = 0
    bear_pts = 0
    if pin == "bullish_rejection":
        bull_pts += 2
    elif pin == "bearish_rejection":
        bear_pts += 2
    if engulf == "bullish_engulfing":
        bull_pts += 2
    elif engulf == "bearish_engulfing":
        bear_pts += 2
    if trap in ("bear_trap", "eql_liquidity_grab", "vp_reject_from_hvn_bullish"):
        bull_pts += 3
    elif trap in ("bull_trap", "eqh_liquidity_grab", "vp_reject_from_hvn_bearish"):
        bear_pts += 3
    if near_low and g["dir"] >= 0:
        bull_pts += 1
    if near_high and g["dir"] <= 0:
        bear_pts += 1
    for ob in order_blocks:
        rel = ob.get("spot_rel")
        if ob.get("side") == "demand" and rel in ("inside", "above"):
            bull_pts += 1 if rel == "inside" else 0
            if rel == "inside":
                cues.append("spot_in_demand_ob")
        if ob.get("side") == "supply" and rel in ("inside", "below"):
            bear_pts += 1 if rel == "inside" else 0
            if rel == "inside":
                cues.append("spot_in_supply_ob")
    if intent.get("label") == "initiative_up":
        bull_pts += 1
    elif intent.get("label") == "initiative_down":
        bear_pts += 1
    elif intent.get("label") == "absorption":
        # Absorption near highs/lows flips.
        if near_high:
            bear_pts += 1
        elif near_low:
            bull_pts += 1

    if bull_pts >= bear_pts + 2 and bull_pts >= 2:
        bias = "bullish_structure"
    elif bear_pts >= bull_pts + 2 and bear_pts >= 2:
        bias = "bearish_structure"
    elif compress:
        bias = "compression"
    else:
        bias = "neutral"

    recent = []
    for bar, ohlc, geom in parsed[-5:]:
        recent.append(
            {
                "t": str(bar.get("t") or "")[:16],
                "o": ohlc[0],
                "h": ohlc[1],
                "l": ohlc[2],
                "c": ohlc[3],
                "v": _f(bar.get("v")),
                "body": geom["body"],
                "upper_wick": geom["upper_wick"],
                "lower_wick": geom["lower_wick"],
            }
        )

    return {
        "ok": True,
        "bars_used": len(parsed),
        "session_bars": len(day_parsed),
        "last": {
            "t": str(last_bar.get("t") or "")[:16],
            "o": o,
            "h": h,
            "l": l,
            "c": c,
            "v": _f(last_bar.get("v")),
            **g,
        },
        "pin": pin,
        "engulfing": engulf,
        "sweep": sweep,
        "trap": trap,
        "compression": compress,
        "session": {
            "high": round(sess_high, 2),
            "low": round(sess_low, 2),
            "near_high": near_high,
            "near_low": near_low,
            "day": last_day,
        },
        "order_blocks": order_blocks[:3],
        "liquidity_pools": pools,
        "volume_profile": vp,
        "intent_proxy": intent,
        "bias": bias,
        "bias_score": {"bull": bull_pts, "bear": bear_pts},
        "cues": cues[:14],
        "recent_bars": recent,
        "spot": spot_f,
        "playbook": {
            "bull_trap": "failed high sweep — favor long PE / short CE; avoid chasing long CE",
            "bear_trap": "failed low sweep — favor long CE / short PE; avoid chasing long PE",
            "eqh_liquidity_grab": "equal-highs taken then rejected — soft bearish",
            "eql_liquidity_grab": "equal-lows taken then rejected — soft bullish",
            "demand_ob": "spot in/near demand order block — soft long CE",
            "supply_ob": "spot in/near supply order block — soft long PE",
            "vp_reject_from_hvn_bearish": "rejected from HVN from above — soft bearish",
            "vp_reject_from_hvn_bullish": "rejected from HVN from below — soft bullish",
            "intent_proxy": "volume/OI proxy only — not true institutional intent",
            "compression": "tight ranges — prefer short premium or wait",
        },
    }
