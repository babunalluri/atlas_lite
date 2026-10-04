#!/usr/bin/env python3
"""Theta-Cliff Fence (TCF) backtest — expiry-day noon iron condor.

Research script behind ``atlas_lite/paper_theta_cliff.py``.

Strikes sit beyond BOTH the morning high/low fence and k x remaining sigma
(prev-day VIX). Premiums are Black-Scholes at VIX_prev x f with a per-sigma
skew bump, plus slippage and Kite charges (1 lot). f=1.0 / skew=0.1 matched
the recorded 2026-09-29 expiry chain within ~1-3 pts; f=0.85 / skew=0 is the
pessimistic stress.

Data: ``data/regime/nifty_5m.json`` + ``data/regime/vix_day.json``.

Usage:
    python scripts/tcf_bt.py           # locked rule: IS / OOS / stress grid
    python scripts/tcf_bt.py --scan    # in-sample parameter scan
    python scripts/tcf_bt.py --trades  # per-expiry trade log
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import itertools
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402

QTY = 65
RATE = 0.065
YEAR_MIN = 252 * 375  # trading-minute year
REGIME = ROOT / "data" / "regime"

# Locked rule (chosen on the first 2/3 of expiries only).
RULE = dict(T="12:00", k=0.75, buf=0, stop=0, gate=0.9)


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, v, cp, r=RATE):
    if T <= 0 or v <= 0:
        return max(0.0, (S - K) if cp == "C" else (K - S))
    d1 = (math.log(S / K) + (r + v * v / 2) * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)
    if cp == "C":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def _load():
    bars = json.loads((REGIME / "nifty_5m.json").read_text())
    vix = {r[0][:10]: r[4] for r in json.loads((REGIME / "vix_day.json").read_text())}
    days = collections.OrderedDict()
    for t, o, h, l, c, _v in bars:
        days.setdefault(t[:10], []).append((t[11:16], o, h, l, c))
    dl = [d for d in days if days[d][-1][0] >= "15:20" and len(days[d]) >= 70]
    pv, last = {}, None
    for d in sorted(set(dl) | set(vix)):
        pv[d] = last
        if d in vix:
            last = vix[d]
    # Weekly expiry = last session of each Wed..Tue bucket (Mon when Tue is a holiday).
    by = collections.defaultdict(list)
    for d in dl:
        x = dt.date.fromisoformat(d)
        by[(x - dt.timedelta(days=(x.weekday() - 2) % 7)).isoformat()].append(d)
    exp = sorted(max(v) for v in by.values())
    # Drop a trailing partial week (its last session is not a Mon/Tue expiry).
    exp = [d for d in exp if pv.get(d) and dt.date.fromisoformat(d).weekday() in (0, 1)]
    return days, pv, exp


days, pv, EXP = _load()


def mins(hm):
    h, m = map(int, hm.split(":"))
    return h * 60 + m


def slip(p):
    return max(0.5, 0.02 * p)


def px(S, K, Tm, vol, cp, skew):
    T = max(Tm, 0) / YEAR_MIN
    if T <= 0:
        return bs(S, K, 0, vol, cp)
    z = abs(K - S) / (S * vol * math.sqrt(T))
    return bs(S, K, T, vol * (1 + skew * min(z, 3)), cp)


def run(d, T, k, buf, stop, f, W=100, exit_hm="15:15", fstop=1.3, skew=0.1, gate=None):
    bs_ = days[d]
    pre = [x for x in bs_ if x[0] < T]
    post = [x for x in bs_ if T <= x[0] < exit_hm]
    S = pre[-1][4]
    H = max(x[2] for x in pre)
    L = min(x[3] for x in pre)
    vol = pv[d] / 100 * f
    rem = 15 * 60 + 30 - mins(T)
    sig_imp = S * pv[d] / 100 * math.sqrt(rem / YEAR_MIN)  # strike rule: plain prev-day VIX
    sig_bar = S * pv[d] / 100 * math.sqrt(5 / YEAR_MIN)
    rets = [pre[i][4] - pre[i - 1][4] for i in range(2, len(pre))]  # skip opening bar
    rv_ratio = math.sqrt(sum(x * x for x in rets) / len(rets)) / sig_bar
    if gate is not None and rv_ratio > gate:
        return None
    up = S + k * sig_imp
    dn = S - k * sig_imp
    if buf is not None:
        up = max(up, H + buf)
        dn = min(dn, L - buf)
    Kc = math.ceil(up / 50) * 50
    Kp = math.floor(dn / 50) * 50
    legs = {"C": (Kc, Kc + W), "P": (Kp, Kp - W)}
    open_px, ch_legs, pnl_pts = {}, [], 0.0
    for cp, (Ks, Kl) in legs.items():
        s = px(S, Ks, rem, vol, cp, skew)
        b = px(S, Kl, rem, vol, cp, skew)
        sp, bp = max(0.05, s - slip(s)), b + slip(b)
        open_px[cp] = sp - bp
        ch_legs += [(sp, QTY, "sell"), (bp, QTY, "buy")]
    credit = sum(open_px.values())
    if credit <= 0.5:
        return None
    closed = {}
    for hm, o, h, l, _c in post:
        tm = 15 * 60 + 30 - mins(hm) - 5
        for cp, (Ks, _Kl) in legs.items():
            if cp in closed or stop is None:
                continue
            trig = (h >= Ks - stop) if cp == "C" else (l <= Ks + stop)
            if trig:
                px_S = (Ks - stop) if cp == "C" else (Ks + stop)
                gap = (o >= Ks - stop) if cp == "C" else (o <= Ks + stop)
                if gap:
                    px_S = o
                closed[cp] = (px_S, tm, vol * fstop)
    Sx = post[-1][4] if post else S
    tx = 15 * 60 + 30 - mins(exit_hm)
    for cp, (Ks, Kl) in legs.items():
        Sc, tm, v = closed.get(cp, (Sx, tx, vol))
        s = px(Sc, Ks, tm, v, cp, skew)
        b = px(Sc, Kl, tm, v, cp, skew)
        sp, bp = s + slip(s), max(0.0, b - slip(b))
        pnl_pts += open_px[cp] - min(W, sp - bp)
        ch_legs += [(sp, QTY, "buy"), (bp, QTY, "sell")]
    gross = pnl_pts * QTY
    charges = float(kite_nfo_charges([x for x in ch_legs if x[0] > 0])["total"])
    return dict(d=d, S=S, Kc=Kc, Kp=Kp, credit=round(credit, 2), pnl=round(gross - charges, 0),
                stopped="".join(sorted(closed)), rv=round(rv_ratio, 2),
                max_loss=round((W - credit) * QTY, 0))


def stats(tr):
    if not tr:
        return None
    p = [t["pnl"] for t in tr]
    w = [x for x in p if x > 0]
    ls = [x for x in p if x <= 0]
    eq = pk = dd = 0
    for x in p:
        eq += x
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return dict(n=len(p), win=round(100 * len(w) / len(p), 1), total=round(sum(p)),
                avg=round(sum(p) / len(p)), avg_win=round(sum(w) / len(w)) if w else 0,
                avg_loss=round(sum(ls) / len(ls)) if ls else 0, worst=round(min(p)),
                pf=round(sum(w) / -sum(ls), 2) if ls and sum(ls) < 0 else 99, dd=round(dd))


def _rule_trades(dates, **kw):
    r = dict(RULE)
    gate = r.pop("gate")
    return [t for d in dates if (t := run(d, **r, gate=gate, **kw))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scan", action="store_true", help="in-sample parameter scan")
    ap.add_argument("--trades", action="store_true", help="per-expiry trade log (realistic pricing)")
    args = ap.parse_args()
    split = int(len(EXP) * 2 / 3)
    IS, OOS = EXP[:split], EXP[split:]
    print(f"expiry days {len(EXP)}  IS {IS[0]}..{IS[-1]}  OOS {OOS[0]}..{OOS[-1]}")

    if args.scan:
        res = []
        for T, k, buf, stop, gate in itertools.product(
            ["11:30", "12:00", "12:30", "13:00"], [0.75, 1.0, 1.25, 1.5], [None, 0], [None, 0], [None, 0.9, 0.75]
        ):
            s = stats([r for d in IS if (r := run(d, T, k, buf, stop, f=1.0, skew=0.1, gate=gate))])
            if s and s["n"] >= 20:
                res.append(((T, k, buf, stop, gate), s))
        print("\nIS (f=1.0 skew=0.1), win>=85%, by expectancy:")
        for p, s in sorted([r for r in res if r[1]["win"] >= 85], key=lambda x: -x[1]["avg"])[:15]:
            print(p, s)
        return

    if args.trades:
        for d in EXP:
            r = dict(RULE)
            gate = r.pop("gate")
            t = run(d, **r, gate=gate, f=1.0, skew=0.1)
            if t is None:
                print(d, "SKIP (hot morning)")
            else:
                print(f"{d} S={t['S']:.0f} {t['Kp']}P/{t['Kc']}C credit={t['credit']:5.1f} "
                      f"rv={t['rv']} stop={t['stopped'] or '-'} pnl={t['pnl']:+.0f}")
        return

    print("locked rule", RULE)
    for label, f, sk in [("realistic", 1.0, 0.1), ("pessimistic", 0.85, 0.0)]:
        for name, dates in [("ALL", EXP), ("IS", IS), ("OOS", OOS)]:
            print(f"{label:11} {name:3}", stats(_rule_trades(dates, f=f, skew=sk)))


if __name__ == "__main__":
    main()
