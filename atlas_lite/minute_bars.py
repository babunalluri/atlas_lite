"""Build 1-minute OHLC bars from live WS ticks for ADX (no ongoing REST)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MAX_BARS = 2000
# NSE cash equity session (IST). Index 1m ADX must ignore pre/post ticks.
CASH_SESSION_START = (9, 15)
CASH_SESSION_END = (15, 29)  # last closed 1m bar starts 15:29


def hm_ge(now: datetime, hhmm: tuple[int, int]) -> bool:
    return (now.hour, now.minute) >= hhmm


def hm_lt(now: datetime, hhmm: tuple[int, int]) -> bool:
    return (now.hour, now.minute) < hhmm


def hm_le(now: datetime, hhmm: tuple[int, int]) -> bool:
    return (now.hour, now.minute) <= hhmm


def bar_tip(bar: dict[str, Any] | None) -> str:
    """Compact OHLC fingerprint for paper/VWAP cache keys."""
    if not bar:
        return ""
    return (
        f"{bar.get('o')}|{bar.get('h')}|{bar.get('l')}|{bar.get('c')}|"
        f"{bar.get('v')}|{bar.get('oi')}"
    )


def _candle_volume(candle: list[Any] | tuple[Any, ...]) -> float:
    if len(candle) < 6:
        return 0.0
    try:
        return float(candle[5] or 0)
    except (TypeError, ValueError):
        return 0.0


def _candle_oi(candle: list[Any] | tuple[Any, ...]) -> float:
    if len(candle) < 7:
        return 0.0
    try:
        return float(candle[6] or 0)
    except (TypeError, ValueError):
        return 0.0


def _minute_key(when: datetime | None = None) -> str:
    now = when or datetime.now(IST)
    return now.strftime("%Y-%m-%d %H:%M")


def kite_adx_window_start(when: datetime | None = None, *, days: int = 5) -> str:
    """Earliest IST minute kept for ADX — matches Kite historical ``from`` date.

    ``days`` is calendar days (see feed_engine.ADX_REST_DAYS). Default 5 so a
    weekend + holiday still retains prior cash-session bars for Wilder warmup.
    """
    now = when or datetime.now(IST)
    return (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")[:16]


def bars_from_kite_candles(
    candles: list[list[Any]],
    *,
    include_forming: bool = True,
) -> list[dict[str, Any]]:
    """Build session 1m bars from Kite REST candles (ADX/chart authority)."""
    current = _minute_key()
    by_ts: dict[str, dict[str, Any]] = {}
    for c in candles:
        if not isinstance(c, (list, tuple)) or len(c) < 5:
            continue
        ts = str(c[0])[:16].replace("T", " ")
        if not is_cash_session_minute(ts):
            continue
        if ts > current:
            continue
        if not include_forming and ts >= current:
            continue
        by_ts[ts] = {
            "t": ts,
            "o": round(float(c[1]), 4),
            "h": round(float(c[2]), 4),
            "l": round(float(c[3]), 4),
            "c": round(float(c[4]), 4),
            "v": round(_candle_volume(c), 4),
            "oi": round(_candle_oi(c), 4),
        }
    return [by_ts[ts] for ts in sorted(by_ts)]


def ohlc_from_bar_dicts(
    bars: list[dict[str, Any]],
) -> tuple[list[float], list[float], list[float]]:
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for bar in bars:
        highs.append(float(bar["h"]))
        lows.append(float(bar["l"]))
        closes.append(float(bar["c"]))
    return highs, lows, closes


def is_cash_session_minute(ts: str) -> bool:
    """True for NSE cash minutes YYYY-MM-DD HH:MM in [09:15, 15:29] on weekdays."""
    raw = str(ts).replace("T", " ")[:16]
    if len(raw) < 16:
        return False
    try:
        day = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        hour = int(raw[11:13])
        minute = int(raw[14:16])
    except ValueError:
        return False
    if day.weekday() >= 5:
        return False
    start_m = CASH_SESSION_START[0] * 60 + CASH_SESSION_START[1]
    end_m = CASH_SESSION_END[0] * 60 + CASH_SESSION_END[1]
    now_m = hour * 60 + minute
    return start_m <= now_m <= end_m


def in_adx_seed_window(when: datetime) -> bool:
    """True when a Kite 1m historical fetch can still produce today's cash bars.

    Weekends and off-session (overnight / pre-open / after 15:29) return False so
    we do not stamp a day-latch at 02:00 and then skip the 09:15 seed.
    """
    if when.weekday() >= 5:
        return False
    start_m = CASH_SESSION_START[0] * 60 + CASH_SESSION_START[1]
    end_m = CASH_SESSION_END[0] * 60 + CASH_SESSION_END[1]
    now_m = when.hour * 60 + when.minute
    return start_m <= now_m <= end_m


@dataclass
class MinuteBarBuilder:
    symbol: str
    bars: list[dict[str, Any]] = field(default_factory=list)
    _current_key: str | None = field(default=None, repr=False)
    _open: float | None = field(default=None, repr=False)
    _high: float | None = field(default=None, repr=False)
    _low: float | None = field(default=None, repr=False)
    _close: float | None = field(default=None, repr=False)
    _volume: float = field(default=0.0, repr=False)
    _session_vol: float | None = field(default=None, repr=False)
    _oi: float = field(default=0.0, repr=False)
    # Bumped when closed-bar history mutates (not on live LTP ticks). Every
    # write to `bars` goes through `_set_bars` / `_touch` so the fingerprint
    # cannot miss a mutation that leaves len/last/tip unchanged.
    _rev: int = field(default=0, repr=False)

    def _touch(self) -> None:
        """Closed-bar history changed in place — invalidate fingerprint consumers."""
        self._rev += 1

    def _set_bars(self, new_bars: list[dict[str, Any]]) -> None:
        """Replace the closed-bar list, bumping `_rev` only on a real change."""
        if new_bars != self.bars:
            self.bars = new_bars
            self._rev += 1

    def _cap(self) -> None:
        if len(self.bars) > MAX_BARS:
            self._set_bars(self.bars[-MAX_BARS:])

    def ingest(self, ltp: float | None, ohlc: dict[str, Any] | None = None) -> bool:
        """Update current minute bar from tick LTP. Returns True if a bar was finalized.

        Note: Kite WS ohlc on index ticks is the *session* OHLC, not the current 1m bar —
        only LTP must be used for intrabar high/low.
        """
        price = ltp
        if price is None and isinstance(ohlc, dict):
            price = ohlc.get("close") or ohlc.get("open")
        if price is None:
            return False
        price = float(price)
        key = _minute_key()
        finalized = False
        if self._current_key is not None and key != self._current_key:
            self._finalize_current()
            finalized = True
        if self._current_key != key:
            self._current_key = key
            self._open = self._high = self._low = self._close = price
            self._volume = 0.0
        else:
            self._close = price
            self._high = max(self._high or price, price)
            self._low = min(self._low or price, price)
        return finalized

    def ingest_volume(self, session_volume: float | None) -> None:
        """Accumulate NIFTY FUT session volume into the in-progress 1m bar."""
        if session_volume is None or self._current_key is None:
            return
        vol = float(session_volume)
        prev = self._session_vol
        self._session_vol = vol
        if prev is None or vol < prev:
            return
        self._volume += vol - prev

    def ingest_oi(self, oi: float | None) -> None:
        """Store latest NIFTY FUT open interest on the in-progress 1m bar."""
        if oi is None or self._current_key is None:
            return
        try:
            self._oi = float(oi)
        except (TypeError, ValueError):
            return

    def _finalize_current(self) -> None:
        if self._current_key is None or self._close is None:
            return
        # Drop pre-open / after-hours minutes — they skew Wilder ADX vs Kite chart.
        if not is_cash_session_minute(self._current_key):
            self._current_key = None
            self._open = self._high = self._low = self._close = None
            self._volume = 0.0
            self._session_vol = None
            self._oi = 0.0
            return
        bar = self._live_bar()
        assert bar is not None  # session + OHLC already gated above
        if self.bars and self.bars[-1].get("t") == bar["t"]:
            self.bars[-1] = bar
        else:
            self.bars.append(bar)
        self._touch()
        self._cap()

    def _live_bar(self) -> dict[str, Any] | None:
        """In-progress session minute as an OHLC dict, or None."""
        if (
            self._current_key is None
            or self._close is None
            or not is_cash_session_minute(self._current_key)
        ):
            return None
        return {
            "t": self._current_key,
            "o": round(float(self._open or self._close), 4),
            "h": round(float(self._high or self._close), 4),
            "l": round(float(self._low or self._close), 4),
            "c": round(float(self._close), 4),
            "v": round(float(self._volume), 4),
            "oi": round(float(self._oi), 4),
        }

    def drop_non_session_bars(self) -> int:
        """Remove bars outside NSE cash session. Returns count removed."""
        before = len(self.bars)
        self._set_bars(
            [b for b in self.bars if is_cash_session_minute(str(b.get("t") or ""))]
        )
        return before - len(self.bars)

    def drop_bars_before(self, minute_key: str) -> int:
        """Drop closed bars strictly before ``minute_key`` (Kite window floor)."""
        floor = str(minute_key).replace("T", " ")[:16]
        before = len(self.bars)
        self._set_bars([b for b in self.bars if str(b.get("t") or "") >= floor])
        removed = before - len(self.bars)
        if removed:
            self._cap()
        return removed

    def sync_closed_bars_from_kite(
        self,
        candles: list[list[Any]],
        *,
        window_floor: str,
    ) -> int:
        """Closed bars in [window_floor, now) must match Kite candles exactly."""
        current = _minute_key()
        floor = str(window_floor).replace("T", " ")[:16]
        kite_by_ts: dict[str, dict[str, Any]] = {}
        for c in candles:
            if not isinstance(c, (list, tuple)) or len(c) < 5:
                continue
            ts = str(c[0])[:16].replace("T", " ")
            if ts >= current or ts < floor or not is_cash_session_minute(ts):
                continue
            kite_by_ts[ts] = {
                "t": ts,
                "o": round(float(c[1]), 4),
                "h": round(float(c[2]), 4),
                "l": round(float(c[3]), 4),
                "c": round(float(c[4]), 4),
                "v": round(_candle_volume(c), 4),
                "oi": round(_candle_oi(c), 4),
            }
        if not kite_by_ts:
            return 0
        before = len(self.bars)
        self._set_bars([kite_by_ts[ts] for ts in sorted(kite_by_ts)])
        self._cap()
        return abs(before - len(self.bars))

    def drop_closed_bars_not_in_kite(
        self,
        candles: list[list[Any]],
        *,
        range_from: str,
    ) -> int:
        """Drop closed bars in [range_from, now) that Kite did not return (tail reconcile)."""
        current = _minute_key()
        start = str(range_from).replace("T", " ")[:16]
        allowed: set[str] = set()
        for c in candles:
            if not isinstance(c, (list, tuple)) or len(c) < 5:
                continue
            ts = str(c[0])[:16].replace("T", " ")
            if ts >= current or ts < start or not is_cash_session_minute(ts):
                continue
            allowed.add(ts)
        if not allowed:
            return 0
        before = len(self.bars)
        self._set_bars(
            [
                b
                for b in self.bars
                if str(b.get("t") or "") < start
                or str(b.get("t") or "") in allowed
            ]
        )
        return before - len(self.bars)

    def load_candles(self, candles: list[list[Any]]) -> None:
        added: list[dict[str, Any]] = []
        for c in candles:
            if not isinstance(c, (list, tuple)) or len(c) < 5:
                continue
            ts = str(c[0])[:16].replace("T", " ")
            if not is_cash_session_minute(ts):
                continue
            added.append(
                {
                    "t": ts,
                    "o": round(float(c[1]), 4),
                    "h": round(float(c[2]), 4),
                    "l": round(float(c[3]), 4),
                    "c": round(float(c[4]), 4),
                    "v": round(_candle_volume(c), 4),
                    "oi": round(_candle_oi(c), 4),
                }
            )
        if added:
            self._set_bars(self.bars + added)
        self._dedupe()
        self.trim_incomplete_current_bar()
        self._cap()

    def trim_incomplete_current_bar(self) -> bool:
        """Drop the last bar if it is the current IST minute (Kite REST includes it in-progress)."""
        if not self.bars:
            return False
        current = _minute_key()
        if str(self.bars[-1].get("t") or "") == current:
            self.bars.pop()
            self._touch()
            return True
        return False

    def last_bar_minute(self) -> str | None:
        if not self.bars:
            return None
        return str(self.bars[-1].get("t") or "") or None

    def chart_series_fingerprint(self) -> str:
        """Cheap identity for paper VWAP cache (no full bar copy).

        Includes last closed OHLC so same-shape Kite corrections invalidate the key.
        """
        n = len(self.bars)
        last = self.last_bar_minute() or ""
        tip = ""
        if self.bars:
            tip = bar_tip(self.bars[-1])
        live = self._live_bar()
        if live is not None:
            if live["t"] != last:
                n += 1
            last = str(live["t"])
            # Keep tip on last *closed* OHLC only — live LTP would bust the cache every tick.
        return f"{last}|{n}|{tip}|{self._rev}"

    def forming_or_last_bar(self) -> dict[str, Any] | None:
        """Live forming minute, else last closed bar — no full series copy."""
        live = self._live_bar()
        if live is not None:
            return live
        if not self.bars:
            return None
        return dict(self.bars[-1])

    def _dedupe(self) -> None:
        seen: dict[str, dict[str, Any]] = {}
        for bar in self.bars:
            key = str(bar.get("t") or "")
            if key:
                seen[key] = bar
        self._set_bars([seen[k] for k in sorted(seen.keys())])

    def merge_kite_candles(self, candles: list[list[Any]]) -> int:
        """Replace closed minute bars with Kite REST OHLC (authoritative vs tick build)."""
        current = _minute_key()
        updated = 0
        for c in candles:
            if not isinstance(c, (list, tuple)) or len(c) < 5:
                continue
            ts = str(c[0])[:16].replace("T", " ")
            if ts >= current:
                continue
            if not is_cash_session_minute(ts):
                continue
            vol = round(_candle_volume(c), 4)
            oi = round(_candle_oi(c), 4)
            replaced = False
            for i, existing in enumerate(self.bars):
                if str(existing.get("t") or "") == ts:
                    # Index candles carry no volume/OI — keep the live/FUT values.
                    if vol <= 0:
                        vol = round(float(existing.get("v") or 0), 4)
                    if oi <= 0:
                        oi = round(float(existing.get("oi") or 0), 4)
                    self.bars[i] = {
                        "t": ts,
                        "o": round(float(c[1]), 4),
                        "h": round(float(c[2]), 4),
                        "l": round(float(c[3]), 4),
                        "c": round(float(c[4]), 4),
                        "v": vol,
                        "oi": oi,
                    }
                    replaced = True
                    updated += 1
                    break
            if not replaced:
                self.bars.append(
                    {
                        "t": ts,
                        "o": round(float(c[1]), 4),
                        "h": round(float(c[2]), 4),
                        "l": round(float(c[3]), 4),
                        "c": round(float(c[4]), 4),
                        "v": vol,
                        "oi": oi,
                    }
                )
                updated += 1
        if updated:
            # In-place OHLC replace — `_set_bars` cannot see it, so touch explicitly.
            self._touch()
            self._dedupe()
            self.drop_non_session_bars()
        return updated

    def merge_volume_from_candles(self, candles: list[list[Any]]) -> int:
        """Fill missing/zero volume and OI from another Kite series (usually NIFTY FUT)."""
        by_vol: dict[str, float] = {}
        by_oi: dict[str, float] = {}
        for c in candles:
            if not isinstance(c, (list, tuple)) or len(c) < 6:
                continue
            ts = str(c[0])[:16].replace("T", " ")
            vol = _candle_volume(c)
            if vol > 0:
                by_vol[ts] = vol
            oi = _candle_oi(c)
            if oi > 0:
                by_oi[ts] = oi
        updated = 0
        for bar in self.bars:
            ts = str(bar.get("t") or "")
            vol = by_vol.get(ts)
            if vol is not None and float(bar.get("v") or 0) <= 0:
                bar["v"] = round(vol, 4)
                updated += 1
            oi = by_oi.get(ts)
            if oi is not None and float(bar.get("oi") or 0) <= 0:
                bar["oi"] = round(oi, 4)
                updated += 1
        if updated:
            self._touch()
        return updated

    def ohlc_series(
        self,
        *,
        closed_only: bool = False,
        session_only: bool = True,
    ) -> tuple[list[float], list[float], list[float]]:
        highs, lows, closes = [], [], []
        current = _minute_key() if closed_only else None
        for bar in self.bars:
            ts = str(bar.get("t") or "")
            if session_only and not is_cash_session_minute(ts):
                continue
            # Skip in-progress IST minute when closed_only (Kite closed-bar baseline).
            if closed_only and current and ts >= current:
                continue
            highs.append(float(bar["h"]))
            lows.append(float(bar["l"]))
            closes.append(float(bar["c"]))
        if (
            not closed_only
            and self._close is not None
            and self._current_key
            and (not session_only or is_cash_session_minute(self._current_key))
        ):
            highs.append(float(self._high or self._close))
            lows.append(float(self._low or self._close))
            closes.append(float(self._close))
        return highs, lows, closes

    def bar_count(self) -> int:
        return len(self.bars) + (1 if self._current_key else 0)

    def chart_bars(self) -> list[dict[str, Any]]:
        """Closed 1m bars plus the in-progress minute, for the NIFTY 50 chart."""
        bars = [dict(bar) for bar in self.bars]
        # Keep chart timeline aligned to NSE cash session; hide off-session
        # live bars (for example, stale WS ticks after market close).
        live = self._live_bar()
        if live is not None:
            if bars and bars[-1].get("t") == live["t"]:
                bars[-1] = live
            else:
                bars.append(live)
        return bars

    def to_json(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "bars": self.bars}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> MinuteBarBuilder:
        symbol = str(raw.get("symbol") or "")
        bars = raw.get("bars")
        builder = cls(symbol=symbol, bars=list(bars) if isinstance(bars, list) else [])
        builder._dedupe()
        return builder


def load_bars(path: Path, symbol: str) -> MinuteBarBuilder:
    if not path.is_file():
        return MinuteBarBuilder(symbol=symbol)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and str(raw.get("symbol") or "") == symbol:
            return MinuteBarBuilder.from_json(raw)
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
        pass
    return MinuteBarBuilder(symbol=symbol)


def save_bars(path: Path, builder: MinuteBarBuilder) -> None:
    path.write_text(json.dumps(builder.to_json(), indent=2), encoding="utf-8")
