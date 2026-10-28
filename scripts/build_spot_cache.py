"""Per-minute spot OHLC + IV/VIX per day from recordings → spot_cache.pkl."""
import gzip, json, pickle
from pathlib import Path
REC = Path("data/recordings")
OUT = Path("data/spot_cache.pkl")
days = sorted({f.name[:10] for f in REC.glob("2026-*_*.jsonl.gz")})
out = {}
for d in days:
    m = {}
    for f in sorted(REC.glob(f"{d}_*.jsonl.gz")):
        with gzip.open(f, "rt") as fh:
            for line in fh:
                try: r = json.loads(line)
                except ValueError: continue
                hm = (r.get("ts") or "")[11:16]; s = r.get("spot")
                if not hm or hm < "09:15" or hm > "15:30" or not s: continue
                s = float(s); fd = r.get("feed") or {}
                b = m.get(hm)
                if b is None: m[hm] = b = {"o": s, "h": s, "l": s, "c": s}
                b["h"] = max(b["h"], s); b["l"] = min(b["l"], s); b["c"] = s
                for k in ("iv", "vix", "vix_chg", "iv_vs_day_low", "ivp", "adx", "expiry", "fut_oi", "fut_oi_base"):
                    if fd.get(k) is not None: b[k] = fd[k]
    if len(m) > 200: out[d] = m
OUT.write_bytes(pickle.dumps(out))
print(len(out), "days")
