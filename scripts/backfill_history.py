#!/usr/bin/env python3
"""Backfill local history files used by Atlas Lite (IVP + ADX minute bars).

Usage:
  python3 scripts/backfill_history.py
  python3 scripts/backfill_history.py --iv-days 252 --minute-days 5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.config import load_settings
from atlas_lite.instruments import lookup_token
from atlas_lite.iv_history import IVP_HISTORY_FILE, backfill_iv_history
from atlas_lite.kite_rest import KiteRest, normalize_quote_map
from atlas_lite.metrics import atm_greeks_iv, quote_ltp
from atlas_lite.instruments import parse_nfo_csv, resolve_atm_legs
from atlas_lite.minute_bars import MinuteBarBuilder, save_bars
from atlas_lite.specs import NIFTY_SYMBOL

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = ROOT / "data"
MINUTE_BARS_FILE = "minute_bars.json"
NFO_INSTRUMENTS_FILE = "nfo_instruments.csv"
NSE_INSTRUMENTS_FILE = "nse_instruments.csv"
BSE_INSTRUMENTS_FILE = "bse_instruments.csv"
ADX_SYMBOL = NIFTY_SYMBOL


def _load_csvs() -> list[str]:
    paths = [NFO_INSTRUMENTS_FILE, NSE_INSTRUMENTS_FILE, BSE_INSTRUMENTS_FILE]
    csvs: list[str] = []
    for name in paths:
        path = DATA_DIR / name
        if not path.is_file():
            raise FileNotFoundError(
                f"Missing {path}. Start the app once (./run.sh) to cache instruments, then retry."
            )
        csvs.append(path.read_text(encoding="utf-8"))
    return csvs


def _fmt_dt(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S")


async def backfill_minute_bars(rest: KiteRest, csvs: list[str], days: int) -> int:
    token = lookup_token(csvs, ADX_SYMBOL)
    if token is None:
        raise RuntimeError(f"Token not found for {ADX_SYMBOL}")
    now = datetime.now(IST)
    frm = _fmt_dt(now - timedelta(days=days))
    to = _fmt_dt(now)
    candles = await rest.historical(token, "minute", from_date=frm, to_date=to)
    builder = MinuteBarBuilder(symbol=ADX_SYMBOL)
    builder.load_candles(candles)
    save_bars(DATA_DIR / MINUTE_BARS_FILE, builder)
    return builder.bar_count()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Atlas Lite local history files")
    parser.add_argument("--iv-days", type=int, default=252, help="Days of ATM IV history")
    parser.add_argument("--minute-days", type=int, default=5, help="Days of 1m bars for ADX")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    csvs = _load_csvs()
    settings = load_settings()
    rest = KiteRest(settings.api_key, settings.access_token)
    try:
        if args.minute_days > 0:
            bars = await backfill_minute_bars(rest, csvs, args.minute_days)
            print(f"Updated {DATA_DIR / MINUTE_BARS_FILE} → {bars} minute bars ({ADX_SYMBOL})")

        atm_iv = None
        vix_live = None
        try:
            nfo = csvs[0]
            universe = parse_nfo_csv(nfo)
            spot_row = await rest.quote(["NSE:NIFTY 50", "NSE:INDIA VIX"])
            quotes = normalize_quote_map(spot_row if isinstance(spot_row, dict) else {})
            spot = quote_ltp(quotes.get("NSE:NIFTY 50"))
            vix_live = quote_ltp(quotes.get("NSE:INDIA VIX"))
            if spot is not None:
                legs = resolve_atm_legs(universe, spot)
                chain = await rest.quote([legs.ce_symbol, legs.pe_symbol])
                chain_q = normalize_quote_map(chain if isinstance(chain, dict) else {})
                atm_iv = atm_greeks_iv(
                    chain_q.get(legs.ce_symbol),
                    chain_q.get(legs.pe_symbol),
                )
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: could not fetch live ATM IV for bootstrap scale: {exc}")

        iv_days = await backfill_iv_history(
            rest,
            csvs,
            days=args.iv_days,
            data_dir=DATA_DIR,
            atm_iv=atm_iv,
            vix_live=vix_live,
        )
    finally:
        await rest.close()

    print(
        f"Updated {DATA_DIR / IVP_HISTORY_FILE} → {iv_days} daily ATM IV samples "
        f"(VIX-scaled bootstrap; EOD greeks replace going forward)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
