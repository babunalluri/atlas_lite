"""Per-notebook live state (NIFTY / SENSEX). Paper uses nifty only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atlas_lite.instruments import AtmLegs, IndexOptionUniverse
from atlas_lite.minute_bars import MinuteBarBuilder
from atlas_lite.notebook import NotebookConfig, NotebookId


@dataclass
class NotebookSession:
    fut_oi_baseline: float | None = None
    fut_oi_day_high: float | None = None
    iv_day_high: float | None = None
    iv_day_low: float | None = None
    day: str = ""


@dataclass
class NotebookRuntime:
    config: NotebookConfig
    universe: IndexOptionUniverse | None = None
    atm: AtmLegs | None = None
    fo_csv: str = ""
    chain_cache: tuple[list[int], list[str], list[str]] | None = None
    last_atm_strike: int | None = None
    session: NotebookSession = field(default_factory=NotebookSession)
    adx: float | None = None
    atr: float | None = None
    adx_hint: str = ""
    bar_builder: MinuteBarBuilder | None = None
    kite_adx_bars: list[dict[str, Any]] = field(default_factory=list)
    adx_bars_day: str = ""
    adx_kite_seed_day: str = ""
    adx_warnings: list[str] = field(default_factory=list)
    adx_tail_task: Any = None
    adx_live_at: float = 0.0
    adx_kite_fetch_at: float = 0.0
    cached_atm_greeks_iv: float | None = None
    cached_atm_greeks_strike: int | None = None
    enabled: bool = True

    @property
    def id(self) -> NotebookId:
        return self.config.id

    @property
    def symbol(self) -> str:
        return self.config.symbol

    @property
    def label(self) -> str:
        return self.config.label
