#!/usr/bin/env python3
"""Build AI-agent training rows from Kite minute candles and retrain.

Uses contracts still in the instrument file. For each past session, the row is
the front expiry's at-the-money call and put, plus the Nifty minute bar. Tape
minutes are kept when both exist. Does not change the scorecard agent and does
not place orders.

  python3 scripts/fetch_ai_agent_history.py
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.ai_agent_model import save_model, walk_forward  # noqa: E402
from atlas_lite.config import load_settings  # noqa: E402
from atlas_lite.instruments import NIFTY_STRIKE_STEP, round_atm_strike  # noqa: E402
from atlas_lite.kite_rest import KiteRest  # noqa: E402
from atlas_lite.metrics import wilder_dmi_series, wilder_smooth  # noqa: E402
from atlas_lite.minute_bars import is_cash_session_minute  # noqa: E402
from scripts.score_recordings import load_minute_rows  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
DATA = ROOT / "data"
CACHE = DATA / "ai_agent_kite_cache.json"
ROWS_OUT = DATA / "ai_agent_kite_rows.json"
MODEL_OUT = ROOT / "atlas_lite" / "ai_agent_model.json"
# Near listed expiries only. A monthly a year out is a different product from the front week.
NEAR_EXPIRY_DAYS = 35


def _parse_ts(raw: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _atr_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    n = len(closes)
    out: list[float | None] = [None] * n
    trs: list[float] = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return out
    atr = sum(trs[:period]) / period
    out[period] = round(atr, 2)
    for i in range(period, len(trs)):
        atr = wilder_smooth(atr, trs[i], period)
        out[i + 1] = round(atr, 2)
    return out


def _listed_cutoff(today: date) -> date:
    return today + timedelta(days=NEAR_EXPIRY_DAYS)


def _load_contracts(nfo: Path, *, today: date) -> tuple[dict[tuple[str, int, str], int], list[date]]:
    """(expiry iso, strike, CE|PE) -> token, and sorted expiries inside the near window."""
    cutoff = _listed_cutoff(today)
    found: dict[tuple[str, int, str], int] = {}
    expiries: set[date] = set()
    with nfo.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("name") or "").strip().upper() != "NIFTY":
                continue
            side = (row.get("instrument_type") or "").strip().upper()
            if side not in {"CE", "PE"}:
                continue
            try:
                exp = date.fromisoformat((row.get("expiry") or "")[:10])
                strike = int(float(row.get("strike") or 0))
                token = int(row.get("instrument_token") or 0)
            except ValueError:
                continue
            if token <= 0 or strike <= 0 or exp > cutoff:
                continue
            found[(exp.isoformat(), strike, side)] = token
            expiries.add(exp)
    return found, sorted(expiries)


def _nifty_token(nse: Path) -> int:
    with nse.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("tradingsymbol") or "").strip()
            if sym in ("NIFTY 50", "NIFTY50"):
                return int(row["instrument_token"])
    raise RuntimeError("NIFTY 50 token missing from nse_instruments.csv")


def _front_windows(expiries: list[date]) -> list[tuple[date, date, date]]:
    """(start, expiry, expiry) active days for the front contract."""
    windows: list[tuple[date, date, date]] = []
    for i, exp in enumerate(expiries):
        if i == 0:
            start = exp - timedelta(days=6)
        else:
            start = expiries[i - 1] + timedelta(days=1)
        if start > exp:
            continue
        windows.append((start, exp, exp))
    return windows


def _last_candle_day(candles: list[Any]) -> date | None:
    last: date | None = None
    for candle in candles:
        if not candle:
            continue
        dt = _parse_ts(candle[0])
        if dt is None:
            continue
        if last is None or dt.date() > last:
            last = dt.date()
    return last


def _candles_to_map(candles: list[list[Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for candle in candles:
        if not candle or len(candle) < 5:
            continue
        dt = _parse_ts(candle[0])
        if dt is None:
            continue
        try:
            close = float(candle[4])
        except (TypeError, ValueError):
            continue
        body: dict[str, float] = {"c": close}
        if len(candle) > 6 and candle[6] not in (None, ""):
            try:
                body["oi"] = float(candle[6])
            except (TypeError, ValueError):
                pass
        out[_key(dt)] = body
    return out


async def _fetch(rest: KiteRest, token: int, start: date, end: date, *, oi: int) -> list[list[Any]]:
    frm = f"{start.isoformat()} 09:15:00"
    to = f"{end.isoformat()} 15:30:00"
    try:
        return await rest.historical(token, "minute", from_date=frm, to_date=to, oi=oi)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "403" in text or "token" in text.lower():
            raise RuntimeError("Kite rejected the session. Refresh the access token and run this again.") from exc
        print(f"warn historical token={token} {start}->{end}: {exc}", file=sys.stderr)
        return []


async def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Kite minutes and retrain the AI agent")
    parser.add_argument("--tape", type=Path, default=DATA / "recordings")
    parser.add_argument("--out", type=Path, default=MODEL_OUT)
    args = parser.parse_args()

    nfo = DATA / "nfo_instruments.csv"
    nse = DATA / "nse_instruments.csv"
    if not nfo.is_file() or not nse.is_file():
        print("missing instrument csv under data/", file=sys.stderr)
        return 1
    contracts, expiries = _load_contracts(nfo, today=datetime.now(IST).date())
    if not expiries:
        print("no Nifty option expiries in the instrument file", file=sys.stderr)
        return 1
    windows = _front_windows(expiries)
    index_start = min(start for start, _, _ in windows) - timedelta(days=5)
    index_end = max(exp for _, exp, _ in windows)
    today = datetime.now(IST).date()
    if index_end > today:
        index_end = today

    cache: dict[str, Any] = {}
    if CACHE.is_file():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    index_cache = cache.get("index") if isinstance(cache.get("index"), list) else []
    option_cache = cache.get("options") if isinstance(cache.get("options"), dict) else {}

    settings = load_settings()
    rest = KiteRest(settings.api_key, settings.access_token)
    try:
        index_through = _last_candle_day(index_cache)
        if index_through is None or index_through < index_end:
            candles = await _fetch(rest, _nifty_token(nse), index_start, index_end, oi=0)
            if candles:
                index_cache = candles
            await asyncio.sleep(0.35)
        bars: list[dict[str, Any]] = []
        for candle in index_cache:
            dt = _parse_ts(candle[0]) if candle else None
            if dt is None or not is_cash_session_minute(_key(dt)):
                continue
            try:
                bars.append(
                    {
                        "dt": dt,
                        "o": float(candle[1]),
                        "h": float(candle[2]),
                        "l": float(candle[3]),
                        "c": float(candle[4]),
                    }
                )
            except (TypeError, ValueError, IndexError):
                continue
        bars.sort(key=lambda b: b["dt"])
        if len(bars) < 40:
            print(f"Kite returned only {len(bars)} Nifty minutes", file=sys.stderr)
            return 1

        needed: set[tuple[str, int, str]] = set()
        by_day_spot: dict[str, list[dict[str, Any]]] = {}
        for bar in bars:
            by_day_spot.setdefault(bar["dt"].date().isoformat(), []).append(bar)
        for start, exp, _ in windows:
            exp_iso = exp.isoformat()
            day = start
            while day <= min(exp, today):
                day_bars = by_day_spot.get(day.isoformat()) or []
                if day_bars:
                    low = min(b["l"] for b in day_bars)
                    high = max(b["h"] for b in day_bars)
                    strike = round_atm_strike(low) - NIFTY_STRIKE_STEP
                    end_strike = round_atm_strike(high) + NIFTY_STRIKE_STEP
                    while strike <= end_strike:
                        for side in ("CE", "PE"):
                            key = (exp_iso, strike, side)
                            if key in contracts:
                                needed.add(key)
                        strike += NIFTY_STRIKE_STEP
                day += timedelta(days=1)

        fetched = 0
        for exp_iso, strike, side in sorted(needed):
            cache_key = f"{exp_iso}|{strike}|{side}"
            exp = date.fromisoformat(exp_iso)
            covered = _last_candle_day(option_cache.get(cache_key) or [])
            if covered is not None and covered >= min(exp, today):
                continue
            window = next((w for w in windows if w[1] == exp), None)
            if window is None:
                continue
            candles = await _fetch(
                rest,
                contracts[(exp_iso, strike, side)],
                window[0],
                min(exp, today),
                oi=1,
            )
            option_cache[cache_key] = candles
            fetched += 1
            await asyncio.sleep(0.35)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(
            json.dumps({"index": index_cache, "options": option_cache}),
            encoding="utf-8",
        )
        print(f"contracts needed={len(needed)} fetched_now={fetched}")
    finally:
        await rest.close()

    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    closes = [b["c"] for b in bars]
    _, _, adx = wilder_dmi_series(highs, lows, closes, 14)
    atr = _atr_series(highs, lows, closes, 14)
    option_maps = {key: _candles_to_map(candles) for key, candles in option_cache.items()}

    prev_close: float | None = None
    kite_rows: list[dict[str, Any]] = []
    last_day = ""
    day_open: float | None = None
    for i, bar in enumerate(bars):
        day = bar["dt"].date()
        day_iso = day.isoformat()
        if day_iso != last_day:
            prev_close = closes[i - 1] if i and bars[i - 1]["dt"].date().isoformat() != day_iso else prev_close
            # previous day's last close
            if i:
                prev_day = bars[i - 1]["dt"].date()
                if prev_day != day:
                    prev_close = bars[i - 1]["c"]
            day_open = next(
                (b["o"] for b in bars[i:] if b["dt"].date() == day and b["dt"].strftime("%H:%M") >= "09:15"),
                bar["o"],
            )
            last_day = day_iso
        exp = next((e for s, e, _ in windows if s <= day <= e), None)
        if exp is None or day_open in (None, 0):
            continue
        hm = bar["dt"].strftime("%H:%M")
        if hm < "09:45" or hm > "15:14":
            continue
        strike = round_atm_strike(bar["c"])
        stamp = _key(bar["dt"])
        ce = option_maps.get(f"{exp.isoformat()}|{strike}|CE", {}).get(stamp)
        pe = option_maps.get(f"{exp.isoformat()}|{strike}|PE", {}).get(stamp)
        if not ce or not pe or ce["c"] <= 0 or pe["c"] <= 0:
            continue
        nifty_chg = None
        if prev_close not in (None, 0):
            nifty_chg = round((bar["c"] - float(prev_close)) / float(prev_close) * 100.0, 3)
        kite_rows.append(
            {
                "date": day_iso,
                "hm": hm,
                "atm": strike,
                "spot": bar["c"],
                "ce": ce["c"],
                "pe": pe["c"],
                "adx": adx[i],
                "atr": atr[i],
                "pcr": None,
                "nifty_chg": nifty_chg,
                "spot_chg_open_pct": round((bar["c"] - float(day_open)) / float(day_open) * 100.0, 3),
                "source": "kite",
            }
        )

    tape_rows = load_minute_rows(args.tape) if args.tape.is_dir() else []
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in kite_rows:
        merged[(row["date"], row["hm"])] = row
    for row in tape_rows:
        merged[(str(row["date"]), str(row["hm"]))] = row
    rows = [merged[key] for key in sorted(merged)]
    ROWS_OUT.write_text(json.dumps(rows), encoding="utf-8")
    report = walk_forward(rows)
    save_model(args.out, report)
    kite_days = sorted({r["date"] for r in kite_rows})
    summary = {
        "kite_minutes": len(kite_rows),
        "kite_days": kite_days,
        "tape_minutes": len(tape_rows),
        "merged_minutes": len(rows),
        "merged_days": sorted({r["date"] for r in rows}),
        "promoted": report.get("promoted"),
        "reason": report.get("reason"),
        "train_days": report.get("train_days"),
        "test_days": report.get("test_days"),
        "test_net": report.get("test_net"),
        "test_trades": report.get("test_trades"),
        "scorecard_test_net": report.get("scorecard_test_net"),
        "scorecard_test_trades": report.get("scorecard_test_trades"),
        "out": str(args.out),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
