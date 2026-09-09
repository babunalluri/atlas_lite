"""NSE F&O bhavcopy — nearest-expiry NIFTY ATM IV from settlement prices."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from atlas_lite.metrics import EXPIRY_HHMM, IST, atm_iv_from_prices

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

UDIFF_URLS = (
    "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip",
    "https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip",
)


def _legacy_url(day: date) -> str:
    mon = day.strftime("%b").upper()
    name = f"fo{day.strftime('%d')}{mon}{day.year}bhav.csv.zip"
    return (
        f"https://archives.nseindia.com/content/historical/DERIVATIVES/"
        f"{day.year}/{mon}/{name}"
    )


def _f(row: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        raw = (row.get(key) or "").strip()
        if not raw:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return None


def _s(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        raw = (row.get(key) or "").strip()
        if raw:
            return raw
    return ""


def _parse_expiry(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_nifty_atm_iv(csv_text: str, trade_day: date) -> float | None:
    """Nearest weekly NIFTY ATM IV from one FO bhavcopy (UDiFF or legacy)."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return None
    ce_pe: dict[tuple[date, float, str], float] = {}
    spots: list[float] = []
    for row in reader:
        symbol = _s(row, "TckrSymb", "SYMBOL")
        if symbol != "NIFTY":
            continue
        opt = _s(row, "OptnTp", "OPTION_TYP").upper()
        if opt not in {"CE", "PE"}:
            continue
        inst = _s(row, "FinInstrmTp", "INSTRUMENT").upper()
        if inst and inst not in {"IDO", "OPTIDX"}:
            continue
        expiry = _parse_expiry(_s(row, "XpryDt", "EXPIRY_DT"))
        strike = _f(row, "StrkPric", "STRIKE_PR")
        if expiry is None or strike is None or expiry <= trade_day:
            continue
        px = _f(row, "LastPric", "ClsPric", "SttlmPric", "CLOSE", "SETTLE_PR")
        if px is None or px <= 0:
            continue
        ce_pe[(expiry, strike, opt)] = px
        und = _f(row, "UndrlygPric")
        if und is not None and und > 0:
            spots.append(und)
    if not ce_pe:
        return None
    expiries = sorted({exp for exp, _k, _o in ce_pe})
    expiry = expiries[0]
    if spots:
        spot = sum(spots) / len(spots)
    else:
        return None
    strike = float(int(round(spot / 50.0) * 50))
    ce = ce_pe.get((expiry, strike, "CE"))
    pe = ce_pe.get((expiry, strike, "PE"))
    if ce is None or pe is None:
        # nearest listed strike to ATM
        strikes = sorted({k for exp, k, _o in ce_pe if exp == expiry})
        if not strikes:
            return None
        strike = min(strikes, key=lambda k: abs(k - spot))
        ce = ce_pe.get((expiry, strike, "CE"))
        pe = ce_pe.get((expiry, strike, "PE"))
    eod = datetime.combine(trade_day, EXPIRY_HHMM, tzinfo=IST)
    return atm_iv_from_prices(ce, pe, spot, strike, expiry, now=eod)


def unzip_csv(payload: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), zf.namelist()[0])
        return zf.read(name).decode("utf-8", errors="replace")


def cache_path(cache_dir: Path, day: date) -> Path:
    return cache_dir / f"{day.strftime('%Y%m%d')}.csv"


def prune_fo_bhav_cache(cache_dir: Path | None, *, keep_days: int = 0) -> int:
    """Delete disposable FO bhav CSVs (ATM IV lives in iv_history.json).

    keep_days=0 removes every cached CSV. Positive keep_days retains the newest
    N weekday files by filename for short-term re-bootstrap debugging.
    """
    if cache_dir is None or not cache_dir.is_dir():
        return 0
    files = sorted(cache_dir.glob("*.csv"), key=lambda p: p.name, reverse=True)
    keep = max(0, int(keep_days))
    removed = 0
    for i, path in enumerate(files):
        if i < keep:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _drop_fo_bhav_day(cache_dir: Path | None, day: date) -> None:
    if cache_dir is None:
        return
    try:
        cache_path(cache_dir, day).unlink(missing_ok=True)
    except OSError:
        pass


async def fetch_fo_bhav_csv(
    client: httpx.AsyncClient,
    day: date,
    cache_dir: Path | None = None,
    *,
    persist: bool = True,
) -> str | None:
    """Fetch FO bhavcopy CSV text for ``day``.

    When ``persist`` is False, skip writing the multi-MB CSV to disk — callers that
    only need a parsed ATM IV should use this to avoid write-then-unlink churn.
    Existing cache files are still read when present.
    """
    if cache_dir is not None:
        cached = cache_path(cache_dir, day)
        if cached.is_file():
            return cached.read_text(encoding="utf-8")
    urls = [u.format(ymd=day.strftime("%Y%m%d")) for u in UDIFF_URLS]
    urls.append(_legacy_url(day))
    for url in urls:
        try:
            resp = await client.get(url, headers=NSE_HEADERS, follow_redirects=True)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200 or not resp.content:
            continue
        body = resp.content
        text = unzip_csv(body) if body[:2] == b"PK" else body.decode("utf-8", errors="replace")
        if persist and cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path(cache_dir, day).write_text(text, encoding="utf-8")
        return text
    return None


def weekday_range(end: date, *, trading_days: int) -> list[date]:
    out: list[date] = []
    cursor = end
    while len(out) < trading_days:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor -= timedelta(days=1)
    out.reverse()
    return out


async def fetch_nifty_atm_iv_series(
    *,
    days: int,
    end: date | None = None,
    cache_dir: Path | None = None,
    concurrency: int = 6,
) -> list[dict[str, Any]]:
    """Nearest-expiry NIFTY ATM IV for the last `days` weekdays (skips holidays/404)."""
    import asyncio

    end = end or datetime.now(IST).date()
    if datetime.now(IST).time() < time(15, 25):
        end = end - timedelta(days=1)
    wanted = weekday_range(end, trading_days=days + 20)
    sem = asyncio.Semaphore(concurrency)
    series: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:

        async def one(day: date) -> dict[str, Any] | None:
            async with sem:
                # Parse in memory — do not persist multi-MB CSVs for a single float.
                text = await fetch_fo_bhav_csv(client, day, cache_dir, persist=False)
            if not text:
                return None
            iv = parse_nifty_atm_iv(text, day)
            # Drop any leftover on-disk cache from older builds.
            _drop_fo_bhav_day(cache_dir, day)
            if iv is None:
                return None
            return {"day": day.isoformat(), "iv": round(float(iv), 2)}

        rows = await asyncio.gather(*(one(d) for d in wanted))
    prune_fo_bhav_cache(cache_dir, keep_days=0)
    for row in rows:
        if row is not None:
            series.append(row)
    series.sort(key=lambda item: str(item["day"]))
    if len(series) > days:
        series = series[-days:]
    return series
