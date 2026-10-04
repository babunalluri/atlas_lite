"""Paper-only NIFTY VWAP long book (never Kite orders).

Separate from the Rich-IV iron fly ledger. Rules (paper timeframe = 5m bars):

* Session-anchored **equal-weight** VWAP with ±1σ / ±2σ bands (IST cash day).
* Long only: **B** = tag **−0.5σ** and close back **above VWAP**; **S** = take
  at **+1σ**, stop at **−1σ**, or Supertrend flip. Qty **1** (no scale-out).
* Trend gate: **Supertrend(10,3) up** and close above VWAP (same 5m series).
* Entries **09:30–14:00 IST** (max **2**/day); hard flat by **15:15 IST**.
* Size: **1 NIFTY unit** at spot (P&L ₹1 per index point) — paper only.
* Chart interval is independent; this book always aggregates Kite 1m → 5m.
* Retired (off by default): OCI live ledger has 0 fills. Re-enable with
  ``ATLAS_LITE_PAPER_VWAP=1``.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from atlas_lite.kite_charges import KITE_GST, KITE_SEBI, order_charges
from atlas_lite.log_util import get_logger, ist_now
from atlas_lite.minute_bars import hm_ge, hm_lt, is_cash_session_minute

IST = ZoneInfo("Asia/Kolkata")

STRATEGY = "nifty_vwap_long"
CAPITAL = 200_000.0
# 1 NIFTY "stock"/unit at spot — P&L = points × qty.
QTY = 1
SQUARE_OFF = (15, 15)
# Skip the first 15m so session bands settle; still allow until 14:00.
ENTRY_AFTER = (9, 30)
# No new entries near square-off (matches iron-fly cutoff style).
ENTRY_UNTIL = (14, 0)
MIN_BARS_FOR_SIGNAL = 5
# Reject collapsed bands (σ≈0) so first-volume / flat prints cannot fake B/S or stops.
MIN_SIGMA_PTS = 1.0
# Pullback depth for long B: 0.5 = halfway to −1σ (more fills than full −1σ).
PULLBACK_SIGMA_FRAC = 0.5
# Paper signal timeframe (Kite feed stays 1m; we aggregate here).
PAPER_BAR_MINUTES = 5
# ST defaults on the paper timeframe.
ST_PERIOD = 10
ST_MULT = 3.0
# Equity intraday-ish friction (Zerodha: brokerage ₹20 or 0.03%, whichever lower).
BROKERAGE_CAP = 20.0
BROKERAGE_RATE = 0.0003  # 0.03% of turnover
STT_SELL = 0.00025  # 0.025% of sell turnover (equity intraday sell)
TXN = 0.0000345
STAMP_BUY = 0.00003
# Session VWAP is always equal-weight typical-price (index 1m volume is unreliable).
MAX_ENTRIES_PER_DAY = 2
# Minimum points of room to +1σ at entry. Charge-equivalent pts (~25–30 on
# this ₹1/point book) were used as the gate and blocked every live setup.
MIN_EDGE_BUFFER_PTS = 5.0


def paper_vwap_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_PAPER_VWAP", "0").strip().lower()
    return raw in ("1", "true", "yes")


def in_vwap_entry_window(now: datetime) -> bool:
    return hm_ge(now, ENTRY_AFTER) and hm_lt(now, ENTRY_UNTIL)


def spot_order_charges(price: float, qty: int, side: str) -> dict[str, float]:
    """Approx Zerodha equity intraday charges for one fill (shared Kite waterfall)."""
    turnover = max(0.0, float(price) * int(qty))
    brokerage = min(BROKERAGE_CAP, round(turnover * BROKERAGE_RATE, 2))
    return order_charges(
        turnover,
        side=side,
        brokerage=brokerage,
        txn_rate=TXN,
        stt_sell_rate=STT_SELL,
        stamp_buy_rate=STAMP_BUY,
        sebi_rate=KITE_SEBI,
        gst_rate=KITE_GST,
    )


def _bar_day(ts: str) -> str:
    return str(ts).replace("T", " ")[:10]


def _session_bars(bars: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    """Today's cash-session bars — walk from the tail (series is chronological)."""
    out: list[dict[str, Any]] = []
    for b in reversed(bars):
        t = str(b.get("t") or "")
        d = _bar_day(t)
        if d > day:
            continue
        if d < day:
            break
        if is_cash_session_minute(t):
            out.append(b)
    out.reverse()
    return out


def bucket_floor_ts(ts: str | datetime, minutes: int) -> str:
    """IST clock bucket start as `YYYY-MM-DD HH:MM` (e.g. 5 → 09:15, 09:20, …)."""
    if isinstance(ts, datetime):
        day = ts.strftime("%Y-%m-%d")
        hh, mm = ts.hour, ts.minute
    else:
        raw = str(ts).replace("T", " ")
        day = raw[:10]
        try:
            hh = int(raw[11:13])
            mm = int(raw[14:16])
        except (TypeError, ValueError, IndexError):
            return raw[:16]
    if minutes <= 1:
        return f"{day} {hh:02d}:{mm:02d}"
    total = hh * 60 + mm
    floored = (total // minutes) * minutes
    bh, bm = divmod(floored, 60)
    return f"{day} {bh:02d}:{bm:02d}"


def aggregate_bars(
    bars: list[dict[str, Any]],
    minutes: int,
) -> list[dict[str, Any]]:
    """Roll 1m OHLC into `minutes` buckets (IST label floor)."""
    if minutes <= 1:
        return [dict(b) for b in bars]
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    cur_key = ""
    for b in bars:
        t = str(b.get("t") or "")
        if not t:
            continue
        key = bucket_floor_ts(t, minutes)
        high = float(b["h"])
        low = float(b["l"])
        close = float(b["c"])
        vol = float(b.get("v") or 0.0)
        if cur is None or key != cur_key:
            cur = {
                "t": key,
                "o": float(b.get("o") if b.get("o") is not None else close),
                "h": high,
                "l": low,
                "c": close,
                "v": vol,
            }
            if b.get("oi") is not None:
                cur["oi"] = b.get("oi")
            out.append(cur)
            cur_key = key
        else:
            cur["h"] = max(float(cur["h"]), high)
            cur["l"] = min(float(cur["l"]), low)
            cur["c"] = close
            cur["v"] = float(cur.get("v") or 0.0) + vol
            if b.get("oi") is not None:
                cur["oi"] = b.get("oi")
    return out


def _session_vwap_stats(
    *,
    eq_sum: float,
    eq_sum_sq: float,
    eq_n: int,
) -> tuple[float, float]:
    """Equal-weight session VWAP + σ from typical price (volume path dropped)."""
    if eq_n <= 0:
        return 0.0, 0.0
    vwap = eq_sum / eq_n
    variance = max(0.0, eq_sum_sq / eq_n - vwap * vwap)
    return vwap, math.sqrt(variance)


class _SupertrendState:
    """Incremental chart ST(period, mult): push bars in order, one row per bar.

    ATR matches chart calcAtrSeries (SMA seed over the first `period` true
    ranges, then RMA). Copy the state before pushing a forming bar so the
    closed-bar prefix can be reused tick after tick.
    """

    __slots__ = (
        "_seed",
        "atr",
        "direction",
        "final_lower",
        "final_upper",
        "mult",
        "n",
        "period",
        "prev_close",
    )

    def __init__(self, period: int = ST_PERIOD, mult: float = ST_MULT) -> None:
        self.period = int(period)
        self.mult = float(mult)
        self.n = 0
        self.prev_close: float | None = None
        self.atr: float | None = None
        self._seed: list[float] = []
        self.final_upper: float | None = None
        self.final_lower: float | None = None
        self.direction = 1

    def copy(self) -> _SupertrendState:
        other = _SupertrendState.__new__(_SupertrendState)
        for name in self.__slots__:
            setattr(other, name, getattr(self, name))
        other._seed = list(self._seed)
        return other

    def push(self, bar: dict[str, Any]) -> dict[str, Any]:
        t = str(bar.get("t") or "")
        high = float(bar["h"])
        low = float(bar["l"])
        close = float(bar["c"])
        i = self.n
        self.n += 1
        prev_close = self.prev_close
        self.prev_close = close
        if i == 0 or prev_close is None:
            return {"t": t, "dir": None, "up": None, "dn": None, "ready": False}
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        if i <= self.period:
            self._seed.append(tr)
            atr = sum(self._seed) / len(self._seed)
        else:
            atr = (float(self.atr) * (self.period - 1) + tr) / self.period
        self.atr = atr
        mid = (high + low) / 2.0
        basic_upper = mid + self.mult * atr
        basic_lower = mid - self.mult * atr
        if self.final_upper is None or self.final_lower is None:
            self.final_upper = basic_upper
            self.final_lower = basic_lower
        else:
            if basic_upper < self.final_upper or prev_close > self.final_upper:
                self.final_upper = basic_upper
            if basic_lower > self.final_lower or prev_close < self.final_lower:
                self.final_lower = basic_lower
        if self.direction == 1:
            self.direction = -1 if close < self.final_lower else 1
        else:
            self.direction = 1 if close > self.final_upper else -1
        return {
            "t": t,
            "dir": self.direction,
            "up": self.final_lower if self.direction == 1 else None,
            "dn": self.final_upper if self.direction == -1 else None,
            "ready": True,
        }


def supertrend_series(
    bars: list[dict[str, Any]],
    *,
    period: int = ST_PERIOD,
    mult: float = ST_MULT,
) -> list[dict[str, Any]]:
    """Chart ST(10,3) state per bar. dir=+1 uptrend (line below price)."""
    st = _SupertrendState(period, mult)
    return [st.push(b) for b in bars]


def _series_identity(bars: list[dict[str, Any]]) -> tuple[Any, ...]:
    """Cheap O(n) identity for a closed-bar prefix (catches mid-series Kite fixes)."""
    if not bars:
        return (0,)
    c_sum = 0.0
    range_sum = 0.0
    for b in bars:
        c_sum += float(b["c"])
        range_sum += float(b["h"]) - float(b["l"])
    return (
        len(bars),
        str(bars[0].get("t") or ""),
        str(bars[-1].get("t") or ""),
        round(c_sum, 4),
        round(range_sum, 4),
    )


def scan_vwap_signals(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match portal Session VWAP + B/S state machine (long + short marks).

    Paper book only *acts* on long entries (B without exit/stop) and S exits.
    """
    day = ""
    eq_sum = eq_sum_sq = 0.0
    eq_n = 0
    long_open = False
    short_open = False
    rows: list[dict[str, Any]] = []
    for b in bars:
        t = str(b.get("t") or "")
        key = _bar_day(t)
        if key != day:
            day = key
            eq_sum = eq_sum_sq = 0.0
            eq_n = 0
            long_open = False
            short_open = False
        high = float(b["h"])
        low = float(b["l"])
        close = float(b["c"])
        typical = (high + low + close) / 3.0
        eq_sum += typical
        eq_sum_sq += typical * typical
        eq_n += 1
        vwap, sigma = _session_vwap_stats(
            eq_sum=eq_sum,
            eq_sum_sq=eq_sum_sq,
            eq_n=eq_n,
        )
        up1 = vwap + sigma
        dn1 = vwap - sigma
        up2 = vwap + 2 * sigma
        dn2 = vwap - 2 * sigma
        pull_long = vwap - PULLBACK_SIGMA_FRAC * sigma
        pull_short = vwap + PULLBACK_SIGMA_FRAC * sigma
        bands_ready = sigma >= MIN_SIGMA_PTS and eq_n >= MIN_BARS_FOR_SIGNAL

        buy = sell = exit_ = stop = False
        signal: str | None = None
        bias_long = close > vwap
        bias_short = close < vwap

        if bands_ready:
            if long_open:
                if low <= dn1:
                    stop = sell = True
                    signal = "S"
                    long_open = False
                elif high >= up1:
                    # Take profit at +1σ (not VWAP — that made ±1σ exits unreachable).
                    exit_ = sell = True
                    signal = "S"
                    long_open = False
            elif short_open:
                if high >= up2:
                    stop = buy = True
                    signal = "B"
                    short_open = False
                elif low <= dn1:
                    exit_ = buy = True
                    signal = "B"
                    short_open = False
            elif bias_long and low <= pull_long and close > vwap and close < up1:
                # Milder pullback (−0.5σ) vs full −1σ for more actionable longs.
                buy = True
                signal = "B"
                long_open = True
            elif bias_short and high >= pull_short and close < vwap and close > dn1:
                sell = True
                signal = "S"
                short_open = True

        rows.append(
            {
                "t": t,
                "vwap": vwap,
                "sigma": sigma,
                "up1": up1,
                "dn1": dn1,
                "up2": up2,
                "dn2": dn2,
                "pull_long": pull_long,
                "pull_short": pull_short,
                "bands_ready": bands_ready,
                "buy": buy,
                "sell": sell,
                "exit": exit_,
                "stop": stop,
                "signal": signal,
                "bias": "long" if bias_long else ("short" if bias_short else "flat"),
                # Long exit only from the long-open branch (exit_/stop). Plain short
                # entry also sets sell+S and must not count as a long flat signal.
                "long_entry": bool(buy and signal == "B" and not exit_ and not stop),
                "long_exit": bool(sell and signal == "S" and (exit_ or stop)),
                # Geometry-only long setup (ignores scan long_open). Paper uses this
                # so an ST-rejected B cannot leave a phantom open that blocks later entries.
                "long_setup": bool(
                    bands_ready
                    and bias_long
                    and low <= pull_long
                    and close > vwap
                    and close < up1
                ),
            }
        )
    return rows


@dataclass
class VwapPosition:
    day: str
    entry: float
    qty: int
    opened_at: str
    charges_open: float
    signal_t: str | None = None
    # Entry-bucket H/L at the moment of fill (persisted on the open row). The
    # closed entry bar's extremes include pre-fill wicks, so only a print
    # beyond these counts as post-fill risk — restart-safe by construction.
    fill_bar_l: float | None = None
    fill_bar_h: float | None = None


@dataclass
class PaperVwapLong:
    path: Path
    qty: int = QTY
    capital: float = CAPITAL
    require_st: bool = True
    bar_minutes: int = PAPER_BAR_MINUTES
    require_edge: bool = True
    max_entries_per_day: int = MAX_ENTRIES_PER_DAY
    position: VwapPosition | None = None
    traded_day: str = ""
    entries_today: int = 0
    day_pnl: float = 0.0
    eod_written: bool = False
    _last_entry_t: str | None = field(default=None, repr=False)
    _last_exit_t: str | None = field(default=None, repr=False)
    # (closed-prefix identity, Supertrend state after that prefix).
    _st_cache: tuple[Any, ...] | None = field(default=None, repr=False)
    _log: Any = field(default_factory=lambda: get_logger("atlas_lite.paper_vwap"), repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restore()

    def _restore(self) -> None:
        if not self.path.is_file():
            return
        last_open: dict[str, Any] | None = None
        day_pnl = 0.0
        entries = 0
        day = ""
        eod_written = False
        last_exit_t: str | None = None
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("strategy") != STRATEGY:
                continue
            kind = ev.get("event")
            d = str(ev.get("day") or "")
            if d and d != day:
                day = d
                day_pnl = 0.0
                entries = 0
                last_open = None
                eod_written = False
                last_exit_t = None
            if kind == "open":
                last_open = ev
                entries += 1
            elif kind == "close":
                last_open = None
                if ev.get("day_pnl") is not None:
                    day_pnl = float(ev["day_pnl"])
                if ev.get("signal_t"):
                    last_exit_t = str(ev["signal_t"])
            elif kind == "day_pnl":
                if ev.get("day_pnl") is not None:
                    day_pnl = float(ev["day_pnl"])
                eod_written = True
        self.traded_day = day
        self.entries_today = entries
        self.day_pnl = round(day_pnl, 2)
        # Keep sealed flag for the ledger's last day even across calendar rollover so
        # Monday restart does not append another Friday day_pnl (straddle-style).
        self.eod_written = bool(eod_written)
        today = ist_now()[:10]
        # Restore any unclosed open (including prior session) so we can flatten.
        if last_open:
            self.position = VwapPosition(
                day=str(last_open["day"]),
                entry=float(last_open["entry"]),
                qty=int(last_open.get("qty") or self.qty),
                opened_at=str(last_open.get("ts") or ""),
                charges_open=float(last_open.get("charges") or 0.0),
                signal_t=last_open.get("signal_t"),
                fill_bar_l=float(last_open["fill_bar_l"]) if last_open.get("fill_bar_l") is not None else None,
                fill_bar_h=float(last_open["fill_bar_h"]) if last_open.get("fill_bar_h") is not None else None,
            )
            if str(last_open.get("day")) == today:
                self._last_entry_t = last_open.get("signal_t")
        elif day == today:
            self._last_exit_t = last_exit_t

    def _append(self, event: dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("mode", "paper")
        event.setdefault("strategy", STRATEGY)
        event.setdefault("logged_at", ist_now())
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")

    def snapshot(self, *, spot: float | None) -> dict[str, Any]:
        open_pnl = 0.0
        open_gross = 0.0
        charges = 0.0
        pos_body = None
        if self.position is not None:
            pos_body = {
                "day": self.position.day,
                "strategy": STRATEGY,
                "entry": self.position.entry,
                "qty": self.position.qty,
                "opened_at": self.position.opened_at,
                "charges_open": self.position.charges_open,
                "signal_t": self.position.signal_t,
            }
            charges = float(self.position.charges_open)
            if spot is not None:
                open_gross = round((float(spot) - self.position.entry) * self.position.qty, 2)
                close_ch = spot_order_charges(spot, self.position.qty, "sell")["total"]
                charges = round(self.position.charges_open + close_ch, 2)
                open_pnl = round(open_gross - charges, 2)
        mtm = round(self.day_pnl + open_pnl, 2)
        equity = round(self.capital + mtm, 2)
        return {
            "ok": True,
            "mode": "paper",
            "book": STRATEGY,
            "live_orders": False,
            "qty": self.qty,
            "capital": self.capital,
            "traded_day": self.traded_day,
            "entries_today": self.entries_today,
            "day_pnl": round(self.day_pnl, 2),
            "day_pnl_pct": round(self.day_pnl / self.capital * 100.0, 4) if self.capital else 0.0,
            "open_pnl": open_pnl,
            "open_pnl_gross": open_gross,
            "charges": charges,
            "mtm_pnl": mtm,
            "equity": equity,
            "position": pos_body,
            "spot_missing": bool(self.position is not None and spot is None),
            "square_off": f"{SQUARE_OFF[0]:02d}:{SQUARE_OFF[1]:02d}",
            "entry_window": (
                f"{ENTRY_AFTER[0]:02d}:{ENTRY_AFTER[1]:02d}"
                f"-{ENTRY_UNTIL[0]:02d}:{ENTRY_UNTIL[1]:02d}"
            ),
            "bar_minutes": int(self.bar_minutes),
            "max_entries_per_day": int(self.max_entries_per_day),
        }

    def _roll_to_day(self, day: str) -> None:
        """Start a fresh accounting day (call only when flat)."""
        self.traded_day = day
        self.entries_today = 0
        self.day_pnl = 0.0
        self.eod_written = False
        self._last_entry_t = None
        self._last_exit_t = None

    def _seal_traded_day(
        self, now: datetime, *, allow_open: bool = False
    ) -> dict[str, Any] | None:
        """Write day_pnl for the current traded_day rollup if anything happened."""
        return self._write_eod_if_needed(
            now, day=self.traded_day or None, allow_open=allow_open
        )

    def _flatten_prior_session(
        self,
        now: datetime,
        *,
        bars: list[dict[str, Any]],
        spot: float | None,
        calendar_day: str,
    ) -> dict[str, Any] | None:
        """Close a book from a prior IST day and seal that day's P&L before rolling."""
        pos = self.position
        if pos is None or pos.day == calendar_day:
            return None
        # Prefer the prior session's last close — today's live spot books gap P&L
        # into yesterday's sealed day when history is present.
        prior = _session_bars(bars, pos.day)
        if prior:
            px = float(prior[-1]["c"])
            if self.traded_day != pos.day:
                self.traded_day = pos.day
            closed = self._close(now, float(px), reason="session")
            sealed = self._seal_traded_day(now)
            self._roll_to_day(calendar_day)
            return closed or sealed

        # History gap (process down past Kite window): do not stamp today's spot
        # onto a day that already closed — book onto calendar_day instead.
        if spot is None:
            return None
        self._log.warning(
            "PAPER VWAP flatten without prior-session bars; "
            "booking session close on calendar_day=%s (position day=%s) spot=%.2f",
            calendar_day,
            pos.day,
            float(spot),
        )
        if self.traded_day and self.traded_day != calendar_day:
            # Position still open — must allow seal or prior-day day_pnl is lost on roll.
            self._seal_traded_day(now, allow_open=True)
        # Persist the close first; `_close` rolls the accounting day only once
        # the row is durable, so a failed append leaves this path retryable.
        return self._close(now, float(spot), reason="session", book_day=calendar_day)

    def _tf_bars(self, bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return aggregate_bars(bars, int(self.bar_minutes))

    def _forming_bucket(self, now: datetime) -> str:
        return bucket_floor_ts(now, int(self.bar_minutes))

    def _st_dir_for_bar(
        self,
        *,
        bars: list[dict[str, Any]],
        session: list[dict[str, Any]],
        sig_t: str,
    ) -> int | None:
        series = bars or session
        if not series:
            return None
        tail = series[-1]
        if str(tail.get("t") or "") != sig_t:
            # Signal bar is not the tail — full scan (rare; keeps prior semantics).
            latest: int | None = None
            for row in supertrend_series(series):
                if not row.get("ready") or row.get("dir") is None:
                    continue
                latest = int(row["dir"])
                if str(row.get("t") or "") == sig_t:
                    return latest
            return latest
        # Closed prefix changes once per bucket; only the forming bar moves per tick.
        closed = series[:-1]
        key = _series_identity(closed)
        cached = self._st_cache
        if cached is None or cached[0] != key:
            st = _SupertrendState()
            for b in closed:
                st.push(b)
            cached = (key, st)
            self._st_cache = cached
        row = cached[1].copy().push(tail)
        if not row.get("ready") or row.get("dir") is None:
            return None
        return int(row["dir"])

    @staticmethod
    def _entry_bar_risk(
        pos: VwapPosition,
        bar: dict[str, Any],
        *,
        stop_px: float,
        up1: float,
    ) -> tuple[bool, bool, float]:
        """Stop/take on the entry bucket from printed OHLC alone.

        The bucket's H/L include the pre-fill pullback wick, so a low counts
        only if it undercuts the low seen at fill (a new post-fill print); same
        for highs. The close is always post-fill. Fill extremes are persisted
        on the open row, so this holds across a restart.
        """
        low = float(bar["l"])
        high = float(bar["h"])
        close = float(bar["c"])
        new_low = pos.fill_bar_l is not None and low < float(pos.fill_bar_l)
        new_high = pos.fill_bar_h is not None and high > float(pos.fill_bar_h)
        stop = close <= stop_px or (new_low and low <= stop_px)
        take = (
            not stop
            and up1 > float(pos.entry)
            and (close >= up1 or (new_high and high >= up1))
        )
        fill = stop_px if stop else (up1 if take else 0.0)
        return stop, take, fill

    def _manage_open_risk(
        self,
        now: datetime,
        session: list[dict[str, Any]],
        *,
        spot: float | None,
        bars: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Stop/take/ST-flip for an open book — not gated on bands_ready / spot.

        Walks closed session bars after the entry bar so a gap or restart still
        honors a wick that pierced −1σ / +1σ while we were offline. The forming
        bucket prefers live spot; if LTP is missing, falls back to printed bar
        extremes for stops so an entry-bucket outage cannot skip risk.
        """
        pos = self.position
        if pos is None or not session:
            return None
        rows = scan_vwap_signals(session)
        entry_t = str(pos.signal_t or "")
        forming_t = self._forming_bucket(now)
        st_bars = bars or session
        if (
            session
            and st_bars
            and str(st_bars[-1].get("t") or "")
            == str(session[-1].get("t") or "")
        ):
            st_bars = st_bars[:-1] + [session[-1]]
        for idx, (row, bar) in enumerate(zip(rows, session)):
            sig_t = str(row.get("t") or "")
            if entry_t and sig_t and sig_t < entry_t:
                continue
            on_entry_bar = bool(entry_t and sig_t and sig_t == entry_t)
            stop_px = float(row["dn1"])
            up1 = float(row["up1"])
            is_forming = idx == len(session) - 1 and sig_t[:16] == forming_t
            if is_forming and spot is not None:
                px = float(spot)
                stop = px <= stop_px
                # Allow take on the forming entry bucket (live +1σ).
                take = px >= up1 and up1 > float(pos.entry)
                fill = min(px, stop_px) if stop else max(px, up1)
            elif on_entry_bar:
                # Entry bucket without a live tick (LTP gap, or already closed):
                # only prints beyond the fill-time extremes are post-fill.
                stop, take, fill = self._entry_bar_risk(
                    pos, bar, stop_px=stop_px, up1=up1
                )
            elif is_forming:
                # LTP gap on a later bucket — stop from printed low only (no take
                # without live px).
                stop = float(bar["l"]) <= stop_px
                take = False
                fill = stop_px if stop else 0.0
            else:
                low = float(bar["l"])
                high = float(bar["h"])
                stop = low <= stop_px
                take = high >= up1 and up1 > float(pos.entry)
                fill = stop_px if stop else up1
            reason = None
            if stop:
                reason = "stop"
            elif take:
                reason = "signal_s"
            elif self.require_st and not on_entry_bar:
                st_dir = self._st_dir_for_bar(
                    bars=st_bars, session=session, sig_t=sig_t
                )
                if st_dir == -1:
                    reason = "st_flip"
                    if is_forming and spot is not None:
                        fill = float(spot)
                    else:
                        fill = float(bar["c"])
            if reason is None:
                continue
            closed = self._close(
                now, float(fill), reason=reason, signal_t=sig_t or None
            )
            if closed is not None:
                self._last_exit_t = sig_t or self._last_exit_t
            return closed
        return None

    def on_bars(
        self,
        *,
        now: datetime,
        bars: list[dict[str, Any]],
        spot: float | None,
    ) -> dict[str, Any] | None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)
        else:
            now = now.astimezone(IST)
        day = now.strftime("%Y-%m-%d")

        # Weekend: only flatten a stale open book (feed loop backs off after).
        if now.weekday() >= 5:
            if self.position is not None:
                return self._flatten_prior_session(
                    now, bars=bars, spot=spot, calendar_day=day
                )
            return None

        # Flatten anything carried from a prior session before new signals.
        if self.position is not None and self.position.day != day:
            return self._flatten_prior_session(
                now, bars=bars, spot=spot, calendar_day=day
            )

        if self.traded_day != day and self.position is None:
            # Seal a prior flat day that never got an EOD row (crash / early stop).
            if self.traded_day and (self.entries_today or self.day_pnl):
                self._seal_traded_day(now)
            self._roll_to_day(day)

        session = self._tf_bars(_session_bars(bars, day))
        forming_t = self._forming_bucket(now)
        if spot is not None and session:
            last_t = str(session[-1].get("t") or "")[:16]
            # Only paint live spot onto the *current* TF bucket — never contaminate
            # the prior bucket when the series has not advanced yet.
            if last_t == forming_t:
                last = dict(session[-1])
                last["c"] = float(spot)
                last["h"] = max(float(last["h"]), float(spot))
                last["l"] = min(float(last["l"]), float(spot))
                session = session[:-1] + [last]

        if hm_ge(now, SQUARE_OFF):
            closed = None
            px = spot
            if px is None and session:
                px = float(session[-1]["c"])
            if self.position is not None and px is not None:
                closed = self._close(now, float(px), reason="time")
            # Only stamp EOD once flat (missing spot with an open book waits).
            eod = None if self.position is not None else self._write_eod_if_needed(now)
            return closed or eod

        # Risk first: open books must stop/take even when spot is missing, σ is
        # soft, or the session is still warming up for new entries.
        if self.position is not None:
            hit = self._manage_open_risk(
                now, session, spot=spot, bars=self._tf_bars(bars)
            )
            if hit is not None:
                return hit
            return None

        if len(session) < MIN_BARS_FOR_SIGNAL or spot is None:
            return None

        rows = scan_vwap_signals(session)
        last = rows[-1]
        sig_t = str(last.get("t") or "")
        if not last.get("bands_ready"):
            return None

        # Use geometry-only setup — not scan long_entry — so ST rejects cannot
        # desync the chart state machine into a phantom long_open.
        if last.get("long_setup") and in_vwap_entry_window(now):
            # Cheap same-bar dedupe before Supertrend over full history.
            if sig_t and (
                sig_t == self._last_entry_t or sig_t == self._last_exit_t
            ):
                return None
            if self.entries_today >= int(self.max_entries_per_day):
                return None
            if self.require_st:
                # Paint the current TF bucket onto the ST series so dir matches
                # the same bar VWAP setup sees (spot-adjusted H/L/C).
                st_bars = self._tf_bars(bars)
                if (
                    session
                    and st_bars
                    and str(st_bars[-1].get("t") or "")
                    == str(session[-1].get("t") or "")
                ):
                    st_bars = st_bars[:-1] + [session[-1]]
                st_dir = self._st_dir_for_bar(
                    bars=st_bars, session=session, sig_t=sig_t
                )
                if st_dir != 1:
                    return None
            up1 = float(last.get("up1") or 0)
            # Refuse already-at-target / tiny-headroom entries. Charges still
            # hit the ledger; they are not the entry distance (σ is usually
            # smaller than equity-style round-trip pts on qty=1).
            if float(spot) >= up1:
                return None
            if self.require_edge and (up1 - float(spot)) < MIN_EDGE_BUFFER_PTS:
                return None
            opened = self._open(
                now, float(spot), signal_t=sig_t, row=last, bar=session[-1]
            )
            self._last_entry_t = sig_t or self._last_entry_t
            return opened

        return None

    def _open(
        self,
        now: datetime,
        spot: float,
        *,
        signal_t: str,
        row: dict[str, Any],
        bar: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        qty = int(self.qty)
        ch = spot_order_charges(spot, qty, "buy")
        ts = now.isoformat()
        day = now.strftime("%Y-%m-%d")
        entry = round(spot, 4)
        charges_open = float(ch["total"])
        entry_n = self.entries_today + 1
        # Entry-bucket extremes at fill (spot-painted) — see _entry_bar_risk.
        # Unrounded on purpose: a rounded copy would read the same printed
        # wick as a "new" extreme once the bucket closes.
        fill_bar_l = min(float(bar["l"]), spot) if bar else spot
        fill_bar_h = max(float(bar["h"]), spot) if bar else spot
        event = {
            "event": "open",
            "ts": ts,
            "day": day,
            "entry": entry,
            "qty": qty,
            "charges": charges_open,
            "signal_t": signal_t,
            "fill_bar_l": fill_bar_l,
            "fill_bar_h": fill_bar_h,
            "vwap": round(float(row.get("vwap") or 0), 4),
            "dn1": round(float(row.get("dn1") or 0), 4),
            "pull_long": round(float(row.get("pull_long") or 0), 4),
            "bias": row.get("bias"),
            "entry_n": entry_n,
            "st_required": self.require_st,
        }
        # Persist first — if append fails, memory stays flat (no ghost open).
        self._append(event)
        self.position = VwapPosition(
            day=day,
            entry=entry,
            qty=qty,
            opened_at=ts,
            charges_open=charges_open,
            signal_t=signal_t,
            fill_bar_l=fill_bar_l,
            fill_bar_h=fill_bar_h,
        )
        self.traded_day = day
        self.entries_today = entry_n
        self._log.info(
            "PAPER VWAP LONG open spot=%.2f qty=%s charges=%.2f signal_t=%s",
            spot,
            qty,
            charges_open,
            signal_t,
        )
        return event

    def _close(
        self,
        now: datetime,
        spot: float,
        *,
        reason: str,
        signal_t: str | None = None,
        book_day: str | None = None,
    ) -> dict[str, Any] | None:
        """Close the open book. `book_day` books the P&L onto a different
        accounting day (history-gap flatten); the day roll happens only after
        the close row is on disk."""
        pos = self.position
        if pos is None:
            return None
        qty = int(pos.qty)
        close_ch = spot_order_charges(spot, qty, "sell")
        gross = round((float(spot) - pos.entry) * qty, 2)
        charges = round(pos.charges_open + float(close_ch["total"]), 2)
        pnl = round(gross - charges, 2)
        book_day = book_day or pos.day
        rolling = book_day != (self.traded_day or pos.day)
        new_day_pnl = round((0.0 if rolling else self.day_pnl) + pnl, 2)
        ts = now.isoformat()
        event = {
            "event": "close",
            "ts": ts,
            "day": book_day,
            "reason": reason,
            "entry": pos.entry,
            "exit": round(float(spot), 4),
            "qty": qty,
            "pnl_gross": gross,
            "charges_open": pos.charges_open,
            "charges_close": float(close_ch["total"]),
            "charges": charges,
            "pnl": pnl,
            "pnl_known": True,
            "opened_at": pos.opened_at,
            "signal_t": signal_t or pos.signal_t,
            "day_pnl": new_day_pnl,
            "day_pnl_pct": round(new_day_pnl / self.capital * 100.0, 4) if self.capital else 0.0,
            "capital": self.capital,
            "equity": round(self.capital + new_day_pnl, 2),
        }
        # Persist first — if append fails, keep the open book in memory.
        self._append(event)
        if rolling:
            self._roll_to_day(book_day)
        self.position = None
        self.day_pnl = new_day_pnl
        self._log.info(
            "PAPER VWAP LONG close reason=%s entry=%.2f exit=%.2f net=%.2f day_pnl=%.2f",
            reason,
            event["entry"],
            event["exit"],
            pnl,
            new_day_pnl,
        )
        return event

    def _write_eod_if_needed(
        self,
        now: datetime,
        *,
        day: str | None = None,
        allow_open: bool = False,
    ) -> dict[str, Any] | None:
        if self.eod_written:
            return None
        if self.position is not None and not allow_open:
            return None
        stamp_day = day or self.traded_day or now.strftime("%Y-%m-%d")
        # Holidays / idle days: suppress a zero-trade day_pnl row (and stop retrying).
        if self.entries_today == 0 and self.day_pnl == 0.0:
            self.eod_written = True
            return None
        event = {
            "event": "day_pnl",
            "ts": now.isoformat(),
            "day": stamp_day,
            "capital": self.capital,
            "trades": self.entries_today,
            "day_pnl": round(self.day_pnl, 2),
            "day_pnl_pct": round(self.day_pnl / self.capital * 100.0, 4) if self.capital else 0.0,
            "equity": round(self.capital + self.day_pnl, 2),
        }
        self._append(event)
        self.eod_written = True
        return event
