"""Overnight / global cues for the LLM buyer: how US futures, Asian markets, crude and the rupee moved since their
previous close, read from Yahoo daily bars. Information only (no trading rule uses it); fields that can't be read are
left out, never guessed. GIFT Nifty is not available from this source; NIFTY's own gap (open vs previous close) is
computed by the LLM buyer from its feed.

The change is measured against the previous *daily close* (the last daily bar before the latest one when the latest
bar is from the current session), not Yahoo's chartPreviousClose, which for a multi-day range is the close before the
whole range."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from atlas_lite.log_util import get_logger

IST = ZoneInfo("Asia/Kolkata")
log = get_logger("atlas_lite.overnight_cues")
TIMEOUT_S = 4.0
CUES: dict[str, str] = {
    "us_sp500_futures_pct": "ES=F",
    "us_nasdaq_futures_pct": "NQ=F",
    "japan_nikkei_pct": "^N225",
    "hong_kong_hang_seng_pct": "^HSI",
    "crude_brent_pct": "BZ=F",
    "usd_inr_pct": "INR=X",
}


def _pct_since_prev_close(payload: dict[str, Any]) -> float | None:
    res = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(res, dict):
        return None
    meta = res.get("meta") or {}
    last, last_t = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    ts = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    bars = [(t, c) for t, c in zip(ts, closes) if c is not None]
    if last is None or not bars:
        return None
    day = lambda t: datetime.fromtimestamp(float(t), tz=timezone.utc).date()
    # The latest daily bar is the current session when it has the same date as the last trade: compare with the one before.
    prev = bars[-2][1] if (last_t is not None and len(bars) >= 2 and day(bars[-1][0]) == day(last_t)) else bars[-1][1]
    if not prev:
        return None
    return round((float(last) / float(prev) - 1) * 100, 2)


def fetch_overnight_cues() -> dict[str, Any]:
    """Blocking (run in a worker thread). {"as_of": "HH:MM", "date": ..., <cue>: pct, ...}; unreadable cues are left out."""
    out: dict[str, Any] = {}
    try:
        with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 AtlasLite/1.0"}) as client:
            for key, sym in CUES.items():
                try:
                    r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(sym, safe='')}"
                                   "?interval=1d&range=5d")
                    r.raise_for_status()
                    pct = _pct_since_prev_close(r.json())
                except Exception as exc:  # noqa: BLE001 - one bad symbol never blocks the others
                    log.warning("overnight cue %s failed: %s", sym, str(exc)[:120])
                    continue
                if pct is not None:
                    out[key] = pct
    except Exception as exc:  # noqa: BLE001
        log.warning("overnight cues failed: %s", exc)
    if out:
        now = datetime.now(IST)
        out["as_of"], out["date"] = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
    return out
