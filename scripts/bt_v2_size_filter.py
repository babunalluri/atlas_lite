"""Usage: <python with scikit-learn> scripts/bt_v2_size_filter.py

Scalper v2 (buy ATM CE/PE when spot is >=100 pt from the open, 09:30-12:00,
+20%/-10%/30 min, max 3/day) with and without a move-size filter: take a v2 signal
only when a gradient-boosted model expects a big NIFTY move over the next 30 min.

The size model is walk-forward (trained on earlier months only; see
scripts/ml_scalp_study.py for features). Label: |30-min move| above the training
median. Thresholds: the model's own training-set percentiles (top 50/30/20%).
Scored on the Kite year with estimated premiums and on the recorded days with
real premiums. Book rules and costs from scripts/bt_setup_search.py (0.5 pt slip).
Caveat: the percentile cut-offs come from in-sample training predictions, which are
more extreme than test-month ones, so "top 20%" keeps fewer signals than its name;
every row prints its actual trade count.
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import bt_common as eng  # noqa: E402
import ml_scalp_study as ml  # noqa: E402
from bt_setup_search import runx, trend  # noqa: E402

V2_EXITS = (0.20, 0.10, 30)
TOPS = (0.5, 0.3, 0.2)


def size_predictions() -> dict[tuple[str, str], tuple[float, dict]]:
    """(day, HH:MM) -> (p_big, {top: threshold}) for every walk-forward test minute."""
    SP = pickle.load(open(ROOT / "data" / "kite_spot_cache.pkl", "rb"))
    df = ml.build(SP, 30)
    df["month"] = df["day"].str[:7]
    months = sorted(df["month"].unique())
    out = {}
    for i in range(3, len(months)):
        tr, te = df[df["month"] < months[i]], df[df["month"] == months[i]]
        cut = np.median(np.abs(tr["fwd"]))
        m = ml.model("hgb").fit(tr[ml.FEATURES], (np.abs(tr["fwd"]) > cut).astype(int))
        ptr = m.predict_proba(tr[ml.FEATURES])[:, 1]
        thr = {top: float(np.quantile(ptr, 1 - top)) for top in TOPS}
        for (d, hm), p in zip(zip(te["day"], te["hm"]), m.predict_proba(te[ml.FEATURES])[:, 1]):
            out[(d, hm)] = (float(p), thr)
    return out


def summ(tr: list[dict]) -> str:
    if not tr:
        return "no trades"
    v = np.array([t["net"] for t in tr])
    gl = -v[v <= 0].sum()
    return (f"n {len(v):4d}  total ₹{v.sum():8.0f}  ₹/trade {v.mean():5.0f}  win {np.mean(v > 0):4.0%}  "
            f"PF {v[v > 0].sum() / gl if gl else 0:4.2f}")


def run(spot: str, px: str, label: str, preds: dict) -> None:
    eng.bootstrap(ROOT / spot, ROOT / px)
    test_days = sorted({d for d in eng.days if (d, "10:00") in preds})
    if not test_days:
        print(f"\n{label}: no days inside the walk-forward test months")
        return
    base = trend(100, "09:30", "11:59")
    print(f"\n{label}: {len(test_days)} walk-forward test days {test_days[0]}..{test_days[-1]}")
    print(f"  v2 as live                         {summ(runx(base, V2_EXITS, 'none', test_days))}")
    for top in TOPS:
        def filtered(d, top=top):
            sig = base(d)
            keep = {}
            for t, s in sig.items():
                p = preds.get((d, eng.m2h(t)))
                if p and p[0] >= p[1][top]:
                    keep[t] = s
            return keep
        print(f"  v2 + size filter (top {top:.0%} predicted) {summ(runx(filtered, V2_EXITS, 'none', test_days))}")


def main() -> None:
    preds = size_predictions()
    run("data/kite_spot_cache.pkl", "data/px_est_kite_year.pkl", "Kite year, estimated premiums", preds)
    run("data/spot_cache.pkl", "data/px_cache.pkl", "Recorded days, REAL premiums", preds)


if __name__ == "__main__":
    main()
