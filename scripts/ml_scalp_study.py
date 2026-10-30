"""Usage: <python with scikit-learn> scripts/ml_scalp_study.py [--horizon 15] [--model hgb|xgb]

Can gradient-boosted trees (XGBoost-style) predict NIFTY's next N minutes well
enough to scalp ATM options after costs? AI-trader's core idea, tested on our year.

Data: data/kite_spot_cache.pkl (Kite 1-min NIFTY + India VIX, 247 sessions) and
data/px_est_kite_year.pkl (calibrated premium estimates). Days with real recorded
premiums (data/px_cache.pkl) are also scored on real prices.

Walk-forward by month: train on every earlier month, predict the next one. The
model never sees the month it is scored on. Features use only bars up to the
signal minute. Trades: buy the ATM CE (model says up) or PE (down) on the most
confident signals, hold N minutes, at most 3 a day, no overlap, 09:30-14:45 entries.
Costs: Kite charges + slippage per leg per side (0.5 and 1.0 pt).

Needs scikit-learn (and xgboost for --model xgb); our .venv has neither, so run it
with a venv that does.
"""

from __future__ import annotations

import argparse
import math
import pickle
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from atlas_lite.agent_gates import NSE_HOLIDAYS  # noqa: E402
from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402

QTY = 65
MAX_TRADES = 3
FIRST, LAST = "09:30", "14:45"


def m2h(t: int) -> str:
    return f"{t // 60:02d}:{t % 60:02d}"


def sessions_to_expiry(d: str) -> int:
    day = date.fromisoformat(d)
    tue = day + timedelta(days=(1 - day.weekday()) % 7)
    while tue.isoformat() in NSE_HOLIDAYS:
        tue -= timedelta(days=1)
    n, cur = 0, day
    while cur < tue:
        cur += timedelta(days=1)
        if cur.weekday() < 5 and cur.isoformat() not in NSE_HOLIDAYS:
            n += 1
    return n


def day_frame(d: str, bars: dict, prev_close: float | None, horizon: int) -> pd.DataFrame:
    hms = sorted(bars)
    # Labels and lags shift by rows; they equal minutes only on a complete 09:15-15:29 day.
    if len(hms) != 375 or hms[0] != "09:15" or hms[-1] != "15:29":
        raise ValueError(f"{d}: {len(hms)} minute bars ({hms[0]}..{hms[-1]}); this test needs complete days")
    df = pd.DataFrame([bars[h] for h in hms], index=hms)
    c, h, l = df["c"], df["h"], df["l"]
    lr = np.log(c).diff()
    out = pd.DataFrame(index=hms)
    for k in (1, 3, 5, 10, 15, 30):
        out[f"r{k}"] = np.log(c / c.shift(k))
    o = df["o"].iloc[0]
    out["from_open"] = c / o - 1
    out["gap"] = (o / prev_close - 1) if prev_close else 0.0
    hi, lo = h.cummax(), l.cummin()
    out["range_pos"] = (c - lo) / (hi - lo).replace(0, np.nan)
    orh, orl = h.iloc[:15].max(), l.iloc[:15].min()
    out["or_pos"] = (c - orl) / max(orh - orl, 1e-9)
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    out["rsi14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    for span in (9, 21, 50):
        ema = c.ewm(span=span, adjust=False).mean()
        out[f"ema{span}_gap"] = c / ema - 1
    out["ema9_slope"] = c.ewm(span=9, adjust=False).mean().pct_change(5)
    out["atr14"] = (h - l).rolling(14).mean() / c
    out["rv15"] = lr.rolling(15).std()
    out["rv60"] = lr.rolling(60).std()
    vix = df["vix"] if "vix" in df else pd.Series(np.nan, index=hms)
    vix = vix.ffill()
    out["vix"] = vix
    out["vix_chg_open"] = vix / vix.dropna().iloc[0] - 1 if vix.notna().any() else np.nan
    out["vix_chg15"] = vix / vix.shift(15) - 1
    out["minute"] = [int(x[:2]) * 60 + int(x[3:]) for x in hms]
    out["dow"] = date.fromisoformat(d).weekday()
    out["dte"] = sessions_to_expiry(d)
    out["fwd"] = np.log(c.shift(-horizon) / c)
    out["fwd_pts"] = c.shift(-horizon) - c
    out["day"] = d
    out["hm"] = hms
    return out


def build(SP: dict, horizon: int) -> pd.DataFrame:
    frames, prev = [], None
    for d in sorted(SP):
        frames.append(day_frame(d, SP[d], prev, horizon))
        prev = SP[d][max(SP[d])]["c"]
    df = pd.concat(frames, ignore_index=True)
    df = df[(df["hm"] >= FIRST) & (df["hm"] <= LAST)].dropna(subset=["fwd"])
    return df


FEATURES = ["r1", "r3", "r5", "r10", "r15", "r30", "from_open", "gap", "range_pos", "or_pos", "rsi14",
            "ema9_gap", "ema21_gap", "ema50_gap", "ema9_slope", "atr14", "rv15", "rv60", "vix",
            "vix_chg_open", "vix_chg15", "minute", "dow", "dte"]


def model(kind: str):
    if kind == "xgb":
        from xgboost import XGBClassifier
        return XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.05, subsample=0.8,
                             colsample_bytree=0.8, eval_metric="logloss")
    return HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                          min_samples_leaf=200, l2_regularization=1.0)


def walk_forward(df: pd.DataFrame, kind: str, min_train_months: int = 3) -> pd.DataFrame:
    df = df.copy()
    df["month"] = df["day"].str[:7]
    months = sorted(df["month"].unique())
    parts = []
    for i in range(min_train_months, len(months)):
        tr, te = df[df["month"] < months[i]], df[df["month"] == months[i]]
        m = model(kind)
        m.fit(tr[FEATURES], (tr["fwd"] > 0).astype(int))
        p = m.predict_proba(te[FEATURES])[:, 1]
        trp = m.predict_proba(tr[FEATURES])[:, 1]
        te = te.assign(p=p, hi=np.quantile(trp, 0.95), lo=np.quantile(trp, 0.05))
        parts.append(te)
    return pd.concat(parts)


def option_pnl(PX: dict, d: str, hm: str, side: str, horizon: int, slip: float) -> float | None:
    t = int(hm[:2]) * 60 + int(hm[3:])
    row = PX.get(d, {}).get(hm) or {}
    atm = row.get("_atm")
    if not atm:
        return None
    sym = atm[0] if side == "ce" else atm[1]
    e = row.get(sym)
    x = (PX[d].get(m2h(t + horizon)) or {}).get(sym)
    if not e or x is None:
        return None
    fills = [(e + slip, QTY, "buy"), (max(0.05, x - slip), QTY, "sell")]
    return (x - e - 2 * slip) * QTY - kite_nfo_charges(fills)["total"]


def trade(pred: pd.DataFrame, PX: dict, horizon: int, slip: float, rng: np.random.Generator | None = None) -> list[dict]:
    """Most confident signals per day (beyond the train-set 95th/5th percentile), max 3, no overlap."""
    out = []
    for d, g in pred.groupby("day"):
        cand = g[(g["p"] >= g["hi"]) | (g["p"] <= g["lo"])].copy()
        cand["conf"] = (cand["p"] - 0.5).abs()
        free, n = -1, 0
        for _, r in cand.sort_values("minute").iterrows():
            if n >= MAX_TRADES or r["minute"] < free:
                continue
            side = "ce" if r["p"] >= 0.5 else "pe"
            if rng is not None:
                side = "ce" if rng.random() < 0.5 else "pe"
            pnl = option_pnl(PX, d, r["hm"], side, horizon, slip)
            if pnl is None:
                continue
            out.append({"day": d, "hm": r["hm"], "side": side, "pnl": pnl,
                        "right": (r["fwd"] > 0) == (side == "ce")})
            n += 1
            free = r["minute"] + horizon
    return out


def summ(tr: list[dict]) -> str:
    if not tr:
        return "no trades"
    v = np.array([t["pnl"] for t in tr])
    gl = -v[v <= 0].sum()
    return (f"n {len(v):4d}  total ₹{v.sum():9.0f}  ₹/trade {v.mean():6.0f}  win {np.mean(v > 0):4.0%}  "
            f"direction right {np.mean([t['right'] for t in tr]):4.0%}  PF {v[v > 0].sum() / gl if gl else 0:4.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=15)
    ap.add_argument("--model", choices=("hgb", "xgb"), default="hgb")
    args = ap.parse_args()
    SP = pickle.load(open(ROOT / "data" / "kite_spot_cache.pkl", "rb"))
    PX = pickle.load(open(ROOT / "data" / "px_est_kite_year.pkl", "rb"))
    df = build(SP, args.horizon)
    pred = walk_forward(df, args.model)
    up = (pred["fwd"] > 0).astype(int)
    print(f"{args.model} · {args.horizon}-min horizon · {len(df):,} minutes · walk-forward test "
          f"{pred['day'].min()}..{pred['day'].max()} ({pred['day'].nunique()} days, never seen in training)\n")
    print(f"prediction quality (AUC, 0.50 = coin flip): all minutes {roc_auc_score(up, pred['p']):.3f}")
    for mo, g in pred.groupby("month"):
        print(f"  {mo}  AUC {roc_auc_score((g['fwd'] > 0).astype(int), g['p']):.3f}")
    conf = pred[(pred["p"] >= pred["hi"]) | (pred["p"] <= pred["lo"])]
    signed = np.where(conf["p"] >= 0.5, conf["fwd_pts"], -conf["fwd_pts"])
    print(f"\nmost confident 10% of minutes: direction right {np.mean(signed > 0):.0%}, "
          f"average move in the predicted direction {signed.mean():+.1f} NIFTY pts over {args.horizon} min")
    print("(an ATM option moves ~0.5x NIFTY; a round trip costs ~₹130-260 = ~2-4 premium pts, i.e. ~4-8 NIFTY pts)\n")
    rng = np.random.default_rng(7)
    for slip in (0.5, 1.0):
        print(f"slippage {slip} pt/side · estimated premiums · ML signals : {summ(trade(pred, PX, args.horizon, slip))}")
        print(f"slippage {slip} pt/side · estimated premiums · random side: {summ(trade(pred, PX, args.horizon, slip, rng))}")
    real = ROOT / "data" / "px_cache.pkl"
    if real.is_file():
        RX = pickle.load(open(real, "rb"))
        sub = pred[pred["day"].isin(RX)]
        if len(sub):
            print(f"\nREAL recorded premiums, {sub['day'].nunique()} overlapping days, slippage 0.5: "
                  f"{summ(trade(sub, RX, args.horizon, 0.5))}")


if __name__ == "__main__":
    main()
