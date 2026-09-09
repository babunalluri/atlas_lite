"""IST timestamp logging for Kite fetch tracing."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_CONFIGURED = False


class IstFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, IST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def ist_now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.environ.get("ATLAS_LITE_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        IstFormatter("%(asctime)s IST | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S.%f")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def log_tick(logger: logging.Logger, *, source: str, symbol: str, row: dict) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    ltp = row.get("last_price") or row.get("ltp")
    oi = row.get("oi") or row.get("open_interest")
    parts = [f"source={source}", f"symbol={symbol}", f"ltp={ltp}"]
    if oi is not None:
        parts.append(f"oi={oi}")
    ohlc = row.get("ohlc")
    if isinstance(ohlc, dict) and ohlc.get("close") is not None:
        parts.append(f"prev_close={ohlc['close']}")
    greeks = row.get("greeks")
    if isinstance(greeks, dict) and greeks.get("iv") is not None:
        parts.append(f"iv={greeks['iv']}")
    logger.info("KITE %s", " ".join(str(p) for p in parts))


def log_feed_snapshot(logger: logging.Logger, feed: dict) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    keys = (
        "adx",
        "atr",
        "index_nifty_chg",
        "index_banknifty_chg",
        "index_sensex_chg",
        "vix_chg",
        "pcr",
        "ivp",
        "ce",
        "pe",
        "iv",
        "iv_day_high",
        "iv_day_low",
        "iv_vs_day_low",
        "oi_pct_chg",
        "oi_vs_day_high",
        "fut_oi",
        "fut_oi_day_high",
        "ce_oi",
        "pe_oi",
        "max_pain",
        "nifty_ltp",
        "atm",
    )
    parts = [f"{k}={feed[k]}" for k in keys if feed.get(k) is not None]
    logger.info("SHEET @ %s | %s", ist_now(), " | ".join(parts))
