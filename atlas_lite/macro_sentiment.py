"""Timestamped macro / sentiment snapshot (Yahoo quotes + optional RSS news).

The LLM must only *read* these fields — never invent oil/gold/FX/news.
"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from atlas_lite.log_util import get_logger

IST = ZoneInfo("Asia/Kolkata")
log = get_logger("atlas_lite.macro_sentiment")

CACHE_TTL_S = float(os.environ.get("ATLAS_LITE_MACRO_TTL_S", "120") or 120)
HTTP_TIMEOUT_S = 3.0

# Yahoo finance symbols
SYMBOLS: dict[str, str] = {
    "crude_wti": "CL=F",
    "crude_brent": "BZ=F",
    "gold": "GC=F",
    "usdinr": "INR=X",
    "es_futures": "ES=F",
    "nq_futures": "NQ=F",
    "india_vix": "^INDIAVIX",
}

NEWS_KEYWORDS = (
    "india",
    "nifty",
    "sensex",
    "rbi",
    "fed",
    "oil",
    "crude",
    "gold",
    "geopolit",
    "war",
    "inflation",
    "rate",
)

DEFAULT_NEWS_RSS = "https://feeds.reuters.com/reuters/businessNews"

_cache: dict[str, Any] = {"ts": 0.0, "snapshot": None}


def agent_news_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_AGENT_NEWS", "0").strip().lower()
    return raw in ("1", "true", "yes")


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


def _day_pct(last: float | None, prev: float | None) -> float | None:
    if last is None or prev is None or prev == 0:
        return None
    return round((last - prev) / prev * 100.0, 4)


def _quote_field(
    *,
    last: float | None,
    prev_close: float | None,
    as_of: str | None,
    error: str | None = None,
    stale: bool = False,
) -> dict[str, Any]:
    return {
        "value": last,
        "prev_close": prev_close,
        "day_pct": _day_pct(last, prev_close),
        "as_of": as_of,
        "stale": bool(stale),
        "error": error,
    }


def _parse_yahoo_result(result: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    last = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose")
    if prev is None:
        prev = meta.get("previousClose")
    ts = meta.get("regularMarketTime")
    as_of = None
    if isinstance(ts, (int, float)):
        as_of = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(IST).isoformat()
    try:
        last_f = float(last) if last is not None else None
    except (TypeError, ValueError):
        last_f = None
    try:
        prev_f = float(prev) if prev is not None else None
    except (TypeError, ValueError):
        prev_f = None
    return last_f, prev_f, as_of


def _fetch_yahoo_symbol(client: httpx.Client, symbol: str) -> dict[str, Any]:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol, safe='')}?interval=1d&range=2d"
    )
    try:
        resp = client.get(url, headers={"User-Agent": "Mozilla/5.0 AtlasLite/1.0"})
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return _quote_field(last=None, prev_close=None, as_of=None, error=str(exc)[:160])
    chart = payload.get("chart") if isinstance(payload, dict) else None
    results = (chart or {}).get("result") if isinstance(chart, dict) else None
    if not results or not isinstance(results[0], dict):
        err = None
        if isinstance(chart, dict) and chart.get("error"):
            err = str(chart.get("error"))[:160]
        return _quote_field(last=None, prev_close=None, as_of=None, error=err or "no_result")
    last, prev, as_of = _parse_yahoo_result(results[0])
    if last is None:
        return _quote_field(last=None, prev_close=prev, as_of=as_of, error="no_price")
    return _quote_field(last=last, prev_close=prev, as_of=as_of or _now_iso(), error=None)


def _headline_relevant(title: str) -> bool:
    low = title.lower()
    return any(k in low for k in NEWS_KEYWORDS)


def _fetch_news_rss(client: httpx.Client) -> list[dict[str, Any]]:
    if not agent_news_enabled():
        return []
    url = str(os.environ.get("ATLAS_LITE_AGENT_NEWS_RSS") or DEFAULT_NEWS_RSS).strip()
    if not url:
        return []
    try:
        resp = client.get(url, headers={"User-Agent": "Mozilla/5.0 AtlasLite/1.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as exc:  # noqa: BLE001
        log.warning("macro news RSS failed: %s", exc)
        return []
    items: list[dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title or not _headline_relevant(title):
            continue
        items.append(
            {
                "title": title[:200],
                "source": (item.findtext("source") or "rss").strip()[:80] or "rss",
                "published_at": (item.findtext("pubDate") or "").strip()[:64] or None,
            }
        )
        if len(items) >= 5:
            break
    return items


def _mark_stale(field: dict[str, Any], *, max_age_s: float) -> dict[str, Any]:
    out = dict(field)
    as_of = out.get("as_of")
    if not as_of or out.get("value") is None:
        out["stale"] = True if out.get("value") is None else bool(out.get("stale"))
        return out
    try:
        dt = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        age = (datetime.now(IST) - dt.astimezone(IST)).total_seconds()
        # Daily Yahoo closes can be hours old outside US hours — allow up to 36h
        # for futures/FX day_pct usefulness; still flag extreme age.
        out["stale"] = age > max(max_age_s, 36 * 3600)
        out["age_s"] = round(age, 1)
    except ValueError:
        out["stale"] = True
    return out


def fetch_macro_snapshot(*, force: bool = False) -> dict[str, Any]:
    """Return cached or fresh macro snapshot. Never invents prices."""
    now = time.monotonic()
    cached = _cache.get("snapshot")
    if (
        not force
        and cached is not None
        and (now - float(_cache.get("ts") or 0.0)) < CACHE_TTL_S
    ):
        return cached

    fetched_at = _now_iso()
    fields: dict[str, Any] = {}
    headlines: list[dict[str, Any]] = []
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
            for key, symbol in SYMBOLS.items():
                fields[key] = _mark_stale(
                    _fetch_yahoo_symbol(client, symbol),
                    max_age_s=CACHE_TTL_S,
                )
            headlines = _fetch_news_rss(client)
    except Exception as exc:  # noqa: BLE001
        log.warning("macro snapshot failed: %s", exc)
        for key in SYMBOLS:
            fields.setdefault(
                key,
                _quote_field(last=None, prev_close=None, as_of=None, error=str(exc)[:160]),
            )

    # Prefer WTI as primary crude; keep brent alongside
    crude = fields.get("crude_wti") or fields.get("crude_brent")
    snapshot = {
        "ok": True,
        "fetched_at": fetched_at,
        "ttl_s": CACHE_TTL_S,
        "news_enabled": agent_news_enabled(),
        "crude": crude,
        "crude_wti": fields.get("crude_wti"),
        "crude_brent": fields.get("crude_brent"),
        "gold": fields.get("gold"),
        "usdinr": fields.get("usdinr"),
        "es_futures": fields.get("es_futures"),
        "nq_futures": fields.get("nq_futures"),
        "india_vix": fields.get("india_vix"),
        "headlines": headlines,
    }
    _cache["ts"] = now
    _cache["snapshot"] = snapshot
    return snapshot


def clear_macro_cache() -> None:
    _cache["ts"] = 0.0
    _cache["snapshot"] = None
