"""Usage: python3 scripts/build_spot_cache.py && python3 scripts/bt_sweep_factors.py data/spot_cache.pkl  (needs px_cache.pkl in repo root)

Liquidity-sweep setup + factor filters on the 23-day tape, full day by time band.

Book rules as live v2: 1 lot, +20%/-10%/30m, max 3/day, -2000 day stop, one position,
side blocks (stop 15m, loss 10m, win 5m), side lock after stop <= -400 or 2 stops.
Costs: 0.5 pt slip per leg + Kite charges.
"""
import pickle, statistics as st, sys
from datetime import date
sys.path.insert(0, ".")
from atlas_lite.kite_charges import kite_nfo_charges

PX = pickle.load(open("px_cache.pkl", "rb"))
SP = pickle.load(open(sys.argv[1], "rb"))
QTY, SLIP = 65, 0.5
days = sorted(set(PX) & set(SP))
half1 = set(days[: len(days) // 2])
h2m = lambda h: int(h[:2]) * 60 + int(h[3:])
m2h = lambda t: f"{t // 60:02d}:{t % 60:02d}"
SQ = h2m("15:14")
BANDS = [("09:30-12:00", "09:30", "11:59"), ("12:00-13:30", "12:00", "13:29"), ("13:30-14:44", "13:30", "14:44")]


def sim(px, t, side):
    hm = m2h(t); a = px.get(hm, {}).get("_atm")
    if not a: return None
    sym = a[0] if side == "ce" else a[1]; e = px[hm].get(sym)
    if not e: return None
    end = min(SQ, t + 30); last = e; x = None; xt = end; why = "time"
    for u in range(t + 1, end + 1):
        p = px.get(m2h(u), {}).get(sym)
        if p is None: continue
        last = p
        if p >= e * 1.2: x, xt, why = p, u, "target"; break
        if p <= e * 0.9: x, xt, why = p, u, "stop"; break
    x = last if x is None else x
    fills = [(e + SLIP, QTY, "buy"), (x - SLIP, QTY, "sell")]
    return (x - e - 2 * SLIP) * QTY - kite_nfo_charges(fills)["total"], xt, why


def feats(d, t):
    sp = SP[d]; hm = m2h(t); b = sp.get(hm) or {}
    ivs = [sp[m2h(u)]["iv"] for u in range(h2m("09:16"), t + 1) if m2h(u) in sp and sp[m2h(u)].get("iv")]
    vx0 = next((sp[m2h(u)].get("vix") for u in range(h2m("09:16"), t) if m2h(u) in sp and sp[m2h(u)].get("vix")), None)
    return {
        "ivp": b.get("ivp"), "adx": b.get("adx"),
        "iv_rise": (ivs[-1] / min(ivs) - 1) if ivs else None,
        "vix_rise": (b["vix"] / vx0 - 1) if b.get("vix") and vx0 else None,
        "expiry_pm": b.get("expiry") == d and t >= h2m("13:00"),
    }


FILTERS = {
    "none": lambda f: True,
    "ivp<=70": lambda f: f["ivp"] is None or f["ivp"] <= 70,
    "iv_rise<10%": lambda f: f["iv_rise"] is None or f["iv_rise"] < 0.10,
    "no expiry pm": lambda f: not f["expiry_pm"],
    "vix_rise<5%": lambda f: f["vix_rise"] is None or f["vix_rise"] < 0.05,
    "adx>=25": lambda f: f["adx"] is not None and f["adx"] >= 25,
}


def v2_signals(d, start, until):
    sp = SP[d]; o = sp.get("09:15", {}).get("o")
    out = {}
    for t in range(h2m(start), h2m(until) + 1):
        b = sp.get(m2h(t))
        if o and b:
            tr = b["c"] - o
            if abs(tr) >= 100: out[t] = ("ce" if tr > 0 else "pe", "trend")
    return out


def levels(d):
    sp = SP[d]; i = days.index(d); L = []
    orb = [sp[m2h(t)] for t in range(h2m("09:15"), h2m("09:30")) if m2h(t) in sp]
    if orb: L += [("ORH", max(b["h"] for b in orb), "hi"), ("ORL", min(b["l"] for b in orb), "lo")]
    if i > 0:
        p = days[i - 1]
        if (date.fromisoformat(d) - date.fromisoformat(p)).days <= 4:
            pb = SP[p].values(); L += [("PDH", max(b["h"] for b in pb), "hi"), ("PDL", min(b["l"] for b in pb), "lo")]
    return L


def sweep_signals(d, start, until, X, N, no_conflict, only=None):
    sp = SP[d]; o = sp.get("09:15", {}).get("o"); out = {}
    for name, lv, kind in levels(d):
        if only and name[:2] not in only: continue
        swept = None; prev_c = None
        for t in range(h2m("09:30"), h2m(until) + 1):
            b = sp.get(m2h(t))
            if not b: continue
            if swept is None:
                inside_before = prev_c is not None and (prev_c <= lv if kind == "hi" else prev_c >= lv)
                if inside_before and ((kind == "hi" and b["h"] > lv + X) or (kind == "lo" and b["l"] < lv - X)):
                    swept = t
            else:
                if t - swept > N: break
                if (kind == "hi" and b["c"] < lv) or (kind == "lo" and b["c"] > lv):
                    te = t + 1; side = "pe" if kind == "hi" else "ce"
                    if h2m(start) <= te <= h2m(until):
                        tr = (b["c"] - o) if o else 0
                        if no_conflict and ((side == "pe" and tr >= 100) or (side == "ce" and tr <= -100)): break
                        out.setdefault(te, (side, name))
                    break
            prev_c = b["c"]
    return out


def run(sigfn, flt="none"):
    trades = []
    for d in days:
        sig = sigfn(d); n = 0; dayp = 0.0; t_free = 0; block = {}; locked = set(); stops = 0
        for t in sorted(sig):
            side, tag = sig[t]
            if n >= 3 or dayp <= -2000 or t < t_free or side in locked or t < block.get(side, 0): continue
            if not FILTERS[flt](feats(d, t)): continue
            r = sim(PX[d], t, side)
            if not r: continue
            net, xt, why = r; n += 1; dayp += net; t_free = xt + 5
            trades.append({"d": d, "t": t, "side": side, "net": net, "why": why, "tag": tag})
            if why == "stop":
                stops += 1; block[side] = xt + 15
                if net <= -400 or stops >= 2: locked.add(side)
            elif net < 0: block[side] = xt + 10
            else: block[side] = xt + 5
    return trades


def summ(tr, label):
    if not tr: return f"{label:28s} no trades"
    v = [x["net"] for x in tr]; h1 = sum(x["net"] for x in tr if x["d"] in half1); h2 = sum(v) - h1
    ce = sum(x["net"] for x in tr if x["side"] == "ce"); pe = sum(v) - ce
    byd = {}
    for x in tr: byd[x["d"]] = byd.get(x["d"], 0) + x["net"]
    gw = sum(x for x in v if x > 0); gl = -sum(x for x in v if x < 0)
    pf = gw / gl if gl else float("inf")
    ok = "PASS" if h1 > 0 and h2 > 0 and ce > 0 and pe > 0 and len(v) >= 30 and min(byd.values()) >= -2000 and pf >= 1.2 else ""
    return (f"{label:28s} n {len(v):3d}  ₹/tr {st.mean(v):5.0f}  tot ₹{sum(v):6.0f}  H1 {h1:6.0f} H2 {h2:6.0f}"
            f"  CE {ce:6.0f} PE {pe:6.0f}  win {sum(x > 0 for x in v):2d}/{len(v):<2d} PF {pf:4.2f} worst {min(byd.values()):5.0f} {ok}")


def bands(tr, indent="    "):
    for bn, a, b in BANDS:
        print(indent + summ([x for x in tr if h2m(a) <= x["t"] <= h2m(b)], bn))


print(f"{len(days)} days {days[0]}..{days[-1]}; halves split at {max(half1)}\n")
print("=== V2 (spot vs open >= 100), full day, by band ===")
v2 = run(lambda d: v2_signals(d, "09:30", "14:44"))
print(summ(v2, "v2 09:30-14:44")); bands(v2)
print("\n=== SWEEP variants (X pts beyond level, N min to reclaim), 09:30-14:44, skip vs v2 trend ===")
for X in (5, 10, 20):
    for N in (3, 5, 10):
        tr = run(lambda d: sweep_signals(d, "09:30", "14:44", X, N, True))
        print(summ(tr, f"X={X:2d} N={N:2d}"))
print("\n=== SWEEP X=10 N=5 detail ===")
base = lambda d: sweep_signals(d, "09:30", "14:44", 10, 5, True)
tr = run(base); print(summ(tr, "base (no-conflict)")); bands(tr)
tr2 = run(lambda d: sweep_signals(d, "09:30", "14:44", 10, 5, False)); print(summ(tr2, "base incl. vs-trend"))
for lv in ("OR", "PD"):
    t3 = run(lambda d, lv=lv: sweep_signals(d, "09:30", "14:44", 10, 5, True, only=(lv,))); print(summ(t3, f"levels {lv} only"))
print("\n=== FILTERS (each alone) ===")
for name in FILTERS:
    if name == "none": continue
    for lab, fn in (("v2", lambda d: v2_signals(d, "09:30", "14:44")), ("sweep", base)):
        t = run(fn, name); print(summ(t, f"{lab} + {name}")); bands(t, "      ")
