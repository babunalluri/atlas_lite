"""Score COMBO paper rules on minute_bars + recordings (1 lot, Kite NFO).

Matches ``paper_combo``: 1m closed votes, B→CE / S→PE, +8/−6 or 12m,
flatten on confluence lost or opposite letter, max 4/day, 8m cooldown.
5m research rows stamp the **bucket close** minute so the option walk
does not start inside the unfinished bucket.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.combo import confluence_rows  # noqa: E402
from atlas_lite.minute_bars import load_bars  # noqa: E402
from atlas_lite.paper_vwap_long import aggregate_bars  # noqa: E402
from atlas_lite.recorder import record_dir  # noqa: E402
from atlas_lite.specs import NIFTY_SYMBOL  # noqa: E402
from scripts.scan_option_ideas import _settle, _trade  # noqa: E402
from scripts.score_recordings import _hm_to_min, _index_by_day, load_minute_rows  # noqa: E402

QTY = 65
AFTER = "09:30"
BEFORE = "14:45"
UNTIL = "15:14"
COOLDOWN = 8
HOLD_MIN = 12
TARGET = 0.08
STOP = -0.06
MAX_DAY = 4


def _hm(ts: str) -> str:
    return str(ts).replace("T", " ")[11:16]


def _day(ts: str) -> str:
    return str(ts).replace("T", " ")[:10]


def tape_hm(ts: str, minutes: int) -> str:
    """Option-tape minute for a bar. 5m buckets fill at the last minute."""
    hm = _hm(ts)
    if minutes <= 1:
        return hm
    end = _hm_to_min(hm) + minutes - 1
    return f"{end // 60:02d}:{end % 60:02d}"


def _walk_option(by_day, entry, field, letter, until, target, stop):
    """Long premium. Opposite letter or lost confluence exits; same-letter reprint does not."""
    px0 = entry.get(field)
    if px0 is None or px0 <= 0:
        return None
    day = entry["date"]
    last = None
    for t in range(_hm_to_min(entry["hm"]) + 1, _hm_to_min(until) + 1):
        hm = f"{t // 60:02d}:{t % 60:02d}"
        row = by_day[day].get(hm)
        if not row or row.get(field) is None:
            continue
        px = row[field]
        last = (px, hm)
        pct = (px - px0) / px0
        if target is not None and pct >= target:
            return px, hm, "target"
        if stop is not None and pct <= stop:
            return px, hm, "stop"
        sig = row.get("combo_signal")
        if sig in ("B", "S") and sig != letter:
            return px, hm, "flip"
        if row.get("combo_stamped") and row.get("combo_side") is None:
            return px, hm, "confluence"
    end = by_day[day].get(until)
    if end and end.get(field) is not None:
        return end[field], until, "time"
    if last is not None:
        return last[0], last[1], "tape_end"
    return None


def run_combo(bars, by_day, *, minutes=1, hold=HOLD_MIN, target=TARGET, stop=STOP, max_day=MAX_DAY):
    session = [b for b in bars if "09:15" <= _hm(str(b.get("t") or "")) <= "15:29"]
    tf = aggregate_bars(session, minutes) if minutes > 1 else session
    rows = confluence_rows(tf)
    if by_day:
        for day_map in by_day.values():
            for row in day_map.values():
                row.pop("combo_signal", None)
                row.pop("combo_side", None)
                row.pop("combo_stamped", None)
        for r in rows:
            d, hm = _day(r["t"]), tape_hm(r["t"], minutes)
            if d in by_day and hm in by_day[d]:
                by_day[d][hm]["combo_signal"] = r.get("signal")
                by_day[d][hm]["combo_side"] = r.get("side")
                by_day[d][hm]["combo_stamped"] = True
    trades = []
    for day in sorted({_day(r["t"]) for r in rows}):
        day_rows = [r for r in rows if _day(r["t"]) == day]
        entries = 0
        i = 0
        cool_until = -1
        while i < len(day_rows) and entries < max_day:
            r = day_rows[i]
            hm = tape_hm(r["t"], minutes)
            sig = r.get("signal")
            if not sig or hm < AFTER or hm > BEFORE:
                i += 1
                continue
            if _hm_to_min(hm) < cool_until:
                i += 1
                continue
            field = "ce" if sig == "B" else "pe"
            tape = (by_day.get(day) or {}).get(hm)
            if not tape or not tape.get(field):
                i += 1
                continue
            until_min = min(_hm_to_min(hm) + hold, _hm_to_min(UNTIL))
            until = f"{until_min // 60:02d}:{until_min % 60:02d}"
            walked = _walk_option(by_day, {"date": day, "hm": hm, field: tape[field]}, field, sig, until, target, stop)
            if walked is None:
                i += 1
                continue
            px1, ehm, reason = walked
            trades.append(
                _trade(
                    tape[field],
                    px1,
                    1,
                    QTY,
                    1,
                    reason,
                    {
                        "date": day,
                        "entry": hm,
                        "exit": ehm,
                        "px0": tape[field],
                        "px1": px1,
                        "side": field,
                        "signal": sig,
                    },
                )
            )
            entries += 1
            # Paper reverses on opposite letter in the same closed minute (no cooldown).
            if (
                reason == "flip"
                and entries < max_day
                and AFTER <= ehm <= BEFORE
            ):
                opp_sig = "S" if sig == "B" else "B"
                opp_field = "pe" if opp_sig == "S" else "ce"
                opp_tape = (by_day.get(day) or {}).get(ehm)
                if opp_tape and opp_tape.get(opp_field):
                    until_min = min(_hm_to_min(ehm) + hold, _hm_to_min(UNTIL))
                    until = f"{until_min // 60:02d}:{until_min % 60:02d}"
                    rev = _walk_option(
                        by_day,
                        {"date": day, "hm": ehm, opp_field: opp_tape[opp_field]},
                        opp_field,
                        opp_sig,
                        until,
                        target,
                        stop,
                    )
                    if rev is not None:
                        rpx, rehm, rreason = rev
                        trades.append(
                            _trade(
                                opp_tape[opp_field],
                                rpx,
                                1,
                                QTY,
                                1,
                                rreason,
                                {
                                    "date": day,
                                    "entry": ehm,
                                    "exit": rehm,
                                    "px0": opp_tape[opp_field],
                                    "px1": rpx,
                                    "side": opp_field,
                                    "signal": opp_sig,
                                },
                            )
                        )
                        entries += 1
                        ehm = rehm
            cool_until = _hm_to_min(ehm) + COOLDOWN
            i += 1
            while i < len(day_rows) and tape_hm(day_rows[i]["t"], minutes) <= ehm:
                i += 1
    return trades


def main() -> int:
    rec = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else record_dir(ROOT / "data")
    bars_path = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else ROOT / "data" / "minute_bars.json"
    builder = load_bars(bars_path, NIFTY_SYMBOL)
    bars = [dict(b) for b in builder.bars]
    opt_rows = load_minute_rows(rec) if rec.is_dir() else []
    by_day = _index_by_day(opt_rows)
    print(f"bars={len(bars)} rec_min={len(opt_rows)} days={sorted({_day(b['t']) for b in bars})}")
    ideas = [
        ("1m paper 8/-6 12m 4/d", dict(minutes=1)),
        ("5m paper 8/-6 12m 4/d", dict(minutes=5)),
    ]
    print(f"{'idea':<24} {'n':>4} {'W':>3} {'win%':>6} {'pnl':>9} {'avg':>7} {'dd':>8}")
    for name, kw in ideas:
        trades = run_combo(bars, by_day, **kw)
        s = _settle(trades, exclude=("tape_end",))
        print(
            f"{name:<24} {s['n']:4} {s.get('wins', 0):3} {s.get('win_pct', 0):5.1f}% "
            f"{s['pnl']:9.0f} {s.get('avg', 0):7.0f} {s.get('max_dd', 0):8.0f}"
        )
        reasons: dict[str, int] = {}
        for t in trades:
            if t.get("reason") == "tape_end":
                continue
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        print(f"  reasons {reasons}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
