#!/usr/bin/env python3
"""Fetch Kite 5m history and backtest VWAP+EMA20+RSI ATM-option proxy.

Kite limits (approx): ~100 calendar days of 5minute candles. Expired weekly
option tokens are not in today's instruments dump, so option P&L uses a
delta≈0.5 premium path + Kite NFO charges + slip (labeled option_proxy).

Usage (in container or venv with creds):
  python3 scripts/kite_vwap_ema_rsi_bt.py --days 100
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.config import load_settings  # noqa: E402
from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402
from atlas_lite.kite_rest import KiteRest  # noqa: E402
from atlas_lite.metrics import wilder_dmi_series  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
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
COOLDOWN_BARS = 4
MAX_DAY = 5
ENTRY_AFTER = "09:45"
ENTRY_UNTIL = "14:45"
SQUARE = "15:14"
SLIP_PTS = 0.5
DELTA = 0.5  # option premium ≈ delta * spot move
CHUNK_DAYS = 30


def _hm_to_min(hm: str) -> int:
    h, m = map(int, hm.split(":"))
    return h * 60 + m


def _hm(ts: str) -> str:
    return str(ts).replace("T", " ")[11:16]


def _day(ts: str) -> str:
    return str(ts).replace("T", " ")[:10]


def _ema(vals: list[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    k = 2.0 / (length + 1)
    prev: float | None = None
    seed: list[float] = []
    for i, v in enumerate(vals):
        if prev is None:
            seed.append(v)
            if len(seed) < length:
                continue
            prev = sum(seed) / length
            out[i] = prev
            continue
        prev = v * k + prev * (1.0 - k)
        out[i] = prev
    return out


def _rsi(closes: list[float], length: int = RSI_LEN) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= length:
        return out
    gains, losses = [], []
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
                max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            )
    prev = sum(trs[1 : length + 1]) / length
    out[length] = prev
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
        v = float(b.get("v") or 0.0) or 1.0
        pv += typical * v
        vol += v
        out[i] = pv / vol if vol else None
    return out


def _is_expiry_afternoon(day: str, hm: str) -> bool:
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return False
    return d.weekday() == 1 and hm >= "13:00"


def _charges(entry: float, exit_px: float) -> float:
    return float(kite_nfo_charges([(entry, QTY, "buy"), (exit_px, QTY, "sell")])["total"])


def _settle(trades: list[dict[str, Any]], key: str = "pnl") -> dict[str, Any]:
    if not trades:
        return {"n": 0, "wins": 0, "win_pct": 0.0, "pnl": 0.0, "avg": 0.0, "max_dd": 0.0}
    pnls = [float(t[key]) for t in trades]
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


def _parse_candle(c: list[Any]) -> dict[str, Any] | None:
    if not c or len(c) < 5:
        return None
    ts = str(c[0]).replace("T", " ")[:16]
    try:
        o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
        v = float(c[5]) if len(c) > 5 and c[5] is not None else 0.0
    except (TypeError, ValueError):
        return None
    return {"t": ts, "o": o, "h": h, "l": l, "c": cl, "v": v}


def _token_from_csv(path: Path, *, tradingsymbol: str | None = None, name: str | None = None) -> int | None:
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if tradingsymbol and row.get("tradingsymbol") == tradingsymbol:
                return int(row["instrument_token"])
            if name and row.get("name") == name and row.get("segment") in ("NSE", "INDICES", "NSE-IND"):
                # index rows vary by dump
                if row.get("instrument_type") in ("EQ", "INDEX", "") or row.get("segment") == "INDICES":
                    try:
                        return int(row["instrument_token"])
                    except (TypeError, ValueError):
                        continue
    return None


def _nifty_token(data: Path) -> int | None:
    nse = data / "nse_instruments.csv"
    tok = _token_from_csv(nse, tradingsymbol="NIFTY 50")
    if tok:
        return tok
    # fallback scan
    if nse.is_file():
        with nse.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                sym = (row.get("tradingsymbol") or "").strip()
                if sym in ("NIFTY 50", "NIFTY50"):
                    return int(row["instrument_token"])
    return None


def _nearest_nifty_fut_token(data: Path) -> tuple[int | None, str | None]:
    nfo = data / "nfo_instruments.csv"
    if not nfo.is_file():
        return None, None
    today = date.today()
    best = None
    with nfo.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("name") or "").strip() != "NIFTY":
                continue
            if (row.get("instrument_type") or "").strip() != "FUT":
                continue
            exp = (row.get("expiry") or "").strip()
            try:
                ed = date.fromisoformat(exp[:10])
            except ValueError:
                continue
            if ed < today:
                continue
            tok = int(row["instrument_token"])
            sym = row.get("tradingsymbol") or ""
            if best is None or ed < best[0]:
                best = (ed, tok, sym)
    if not best:
        return None, None
    return best[1], best[2]


async def _fetch_5m(rest: KiteRest, token: int, days: int, *, oi: int = 0, continuous: int = 0) -> list[dict[str, Any]]:
    """Chunked 5m fetch — Kite typically caps ~100d for 5minute."""
    now = datetime.now(IST)
    start = now - timedelta(days=max(1, days))
    out: dict[str, dict[str, Any]] = {}
    cursor = start
    while cursor < now:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), now)
        frm = cursor.strftime("%Y-%m-%d %H:%M:%S")
        to = chunk_end.strftime("%Y-%m-%d %H:%M:%S")
        try:
            candles = await rest.historical(
                token, "5minute", from_date=frm, to_date=to, continuous=continuous, oi=oi
            )
        except Exception as exc:  # noqa: BLE001
            print(f"warn historical token={token} {frm}->{to}: {exc}", file=sys.stderr)
            candles = []
        for c in candles:
            bar = _parse_candle(c)
            if not bar:
                continue
            hm = _hm(bar["t"])
            if hm < "09:15" or hm > "15:29":
                continue
            out[bar["t"]] = bar
        await asyncio.sleep(0.35)  # be polite to Kite
        cursor = chunk_end
    return [out[k] for k in sorted(out)]


def _est_entry_premium(spot: float, atr: float) -> float:
    """Rough ATM weekly premium when chain history unavailable."""
    return max(20.0, round(0.004 * spot + 0.35 * atr, 2))


def run_backtest(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if len(bars) < 50:
        return {"ok": False, "error": "not_enough_bars", "n": len(bars)}
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    vols = [float(b.get("v") or 0) for b in bars]
    has_real_vol = sum(1 for v in vols if v > 0) / max(len(vols), 1) > 0.2
    ema = _ema(closes, EMA_LEN)
    rsi = _rsi(closes)
    atr = _atr(highs, lows, closes)
    vwap = _session_vwap(bars)
    pdi_s, mdi_s, adx_s = wilder_dmi_series(highs, lows, closes, period=14)
    vol_avg: list[float | None] = []
    for i in range(len(vols)):
        window = [v for v in vols[max(0, i - 20) : i] if v > 0]
        vol_avg.append(sum(window) / len(window) if window else None)

    trades: list[dict[str, Any]] = []
    day_count: dict[str, int] = defaultdict(int)
    cooldown_until = -1
    i = 0
    while i < len(bars) - 1:
        b = bars[i]
        day = _day(b["t"])
        hm = _hm(b["t"])
        if hm < ENTRY_AFTER or hm > ENTRY_UNTIL or _is_expiry_afternoon(day, hm):
            i += 1
            continue
        if day_count[day] >= MAX_DAY or i < cooldown_until:
            i += 1
            continue
        if None in (ema[i], rsi[i], atr[i], vwap[i], adx_s[i]):
            i += 1
            continue
        if float(adx_s[i]) <= ADX_MIN:
            i += 1
            continue
        c, e, w, r, a = closes[i], float(ema[i]), float(vwap[i]), float(rsi[i]), float(atr[i])
        if a <= 0:
            i += 1
            continue
        if has_real_vol and vol_avg[i] and vols[i] < VOL_MULT * float(vol_avg[i]):
            i += 1
            continue
        side = None
        if c > w and c > e and r >= RSI_BULL and (pdi_s[i] or 0) >= (mdi_s[i] or 0):
            side = "ce"
        elif c < w and c < e and r <= RSI_BEAR and (mdi_s[i] or 0) >= (pdi_s[i] or 0):
            side = "pe"
        if side is None:
            i += 1
            continue

        spot0 = c
        stop_spot = spot0 - STOP_ATR * a if side == "ce" else spot0 + STOP_ATR * a
        target_spot = spot0 + TARGET_R * STOP_ATR * a if side == "ce" else spot0 - TARGET_R * STOP_ATR * a
        entry_opt = _est_entry_premium(spot0, a) + SLIP_PTS

        reason = "square_off"
        exit_spot = bars[-1]["c"]
        exit_hm = _hm(bars[-1]["t"])
        for j in range(i + 1, len(bars)):
            bj = bars[j]
            if _day(bj["t"]) != day:
                break
            hmj = _hm(bj["t"])
            # path uses bar extremes vs stop/target
            if side == "ce":
                if bj["l"] <= stop_spot:
                    exit_spot, exit_hm, reason = stop_spot, hmj, "stop"
                    break
                if bj["h"] >= target_spot:
                    exit_spot, exit_hm, reason = target_spot, hmj, "target"
                    break
            else:
                if bj["h"] >= stop_spot:
                    exit_spot, exit_hm, reason = stop_spot, hmj, "stop"
                    break
                if bj["l"] <= target_spot:
                    exit_spot, exit_hm, reason = target_spot, hmj, "target"
                    break
            if hmj >= SQUARE:
                exit_spot, exit_hm, reason = bj["c"], hmj, "square_off"
                break
            exit_spot, exit_hm = bj["c"], hmj

        spot_move = (exit_spot - spot0) if side == "ce" else (spot0 - exit_spot)
        # option proxy mark
        exit_opt = max(0.05, entry_opt - SLIP_PTS + DELTA * spot_move - SLIP_PTS)
        ch = _charges(entry_opt, exit_opt)
        gross = (exit_opt - entry_opt) * QTY
        pnl = round(gross - ch, 2)
        under_win = spot_move > 0
        trades.append(
            {
                "day": day,
                "side": side,
                "entry_hm": hm,
                "exit_hm": exit_hm,
                "spot0": round(spot0, 2),
                "exit_spot": round(exit_spot, 2),
                "atr": round(a, 2),
                "adx": round(float(adx_s[i]), 2),
                "rsi": round(r, 2),
                "entry_opt": round(entry_opt, 2),
                "exit_opt": round(exit_opt, 2),
                "charges": ch,
                "pnl": pnl,
                "underlying_win": under_win,
                "reason": reason,
            }
        )
        day_count[day] += 1
        cooldown_until = i + COOLDOWN_BARS
        i += 1

    under_trades = [
        {**t, "pnl": 1.0 if t["underlying_win"] else -1.0} for t in trades
    ]
    return {
        "ok": True,
        "n_bars": len(bars),
        "sessions": sorted({_day(b["t"]) for b in bars}),
        "n_sessions": len({_day(b["t"]) for b in bars}),
        "has_real_volume": has_real_vol,
        "option_proxy": {
            "delta": DELTA,
            "slip_pts": SLIP_PTS,
            "note": "Not live chain — premium path = delta*spot_move + NFO charges",
        },
        "summary_option_proxy": _settle(trades, "pnl"),
        "summary_underlying_direction": {
            "n": len(trades),
            "wins": sum(1 for t in trades if t["underlying_win"]),
            "win_pct": round(100.0 * sum(1 for t in trades if t["underlying_win"]) / len(trades), 1)
            if trades
            else 0.0,
        },
        "by_reason": {
            k: sum(1 for t in trades if t["reason"] == k)
            for k in ("target", "stop", "square_off")
        },
        "trades": trades,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=100, help="Calendar days of 5m history to request")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    data = args.data_dir
    settings = load_settings()
    rest = KiteRest(settings.api_key, settings.access_token)
    try:
        await rest.check_session()
        print("Kite session OK", flush=True)
        fut_tok, fut_sym = _nearest_nifty_fut_token(data)
        idx_tok = _nifty_token(data)
        print(f"tokens index={idx_tok} fut={fut_tok} ({fut_sym})", flush=True)
        bars: list[dict[str, Any]] = []
        source = ""
        if fut_tok:
            bars = await _fetch_5m(rest, fut_tok, args.days, oi=1, continuous=0)
            source = f"NFO fut {fut_sym} token={fut_tok}"
            # If thin (new contract), try continuous
            if len(bars) < 200:
                cont = await _fetch_5m(rest, fut_tok, args.days, oi=1, continuous=1)
                if len(cont) > len(bars):
                    bars = cont
                    source += " continuous=1"
        if len(bars) < 200 and idx_tok:
            idx_bars = await _fetch_5m(rest, idx_tok, args.days, oi=0)
            if len(idx_bars) > len(bars):
                bars = idx_bars
                source = f"NSE NIFTY 50 token={idx_tok}"
        if not bars:
            print("No candles returned", file=sys.stderr)
            return 1
        # persist for reuse
        out_path = data / "kite_5m_backtest_bars.json"
        out_path.write_text(json.dumps(bars), encoding="utf-8")
        print(f"fetched bars={len(bars)} source={source} saved={out_path}", flush=True)
        print(f"range {_day(bars[0]['t'])} -> {_day(bars[-1]['t'])}", flush=True)
    finally:
        await rest.close()

    result = run_backtest(bars)
    result["source"] = source
    result["requested_days"] = args.days
    result["disclaimer"] = (
        "Kite 5m lookback is typically ~100 days (not 12 months). "
        "Expired weekly option tokens unavailable in current instruments — "
        "option P&L is delta-proxy + NFO charges, not true chain fills."
    )
    slim = {k: v for k, v in result.items() if k != "trades"}
    print(json.dumps(slim, indent=2))
    print("\n--- sample trades (first 15) ---")
    for t in (result.get("trades") or [])[:15]:
        print(
            f"{t['day']} {t['entry_hm']}->{t['exit_hm']} {t['side'].upper()} "
            f"pnl={t['pnl']:+.0f} {t['reason']} under_win={t['underlying_win']}"
        )
    s = result.get("summary_option_proxy") or {}
    u = result.get("summary_underlying_direction") or {}
    print(
        f"\nOPTION_PROXY WIN_RATE={s.get('win_pct')}% n={s.get('n')} "
        f"net={s.get('pnl')} | UNDERLYING DIR WIN_RATE={u.get('win_pct')}%"
    )
    # also write full result
    (data / "kite_vwap_ema_rsi_bt_result.json").write_text(
        json.dumps(result, indent=2)[:2_000_000], encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
