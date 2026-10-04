#!/usr/bin/env python3
"""Scan strategy variants on kite_5m_backtest_bars.json — rank by net profit.

Option P&L = delta 0.5 * spot move + NFO charges + 0.5pt slip/side (same proxy as kite bt).
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402
from atlas_lite.metrics import wilder_dmi_series  # noqa: E402

QTY = 65
SLIP = 0.5
DELTA = 0.5
SQUARE = "15:14"
MAX_DAY_DEFAULT = 5


def _hm(ts: str) -> str:
    return str(ts).replace("T", " ")[11:16]


def _day(ts: str) -> str:
    return str(ts).replace("T", " ")[:10]


def _is_tue(day: str) -> bool:
    try:
        return date.fromisoformat(day).weekday() == 1
    except ValueError:
        return False


def _ema(vals: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    k = 2.0 / (n + 1)
    prev = None
    seed: list[float] = []
    for i, v in enumerate(vals):
        if prev is None:
            seed.append(v)
            if len(seed) < n:
                continue
            prev = sum(seed) / n
            out[i] = prev
            continue
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def _rsi(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains, losses = [], []
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains) / n, sum(losses) / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def _atr(h: list[float], l: list[float], c: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(c)
    if len(c) < n + 1:
        return out
    trs = []
    for i in range(len(c)):
        if i == 0:
            trs.append(h[i] - l[i])
        else:
            trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    prev = sum(trs[1 : n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(c)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


def _vwap(bars: list[dict]) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    pv = vol = 0.0
    day = ""
    for i, b in enumerate(bars):
        d = _day(b["t"])
        if d != day:
            day = d
            pv = vol = 0.0
        v = float(b.get("v") or 0) or 1.0
        typ = (b["h"] + b["l"] + b["c"]) / 3
        pv += typ * v
        vol += v
        out[i] = pv / vol
    return out


def _prem(spot: float, atr: float) -> float:
    return max(20.0, round(0.004 * spot + 0.35 * atr, 2))


def _charges(entry: float, exit_px: float) -> float:
    return float(kite_nfo_charges([(entry, QTY, "buy"), (exit_px, QTY, "sell")])["total"])


def _supertrend(h, l, c, atr, period=10, mult=3.0):
    """Return list of (st_line, direction) direction +1/-1."""
    n = len(c)
    st: list[float | None] = [None] * n
    direction = [0] * n
    final_upper = final_lower = None
    prev_dir = 1
    for i in range(n):
        if atr[i] is None:
            continue
        hl2 = (h[i] + l[i]) / 2
        basic_u = hl2 + mult * float(atr[i])
        basic_l = hl2 - mult * float(atr[i])
        if final_upper is None:
            final_upper, final_lower = basic_u, basic_l
            st[i] = final_lower
            direction[i] = 1
            prev_dir = 1
            continue
        final_upper = basic_u if basic_u < final_upper or c[i - 1] > final_upper else final_upper
        final_lower = basic_l if basic_l > final_lower or c[i - 1] < final_lower else final_lower
        if prev_dir == 1:
            if c[i] < final_lower:
                prev_dir = -1
                st[i] = final_upper
            else:
                st[i] = final_lower
        else:
            if c[i] > final_upper:
                prev_dir = 1
                st[i] = final_lower
            else:
                st[i] = final_upper
        direction[i] = prev_dir
    return st, direction


@dataclass
class Cfg:
    name: str
    after: str = "09:45"
    until: str = "14:45"
    max_day: int = 5
    cooldown: int = 4
    stop_atr: float = 1.0
    target_r: float = 1.75
    adx_min: float = 22.0
    skip_tue_pm: bool = True
    sides: str = "both"  # both|ce|pe
    fresh_only: bool = False
    # signal kind
    kind: str = "mom"  # mom|pullback|fade|st_flip|orb


SignalFn = Callable[[int, dict], str | None]


def simulate(bars: list[dict], cfg: Cfg, ctx: dict, signal: SignalFn) -> dict[str, Any]:
    closes, highs, lows = ctx["c"], ctx["h"], ctx["l"]
    atr, adx = ctx["atr"], ctx["adx"]
    trades = []
    day_count: dict[str, int] = defaultdict(int)
    cooldown_until = -1
    prev_side: str | None = None
    i = 0
    while i < len(bars) - 1:
        day = _day(bars[i]["t"])
        hm = _hm(bars[i]["t"])
        if hm < cfg.after or hm > cfg.until:
            prev_side = None
            i += 1
            continue
        if cfg.skip_tue_pm and _is_tue(day) and hm >= "13:00":
            i += 1
            continue
        if day_count[day] >= cfg.max_day or i < cooldown_until:
            i += 1
            continue
        if atr[i] is None or adx[i] is None or float(adx[i]) < cfg.adx_min:
            i += 1
            continue
        side = signal(i, cfg.__dict__)
        if side is None:
            prev_side = None
            i += 1
            continue
        if cfg.sides == "ce" and side != "ce":
            i += 1
            continue
        if cfg.sides == "pe" and side != "pe":
            i += 1
            continue
        if cfg.fresh_only:
            if side == prev_side:
                i += 1
                continue
        prev_side = side

        a = float(atr[i])
        if a <= 0:
            i += 1
            continue
        spot0 = closes[i]
        stop = spot0 - cfg.stop_atr * a if side == "ce" else spot0 + cfg.stop_atr * a
        target = (
            spot0 + cfg.target_r * cfg.stop_atr * a
            if side == "ce"
            else spot0 - cfg.target_r * cfg.stop_atr * a
        )
        entry_opt = _prem(spot0, a) + SLIP
        exit_spot = spot0
        reason = "square_off"
        for j in range(i + 1, len(bars)):
            bj = bars[j]
            if _day(bj["t"]) != day:
                break
            hmj = _hm(bj["t"])
            if side == "ce":
                if bj["l"] <= stop:
                    exit_spot, reason = stop, "stop"
                    break
                if bj["h"] >= target:
                    exit_spot, reason = target, "target"
                    break
            else:
                if bj["h"] >= stop:
                    exit_spot, reason = stop, "stop"
                    break
                if bj["l"] <= target:
                    exit_spot, reason = target, "target"
                    break
            if hmj >= SQUARE:
                exit_spot, reason = bj["c"], "square_off"
                break
            exit_spot = bj["c"]
        move = (exit_spot - spot0) if side == "ce" else (spot0 - exit_spot)
        exit_opt = max(0.05, entry_opt - SLIP + DELTA * move - SLIP)
        pnl = round((exit_opt - entry_opt) * QTY - _charges(entry_opt, exit_opt), 2)
        trades.append({"pnl": pnl, "side": side, "reason": reason, "day": day, "hm": hm})
        day_count[day] += 1
        cooldown_until = i + cfg.cooldown
        i += 1

    if not trades:
        return {"name": cfg.name, "n": 0, "wr": 0.0, "net": 0.0, "avg": 0.0, "pf": 0.0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), abs(sum(losses))
    return {
        "name": cfg.name,
        "n": len(trades),
        "wr": round(100 * len(wins) / len(pnls), 1),
        "net": round(sum(pnls), 2),
        "avg": round(sum(pnls) / len(pnls), 2),
        "pf": round(gp / gl, 2) if gl > 0 else 99.0,
        "wins": len(wins),
        "ce": sum(1 for t in trades if t["side"] == "ce"),
        "pe": sum(1 for t in trades if t["side"] == "pe"),
    }


def build_ctx(bars: list[dict]) -> dict:
    c = [b["c"] for b in bars]
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    atr = _atr(h, l, c)
    ema20 = _ema(c, 20)
    ema50 = _ema(c, 50)
    rsi = _rsi(c)
    vwap = _vwap(bars)
    pdi, mdi, adx = wilder_dmi_series(h, l, c, period=14)
    st, stdir = _supertrend(h, l, c, atr)
    # session open = first bar of day close-ish open
    sess_open: dict[str, float] = {}
    for b in bars:
        d = _day(b["t"])
        if d not in sess_open and _hm(b["t"]) >= "09:15":
            sess_open[d] = b["o"]
    # ORB high/low 09:15-09:30
    orb: dict[str, tuple[float, float]] = {}
    tmp: dict[str, list] = defaultdict(list)
    for b in bars:
        if "09:15" <= _hm(b["t"]) <= "09:29":
            tmp[_day(b["t"])].append(b)
    for d, xs in tmp.items():
        orb[d] = (max(x["h"] for x in xs), min(x["l"] for x in xs))
    # vol avg
    vols = [float(b.get("v") or 0) for b in bars]
    vol_avg = []
    for i in range(len(vols)):
        w = [v for v in vols[max(0, i - 20) : i] if v > 0]
        vol_avg.append(sum(w) / len(w) if w else None)
    return {
        "c": c,
        "h": h,
        "l": l,
        "atr": atr,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "vwap": vwap,
        "pdi": pdi,
        "mdi": mdi,
        "adx": adx,
        "st": st,
        "stdir": stdir,
        "sess_open": sess_open,
        "orb": orb,
        "vols": vols,
        "vol_avg": vol_avg,
        "bars": bars,
    }


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "kite_5m_backtest_bars.json"
    bars = json.loads(path.read_text(encoding="utf-8"))
    ctx = build_ctx(bars)
    c, ema20, ema50, rsi, vwap = ctx["c"], ctx["ema20"], ctx["ema50"], ctx["rsi"], ctx["vwap"]
    pdi, mdi, adx = ctx["pdi"], ctx["mdi"], ctx["adx"]
    atr, stdir, st = ctx["atr"], ctx["stdir"], ctx["st"]
    sess_open, orb = ctx["sess_open"], ctx["orb"]
    vols, vol_avg = ctx["vols"], ctx["vol_avg"]
    h, l = ctx["h"], ctx["l"]

    results: list[dict] = []

    def add(cfg: Cfg, fn: SignalFn) -> None:
        results.append(simulate(bars, cfg, ctx, fn))

    # --- momentum stacked (baseline family) ---
    def mom(i, _):
        if None in (ema20[i], rsi[i], vwap[i]):
            return None
        if vols[i] > 0 and vol_avg[i] and vols[i] < 1.2 * vol_avg[i]:
            return None
        if c[i] > vwap[i] and c[i] > ema20[i] and rsi[i] >= 55 and (pdi[i] or 0) >= (mdi[i] or 0):
            return "ce"
        if c[i] < vwap[i] and c[i] < ema20[i] and rsi[i] <= 45 and (mdi[i] or 0) >= (pdi[i] or 0):
            return "pe"
        return None

    add(Cfg("mom_baseline"), mom)
    add(Cfg("mom_morning", until="11:30"), mom)
    add(Cfg("mom_morning_pe", until="11:30", sides="pe"), mom)
    add(Cfg("mom_adx25", adx_min=25), mom)
    add(Cfg("mom_morning_adx25", until="11:30", adx_min=25), mom)
    add(Cfg("mom_morning_pe_adx25", until="11:30", sides="pe", adx_min=25), mom)
    add(Cfg("mom_morning_pe_stop15_2R", until="11:30", sides="pe", stop_atr=1.5, target_r=2.0), mom)
    add(Cfg("mom_morning_pe_fresh", until="11:30", sides="pe", fresh_only=True), mom)
    add(Cfg("mom_pe_only", sides="pe"), mom)
    add(Cfg("mom_ce_only", sides="ce"), mom)

    # --- pullback to EMA/VWAP while bias ---
    def pullback(i, _):
        if i < 2 or None in (ema20[i], vwap[i], rsi[i], ema20[i - 1]):
            return None
        # bullish bias: above ema50 or vwap, pullback touch ema20 then green close
        if ema50[i] is None:
            return None
        bull = c[i] > ema50[i] and (pdi[i] or 0) > (mdi[i] or 0) and rsi[i] >= 45
        bear = c[i] < ema50[i] and (mdi[i] or 0) > (pdi[i] or 0) and rsi[i] <= 55
        touched = l[i] <= ema20[i] <= h[i] or l[i] <= vwap[i] <= h[i]
        if bull and touched and c[i] > ema20[i] and c[i] >= bars[i]["o"]:
            return "ce"
        if bear and touched and c[i] < ema20[i] and c[i] <= bars[i]["o"]:
            return "pe"
        return None

    add(Cfg("pullback_ema_vwap", adx_min=20), pullback)
    add(Cfg("pullback_morning", until="11:30", adx_min=20), pullback)
    add(Cfg("pullback_morning_pe", until="11:30", sides="pe", adx_min=20), pullback)
    add(Cfg("pullback_morn_pe_s15", until="11:30", sides="pe", adx_min=22, stop_atr=1.5, target_r=2.0), pullback)
    add(Cfg("pullback_morn_pe_s15_adx25", until="11:30", sides="pe", adx_min=25, stop_atr=1.5, target_r=1.5), pullback)

    # --- mean-reversion fade extremes ---
    def fade(i, _):
        if None in (rsi[i], vwap[i], atr[i]):
            return None
        # fade stretch from vwap
        if atr[i] <= 0:
            return None
        dist = (c[i] - vwap[i]) / atr[i]
        if dist >= 1.2 and rsi[i] >= 70 and (mdi[i] or 0) >= (pdi[i] or 0) * 0.9:
            return "pe"  # fade up → long PE
        if dist <= -1.2 and rsi[i] <= 30 and (pdi[i] or 0) >= (mdi[i] or 0) * 0.9:
            return "ce"
        return None

    add(Cfg("fade_vwap_rsi", adx_min=18, stop_atr=1.2, target_r=1.2), fade)
    add(Cfg("fade_morning", until="11:30", adx_min=18, stop_atr=1.2, target_r=1.5), fade)
    add(Cfg("fade_morning_pe", until="11:30", sides="pe", adx_min=18, stop_atr=1.5, target_r=1.5), fade)

    # --- supertrend flip ---
    def st_flip(i, _):
        if i < 1 or stdir[i] == 0 or stdir[i - 1] == 0:
            return None
        if stdir[i] == 1 and stdir[i - 1] == -1 and c[i] > (vwap[i] or c[i]):
            return "ce"
        if stdir[i] == -1 and stdir[i - 1] == 1 and c[i] < (vwap[i] or c[i]):
            return "pe"
        return None

    add(Cfg("supertrend_flip", adx_min=20), st_flip)
    add(Cfg("supertrend_morning", until="11:30", adx_min=20), st_flip)
    add(Cfg("supertrend_morn_pe", until="11:30", sides="pe", adx_min=22, stop_atr=1.5, target_r=2.0), st_flip)

    # --- ORB break 09:30+ ---
    def orb_brk(i, _):
        day = _day(bars[i]["t"])
        hm = _hm(bars[i]["t"])
        if hm < "09:35" or day not in orb:
            return None
        hi, lo = orb[day]
        if c[i] > hi and (pdi[i] or 0) > (mdi[i] or 0):
            return "ce"
        if c[i] < lo and (mdi[i] or 0) > (pdi[i] or 0):
            return "pe"
        return None

    add(Cfg("orb_break", after="09:35", until="11:30", adx_min=18, fresh_only=True, max_day=3), orb_brk)
    add(Cfg("orb_break_pe", after="09:35", until="11:30", sides="pe", adx_min=18, fresh_only=True, max_day=3), orb_brk)
    add(
        Cfg(
            "orb_break_pe_s15",
            after="09:35",
            until="11:30",
            sides="pe",
            adx_min=20,
            stop_atr=1.5,
            target_r=2.0,
            fresh_only=True,
            max_day=3,
        ),
        orb_brk,
    )

    # --- trend from open + DI ---
    def open_trend(i, _):
        day = _day(bars[i]["t"])
        o0 = sess_open.get(day)
        if o0 is None or atr[i] is None or atr[i] <= 0:
            return None
        move = (c[i] - o0) / atr[i]
        if move >= 0.8 and (pdi[i] or 0) > (mdi[i] or 0) and rsi[i] and rsi[i] >= 52:
            return "ce"
        if move <= -0.8 and (mdi[i] or 0) > (pdi[i] or 0) and rsi[i] and rsi[i] <= 48:
            return "pe"
        return None

    add(Cfg("open_trend", until="11:30", adx_min=22, fresh_only=True), open_trend)
    add(Cfg("open_trend_pe", until="11:30", sides="pe", adx_min=22, fresh_only=True, stop_atr=1.5, target_r=2.0), open_trend)
    add(
        Cfg(
            "open_trend_pe_strict",
            until="11:15",
            sides="pe",
            adx_min=25,
            fresh_only=True,
            stop_atr=1.5,
            target_r=2.0,
            max_day=3,
            cooldown=6,
        ),
        open_trend,
    )

    # --- DI cross with ADX rising ---
    def di_cross(i, _):
        if i < 2 or None in (pdi[i], mdi[i], pdi[i - 1], mdi[i - 1], adx[i], adx[i - 1]):
            return None
        if float(adx[i]) < float(adx[i - 1]):
            return None
        if pdi[i] > mdi[i] and pdi[i - 1] <= mdi[i - 1] and c[i] > (vwap[i] or c[i]):
            return "ce"
        if mdi[i] > pdi[i] and mdi[i - 1] <= pdi[i - 1] and c[i] < (vwap[i] or c[i]):
            return "pe"
        return None

    add(Cfg("di_cross_rising_adx", until="12:00", adx_min=25, fresh_only=True, stop_atr=1.5, target_r=2.0), di_cross)
    add(Cfg("di_cross_pe", until="11:30", sides="pe", adx_min=25, fresh_only=True, stop_atr=1.5, target_r=2.0), di_cross)

    # rank
    results = [r for r in results if r["n"] >= 8]  # need some sample
    results.sort(key=lambda r: (r["net"], r["pf"], r["wr"]), reverse=True)

    print(f"bars={len(bars)}  {_day(bars[0]['t'])} -> {_day(bars[-1]['t'])}")
    print(f"scanned={len(results)} strategies with n>=8\n")
    print(f"{'rank':4} {'net':>10} {'wr%':>6} {'n':>4} {'pf':>5} {'avg':>8}  name")
    for i, r in enumerate(results[:20], 1):
        print(
            f"{i:4} {r['net']:+10.0f} {r['wr']:6.1f} {r['n']:4} {r['pf']:5.2f} {r['avg']:+8.0f}  {r['name']}"
        )
    print("\n=== BEST ===")
    if results:
        b = results[0]
        print(json.dumps(b, indent=2))
        # also show top profitable with WR>=40 if any
        hi = [r for r in results if r["net"] > 0 and r["wr"] >= 40]
        print("\n=== profitable & WR>=40 ===")
        for r in hi[:10]:
            print(f"{r['net']:+.0f} wr={r['wr']} n={r['n']} pf={r['pf']}  {r['name']}")
        if not hi:
            print("(none)")
        pos = [r for r in results if r["net"] > 0]
        print(f"\nprofitable_count={len(pos)} / {len(results)}")
    return 0


if __name__ == "__main__":
    # fix bars reference in pullback
    raise SystemExit(main())
