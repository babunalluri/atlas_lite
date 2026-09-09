"""IV Percentile history — NIFTY ATM IV daily EOD + live ATM IV for today.

IVP compares today's live ATM IV (Kite greeks when present, else Black-76) against
~252 prior trading days of stored ATM IV (EOD). Historical days without a direct
reading are bootstrapped from scaled India VIX closes on version migration.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from atlas_lite.instruments import lookup_token
from atlas_lite.kite_rest import KiteRest
from atlas_lite.log_util import get_logger
from atlas_lite.nse_fo_bhav import fetch_nifty_atm_iv_series, prune_fo_bhav_cache
from atlas_lite.specs import NIFTY_SYMBOL, VIX_SYMBOLS

IST = ZoneInfo("Asia/Kolkata")
IVP_HISTORY_FILE = "iv_history.json"
IVP_HISTORY_META = "iv_history.meta.json"
IVP_HISTORY_VERSION = 5
IVP_HISTORY_SOURCE = "atm_iv"  # Black-76 ATM IV (NSE FO bhavcopy + VIX gap-fill)
IVP_MIN_SAMPLES = 5
IVP_MAX_DAYS = 252
IVP_FETCH_CALENDAR_DAYS = 400
IV_PROXY_SYMBOL = "NSE:INDIA VIX"
EOD_IV_RECORD_AFTER = time(15, 25)
FO_BHAV_CACHE = "fo_bhav"


def iv_sample_count(history: dict[str, Any], symbol: str = NIFTY_SYMBOL) -> int:
    series = history.get(symbol) or []
    return sum(1 for row in series if row.get("iv") is not None)


def ivp_sample_values(
    history: dict[str, Any],
    symbol: str = NIFTY_SYMBOL,
    *,
    exclude_today: bool = True,
) -> list[float]:
    """Historical ATM IV samples for percentile — excludes today's incomplete EOD."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    series = history.get(symbol) or []
    out: list[float] = []
    for row in series:
        if row.get("iv") is None:
            continue
        day = str(row.get("day") or "")
        if exclude_today and day >= today:
            continue
        out.append(float(row["iv"]))
    return out


def iv_change_n_days(
    history: dict[str, Any],
    live_iv: float | None,
    *,
    n: int = 5,
    symbol: str = NIFTY_SYMBOL,
) -> float | None:
    """Live IV minus ATM IV from n prior EOD samples. None if history is short."""
    if live_iv is None:
        return None
    today = datetime.now(IST).strftime("%Y-%m-%d")
    prior: list[float] = []
    for row in history.get(symbol) or []:
        if row.get("iv") is None:
            continue
        day = str(row.get("day") or "")
        if not day or day >= today:
            continue
        prior.append(float(row["iv"]))
    if len(prior) < n:
        return None
    return round(float(live_iv) - prior[-n], 3)


def load_iv_history(path: Path) -> dict[str, list[dict[str, Any]]]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_iv_history(path: Path, history: dict[str, Any]) -> None:
    path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")


def load_iv_history_meta(data_dir: Path) -> dict[str, Any]:
    path = data_dir / IVP_HISTORY_META
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_iv_history_meta(data_dir: Path, meta: dict[str, Any]) -> None:
    path = data_dir / IVP_HISTORY_META
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def needs_iv_history_rebuild(data_dir: Path, symbol: str = NIFTY_SYMBOL) -> bool:
    meta = load_iv_history_meta(data_dir)
    if int(meta.get("version") or 0) < IVP_HISTORY_VERSION:
        return True
    if meta.get("source") != IVP_HISTORY_SOURCE:
        return True
    history = load_iv_history(data_dir / IVP_HISTORY_FILE)
    if iv_sample_count(history, symbol) < IVP_MIN_SAMPLES:
        return True
    if needs_iv_history_rescale(data_dir, symbol):
        return True
    return False


def needs_iv_history_rescale(data_dir: Path, symbol: str = NIFTY_SYMBOL) -> bool:
    """True when bootstrap used unscaled VIX (scale=1.0) and history is all proxy."""
    meta = load_iv_history_meta(data_dir)
    try:
        scale = float(meta.get("bootstrap_scale") or 0)
    except (TypeError, ValueError):
        return False
    if abs(scale - 1.0) > 0.001:
        return False
    stats = ivp_history_stats(load_iv_history(data_dir / IVP_HISTORY_FILE), symbol)
    return stats["real"] == 0 and stats["proxy"] >= IVP_MIN_SAMPLES


def needs_iv_history_daily_refresh(data_dir: Path) -> bool:
    """True once per IST calendar day (EOD sample + proxy refresh check)."""
    meta = load_iv_history_meta(data_dir)
    updated = str(meta.get("updated_at") or "")[:10]
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return updated != today


def ivp_history_stats(
    history: dict[str, Any],
    symbol: str = NIFTY_SYMBOL,
    *,
    exclude_today: bool = True,
) -> dict[str, int]:
    """Count proxy vs real EOD samples used for IVP (excludes today by default)."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    series = history.get(symbol) or []
    proxy = real = 0
    for row in series:
        if row.get("iv") is None:
            continue
        day = str(row.get("day") or "")
        if exclude_today and day >= today:
            continue
        if row.get("proxy"):
            proxy += 1
        else:
            real += 1
    return {"proxy": proxy, "real": real, "total": proxy + real}


def compute_ivp(samples: list[float], current_iv: float | None) -> float | None:
    if current_iv is None:
        return None
    clean = [float(v) for v in samples if v is not None]
    if len(clean) < IVP_MIN_SAMPLES:
        return None
    below = sum(1 for v in clean if v < current_iv)
    return round(below / len(clean) * 100, 1)


def _fmt_dt(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _day_from_candle_ts(raw: str) -> str:
    return str(raw)[:10]


def _resolve_vix_token(csvs: list[str]) -> int | None:
    token = lookup_token(csvs, IV_PROXY_SYMBOL)
    if token is not None:
        return token
    for sym in VIX_SYMBOLS:
        token = lookup_token(csvs, sym)
        if token is not None:
            return token
    return None


async def _fetch_vix_daily_series(
    rest: KiteRest,
    csvs: list[str],
    *,
    days: int = IVP_MAX_DAYS,
) -> list[dict[str, Any]]:
    token = _resolve_vix_token(csvs)
    if token is None:
        raise RuntimeError(f"Token not found for {IV_PROXY_SYMBOL}")

    now = datetime.now(IST)
    frm = _fmt_dt(now - timedelta(days=IVP_FETCH_CALENDAR_DAYS))
    to = _fmt_dt(now)
    candles = await rest.historical(token, "day", from_date=frm, to_date=to)

    series: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in candles:
        if not isinstance(c, (list, tuple)) or len(c) < 5:
            continue
        day = _day_from_candle_ts(str(c[0]))
        if day in seen:
            continue
        close = float(c[4])
        if close <= 0:
            continue
        seen.add(day)
        series.append({"day": day, "iv": round(close, 2)})
    series.sort(key=lambda row: str(row["day"]))
    if len(series) > days:
        series = series[-days:]
    return series


def _scale_vix_to_atm_iv(
    vix_series: list[dict[str, Any]],
    *,
    scale: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in vix_series:
        iv = row.get("iv")
        if iv is None:
            continue
        out.append(
            {
                "day": row["day"],
                "iv": round(float(iv) * scale, 2),
                "proxy": True,
            }
        )
    return out


def _resolve_bootstrap_scale(
    vix_series: list[dict[str, Any]],
    *,
    atm_iv: float | None,
    vix_live: float | None,
) -> float:
    """Align VIX-shaped history to today's ATM IV level."""
    if atm_iv is not None and vix_live is not None and vix_live > 0:
        return float(atm_iv) / float(vix_live)
    if atm_iv is not None and vix_series:
        last_vix = float(vix_series[-1]["iv"])
        if last_vix > 0:
            return float(atm_iv) / last_vix
    return 1.0


def upsert_atm_iv_sample(
    data_dir: Path,
    symbol: str,
    day: str,
    iv: float,
    *,
    proxy: bool = False,
) -> None:
    """Insert or replace one daily ATM IV sample."""
    path = data_dir / IVP_HISTORY_FILE
    history = load_iv_history(path)
    series = list(history.get(symbol) or [])
    row: dict[str, Any] = {"day": day, "iv": round(float(iv), 2)}
    if proxy:
        row["proxy"] = True
    replaced = False
    for i, existing in enumerate(series):
        if str(existing.get("day") or "") == day:
            series[i] = row
            replaced = True
            break
    if not replaced:
        series.append(row)
    series.sort(key=lambda item: str(item.get("day") or ""))
    if len(series) > IVP_MAX_DAYS:
        series = series[-IVP_MAX_DAYS:]
    history[symbol] = series
    save_iv_history(path, history)


def has_eod_sample(data_dir: Path, symbol: str, day: str) -> bool:
    history = load_iv_history(data_dir / IVP_HISTORY_FILE)
    for row in history.get(symbol) or []:
        if str(row.get("day") or "") == day and row.get("iv") is not None:
            if not row.get("proxy"):
                return True
    return False


def should_record_eod_iv(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return now.time() >= EOD_IV_RECORD_AFTER


def prune_weekend_iv_samples(data_dir: Path, symbol: str = NIFTY_SYMBOL) -> int:
    """Drop weekend rows mistakenly recorded as EOD IV samples."""
    path = data_dir / IVP_HISTORY_FILE
    history = load_iv_history(path)
    series = list(history.get(symbol) or [])
    kept: list[dict[str, Any]] = []
    removed = 0
    for row in series:
        day = str(row.get("day") or "")[:10]
        if len(day) == 10:
            try:
                if datetime.strptime(day, "%Y-%m-%d").weekday() >= 5:
                    removed += 1
                    continue
            except ValueError:
                pass
        kept.append(row)
    if removed:
        history[symbol] = kept
        save_iv_history(path, history)
    return removed


def _merge_proxy_with_real(
    scaled_proxy: list[dict[str, Any]],
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep real EOD samples when re-bootstrapping proxy history."""
    real_by_day = {
        str(row.get("day") or ""): row
        for row in existing
        if row.get("iv") is not None and not row.get("proxy")
    }
    merged: list[dict[str, Any]] = []
    proxy_days: set[str] = set()
    for row in scaled_proxy:
        day = str(row.get("day") or "")
        proxy_days.add(day)
        merged.append(real_by_day.get(day, row))
    for day in sorted(real_by_day):
        if day not in proxy_days:
            merged.append(real_by_day[day])
    merged.sort(key=lambda item: str(item.get("day") or ""))
    if len(merged) > IVP_MAX_DAYS:
        merged = merged[-IVP_MAX_DAYS:]
    return merged


async def rebuild_atm_iv_history(
    rest: KiteRest,
    csvs: list[str],
    *,
    days: int = IVP_MAX_DAYS,
    data_dir: Path,
    symbol: str = NIFTY_SYMBOL,
    atm_iv: float | None = None,
    vix_live: float | None = None,
) -> int:
    """Build ATM IV history from NSE FO bhavcopy; VIX-scaled fill for missing days."""
    log = get_logger("iv.history")
    bhav = await fetch_nifty_atm_iv_series(
        days=days,
        cache_dir=data_dir / FO_BHAV_CACHE,
    )
    log.info("NSE FO bhavcopy ATM IV days=%d", len(bhav))
    pruned = prune_fo_bhav_cache(data_dir / FO_BHAV_CACHE, keep_days=0)
    if pruned:
        log.info("pruned fo_bhav cache files=%d", pruned)
    if len(bhav) < IVP_MIN_SAMPLES and atm_iv is None:
        raise RuntimeError("IVP bootstrap requires NSE FO bhavcopy ATM IV or live ATM IV")

    vix_series = await _fetch_vix_daily_series(rest, csvs, days=days)
    scale = _resolve_bootstrap_scale(vix_series, atm_iv=atm_iv, vix_live=vix_live)
    if bhav and abs(scale - 1.0) < 0.001:
        # Prefer last bhav ATM vs last VIX so gap-fill sits on the same level.
        last_bhav = float(bhav[-1]["iv"])
        last_vix = float(vix_series[-1]["iv"]) if vix_series else 0.0
        if last_vix > 0:
            scale = last_bhav / last_vix
    scaled = _scale_vix_to_atm_iv(vix_series, scale=scale)
    series = _merge_proxy_with_real(scaled, bhav)
    history = load_iv_history(data_dir / IVP_HISTORY_FILE)
    history[symbol] = series
    save_iv_history(data_dir / IVP_HISTORY_FILE, history)

    now = datetime.now(IST)
    stats = ivp_history_stats(history, symbol)
    save_iv_history_meta(
        data_dir,
        {
            "version": IVP_HISTORY_VERSION,
            "source": IVP_HISTORY_SOURCE,
            "model": "black76",
            "symbol": symbol,
            "samples": len(series),
            "bhav_days": len(bhav),
            "bootstrap_scale": round(scale, 4),
            "bootstrap": "nse_fo_bhav+vix_fill",
            "bootstrap_atm_iv": round(float(atm_iv), 4) if atm_iv is not None else None,
            "bootstrap_vix": round(float(vix_live), 4) if vix_live is not None else None,
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "ATM IV from NSE FO bhavcopy (Black-76); VIX-scaled fill for holidays/gaps",
            "proxy": stats["proxy"],
            "real": stats["real"],
        },
    )
    return len(series)


async def backfill_iv_history(
    rest: KiteRest,
    csvs: list[str],
    *,
    days: int = IVP_MAX_DAYS,
    data_dir: Path,
    symbol: str = NIFTY_SYMBOL,
    existing: dict[str, Any] | None = None,
    atm_iv: float | None = None,
    vix_live: float | None = None,
) -> int:
    _ = existing
    return await rebuild_atm_iv_history(
        rest,
        csvs,
        days=days,
        data_dir=data_dir,
        symbol=symbol,
        atm_iv=atm_iv,
        vix_live=vix_live,
    )


async def ensure_iv_history(
    rest: KiteRest,
    csvs: list[str],
    data_dir: Path,
    *,
    symbol: str = NIFTY_SYMBOL,
    min_samples: int = IVP_MIN_SAMPLES,
    days: int = IVP_MAX_DAYS,
    atm_iv: float | None = None,
    vix_live: float | None = None,
) -> int:
    """Ensure ATM IV history exists for IVP (rebuild on version/source mismatch)."""
    # Disposable FO CSVs — ATM IV already lives in iv_history.json.
    pruned = prune_fo_bhav_cache(data_dir / FO_BHAV_CACHE, keep_days=0)
    if pruned:
        get_logger("iv.history").info("pruned leftover fo_bhav cache files=%d", pruned)
    if not needs_iv_history_rebuild(data_dir, symbol):
        history = load_iv_history(data_dir / IVP_HISTORY_FILE)
        return iv_sample_count(history, symbol)
    return await rebuild_atm_iv_history(
        rest,
        csvs,
        days=days,
        data_dir=data_dir,
        symbol=symbol,
        atm_iv=atm_iv,
        vix_live=vix_live,
    )


async def record_eod_atm_iv_if_due(
    data_dir: Path,
    symbol: str,
    iv: float,
    *,
    now: datetime | None = None,
) -> bool:
    """Append today's EOD ATM greeks IV once per IST day after 15:25."""
    now = now or datetime.now(IST)
    if not should_record_eod_iv(now):
        return False
    day = now.strftime("%Y-%m-%d")
    if has_eod_sample(data_dir, symbol, day):
        return False
    upsert_atm_iv_sample(data_dir, symbol, day, iv, proxy=False)
    meta = load_iv_history_meta(data_dir)
    meta["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    meta["last_eod_day"] = day
    meta["last_eod_iv"] = round(float(iv), 2)
    save_iv_history_meta(data_dir, meta)
    return True
