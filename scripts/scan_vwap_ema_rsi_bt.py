#!/usr/bin/env python3
"""Best-effort backtest: 5m VWAP+EMA20+RSI+vol+ADX → ATM weekly option.

Data: OCI recordings (ATM CE/PE) + minute_bars.json volume when available.
NOT 12-month futures chain — only what is on disk (~2 weeks of sessions).

Rules (approx of requested book):
- Signal on 5m bars from spot (prefer minute_bars OHLC+V; else recording spot)
- Long CE / long PE on VWAP+EMA20 alignment, RSI≥55 / ≤45, vol≥1.2×20avg
- ADX > 22 (from tape when present, else Wilder on 5m)
- Stop 1.0×ATR (underlying), target 1.75R; 4-bar (5m) cooldown; max 5/day
- Skip expiry afternoons (Tue ≥13:00 IST — NIFTY weekly)
- Costs: Kite NFO + 0.5pt slippage/side on option premium
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402
from atlas_lite.metrics import wilder_dmi_series  # noqa: E402
from atlas_lite.recorder import record_dir  # noqa: E402
from scripts.score_recordings import (  # noqa: E402
    _hm_to_min,
    _index_by_day,
    load_minute_rows,
)

QTY = 65
ADX_MIN = 22.0
RSI_BULL = 55.0
RSI_BEAR = 45.0
VOL_MULT = 1.2
EMA_LEN = 20
ATR_LEN = 14
RSI_LEN = 14
STOP_ATR = 1.0
TARGET_R = 1.75
COOLDOWN_BARS = 4  # 5m bars
MAX_DAY = 5
ENTRY_AFTER = "09:45"
ENTRY_UNTIL = "14:45"
SQUARE = "15:14"
SLIP_PTS = 0.5  # option pts per side


def _hm(ts: str) -> str:
    return str(ts).replace("T", " ")[11:16]


def _day(ts: str) -> str:
    return str(ts).replace("T", " ")[:10]


def _charges(entry: float, exit_px: float) -> float:
    legs = [(entry, QTY, "buy"), (exit_px, QTY, "sell")]
    return float(kite_nfo_charges(legs)["total"])


def _ema(vals: list[float | None], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    k = 2.0 / (length + 1)
    prev: float | None = None
    seed: list[float] = []
    for i, v in enumerate(vals):
        if v is None:
            continue
        if prev is None:
            seed.append(float(v))
            if len(seed) < length:
                continue
            prev = sum(seed) / length
            out[i] = prev
            continue
        prev = float(v) * k + prev * (1.0 - k)
        out[i] = prev
    return out


def _rsi(closes: list[float], length: int = RSI_LEN) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= length:
        return out
    gains = []
    losses = []
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains) / length
    avg_l = sum(losses) / length
    out[length] = 100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))
    for i in range(length + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (length - 1) + max(d, 0.0)) / length
        avg_l = (avg_l * (length - 1) + max(-d, 0.0)) / length
        out[i] = 100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))
    return out


def _atr(highs: list[float], lows: list[float], closes: list[float], length: int = ATR_LEN) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < length + 1:
        return out
    trs: list[float] = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )
    first = sum(trs[1 : length + 1]) / length
    out[length] = first
    prev = first
    for i in range(length + 1, len(closes)):
        prev = (prev * (length - 1) + trs[i]) / length
        out[i] = prev
    return out


def _session_vwap(bars: list[dict[str, Any]]) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    pv = vol = 0.0
    day = ""
    for i, b in enumerate(bars):
        d = _day(b["t"])
        if d != day:
            day = d
            pv = vol = 0.0
        typical = (b["h"] + b["l"] + b["c"]) / 3.0
        v = float(b.get("v") or 0.0)
        if v <= 0:
            v = 1.0  # equal-weight fallback when volume missing
        pv += typical * v
        vol += v
        out[i] = pv / vol if vol > 0 else None
    return out


def _agg_5m(bars_1m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for b in bars_1m:
        hm = _hm(b["t"])
        if hm < "09:15" or hm > "15:29":
            continue
        mins = _hm_to_min(hm)
        slot = (mins // 5) * 5
        buckets[(_day(b["t"]), slot)].append(b)
    out: list[dict[str, Any]] = []
    for (day, slot), xs in sorted(buckets.items()):
        if not xs:
            continue
        end = slot + 4
        out.append(
            {
                "t": f"{day} {end // 60:02d}:{end % 60:02d}",
                "o": float(xs[0]["o"]),
                "h": max(float(x["h"]) for x in xs),
                "l": min(float(x["l"]) for x in xs),
                "c": float(xs[-1]["c"]),
                "v": sum(float(x.get("v") or 0) for x in xs),
                "has_vol": any(float(x.get("v") or 0) > 0 for x in xs),
            }
        )
    return out


def _load_minute_bars(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    bars = raw.get("bars") if isinstance(raw, dict) else raw
    out = []
    for b in bars or []:
        out.append(
            {
                "t": str(b["t"]).replace("T", " ")[:16],
                "o": float(b["o"]),
                "h": float(b["h"]),
                "l": float(b["l"]),
                "c": float(b["c"]),
                "v": float(b.get("v") or 0),
            }
        )
    return out


def _bars_from_recordings(by_day: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """1m OHLC proxy from recording spot (single print → o=h=l=c)."""
    out = []
    for day in sorted(by_day):
        for hm in sorted(by_day[day]):
            spot = by_day[day][hm].get("spot")
            if spot is None:
                continue
            px = float(spot)
            out.append({"t": f"{day} {hm}", "o": px, "h": px, "l": px, "c": px, "v": 0.0})
    return out


def _is_expiry_afternoon(day: str, hm: str) -> bool:
    """NIFTY weekly expiry Tue — skip from 13:00."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return False
    return d.weekday() == 1 and hm >= "13:00"


def _settle(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"n": 0, "wins": 0, "win_pct": 0.0, "pnl": 0.0, "avg": 0.0, "max_dd": 0.0}
    pnls = [float(t["pnl"]) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {
        "n": len(trades),
        "wins": wins,
        "win_pct": round(100.0 * wins / len(pnls), 1),
        "pnl": round(sum(pnls), 2),
        "avg": round(sum(pnls) / len(pnls), 2),
        "max_dd": round(dd, 2),
    }


def run() -> dict[str, Any]:
    data = ROOT / "data"
    rec = record_dir(data)
    rows = load_minute_rows(rec)
    by_day = _index_by_day(rows)
    mb = _load_minute_bars(data / "minute_bars.json")
    # Prefer real OHLC+V bars; fill gaps with recording spot proxies.
    mb_days = { _day(b["t"]) for b in mb }
    rec_1m = _bars_from_recordings(by_day)
    merged: dict[str, dict[str, Any]] = {}
    for b in rec_1m:
        merged[b["t"]] = b
    for b in mb:
        merged[b["t"]] = b  # overwrite with true OHLC+V
    bars_1m = [merged[k] for k in sorted(merged)]
    bars = _agg_5m(bars_1m)
    if len(bars) < 40:
        return {"ok": False, "error": "not_enough_5m_bars", "n_5m": len(bars)}

    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    vols = [float(b.get("v") or 0) for b in bars]
    ema = _ema(closes, EMA_LEN)
    rsi = _rsi(closes, RSI_LEN)
    atr = _atr(highs, lows, closes, ATR_LEN)
    vwap = _session_vwap(bars)
    # ADX on 5m
    pdi_s, mdi_s, adx_s = wilder_dmi_series(highs, lows, closes, period=14)
    vol_avg = []
    for i in range(len(vols)):
        window = [v for v in vols[max(0, i - 20) : i] if v > 0]
        vol_avg.append(sum(window) / len(window) if window else None)

    trades: list[dict[str, Any]] = []
    day_count: dict[str, int] = defaultdict(int)
    cooldown_until_i = -1
    i = 0
    while i < len(bars) - 1:
        b = bars[i]
        day = _day(b["t"])
        hm = _hm(b["t"])
        if hm < ENTRY_AFTER or hm > ENTRY_UNTIL:
            i += 1
            continue
        if _is_expiry_afternoon(day, hm):
            i += 1
            continue
        if day_count[day] >= MAX_DAY:
            i += 1
            continue
        if i < cooldown_until_i:
            i += 1
            continue
        if None in (ema[i], rsi[i], atr[i], vwap[i], adx_s[i]):
            i += 1
            continue
        adx_val = float(adx_s[i])
        # Prefer live tape ADX at this minute if present.
        tape = by_day.get(day, {}).get(hm) or {}
        if tape.get("adx") is not None:
            adx_val = float(tape["adx"])
        if adx_val <= ADX_MIN:
            i += 1
            continue
        c = closes[i]
        e = float(ema[i])
        w = float(vwap[i])
        r = float(rsi[i])
        a = float(atr[i])
        if a <= 0:
            i += 1
            continue
        # Volume filter when we have real volume on this bucket.
        if b.get("has_vol") and vol_avg[i]:
            if vols[i] < VOL_MULT * float(vol_avg[i]):
                i += 1
                continue
        side = None  # "ce" / "pe"
        if c > w and c > e and r >= RSI_BULL and (pdi_s[i] or 0) >= (mdi_s[i] or 0):
            side = "ce"
        elif c < w and c < e and r <= RSI_BEAR and (mdi_s[i] or 0) >= (pdi_s[i] or 0):
            side = "pe"
        if side is None:
            i += 1
            continue
        field = side
        entry_row = by_day.get(day, {}).get(hm)
        if not entry_row or entry_row.get(field) is None:
            i += 1
            continue
        entry_opt = float(entry_row[field]) + SLIP_PTS  # buy slip
        if entry_opt <= 0:
            i += 1
            continue
        spot0 = float(entry_row.get("spot") or c)
        stop_spot = spot0 - STOP_ATR * a if side == "ce" else spot0 + STOP_ATR * a
        target_spot = (
            spot0 + TARGET_R * STOP_ATR * a if side == "ce" else spot0 - TARGET_R * STOP_ATR * a
        )

        # Walk subsequent 1m tape until square / stop / target.
        exit_px = None
        exit_hm = None
        reason = "time"
        start_m = _hm_to_min(hm) + 1
        end_m = _hm_to_min(SQUARE)
        last_opt = None
        for m in range(start_m, end_m + 1):
            hms = f"{m // 60:02d}:{m % 60:02d}"
            row = by_day.get(day, {}).get(hms)
            if not row:
                continue
            spot = row.get("spot")
            opt = row.get(field)
            if opt is not None:
                last_opt = float(opt)
            if spot is None or opt is None:
                continue
            spot = float(spot)
            if side == "ce":
                if spot <= stop_spot:
                    exit_px, exit_hm, reason = float(opt) - SLIP_PTS, hms, "stop"
                    break
                if spot >= target_spot:
                    exit_px, exit_hm, reason = float(opt) - SLIP_PTS, hms, "target"
                    break
            else:
                if spot >= stop_spot:
                    exit_px, exit_hm, reason = float(opt) - SLIP_PTS, hms, "stop"
                    break
                if spot <= target_spot:
                    exit_px, exit_hm, reason = float(opt) - SLIP_PTS, hms, "target"
                    break
        if exit_px is None:
            if last_opt is None:
                i += 1
                continue
            exit_px = last_opt - SLIP_PTS
            exit_hm = SQUARE
            reason = "square_off"
        exit_px = max(0.05, float(exit_px))
        ch = _charges(entry_opt, exit_px)
        gross = (exit_px - entry_opt) * QTY
        pnl = round(gross - ch, 2)
        trades.append(
            {
                "day": day,
                "side": side,
                "entry_hm": hm,
                "exit_hm": exit_hm,
                "spot0": round(spot0, 2),
                "atr": round(a, 2),
                "adx": round(adx_val, 2),
                "rsi": round(r, 2),
                "entry_opt": round(entry_opt, 2),
                "exit_opt": round(exit_px, 2),
                "gross": round(gross, 2),
                "charges": ch,
                "pnl": pnl,
                "reason": reason,
                "vol_ok": bool(b.get("has_vol")),
                "signal_from_mb": day in mb_days,
            }
        )
        day_count[day] += 1
        cooldown_until_i = i + COOLDOWN_BARS
        i += 1

    summary = _settle(trades)
    by_reason: dict[str, int] = defaultdict(int)
    for t in trades:
        by_reason[str(t["reason"])] += 1
    return {
        "ok": True,
        "disclaimer": (
            "Best-effort on OCI disk only (~2 weeks recordings). "
            "Not 12m NIFTY futures chain. Spot proxy when fut absent; "
            "volume filter only on minute_bars days."
        ),
        "sessions": sorted({r["date"] for r in rows}),
        "n_sessions": len({r["date"] for r in rows}),
        "n_5m_bars": len(bars),
        "minute_bars_days": sorted(mb_days),
        "rules": {
            "tf": "5m",
            "entry": "VWAP+EMA20+RSI55/45+vol1.2x+ADX>22+DI",
            "stop_atr": STOP_ATR,
            "target_R": TARGET_R,
            "cooldown_5m_bars": COOLDOWN_BARS,
            "max_day": MAX_DAY,
            "slip_pts_side": SLIP_PTS,
            "qty": QTY,
            "skip_expiry_afternoon": "Tue>=13:00",
        },
        "summary": summary,
        "by_reason": dict(by_reason),
        "trades": trades,
    }


if __name__ == "__main__":
    out = run()
    print(json.dumps({k: v for k, v in out.items() if k != "trades"}, indent=2))
    print("\n--- trades ---")
    for t in out.get("trades") or []:
        print(
            f"{t['day']} {t['entry_hm']}->{t['exit_hm']} {t['side'].upper()} "
            f"pnl={t['pnl']:+.0f} {t['reason']} rsi={t['rsi']} adx={t['adx']}"
        )
    s = out.get("summary") or {}
    print(
        f"\nWIN_RATE={s.get('win_pct')}%  n={s.get('n')}  "
        f"wins={s.get('wins')}  net={s.get('pnl')}  avg={s.get('avg')}"
    )
