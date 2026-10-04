#!/usr/bin/env python3
"""Diagnostics on saved kite_5m_backtest_bars.json (gross / MFE-MAE / splits)."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BT_PATH = ROOT / "scripts" / "kite_vwap_ema_rsi_bt.py"
spec = importlib.util.spec_from_file_location("kite_bt", BT_PATH)
bt = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bt)


def main() -> int:
    data = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "kite_5m_backtest_bars.json"
    bars = json.loads(data.read_text(encoding="utf-8"))
    print("bars", len(bars), bars[0]["t"], "->", bars[-1]["t"])

    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    vols = [float(b.get("v") or 0) for b in bars]
    ema = bt._ema(closes, bt.EMA_LEN)
    rsi = bt._rsi(closes)
    atr = bt._atr(highs, lows, closes)
    vwap = bt._session_vwap(bars)
    pdi, mdi, adx = bt.wilder_dmi_series(highs, lows, closes, period=14)
    vol_avg: list[float | None] = []
    for i in range(len(vols)):
        window = [v for v in vols[max(0, i - 20) : i] if v > 0]
        vol_avg.append(sum(window) / len(window) if window else None)
    has_real_vol = sum(1 for v in vols if v > 0) / max(len(vols), 1) > 0.2

    trades: list[dict] = []
    day_count: dict[str, int] = defaultdict(int)
    cooldown = -1
    i = 0
    while i < len(bars) - 1:
        b = bars[i]
        day = bt._day(b["t"])
        hm = bt._hm(b["t"])
        if hm < bt.ENTRY_AFTER or hm > bt.ENTRY_UNTIL or bt._is_expiry_afternoon(day, hm):
            i += 1
            continue
        if day_count[day] >= bt.MAX_DAY or i < cooldown:
            i += 1
            continue
        if None in (ema[i], rsi[i], atr[i], vwap[i], adx[i]):
            i += 1
            continue
        if float(adx[i]) <= bt.ADX_MIN:
            i += 1
            continue
        c, e, w, r, a = closes[i], float(ema[i]), float(vwap[i]), float(rsi[i]), float(atr[i])
        if a <= 0:
            i += 1
            continue
        if has_real_vol and vol_avg[i] and vols[i] < bt.VOL_MULT * float(vol_avg[i]):
            i += 1
            continue
        side = None
        if c > w and c > e and r >= bt.RSI_BULL and (pdi[i] or 0) >= (mdi[i] or 0):
            side = "ce"
        elif c < w and c < e and r <= bt.RSI_BEAR and (mdi[i] or 0) >= (pdi[i] or 0):
            side = "pe"
        if not side:
            i += 1
            continue

        spot0 = c
        stop = spot0 - bt.STOP_ATR * a if side == "ce" else spot0 + bt.STOP_ATR * a
        target = (
            spot0 + bt.TARGET_R * bt.STOP_ATR * a
            if side == "ce"
            else spot0 - bt.TARGET_R * bt.STOP_ATR * a
        )
        entry_opt = bt._est_entry_premium(spot0, a) + bt.SLIP_PTS
        reason = "square_off"
        exit_spot = spot0
        exit_hm = hm
        mfe = mae = 0.0
        risk = abs(spot0 - stop) or a
        for j in range(i + 1, len(bars)):
            bj = bars[j]
            if bt._day(bj["t"]) != day:
                break
            hmj = bt._hm(bj["t"])
            if side == "ce":
                fav = (bj["h"] - spot0) / risk
                adv = (spot0 - bj["l"]) / risk
            else:
                fav = (spot0 - bj["l"]) / risk
                adv = (bj["h"] - spot0) / risk
            mfe = max(mfe, fav)
            mae = max(mae, adv)
            if side == "ce":
                if bj["l"] <= stop:
                    exit_spot, exit_hm, reason = stop, hmj, "stop"
                    break
                if bj["h"] >= target:
                    exit_spot, exit_hm, reason = target, hmj, "target"
                    break
            else:
                if bj["h"] >= stop:
                    exit_spot, exit_hm, reason = stop, hmj, "stop"
                    break
                if bj["l"] <= target:
                    exit_spot, exit_hm, reason = target, hmj, "target"
                    break
            if hmj >= bt.SQUARE:
                exit_spot, exit_hm, reason = bj["c"], hmj, "square_off"
                break
            exit_spot, exit_hm = bj["c"], hmj

        spot_move = (exit_spot - spot0) if side == "ce" else (spot0 - exit_spot)
        exit_opt = max(0.05, entry_opt - bt.SLIP_PTS + bt.DELTA * spot_move - bt.SLIP_PTS)
        entry_clean = entry_opt - bt.SLIP_PTS
        exit_clean = max(0.05, entry_clean + bt.DELTA * spot_move)
        gross = (exit_clean - entry_clean) * bt.QTY
        ch = bt._charges(entry_opt, exit_opt)
        pnl = round((exit_opt - entry_opt) * bt.QTY - ch, 2)

        hit_15_later = False
        if reason == "stop":
            for j in range(i + 1, len(bars)):
                bj = bars[j]
                if bt._day(bj["t"]) != day:
                    break
                if side == "ce" and bj["h"] >= target:
                    hit_15_later = True
                    break
                if side == "pe" and bj["l"] <= target:
                    hit_15_later = True
                    break

        bucket = (
            "09:45-11:30"
            if hm < "11:30"
            else ("11:30-13:30" if hm < "13:30" else "13:30-15:00")
        )
        adx_v = float(adx[i])
        try:
            expiry_day = date.fromisoformat(day).weekday() == 1
        except ValueError:
            expiry_day = False

        trades.append(
            {
                "day": day,
                "side": side,
                "hm": hm,
                "bucket": bucket,
                "adx_bucket": "22-25" if adx_v < 25 else "25+",
                "expiry_day": expiry_day,
                "reason": reason,
                "pnl": pnl,
                "gross": round(gross, 2),
                "mfe": round(mfe, 2),
                "mae": round(mae, 2),
                "hit_15_later": hit_15_later,
                "never_green": mfe < 0.1,
            }
        )
        day_count[day] += 1
        cooldown = i + bt.COOLDOWN_BARS
        i += 1

    n = len(trades)
    print("\n=== BASELINE ===")
    print("n", n)
    print(
        "gross_before_costs",
        round(sum(t["gross"] for t in trades), 2),
        "net_after_costs",
        round(sum(t["pnl"] for t in trades), 2),
    )
    print(
        "wins_net",
        sum(1 for t in trades if t["pnl"] > 0),
        f"{100 * sum(1 for t in trades if t['pnl'] > 0) / n:.1f}%",
    )
    print(
        "wins_gross",
        sum(1 for t in trades if t["gross"] > 0),
        f"{100 * sum(1 for t in trades if t['gross'] > 0) / n:.1f}%",
    )

    stops = [t for t in trades if t["reason"] == "stop"]

    def avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 2) if xs else None

    print("\n=== MFE/MAE (R units) ===")
    print("all mfe_avg", avg([t["mfe"] for t in trades]), "mae_avg", avg([t["mae"] for t in trades]))
    print(
        "stops mfe_avg",
        avg([t["mfe"] for t in stops]),
        "mae_avg",
        avg([t["mae"] for t in stops]),
    )
    if stops:
        later = sum(1 for t in stops if t["hit_15_later"])
        never = sum(1 for t in stops if t["never_green"])
        mfe1 = sum(1 for t in stops if t["mfe"] >= 1.0)
        print(f"stops that later hit 1.5R {later}/{len(stops)} ({100 * later / len(stops):.0f}%)")
        print(f"stops never green (mfe<0.1R) {never}/{len(stops)} ({100 * never / len(stops):.0f}%)")
        print(f"stops with mfe>=1.0R before stop {mfe1}/{len(stops)} ({100 * mfe1 / len(stops):.0f}%)")

    def split(label: str, keyfn) -> None:
        groups: dict[str, list] = defaultdict(list)
        for t in trades:
            groups[keyfn(t)].append(t)
        print(f"\n=== SPLIT {label} ===")
        for k in sorted(groups):
            g = groups[k]
            wr = 100 * sum(1 for t in g if t["pnl"] > 0) / len(g)
            print(
                f"{k:16} n={len(g):3} wr={wr:5.1f}% "
                f"net={sum(t['pnl'] for t in g):+9.0f} "
                f"gross={sum(t['gross'] for t in g):+9.0f}"
            )

    split("time", lambda t: t["bucket"])
    split("side", lambda t: t["side"])
    split("ADX", lambda t: t["adx_bucket"])
    split("expiry", lambda t: "expiry_Tue" if t["expiry_day"] else "other")

    # Experiment: fresh signals only
    print("\n=== EXPERIMENT: fresh signals only ===")
    trades_f: list[float] = []
    day_count = defaultdict(int)
    cooldown = -1
    prev = False
    i = 0
    while i < len(bars) - 1:
        day = bt._day(bars[i]["t"])
        hm = bt._hm(bars[i]["t"])
        cond = False
        side = None
        ok = hm >= bt.ENTRY_AFTER and hm <= bt.ENTRY_UNTIL and not bt._is_expiry_afternoon(day, hm)
        if ok and None not in (ema[i], rsi[i], atr[i], vwap[i], adx[i]) and float(adx[i]) > bt.ADX_MIN:
            c, e, w, r, a = closes[i], float(ema[i]), float(vwap[i]), float(rsi[i]), float(atr[i])
            vol_ok = (not has_real_vol) or not vol_avg[i] or vols[i] >= bt.VOL_MULT * float(vol_avg[i])
            if a > 0 and vol_ok:
                if c > w and c > e and r >= bt.RSI_BULL and (pdi[i] or 0) >= (mdi[i] or 0):
                    side, cond = "ce", True
                elif c < w and c < e and r <= bt.RSI_BEAR and (mdi[i] or 0) >= (pdi[i] or 0):
                    side, cond = "pe", True
        fresh = cond and not prev
        prev = cond
        if not (fresh and side and day_count[day] < bt.MAX_DAY and i >= cooldown):
            i += 1
            continue
        spot0 = closes[i]
        a = float(atr[i])
        stop = spot0 - bt.STOP_ATR * a if side == "ce" else spot0 + bt.STOP_ATR * a
        target = (
            spot0 + bt.TARGET_R * bt.STOP_ATR * a
            if side == "ce"
            else spot0 - bt.TARGET_R * bt.STOP_ATR * a
        )
        entry_opt = bt._est_entry_premium(spot0, a) + bt.SLIP_PTS
        exit_spot = spot0
        for j in range(i + 1, len(bars)):
            bj = bars[j]
            if bt._day(bj["t"]) != day:
                break
            hmj = bt._hm(bj["t"])
            if side == "ce":
                if bj["l"] <= stop:
                    exit_spot = stop
                    break
                if bj["h"] >= target:
                    exit_spot = target
                    break
            else:
                if bj["h"] >= stop:
                    exit_spot = stop
                    break
                if bj["l"] <= target:
                    exit_spot = target
                    break
            if hmj >= bt.SQUARE:
                exit_spot = bj["c"]
                break
            exit_spot = bj["c"]
        spot_move = (exit_spot - spot0) if side == "ce" else (spot0 - exit_spot)
        exit_opt = max(0.05, entry_opt - bt.SLIP_PTS + bt.DELTA * spot_move - bt.SLIP_PTS)
        pnl = round((exit_opt - entry_opt) * bt.QTY - bt._charges(entry_opt, exit_opt), 2)
        trades_f.append(pnl)
        day_count[day] += 1
        cooldown = i + bt.COOLDOWN_BARS
        i += 1
    if trades_f:
        print(
            "n",
            len(trades_f),
            "wr",
            round(100 * sum(1 for p in trades_f if p > 0) / len(trades_f), 1),
            "net",
            round(sum(trades_f), 2),
        )

    print("\n=== EXPERIMENT: stop 1.5ATR / target 2R ===")
    trades2: list[tuple[float, str]] = []
    day_count = defaultdict(int)
    cooldown = -1
    i = 0
    stop_m, tgt_m = 1.5, 2.0
    while i < len(bars) - 1:
        day = bt._day(bars[i]["t"])
        hm = bt._hm(bars[i]["t"])
        if hm < bt.ENTRY_AFTER or hm > bt.ENTRY_UNTIL or bt._is_expiry_afternoon(day, hm):
            i += 1
            continue
        if day_count[day] >= bt.MAX_DAY or i < cooldown:
            i += 1
            continue
        if None in (ema[i], rsi[i], atr[i], vwap[i], adx[i]) or float(adx[i]) <= bt.ADX_MIN:
            i += 1
            continue
        c, e, w, r, a = closes[i], float(ema[i]), float(vwap[i]), float(rsi[i]), float(atr[i])
        if a <= 0:
            i += 1
            continue
        if has_real_vol and vol_avg[i] and vols[i] < bt.VOL_MULT * float(vol_avg[i]):
            i += 1
            continue
        side = None
        if c > w and c > e and r >= bt.RSI_BULL and (pdi[i] or 0) >= (mdi[i] or 0):
            side = "ce"
        elif c < w and c < e and r <= bt.RSI_BEAR and (mdi[i] or 0) >= (pdi[i] or 0):
            side = "pe"
        if not side:
            i += 1
            continue
        spot0 = c
        stop = spot0 - stop_m * a if side == "ce" else spot0 + stop_m * a
        target = spot0 + tgt_m * stop_m * a if side == "ce" else spot0 - tgt_m * stop_m * a
        entry_opt = bt._est_entry_premium(spot0, a) + bt.SLIP_PTS
        exit_spot = c
        reason = "square_off"
        for j in range(i + 1, len(bars)):
            bj = bars[j]
            if bt._day(bj["t"]) != day:
                break
            hmj = bt._hm(bj["t"])
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
            if hmj >= bt.SQUARE:
                exit_spot, reason = bj["c"], "square_off"
                break
            exit_spot = bj["c"]
        spot_move = (exit_spot - spot0) if side == "ce" else (spot0 - exit_spot)
        exit_opt = max(0.05, entry_opt - bt.SLIP_PTS + bt.DELTA * spot_move - bt.SLIP_PTS)
        pnl = round((exit_opt - entry_opt) * bt.QTY - bt._charges(entry_opt, exit_opt), 2)
        trades2.append((pnl, reason))
        day_count[day] += 1
        cooldown = i + bt.COOLDOWN_BARS
        i += 1
    if trades2:
        wr = 100 * sum(1 for p, _ in trades2 if p > 0) / len(trades2)
        print(
            "n",
            len(trades2),
            "wr",
            round(wr, 1),
            "net",
            round(sum(p for p, _ in trades2), 2),
            "stops",
            sum(1 for _, r in trades2 if r == "stop"),
            "targets",
            sum(1 for _, r in trades2 if r == "target"),
        )

    print("\n=== EXPERIMENT: morning 09:45-11:30 only (baseline entries) ===")
    m = [t for t in trades if t["bucket"] == "09:45-11:30"]
    if m:
        print(
            "n",
            len(m),
            "wr",
            round(100 * sum(1 for t in m if t["pnl"] > 0) / len(m), 1),
            "net",
            round(sum(t["pnl"] for t in m), 2),
        )

    print("\n=== EXPERIMENT: ADX 25+ only (baseline entries) ===")
    m = [t for t in trades if t["adx_bucket"] == "25+"]
    if m:
        print(
            "n",
            len(m),
            "wr",
            round(100 * sum(1 for t in m if t["pnl"] > 0) / len(m), 1),
            "net",
            round(sum(t["pnl"] for t in m), 2),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
