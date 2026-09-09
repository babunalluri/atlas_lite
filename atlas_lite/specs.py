"""NIFTY 50 entry sheet — notebook gates (17/8/26) + display-only rows."""

from __future__ import annotations

from typing import Any

NIFTY_SYMBOL = "NSE:NIFTY 50"
NIFTY_LABEL = "NIFTY 50"
# ATM CE≈PE: |CE−PE|/avg ≤ this % (exact tick equality almost never happens).
CE_PE_BALANCE_PCT = 15.0
# FUT OI must sit on the session high; ATM IV on the session low (same band).
OI_NEAR_DAY_HIGH_PCT = 99.5
IV_NEAR_DAY_LOW_PCT = 99.5

INDEX_GATE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "nifty_chg",
        "label": "NIFTY 50",
        "feed_key": "index_nifty_chg",
        "rule": "abs_lte",
        "target": 0.49,
        "gates_entry": False,
        "show_in_table": False,
        "hint": "±0.49% vs prev close (display only)",
    },
    {
        "id": "banknifty_chg",
        "label": "BANKNIFTY",
        "feed_key": "index_banknifty_chg",
        "rule": "abs_lte",
        "target": 0.49,
        "gates_entry": False,
        "show_in_table": False,
        "hint": "±0.49% vs prev close (display only)",
    },
    {
        "id": "niftyit_chg",
        "label": "NIFTY IT",
        "feed_key": "index_niftyit_chg",
        "rule": "abs_lte",
        "target": 0.49,
        "gates_entry": False,
        "show_in_table": False,
        "hint": "±0.49% vs prev close (display only)",
    },
    {
        "id": "sensex_chg",
        "label": "SENSEX",
        "feed_key": "index_sensex_chg",
        "rule": "abs_lte",
        "target": 0.49,
        "gates_entry": False,
        "show_in_table": False,
        "hint": "±0.49% vs prev close (display only)",
    },
)

SHEET_SPECS: tuple[dict[str, Any], ...] = (
    {
        "row": 1,
        "id": "adx",
        "label": "ADX",
        "feed_key": "adx",
        "rule": "lt",
        "target": 25,
        "gates_entry": True,
        "hint": "ADX(14) < 25 · NIFTY 50 1m",
    },
    {
        "row": 2,
        "id": "atr",
        "label": "ATR",
        "feed_key": "atr",
        "rule": "info",
        "target": 0,
        "gates_entry": False,
        "hint": "ATR(14) · NIFTY 50 1m",
    },
    {
        "row": 3,
        "id": "vix_chg",
        "label": "VIX chg",
        "feed_key": "vix_chg",
        "rule": "abs_lte",
        "target": 2.99,
        "gates_entry": True,
        "hint": "±2.99 pts vs session open",
    },
    {
        "row": 4,
        "id": "pcr",
        "label": "PCR",
        "feed_key": "pcr",
        "rule": "between",
        "target": 1.0,
        "target_high": 1.3,
        "gates_entry": True,
        "hint": "PCR 1.0–1.3 · =1 no trend · <1 down · >1 up",
    },
    {
        "row": 5,
        "id": "ivp",
        "label": "IV Percentile",
        "feed_key": "ivp",
        "rule": "lt",
        "target": 70,
        "gates_entry": True,
        "hint": "IVP < 70 · room for a move · ATM IV vs 252d",
    },
    {
        "row": 6,
        "id": "ce",
        "label": "ATM CE",
        "feed_key": "ce",
        "rule": "ce_pe_balanced",
        "target": CE_PE_BALANCE_PCT,
        "gates_entry": True,
        "hint": "CE ≈ PE · |CE−PE|/avg ≤ 15%",
    },
    {
        "row": 7,
        "id": "pe",
        "label": "ATM PE",
        "feed_key": "pe",
        "rule": "info",
        "target": 0,
        "gates_entry": False,
        "hint": "ATM PE premium · same strike as ATM CE",
    },
    {
        "row": 8,
        "id": "iv_day_high",
        "label": "IV vs today low",
        "feed_key": "iv",
        "rule": "iv_near_day_low",
        "target": IV_NEAR_DAY_LOW_PCT,
        "gates_entry": True,
        "hint": "ATM IV near today's session low",
    },
    {
        "row": 9,
        "id": "oi_day_high",
        "label": "OI vs today high",
        "feed_key": "oi_vs_day_high",
        "rule": "gte",
        "target": OI_NEAR_DAY_HIGH_PCT,
        "gates_entry": True,
        "hint": "FUT OI on today's session high",
    },
    {
        "row": 10,
        "id": "max_pain",
        "label": "Max pain",
        "feed_key": "max_pain",
        "rule": "info",
        "target": 0,
        "gates_entry": False,
        "hint": "Max pain strike (display only)",
    },
)

ALL_SPECS: tuple[dict[str, Any], ...] = SHEET_SPECS + INDEX_GATE_SPECS

INDEX_SYMBOLS: dict[str, str] = {
    "index_nifty_chg": "NSE:NIFTY 50",
    "index_banknifty_chg": "NSE:NIFTY BANK",
    "index_niftyit_chg": "NSE:NIFTY IT",
    "index_sensex_chg": "BSE:SENSEX",
}

# Display-only strip (LTP / pts / %). Does not feed 7/7 AND.
HEADER_WATCHLIST: tuple[dict[str, str], ...] = (
    {"id": "nifty", "label": "NIFTY 50", "symbol": "NSE:NIFTY 50", "group": "index"},
    {"id": "sensex", "label": "SENSEX", "symbol": "BSE:SENSEX", "group": "index"},
    {"id": "banknifty", "label": "BANKNIFTY", "symbol": "NSE:NIFTY BANK", "group": "index"},
    {"id": "bankex", "label": "BANKEX", "symbol": "BSE:BANKEX", "group": "index"},
    {"id": "niftyit", "label": "NIFTY IT", "symbol": "NSE:NIFTY IT", "group": "index"},
    {"id": "nifty_fut", "label": "NIFTY FUT", "fut_name": "NIFTY", "group": "fut"},
    {"id": "sensex_fut", "label": "SENSEX FUT", "fut_name": "SENSEX", "group": "fut"},
    {"id": "banknifty_fut", "label": "BANKNIFTY FUT", "fut_name": "BANKNIFTY", "group": "fut"},
    {"id": "bankex_fut", "label": "BANKEX FUT", "fut_name": "BANKEX", "group": "fut"},
    {"id": "reliance", "label": "RELIANCE", "symbol": "NSE:RELIANCE", "group": "stock"},
    {"id": "hdfcbank", "label": "HDFC BANK", "symbol": "NSE:HDFCBANK", "group": "stock"},
    {"id": "icicibank", "label": "ICICI BANK", "symbol": "NSE:ICICIBANK", "group": "stock"},
    {"id": "tcs", "label": "TCS", "symbol": "NSE:TCS", "group": "stock"},
    {"id": "infy", "label": "INFOSYS", "symbol": "NSE:INFY", "group": "stock"},
    {"id": "sbin", "label": "SBI", "symbol": "NSE:SBIN", "group": "stock"},
    {"id": "airtel", "label": "AIRTEL", "symbol": "NSE:BHARTIARTL", "group": "stock"},
    {"id": "lt", "label": "L&T", "symbol": "NSE:LT", "group": "stock"},
    {"id": "mm", "label": "M&M", "symbol": "NSE:M&M", "group": "stock"},
    {"id": "bajfinance", "label": "BAJAJ FINANCE", "symbol": "NSE:BAJFINANCE", "group": "stock"},
    {"id": "sunpharma", "label": "SUNPHARMA", "symbol": "NSE:SUNPHARMA", "group": "stock"},
)

HEADER_INDICES: tuple[dict[str, str], ...] = tuple(
    item for item in HEADER_WATCHLIST if item.get("group") == "index"
)

VIX_SYMBOLS = ("NSE:INDIA VIX",)
