"""Index F&O symbol resolution — ATM CE/PE from live spot (Kite instruments CSV)."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
NIFTY_STRIKE_STEP = 50
SENSEX_STRIKE_STEP = 100
CHAIN_WINGS = 5
# Hold ATM until price clears the mid by this many points (stops strike chatter).
ATM_HYSTERESIS_PTS = 8.0
SENSEX_ATM_HYSTERESIS_PTS = 16.0


def round_atm_strike(spot: float, step: int = NIFTY_STRIKE_STEP) -> int:
    """Same rule as Atlas signal engine: round(spot / step) × step."""
    step = max(step, 1)
    return int(round(spot / step) * step)


def sticky_atm_strike(
    price: float,
    current: int | None,
    *,
    step: int = NIFTY_STRIKE_STEP,
    hysteresis: float = ATM_HYSTERESIS_PTS,
) -> int:
    """Nearest strike, but keep `current` until price is hysteresis pts past the mid."""
    step = max(int(step), 1)
    naive = round_atm_strike(price, step)
    if current is None:
        return naive
    current = int(current)
    half = step / 2.0
    hyst = max(float(hysteresis), 0.0)
    if price >= current + half + hyst:
        return naive
    if price <= current - half - hyst:
        return naive
    return current


def strike_ladder(atm: int, step: int = NIFTY_STRIKE_STEP, wings: int = CHAIN_WINGS) -> list[int]:
    step = max(step, 1)
    return [atm + step * i for i in range(-wings, wings + 1)]


def _parse_expiry(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class IndexOptionUniverse:
    """NIFTY (NFO) or SENSEX (BFO) options + nearest FUT."""

    name: str
    fut_symbol: str
    fut_token: int
    expiry: date
    prefix: str
    exchange: str = "NFO"
    strike_step: int = NIFTY_STRIKE_STEP
    hysteresis_pts: float = ATM_HYSTERESIS_PTS

    def option_symbol(self, strike: int, side: str) -> str:
        side = side.upper()
        if side not in {"CE", "PE"}:
            raise ValueError(f"invalid side {side}")
        return f"{self.exchange}:{self.prefix}{int(strike)}{side}"


# Back-compat alias used across the codebase / paper path.
NiftyUniverse = IndexOptionUniverse


def _option_prefix_from_row(row: dict[str, Any]) -> str:
    ts = (row.get("tradingsymbol") or "").strip().upper()
    strike = str(int(float(row.get("strike") or 0)))
    side = (row.get("instrument_type") or "").strip().upper()
    suffix = f"{strike}{side}"
    if not ts.endswith(suffix):
        raise RuntimeError(f"Cannot derive option prefix from {ts}")
    return ts[: -len(suffix)]


def _nearest_option_expiry(
    csv_text: str,
    today: date,
    *,
    name: str = "NIFTY",
) -> tuple[date, str]:
    """Nearest CE/PE expiry on or after today for ``name``."""
    want = (name or "").strip().upper()
    prefixes: dict[date, str] = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        if (row.get("name") or "").strip().upper() != want:
            continue
        inst_type = (row.get("instrument_type") or "").strip().upper()
        if inst_type != "CE":
            continue
        expiry = _parse_expiry(row.get("expiry") or "")
        if expiry is None or expiry < today:
            continue
        if expiry not in prefixes:
            prefixes[expiry] = _option_prefix_from_row(row)
    if not prefixes:
        raise RuntimeError(f"No active {want} options expiry found in instruments CSV")
    nearest = min(prefixes.keys())
    return nearest, prefixes[nearest]


def parse_fo_csv(
    csv_text: str,
    *,
    name: str = "NIFTY",
    exchange: str = "NFO",
    fut_segment: str | None = None,
    strike_step: int = NIFTY_STRIKE_STEP,
    hysteresis_pts: float = ATM_HYSTERESIS_PTS,
    today: date | None = None,
) -> IndexOptionUniverse:
    """Nearest FUT + nearest options expiry for an index (NFO NIFTY or BFO SENSEX)."""
    today = today or datetime.now(IST).date()
    want = (name or "").strip().upper()
    exch = (exchange or "").strip().upper()
    segment = (fut_segment or f"{exch}-FUT").strip().upper()
    best_fut: tuple[date, dict[str, Any]] | None = None

    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        row_name = (row.get("name") or "").strip().upper()
        inst_type = (row.get("instrument_type") or "").strip().upper()
        row_segment = (row.get("segment") or "").strip().upper()
        row_exch = (row.get("exchange") or "").strip().upper()
        if row_name != want or inst_type != "FUT":
            continue
        if row_exch and row_exch != exch:
            continue
        if row_segment and segment and row_segment != segment:
            # Tolerate exchange-FUT aliases (e.g. BFO-FUT).
            if not (row_segment.endswith("-FUT") and row_exch == exch):
                continue
        expiry = _parse_expiry(row.get("expiry") or "")
        if expiry is None or expiry < today:
            continue
        if best_fut is None or expiry < best_fut[0]:
            best_fut = (expiry, row)

    if best_fut is None:
        raise RuntimeError(f"No active {want} FUT found in {exch} instruments")

    _fut_expiry, fut_row = best_fut
    fut_ts = (fut_row.get("tradingsymbol") or "").strip().upper()
    if not fut_ts.endswith("FUT"):
        raise RuntimeError(f"Unexpected {want} FUT symbol: {fut_ts}")

    fut_token = int(fut_row.get("instrument_token") or 0)
    if fut_token <= 0:
        raise RuntimeError(f"Missing instrument_token for {fut_ts}")

    opt_expiry, opt_prefix = _nearest_option_expiry(csv_text, today, name=want)

    return IndexOptionUniverse(
        name=want,
        fut_symbol=f"{exch}:{fut_ts}",
        fut_token=fut_token,
        expiry=opt_expiry,
        prefix=opt_prefix,
        exchange=exch,
        strike_step=max(int(strike_step), 1),
        hysteresis_pts=max(float(hysteresis_pts), 0.0),
    )


def parse_nfo_csv(csv_text: str, *, today: date | None = None) -> IndexOptionUniverse:
    """Pick nearest NIFTY FUT + nearest weekly options expiry (Kite chain parity)."""
    return parse_fo_csv(
        csv_text,
        name="NIFTY",
        exchange="NFO",
        fut_segment="NFO-FUT",
        strike_step=NIFTY_STRIKE_STEP,
        hysteresis_pts=ATM_HYSTERESIS_PTS,
        today=today,
    )


def nearest_fut_symbol(
    csv_text: str,
    name: str,
    *,
    today: date | None = None,
    exchange: str | None = None,
) -> str | None:
    """Nearest listed FUT for `name` (NIFTY, BANKNIFTY, SENSEX, BANKEX)."""
    if not csv_text:
        return None
    today = today or datetime.now(IST).date()
    want = (name or "").strip().upper()
    want_exch = (exchange or "").strip().upper()
    best: tuple[date, str] | None = None
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        if (row.get("name") or "").strip().upper() != want:
            continue
        inst_type = (row.get("instrument_type") or "").strip().upper()
        if inst_type != "FUT":
            continue
        exch = (row.get("exchange") or "").strip().upper()
        if want_exch and exch != want_exch:
            continue
        expiry = _parse_expiry(row.get("expiry") or "")
        if expiry is None or expiry < today:
            continue
        ts = (row.get("tradingsymbol") or "").strip().upper()
        if not ts or not exch:
            continue
        symbol = f"{exch}:{ts}"
        if best is None or expiry < best[0]:
            best = (expiry, symbol)
    return best[1] if best else None


def resolve_header_watch(
    watchlist: tuple[dict[str, str], ...] | list[dict[str, str]],
    csv_texts: list[str],
    *,
    today: date | None = None,
) -> list[dict[str, str]]:
    """Fill nearest-FUT symbols on watchlist rows that only have `fut_name`."""
    today = today or datetime.now(IST).date()
    out: list[dict[str, str]] = []
    for item in watchlist:
        row = dict(item)
        if not row.get("symbol") and row.get("fut_name"):
            for csv_text in csv_texts:
                found = nearest_fut_symbol(csv_text, row["fut_name"], today=today)
                if found:
                    row["symbol"] = found
                    break
        out.append(row)
    return out


@dataclass
class AtmLegs:
    strike: int
    ce_symbol: str
    pe_symbol: str
    ce_token: int | None = None
    pe_token: int | None = None

    def symbols(self) -> tuple[str, str]:
        return self.ce_symbol, self.pe_symbol


def resolve_atm_legs(
    universe: IndexOptionUniverse,
    spot: float,
    *,
    current_strike: int | None = None,
    ref_price: float | None = None,
) -> AtmLegs:
    """ATM CE + PE. `ref_price` is synthetic forward when available; else spot."""
    price = float(spot if ref_price is None else ref_price)
    strike = sticky_atm_strike(
        price,
        current_strike,
        step=universe.strike_step,
        hysteresis=universe.hysteresis_pts,
    )
    ce = universe.option_symbol(strike, "CE")
    pe = universe.option_symbol(strike, "PE")
    return AtmLegs(strike=strike, ce_symbol=ce, pe_symbol=pe)


def chain_symbols(universe: IndexOptionUniverse, atm: int) -> tuple[list[int], list[str], list[str]]:
    """ATM ±5 strikes (legacy narrow ladder)."""
    strikes = strike_ladder(atm, step=universe.strike_step)
    ce_syms = [universe.option_symbol(s, "CE") for s in strikes]
    pe_syms = [universe.option_symbol(s, "PE") for s in strikes]
    return strikes, ce_syms, pe_syms


def full_chain_symbols(
    universe: IndexOptionUniverse,
    fo_csv: str,
) -> tuple[list[int], list[str], list[str]]:
    """All listed CE/PE strikes for the active expiry (matches Kite option chain)."""
    want = (universe.name or "NIFTY").strip().upper()
    strikes: set[int] = set()
    reader = csv.DictReader(io.StringIO(fo_csv))
    for row in reader:
        if (row.get("name") or "").strip().upper() != want:
            continue
        inst_type = (row.get("instrument_type") or "").strip().upper()
        if inst_type not in {"CE", "PE"}:
            continue
        expiry = _parse_expiry(row.get("expiry") or "")
        if expiry != universe.expiry:
            continue
        try:
            strike = int(float(row.get("strike") or 0))
        except (TypeError, ValueError):
            continue
        if strike > 0:
            strikes.add(strike)
    ordered = sorted(strikes)
    ce_syms = [universe.option_symbol(s, "CE") for s in ordered]
    pe_syms = [universe.option_symbol(s, "PE") for s in ordered]
    return ordered, ce_syms, pe_syms


def build_symbol_token_index(csv_texts: list[str]) -> dict[str, int]:
    """One-time symbol → token map from instrument CSVs."""
    out: dict[str, int] = {}
    for csv_text in csv_texts:
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            exch = (row.get("exchange") or "").strip().upper()
            ts = (row.get("tradingsymbol") or "").strip()
            if not exch or not ts:
                continue
            sym = f"{exch}:{ts}"
            try:
                token = int(row.get("instrument_token") or 0)
            except (TypeError, ValueError):
                token = 0
            if token > 0:
                out[sym] = token
    return out


def token_map_for_symbols(index: dict[str, int], symbols: list[str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for sym in symbols:
        token = index.get(sym)
        if token:
            out[token] = sym
    return out


def instrument_token_map(csv_text: str, symbols: list[str]) -> dict[int, str]:
    """Map Kite instrument_token → exchange:symbol from instruments CSV."""
    wanted = set(symbols)
    out: dict[int, str] = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        exch = (row.get("exchange") or "").strip().upper()
        ts = (row.get("tradingsymbol") or "").strip()
        sym = f"{exch}:{ts}"
        if sym not in wanted:
            continue
        try:
            token = int(row.get("instrument_token") or 0)
        except (TypeError, ValueError):
            token = 0
        if token > 0:
            out[token] = sym
    return out


def instrument_token_map_multi(csv_texts: list[str], symbols: list[str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for csv_text in csv_texts:
        out.update(instrument_token_map(csv_text, symbols))
    return out


def lookup_token(source: dict[str, int] | list[str], symbol: str) -> int | None:
    if isinstance(source, dict):
        return source.get(symbol)
    mapping = token_map_for_symbols(build_symbol_token_index(source), [symbol])
    for token, sym in mapping.items():
        if sym == symbol:
            return token
    return None


def nifty_option_lot_size(nfo_csv: str, *, expiry: date | None = None) -> int:
    """Lot size for NIFTY options (currently 65). Fallback 65 if CSV has no row."""
    reader = csv.DictReader(io.StringIO(nfo_csv or ""))
    for row in reader:
        if (row.get("name") or "").strip().upper() != "NIFTY":
            continue
        inst_type = (row.get("instrument_type") or "").strip().upper()
        if inst_type not in {"CE", "PE"}:
            continue
        if expiry is not None:
            row_exp = _parse_expiry(row.get("expiry") or "")
            if row_exp != expiry:
                continue
        try:
            lot = int(float(row.get("lot_size") or 0))
        except (TypeError, ValueError):
            continue
        if lot > 0:
            return lot
    return 65
