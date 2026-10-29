"""Usage: python3 scripts/bt_iron_condor_eod.py [--slip 0.5] [--variants]

Weekly NIFTY iron condor on real NSE end-of-day prices (data/nifty_opt_eod.pkl,
from scripts/nse_nifty_options_history.py). Rules as published by Quantsapp:

- Entry at the close, three sessions before the weekly expiry.
- Forward F from put-call parity at the strike where |call - put| is smallest;
  ATM IV from Black-76 at that strike.
- Short strikes: F * exp(+-1.5 * IV * sqrt(3/252)); put rounded down, call up.
- Wings: 2% of F beyond each short strike (put down, call up).
- Stop: at each close before expiry day, exit if the structure's closing price is
  >= 3x the entry credit (both at closing prices; slippage only on the fills).
  No stop on expiry day.
- Otherwise settle at intrinsic value against NIFTY's final settlement level. On
  expiry day NSE's bhavcopy puts that index level in every option's settlement
  field (2019 files put 0 there; then the level comes from put-call parity).

Every leg must have traded (contracts > 0) on the entry day; a missing wing moves
outward to the nearest strike that traded. Fills at close +- slip per leg, plus
Kite charges (current rates). P&L is per 65 units (today's lot) for every year.
"""

from __future__ import annotations

import argparse
import math
import pickle
import statistics as st
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402
from atlas_lite.metrics import atm_iv_from_prices  # noqa: E402
from atlas_lite.paper_weekly_sell import iv_decimal, select_strikes  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
QTY = 65
CAPITAL = 150_000.0  # per lot, from the research brief


def load() -> tuple[list[str], dict]:
    store = pickle.loads((ROOT / "data" / "nifty_opt_eod.pkl").read_bytes())
    chains: dict[str, dict[str, dict[tuple[int, str], tuple]]] = {}
    for day, rows in store["days"].items():
        by_exp: dict[str, dict[tuple[int, str], tuple]] = {}
        for exp, k, opt, close, settle, contracts, und in rows:
            by_exp.setdefault(exp, {})[(k, opt)] = (close, settle, contracts)
        chains[day] = by_exp
    return sorted(chains), chains


def forward_iv(chain: dict, day: str, exp: str) -> tuple[float, float, int] | None:
    pairs = []
    for (k, opt), (c, _s, n) in chain.items():
        if opt != "CE" or not c or n <= 0:
            continue
        p = chain.get((k, "PE"))
        if p and p[0] and p[2] > 0:
            pairs.append((abs(c - p[0]), k, c, p[0]))
    if not pairs:
        return None
    _, k, c, p = min(pairs)
    F = k + c - p
    now = datetime.fromisoformat(f"{day}T15:25:00").replace(tzinfo=IST)
    iv = atm_iv_from_prices(c, p, F, float(k), date.fromisoformat(exp), now=now)
    if not iv:
        return None
    return F, iv_decimal(iv), k


def settlement_level(chain: dict, day: str, exp: str) -> float | None:
    """NIFTY's final settlement on expiry day: the index level NSE writes into the settle field."""
    levels = [row[1] for row in chain.values() if row[1] and row[1] > 1000]
    if levels:
        return max(set(levels), key=levels.count)
    fi = forward_iv(chain, day, exp)
    return fi[0] if fi else None


def daily_forwards(days: list[str], chains: dict) -> dict[str, float]:
    """Forward from the nearest expiry that is still live after the close, per day."""
    out = {}
    for d in days:
        live = [e for e in chains[d] if e > d]
        if not live:
            continue
        e = min(live)
        fi = forward_iv(chains[d][e], d, e)
        if fi:
            out[d] = fi[0]
    return out


def traded(chain: dict, k: int, opt: str) -> bool:
    row = chain.get((k, opt))
    return bool(row and row[0] and row[0] > 0 and row[2] > 0)


def pick(chain: dict, target: float, opt: str, outward: bool) -> int | None:
    """Nearest traded strike at/below (puts) or at/above (calls) the target, moving outward."""
    strikes = sorted({k for (k, o) in chain if o == opt and traded(chain, k, opt)})
    if opt == "PE":
        cands = [k for k in strikes if k <= target]
        return max(cands) if cands else None
    cands = [k for k in strikes if k >= target]
    return min(cands) if cands else None


def run(days: list[str], chains: dict, *, slip: float, sd: float = 1.5, sessions: int = 3,
        wing_pct: float = 0.02, stop_mult: float | None = 3.0, rv_filter: bool = False,
        structure: str = "condor", rank: int = 0) -> list[dict]:
    """structure: condor | put_spread | call_spread | strangle (no wings).
    rank: 0 sells the nearest live expiry, 1 the one after it (a two-week trade)."""
    idx = {d: i for i, d in enumerate(days)}
    expiries = sorted({e for d in days for e in chains[d]})
    fwd_hist = daily_forwards(days, chains) if rv_filter else {}
    trades = []
    for exp in expiries:
        if exp > days[-1]:  # week not finished in the data
            continue
        settle_day = max((d for d in days if d <= exp), default=None)
        if settle_day is None or settle_day not in idx or idx[settle_day] - sessions < 0:
            continue
        entry = days[idx[settle_day] - sessions]
        chain = chains[entry].get(exp)
        # Must be the nearest expiry still live after the entry close (the weekly being sold).
        live = sorted(e for e in chains[entry] if e > entry)
        if not chain or len(live) <= rank or live[rank] != exp:
            continue
        fi = forward_iv(chain, entry, exp)
        if not fi:
            continue
        F, iv, _k = fi
        if rv_filter:
            prior = [fwd_hist[d] for d in days[max(0, idx[entry] - 11): idx[entry] + 1] if d in fwd_hist]
            if len(prior) >= 6:
                rets = [math.log(b / a) for a, b in zip(prior, prior[1:])]
                rv = st.pstdev(rets) * math.sqrt(252)
                if iv <= rv:
                    continue
        # Same selection as the live book (atlas_lite/paper_weekly_sell.py).
        put_k = sorted(k for (k, o) in chain if o == "PE" and traded(chain, k, "PE"))
        call_k = sorted(k for (k, o) in chain if o == "CE" and traded(chain, k, "CE"))
        picked = select_strikes(F, iv, put_k, call_k, sd=sd, sessions=sessions, wing_pct=wing_pct)
        if picked is None:
            continue
        sp, sc, lp, lc = picked
        legs = {
            "condor": [(sp, "PE", -1), (sc, "CE", -1), (lp, "PE", +1), (lc, "CE", +1)],
            "put_spread": [(sp, "PE", -1), (lp, "PE", +1)],
            "call_spread": [(sc, "CE", -1), (lc, "CE", +1)],
            "strangle": [(sp, "PE", -1), (sc, "CE", -1)],
        }[structure]
        n_legs = len(legs)
        px_in = {(k, o): chain[(k, o)][0] for k, o, _ in legs}
        credit_raw = sum(-s * px_in[(k, o)] for k, o, s in legs)
        credit = credit_raw - n_legs * slip  # what the fills actually collect
        if credit <= 0:
            continue
        fills = [(px_in[(k, o)] - slip, QTY, "sell") if s < 0 else (px_in[(k, o)] + slip, QTY, "buy") for k, o, s in legs]
        exit_px, why, exit_day = None, "expiry", settle_day
        for d in days[idx[entry] + 1: idx[settle_day]]:
            ch = chains[d].get(exp) or {}
            if stop_mult is None or any(not (ch.get((k, o)) or (None,))[0] for k, o, _ in legs):
                continue
            # Trigger on closing prices, as published; slippage applies to the fills only.
            cost_raw = sum(-s * ch[(k, o)][0] for k, o, s in legs)
            if cost_raw >= stop_mult * credit_raw:
                exit_px = {(k, o): ch[(k, o)][0] for k, o, _ in legs}
                why, exit_day = "stop", d
                break
        if exit_px is None:
            ch = chains[settle_day].get(exp) or {}
            level = settlement_level(ch, settle_day, exp) if ch else None
            if level is None:
                continue
            exit_px = {(k, o): max(0.0, (level - k) if o == "CE" else (k - level)) for k, o, _ in legs}
        close_slip = slip if why == "stop" else 0.0
        for k, o, s in legs:
            x = exit_px[(k, o)]
            if x <= 0:
                continue
            fills.append((x + close_slip, QTY, "buy") if s < 0 else (max(0.0, x - close_slip), QTY, "sell"))
        debit = sum(-s * exit_px[(k, o)] for k, o, s in legs) + (n_legs * close_slip)
        net = (credit - debit) * QTY - kite_nfo_charges(fills)["total"]
        width = {"condor": max(sp - lp, lc - sc), "put_spread": sp - lp, "call_spread": lc - sc}.get(structure)
        trades.append({"entry": entry, "exp": exp, "exit": exit_day, "why": why, "net": net, "credit": credit,
                       "F": F, "iv": iv, "strikes": (lp, sp, sc, lc),
                       "max_loss": (width - credit) * QTY if width is not None else float("nan")})
    return trades


def stats(tr: list[dict]) -> str:
    if not tr:
        return "no trades"
    v = [t["net"] for t in tr]
    tot = sum(v)
    wins = [x for x in v if x > 0]
    losses = [x for x in v if x <= 0]
    gl = -sum(losses)
    eq = pk = dd = 0.0
    for x in v:
        eq += x
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return (f"n {len(v):3d}  tot ₹{tot:9.0f}  ₹/wk {tot / len(v):5.0f}  win {len(wins) / len(v):4.0%}  "
            f"avg win ₹{st.mean(wins) if wins else 0:5.0f}  avg loss ₹{st.mean(losses) if losses else 0:6.0f}  "
            f"PF {sum(wins) / gl if gl else 0:4.2f}  worst ₹{min(v):7.0f}  maxDD ₹{dd:8.0f}")


def search(days: list[str], chains: dict, slip: float) -> None:
    """Pick on entries before 2024 only; the 2024-2026 holdout is scored once, after."""
    cut = "2024-01-01"
    grid = [(sd, sess, stop) for sd in (1.0, 1.25, 1.5, 1.75, 2.0) for sess in (2, 3, 4) for stop in (2.0, 3.0, None)]
    rows = []
    for sd, sess, stop in grid:
        tr = run(days, chains, slip=slip, sd=sd, sessions=sess, stop_mult=stop)
        fit = [t for t in tr if t["entry"] < cut]
        test = [t for t in tr if t["entry"] >= cut]
        rows.append(((sd, sess, stop), fit, test))
    tot = lambda tr: sum(t["net"] for t in tr)  # noqa: E731
    print(f"{len(grid)} combos · slip {slip} · pick on entries before {cut}, holdout {cut}..{days[-1]}\n")
    print("ranked by 2019-2023 profit (the only thing used to choose):")
    for (sd, sess, stop), fit, test in sorted(rows, key=lambda r: -tot(r[1]))[:12]:
        lab = f"{sd:.2f} SD, entry {sess} sessions before, stop {'none' if stop is None else f'{stop:g}x'}"
        print(f"  {lab:44s} fit ₹{tot(fit):8.0f} (PF {pf(fit):4.2f})  ->  HOLDOUT ₹{tot(test):8.0f} (PF {pf(test):4.2f}, n {len(test)})")
    pos_both = sum(1 for _, f, t in rows if tot(f) > 0 and tot(t) > 0)
    print(f"\npositive in both periods: {pos_both}/{len(rows)}")
    (sd, sess, stop), fit, test = max(rows, key=lambda r: tot(r[1]))
    print(f"\nchosen on 2019-2023: {sd} SD, {sess} sessions, stop {stop}")
    print(f"  2019-2023  {stats(fit)}")
    print(f"  HOLDOUT    {stats(test)}")


def pf(tr: list[dict]) -> float:
    gw = sum(t["net"] for t in tr if t["net"] > 0)
    gl = -sum(t["net"] for t in tr if t["net"] <= 0)
    return gw / gl if gl else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slip", type=float, default=0.5, help="points per leg per side")
    ap.add_argument("--variants", action="store_true", help="also print a few sensitivity variants")
    ap.add_argument("--structures", action="store_true", help="compare structures at the published settings")
    ap.add_argument("--search", action="store_true",
                    help="45-combo grid: pick on 2019-2023, judge on the 2024-2026 holdout")
    args = ap.parse_args()
    days, chains = load()
    if args.search:
        search(days, chains, args.slip)
        return
    if args.structures:
        cut = "2024-01-01"
        print(f"published settings (1.5 SD, 3x stop) · slip {args.slip} · 2019-2023 | 2024-2026\n")
        for lab, kw in (("weekly condor (published)", {}),
                        ("put credit spread only", {"structure": "put_spread"}),
                        ("call credit spread only", {"structure": "call_spread"}),
                        ("naked strangle, no wings", {"structure": "strangle"}),
                        ("two-week condor, 8 sessions", {"rank": 1, "sessions": 8})):
            tr = run(days, chains, slip=args.slip, **kw)
            a = [t for t in tr if t["entry"] < cut]
            b = [t for t in tr if t["entry"] >= cut]
            print(f"{lab:30s} ALL  {stats(tr)}")
            print(f"{'':30s} pre  ₹{sum(t['net'] for t in a):8.0f} PF {pf(a):4.2f} | 2024+ ₹{sum(t['net'] for t in b):8.0f} PF {pf(b):4.2f}")
        return
    tr = run(days, chains, slip=args.slip)
    print(f"{len(days)} trading days {days[0]}..{days[-1]} · slip {args.slip} pt/leg/side · per 65 qty\n")
    print(f"ALL   {stats(tr)}")
    for y in sorted({t['entry'][:4] for t in tr}):
        t = [x for x in tr if x["entry"][:4] == y]
        tot = sum(x["net"] for x in t)
        print(f"{y}  {stats(t)}  ({tot / CAPITAL:+.0%} on ₹1.5L)")
    pre = [t for t in tr if t["entry"] < "2024-11-20"]
    post = [t for t in tr if t["entry"] >= "2024-11-20"]
    print(f"\nbefore 20 Nov 2024 rules  {stats(pre)}")
    print(f"after  20 Nov 2024 rules  {stats(post)}")
    print("\nworst 8 weeks:")
    for t in sorted(tr, key=lambda t: t["net"])[:8]:
        lp, sp, sc, lc = t["strikes"]
        print(f"  entry {t['entry']} exp {t['exp']} exit {t['exit']} {t['why']:6s} ₹{t['net']:7.0f}  "
              f"F {t['F']:.0f} IV {t['iv']:.1%} credit {t['credit']:.1f}  {lp}/{sp}PE {sc}/{lc}CE")
    stops = sum(t["why"] == "stop" for t in tr)
    print(f"\nstops {stops}/{len(tr)} · median credit {st.median(t['credit'] for t in tr):.1f} pt · "
          f"median max loss ₹{st.median(t['max_loss'] for t in tr):.0f}")
    if args.variants:
        print("\nsensitivity (each changes one rule; 6 variants tried in total):")
        for lab, kw in (("no stop", {"stop_mult": None}), ("stop 2x credit", {"stop_mult": 2.0}),
                        ("short strikes 1.0 SD", {"sd": 1.0}), ("short strikes 2.0 SD", {"sd": 2.0}),
                        ("entry 5 sessions before", {"sessions": 5}), ("only when IV > 10-day RV", {"rv_filter": True})):
            print(f"  {lab:26s} {stats(run(days, chains, slip=args.slip, **kw))}")


if __name__ == "__main__":
    main()
