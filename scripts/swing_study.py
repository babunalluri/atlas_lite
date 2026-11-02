"""Usage: python3 scripts/swing_study.py [--capital 3000000]

Swing trading study on NSE F&O stocks, daily closes, long only, delivery (CNC). Three published
rules, fixed before looking at results and not tuned; every one is reported:

- momentum: Jegadeesh & Titman (1993) 12-1. Each month, hold the TOP_N stocks with the best return
  over the past 12 months excluding the latest month, equal rupees.
- high52: George & Hwang (2004). Each month, hold the TOP_N stocks whose close is nearest their
  52-week high.
- breakout: Donchian channel (the turtles' system 2 entry, system 1 exit). Buy when the close beats
  the prior 55-day high, sell when it falls below the prior 20-day low; at most TOP_N positions,
  each 1/TOP_N of equity at entry; the widest breakouts first when slots are short.

Signals on a day's close execute at the next day's close. Costs: Zerodha delivery (no brokerage,
STT 0.1% each side, exchange, SEBI, stamp 0.015% on buys, GST, DP charge on sells) plus SLIPPAGE
per order. Benchmarks: NIFTY 50 (price index) and an equal-weight portfolio of the same stocks,
rebalanced monthly with the same costs: it carries the same survivorship bias, so beating it is
the real test. Price returns only (no dividends) for every line, so they compare like for like.

Caveats printed with the results: today's F&O list (survivorship bias flatters every line, the
equal-weight benchmark included); daily closes; tax not included (short-term gains 20%).

Data: data/kite_history/stock_daily.json (scripts/pairs_study.py --fetch) and nifty_daily.json.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from pairs_study import load_panel  # noqa: E402

TOP_N = 20
SLIPPAGE = 0.0005
LOOKBACK = 252  # 12 months of sessions
SKIP = 21  # the latest month, left out of the momentum score
BREAK_IN, BREAK_OUT = 55, 20


def buy_cost(value: float) -> float:
    exch = 0.0000297 * value
    return 0.001 * value + exch + 0.000001 * value + 0.00015 * value + 0.18 * exch + SLIPPAGE * value


def sell_cost(value: float) -> float:
    exch = 0.0000297 * value
    return 0.001 * value + exch + 0.000001 * value + 0.18 * exch + 15.34 + SLIPPAGE * value


class Book:
    """Cash plus share holdings, valued at each day's close."""

    def __init__(self, capital: float) -> None:
        self.cash = capital
        self.shares: dict[str, float] = {}
        self.costs = 0.0
        self.trades = 0

    def value(self, px: dict[str, float]) -> float:
        return self.cash + sum(n * px[s] for s, n in self.shares.items())

    def sell(self, s: str, price: float) -> None:
        v = self.shares.pop(s) * price
        c = sell_cost(v)
        self.cash += v - c
        self.costs += c
        self.trades += 1

    def buy(self, s: str, price: float, budget: float) -> None:
        budget = min(budget, self.cash)
        if budget < 10_000:
            return
        v = budget / (1 + buy_cost(1.0))
        c = buy_cost(v)
        self.cash -= v + c
        self.shares[s] = self.shares.get(s, 0.0) + v / price
        self.costs += c
        self.trades += 1


def simulate(rule: str, dates: list[str], panel: dict[str, list], capital: float) -> tuple[list[float], Book]:
    book = Book(capital)
    start = LOOKBACK + 1
    curve: list[float] = []
    pending: tuple[str, object] | None = None
    syms = list(panel)

    def px_at(i: int) -> dict[str, float]:
        out = {}
        for s in syms:
            v = panel[s][i]
            if v is not None:
                out[s] = v
        return out

    last_px: dict[str, float] = {}
    for i in range(start, len(dates)):
        px = px_at(i)
        last_px.update(px)
        # 1) execute yesterday's decision at today's close
        if pending is not None:
            kind, arg = pending
            if kind == "rebalance":
                target = set(arg)
                for s in [s for s in book.shares if s not in target and s in px]:
                    book.sell(s, px[s])
                new = [s for s in arg if s not in book.shares and s in px]
                if new:
                    each = book.value(last_px) / max(TOP_N if rule != "equal" else len(arg), 1)
                    for s in new:
                        book.buy(s, px[s], each)
            elif kind == "trade":
                exits, entries = arg
                for s in exits:
                    if s in book.shares and s in px:
                        book.sell(s, px[s])
                for s in entries:
                    if len(book.shares) >= TOP_N:
                        break
                    if s not in book.shares and s in px:
                        book.buy(s, px[s], book.value(last_px) / TOP_N)
            pending = None
        curve.append(book.value(last_px))
        # 2) today's signal
        month_end = i + 1 < len(dates) and dates[i + 1][:7] != dates[i][:7]
        if rule in ("momentum", "high52", "equal") and month_end:
            elig = [s for s in syms if panel[s][i] and panel[s][i - LOOKBACK]]
            if rule == "equal":
                pending = ("rebalance", elig)
            elif rule == "momentum":
                score = {s: panel[s][i - SKIP] / panel[s][i - LOOKBACK] for s in elig if panel[s][i - SKIP]}
                pending = ("rebalance", sorted(score, key=score.get, reverse=True)[:TOP_N])
            else:
                score = {}
                for s in elig:
                    hist = [v for v in panel[s][i - LOOKBACK + 1:i + 1] if v]
                    score[s] = panel[s][i] / max(hist)
                pending = ("rebalance", sorted(score, key=score.get, reverse=True)[:TOP_N])
        elif rule == "breakout":
            exits, cands = [], []
            for s in syms:
                p = panel[s][i]
                if not p:
                    continue
                if s in book.shares:
                    lows = [v for v in panel[s][i - BREAK_OUT:i] if v]
                    if lows and p < min(lows):
                        exits.append(s)
                else:
                    highs = [v for v in panel[s][i - BREAK_IN:i] if v]
                    if len(highs) == BREAK_IN and p > max(highs):
                        cands.append((p / max(highs), s))
            cands.sort(reverse=True)
            if exits or cands:
                pending = ("trade", (exits, [s for _, s in cands]))
    return curve, book


def stats(curve: list[float], dates: list[str], capital: float) -> dict:
    years = len(curve) / 252
    cagr = (curve[-1] / capital) ** (1 / years) - 1
    rets = [curve[k] / curve[k - 1] - 1 for k in range(1, len(curve))]
    sharpe = st.mean(rets) / st.pstdev(rets) * math.sqrt(252) if st.pstdev(rets) else 0.0
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    by_year: dict[str, tuple[float, float]] = {}
    for d, v in zip(dates, curve):
        y = d[:4]
        first = by_year.get(y, (v, v))[0]
        by_year[y] = (first, v)
    yearly, prev_end = {}, capital
    for y in sorted(by_year):
        yearly[y] = by_year[y][1] / prev_end - 1
        prev_end = by_year[y][1]
    return {"cagr": cagr, "sharpe": sharpe, "mdd": mdd, "end": curve[-1], "yearly": yearly}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=3_000_000.0)
    args = ap.parse_args()
    dates, panel, _ = load_panel()
    nifty = json.loads((ROOT / "data" / "kite_history" / "nifty_daily.json").read_text())
    test_dates = dates[LOOKBACK + 1:]
    results = {}
    for rule in ("momentum", "high52", "breakout", "equal"):
        curve, book = simulate(rule, dates, panel, args.capital)
        results[rule] = (stats(curve, test_dates, args.capital), book)
    nc, last = [], None
    for d in test_dates:
        last = nifty.get(d, last)
        nc.append(last)
    results["nifty"] = (stats([args.capital * v / nc[0] for v in nc], test_dates, args.capital), None)
    # Real, survivorship-free indices: what the public versions of these ideas actually returned.
    for name, series in json.loads((ROOT / "data" / "kite_history" / "index_daily.json").read_text()).items():
        ic, last = [], None
        for d in test_dates:
            last = series.get(d, last)
            ic.append(last)
        if ic[0]:
            results[name] = (stats([args.capital * v / ic[0] for v in ic], test_dates, args.capital), None)
    names = {"momentum": "12-1 momentum, top 20", "high52": "52-week high, top 20", "breakout": "55/20 breakout",
             "equal": "equal weight, all stocks", "nifty": "NIFTY 50",
             "NIFTY200MOMENTM30": "NSE Nifty200 Momentum 30", "NIFTY 500": "NIFTY 500"}
    print(f"₹{args.capital:,.0f}, delivery, long only, {test_dates[0]} .. {test_dates[-1]} "
          f"({len(test_dates) / 252:.1f} years), price returns, before tax")
    print(f"{'rule':26} {'CAGR':>6} {'Sharpe':>6} {'max DD':>7} {'end ₹':>12} {'trades':>6} {'costs ₹':>10}")
    for k, (s, book) in results.items():
        print(f"{names[k]:26} {s['cagr']:6.1%} {s['sharpe']:6.2f} {s['mdd']:7.1%} {s['end']:12,.0f} "
              f"{book.trades if book else '-':>6} {(f'{book.costs:,.0f}' if book else '-'):>10}")
    years = sorted(results["nifty"][0]["yearly"])
    print("yearly: " + " ".join(f"{y:>6}" for y in years))
    for k, (s, _) in results.items():
        print(f"{k[:8]:8}" + " ".join(f"{s['yearly'].get(y, float('nan')):6.0%}" for y in years))
    print("caveats: today's F&O list (survivorship bias lifts every line, the equal-weight one too); "
          "no dividends; no tax (short-term gains 20%)")


if __name__ == "__main__":
    main()
