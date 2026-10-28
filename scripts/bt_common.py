"""Shared backtest engine helpers for bt_sweep_factors / bt_setup_search / bt_oi_confirm.

Load spot + premium caches once via ``bootstrap()``. Prefer ``data/px_cache.pkl``.
"""

from __future__ import annotations

import pickle
import statistics as st
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atlas_lite.kite_charges import kite_nfo_charges  # noqa: E402

PX: dict[str, Any] = {}
SP: dict[str, Any] = {}
days: list[str] = []
half1: set[str] = set()
QTY, SLIP = 65, 0.5
h2m = lambda h: int(h[:2]) * 60 + int(h[3:])  # noqa: E731
m2h = lambda t: f"{t // 60:02d}:{t % 60:02d}"  # noqa: E731
SQ = h2m("15:14")
BANDS = [
    ("09:30-12:00", "09:30", "11:59"),
    ("12:00-13:30", "12:00", "13:29"),
    ("13:30-14:44", "13:30", "14:44"),
]


def bootstrap(spot_pkl: str | Path, px_pkl: str | Path | None = None) -> None:
    """Load premium + spot pickles into module globals."""
    global PX, SP, days, half1
    spot_path = Path(spot_pkl)
    px_path = Path(px_pkl) if px_pkl else ROOT / "data" / "px_cache.pkl"
    if not px_path.is_file():
        legacy = ROOT / "px_cache.pkl"
        if legacy.is_file():
            px_path = legacy
    PX = pickle.load(open(px_path, "rb"))
    SP = pickle.load(open(spot_path, "rb"))
    days = sorted(set(PX) & set(SP))
    half1 = set(days[: len(days) // 2])


def sim(px: dict[str, Any], t: int, side: str):
    hm = m2h(t)
    a = px.get(hm, {}).get("_atm")
    if not a:
        return None
    sym = a[0] if side == "ce" else a[1]
    e = px[hm].get(sym)
    if not e:
        return None
    end = min(SQ, t + 30)
    last = e
    x = None
    xt = end
    why = "time"
    for u in range(t + 1, end + 1):
        p = px.get(m2h(u), {}).get(sym)
        if p is None:
            continue
        last = p
        if p >= e * 1.2:
            x, xt, why = p, u, "target"
            break
        if p <= e * 0.9:
            x, xt, why = p, u, "stop"
            break
    x = last if x is None else x
    fills = [(e + SLIP, QTY, "buy"), (x - SLIP, QTY, "sell")]
    return (x - e - 2 * SLIP) * QTY - kite_nfo_charges(fills)["total"], xt, why


def feats(d: str, t: int) -> dict[str, Any]:
    sp = SP[d]
    hm = m2h(t)
    b = sp.get(hm) or {}
    ivs = [
        sp[m2h(u)]["iv"]
        for u in range(h2m("09:16"), t + 1)
        if m2h(u) in sp and sp[m2h(u)].get("iv")
    ]
    vx0 = next(
        (
            sp[m2h(u)].get("vix")
            for u in range(h2m("09:16"), t)
            if m2h(u) in sp and sp[m2h(u)].get("vix")
        ),
        None,
    )
    return {
        "ivp": b.get("ivp"),
        "adx": b.get("adx"),
        "iv_rise": (ivs[-1] / min(ivs) - 1) if ivs else None,
        "vix_rise": (b["vix"] / vx0 - 1) if b.get("vix") and vx0 else None,
        "expiry_pm": b.get("expiry") == d and t >= h2m("13:00"),
    }


FILTERS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "none": lambda f: True,
    "ivp<=70": lambda f: f["ivp"] is None or f["ivp"] <= 70,
    "iv_rise<10%": lambda f: f["iv_rise"] is None or f["iv_rise"] < 0.10,
    "no expiry pm": lambda f: not f["expiry_pm"],
    "vix_rise<5%": lambda f: f["vix_rise"] is None or f["vix_rise"] < 0.05,
    "adx>=25": lambda f: f["adx"] is not None and f["adx"] >= 25,
}


def v2_signals(d: str, start: str, until: str) -> dict[int, tuple[str, str]]:
    sp = SP[d]
    o = sp.get("09:15", {}).get("o")
    out: dict[int, tuple[str, str]] = {}
    for t in range(h2m(start), h2m(until) + 1):
        b = sp.get(m2h(t))
        if o and b:
            tr = b["c"] - o
            if abs(tr) >= 100:
                out[t] = ("ce" if tr > 0 else "pe", "trend")
    return out


def levels(d: str) -> list[tuple[str, float, str]]:
    sp = SP[d]
    i = days.index(d)
    L: list[tuple[str, float, str]] = []
    orb = [sp[m2h(t)] for t in range(h2m("09:15"), h2m("09:30")) if m2h(t) in sp]
    if orb:
        L += [("ORH", max(b["h"] for b in orb), "hi"), ("ORL", min(b["l"] for b in orb), "lo")]
    if i > 0:
        p = days[i - 1]
        if (date.fromisoformat(d) - date.fromisoformat(p)).days <= 4:
            pb = SP[p].values()
            L += [("PDH", max(b["h"] for b in pb), "hi"), ("PDL", min(b["l"] for b in pb), "lo")]
    return L


def sweep_signals(
    d: str,
    start: str,
    until: str,
    X: float,
    N: int,
    no_conflict: bool,
    only: tuple[str, ...] | None = None,
) -> dict[int, tuple[str, str]]:
    sp = SP[d]
    o = sp.get("09:15", {}).get("o")
    out: dict[int, tuple[str, str]] = {}
    for name, lv, kind in levels(d):
        if only and name[:2] not in only:
            continue
        swept = None
        prev_c = None
        for t in range(h2m("09:30"), h2m(until) + 1):
            b = sp.get(m2h(t))
            if not b:
                continue
            if swept is None:
                inside_before = prev_c is not None and (
                    prev_c <= lv if kind == "hi" else prev_c >= lv
                )
                if inside_before and (
                    (kind == "hi" and b["h"] > lv + X) or (kind == "lo" and b["l"] < lv - X)
                ):
                    swept = t
            else:
                if t - swept > N:
                    break
                if (kind == "hi" and b["c"] < lv) or (kind == "lo" and b["c"] > lv):
                    te = t + 1
                    side = "pe" if kind == "hi" else "ce"
                    if h2m(start) <= te <= h2m(until):
                        tr = (b["c"] - o) if o else 0
                        if no_conflict and (
                            (side == "pe" and tr >= 100) or (side == "ce" and tr <= -100)
                        ):
                            break
                        out.setdefault(te, (side, name))
                    break
            prev_c = b["c"]
    return out


def run(sigfn, flt: str = "none"):
    trades = []
    for d in days:
        sig = sigfn(d)
        n = 0
        dayp = 0.0
        t_free = 0
        block: dict[str, int] = {}
        locked: set[str] = set()
        stops = 0
        for t in sorted(sig):
            side, tag = sig[t]
            if n >= 3 or dayp <= -2000 or t < t_free or side in locked or t < block.get(side, 0):
                continue
            if not FILTERS[flt](feats(d, t)):
                continue
            r = sim(PX[d], t, side)
            if not r:
                continue
            net, xt, why = r
            n += 1
            dayp += net
            t_free = xt + 5
            trades.append({"d": d, "t": t, "side": side, "net": net, "why": why, "tag": tag})
            if why == "stop":
                stops += 1
                block[side] = xt + 15
                if net <= -400 or stops >= 2:
                    locked.add(side)
            elif net < 0:
                block[side] = xt + 10
            else:
                block[side] = xt + 5
    return trades


def summ(tr, label: str) -> str:
    if not tr:
        return f"{label:28s} no trades"
    v = [x["net"] for x in tr]
    h1 = sum(x["net"] for x in tr if x["d"] in half1)
    h2 = sum(v) - h1
    ce = sum(x["net"] for x in tr if x["side"] == "ce")
    pe = sum(v) - ce
    byd: dict[str, float] = {}
    for x in tr:
        byd[x["d"]] = byd.get(x["d"], 0) + x["net"]
    gw = sum(x for x in v if x > 0)
    gl = -sum(x for x in v if x < 0)
    pf = gw / gl if gl else float("inf")
    ok = (
        "PASS"
        if h1 > 0
        and h2 > 0
        and ce > 0
        and pe > 0
        and len(v) >= 30
        and min(byd.values()) >= -2000
        and pf >= 1.2
        else ""
    )
    return (
        f"{label:28s} n {len(v):3d}  ₹/tr {st.mean(v):5.0f}  tot ₹{sum(v):6.0f}  "
        f"H1 {h1:6.0f} H2 {h2:6.0f}"
        f"  CE {ce:6.0f} PE {pe:6.0f}  win {sum(x > 0 for x in v):2d}/{len(v):<2d} "
        f"PF {pf:4.2f} worst {min(byd.values()):5.0f} {ok}"
    )


def bands(tr, indent: str = "    ") -> None:
    for bn, a, b in BANDS:
        print(indent + summ([x for x in tr if h2m(a) <= x["t"] <= h2m(b)], bn))
