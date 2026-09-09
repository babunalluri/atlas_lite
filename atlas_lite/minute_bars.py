"""Build 1-minute OHLC bars from live WS ticks for ADX (no ongoing REST)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MAX_BARS = 2000
# NSE cash equity session (IST). Index 1m ADX must ignore pre/post ticks.
CASH_SESSION_START = (9, 15)
CASH_SESSION_END = (15, 29)  # last closed 1m bar starts 15:29


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


def is_cash_session_minute(ts: str) -> bool:
    """True for NSE cash minutes YYYY-MM-DD HH:MM in [09:15, 15:29]."""
    raw = str(ts).replace("T", " ")[:16]
    if len(raw) < 16:
        return False
    try:
        hour = int(raw[11:13])
        minute = int(raw[14:16])
    except ValueError:
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
        bar = {
            "t": self._current_key,
            "o": round(float(self._open or self._close), 4),
            "h": round(float(self._high or self._close), 4),
            "l": round(float(self._low or self._close), 4),
            "c": round(float(self._close), 4),
            "v": round(float(self._volume), 4),
            "oi": round(float(self._oi), 4),
        }
        if self.bars and self.bars[-1].get("t") == bar["t"]:
            self.bars[-1] = bar
        else:
            self.bars.append(bar)
        if len(self.bars) > MAX_BARS:
            self.bars = self.bars[-MAX_BARS:]

    def drop_non_session_bars(self) -> int:
        """Remove bars outside NSE cash session. Returns count removed."""
        before = len(self.bars)
        self.bars = [b for b in self.bars if is_cash_session_minute(str(b.get("t") or ""))]
        return before - len(self.bars)

    def load_candles(self, candles: list[list[Any]]) -> None:
        for c in candles:
            if not isinstance(c, (list, tuple)) or len(c) < 5:
                continue
            ts = str(c[0])[:16].replace("T", " ")
            if not is_cash_session_minute(ts):
                continue
            self.bars.append(
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
        self._dedupe()
        self.trim_incomplete_current_bar()
        if len(self.bars) > MAX_BARS:
            self.bars = self.bars[-MAX_BARS:]

    def trim_incomplete_current_bar(self) -> bool:
        """Drop the last bar if it is the current IST minute (Kite REST includes it in-progress)."""
        if not self.bars:
            return False
        current = _minute_key()
        if str(self.bars[-1].get("t") or "") == current:
            self.bars.pop()
            return True
        return False

    def last_bar_minute(self) -> str | None:
        if not self.bars:
            return None
        return str(self.bars[-1].get("t") or "") or None

    def _dedupe(self) -> None:
        seen: dict[str, dict[str, Any]] = {}
        for bar in self.bars:
            key = str(bar.get("t") or "")
            if key:
                seen[key] = bar
        self.bars = [seen[k] for k in sorted(seen.keys())]

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
            bar = {
                "t": ts,
                "o": round(float(c[1]), 4),
                "h": round(float(c[2]), 4),
                "l": round(float(c[3]), 4),
                "c": round(float(c[4]), 4),
                "v": round(_candle_volume(c), 4),
                "oi": round(_candle_oi(c), 4),
            }
            replaced = False
            for i, existing in enumerate(self.bars):
                if str(existing.get("t") or "") == ts:
                    self.bars[i] = bar
                    replaced = True
                    updated += 1
                    break
            if not replaced:
                self.bars.append(bar)
                updated += 1
        if updated:
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
        if (
            self._current_key
            and self._close is not None
            and is_cash_session_minute(self._current_key)
        ):
            live = {
                "t": self._current_key,
                "o": round(float(self._open or self._close), 4),
                "h": round(float(self._high or self._close), 4),
                "l": round(float(self._low or self._close), 4),
                "c": round(float(self._close), 4),
                "v": round(float(self._volume), 4),
                "oi": round(float(self._oi), 4),
            }
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
