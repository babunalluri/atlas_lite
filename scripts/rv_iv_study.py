#!/usr/bin/env python3
"""Join 252d ATM IV history to Kite NIFTY daily bars (RV − IV).

  python3 scripts/rv_iv_study.py
  python3 scripts/rv_iv_study.py --json

Does not change paper side (still long straddle). Capital in the report is ₹2L.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.config import load_settings  # noqa: E402
from atlas_lite.instruments import lookup_token  # noqa: E402
from atlas_lite.iv_history import (  # noqa: E402
    IVP_HISTORY_FILE,
    compute_ivp,
    load_iv_history,
)
from atlas_lite.kite_rest import KiteRest  # noqa: E402
from atlas_lite.specs import NIFTY_SYMBOL  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "regime" / "rv_iv_study.json"
TRADING_DAYS = 252.0
RV_WINDOW = 20
BUY_IVP_LT = 35.0
SELL_IVP_GT = 65.0
CAPITAL = 200_000.0
SHORT_STRADDLE_MARGIN_LO = 130_000.0
SHORT_STRADDLE_MARGIN_HI = 160_000.0
LOT_SIZE = 65
# Nearest weekly ATM ≈ 6 calendar DTE (matches S×IV×√T×√(2/π) ≈ 249 pts at IV 10.2).
STRADDLE_T_YEARS = 6.0 / 365.0
FLY_WING_PTS = 250.0
FLY_CREDIT_KEEP = 0.67
CUSHION_TIGHT = CAPITAL - SHORT_STRADDLE_MARGIN_HI  # ₹40k
CUSHION_LOOSE = CAPITAL - SHORT_STRADDLE_MARGIN_LO  # ₹70k
VOL_OF_VOL_PTS = 3.0
VOL_OF_VOL_LOOKBACK = 5


def _load_csvs() -> list[str]:
    csvs: list[str] = []
    for name in ("nse_instruments.csv", "nfo_instruments.csv", "bse_instruments.csv"):
        path = DATA_DIR / name
        if path.is_file():
            csvs.append(path.read_text(encoding="utf-8"))
    if not csvs:
        raise FileNotFoundError(f"No instrument CSVs under {DATA_DIR}")
    return csvs


def _candle_day(raw: Any) -> str | None:
    text = str(raw).replace("Z", "+00:00")
    # Kite sends +0530 (no colon); datetime.fromisoformat wants +05:30.
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).date().isoformat()


def parkinson_rv_pct(high: float, low: float) -> float | None:
    if high <= 0 or low <= 0 or high < low:
        return None
    hl = math.log(high / low)
    var = (hl * hl) / (4.0 * math.log(2.0))
    return math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100.0


def close_to_close_rv_pct(closes: list[float], *, window: int = RV_WINDOW) -> float | None:
    if len(closes) < window + 1:
        return None
    rets = []
    slice_ = closes[-(window + 1) :]
    for prev, cur in zip(slice_, slice_[1:]):
        if prev <= 0 or cur <= 0:
            return None
        rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(TRADING_DAYS) * 100.0


def abs_move_ann_pct(prev: float, close: float) -> float | None:
    if prev <= 0 or close <= 0:
        return None
    return abs(math.log(close / prev)) * math.sqrt(TRADING_DAYS) * 100.0


def atm_straddle_credit_pts(spot: float, iv_pct: float, *, t_years: float = STRADDLE_T_YEARS) -> float:
    """Black-Scholes ATM straddle ≈ S σ √T √(2/π)."""
    return float(spot) * (float(iv_pct) / 100.0) * math.sqrt(t_years) * math.sqrt(2.0 / math.pi)


def move_pts_from_fwd1(spot: float, fwd1: float) -> float:
    """Conservative |ΔS| from annualized abs log-return (up-move size)."""
    abs_log = float(fwd1) / (100.0 * math.sqrt(TRADING_DAYS))
    return float(spot) * (math.exp(abs_log) - 1.0)


def _pnl_stats(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {"n": 0}
    s = sorted(pnls)
    n = len(s)

    def q(p: float) -> float:
        i = min(n - 1, max(0, int(round(p * (n - 1)))))
        return round(s[i], 2)

    return {
        "n": n,
        "mean": round(sum(s) / n, 2),
        "median": round(statistics.median(s), 2),
        "p05": q(0.05),
        "p01": q(0.01),
        "min": round(s[0], 2),
        "max": round(s[-1], 2),
        "total": round(sum(s), 2),
        "win_pct": round(100.0 * sum(1 for x in s if x > 0) / n, 1),
        "loss_gt_40k": sum(1 for x in s if x < -CUSHION_TIGHT),
        "loss_gt_70k": sum(1 for x in s if x < -CUSHION_LOOSE),
        "loss_gt_2l": sum(1 for x in s if x < -CAPITAL),
    }


def short_straddle_pnl(joined: list[dict[str, Any]]) -> dict[str, Any]:
    """Overnight P&L: 6-DTE ATM short credit − next-day |spot move|, 1 lot."""
    iv_by_day = {str(r["day"]): float(r["iv"]) for r in joined if r.get("iv") is not None}
    days_sorted = [str(r["day"]) for r in joined]
    day_ix = {d: i for i, d in enumerate(days_sorted)}
    rows: list[dict[str, Any]] = []
    for rec in joined:
        close = rec.get("close")
        fwd1 = rec.get("fwd1")
        iv = rec.get("iv")
        if close is None or fwd1 is None or iv is None or close <= 0:
            continue
        credit = atm_straddle_credit_pts(float(close), float(iv))
        move = move_pts_from_fwd1(float(close), float(fwd1))
        naked_pts = credit - move
        naked_inr = round(naked_pts * LOT_SIZE, 2)
        fly_credit = min(credit * FLY_CREDIT_KEEP, FLY_WING_PTS * 0.5)
        fly_pts = min(fly_credit, max(fly_credit - FLY_WING_PTS, fly_credit - move))
        fly_inr = round(fly_pts * LOT_SIZE, 2)
        day = str(rec["day"])
        i = day_ix[day]
        iv_prev = None
        if i >= VOL_OF_VOL_LOOKBACK:
            prev_day = days_sorted[i - VOL_OF_VOL_LOOKBACK]
            iv_prev = iv_by_day.get(prev_day)
        iv_chg = None if iv_prev is None else round(float(iv) - iv_prev, 3)
        hot = iv_chg is not None and iv_chg > VOL_OF_VOL_PTS
        ivp = rec.get("ivp")
        rv20 = rec.get("rv20")
        sell = (
            ivp is not None
            and rv20 is not None
            and float(ivp) > SELL_IVP_GT
            and float(rv20) < float(iv)
        )
        buy = (
            ivp is not None
            and rv20 is not None
            and float(ivp) < BUY_IVP_LT
            and float(rv20) > float(iv)
        )
        rows.append(
            {
                "day": day,
                "iv": round(float(iv), 2),
                "credit": round(credit, 1),
                "move": round(move, 1),
                "naked_inr": naked_inr,
                "fly_inr": fly_inr,
                "iv_chg_5d": iv_chg,
                "vol_of_vol_hot": hot,
                "sell": sell,
                "buy": buy,
            }
        )

    naked = [r["naked_inr"] for r in rows]
    fly = [r["fly_inr"] for r in rows]
    sell_rows = [r for r in rows if r["sell"]]
    sell_cool = [r for r in sell_rows if not r["vol_of_vol_hot"]]
    worst = sorted(rows, key=lambda r: r["naked_inr"])[:5]
    return {
        "model": "6dte_atm_short_credit_minus_nextday_|dS|",
        "lot": LOT_SIZE,
        "t_days": 6,
        "capital": CAPITAL,
        "cushion_tight": CUSHION_TIGHT,
        "cushion_loose": CUSHION_LOOSE,
        "every_day_naked": _pnl_stats(naked),
        "every_day_fly_250": _pnl_stats(fly),
        "sell_regime_naked": _pnl_stats([r["naked_inr"] for r in sell_rows]),
        "sell_regime_fly": _pnl_stats([r["fly_inr"] for r in sell_rows]),
        "sell_cool_naked": _pnl_stats([r["naked_inr"] for r in sell_cool]),
        "worst5_naked": [
            {
                "day": r["day"],
                "iv": r["iv"],
                "credit": r["credit"],
                "move": r["move"],
                "pnl": r["naked_inr"],
                "pnl_pct_2l": round(r["naked_inr"] / CAPITAL * 100.0, 1),
                "sell": r["sell"],
                "vol_of_vol_hot": r["vol_of_vol_hot"],
            }
            for r in worst
        ],
    }


def _pctile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return float("nan")
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _med(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


async def fetch_nifty_daily(rest: KiteRest, token: int, *, days: int = 420) -> list[dict[str, float | str]]:
    now = datetime.now(IST)
    start = now - timedelta(days=days)
    candles = await rest.historical(
        token,
        "day",
        from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
        to_date=now.strftime("%Y-%m-%d %H:%M:%S"),
    )
    rows: list[dict[str, float | str]] = []
    for c in candles:
        if not isinstance(c, (list, tuple)) or len(c) < 5:
            continue
        day = _candle_day(c[0])
        if day is None:
            continue
        o, h, low, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
        rows.append({"day": day, "open": o, "high": h, "low": low, "close": cl})
    rows.sort(key=lambda r: str(r["day"]))
    return rows


def join_study(
    iv_rows: list[dict[str, Any]],
    bars: list[dict[str, float | str]],
) -> dict[str, Any]:
    bar_by_day = {str(b["day"]): b for b in bars}
    closes: list[float] = []
    close_days: list[str] = []
    for b in bars:
        closes.append(float(b["close"]))
        close_days.append(str(b["day"]))
    close_ix = {d: i for i, d in enumerate(close_days)}

    prior_iv: list[float] = []
    joined: list[dict[str, Any]] = []
    for row in iv_rows:
        day = str(row.get("day") or "")
        iv = row.get("iv")
        if not day or iv is None:
            continue
        iv_f = float(iv)
        bar = bar_by_day.get(day)
        idx = close_ix.get(day)
        rv20 = None
        park = None
        fwd1 = None
        close = None
        if bar is not None:
            close = float(bar["close"])
            park = parkinson_rv_pct(float(bar["high"]), float(bar["low"]))
        if idx is not None:
            rv20 = close_to_close_rv_pct(closes[: idx + 1])
            if idx + 1 < len(closes) and closes[idx] > 0:
                fwd1 = abs_move_ann_pct(closes[idx], closes[idx + 1])
        ivp = compute_ivp(prior_iv, iv_f) if len(prior_iv) >= 5 else None
        prior_iv.append(iv_f)
        rec = {
            "day": day,
            "iv": round(iv_f, 4),
            "ivp": ivp,
            "proxy": bool(row.get("proxy")),
            "close": None if close is None else round(close, 2),
            "rv20": None if rv20 is None else round(rv20, 4),
            "rv_park": None if park is None else round(park, 4),
            "fwd1": None if fwd1 is None else round(fwd1, 4),
        }
        if rv20 is not None:
            rec["spread20"] = round(rv20 - iv_f, 4)
        if park is not None:
            rec["spread_park"] = round(park - iv_f, 4)
        joined.append(rec)

    usable = [r for r in joined if r.get("rv20") is not None]
    spreads = [float(r["spread20"]) for r in usable]
    rv_gt = sum(1 for r in usable if float(r["rv20"]) > float(r["iv"]))
    with_ivp = [r for r in usable if r.get("ivp") is not None]
    buy = [r for r in with_ivp if float(r["ivp"]) < BUY_IVP_LT and float(r["rv20"]) > float(r["iv"])]
    sell = [r for r in with_ivp if float(r["ivp"]) > SELL_IVP_GT and float(r["rv20"]) < float(r["iv"])]
    flat = len(with_ivp) - len(buy) - len(sell)

    ivs = [float(r["iv"]) for r in joined]
    return {
        "ok": True,
        "capital": CAPITAL,
        "short_straddle_margin": [SHORT_STRADDLE_MARGIN_LO, SHORT_STRADDLE_MARGIN_HI],
        "naked_short_fits_2l": CAPITAL >= SHORT_STRADDLE_MARGIN_HI,
        "iv_days": len(joined),
        "joined_rv20": len(usable),
        "proxy_days": sum(1 for r in joined if r.get("proxy")),
        "window": RV_WINDOW,
        "iv_pctiles": {
            "p10": round(_pctile(ivs, 0.10), 2),
            "p25": round(_pctile(ivs, 0.25), 2),
            "p50": round(_pctile(ivs, 0.50), 2),
            "p75": round(_pctile(ivs, 0.75), 2),
            "p90": round(_pctile(ivs, 0.90), 2),
        },
        "spread20": {
            "median": None if _med(spreads) is None else round(_med(spreads), 3),
            "mean": round(sum(spreads) / len(spreads), 3) if spreads else None,
            "rv_gt_iv_pct": round(100.0 * rv_gt / len(usable), 1) if usable else None,
        },
        "regime": {
            "buy_ivp_lt": BUY_IVP_LT,
            "sell_ivp_gt": SELL_IVP_GT,
            "buy": len(buy),
            "sell": len(sell),
            "flat": flat,
            "n": len(with_ivp),
            "buy_pct": round(100.0 * len(buy) / len(with_ivp), 1) if with_ivp else None,
            "sell_pct": round(100.0 * len(sell) / len(with_ivp), 1) if with_ivp else None,
            "flat_pct": round(100.0 * flat / len(with_ivp), 1) if with_ivp else None,
        },
        "buy_days": [r["day"] for r in buy][-12:],
        "sell_days": [r["day"] for r in sell][-12:],
        "rows": joined,
    }


async def run() -> dict[str, Any]:
    history = load_iv_history(DATA_DIR / IVP_HISTORY_FILE)
    iv_rows = list(history.get(NIFTY_SYMBOL) or [])
    if not iv_rows:
        raise RuntimeError(f"No ATM IV samples in {DATA_DIR / IVP_HISTORY_FILE}")
    settings = load_settings()
    csvs = _load_csvs()
    token = lookup_token(csvs, NIFTY_SYMBOL)
    if not token:
        raise RuntimeError(f"No instrument token for {NIFTY_SYMBOL}")
    rest = KiteRest(settings.api_key, settings.access_token)
    try:
        bars = await fetch_nifty_daily(rest, token)
    finally:
        await rest.close()
    report = join_study(iv_rows, bars)
    report["spot_last"] = bars[-1]["close"] if bars else None
    report["bar_days"] = len(bars)
    report["from"] = iv_rows[0].get("day")
    report["to"] = iv_rows[-1].get("day")
    report["short_pnl"] = short_straddle_pnl(report["rows"])
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def _print(report: dict[str, Any]) -> None:
    sp = report["spread20"]
    rg = report["regime"]
    print(
        f"IV days={report['iv_days']}  joined 20d RV={report['joined_rv20']}  "
        f"{report.get('from')} → {report.get('to')}"
    )
    print(
        f"IV p10/25/50/75/90 = {report['iv_pctiles']['p10']}/"
        f"{report['iv_pctiles']['p25']}/{report['iv_pctiles']['p50']}/"
        f"{report['iv_pctiles']['p75']}/{report['iv_pctiles']['p90']}"
    )
    print(
        f"RV20 − IV  median={sp['median']}  mean={sp['mean']}  "
        f"RV>IV {sp['rv_gt_iv_pct']}% of days"
    )
    print(
        f"Regime (IVP<{BUY_IVP_LT} & RV>IV buy / IVP>{SELL_IVP_GT} & RV<IV sell): "
        f"buy {rg['buy']} ({rg['buy_pct']}%)  "
        f"sell {rg['sell']} ({rg['sell_pct']}%)  "
        f"flat {rg['flat']} ({rg['flat_pct']}%)"
    )
    print(
        f"Capital ₹{CAPITAL:,.0f}  naked short margin "
        f"₹{SHORT_STRADDLE_MARGIN_LO/1000:.0f}–{SHORT_STRADDLE_MARGIN_HI/1000:.0f}k  "
        f"fits={report['naked_short_fits_2l']}"
    )
    spnl = report.get("short_pnl") or {}
    ev = spnl.get("every_day_naked") or {}
    if ev.get("n"):
        print(
            f"Short 1-lot overnight  n={ev['n']}  mean={ev['mean']}  "
            f"median={ev['median']}  min={ev['min']}  "
            f"loss>40k={ev['loss_gt_40k']}  loss>70k={ev['loss_gt_70k']}"
        )
        print("Worst 5:", ", ".join(
            f"{w['day']} {w['pnl']}" for w in (spnl.get("worst5_naked") or [])
        ))
    print(f"Wrote {OUT_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Join ATM IV history to NIFTY daily RV")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--from-json",
        action="store_true",
        help="Recompute short P&L from existing rv_iv_study.json (no Kite)",
    )
    args = parser.parse_args()
    if args.from_json:
        report = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        report["short_pnl"] = short_straddle_pnl(report.get("rows") or [])
        OUT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    else:
        report = asyncio.run(run())
    if args.json:
        slim = {k: v for k, v in report.items() if k != "rows"}
        print(json.dumps(slim, indent=2))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
