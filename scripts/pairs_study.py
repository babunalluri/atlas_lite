"""Usage: python3 scripts/pairs_study.py [--fetch] [--capital 3000000]

Pairs trading (statistical arbitrage) study on NSE F&O stocks, daily closes, walk-forward.

Rule: Gatev, Goetzmann & Rouwenhorst (2006), as published, untuned. Every 6 months: over the past
12 months (formation), normalise each stock's price to 1 at the start and pick the TOP_PAIRS pairs
with the smallest sum of squared differences. Trade them over the next 6 months (trading): open when
the normalised spread is more than 2 formation standard deviations wide (short the rich leg, long
the cheap one, equal rupees), close when the prices cross, or at the end of the period. A signal on a
day's close is executed at the next day's close (one-day wait, as in the paper: avoids trading on
the close that made the signal).

Execution: both legs as stock futures (shortable overnight), equal notional per leg, LEG_NOTIONAL
each. Costs per leg per order: Zerodha futures charges (brokerage, STT 0.02% on sells, exchange,
SEBI, stamp, GST) plus SLIPPAGE of price; a position open across a monthly expiry pays a roll
(close and reopen both legs). Margin: MARGIN_PCT of each leg's notional, both legs.

Caveats, printed with the results: the universe is today's F&O list (survivorship bias: stocks
dropped from F&O since 2016 are missing, which flatters the result); daily closes, not executable
futures prices; lot sizes ignored (equal rupee legs).

--fetch downloads daily candles from Kite (REST, not the websocket) into data/kite_history/stock_daily.json.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import statistics as st
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DAILY = ROOT / "data" / "kite_history" / "stock_daily.json"
START = date(2016, 1, 1)
FORMATION_DAYS, TRADING_DAYS = 250, 125  # 12 and 6 months of sessions
TOP_PAIRS = 20
OPEN_SD = 2.0
LEG_NOTIONAL = 500_000.0
MARGIN_PCT = 0.20
SLIPPAGE = 0.0005  # 0.05% of price per order, per leg


def fo_stocks() -> dict[str, int]:
    """NSE cash token for every stock with NSE futures today."""
    names = {r["name"] for r in csv.DictReader((ROOT / "data" / "nfo_instruments.csv").open(encoding="utf-8"))
             if r["instrument_type"] == "FUT" and r["segment"] == "NFO-FUT"}
    out = {}
    for r in csv.DictReader((ROOT / "data" / "nse_instruments.csv").open(encoding="utf-8")):
        if r["exchange"] == "NSE" and r["instrument_type"] == "EQ" and r["tradingsymbol"] in names:
            out[r["tradingsymbol"]] = int(r["instrument_token"])
    return out


async def fetch() -> None:
    from atlas_lite.config import load_settings
    from atlas_lite.kite_rest import KiteRest

    s = load_settings()
    k = KiteRest(s.api_key, s.access_token)
    out: dict[str, dict[str, float]] = json.loads(DAILY.read_text()) if DAILY.is_file() else {}
    stocks = fo_stocks()
    today = date.today()
    for i, (sym, tok) in enumerate(sorted(stocks.items())):
        if sym in out and max(out[sym]) >= (today - timedelta(days=4)).isoformat():
            continue
        closes: dict[str, float] = {}
        frm = START
        while frm <= today:  # Kite: at most 2000 days of daily candles per request
            to = min(today, frm + timedelta(days=1900))
            for attempt in range(3):
                try:
                    rows = await k.historical(tok, "day", from_date=frm.isoformat(), to_date=to.isoformat())
                    break
                except Exception as exc:  # noqa: BLE001 - rate limit or network: wait and retry
                    if attempt == 2:
                        raise
                    print(f"{sym}: {exc!r}; retrying", flush=True)
                    await asyncio.sleep(2)
            for r in rows:
                closes[str(r[0])[:10]] = float(r[4])
            frm = to + timedelta(days=1)
            await asyncio.sleep(0.4)  # Kite historical: 3 requests a second
        out[sym] = closes
        if i % 20 == 0:
            print(f"{i + 1}/{len(stocks)} {sym}: {len(closes)} days", flush=True)
            DAILY.write_text(json.dumps(out))
    DAILY.write_text(json.dumps(out))
    print(f"saved {len(out)} stocks to {DAILY}", flush=True)


# ---- data preparation ----
SPLIT_RATIOS = (1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10, 2 / 3, 1 / 1.5, 1 / 6, 1 / 8, 1 / 20)


def adjust_splits(closes: list[float]) -> tuple[list[float], int]:
    """Undo unadjusted splits and bonuses: an overnight fall within 2% of a common ratio, more than 25%,
    scales all earlier prices by that ratio. Returns (adjusted, number of adjustments)."""
    out = list(closes)
    n = 0
    for i in range(len(out) - 1, 0, -1):
        r = out[i] / out[i - 1] if out[i - 1] else 1.0
        if r < 0.75:
            k = min(SPLIT_RATIOS, key=lambda x: abs(r / x - 1))
            if abs(r / k - 1) < 0.02:
                for j in range(i):
                    out[j] *= k
                n += 1
    return out, n


def load_panel() -> tuple[list[str], dict[str, list[float | None]], int]:
    """(dates, stock -> close on each date or None, splits adjusted)."""
    raw = json.loads(DAILY.read_text())
    seen: dict[str, int] = {}
    for v in raw.values():
        for d in v:
            seen[d] = seen.get(d, 0) + 1
    dates = sorted(d for d, n in seen.items() if n >= 20)  # drops Muhurat sessions only a few stocks traded
    panel, adjusted = {}, 0
    for sym, closes in raw.items():
        have = [closes.get(d) for d in dates]
        idx = [i for i, v in enumerate(have) if v]
        if not idx:
            continue
        vals, n = adjust_splits([have[i] for i in idx])
        adjusted += n
        col: list[float | None] = [None] * len(dates)
        for i, v in zip(idx, vals):
            col[i] = v
        gap = 0
        for i in range(idx[0] + 1, idx[-1] + 1):  # a missing day or three inside the history: last price
            if col[i] is None and gap < 3:
                col[i], gap = col[i - 1], gap + 1
            elif col[i] is not None:
                gap = 0
        panel[sym] = col
    return dates, panel, adjusted


# ---- costs ----
def order_cost(_px: float, notional: float, side: str) -> float:
    """One futures order on one leg: Zerodha charges + slippage, rupees."""
    brokerage = min(20.0, 0.0003 * notional)
    exch, sebi = 0.0000173 * notional, 0.000001 * notional
    stt = 0.0002 * notional if side == "sell" else 0.0
    stamp = 0.00002 * notional if side == "buy" else 0.0
    gst = 0.18 * (brokerage + exch + sebi)
    return brokerage + exch + sebi + stt + stamp + gst + SLIPPAGE * notional


def close_trade(pos: dict, sx: float, lx: float, d: str) -> dict:
    """Round trip of both legs at the given exit prices, with a roll (both legs closed and reopened)
    for every monthly expiry crossed."""
    gross = LEG_NOTIONAL * (pos["sp"] - sx) / pos["sp"] + LEG_NOTIONAL * (lx - pos["lp"]) / pos["lp"]
    rolls = month_ends_between(pos["d0"], d)
    one_leg_trip = order_cost(0.0, LEG_NOTIONAL, "buy") + order_cost(0.0, LEG_NOTIONAL, "sell")
    cost = 2 * one_leg_trip * (1 + rolls)
    return {"pair": f"{pos['short']}/{pos['long']}", "open": pos["d0"], "close": d, "gross": gross,
            "cost": cost, "net": gross - cost, "rolls": rolls}


def month_ends_between(d0: str, d1: str) -> int:
    """Monthly expiries crossed while open (one per calendar month boundary)."""
    y0, m0 = int(d0[:4]), int(d0[5:7])
    y1, m1 = int(d1[:4]), int(d1[5:7])
    return (y1 - y0) * 12 + (m1 - m0)


# ---- the walk-forward backtest ----
def run(capital: float) -> dict:
    dates, panel, adjusted = load_panel()
    max_open = int(capital * 0.8 // (2 * LEG_NOTIONAL * MARGIN_PCT))  # 20% of capital kept for MTM swings
    periods, trades = [], []
    equity_days: dict[str, float] = {}
    realized_total = 0.0
    start = 0
    while start + FORMATION_DAYS + 20 < len(dates):
        f0, f1 = start, start + FORMATION_DAYS  # formation [f0, f1), trading [f1, t1)
        t1 = min(f1 + TRADING_DAYS, len(dates))
        syms = [s for s, col in panel.items()
                if all(col[i] for i in range(f0, f1)) and col[f1]]  # full formation history, listed at the start
        norm = {s: [panel[s][i] / panel[s][f0] for i in range(f0, f1)] for s in syms}
        cands = []
        for a_i in range(len(syms)):
            na = norm[syms[a_i]]
            for b_i in range(a_i + 1, len(syms)):
                nb = norm[syms[b_i]]
                ssd = sum((x - y) ** 2 for x, y in zip(na, nb))
                cands.append((ssd, syms[a_i], syms[b_i]))
        cands.sort()
        pairs, used = [], set()
        for ssd, a, b in cands:  # GGR allow a stock in several pairs; keep that
            sd = st.pstdev([x - y for x, y in zip(norm[a], norm[b])])
            pairs.append((a, b, sd))
            if len(pairs) >= TOP_PAIRS:
                break
        # trading period
        def px(s, i):
            j = i
            while panel[s][j] is None and j > f1:  # suspended: last price
                j -= 1
            return panel[s][j]
        base = {s: px(s, f1) for p in pairs for s in p[:2]}
        open_pos: dict[int, dict] = {}
        pending: dict[int, str] = {}  # pair index -> "open" / "close", executed at the next close
        period_net, period_trades = 0.0, 0
        for i in range(f1, t1):
            d = dates[i]
            # 1) execute yesterday's signals at today's close
            for k, what in list(pending.items()):
                a, b, sd = pairs[k]
                pa, pb = px(a, i), px(b, i)
                if what == "open" and k not in open_pos and len(open_pos) < max_open:
                    spread = pa / base[a] - pb / base[b]
                    if abs(spread) > OPEN_SD * sd:  # still open at execution
                        short, long_ = (a, b) if spread > 0 else (b, a)
                        open_pos[k] = {"short": short, "long": long_, "sp": px(short, i), "lp": px(long_, i),
                                       "d0": d, "sign": 1 if spread > 0 else -1}
                elif what == "close" and k in open_pos:
                    pos = open_pos.pop(k)
                    t = close_trade(pos, px(pos["short"], i), px(pos["long"], i), d)
                    trades.append(t)
                    period_net += t["net"]
                    period_trades += 1
                pending.pop(k)
            # 2) signals on today's close
            last = i == t1 - 1
            for k, (a, b, sd) in enumerate(pairs):
                spread = px(a, i) / base[a] - px(b, i) / base[b]
                if k in open_pos:
                    if last or spread * open_pos[k]["sign"] <= 0:  # prices crossed, or the period ends
                        pending[k] = "close"
                elif not last and abs(spread) > OPEN_SD * sd:
                    pending[k] = "open"
            # 3) mark to market
            mtm = sum(LEG_NOTIONAL * (p["sp"] - px(p["short"], i)) / p["sp"]
                      + LEG_NOTIONAL * (px(p["long"], i) - p["lp"]) / p["lp"] for p in open_pos.values())
            equity_days[d] = realized_total + period_net + mtm
        # positions still open at the period end close on the last day (signalled above, executed here)
        for k, pos in list(open_pos.items()):
            i = t1 - 1
            t = close_trade(pos, px(pos["short"], i), px(pos["long"], i), dates[i])
            trades.append(t)
            period_net += t["net"]
            period_trades += 1
        realized_total += period_net
        periods.append({"from": dates[f1], "to": dates[t1 - 1], "stocks": len(syms), "trades": period_trades,
                        "net": period_net})
        start += TRADING_DAYS
    # drawdown on daily equity
    peak, mdd = 0.0, 0.0
    for d in sorted(equity_days):
        peak = max(peak, equity_days[d])
        mdd = min(mdd, equity_days[d] - peak)
    return {"periods": periods, "trades": trades, "max_drawdown": mdd, "adjusted_splits": adjusted,
            "max_open_pairs": max_open, "first": periods[0]["from"] if periods else None,
            "last": periods[-1]["to"] if periods else None}


def report(res: dict, capital: float) -> None:
    trades, periods = res["trades"], res["periods"]
    total = sum(t["net"] for t in trades)
    years = (date.fromisoformat(res["last"]) - date.fromisoformat(res["first"])).days / 365.25
    print(f"Pairs (GGR distance, top {TOP_PAIRS}, open {OPEN_SD} sd, close on cross), ₹{LEG_NOTIONAL:,.0f} a leg, "
          f"capital ₹{capital:,.0f} (max {res['max_open_pairs']} pairs open)")
    print(f"test span {res['first']} .. {res['last']} ({years:.1f} years); split/bonus jumps adjusted: {res['adjusted_splits']}")
    print(f"{'period':25} {'stocks':>6} {'trades':>6} {'net ₹':>11}")
    for p in periods:
        print(f"{p['from']} .. {p['to']} {p['stocks']:6} {p['trades']:6} {p['net']:11,.0f}")
    wins = [t for t in trades if t["net"] > 0]
    gross = sum(t["gross"] for t in trades)
    cost = sum(t["cost"] for t in trades)
    print(f"trades {len(trades)}, win rate {len(wins) / max(len(trades), 1):.0%}, gross ₹{gross:,.0f}, "
          f"costs ₹{cost:,.0f}, net ₹{total:,.0f}")
    print(f"net per year ₹{total / years:,.0f} = {total / years / capital:.1%} of capital; "
          f"periods positive {sum(p['net'] > 0 for p in periods)}/{len(periods)}; "
          f"max drawdown ₹{res['max_drawdown']:,.0f}")
    for lo, hi in (("2016", "2021-12-31"), ("2022", "2099")):
        ps = [p for p in periods if lo <= p["from"] <= hi]
        n = sum(p["net"] for p in ps)
        print(f"  periods starting {lo}..{hi[:4]}: {len(ps)}, net ₹{n:,.0f}, positive {sum(p['net'] > 0 for p in ps)}")
    print("caveats: today's F&O list (survivorship bias flatters this); daily closes, not futures fills; "
          "lot sizes ignored")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--capital", type=float, default=3_000_000.0)
    args = ap.parse_args()
    if args.fetch:
        asyncio.run(fetch())
    else:
        report(run(args.capital), args.capital)
