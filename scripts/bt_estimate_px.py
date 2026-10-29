"""Usage: python3 scripts/bt_estimate_px.py

Estimate per-minute NIFTY weekly option premiums where real ones are missing
(Kite keeps no expired options), so the tape engine can run on the Kite year.

Model: Black-76 on forward F = spot * (1 + basis * calendar days to expiry),
sigma = VIX * ratio(days-to-expiry bucket), time to expiry on the same 252-day
trading basis as the live ATM IV (atlas_lite.metrics.trading_years_to_expiry).
Weekly expiry = Tuesday, moved back to the previous session on a holiday.

ratio and basis are fitted on the recorded days, where real ATM IV and real
premiums exist (data/spot_cache.pkl + data/px_cache.pkl). The fit is checked on
those days before the year is priced.

Writes data/px_est_recorded.pkl (the recorded days, for the check) and
data/px_est_kite_year.pkl (the Kite year), same shape as data/px_cache.pkl.
"""

from __future__ import annotations

import math
import pickle
import statistics as st
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.metrics import trading_years_to_expiry  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
DATA = ROOT / "data"
STEP = 50
WING = 12  # strikes each side of ATM priced every minute (±600 pt)
h2m = lambda h: int(h[:2]) * 60 + int(h[3:])  # noqa: E731
m2h = lambda t: f"{t // 60:02d}:{t % 60:02d}"  # noqa: E731


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def b76(F: float, K: float, T: float, sigma: float, call: bool) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, (F - K) if call else (K - F))
    s = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    d2 = d1 - s
    return F * ncdf(d1) - K * ncdf(d2) if call else K * ncdf(-d2) - F * ncdf(-d1)


def expiry_for(d: str, sessions: set[str], holidays: set[str]) -> date:
    day = date.fromisoformat(d)
    tue = day + timedelta(days=(1 - day.weekday()) % 7)
    while tue.isoformat() in holidays or (tue.isoformat() < max(sessions) and tue.isoformat() not in sessions):
        tue -= timedelta(days=1)
    return tue


def bucket(d: str, hm: str, exp: date, holidays: set[str]) -> str:
    """Days to expiry x hour of day: real IV falls through the session, most on expiry day."""
    hour = hm[:2] if hm >= "09:30" else "0915"
    if d == exp.isoformat():
        return f"exp@{hour}"
    n, cur = 0, date.fromisoformat(d)
    while cur < exp:  # trading days after d up to and including expiry
        cur += timedelta(days=1)
        if cur.weekday() < 5 and cur.isoformat() not in holidays:
            n += 1
    return f"d{min(n, 4)}@{hour}"


def tte(d: str, hm: str, exp: date) -> float:
    now = datetime.fromisoformat(f"{d}T{hm}:59").replace(tzinfo=IST)
    return trading_years_to_expiry(exp, now=now)


def fit(SP: dict, PX: dict, holidays: set[str]) -> tuple[dict[str, float], float]:
    sessions = sorted(set(SP) & set(PX))
    ratios: dict[str, list[float]] = {}
    basis: list[float] = []
    for d in sessions:
        for hm, b in SP[d].items():
            if hm < "09:20" or hm > "15:25":
                continue
            exp = expiry_for(d, set(sessions), holidays)
            if b.get("iv") and b.get("vix"):
                ratios.setdefault(bucket(d, hm, exp, holidays), []).append(b["iv"] / b["vix"])
            px = PX[d].get(hm) or {}
            atm = px.get("_atm")
            if atm and px.get(atm[0]) and px.get(atm[1]) and b.get("c"):
                K = float(atm[0].split("CE")[0][-5:])
                cal = (datetime.combine(exp, datetime.min.time()) - datetime.fromisoformat(d)).days + 0.5
                basis.append((K + px[atm[0]] - px[atm[1]] - b["c"]) / b["c"] / cal)
    out = {k: st.median(v) for k, v in ratios.items() if len(v) >= 30}
    for day_key in {k.split("@")[0] for k in ratios}:
        pooled = [x for k, v in ratios.items() if k.split("@")[0] == day_key for x in v]
        out[day_key] = st.median(pooled)
    return out, st.median(basis)


def price_days(SP: dict, ratio: dict[str, float], basis: float, holidays: set[str], wing: int = WING) -> dict:
    sessions = sorted(SP)
    out: dict = {}
    for d in sessions:
        exp = expiry_for(d, set(sessions), holidays)
        cal = (datetime.combine(exp, datetime.min.time()) - datetime.fromisoformat(d)).days + 0.5
        day: dict = {}
        for hm, b in SP[d].items():
            S, vix = b.get("c"), b.get("vix")
            if not S or not vix:
                continue
            key = bucket(d, hm, exp, holidays)
            sigma = vix / 100.0 * ratio.get(key, ratio.get(key.split("@")[0], 1.0))
            T = tte(d, hm, exp)
            F = S * (1.0 + basis * cal)
            k0 = int(round(S / STEP) * STEP)
            row = {"_atm": (f"{k0}CE", f"{k0}PE")}
            for i in range(-wing, wing + 1):
                k = k0 + i * STEP
                row[f"{k}CE"] = round(b76(F, k, T, sigma, True), 2)
                row[f"{k}PE"] = round(b76(F, k, T, sigma, False), 2)
            day[hm] = row
        out[d] = day
    return out


def main() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from atlas_lite.agent_gates import NSE_HOLIDAYS

    holidays = set(NSE_HOLIDAYS)
    SP = pickle.load(open(DATA / "spot_cache.pkl", "rb"))
    PX = pickle.load(open(DATA / "px_cache.pkl", "rb"))
    real_days = sorted(set(SP) & set(PX))
    ratio, basis = fit(SP, PX, holidays)
    print(f"fit on {len(real_days)} recorded days {real_days[0]}..{real_days[-1]}")
    print("  IV/VIX by days to expiry:", {k: round(v, 3) for k, v in sorted(ratio.items()) if "@" not in k})
    print("  IV/VIX by hour, expiry day:", {k.split("@")[1]: round(v, 2) for k, v in sorted(ratio.items()) if k.startswith("exp@")})
    print("  IV/VIX by hour, 1 day before:", {k.split("@")[1]: round(v, 2) for k, v in sorted(ratio.items()) if k.startswith("d1@")})
    print(f"  forward basis: {basis * 1e4:.2f} bp per calendar day")

    # Check: estimated vs real ATM premium on the recorded days.
    est = price_days({d: SP[d] for d in real_days}, ratio, basis, holidays)
    errs: list[float] = []
    for d in real_days:
        for hm, row in PX[d].items():
            atm = row.get("_atm")
            if not atm or hm < "09:20" or hm > "15:25":
                continue
            k = atm[0].split("CE")[0][-5:]
            e = est[d].get(hm) or {}
            for real_sym, est_sym in ((atm[0], f"{k}CE"), (atm[1], f"{k}PE")):
                if row.get(real_sym) and e.get(est_sym):
                    errs.append(e[est_sym] / row[real_sym] - 1.0)
    errs.sort()
    q = lambda p: errs[int(p * (len(errs) - 1))]  # noqa: E731
    print(f"  estimated / real ATM premium - 1 over {len(errs)} quotes: median {q(0.5):+.1%}, "
          f"middle 80% {q(0.1):+.1%}..{q(0.9):+.1%}")
    # Real tape keys symbols as NFO:NIFTY...; estimates as "<strike>CE". Re-key so _atm matches.
    (DATA / "px_est_recorded.pkl").write_bytes(pickle.dumps(est))

    KY = pickle.load(open(DATA / "kite_spot_cache.pkl", "rb"))
    year = price_days(KY, ratio, basis, holidays)
    (DATA / "px_est_kite_year.pkl").write_bytes(pickle.dumps(year))
    print(f"priced Kite year: {len(year)} sessions {min(year)}..{max(year)} -> data/px_est_kite_year.pkl")
    if "--wide" in sys.argv:  # +-2,000 pt grid for far-wing structures
        wide = price_days(KY, ratio, basis, holidays, wing=40)
        (DATA / "px_est_kite_year_wide.pkl").write_bytes(pickle.dumps(wide))
        print("priced +-2,000 pt grid -> data/px_est_kite_year_wide.pkl")


if __name__ == "__main__":
    main()
