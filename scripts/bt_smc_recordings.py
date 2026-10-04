#!/usr/bin/env python3
"""Offline: does BOS/ChoCH/FVG/MSS / OB+Fib help vs structure on OCI recordings?

Uses REAL ATM CE/PE premiums from JSONL (not delta proxy).
Builds 1m OHLC from intra-minute spot ticks, then compares:

  A) baseline — current agent_structure traps/bias/OB cues
  B) baseline + SMC confluence filter (BOS/ChoCH/FVG/MSS/sweep)
  C) SMC-only entries (confluence score)
  D) ob_fib — sample SMC: BOS/CHoCH → last opposite candle OB → Fib discount/premium
  E) ob_fib_session — D + NSE session windows (09:15–11:30, 13:30–15:00)

Verdict: implement only if B/D clearly beats A on net + WR (and OOS half).

Usage:
  python3 scripts/bt_smc_recordings.py --dir ~/atlas_lite/data/recordings
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.agent_structure import build_structure_expert  # noqa: E402
from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402
from atlas_lite.recorder import iter_jsonl_dicts, list_slot_recording_paths  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
QTY = 65
SLIP = 0.5
SQUARE = "15:14"
ENTRY_AFTER = "09:45"
ENTRY_UNTIL = "14:30"
# Doc session filters (NSE liquidity windows); still capped by ENTRY_* above.
SESSION_WINDOWS = (("09:15", "11:30"), ("13:30", "15:00"))
MAX_DAY = 3
COOLDOWN = 8
TARGET_PCT = 0.25  # +25% premium
STOP_PCT = 0.20  # −20% premium
HOLD_MIN = 45
SWING_N = 3  # sample lookback for causal swings


def _f(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def load_1m_from_recordings(rec_dir: Path) -> list[dict[str, Any]]:
    """True-ish 1m OHLC from ticks + last CE/PE/ADX in that minute."""
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for path in list_slot_recording_paths(rec_dir):
        for obj in iter_jsonl_dicts(path):
            ts = obj.get("ts")
            if not isinstance(ts, str):
                continue
            dt = _parse_ts(ts)
            if dt is None:
                continue
            feed = obj.get("feed") if isinstance(obj.get("feed"), dict) else {}
            spot = _f(obj.get("spot") or feed.get("nifty_ltp") or feed.get("spot"))
            if spot is None:
                continue
            key = (dt.date().isoformat(), dt.strftime("%H:%M"))
            ce, pe = _f(feed.get("ce")), _f(feed.get("pe"))
            adx = _f(feed.get("adx"))
            if key not in buckets:
                buckets[key] = {
                    "t": f"{key[0]} {key[1]}",
                    "o": spot,
                    "h": spot,
                    "l": spot,
                    "c": spot,
                    "v": 1.0,
                    "ce": ce,
                    "pe": pe,
                    "adx": adx,
                    "n": 1,
                }
                continue
            b = buckets[key]
            b["h"] = max(float(b["h"]), spot)
            b["l"] = min(float(b["l"]), spot)
            b["c"] = spot
            b["v"] = float(b["v"]) + 1.0
            b["n"] = int(b["n"]) + 1
            if ce is not None:
                b["ce"] = ce
            if pe is not None:
                b["pe"] = pe
            if adx is not None:
                b["adx"] = adx
    return [buckets[k] for k in sorted(buckets)]


def _swings(h: list[float], l: list[float], left: int = 3, right: int = 3) -> list[tuple[int, str, float]]:
    """Return list of (index, 'H'|'L', price) swing points."""
    out: list[tuple[int, str, float]] = []
    n = len(h)
    for i in range(left, n - right):
        if h[i] >= max(h[i - left : i + right + 1]) and h[i] > h[i - 1] and h[i] >= h[i + 1]:
            out.append((i, "H", h[i]))
        if l[i] <= min(l[i - left : i + right + 1]) and l[i] < l[i - 1] and l[i] <= l[i + 1]:
            out.append((i, "L", l[i]))
    out.sort(key=lambda x: x[0])
    return out


def compute_smc(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-bar SMC flags from closed bars only (causal: use data <= i)."""
    c = [float(b["c"]) for b in bars]
    h = [float(b["h"]) for b in bars]
    l = [float(b["l"]) for b in bars]
    o = [float(b["o"]) for b in bars]
    n = len(bars)
    flags = [
        {
            "bos_bull": False,
            "bos_bear": False,
            "choch_bull": False,
            "choch_bear": False,
            "mss_bull": False,
            "mss_bear": False,
            "fvg_bull": False,
            "fvg_bear": False,
            "sweep_high": False,
            "sweep_low": False,
            "trend": 0,  # +1 bull structure, -1 bear
        }
        for _ in range(n)
    ]

    trend = 0
    last_sh: float | None = None
    last_sl: float | None = None
    prev_sh: float | None = None
    prev_sl: float | None = None

    # Rolling causal swings: recompute on growing window (OK for ~few k bars)
    for i in range(10, n):
        swings = _swings(h[: i + 1], l[: i + 1], left=3, right=2)
        # only confirmed swings (right=2 means last 2 bars can't confirm — already excluded)
        shs = [p for idx, k, p in swings if k == "H"]
        sls = [p for idx, k, p in swings if k == "L"]
        if shs:
            if last_sh is not None and shs[-1] != last_sh:
                prev_sh = last_sh
            last_sh = shs[-1]
        if sls:
            if last_sl is not None and sls[-1] != last_sl:
                prev_sl = last_sl
            last_sl = sls[-1]

        f = flags[i]
        # BOS: close beyond last swing in trend direction
        if last_sh is not None and c[i] > last_sh and trend >= 0:
            f["bos_bull"] = True
            trend = 1
        if last_sl is not None and c[i] < last_sl and trend <= 0:
            f["bos_bear"] = True
            trend = -1

        # ChoCH: close against prior trend through opposite swing
        if trend == 1 and last_sl is not None and c[i] < last_sl:
            f["choch_bear"] = True
            trend = -1
        if trend == -1 and last_sh is not None and c[i] > last_sh:
            f["choch_bull"] = True
            trend = 1

        # MSS ≈ ChoCH + follow-through BOS in new direction within recent window
        if f["choch_bull"] or (
            trend == 1 and any(flags[j]["choch_bull"] for j in range(max(0, i - 8), i))
        ):
            if last_sh is not None and c[i] > last_sh:
                f["mss_bull"] = True
        if f["choch_bear"] or (
            trend == -1 and any(flags[j]["choch_bear"] for j in range(max(0, i - 8), i))
        ):
            if last_sl is not None and c[i] < last_sl:
                f["mss_bear"] = True

        # FVG (3-candle imbalance) just completed at i-1 / i
        if i >= 2:
            # bullish FVG: low[i] > high[i-2]
            if l[i] > h[i - 2] and c[i] > o[i]:
                f["fvg_bull"] = True
            if h[i] < l[i - 2] and c[i] < o[i]:
                f["fvg_bear"] = True

        # Liquidity sweep: take prior swing then close back inside
        if prev_sh is not None and h[i] > prev_sh and c[i] < prev_sh:
            f["sweep_high"] = True
        if prev_sl is not None and l[i] < prev_sl and c[i] > prev_sl:
            f["sweep_low"] = True

        f["trend"] = trend
        flags[i] = f

    return flags


def _charges(entry: float, exit_px: float) -> float:
    return float(kite_nfo_charges([(entry, QTY, "buy"), (exit_px, QTY, "sell")])["total"])


def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"n": 0, "wr": 0.0, "net": 0.0, "avg": 0.0, "pf": 0.0}
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gl = abs(sum(losses)) or 1e-9
    return {
        "n": len(pnls),
        "wr": round(100 * len(wins) / len(pnls), 1),
        "net": round(sum(pnls), 2),
        "avg": round(sum(pnls) / len(pnls), 2),
        "pf": round(sum(wins) / gl, 2),
    }


def _baseline_side(structure: dict[str, Any], adx: float | None) -> str | None:
    """Map current structure expert → long CE/PE (same soft logic as scorecard)."""
    if not structure.get("ok"):
        return None
    trap = str(structure.get("trap") or "").lower()
    bias = str(structure.get("bias") or "").lower()
    ranging = adx is not None and adx < 18
    trending = adx is not None and adx >= 22
    bearish_traps = {"bull_trap", "eqh_liquidity_grab", "vp_reject_from_hvn_bearish"}
    bullish_traps = {"bear_trap", "eql_liquidity_grab", "vp_reject_from_hvn_bullish"}
    if trap in bearish_traps and (ranging or not trending):
        return "pe"
    if trap in bullish_traps and (ranging or not trending):
        return "ce"
    # OB inside
    for ob in structure.get("order_blocks") or []:
        if not isinstance(ob, dict):
            continue
        if ob.get("spot_rel") == "inside" and ob.get("side") == "demand":
            return "ce"
        if ob.get("spot_rel") == "inside" and ob.get("side") == "supply":
            return "pe"
    if bias == "bullish_structure" and trap not in bearish_traps and not ranging:
        return "ce"
    if bias == "bearish_structure" and trap not in bullish_traps and not ranging:
        return "pe"
    return None


def _smc_side(f: dict[str, Any]) -> str | None:
    """SMC entry: sweep+MSS / ChoCH+FVG / BOS+FVG confluence."""
    # Bullish
    bull = 0
    if f.get("sweep_low"):
        bull += 2
    if f.get("choch_bull") or f.get("mss_bull"):
        bull += 2
    if f.get("bos_bull"):
        bull += 1
    if f.get("fvg_bull"):
        bull += 1
    # Bearish
    bear = 0
    if f.get("sweep_high"):
        bear += 2
    if f.get("choch_bear") or f.get("mss_bear"):
        bear += 2
    if f.get("bos_bear"):
        bear += 1
    if f.get("fvg_bear"):
        bear += 1
    if bull >= 3 and bull > bear:
        return "ce"
    if bear >= 3 and bear > bull:
        return "pe"
    return None


def _smc_agrees(side: str, f: dict[str, Any]) -> bool:
    """Filter: SMC must not contradict; prefer mild agreement."""
    if side == "ce":
        if f.get("choch_bear") or f.get("mss_bear") or f.get("bos_bear"):
            return False
        return bool(
            f.get("trend", 0) >= 0
            or f.get("sweep_low")
            or f.get("fvg_bull")
            or f.get("bos_bull")
            or f.get("choch_bull")
            or f.get("mss_bull")
        )
    if side == "pe":
        if f.get("choch_bull") or f.get("mss_bull") or f.get("bos_bull"):
            return False
        return bool(
            f.get("trend", 0) <= 0
            or f.get("sweep_high")
            or f.get("fvg_bear")
            or f.get("bos_bear")
            or f.get("choch_bear")
            or f.get("mss_bear")
        )
    return False


def _in_session(hm: str) -> bool:
    return any(a <= hm <= b for a, b in SESSION_WINDOWS)


def compute_ob_fib_sides(bars: list[dict[str, Any]], lookback: int = SWING_N) -> list[str | None]:
    """Causal port of the sample OB + Fib discount/premium entry logic.

    Swings confirmed with ``lookback`` bars on each side (no centered lookahead).
    On BOS/CHoCH: last opposite-color candle → OB. Entry when price mitigates OB
    in Fib discount (buy/CE) or premium (sell/PE).
    """
    n = len(bars)
    out: list[str | None] = [None] * n
    if n < lookback * 2 + 5:
        return out

    h = [float(b["h"]) for b in bars]
    l = [float(b["l"]) for b in bars]
    o = [float(b["o"]) for b in bars]
    c = [float(b["c"]) for b in bars]

    trend = 0
    last_sh: float | None = None
    last_sl: float | None = None
    bullish_ob: dict[str, float] | None = None
    bearish_ob: dict[str, float] | None = None

    for i in range(lookback, n - lookback):
        # Confirm swing at i only after ``lookback`` right bars exist → evaluate at i+lookback
        conf = i  # candidate swing index; right window ends at i+lookback
        # Actually confirm swings whose right edge is the current bar
        j = i - lookback  # swing candidate just confirmed at bar i
        if j >= lookback:
            win_h = h[j - lookback : j + lookback + 1]
            win_l = l[j - lookback : j + lookback + 1]
            if h[j] >= max(win_h) and h[j] > h[j - 1] and h[j] >= h[j + 1]:
                last_sh = h[j]
            if l[j] <= min(win_l) and l[j] < l[j - 1] and l[j] <= l[j + 1]:
                last_sl = l[j]

        if last_sh is None or last_sl is None:
            continue

        # Structure breaks on close (sample)
        if c[i] > last_sh:
            trend = 1
            ob_idx = i - 1
            while ob_idx > 0 and c[ob_idx] > o[ob_idx]:
                ob_idx -= 1
            if ob_idx >= 0:
                bullish_ob = {"top": h[ob_idx], "bottom": l[ob_idx]}
        elif c[i] < last_sl:
            trend = -1
            ob_idx = i - 1
            while ob_idx > 0 and c[ob_idx] < o[ob_idx]:
                ob_idx -= 1
            if ob_idx >= 0:
                bearish_ob = {"top": h[ob_idx], "bottom": l[ob_idx]}

        swing_range = last_sh - last_sl
        if swing_range <= 0:
            continue
        fib_618 = last_sh - 0.618 * swing_range
        fib_sell_618 = last_sl + 0.618 * swing_range
        eq = last_sl + 0.5 * swing_range

        signal: str | None = None
        if trend == 1 and bullish_ob is not None:
            top, bot = bullish_ob["top"], bullish_ob["bottom"]
            # Mitigate OB + discount (at/below 0.5; sample used fib_618 as confluence)
            if l[i] <= top and l[i] >= bot and top <= fib_618 and top <= eq:
                signal = "ce"
                bullish_ob = None
        elif trend == -1 and bearish_ob is not None:
            top, bot = bearish_ob["top"], bearish_ob["bottom"]
            if h[i] >= bot and h[i] <= top and bot >= fib_sell_618 and bot >= eq:
                signal = "pe"
                bearish_ob = None
        out[i] = signal

    return out


def precompute_sides(
    bars: list[dict[str, Any]], smc: list[dict[str, Any]]
) -> list[tuple[str | None, str | None, str | None]]:
    """(baseline_side, smc_side, ob_fib_side) per bar."""
    out: list[tuple[str | None, str | None, str | None]] = [(None, None, None)] * len(bars)
    ob_fib = compute_ob_fib_sides(bars)
    last_st: dict[str, Any] | None = None
    last_i = -99
    last_day = ""
    for i in range(20, len(bars)):
        b = bars[i]
        day = b["t"][:10]
        need = (i - last_i >= 2) or (day != last_day)
        if need:
            last_st = build_structure_expert(bars[: i + 1], spot=float(b["c"]))
            last_i = i
            last_day = day
        base = _baseline_side(last_st or {}, _f(b.get("adx"))) if last_st else None
        out[i] = (base, _smc_side(smc[i]), ob_fib[i])
    return out


def simulate(
    bars: list[dict[str, Any]],
    smc: list[dict[str, Any]],
    sides: list[tuple[str | None, str | None, str | None]],
    *,
    mode: str,
    allow_days: set[str] | None = None,
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    day_n: dict[str, int] = defaultdict(int)
    cool = -1
    session_mode = mode.endswith("_session")
    for i in range(20, len(bars) - 2):
        b = bars[i]
        day = b["t"][:10]
        hm = b["t"][11:16]
        if allow_days is not None and day not in allow_days:
            continue
        if hm < ENTRY_AFTER or hm > ENTRY_UNTIL:
            continue
        if session_mode and not _in_session(hm):
            continue
        if day_n[day] >= MAX_DAY or i < cool:
            continue
        # skip thin premium
        ce, pe = _f(b.get("ce")), _f(b.get("pe"))
        if ce is None or pe is None or ce < 20 or pe < 20:
            continue
        # Tuesday PM soft skip
        try:
            if date.fromisoformat(day).weekday() == 1 and hm >= "13:00":
                continue
        except ValueError:
            pass

        base, smc_s, ob_fib = sides[i]
        side = None
        if mode == "baseline":
            side = base
        elif mode == "baseline_smc":
            if base and _smc_agrees(base, smc[i]):
                side = base
        elif mode == "smc_only":
            side = smc_s
        elif mode == "baseline_or_smc":
            side = base or smc_s
        elif mode in ("ob_fib", "ob_fib_session"):
            side = ob_fib
        if side is None:
            continue

        entry = ce if side == "ce" else pe
        if entry is None or entry <= 0:
            continue
        entry = entry + SLIP
        tgt = entry * (1 + TARGET_PCT)
        stp = entry * (1 - STOP_PCT)
        exit_px = entry
        reason = "time"
        for j in range(i + 1, min(len(bars), i + 1 + HOLD_MIN)):
            bj = bars[j]
            if bj["t"][:10] != day:
                break
            px = _f(bj.get("ce") if side == "ce" else bj.get("pe"))
            if px is None:
                continue
            # adverse/favorable using print (no intra-option OHLC)
            if px <= stp:
                exit_px = stp
                reason = "stop"
                break
            if px >= tgt:
                exit_px = tgt
                reason = "target"
                break
            if bj["t"][11:16] >= SQUARE:
                exit_px = px - SLIP
                reason = "square"
                break
            exit_px = px - SLIP
            reason = "time"
        pnl = round((exit_px - entry) * QTY - _charges(entry, max(0.05, exit_px)), 2)
        trades.append(
            {
                "day": day,
                "hm": hm,
                "side": side,
                "pnl": pnl,
                "reason": reason,
                "mode": mode,
            }
        )
        day_n[day] += 1
        cool = i + COOLDOWN
    return trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=ROOT / "data" / "recordings")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "bt_smc_recordings.json")
    args = ap.parse_args()
    if not args.dir.is_dir():
        print(f"missing recordings dir: {args.dir}", file=sys.stderr)
        return 1

    bars = load_1m_from_recordings(args.dir)
    days = sorted({b["t"][:10] for b in bars})
    print(f"bars={len(bars)} days={len(days)} {days[0] if days else '?'}->{days[-1] if days else '?'}")
    if len(bars) < 200 or len(days) < 6:
        print("not enough data", file=sys.stderr)
        return 1

    print("computing SMC…", flush=True)
    smc = compute_smc(bars)
    # feature hit rates
    hits = defaultdict(int)
    for f in smc:
        for k, v in f.items():
            if v is True:
                hits[k] += 1
    print("SMC hit counts:", dict(hits), flush=True)
    print("precomputing structure sides…", flush=True)
    sides = precompute_sides(bars, smc)

    split = max(4, int(len(days) * 0.55))
    is_days, oos_days = set(days[:split]), set(days[split:])
    print(f"IS days={len(is_days)} OOS days={len(oos_days)}")

    modes = [
        "baseline",
        "baseline_smc",
        "smc_only",
        "baseline_or_smc",
        "ob_fib",
        "ob_fib_session",
    ]
    report: dict[str, Any] = {"days": days, "split": {"is": days[:split], "oos": days[split:]}, "modes": {}}

    print(f"\n{'mode':<18} {'full_net':>9} {'WR':>6} {'n':>4} {'PF':>5} | {'IS':>8} {'OOS':>8} {'oosWR':>6}")
    for mode in modes:
        full_t = simulate(bars, smc, sides, mode=mode)
        is_t = simulate(bars, smc, sides, mode=mode, allow_days=is_days)
        oos_t = simulate(bars, smc, sides, mode=mode, allow_days=oos_days)
        sf, si, so = _stats(full_t), _stats(is_t), _stats(oos_t)
        report["modes"][mode] = {"full": sf, "is": si, "oos": so}
        print(
            f"{mode:<18} {sf['net']:+9.0f} {sf['wr']:6.1f} {sf['n']:4} {sf['pf']:5.2f} | "
            f"{si['net']:+8.0f} {so['net']:+8.0f} {so['wr']:6.1f}"
        )

    a = report["modes"]["baseline"]
    b = report["modes"]["baseline_smc"]
    c = report["modes"]["smc_only"]
    d = report["modes"]["ob_fib"]
    e = report["modes"]["ob_fib_session"]

    # Decision rules (strict): B beats A on full net AND oos net, with n>=8 oos
    improve_net = b["full"]["net"] > a["full"]["net"] + 500
    improve_oos = b["oos"]["net"] > a["oos"]["net"]
    improve_wr = b["full"]["wr"] >= a["full"]["wr"] - 2
    enough = b["oos"]["n"] >= 6 and a["full"]["n"] >= 8
    smc_alone_ok = (
        c["full"]["net"] > a["full"]["net"]
        and c["oos"]["net"] > 0
        and c["oos"]["n"] >= 6
        and c["full"]["wr"] >= 48
    )
    best_ob = d if d["full"]["net"] >= e["full"]["net"] else e
    ob_ok = (
        best_ob["full"]["net"] > a["full"]["net"] + 500
        and best_ob["oos"]["net"] > 0
        and best_ob["oos"]["n"] >= 6
        and best_ob["full"]["wr"] >= 48
    )

    if enough and improve_net and improve_oos and improve_wr:
        verdict = "IMPLEMENT"
        why = "baseline+SMC filter beats baseline on full & OOS net without crushing WR"
    elif ob_ok and best_ob["full"]["net"] >= max(b["full"]["net"], c["full"]["net"]):
        verdict = "IMPLEMENT_OB_FIB"
        why = "OB+Fib sample entries beat baseline on full & positive OOS — candidate paper book"
    elif smc_alone_ok and c["full"]["net"] > b["full"]["net"]:
        verdict = "IMPLEMENT_SMC_ONLY"
        why = "SMC-only entries beat baseline; consider as soft scorecard cues"
    elif enough and improve_oos and b["full"]["net"] >= a["full"]["net"]:
        verdict = "SOFT_YES"
        why = "mild OOS improvement — soft weights only, not hard filters"
    else:
        verdict = "DO_NOT_IMPLEMENT"
        why = "SMC / OB+Fib do not clearly beat current structure baseline on this sample"

    report["verdict"] = verdict
    report["why"] = why
    report["smc_hits"] = dict(hits)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n=== VERDICT: {verdict} ===")
    print(why)
    print(
        f"baseline {a['full']} | +SMC {b['full']} | smc_only {c['full']} | "
        f"ob_fib {d['full']} | ob_fib_session {e['full']}"
    )
    print("saved", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
