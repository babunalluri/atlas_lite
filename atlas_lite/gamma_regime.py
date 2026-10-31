"""Dealer-gamma regime for NIFTY from NSE's previous-day open interest. Logging only.

GEX = sum(call OI x gamma) - sum(put OI x gamma), Black-76 gamma at the current spot with
India VIX as sigma, expiries up to 40 days out (US convention: dealers long calls, short
puts). Positive means dealers are long gamma: their hedging sells rallies and buys dips.

On the Kite year (200 non-expiry days, Oct 2025 - Oct 2026) the 13:00-15:14 NIFTY range was
109 pts after a positive 13:00 reading and 139 after a negative one (t = -2.8), about 16%
below what VIX implied vs about equal to it. Scalper v3 logs the reading with each entry so
its paper results can be split by regime on real premiums before any filter is used.
"""

from __future__ import annotations

import csv
import io
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from atlas_lite.agent_gates import _is_nse_trading_day
from atlas_lite.nse_fo_bhav import _f, _parse_expiry, _s, fetch_fo_bhav_csv

MAX_DAYS_TO_EXPIRY = 40
CACHE_DIR = "gamma_oi"
KEEP_FILES = 10

OiRow = tuple[str, int, str, float]  # expiry ISO, strike, CE|PE, open interest


def parse_nifty_oi(csv_text: str, day: date) -> list[OiRow]:
    """NIFTY option open interest by strike for expiries within MAX_DAYS_TO_EXPIRY of ``day``."""
    rows: list[OiRow] = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        if _s(row, "TckrSymb", "SYMBOL") != "NIFTY":
            continue
        opt = _s(row, "OptnTp", "OPTION_TYP").upper()
        if opt not in ("CE", "PE"):
            continue
        exp = _parse_expiry(_s(row, "XpryDt", "EXPIRY_DT"))
        strike = _f(row, "StrkPric", "STRIKE_PR")
        oi = _f(row, "OpnIntrst", "OPEN_INT")
        if exp is None or strike is None or not oi or exp < day or (exp - day).days > MAX_DAYS_TO_EXPIRY:
            continue
        rows.append((exp.isoformat(), int(round(strike)), opt, float(oi)))
    return rows


def black76_gamma(forward: float, strike: float, sigma: float, years: float) -> float:
    if years <= 0 or sigma <= 0 or forward <= 0 or strike <= 0:
        return 0.0
    vol = sigma * math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol) / vol
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi) / (forward * vol)


def gamma_reading(rows: list[OiRow], *, spot: float, vix: float, now: datetime, oi_day: str) -> dict[str, Any] | None:
    """Net and total dealer gamma (rupees per 1% move per unit of OI) at ``spot``."""
    if not rows or spot <= 0 or vix <= 0:
        return None
    sigma = vix / 100.0
    net = total = 0.0
    for exp, strike, opt, oi in rows:
        expires = datetime.fromisoformat(f"{exp}T15:30:00").replace(tzinfo=now.tzinfo)
        years = (expires - now).total_seconds() / (365.0 * 86400.0)
        if years <= 0:
            continue
        g = oi * black76_gamma(spot, strike, sigma, years) * spot * spot * 0.01
        net += g if opt == "CE" else -g
        total += g
    if total <= 0:
        return None
    return {
        "regime": "long" if net > 0 else "short",
        "share": round(net / total, 3),
        "net": round(net),
        "oi_day": oi_day,
        "spot": round(spot, 2),
        "vix": round(vix, 2),
    }


def previous_trading_day(day: date) -> date:
    cur = day - timedelta(days=1)
    while not _is_nse_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


class GammaOi:
    """Previous trading day's NIFTY open interest, one small JSON per day under data/gamma_oi/."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / CACHE_DIR
        self.day: str = ""
        self.rows: list[OiRow] = []

    def _path(self, day: date) -> Path:
        return self.dir / f"{day.isoformat()}.json"

    async def ensure(self, today: date, client: httpx.AsyncClient | None = None) -> bool:
        """Load (or download once) the open interest of the session before ``today``."""
        prev = previous_trading_day(today)
        if self.day == prev.isoformat() and self.rows:
            return True
        path = self._path(prev)
        if path.is_file():
            try:
                self.rows = [tuple(r) for r in json.loads(path.read_text(encoding="utf-8"))]  # type: ignore[misc]
                self.day = prev.isoformat()
                return bool(self.rows)
            except (OSError, ValueError):
                pass
        own = client is None
        client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        try:
            text = await fetch_fo_bhav_csv(client, prev, None, persist=False)
        finally:
            if own:
                await client.aclose()
        rows = parse_nifty_oi(text, prev) if text else []
        if not rows:
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows), encoding="utf-8")
        for old in sorted(self.dir.glob("*.json"))[:-KEEP_FILES]:
            old.unlink(missing_ok=True)
        self.rows, self.day = rows, prev.isoformat()
        return True

    def reading(self, *, spot: float | None, vix: float | None, now: datetime) -> dict[str, Any] | None:
        if not self.rows or spot is None or vix is None:
            return None
        return gamma_reading(self.rows, spot=float(spot), vix=float(vix), now=now, oi_day=self.day)
