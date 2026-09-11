"""Notebook underlying config — NIFTY and SENSEX (paper stays NIFTY-only)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from atlas_lite.specs import (
    NIFTY_LABEL,
    NIFTY_SYMBOL,
    SENSEX_LABEL,
    SENSEX_SYMBOL,
)

NotebookId = Literal["nifty", "sensex"]
DEFAULT_NOTEBOOK: NotebookId = "nifty"
NOTEBOOK_IDS: tuple[NotebookId, ...] = ("nifty", "sensex")


@dataclass(frozen=True)
class NotebookConfig:
    id: NotebookId
    symbol: str
    label: str
    option_name: str
    exchange: str
    fut_segment: str
    strike_step: int
    hysteresis_pts: float
    bars_file: str
    ivp_key: str


NOTEBOOKS: dict[NotebookId, NotebookConfig] = {
    "nifty": NotebookConfig(
        id="nifty",
        symbol=NIFTY_SYMBOL,
        label=NIFTY_LABEL,
        option_name="NIFTY",
        exchange="NFO",
        fut_segment="NFO-FUT",
        strike_step=50,
        hysteresis_pts=8.0,
        bars_file="minute_bars.json",
        ivp_key=NIFTY_SYMBOL,
    ),
    "sensex": NotebookConfig(
        id="sensex",
        symbol=SENSEX_SYMBOL,
        label=SENSEX_LABEL,
        option_name="SENSEX",
        exchange="BFO",
        fut_segment="BFO-FUT",
        strike_step=100,
        hysteresis_pts=16.0,
        bars_file="minute_bars_sensex.json",
        ivp_key=SENSEX_SYMBOL,
    ),
}


def parse_notebook(raw: str | None) -> NotebookId:
    """Parse ?nb= query; unknown values fall back to nifty."""
    value = str(raw or "").strip().lower()
    if value in NOTEBOOKS:
        return value  # type: ignore[return-value]
    return DEFAULT_NOTEBOOK


def get_notebook(notebook_id: NotebookId | str | None = None) -> NotebookConfig:
    return NOTEBOOKS[parse_notebook(str(notebook_id) if notebook_id else None)]


def notebook_list() -> list[dict[str, str]]:
    return [
        {"id": cfg.id, "label": cfg.label, "symbol": cfg.symbol}
        for cfg in (NOTEBOOKS[i] for i in NOTEBOOK_IDS)
    ]
