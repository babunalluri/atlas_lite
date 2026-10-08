"""Build dual-notebook (NIFTY + SENSEX) sheet feed from Kite WS ticks.

Both notebooks stay warm; per-client selection via nb= argument to build_frame,
build_feed, candles, build_option_chain. Paper trading stays NIFTY-only.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from atlas_lite.config import STREAM_INTERVAL_MS, read_kite_credentials, read_llm_credentials
from atlas_lite.frame_util import frame_revision
from atlas_lite.strategy_hint import suggest_strategy
from atlas_lite.agent_gates import AgentGateStore
from atlas_lite.agent_policy import ADX_RANGE, ADX_TREND, apply_book_policy
from atlas_lite.agent_advisor import AgentAdvisor, agent_enabled
from atlas_lite.paper_agent import (
    STRATEGY as AGENT_STRATEGY,
    DEFAULT_LOT_SIZE as AGENT_LOT,
    PaperAgent,
    paper_agent_enabled,
)
from atlas_lite.notebook import NotebookConfig, get_notebook, NOTEBOOK_IDS, parse_notebook
from atlas_lite.notebook_runtime import NotebookRuntime, NotebookSession
from atlas_lite.instruments import (
    AtmLegs,
    IndexOptionUniverse,
    NiftyUniverse,
    build_symbol_token_index,
    full_chain_symbols,
    lookup_token,
    nifty_option_lot_size,
    parse_fo_csv,
    parse_next_option_universe,
    resolve_atm_legs,
    round_atm_strike,
    resolve_header_watch,
    token_map_for_symbols,
)
from atlas_lite.kite_rest import KiteRest, normalize_quote_map
from atlas_lite.kite_ws import KiteTicker, QuoteBook
from atlas_lite.log_util import get_logger, log_feed_snapshot
from atlas_lite.metrics import (
    atm_greeks_iv,
    chain_accumulated_totals,
    chain_pcr_max_pain,
    compute_adx,
    compute_atr,
    evaluate_sheet,
    iv_pct_of_day_low,
    oi_pct_of_day_high,
    option_chain_rows,
    quote_change_pct,
    quote_change_from_open_pct,
    quote_change_pts,
    quote_ask,
    quote_bid,
    quote_ltp,
    quote_oi,
    quote_oi_day_high,
    quote_session_open,
    quote_volume,
    resolve_atm_iv,
    atm_ref_price,
    option_carry_from_fut,
    top_of_book,
    update_oi_day_high,
)
from atlas_lite.iv_history import (
    IVP_HISTORY_FILE,
    IVP_MIN_SAMPLES,
    compute_ivp,
    ensure_iv_history,
    iv_change_n_days,
    iv_sample_count,
    ivp_history_stats,
    ivp_sample_values,
    load_iv_history,
    needs_iv_history_rebuild,
    prune_weekend_iv_samples,
    record_eod_atm_iv_if_due,
)
from atlas_lite.agent_structure import build_structure_expert
from atlas_lite.minute_bars import (
    MinuteBarBuilder,
    bars_from_kite_candles,
    in_adx_seed_window,
    kite_adx_window_start,
    load_bars,
    ohlc_from_bar_dicts,
    save_bars,
)
from atlas_lite.recorder import (
    SheetRecorder,
    is_record_session,
    maintain_recordings,
    record_dir,
    record_enabled,
)
from atlas_lite.paper_straddle import (
    FLY_WING_PTS,
    PaperStraddle,
    evaluate_paper_regime,
    in_paper_entry_window,
    iron_fly_strikes,
    paper_enabled,
)
from atlas_lite.paper_vwap_long import STRATEGY as VWAP_STRATEGY
from atlas_lite.paper_vwap_long import PaperVwapLong, paper_vwap_enabled
from atlas_lite.paper_short_straddle import STRATEGY as SHORT_STR_STRATEGY
from atlas_lite.paper_short_straddle import (
    DEFAULT_LOT_SIZE as SHORT_STR_LOT,
    PaperShortStraddle,
    paper_short_str_enabled,
)
from atlas_lite.paper_skew_fade import STRATEGY as SKEW_FADE_STRATEGY
from atlas_lite.paper_skew_fade import (
    DEFAULT_LOT_SIZE as SKEW_FADE_LOT,
    PaperSkewFade,
    paper_skew_fade_enabled,
)
from atlas_lite.paper_long_iron_condor import STRATEGY as LONG_IC_STRATEGY
from atlas_lite.paper_long_iron_condor import (
    DEFAULT_LOT_SIZE as LONG_IC_LOT,
    PaperLongIronCondor,
    long_iron_condor_strikes,
    paper_long_ic_enabled,
)
from atlas_lite.paper_theta_cliff import STRATEGY as THETA_CLIFF_STRATEGY
from atlas_lite.paper_theta_cliff import (
    DEFAULT_LOT_SIZE as THETA_CLIFF_LOT,
    VIX_PREV_FILE,
    PaperThetaCliff,
    load_vix_prev,
    paper_theta_cliff_enabled,
    save_vix_prev,
)
from atlas_lite.paper_short_iron_condor import STRATEGY as SHORT_IC_STRATEGY
from atlas_lite.paper_short_iron_condor import (
    DEFAULT_LOT_SIZE as SHORT_IC_LOT,
    PaperShortIronCondor,
    paper_short_ic_enabled,
)
from atlas_lite.paper_impulse_fade import STRATEGY as IMPULSE_FADE_STRATEGY
from atlas_lite.paper_impulse_fade import (
    DEFAULT_LOT_SIZE as IMPULSE_FADE_LOT,
    PaperImpulseFade,
    last_session_bar_minute,
    paper_impulse_fade_enabled,
    session_spot_closes,
)
from atlas_lite.paper_combo import STRATEGY as COMBO_STRATEGY
from atlas_lite.paper_combo import (
    DEFAULT_LOT_SIZE as COMBO_LOT,
    PaperCombo,
    paper_combo_enabled,
)
from atlas_lite.paper_ict import STRATEGY as ICT_STRATEGY
from atlas_lite.paper_ict import (
    DEFAULT_LOT_SIZE as ICT_LOT,
    PaperICT,
    paper_ict_enabled,
)
from atlas_lite.specs import (
    HEADER_WATCHLIST,
    INDEX_SYMBOLS,
    NIFTY_SYMBOL,
    SENSEX_SYMBOL,
    SHEET_SPECS,
    VIX_SYMBOLS,
)

IST = ZoneInfo("Asia/Kolkata")
MAINTENANCE_LOOP_S = 60.0
IV_GREEKS_REFRESH_S = 2.0
CHAIN_GREEKS_REFRESH_S = 5.0
CHAIN_GREEKS_WINGS = 12
# Pause 403 recoveries so a dead token does not hit Kite refresh on every 2s quote.
AUTH_RECOVER_COOLDOWN_S = 30.0
# ADX/DMI parity with Kite (verified Sep 2026): Kite REST closed 1m bars + WS forming
# minute, Wilder DMI(14) in metrics.wilder_dmi_series. Do not change bar source,
# refresh cadence, or compute path without re-verifying against Kite 1m DMI.
# Calendar lookback (not trading days): 5 covers weekend + a mid-week holiday so
# Wilder(14) still has prior-session bars (3 calendar days collapsed to "today only"
# after Sat–Mon off, e.g. 2026-09-15).
ADX_REST_DAYS = 5
NFO_INSTRUMENTS_FILE = "nfo_instruments.csv"
NSE_INSTRUMENTS_FILE = "nse_instruments.csv"
BSE_INSTRUMENTS_FILE = "bse_instruments.csv"
BFO_INSTRUMENTS_FILE = "bfo_instruments.csv"
NFO_INSTRUMENTS_META = "instruments.meta.json"
MIN_BARS_FOR_ADX = 29
# Overwrite tick-built OHLC with Kite REST for ~1 session+ of 1m bars.
ADX_TAIL_BARS = 500
# Recompute ADX/ATR from the forming 1m bar at the SSE cadence.
ADX_LIVE_REFRESH_S = STREAM_INTERVAL_MS / 1000.0
# Refresh Kite REST ADX bars (3-day window) often enough to track the forming minute.
ADX_KITE_FETCH_S = 5.0
# WS ticks this fresh count as healthy even if REST auth is sticky-failed.
HEALTH_TICK_MAX_AGE_S = 90.0

# Book playbook legend for trades hover (Entry / Exit gates).
PAPER_BOOK_GATES: dict[str, tuple[str, str]] = {
    "short_iron_condor": (
        "09:20–14:30 · fit CE+PE credit verticals sell@bid−buy@ask ∈ [4, 5] · "
        "Sensibull wings 100–400 (prefer 300–400) · 6 lots · max 1/day · hold to weekly expiry",
        "book TP ≥₹2,000 on ask/bid MTM · per-set SL at 4× mid/LTP (3s, fill@ask/bid) · "
        "re-entry after stop only before expiry 12:00 · flatten expiry 15:20 "
        "(ledger expiry-spot intrinsic / unknown if no print)",
    ),
    "long_iron_condor": (
        "09:15–09:25 · long ATM CE+PE + sell ATM±hedge (POP ~55%) · 1 lot · max 1/day",
        "validate 2m · close red vertical first · leftover flat/red ≤30m · "
        "green may run to giveback / turn-red / 15:14",
    ),
    "short_iron_fly": (
        "09:20–13:00 · short ATM CE+PE + 250pt wings · credit ≥100 · |NIFTY chg| ≤0.75% · "
        "IV rich vs RV · max 5/day",
        "½ credit target / ½ defined-loss stop on premium PnL · flat 15:14",
    ),
    "long_straddle": (
        "legacy ledger only · long ATM CE+PE (not opened live)",
        "target / stop on straddle premium · flat 15:14",
    ),
    "short_atm_straddle": (
        "14:00–14:15 · sell ATM CE+PE · 1 lot · max 1/day · |NIFTY chg| ≤0.75%",
        "hold to 15:14 · no premium target/stop",
    ),
    "atm_skew_fade": (
        "11:00–11:15 · |CE−PE| ≥12 · sell rich ATM wing · 1 lot · max 1/day · "
        "|NIFTY chg| ≤0.75%",
        "hold to 15:14 · stop if wing rises 15%",
    ),
    "atm_impulse_fade": (
        "09:30–14:45 · 3 closed 1m spot ≥12pts · long opposite ATM wing · 1 lot · "
        "max 4/day · 8m cooldown",
        "+8% target / −6% stop / 12m hold · flat 15:14",
    ),
    "combo_confluence": (
        "09:30–14:45 · closed 1m confluence B→CE / S→PE · 1 lot · max 4/day · 8m cooldown",
        "+8% target / −6% stop / 12m hold · flatten if confluence lost · "
        "opposite letter may reverse · flat 15:14",
    ),
    "ict": (
        "09:35–14:00 · 15m bias · 5m sweep + displacement + MSS · enter FVG retrace · "
        "long ATM CE / short ATM PE · 1 lot · no daily cap",
        "spot stop beyond sweep · spot target next liquidity (min 2R) · "
        "time exit 24×5m · flat 15:14",
    ),
    "theta_cliff_fence": (
        "expiry only 12:00–12:10 · short CE/PE outside 0.75σ vs morning H/L · "
        "100pt wings · skip if morning RV >0.9× yesterday VIX · 1 lot",
        "spot touches short → close that vertical · flatten 15:15 (force 15:25)",
    ),
    "nifty_vwap_long": (
        "09:30–14:00 · 5m VWAP long · B = tag −0.5σ then close above VWAP · "
        "ST(10,3) up · max 2/day · 1 unit",
        "take +1σ / stop −1σ / ST flip · hard flat 15:15",
    ),
    "agent_paper": (
        "09:45–15:00 · ATM CE/PE long or short from agent intent · 1 lot · "
        "cooldown / daily loss stop",
        "+10% target / −6% stop / trail after +4% · hold ≤20m · agent_exit · flat 15:14",
    ),
}


@dataclass
class SessionState:
    """Engine-level session state (VIX only; per-notebook state in NotebookRuntime.session)."""
    vix_open: float | None = None
    vix_last: float | None = None  # last print — persisted as yesterday's VIX
    day: str = ""


@dataclass
class FeedEngine:
    rest: KiteRest
    book: QuoteBook = field(default_factory=QuoteBook)
    ticker: KiteTicker | None = None
    notebooks: dict[str, NotebookRuntime] = field(default_factory=dict)
    token_map: dict[int, str] = field(default_factory=dict)
    session: SessionState = field(default_factory=SessionState)
    iv_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    data_dir: Path = field(default_factory=lambda: Path("data"))
    credentials_path: Path = field(default_factory=lambda: Path("kite_credentials"))
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _instrument_csvs: list[str] = field(default_factory=list)
    _header_watch: list[dict[str, str]] = field(default_factory=list, repr=False)
    _iv_history_seeded: bool = field(default=False, repr=False)
    _kite_auth_error: str = ""
    _recorder: SheetRecorder | None = field(default=None, repr=False)
    _record_last_revision: tuple[Any, ...] | None = field(default=None, repr=False)
    _structure_hm: str = field(default="", repr=False)
    _depth_watch: set[str] = field(default_factory=set, repr=False)
    _depth_leg_meta: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _depth_leg_rev: dict[str, tuple[Any, ...]] = field(default_factory=dict, repr=False)
    _depth_queue: deque[dict[str, Any]] = field(default_factory=deque, repr=False)
    _depth_recording: bool = field(default=False, repr=False)
    _token_index: dict[str, int] = field(default_factory=dict, repr=False)
    _instruments_day: str = field(default="", repr=False)
    _credentials_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _last_kite_auth_attempt_at: float = field(default=0.0, repr=False)
    _paper: PaperStraddle | None = field(default=None, repr=False)
    _paper_vwap: PaperVwapLong | None = field(default=None, repr=False)
    _paper_short: PaperShortStraddle | None = field(default=None, repr=False)
    _paper_skew: PaperSkewFade | None = field(default=None, repr=False)
    _paper_lic: PaperLongIronCondor | None = field(default=None, repr=False)
    _paper_theta: PaperThetaCliff | None = field(default=None, repr=False)
    _paper_short_ic: PaperShortIronCondor | None = field(default=None, repr=False)
    _paper_scalp: PaperImpulseFade | None = field(default=None, repr=False)
    _paper_combo: PaperCombo | None = field(default=None, repr=False)
    _paper_ict: PaperICT | None = field(default=None, repr=False)
    _combo_cache_key: tuple[Any, ...] | None = field(default=None, repr=False)
    _combo_cache_row: dict[str, Any] | None = field(default=None, repr=False)
    _paper_agent: PaperAgent | None = field(default=None, repr=False)
    _agent_gates: AgentGateStore | None = field(default=None, repr=False)
    _agent: AgentAdvisor | None = field(default=None, repr=False)
    _last_book_policy: dict[str, Any] | None = field(default=None, repr=False)
    _chain_greeks_at: dict[str, float] = field(default_factory=dict, repr=False)
    _log: Any = field(default_factory=lambda: get_logger("feed"), repr=False)

    # Back-compat properties for tests/paper (delegate to nifty notebook)
    @property
    def universe(self) -> IndexOptionUniverse | None:
        return self.notebooks["nifty"].universe if "nifty" in self.notebooks else None

    @property
    def atm(self) -> AtmLegs | None:
        return self.notebooks["nifty"].atm if "nifty" in self.notebooks else None

    @property
    def adx(self) -> float | None:
        return self.notebooks["nifty"].adx if "nifty" in self.notebooks else None

    @property
    def atr(self) -> float | None:
        return self.notebooks["nifty"].atr if "nifty" in self.notebooks else None

    @property
    def adx_hint(self) -> str:
        return self.notebooks["nifty"].adx_hint if "nifty" in self.notebooks else ""

    @property
    def _bar_builder(self) -> MinuteBarBuilder:
        nb = self.notebooks.get("nifty")
        if nb and nb.bar_builder:
            return nb.bar_builder
        return MinuteBarBuilder(symbol=NIFTY_SYMBOL)

    @property
    def _kite_adx_bars(self) -> list[dict[str, Any]]:
        return self.notebooks["nifty"].kite_adx_bars if "nifty" in self.notebooks else []

    @property
    def _chain_cache(self) -> tuple[list[int], list[str], list[str]] | None:
        return self.notebooks["nifty"].chain_cache if "nifty" in self.notebooks else None

    @property
    def _nfo_csv(self) -> str:
        return self.notebooks["nifty"].fo_csv if "nifty" in self.notebooks else ""

    @property
    def _last_atm_strike(self) -> int | None:
        return self.notebooks["nifty"].last_atm_strike if "nifty" in self.notebooks else None

    def nb_runtime(self, nb: str = "nifty") -> NotebookRuntime:
        """Get NotebookRuntime for the given notebook ID."""
        return self.notebooks[nb]

    async def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.rest.set_auth_error_handler(self._notify_kite_auth_error)
        await self._verify_kite_session()
        self._load_iv_history()
        self.book.on_tick = self._handle_tick

        await self._load_instrument_universe()
        
        # Log info for enabled notebooks
        for nb_id in NOTEBOOK_IDS:
            nb = self.notebooks.get(nb_id)
            if nb and nb.enabled and nb.universe:
                self._log.info(
                    "%s chain expiry=%s strikes=%d",
                    nb_id.upper(),
                    nb.universe.expiry,
                    len(nb.chain_cache[0]) if nb.chain_cache else 0,
                )
        
        self._sync_subscriptions(force=True)
        await self._refresh_atm_greeks_from_kite()
        
        # Seed ADX bars for all enabled notebooks
        for nb_id in NOTEBOOK_IDS:
            nb = self.notebooks.get(nb_id)
            if nb and nb.enabled:
                await self._seed_adx_bars_if_needed(nb)
                self._refresh_adx_from_bars(nb)

        self.ticker = KiteTicker(self.rest.api_key, self.rest.access_token, self.book)
        self.ticker.set_symbols(self.token_map)
        self.ticker.start()
        if record_enabled():
            self._recorder = SheetRecorder(record_dir(self.data_dir))
        if paper_enabled():
            # Paper stays NIFTY-only
            nifty_nb = self.notebooks.get("nifty")
            if nifty_nb and nifty_nb.universe:
                lot = nifty_option_lot_size(nifty_nb.fo_csv, expiry=nifty_nb.universe.expiry)
                self._paper = PaperStraddle(
                    path=self.data_dir / "paper_trades.jsonl",
                    lot_size=lot,
                )
                self._log.info("paper 1 lot qty=%d long overlay + short iron fly (no live orders)", lot)
        if paper_vwap_enabled():
            self._paper_vwap = PaperVwapLong(path=self.data_dir / "paper_vwap_trades.jsonl")
            self._log.info(
                "paper VWAP long qty=%d (separate ledger, no live orders)",
                self._paper_vwap.qty,
            )
        if paper_short_str_enabled():
            nifty_nb = self.notebooks.get("nifty")
            lot = SHORT_STR_LOT
            if nifty_nb and nifty_nb.universe:
                lot = nifty_option_lot_size(nifty_nb.fo_csv, expiry=nifty_nb.universe.expiry)
            elif self._paper is not None:
                lot = self._paper.lot_size
            self._paper_short = PaperShortStraddle(
                path=self.data_dir / "paper_short_straddle.jsonl",
                lot_size=lot,
            )
            self._log.info(
                "paper short ATM straddle %d lots qty=%d (14:00 hold, no live orders)",
                self._paper_short.lots,
                self._paper_short.lots * lot,
            )
        if paper_skew_fade_enabled():
            nifty_nb = self.notebooks.get("nifty")
            lot = SKEW_FADE_LOT
            if nifty_nb and nifty_nb.universe:
                lot = nifty_option_lot_size(nifty_nb.fo_csv, expiry=nifty_nb.universe.expiry)
            elif self._paper is not None:
                lot = self._paper.lot_size
            self._paper_skew = PaperSkewFade(
                path=self.data_dir / "paper_skew_fade.jsonl",
                lot_size=lot,
            )
            self._log.info(
                "paper ATM skew fade %d lots qty=%d (11:00 sell-rich-wing, no live orders)",
                self._paper_skew.lots,
                self._paper_skew.lots * lot,
            )
        if paper_long_ic_enabled():
            nifty_nb = self.notebooks.get("nifty")
            lot = LONG_IC_LOT
            if nifty_nb and nifty_nb.universe:
                lot = nifty_option_lot_size(nifty_nb.fo_csv, expiry=nifty_nb.universe.expiry)
            elif self._paper is not None:
                lot = self._paper.lot_size
            self._paper_lic = PaperLongIronCondor(
                path=self.data_dir / "paper_long_iron_condor.jsonl",
                lot_size=lot,
            )
            self._log.info(
                "paper long iron condor %d lots qty=%d (09:15 200/200, no live orders)",
                self._paper_lic.lots,
                self._paper_lic.lots * lot,
            )
        if paper_theta_cliff_enabled():
            nifty_nb = self.notebooks.get("nifty")
            lot = THETA_CLIFF_LOT
            if nifty_nb and nifty_nb.universe:
                lot = nifty_option_lot_size(nifty_nb.fo_csv, expiry=nifty_nb.universe.expiry)
            elif self._paper is not None:
                lot = self._paper.lot_size
            self._paper_theta = PaperThetaCliff(
                path=self.data_dir / "paper_theta_cliff.jsonl",
                lot_size=lot,
            )
            self._log.info(
                "paper theta-cliff fence %d lots qty=%d (expiry 12:00 IC, no live orders)",
                self._paper_theta.lots,
                self._paper_theta.lots * lot,
            )
        if paper_short_ic_enabled():
            nifty_nb = self.notebooks.get("nifty")
            lot = SHORT_IC_LOT
            if nifty_nb and nifty_nb.universe:
                lot = nifty_option_lot_size(nifty_nb.fo_csv, expiry=nifty_nb.universe.expiry)
            elif self._paper is not None:
                lot = self._paper.lot_size
            self._paper_short_ic = PaperShortIronCondor(
                path=self.data_dir / "paper_short_iron_condor.jsonl",
                lot_size=lot,
            )
            self._log.info(
                "paper short iron condor %d lots qty=%d (credit 4-5/side, 1%% TP, no live orders)",
                self._paper_short_ic.lots,
                self._paper_short_ic.lots * lot,
            )
        if paper_impulse_fade_enabled():
            nifty_nb = self.notebooks.get("nifty")
            lot = IMPULSE_FADE_LOT
            if nifty_nb and nifty_nb.universe:
                lot = nifty_option_lot_size(nifty_nb.fo_csv, expiry=nifty_nb.universe.expiry)
            elif self._paper is not None:
                lot = self._paper.lot_size
            self._paper_scalp = PaperImpulseFade(
                path=self.data_dir / "paper_impulse_fade.jsonl",
                lot_size=lot,
            )
            self._log.info(
                "paper ATM impulse fade %d lots qty=%d (3m scalp, no live orders)",
                self._paper_scalp.lots,
                self._paper_scalp.lots * lot,
            )
        if paper_ict_enabled():
            nifty_nb = self.notebooks.get("nifty")
            lot = ICT_LOT
            if nifty_nb and nifty_nb.universe:
                lot = nifty_option_lot_size(nifty_nb.fo_csv, expiry=nifty_nb.universe.expiry)
            elif self._paper is not None:
                lot = self._paper.lot_size
            self._paper_ict = PaperICT(
                path=self.data_dir / "paper_ict.jsonl",
                lot_size=lot,
            )
            self._log.info(
                "paper ICT %d lots qty=%d (15m/5m sweep-FVG, no live orders)",
                self._paper_ict.lots,
                self._paper_ict.lots * lot,
            )
        if paper_combo_enabled():
            nifty_nb = self.notebooks.get("nifty")
            lot = COMBO_LOT
            if nifty_nb and nifty_nb.universe:
                lot = nifty_option_lot_size(nifty_nb.fo_csv, expiry=nifty_nb.universe.expiry)
            elif self._paper is not None:
                lot = self._paper.lot_size
            self._paper_combo = PaperCombo(
                path=self.data_dir / "paper_combo.jsonl",
                lot_size=lot,
            )
            self._log.info(
                "paper COMBO confluence %d lots qty=%d (1m B/S, no live orders)",
                self._paper_combo.lots,
                self._paper_combo.lots * lot,
            )
        if paper_agent_enabled():
            nifty_nb = self.notebooks.get("nifty")
            lot = AGENT_LOT
            if nifty_nb and nifty_nb.universe:
                lot = nifty_option_lot_size(nifty_nb.fo_csv, expiry=nifty_nb.universe.expiry)
            elif self._paper is not None:
                lot = self._paper.lot_size
            self._paper_agent = PaperAgent(
                path=self.data_dir / "paper_agent.jsonl",
                lot_size=lot,
            )
            self._log.info(
                "paper agent book %d lots qty=%d (ATM CE/PE intents, no live orders)",
                self._paper_agent.lots,
                self._paper_agent.lots * lot,
            )

        self._agent_gates = AgentGateStore(self.data_dir / "agent_gates.json")
        if agent_enabled():
            self._agent = AgentAdvisor(
                data_dir=self.data_dir,
                gates=self._agent_gates,
                get_context=self._agent_market_context,
                get_paper=self.paper_snapshot,
                get_trades=lambda limit: self.list_paper_trades(limit=limit),
                propose_entry=self._agent_propose_entry,
                propose_exit=self._agent_propose_exit,
                tape_ready=self._agent_tape_ready,
                credentials=read_llm_credentials(),
            )

        self._tasks = [
            asyncio.create_task(self._maintenance_loop()),
            asyncio.create_task(self._iv_greeks_loop()),
            asyncio.create_task(self._seed_iv_history_if_needed()),
            asyncio.create_task(self._book_policy_loop()),
        ]
        if self._recorder is not None:
            self._tasks.append(asyncio.create_task(self._record_loop()))
        if self._paper is not None:
            self._tasks.append(asyncio.create_task(self._paper_loop()))
        if self._paper_vwap is not None:
            self._tasks.append(asyncio.create_task(self._paper_vwap_loop()))
        if self._paper_short is not None:
            self._tasks.append(asyncio.create_task(self._paper_short_loop()))
        if self._paper_skew is not None:
            self._tasks.append(asyncio.create_task(self._paper_skew_loop()))
        if self._paper_lic is not None:
            self._tasks.append(asyncio.create_task(self._paper_lic_loop()))
        if self._paper_theta is not None:
            self._tasks.append(asyncio.create_task(self._paper_theta_loop()))
        if self._paper_short_ic is not None:
            self._tasks.append(asyncio.create_task(self._paper_short_ic_loop()))
        if self._paper_scalp is not None:
            self._tasks.append(asyncio.create_task(self._paper_scalp_loop()))
        if self._paper_combo is not None:
            self._tasks.append(asyncio.create_task(self._paper_combo_loop()))
        if self._paper_ict is not None:
            self._tasks.append(asyncio.create_task(self._paper_ict_loop()))
        if self._paper_agent is not None:
            self._tasks.append(asyncio.create_task(self._paper_agent_loop()))
        if self._agent is not None:
            self._tasks.append(asyncio.create_task(self._agent_loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.ticker:
            await self.ticker.stop()
        # Save bars for all notebooks
        for nb_id in NOTEBOOK_IDS:
            nb = self.notebooks.get(nb_id)
            if nb and nb.bar_builder:
                bars_path = self.data_dir / nb.config.bars_file
                save_bars(bars_path, nb.bar_builder)
        await self.rest.close()

    def _today(self) -> str:
        return datetime.now(IST).strftime("%Y-%m-%d")

    async def _notify_kite_auth_error(self) -> None:
        """On 403: refresh_token if we have one, else reload kite_credentials from disk."""
        now = time.monotonic()
        if now - self._last_kite_auth_attempt_at < AUTH_RECOVER_COOLDOWN_S:
            return
        self._last_kite_auth_attempt_at = now
        if await self._try_refresh_kite_token():
            await self._reseed_adx_after_auth_ok()
            return
        await self._try_recover_kite_credentials()
        if not self._kite_auth_error:
            await self._reseed_adx_after_auth_ok()

    async def _reseed_adx_after_auth_ok(self) -> None:
        try:
            await self._maybe_reseed_adx_bars()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("ADX reseed after Kite auth restore failed: %s", exc)

    async def _apply_live_kite_credentials(self, api_key: str, access_token: str) -> None:
        self.rest.update_credentials(api_key, access_token)
        if self.ticker is not None:
            await self.ticker.stop()
            self.ticker.update_credentials(api_key, access_token)
            self.ticker.set_symbols(self.token_map)
            self.ticker.start()
        self._kite_auth_error = ""

    async def _try_refresh_kite_token(self) -> bool:
        """Use stored refresh_token to mint a new access_token without a browser login."""
        async with self._credentials_lock:
            try:
                raw = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            if not isinstance(raw, dict):
                return False
            refresh = str(raw.get("refresh_token") or "").strip()
            secret = str(raw.get("api_secret") or "").strip()
            if not refresh or not secret:
                return False
            try:
                session = await self.rest.renew_access_token(secret, refresh)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("Kite refresh_token failed: %s", exc)
                return False
            new_token = str(session.get("access_token") or "").strip()
            if not new_token:
                return False
            raw["access_token"] = new_token
            if session.get("refresh_token"):
                raw["refresh_token"] = session["refresh_token"]
            try:
                self.credentials_path.write_text(
                    json.dumps(raw, indent=2) + "\n", encoding="utf-8"
                )
            except OSError as exc:
                self._log.warning("Could not persist refreshed access_token: %s", exc)
            await self._apply_live_kite_credentials(
                str(raw.get("api_key") or self.rest.api_key),
                new_token,
            )
            self._log.info("Kite access_token renewed via refresh_token")
            return True

    async def _try_recover_kite_credentials(self) -> None:
        async with self._credentials_lock:
            try:
                api_key, access_token = read_kite_credentials(self.credentials_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._kite_auth_error = (
                    "Kite access token expired — run: python3 scripts/kite_get_access_token.py"
                )
                self._log.warning("Kite credential reload failed: %s", exc)
                return

            if access_token == self.rest.access_token:
                self._kite_auth_error = (
                    "Kite access token expired — update kite_credentials on disk "
                    "(python3 scripts/kite_get_access_token.py)"
                )
                self._log.warning(self._kite_auth_error)
                return

            self._log.info("Kite credentials changed on disk — reloading REST + WS")
            await self._apply_live_kite_credentials(api_key, access_token)

            try:
                await self.rest.check_session()
            except Exception as exc:  # noqa: BLE001
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 403:
                    self._kite_auth_error = (
                        "Kite access token expired — run: python3 scripts/kite_get_access_token.py"
                    )
                else:
                    self._kite_auth_error = f"Kite API error after reload: {exc}"
                self._log.warning(self._kite_auth_error)
                return

            self._log.info("Kite session restored from reloaded credentials")

    async def _verify_kite_session(self) -> None:
        try:
            await self.rest.check_session()
            self._kite_auth_error = ""
        except Exception as exc:  # noqa: BLE001
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 403:
                if await self._try_refresh_kite_token():
                    try:
                        await self.rest.check_session()
                        self._kite_auth_error = ""
                        return
                    except Exception:  # noqa: BLE001
                        pass
                self._kite_auth_error = (
                    "Kite access token expired — run: python3 scripts/kite_get_access_token.py"
                )
            else:
                self._kite_auth_error = f"Kite API error: {exc}"
            self._log.warning(self._kite_auth_error)

    async def _load_instrument_universe(self) -> None:
        today = self._today()
        meta_path = self.data_dir / NFO_INSTRUMENTS_META
        meta = self._read_instruments_meta(meta_path)
        files_meta = dict(meta.get("files") or {})
        dirty = False

        async def load_one(exchange: str, filename: str, *, required: bool = True) -> str:
            nonlocal dirty
            path = self.data_dir / filename
            if path.is_file() and files_meta.get(filename) == today:
                self._log.info("instruments cache hit %s day=%s", exchange, today)
                return path.read_text(encoding="utf-8")
            try:
                csv = await self.rest.instruments_csv(exchange)
            except Exception as exc:  # noqa: BLE001
                if required:
                    raise
                self._log.warning("instruments %s unavailable: %s", exchange, exc)
                return ""
            path.write_text(csv, encoding="utf-8")
            files_meta[filename] = today
            dirty = True
            self._log.info(
                "instruments cache refreshed %s day=%s bytes=%d", exchange, today, len(csv)
            )
            return csv

        nfo_csv = await load_one("NFO", NFO_INSTRUMENTS_FILE)
        nse_csv = await load_one("NSE", NSE_INSTRUMENTS_FILE)
        bse_csv = await load_one("BSE", BSE_INSTRUMENTS_FILE)
        bfo_csv = await load_one("BFO", BFO_INSTRUMENTS_FILE, required=False)
        if dirty:
            meta["day"] = today
            meta["files"] = files_meta
            meta["fetched_at_ms"] = int(time.time() * 1000)
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        self._instrument_csvs = [nfo_csv, nse_csv, bse_csv]
        if bfo_csv:
            self._instrument_csvs.append(bfo_csv)
        self._token_index = build_symbol_token_index(self._instrument_csvs)
        self._instruments_day = today
        self._header_watch = resolve_header_watch(HEADER_WATCHLIST, self._instrument_csvs)
        
        # Initialize NIFTY notebook
        nifty_cfg = get_notebook("nifty")
        nifty_universe = parse_fo_csv(
            nfo_csv,
            name="NIFTY",
            exchange=nifty_cfg.exchange,
            fut_segment=nifty_cfg.fut_segment,
            strike_step=nifty_cfg.strike_step,
            hysteresis_pts=nifty_cfg.hysteresis_pts,
        )
        nifty_nb = NotebookRuntime(config=nifty_cfg, universe=nifty_universe, fo_csv=nfo_csv)
        nifty_nb.next_universe = parse_next_option_universe(nfo_csv, nifty_universe)
        nifty_nb.chain_cache = full_chain_symbols(nifty_universe, nfo_csv)
        if nifty_nb.next_universe:
            self._log.info(
                "NIFTY next-week expiry=%s",
                nifty_nb.next_universe.expiry,
            )
        nifty_nb.bar_builder = load_bars(self.data_dir / nifty_cfg.bars_file, nifty_cfg.symbol)
        nifty_nb.bar_builder.drop_non_session_bars()
        self.notebooks["nifty"] = nifty_nb
        
        # Initialize SENSEX notebook (disable if BFO missing)
        sensex_cfg = get_notebook("sensex")
        sensex_nb = NotebookRuntime(config=sensex_cfg, enabled=False)
        if bfo_csv:
            try:
                sensex_universe = parse_fo_csv(
                    bfo_csv,
                    name="SENSEX",
                    exchange=sensex_cfg.exchange,
                    fut_segment=sensex_cfg.fut_segment,
                    strike_step=sensex_cfg.strike_step,
                    hysteresis_pts=sensex_cfg.hysteresis_pts,
                )
                sensex_nb.universe = sensex_universe
                sensex_nb.fo_csv = bfo_csv
                sensex_nb.chain_cache = full_chain_symbols(sensex_universe, bfo_csv)
                sensex_nb.bar_builder = load_bars(self.data_dir / sensex_cfg.bars_file, sensex_cfg.symbol)
                sensex_nb.bar_builder.drop_non_session_bars()
                sensex_nb.enabled = True
            except Exception as exc:  # noqa: BLE001
                self._log.warning("SENSEX notebook disabled (BFO parse failed): %s", exc)
        else:
            self._log.warning("SENSEX notebook disabled (BFO instruments unavailable)")
        self.notebooks["sensex"] = sensex_nb

    @staticmethod
    def _read_instruments_meta(meta_path: Path) -> dict[str, Any]:
        if not meta_path.is_file():
            return {}
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    async def _try_load_instruments_csv(self, exchange: str, filename: str) -> str:
        try:
            return await self._load_instruments_csv(exchange, filename)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("instruments %s unavailable: %s", exchange, exc)
            return ""

    async def _load_instruments_csv(self, exchange: str, filename: str) -> str:
        """Single-file load (reload path). Universe bootstrap uses the batched loader."""
        path = self.data_dir / filename
        meta_path = self.data_dir / NFO_INSTRUMENTS_META
        today = self._today()
        meta = self._read_instruments_meta(meta_path)
        files = dict(meta.get("files") or {})
        if path.is_file() and files.get(filename) == today:
            self._log.info("instruments cache hit %s day=%s", exchange, today)
            return path.read_text(encoding="utf-8")
        csv = await self.rest.instruments_csv(exchange)
        path.write_text(csv, encoding="utf-8")
        files[filename] = today
        meta["day"] = today
        meta["files"] = files
        meta["fetched_at_ms"] = int(time.time() * 1000)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self._log.info("instruments cache refreshed %s day=%s bytes=%d", exchange, today, len(csv))
        return csv
    async def _bootstrap_iv_inputs(self, nb_id: str = "nifty") -> tuple[float | None, float | None]:
        """ATM IV + India VIX for IVP bootstrap scale (greeks, else BS from LTP)."""
        nb = self.notebooks.get(nb_id)
        if not nb or not nb.universe:
            return None, None
        atm_iv = nb.cached_atm_greeks_iv
        vix_live = quote_ltp(self.book.get(VIX_SYMBOLS[0])) if VIX_SYMBOLS else None
        if atm_iv is not None and vix_live is not None:
            return atm_iv, vix_live
        if not self._instrument_csvs:
            return atm_iv, vix_live
        try:
            raw = await self.rest.quote([nb.config.symbol, *VIX_SYMBOLS])
            quotes = normalize_quote_map(raw if isinstance(raw, dict) else {})
            vix_live = vix_live or quote_ltp(quotes.get(VIX_SYMBOLS[0]))
            spot = quote_ltp(quotes.get(nb.config.symbol))
            if spot is not None:
                legs = resolve_atm_legs(nb.universe, spot)
                chain_raw = await self.rest.quote([legs.ce_symbol, legs.pe_symbol])
                chain_q = normalize_quote_map(chain_raw if isinstance(chain_raw, dict) else {})
                ce_row = chain_q.get(legs.ce_symbol)
                pe_row = chain_q.get(legs.pe_symbol)
                ce = ce_row if isinstance(ce_row, dict) else None
                pe = pe_row if isinstance(pe_row, dict) else None
                # Quotes often omit greeks — fall back to BS IV from LTP for scale.
                fetched = resolve_atm_iv(ce, pe, spot, legs.strike, nb.universe.expiry)
                if fetched is not None:
                    atm_iv = fetched
                    if atm_greeks_iv(ce, pe) is not None:
                        nb.cached_atm_greeks_iv = fetched
                        nb.cached_atm_greeks_strike = legs.strike
        except Exception as exc:  # noqa: BLE001
            self._log.warning("%s IVP bootstrap IV fetch failed: %s", nb_id.upper(), exc)
        return atm_iv, vix_live

    async def _seed_iv_history_if_needed(self) -> None:
        if self._iv_history_seeded:
            return
        try:
            atm_iv, vix_live = await self._bootstrap_iv_inputs()
            samples = await ensure_iv_history(
                self.rest,
                self._instrument_csvs,
                self.data_dir,
                symbol=NIFTY_SYMBOL,
                atm_iv=atm_iv,
                vix_live=vix_live,
            )
            self.iv_history = load_iv_history(self.data_dir / IVP_HISTORY_FILE)
            self._iv_history_seeded = True
            self._log.info(
                "IVP history ready samples=%d source=atm_iv live_iv=greeks_or_black76",
                samples,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("IVP history backfill failed: %s", exc)

    async def _refresh_iv_history_if_stale(self) -> None:
        """Refresh IVP history for all enabled notebooks."""
        try:
            # NIFTY IVP history
            removed = prune_weekend_iv_samples(self.data_dir, NIFTY_SYMBOL)
            if removed:
                self._log.info("NIFTY IVP history pruned weekend samples=%d", removed)
            if needs_iv_history_rebuild(self.data_dir, NIFTY_SYMBOL):
                atm_iv, vix_live = await self._bootstrap_iv_inputs("nifty")
                samples = await ensure_iv_history(
                    self.rest,
                    self._instrument_csvs,
                    self.data_dir,
                    symbol=NIFTY_SYMBOL,
                    atm_iv=atm_iv,
                    vix_live=vix_live,
                )
            else:
                nifty_nb = self.notebooks.get("nifty")
                iv = nifty_nb.cached_atm_greeks_iv if nifty_nb else None
                if iv is None and nifty_nb and nifty_nb.atm and nifty_nb.universe:
                    iv = self._resolve_live_iv(
                        nifty_nb,
                        self.book.get(nifty_nb.atm.ce_symbol),
                        self.book.get(nifty_nb.atm.pe_symbol),
                        self._spot(nifty_nb),
                        nifty_nb.atm.strike,
                        nifty_nb.universe.expiry,
                    )
                if iv is not None:
                    await record_eod_atm_iv_if_due(
                        self.data_dir,
                        NIFTY_SYMBOL,
                        iv,
                    )
                samples = iv_sample_count(
                    load_iv_history(self.data_dir / IVP_HISTORY_FILE),
                    NIFTY_SYMBOL,
                )
            
            # SENSEX IVP history (same file, keyed by SENSEX_SYMBOL)
            sensex_nb = self.notebooks.get("sensex")
            if sensex_nb and sensex_nb.enabled:
                removed_sensex = prune_weekend_iv_samples(self.data_dir, SENSEX_SYMBOL)
                if removed_sensex:
                    self._log.info("SENSEX IVP history pruned weekend samples=%d", removed_sensex)
                if needs_iv_history_rebuild(self.data_dir, SENSEX_SYMBOL):
                    atm_iv_sensex, vix_sensex = await self._bootstrap_iv_inputs("sensex")
                    # Bootstrap if samples missing; else skip for v1
                    if atm_iv_sensex is not None:
                        await ensure_iv_history(
                            self.rest,
                            self._instrument_csvs,
                            self.data_dir,
                            symbol=SENSEX_SYMBOL,
                            atm_iv=atm_iv_sensex,
                            vix_live=vix_sensex,
                        )
                else:
                    iv_sensex = sensex_nb.cached_atm_greeks_iv
                    if iv_sensex is None and sensex_nb.atm and sensex_nb.universe:
                        iv_sensex = self._resolve_live_iv(
                            sensex_nb,
                            self.book.get(sensex_nb.atm.ce_symbol),
                            self.book.get(sensex_nb.atm.pe_symbol),
                            self._spot(sensex_nb),
                            sensex_nb.atm.strike,
                            sensex_nb.universe.expiry,
                        )
                    if iv_sensex is not None:
                        await record_eod_atm_iv_if_due(
                            self.data_dir,
                            SENSEX_SYMBOL,
                            iv_sensex,
                        )
            
            self.iv_history = load_iv_history(self.data_dir / IVP_HISTORY_FILE)
            self._iv_history_seeded = True
            self._log.info("IVP history refreshed")
        except Exception as exc:  # noqa: BLE001
            self._log.warning("IVP history refresh failed: %s", exc)

    async def _reload_instruments_if_needed(self) -> None:
        today = self._today()
        if self._instruments_day == today and self.notebooks:
            return
        # Capture old expiries for all notebooks
        old_expiries = {
            nb_id: nb.universe.expiry if nb.universe else None
            for nb_id, nb in self.notebooks.items()
        }
        await self._load_instrument_universe()
        # Reset ATM cache if expiry changed for any notebook
        for nb_id, nb in self.notebooks.items():
            old_exp = old_expiries.get(nb_id)
            if nb.universe and old_exp != nb.universe.expiry:
                nb.last_atm_strike = None
                nb.cached_atm_greeks_iv = None
                nb.cached_atm_greeks_strike = None
                nb.atm = None
                self._log.info(
                    "%s instruments refreshed expiry=%s strikes=%d",
                    nb_id.upper(),
                    nb.universe.expiry,
                    len(nb.chain_cache[0]) if nb.chain_cache else 0,
                )
        self._sync_subscriptions(force=True)

    def _last_bar_day(self, nb: NotebookRuntime) -> str | None:
        if not nb.bar_builder or not nb.bar_builder.bars:
            return None
        return str(nb.bar_builder.bars[-1].get("t") or "")[:10] or None

    def _apply_kite_bar_authority(
        self,
        nb: NotebookRuntime,
        candles: list[list[Any]],
        *,
        window_floor: str,
        tail_from: str | None = None,
    ) -> tuple[int, int]:
        """Merge Kite OHLC then drop orphans so ADX/ATR match Kite charts."""
        if not nb.bar_builder:
            return 0, 0
        merged = nb.bar_builder.merge_kite_candles(candles)
        if tail_from:
            synced = nb.bar_builder.drop_closed_bars_not_in_kite(
                candles,
                range_from=tail_from,
            )
        else:
            synced = nb.bar_builder.sync_closed_bars_from_kite(
                candles,
                window_floor=window_floor,
            )
        trimmed = self._trim_bars_to_kite_window(nb)
        return merged, synced + trimmed

    def _trim_bars_to_kite_window(self, nb: NotebookRuntime, when: datetime | None = None) -> int:
        """Keep only bars inside the same rolling window as Kite historical fetches."""
        if not nb.bar_builder:
            return 0
        floor = kite_adx_window_start(when or datetime.now(IST), days=ADX_REST_DAYS)
        dropped = nb.bar_builder.drop_bars_before(floor)
        if dropped:
            self._log.info("%s ADX bars trimmed before %s dropped=%d", nb.id.upper(), floor, dropped)
        return dropped

    def _rebuild_kite_adx_bars(self, nb: NotebookRuntime, candles: list[list[Any]]) -> None:
        """ADX/ATR use Kite REST bars only — never WS tick-built OHLC."""
        bars = bars_from_kite_candles(candles, include_forming=True)
        # Skip rev bump when the payload is unchanged (tail refresh every ~5s).
        if bars == nb.kite_adx_bars:
            return
        nb.kite_adx_bars = bars
        nb.kite_adx_rev += 1

    def _adx_bars_fresh(self, nb: NotebookRuntime) -> bool:
        if not nb.bar_builder or len(nb.bar_builder.bars) < MIN_BARS_FOR_ADX:
            return False
        return self._last_bar_day(nb) == self._today()

    async def _seed_adx_bars_from_kite(self, nb: NotebookRuntime) -> None:
        """REST fetch of notebook's 1m candles (Kite chart OHLC — authoritative for ADX/ATR)."""
        if not nb.bar_builder:
            return
        token = lookup_token(self._token_index, nb.config.symbol)
        if token is None:
            return
        now = datetime.now(IST)
        frm = (now - timedelta(days=ADX_REST_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        to = now.strftime("%Y-%m-%d %H:%M:%S")
        candles = await self.rest.historical_minute(token, from_date=frm, to_date=to)
        today = self._today()
        if not candles:
            # Empty during cash session: leave latch unset so we retry until Kite has bars.
            # Off-session / weekend fetches are skipped in _maybe_reseed / tail refresh.
            dropped = nb.bar_builder.drop_non_session_bars()
            if dropped:
                save_bars(self.data_dir / nb.config.bars_file, nb.bar_builder)
            self._log.info(
                "%s ADX bars seed empty from Kite day=%s dropped_off_session=%d",
                nb.id.upper(),
                today,
                dropped,
            )
            return
        nb.adx_kite_seed_day = today
        # Preserve in-progress WS minute across full reseed.
        live_key = nb.bar_builder._current_key
        live_o = nb.bar_builder._open
        live_h = nb.bar_builder._high
        live_l = nb.bar_builder._low
        live_c = nb.bar_builder._close
        live_v = nb.bar_builder._volume
        live_sv = nb.bar_builder._session_vol
        live_oi = nb.bar_builder._oi
        builder = MinuteBarBuilder(symbol=nb.config.symbol)
        floor = kite_adx_window_start(now, days=ADX_REST_DAYS)
        builder.sync_closed_bars_from_kite(candles, window_floor=floor)
        if live_key and live_c is not None and live_key > (builder.last_bar_minute() or ""):
            builder._current_key = live_key
            builder._open = live_o
            builder._high = live_h
            builder._low = live_l
            builder._close = live_c
            builder._volume = live_v
            builder._session_vol = live_sv
            builder._oi = live_oi
        nb.bar_builder = builder
        nb.bar_builder.drop_non_session_bars()
        self._rebuild_kite_adx_bars(nb, candles)
        await self._attach_fut_volume(nb, frm, to)
        nb.adx_bars_day = today
        save_bars(self.data_dir / nb.config.bars_file, nb.bar_builder)
        self._log.info(
            "%s ADX bars seeded from Kite closed=%d symbol=%s",
            nb.id.upper(),
            len(nb.bar_builder.bars),
            nb.config.symbol,
        )

    async def _seed_adx_bars_if_needed(self, nb: NotebookRuntime) -> None:
        if not nb.bar_builder:
            return
        today = self._today()
        # One successful Kite historical seed per IST day.
        if nb.adx_kite_seed_day == today:
            dropped = nb.bar_builder.drop_non_session_bars()
            trimmed = self._trim_bars_to_kite_window(nb)
            nb.adx_bars_day = today
            if dropped or trimmed:
                save_bars(self.data_dir / nb.config.bars_file, nb.bar_builder)
            self._log.info(
                "%s ADX bars cache hit bars=%d day=%s kite_seed=%s dropped_off_session=%d trimmed=%d",
                nb.id.upper(),
                nb.bar_builder.bar_count(),
                nb.adx_bars_day,
                nb.adx_kite_seed_day,
                dropped,
                trimmed,
            )
            return
        if not in_adx_seed_window(datetime.now(IST)):
            # After hours / weekend: skip normal seed so we do not day-latch empty
            # overnight and miss the 09:15 fill. Exception: cold start with too few
            # bars (new notebook like SENSEX) — pull Kite history once so chart/ADX work.
            cold = nb.bar_builder.bar_count() < MIN_BARS_FOR_ADX and len(nb.kite_adx_bars) < MIN_BARS_FOR_ADX
            if not cold:
                dropped = nb.bar_builder.drop_non_session_bars()
                if dropped:
                    save_bars(self.data_dir / nb.config.bars_file, nb.bar_builder)
                self._log.info(
                    "%s ADX bars seed skipped off-session day=%s dropped_off_session=%d",
                    nb.id.upper(),
                    today,
                    dropped,
                )
                return
            self._log.info(
                "%s ADX cold-start seed off-session bars=%d",
                nb.id.upper(),
                nb.bar_builder.bar_count(),
            )
        await self._seed_adx_bars_from_kite(nb)

    async def _maybe_reseed_adx_bars(self) -> None:
        """Re-seed once per IST day from Kite for all enabled notebooks."""
        today = self._today()
        for nb in self.notebooks.values():
            if not nb.enabled:
                continue
            if nb.adx_kite_seed_day == today:
                continue
            if not in_adx_seed_window(datetime.now(IST)):
                cold = (
                    nb.bar_builder is not None
                    and nb.bar_builder.bar_count() < MIN_BARS_FOR_ADX
                    and len(nb.kite_adx_bars) < MIN_BARS_FOR_ADX
                )
                if not cold:
                    if nb.bar_builder:
                        dropped = nb.bar_builder.drop_non_session_bars()
                        if dropped:
                            save_bars(self.data_dir / nb.config.bars_file, nb.bar_builder)
                    continue
            await self._seed_adx_bars_if_needed(nb)
            self._refresh_adx_from_bars(nb)

    def _reset_session_if_new_day(self) -> None:
        today = self._today()
        if self.session.day != today:
            # Persist last VIX print as yesterday for theta-cliff RV filter.
            prev_vix = self.session.vix_last or self.session.vix_open
            if prev_vix is None and VIX_SYMBOLS:
                prev_vix = quote_ltp(self.book.get(VIX_SYMBOLS[0]))
            if self.session.day and prev_vix is not None and prev_vix > 0:
                save_vix_prev(
                    self.data_dir / VIX_PREV_FILE,
                    day=self.session.day,
                    vix=float(prev_vix),
                )
            self.session = SessionState(day=today)
        for nb in self.notebooks.values():
            if nb.session.day != today:
                nb.session = NotebookSession(day=today)

    def _load_iv_history(self) -> None:
        self.iv_history = load_iv_history(self.data_dir / IVP_HISTORY_FILE)

    def _ivp_samples(self) -> list[float]:
        return ivp_sample_values(self.iv_history, NIFTY_SYMBOL)

    def _greeks_iv(
        self,
        nb: NotebookRuntime,
        ce_row: dict[str, Any] | None,
        pe_row: dict[str, Any] | None,
    ) -> float | None:
        if (
            nb.atm is not None
            and nb.cached_atm_greeks_iv is not None
            and nb.cached_atm_greeks_strike == nb.atm.strike
        ):
            return nb.cached_atm_greeks_iv
        return atm_greeks_iv(ce_row, pe_row)

    def _resolve_live_iv(
        self,
        nb: NotebookRuntime,
        ce_row: dict[str, Any] | None,
        pe_row: dict[str, Any] | None,
        spot: float | None,
        strike: int | None,
        expiry: date | None,
    ) -> float | None:
        greeks_iv = self._greeks_iv(nb, ce_row, pe_row)
        if greeks_iv is not None:
            return greeks_iv
        return resolve_atm_iv(ce_row, pe_row, spot, strike, expiry)

    def _persist_bars_async(self, nb: NotebookRuntime) -> None:
        if not nb.bar_builder:
            return
        path = self.data_dir / nb.config.bars_file
        builder = nb.bar_builder
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            save_bars(path, builder)
            return
        loop.create_task(asyncio.to_thread(save_bars, path, builder))

    def _schedule_kite_adx_refresh(self, nb: NotebookRuntime) -> None:
        now = time.monotonic()
        if now - nb.adx_kite_fetch_at < ADX_KITE_FETCH_S:
            return
        nb.adx_kite_fetch_at = now
        self._schedule_adx_tail_refresh(nb)

    def _handle_tick(self, symbol: str, row: dict[str, Any]) -> None:
        self._enqueue_depth_tick(symbol, row)

        # Dispatch FUT volume/OI to owning notebook
        for nb_id, nb in self.notebooks.items():
            if nb.enabled and nb.universe and symbol == nb.universe.fut_symbol:
                if nb.bar_builder:
                    nb.bar_builder.ingest_volume(quote_volume(row))
                    nb.bar_builder.ingest_oi(quote_oi(row))
        
        # Dispatch spot tick to owning notebook for bars + ATM
        for nb_id, nb in self.notebooks.items():
            if not nb.enabled or not nb.universe or symbol != nb.config.symbol:
                continue
            ltp = quote_ltp(row)
            if nb.bar_builder and ltp is not None:
                finalized = nb.bar_builder.ingest(ltp)
                if finalized:
                    # Closed bar: persist + overwrite OHLC from Kite REST for chart parity.
                    nb.adx_live_at = 0.0
                    self._refresh_adx_from_bars(nb)
                    self._persist_bars_async(nb)
                    self._schedule_adx_tail_refresh(nb)
                else:
                    now = time.monotonic()
                    if now - nb.adx_live_at >= ADX_LIVE_REFRESH_S:
                        nb.adx_live_at = now
                        self._refresh_adx_from_bars(nb, purge=False)
                    self._schedule_kite_adx_refresh(nb)
            
            # Update ATM if needed
            spot = ltp
            if spot is None:
                continue
            ref = spot
            if nb.atm is not None:
                ce_ltp = quote_ltp(self.book.get(nb.atm.ce_symbol))
                pe_ltp = quote_ltp(self.book.get(nb.atm.pe_symbol))
                ref = atm_ref_price(spot, float(nb.atm.strike), ce_ltp, pe_ltp)
            legs = resolve_atm_legs(
                nb.universe,
                spot,
                current_strike=nb.last_atm_strike,
                ref_price=ref,
            )
            if nb.last_atm_strike == legs.strike:
                continue
            nb.last_atm_strike = legs.strike
            nb.atm = legs
            nb.cached_atm_greeks_iv = None
            nb.cached_atm_greeks_strike = None
            self._sync_subscriptions(force=True)

    def _spot(self, nb: NotebookRuntime | None = None) -> float | None:
        """Get spot LTP for a notebook (defaults to nifty for back-compat)."""
        if nb is None:
            nb = self.notebooks.get("nifty")
        if nb is None:
            return None
        return quote_ltp(self.book.get(nb.config.symbol))

    def _bootstrap_symbols(self) -> list[str]:
        """Bootstrap symbols: header watch + VIX + all enabled notebook FUT symbols."""
        watch = self._header_watch or list(HEADER_WATCHLIST)
        symbols = [item["symbol"] for item in watch if item.get("symbol")]
        symbols.extend(VIX_SYMBOLS)
        for nb in self.notebooks.values():
            if nb.enabled and nb.universe:
                symbols.append(nb.universe.fut_symbol)
        return list(dict.fromkeys(symbols))

    def _build_indices(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        watch = self._header_watch or list(HEADER_WATCHLIST)
        for item in watch:
            symbol = item.get("symbol") or ""
            row = self.book.get(symbol) if symbol else None
            ltp = quote_ltp(row)
            chg = quote_change_pct(row)
            chg_pts = quote_change_pts(row)
            entry: dict[str, Any] = {
                "id": item["id"],
                "label": item["label"],
                "group": item.get("group") or "index",
            }
            if symbol:
                entry["symbol"] = symbol
            if ltp is not None:
                entry["ltp"] = ltp
            if chg_pts is not None:
                entry["chg_pts"] = chg_pts
            if chg is not None:
                entry["chg_pct"] = chg
            out.append(entry)
        return out

    @staticmethod
    def _merge_live_tail(
        bars: list[dict[str, Any]], live: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Overlay the forming/last builder bar onto a Kite closed series (in place).

        Same-minute → replace; newer → append; older/empty → untouched. Shared by
        the LOCKED ADX series and the paper VWAP cache so both trade one series.
        """
        if not live:
            return bars
        live_ts = str(live.get("t") or "")
        if not live_ts:
            return bars
        if bars and bars[-1]["t"] == live_ts:
            bars[-1] = live
        elif not bars or live_ts > bars[-1]["t"]:
            bars.append(live)
        return bars

    def _kite_adx_series_bars(self, nb: NotebookRuntime) -> list[dict[str, Any]]:
        """Kite REST history + WS tick OHLC on the forming minute (matches Kite app).

        LOCKED: final Kite-parity bar series for ADX/ATR/DMI — do not alter.
        """
        if not nb.bar_builder:
            return []
        if not nb.kite_adx_bars:
            return nb.bar_builder.chart_bars()
        bars = [dict(b) for b in nb.kite_adx_bars]
        live_tail = nb.bar_builder.chart_bars()
        if not live_tail:
            return bars
        return self._merge_live_tail(bars, dict(live_tail[-1]))

    def _bars_for_chart(self, nb: NotebookRuntime, limit: int) -> list[dict[str, Any]]:
        """Bars for chart + DMI (Kite REST closed, WS forming)."""
        return self._kite_adx_series_bars(nb)[-max(1, limit) :]

    @staticmethod
    def _bar_minute_key(ts: Any) -> str:
        return str(ts or "").replace("T", " ")[:16]

    @staticmethod
    def _builder_volume_oi(nb: NotebookRuntime) -> dict[str, tuple[float, float]]:
        """FUT volume/OI by minute — index REST candles carry neither."""
        out: dict[str, tuple[float, float]] = {}
        builder = nb.bar_builder
        if not builder:
            return out
        for src in builder.bars:
            ts = FeedEngine._bar_minute_key(src.get("t"))
            if ts:
                out[ts] = (float(src.get("v") or 0), float(src.get("oi") or 0))
        live = builder.forming_or_last_bar()
        if live:
            ts = FeedEngine._bar_minute_key(live.get("t"))
            if ts:
                out[ts] = (float(live.get("v") or 0), float(live.get("oi") or 0))
        return out

    def _bar_dict_to_candle(
        self,
        bar: dict[str, Any],
        vol_oi: dict[str, tuple[float, float]] | None = None,
    ) -> dict[str, Any] | None:
        minute = self._bar_minute_key(bar.get("t"))
        try:
            t = datetime.strptime(minute, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        except ValueError:
            return None
        volume = float(bar.get("v") or 0)
        oi = float(bar.get("oi") or 0)
        if vol_oi:
            src = vol_oi.get(minute)
            if src:
                if volume <= 0 and src[0] > 0:
                    volume = src[0]
                if oi <= 0 and src[1] > 0:
                    oi = src[1]
        return {
            "time": int(t.timestamp()),
            "open": float(bar["o"]),
            "high": float(bar["h"]),
            "low": float(bar["l"]),
            "close": float(bar["c"]),
            "volume": volume,
            "oi": oi,
        }

    def candles(
        self,
        nb: str = "nifty",
        limit: int = 800,
        since: int | None = None,
    ) -> dict[str, Any]:
        """1-minute OHLC from Kite for a notebook (same bars as ADX/ATR).

        When ``since`` is set (unix seconds of the client's last bar), return
        only bars with time >= since so the forming candle can update without
        re-sending the full window.
        """
        from atlas_lite.metrics import wilder_dmi_series

        runtime = self.notebooks.get(nb)
        if not runtime or not runtime.enabled:
            return {"ok": False, "error": f"Notebook {nb} not enabled"}
        
        series = self._kite_adx_series_bars(runtime)
        vol_oi = self._builder_volume_oi(runtime)
        raw = series[-max(1, limit) :]
        bars: list[dict[str, Any]] = []
        for bar in raw:
            candle = self._bar_dict_to_candle(bar, vol_oi)
            if candle is not None:
                bars.append(candle)
        all_candles: list[dict[str, Any]] = []
        for bar in series:
            candle = self._bar_dict_to_candle(bar, vol_oi)
            if candle is not None:
                all_candles.append(candle)
        if all_candles:
            highs = [float(b["high"]) for b in all_candles]
            lows = [float(b["low"]) for b in all_candles]
            closes = [float(b["close"]) for b in all_candles]
            pdi, mdi, adx = wilder_dmi_series(highs, lows, closes)
            dmi_by_time: dict[int, dict[str, float]] = {}
            for idx, candle in enumerate(all_candles):
                if adx[idx] is None:
                    continue
                entry: dict[str, float] = {"adx": float(adx[idx])}
                if pdi[idx] is not None:
                    entry["pdi"] = float(pdi[idx])
                if mdi[idx] is not None:
                    entry["mdi"] = float(mdi[idx])
                dmi_by_time[int(candle["time"])] = entry
            for candle in bars:
                dmi = dmi_by_time.get(int(candle["time"]))
                if not dmi:
                    continue
                candle.update(dmi)
        delta = since is not None
        if delta:
            bars = [b for b in bars if int(b["time"]) >= int(since)]
        return {
            "ok": True,
            "symbol": runtime.config.symbol,
            "label": runtime.config.label,
            "bars": bars,
            "delta": delta,
        }

    def nifty_candles(
        self,
        limit: int = 800,
        since: int | None = None,
    ) -> dict[str, Any]:
        """NIFTY 50 1-minute OHLC (back-compat wrapper for candles("nifty"))."""
        return self.candles("nifty", limit=limit, since=since)

    def live_chart_bars(self, nb: str = "nifty", limit: int = 2) -> list[dict[str, Any]]:
        """Last 1m bars (including forming) for SSE chart paint."""
        return list(self.candles(nb, limit=max(1, limit)).get("bars") or [])

    def _structure_snapshot(self, now: datetime) -> dict[str, Any] | None:
        """Once-a-minute ATM wings + next-week ATM for later strategy research."""
        nb = self.notebooks.get("nifty")
        if not nb or not nb.enabled or not nb.universe or not nb.atm:
            return None
        atm = int(nb.atm.strike)
        wings: dict[str, dict[str, Any]] = {}
        for pts in (50, 100, 250):
            for sign, key in ((-1, f"-{pts}"), (1, f"+{pts}")):
                strike = atm + sign * pts
                ce_sym = nb.universe.option_symbol(strike, "CE")
                pe_sym = nb.universe.option_symbol(strike, "PE")
                wings[key] = {
                    "strike": strike,
                    "ce_symbol": ce_sym,
                    "pe_symbol": pe_sym,
                    "ce": quote_ltp(self.book.get(ce_sym)),
                    "pe": quote_ltp(self.book.get(pe_sym)),
                }
        body: dict[str, Any] = {
            "ts": now.isoformat(timespec="seconds"),
            "day": now.strftime("%Y-%m-%d"),
            "hm": now.strftime("%H:%M"),
            "spot": self._spot(nb),
            "atm": atm,
            "expiry": nb.universe.expiry.isoformat(),
            "ce_symbol": nb.atm.ce_symbol,
            "pe_symbol": nb.atm.pe_symbol,
            "ce": quote_ltp(self.book.get(nb.atm.ce_symbol)),
            "pe": quote_ltp(self.book.get(nb.atm.pe_symbol)),
            "wings": wings,
        }
        nxt = nb.next_universe
        if nxt is not None:
            n_atm = round_atm_strike(atm, nxt.strike_step)
            n_ce = nxt.option_symbol(n_atm, "CE")
            n_pe = nxt.option_symbol(n_atm, "PE")
            body["next"] = {
                "expiry": nxt.expiry.isoformat(),
                "atm": n_atm,
                "ce_symbol": n_ce,
                "pe_symbol": n_pe,
                "ce": quote_ltp(self.book.get(n_ce)),
                "pe": quote_ltp(self.book.get(n_pe)),
            }
        return body

    def _sync_depth_watch(self) -> None:
        """Track NIFTY ATM±1 option symbols for per-tick depth recording."""
        nb = self.notebooks.get("nifty")
        watch: set[str] = set()
        meta: dict[str, dict[str, Any]] = {}
        if nb and nb.enabled and nb.universe and nb.atm:
            atm = int(nb.atm.strike)
            step = int(nb.universe.strike_step)
            expiry = nb.universe.expiry.isoformat()
            for offset in (-1, 0, 1):
                strike = atm + offset * step
                for side in ("CE", "PE"):
                    sym = nb.universe.option_symbol(strike, side)
                    watch.add(sym)
                    meta[sym] = {
                        "key": f"{offset:+d}{side}",
                        "strike": strike,
                        "atm": atm,
                        "step": step,
                        "expiry": expiry,
                    }
        if watch != self._depth_watch:
            self._depth_leg_rev.clear()
        self._depth_watch = watch
        self._depth_leg_meta = meta

    def _enqueue_depth_tick(self, symbol: str, row: dict[str, Any]) -> None:
        """Queue one ATM±1 leg tick (called from WS merge; drained by record loop)."""
        if not self._depth_recording or symbol not in self._depth_watch:
            return
        if not isinstance(row.get("depth"), dict):
            return
        meta = self._depth_leg_meta.get(symbol)
        if not meta:
            return
        tape = top_of_book(row)
        buy = tuple(tuple(level) for level in (tape.get("buy") or []))
        sell = tuple(tuple(level) for level in (tape.get("sell") or []))
        rev = (
            tape.get("ltp"),
            tape.get("bid"),
            tape.get("ask"),
            tape.get("bid_qty"),
            tape.get("ask_qty"),
            tape.get("buy_qty"),
            tape.get("sell_qty"),
            buy,
            sell,
            tape.get("exch_ts"),
        )
        if self._depth_leg_rev.get(symbol) == rev:
            return
        self._depth_leg_rev[symbol] = rev
        now = datetime.now(IST)
        if len(self._depth_queue) >= 50_000:
            self._depth_queue.popleft()
        self._depth_queue.append(
            {
                "ts": now.isoformat(timespec="milliseconds"),
                "ts_ms": int(now.timestamp() * 1000),
                "leg": meta["key"],
                "strike": meta["strike"],
                "symbol": symbol,
                "atm": meta["atm"],
                "step": meta["step"],
                "expiry": meta["expiry"],
                "spot": self._spot(self.notebooks.get("nifty")),
                **tape,
            }
        )

    def _metrics_chain(self, nb: NotebookRuntime) -> tuple[list[int], list[str], list[str]]:
        if nb.chain_cache is None and nb.universe:
            nb.chain_cache = full_chain_symbols(nb.universe, nb.fo_csv)
        return nb.chain_cache or ([], [], [])

    def _full_symbols(self) -> list[str]:
        """Union of bootstrap + all enabled notebooks' full chains (when ATM set)."""
        symbols = self._bootstrap_symbols()
        for nb in self.notebooks.values():
            if not nb.enabled or not nb.universe or not nb.atm:
                continue
            if nb.chain_cache:
                _strikes, ce_syms, pe_syms = nb.chain_cache
                symbols.extend([nb.atm.ce_symbol, nb.atm.pe_symbol, *ce_syms, *pe_syms])
            if nb.next_universe and nb.atm:
                nxt = round_atm_strike(nb.atm.strike, nb.next_universe.strike_step)
                symbols.extend(
                    [
                        nb.next_universe.option_symbol(nxt, "CE"),
                        nb.next_universe.option_symbol(nxt, "PE"),
                    ]
                )
        return list(dict.fromkeys(symbols))

    def _sync_subscriptions(self, *, force: bool = False) -> None:
        """Subscribe to union of bootstrap + all enabled notebooks' full chains."""
        if not self._token_index:
            return
        # Always start with bootstrap
        symbols = self._bootstrap_symbols()
        # If any notebook has ATM set, use full union
        has_atm = any(nb.atm for nb in self.notebooks.values() if nb.enabled)
        if has_atm:
            symbols = self._full_symbols()
        elif force:
            # Force ATM resolution for all enabled notebooks
            for nb in self.notebooks.values():
                if not nb.enabled or not nb.universe:
                    continue
                spot = self._spot(nb)
                if spot is not None:
                    legs = resolve_atm_legs(nb.universe, spot)
                    nb.last_atm_strike = legs.strike
                    nb.atm = legs
            symbols = self._full_symbols()
        token_map = token_map_for_symbols(self._token_index, symbols)
        if token_map and token_map != self.token_map:
            self.token_map = token_map
            if self.ticker:
                self.ticker.set_symbols(token_map)
        self._sync_depth_watch()

    def _schedule_adx_tail_refresh(self, nb: NotebookRuntime) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if nb.adx_tail_task is not None and not nb.adx_tail_task.done():
            return
        nb.adx_tail_task = loop.create_task(self._refresh_adx_tail_from_kite(nb))

    async def _refresh_adx_tail_from_kite(self, nb: NotebookRuntime, tail: int = ADX_TAIL_BARS) -> None:
        """Replace recent closed 1m bars with Kite REST candles (chart parity)."""
        token = lookup_token(self._token_index, nb.config.symbol)
        if token is None:
            self._refresh_adx_from_bars(nb)
            return
        now = datetime.now(IST)
        if not in_adx_seed_window(now):
            self._refresh_adx_from_bars(nb)
            return
        frm_tail = (now - timedelta(minutes=tail + 5)).strftime("%Y-%m-%d %H:%M:%S")
        frm_full = (now - timedelta(days=ADX_REST_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        to = now.strftime("%Y-%m-%d %H:%M:%S")
        floor = kite_adx_window_start(now, days=ADX_REST_DAYS)
        try:
            candles = await self.rest.historical_minute(
                token,
                from_date=frm_full,
                to_date=to,
            )
            if candles:
                self._rebuild_kite_adx_bars(nb, candles)
                merged, synced = self._apply_kite_bar_authority(
                    nb,
                    candles,
                    window_floor=floor,
                    tail_from=frm_tail[:16],
                )
                vol_updated = await self._attach_fut_volume(nb, frm_tail, to)
                if merged or synced or vol_updated:
                    self._persist_bars_async(nb)
                    self._log.info(
                        "%s ADX tail refreshed from Kite merged=%d synced=%d vol=%d adx_bars=%d",
                        nb.id.upper(),
                        merged,
                        synced,
                        vol_updated,
                        len(nb.kite_adx_bars),
                    )
                # Tail success means historical API is healthy — count as today's seed
                # so we don't thrash a full 3-day reseed every maintenance cycle.
                if nb.adx_kite_seed_day != self._today() and self._adx_bars_fresh(nb):
                    nb.adx_kite_seed_day = self._today()
                    nb.adx_bars_day = self._today()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("%s ADX tail refresh failed: %s", nb.id.upper(), exc)
            # Historical down (e.g. 403) — clear kite seed so we reseed when API recovers.
            if "403" in str(exc):
                nb.adx_kite_seed_day = ""
        self._refresh_adx_from_bars(nb)

    async def _attach_fut_volume(self, nb: NotebookRuntime, from_date: str, to_date: str) -> int:
        """Fill zero index volume from notebook's FUT 1m candles."""
        if not nb.bar_builder or not nb.universe:
            return 0
        token = lookup_token(self._token_index, nb.universe.fut_symbol)
        if token is None:
            return 0
        try:
            candles = await self.rest.historical_minute(
                token,
                from_date=from_date,
                to_date=to_date,
                oi=1,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("%s FUT volume merge failed: %s", nb.id.upper(), exc)
            return 0
        if not candles:
            return 0
        updated = nb.bar_builder.merge_volume_from_candles(candles)
        if updated:
            self._log.info("%s FUT volume merged bars=%d", nb.id.upper(), updated)
        return updated

    async def _iv_greeks_loop(self) -> None:
        """Overlay Kite REST greeks.iv on ATM legs for all enabled notebooks."""
        while True:
            try:
                await self._refresh_atm_greeks_from_kite()
            except Exception as exc:  # noqa: BLE001
                self._log.warning("ATM greeks refresh failed: %s", exc)
            await asyncio.sleep(IV_GREEKS_REFRESH_S)

    async def _refresh_atm_greeks_from_kite(self) -> None:
        """Refresh greeks for all enabled notebooks' ATM legs."""
        all_symbols: list[str] = []
        for nb in self.notebooks.values():
            if nb.enabled and nb.atm:
                all_symbols.extend([nb.atm.ce_symbol, nb.atm.pe_symbol])
        if not all_symbols:
            return
        raw = await self.rest.quote(all_symbols)
        quotes = normalize_quote_map(raw)
        for sym in all_symbols:
            row = quotes.get(sym)
            if not isinstance(row, dict):
                continue
            overlay: dict[str, Any] = {}
            greeks = row.get("greeks")
            if isinstance(greeks, dict) and greeks.get("iv") is not None:
                overlay["greeks"] = greeks
            oi = row.get("oi")
            if oi is not None:
                overlay["oi"] = oi
                overlay["open_interest"] = row.get("open_interest", oi)
            if overlay:
                self.book.merge(sym, overlay)
        # Update cached IV for each notebook
        for nb in self.notebooks.values():
            if not nb.enabled or not nb.atm:
                continue
            ce_row = quotes.get(nb.atm.ce_symbol)
            pe_row = quotes.get(nb.atm.pe_symbol)
            greeks_iv = atm_greeks_iv(
                ce_row if isinstance(ce_row, dict) else None,
                pe_row if isinstance(pe_row, dict) else None,
            )
            if greeks_iv is not None:
                nb.cached_atm_greeks_iv = greeks_iv
                nb.cached_atm_greeks_strike = nb.atm.strike

    def _chain_greeks_symbols(self, runtime: NotebookRuntime) -> list[str]:
        strikes, ce_syms, pe_syms = self._metrics_chain(runtime)
        if not runtime.atm:
            return []
        step = runtime.universe.strike_step if runtime.universe else 50
        lo = runtime.atm.strike - CHAIN_GREEKS_WINGS * step
        hi = runtime.atm.strike + CHAIN_GREEKS_WINGS * step
        out: list[str] = []
        for strike, ce_sym, pe_sym in zip(strikes, ce_syms, pe_syms):
            if lo <= strike <= hi:
                out.extend([ce_sym, pe_sym])
        return out

    def _merge_quote_overlay(self, symbol: str, row: dict[str, Any]) -> None:
        """REST overlay for greeks/volume/OI only.

        Never write depth/bid/ask here — full-mode WS already owns those fields
        for NFO/BFO, and stale REST snapshots would corrupt the depth tape.
        """
        overlay: dict[str, Any] = {}
        greeks = row.get("greeks")
        if isinstance(greeks, dict) and (
            greeks.get("iv") is not None or greeks.get("delta") is not None
        ):
            overlay["greeks"] = greeks
        vol = quote_volume(row)
        if vol is not None:
            overlay["volume"] = vol
        oi = row.get("oi")
        if oi is not None:
            overlay["oi"] = oi
            overlay["open_interest"] = row.get("open_interest", oi)
        if overlay:
            self.book.merge(symbol, overlay)

    async def ensure_chain_greeks(self, nb: str) -> None:
        """REST greeks/depth for ATM±wings. Throttled; only while extras are shown.

        Stamp the throttle before the Kite call and swallow REST errors so
        ``/api/chain?extras=1`` still serves the websocket chain.
        """
        now = time.time()
        last = self._chain_greeks_at.get(nb, 0.0)
        if now - last < CHAIN_GREEKS_REFRESH_S:
            return
        runtime = self.notebooks.get(nb)
        if not runtime or not runtime.enabled or not runtime.atm:
            return
        symbols = self._chain_greeks_symbols(runtime)
        if not symbols:
            return
        self._chain_greeks_at[nb] = now
        try:
            raw = await self.rest.quote(symbols)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("chain greeks REST failed (%s); serving WS chain", exc)
            return
        quotes = normalize_quote_map(raw)
        for sym in symbols:
            row = quotes.get(sym)
            if isinstance(row, dict):
                self._merge_quote_overlay(sym, row)

    async def _maintenance_loop(self) -> None:
        """Daily instruments/IVP refresh only — ADX updates from WS minute bars."""
        while True:
            warnings: list[str] = []
            try:
                await self._reload_instruments_if_needed()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Instruments refresh failed: {exc}")
            if self._kite_auth_error:
                await self._try_recover_kite_credentials()
            try:
                await self._refresh_iv_history_if_stale()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"IVP history refresh failed: {exc}")
            try:
                await self._maybe_reseed_adx_bars()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"ADX bar seed failed: {exc}")
            # Tail refresh for all enabled notebooks
            for nb in self.notebooks.values():
                if nb.enabled:
                    try:
                        await self._refresh_adx_tail_from_kite(nb)
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"{nb.id.upper()} ADX tail refresh failed: {exc}")
            # Shared maintenance issues only — do not accumulate stale feed warnings
            # (WS blips during dual-chain resubscribe used to stick forever).
            for nb in self.notebooks.values():
                if nb.enabled:
                    nb.adx_warnings = list(warnings)
            try:
                # Off the event loop — compressing a backlog can take minutes.
                await asyncio.to_thread(maintain_recordings, record_dir(self.data_dir))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Recording maintain failed: {exc}")
            await asyncio.sleep(MAINTENANCE_LOOP_S)

    def _refresh_adx_from_bars(self, nb: NotebookRuntime, *, purge: bool = True) -> None:
        """ADX/ATR/+DI/-DI from Kite REST 1m bars (matches Kite chart; WS bars are chart-only)."""
        from atlas_lite.metrics import wilder_dmi_series

        if not nb.bar_builder:
            return
        if purge:
            nb.bar_builder.drop_non_session_bars()
        series = self._kite_adx_series_bars(nb)
        highs, lows, closes = ohlc_from_bar_dicts(series)
        if len(closes) < MIN_BARS_FOR_ADX:
            return
        adx = compute_adx(highs, lows, closes)
        atr = compute_atr(highs, lows, closes)
        pdi_s, mdi_s, _adx_s = wilder_dmi_series(highs, lows, closes)
        pdi = next((v for v in reversed(pdi_s) if v is not None), None)
        mdi = next((v for v in reversed(mdi_s) if v is not None), None)
        bar_count = len(closes)
        hint = f"{nb.config.symbol} 1m · {bar_count} bars · Kite+live"
        changed = False
        if adx is not None and adx != nb.adx:
            nb.adx = adx
            self._log.info("%s derived adx=%.2f bars=%d", nb.id.upper(), adx, bar_count)
            changed = True
        if atr is not None and atr != nb.atr:
            nb.atr = atr
            self._log.info("%s derived atr=%.2f bars=%d", nb.id.upper(), atr, bar_count)
            changed = True
        if pdi != nb.pdi:
            nb.pdi = float(pdi) if pdi is not None else None
            changed = True
        if mdi != nb.mdi:
            nb.mdi = float(mdi) if mdi is not None else None
            changed = True
        if hint != nb.adx_hint:
            nb.adx_hint = hint
            changed = True
        if changed:
            self.book.notify_update()

    async def _record_loop(self) -> None:
        """Append JSONL whenever any UI-visible metric changes (tick-driven, sub-second)."""
        if self._recorder is None:
            return
        interval = STREAM_INTERVAL_MS / 1000.0
        closed_sleep_s = 30.0
        while True:
            try:
                now = datetime.now(IST)
                self._depth_recording = is_record_session(now)
                if not self._depth_recording:
                    self._record_last_revision = None
                    self._depth_queue.clear()
                    self._depth_leg_rev.clear()
                    await asyncio.sleep(closed_sleep_s)
                    continue

                frame = self.build_frame()
                feed = frame.get("feed") or {}
                frame["evaluation"] = evaluate_sheet(feed)
                # strategy_hint already set inside build_frame
                rev = frame_revision(frame)
                if rev != self._record_last_revision:
                    self._record_last_revision = rev
                    await self._recorder.append(frame)
                if self._depth_queue:
                    batch = list(self._depth_queue)
                    self._depth_queue.clear()
                    await self._recorder.append_depth_batch(batch, now=now)
                hm = now.strftime("%H:%M")
                if hm != self._structure_hm:
                    snap = self._structure_snapshot(now)
                    if snap is not None:
                        await self._recorder.append_structure(snap, now=now)
                        self._structure_hm = hm
                seen_seq = self.book.tick_seq
                await self.book.wait_for_update(seen_seq, interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("sheet record failed: %s", exc)
                await asyncio.sleep(1.0)

    async def _paper_loop(self) -> None:
        """Paper Rich-IV iron fly only. No broker orders. Bell stays notebook."""
        if self._paper is None:
            return
        interval = STREAM_INTERVAL_MS / 1000.0
        while True:
            try:
                now = datetime.now(IST)
                if now.weekday() >= 5 and self._paper.position is None:
                    await asyncio.sleep(30.0)
                    continue
                frame = self.build_frame()
                feed = frame.get("feed") or {}
                if not isinstance(feed, dict):
                    feed = {}
                regime = evaluate_paper_regime(feed, now=now)
                strategy = regime.get("strategy")
                allow = True
                if self._agent_gates is not None:
                    allow = self._agent_gates.entries_allowed("iron_fly", now)
                atm = frame.get("atm_strike")
                wing_ce = wing_pe = None
                if self.universe is not None and atm is not None:
                    pe_k, ce_k = iron_fly_strikes(int(atm), wing_pts=FLY_WING_PTS)
                    wing_pe = self.universe.option_symbol(pe_k, "PE")
                    wing_ce = self.universe.option_symbol(ce_k, "CE")
                self._paper.on_frame(
                    now=now,
                    entry_ready=strategy is not None and allow,
                    strategy=strategy if allow else None,
                    feed=feed,
                    book=self.book,
                    ce_symbol=frame.get("ce_symbol"),
                    pe_symbol=frame.get("pe_symbol"),
                    atm=atm,
                    wing_ce_symbol=wing_ce,
                    wing_pe_symbol=wing_pe,
                    wing_pts=FLY_WING_PTS,
                )
                seen_seq = self.book.tick_seq
                await self.book.wait_for_update(seen_seq, interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("paper straddle failed: %s", exc)
                await asyncio.sleep(1.0)

    def _paper_vwap_bars(
        self,
        nb: NotebookRuntime,
        cache: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Kite history cached by kite+live identity; forming bar re-merged every call.

        Identity always includes the live chart fingerprint so a *stale* non-empty
        kite snapshot still rebuilds as bar_builder grows — without calling
        chart_bars() (full copy) on every tick just to build the key.
        """
        if not nb.bar_builder:
            cache["bars"] = []
            cache["series_key"] = ""
            return []
        live_fp = nb.bar_builder.chart_series_fingerprint()
        # kite_adx_rev bumps on any REST payload change; the live fingerprint
        # covers builder mutations. Nothing else can change the series.
        series_key = f"{nb.kite_adx_rev}|{live_fp}"
        if series_key != cache.get("series_key") or not cache.get("bars"):
            cache["bars"] = self._kite_adx_series_bars(nb)
            cache["series_key"] = series_key
        bars = list(cache.get("bars") or [])
        return self._merge_live_tail(bars, nb.bar_builder.forming_or_last_bar())

    @staticmethod
    def _paper_vwap_skip_key(
        series_key: Any,
        spot: float | None,
        clock_minute: str,
        open_book: bool,
        tail: dict[str, Any] | None,
    ) -> tuple[Any, ...]:
        """Tick-skip identity for the paper VWAP scan.

        Forming 1m t/h/l is in the key because chart_series_fingerprint only
        hashes the last *closed* bar. A wick that tags −0.5σ / −1σ and recovers
        to the same LTP would otherwise wait until the next clock minute — and
        miss a 5m bucket that just closed, since on_bars only inspects the
        newest bucket.
        """
        wick = None if tail is None else (tail.get("t"), tail.get("h"), tail.get("l"))
        return (series_key, spot, clock_minute, open_book, wick)

    async def _paper_vwap_loop(self) -> None:
        """Paper Session-VWAP long only. Separate ledger from iron fly. No broker orders."""
        if self._paper_vwap is None:
            return
        # True ~1 Hz cadence (wait_for_update alone wakes on every tick_seq).
        interval = max(1.0, STREAM_INTERVAL_MS / 1000.0)
        bars_cache: dict[str, Any] = {"bars": [], "series_key": ""}
        last_run: tuple[Any, ...] | None = None

        def _nifty_bars_spot() -> tuple[list[dict[str, Any]], float | None]:
            nifty_nb = self.notebooks.get("nifty")
            if nifty_nb and nifty_nb.enabled:
                return self._paper_vwap_bars(nifty_nb, bars_cache), self._spot(nifty_nb)
            return [], None

        while True:
            try:
                now = datetime.now(IST)
                if now.weekday() >= 5:
                    # Weekend: try one flatten if a book is still open, then idle.
                    if self._paper_vwap.position is not None:
                        bars, spot = _nifty_bars_spot()
                        self._paper_vwap.on_bars(now=now, bars=bars, spot=spot)
                    await asyncio.sleep(30.0)
                    continue
                bars, spot = _nifty_bars_spot()
                # Skip aggregate/scan when series, spot, and clock minute are unchanged
                # (still wake on minute rollover for square-off / entry window).
                run_key = self._paper_vwap_skip_key(
                    bars_cache.get("series_key"),
                    spot,
                    now.strftime("%Y-%m-%d %H:%M"),
                    self._paper_vwap.position is not None,
                    bars[-1] if bars else None,
                )
                if run_key != last_run:
                    self._paper_vwap.on_bars(now=now, bars=bars, spot=spot)
                    last_run = run_key
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("paper VWAP long failed: %s", exc)
                await asyncio.sleep(1.0)

    def _paper_option_ctx(self) -> tuple[dict[str, Any], str | None, str | None, int | None]:
        """Thin ATM + NIFTY % change for the 14:00 / 11:00 books (no full frame)."""
        nb = self.notebooks.get("nifty")
        if not nb or not nb.enabled:
            return {}, None, None, None
        ce = pe = None
        atm = None
        if nb.atm:
            ce, pe, atm = nb.atm.ce_symbol, nb.atm.pe_symbol, int(nb.atm.strike)
        feed: dict[str, Any] = {
            "index_nifty_chg": quote_change_pct(self.book.get(nb.config.symbol)),
        }
        if ce and pe:
            feed["ce_symbol"] = ce
            feed["pe_symbol"] = pe
            feed["ce"] = quote_ltp(self.book.get(ce))
            feed["pe"] = quote_ltp(self.book.get(pe))
        return feed, ce, pe, atm

    async def _paper_short_loop(self) -> None:
        """Paper 14:00 short ATM straddle. Separate ledger. No broker orders.

        Independent of iron fly / skew / theta / Short IC — agent policy gates only.
        """
        if self._paper_short is None:
            return
        # True ~1 Hz cadence (wait_for_update alone wakes on every tick_seq).
        interval = max(1.0, STREAM_INTERVAL_MS / 1000.0)
        while True:
            try:
                now = datetime.now(IST)
                if now.weekday() >= 5 and self._paper_short.position is None:
                    await asyncio.sleep(30.0)
                    continue
                feed, ce, pe, atm = self._paper_option_ctx()
                # Independent of iron fly / skew / other short-vol books.
                allow = True
                if self._agent_gates is not None:
                    allow = self._agent_gates.entries_allowed("short_straddle", now)
                self._paper_short.on_frame(
                    now=now,
                    feed=feed,
                    book=self.book,
                    ce_symbol=ce,
                    pe_symbol=pe,
                    atm=atm,
                    allow_entry=allow,
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("paper short ATM straddle failed: %s", exc)
                await asyncio.sleep(1.0)

    async def _paper_skew_loop(self) -> None:
        """Paper 11:00 ATM skew fade (sell rich wing). Separate ledger. No broker orders."""
        if self._paper_skew is None:
            return
        # True ~1 Hz cadence (wait_for_update alone wakes on every tick_seq).
        interval = max(1.0, STREAM_INTERVAL_MS / 1000.0)
        while True:
            try:
                now = datetime.now(IST)
                if now.weekday() >= 5 and self._paper_skew.position is None:
                    await asyncio.sleep(30.0)
                    continue
                feed, ce, pe, atm = self._paper_option_ctx()
                allow = self._agent_gates.entries_allowed("skew_fade", now) if self._agent_gates else True
                self._paper_skew.on_frame(
                    now=now,
                    feed=feed,
                    book=self.book,
                    ce_symbol=ce,
                    pe_symbol=pe,
                    atm=atm,
                    allow_new_entries=allow,
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("paper ATM skew fade failed: %s", exc)
                await asyncio.sleep(1.0)

    async def _paper_lic_loop(self) -> None:
        """Paper 09:15 long iron condor (red vertical first). Separate ledger."""
        if self._paper_lic is None:
            return
        interval = max(1.0, STREAM_INTERVAL_MS / 1000.0)
        while True:
            try:
                now = datetime.now(IST)
                if now.weekday() >= 5 and self._paper_lic.position is None:
                    await asyncio.sleep(30.0)
                    continue
                feed, _, _, atm = self._paper_option_ctx()
                symbols: dict[str, str] = {}
                option_symbol = None
                nb = self.notebooks.get("nifty")
                if nb and nb.universe:
                    option_symbol = nb.universe.option_symbol
                    feed["expiry"] = nb.universe.expiry
                    feed["spot"] = self._spot(nb)
                    if nb.cached_atm_greeks_iv is not None:
                        feed["iv"] = nb.cached_atm_greeks_iv
                    if atm is not None:
                        pe_s, pe_l, ce_l, ce_s = long_iron_condor_strikes(int(atm))
                        symbols = {
                            "pe_short": nb.universe.option_symbol(pe_s, "PE"),
                            "pe_long": nb.universe.option_symbol(pe_l, "PE"),
                            "ce_long": nb.universe.option_symbol(ce_l, "CE"),
                            "ce_short": nb.universe.option_symbol(ce_s, "CE"),
                        }
                self._paper_lic.on_frame(
                    now=now,
                    feed=feed,
                    book=self.book,
                    atm=atm,
                    symbols=symbols,
                    option_symbol=option_symbol,
                    allow_new_entries=(
                        self._agent_gates.entries_allowed("long_iron_condor", now)
                        if self._agent_gates
                        else True
                    ),
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("paper long iron condor failed: %s", exc)
                await asyncio.sleep(1.0)

    def _vix_yesterday(self, now: datetime | None = None) -> float | None:
        """Prior session VIX from disk (freshness-checked). No silent today-open relabel."""
        today = (now or datetime.now(IST)).strftime("%Y-%m-%d")
        prev = load_vix_prev(self.data_dir / VIX_PREV_FILE, today=today)
        if prev is not None and prev > 0:
            return prev
        return None

    async def _paper_short_ic_loop(self) -> None:
        """Paper short iron condor (credit 4–5/side, 1% TP, 4× set stop). No broker orders."""
        if self._paper_short_ic is None:
            return
        interval = max(1.0, STREAM_INTERVAL_MS / 1000.0)
        while True:
            try:
                now = datetime.now(IST)
                if now.weekday() >= 5 and self._paper_short_ic.position is None:
                    await asyncio.sleep(30.0)
                    continue
                feed, _, _, atm = self._paper_option_ctx()
                option_symbol = None
                nb = self.notebooks.get("nifty")
                if nb and nb.universe:
                    option_symbol = nb.universe.option_symbol
                    feed["expiry"] = nb.universe.expiry
                    feed["spot"] = self._spot(nb)
                # Independent of iron fly / theta / skew short-vol stack.
                allow = True
                block: str | None = None
                if self._agent_gates is not None and not self._agent_gates.entries_allowed(
                    "short_iron_condor", now
                ):
                    allow = False
                    block = "policy_gate"
                self._paper_short_ic.on_frame(
                    now=now,
                    feed=feed,
                    book=self.book,
                    atm=atm,
                    option_symbol=option_symbol,
                    allow_entry=allow,
                    block_reason=block,
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("paper short iron condor failed: %s", exc)
                await asyncio.sleep(1.0)

    async def _paper_theta_loop(self) -> None:
        """Paper expiry-day 12:00 theta-cliff fence. Separate ledger. No broker orders."""
        if self._paper_theta is None:
            return
        interval = max(1.0, STREAM_INTERVAL_MS / 1000.0)
        while True:
            try:
                now = datetime.now(IST)
                if now.weekday() >= 5 and self._paper_theta.position is None:
                    await asyncio.sleep(30.0)
                    continue
                feed, _, _, _atm = self._paper_option_ctx()
                option_symbol = None
                bars_1m: list[dict[str, Any]] = []
                nb = self.notebooks.get("nifty")
                if nb and nb.universe:
                    option_symbol = nb.universe.option_symbol
                    feed["expiry"] = nb.universe.expiry
                    feed["spot"] = self._spot(nb)
                    if nb.bar_builder:
                        bars_1m = list(nb.bar_builder.bars)
                vix = quote_ltp(self.book.get(VIX_SYMBOLS[0])) if VIX_SYMBOLS else None
                if vix is not None:
                    self.session.vix_last = float(vix)
                    feed["vix"] = round(float(vix), 3)
                vy = self._vix_yesterday(now)
                if vy is not None:
                    feed["vix_yesterday"] = round(float(vy), 4)
                # Independent of iron fly / Short IC / skew short-vol stack.
                allow = True
                block: str | None = None
                if self._agent_gates is not None and not self._agent_gates.entries_allowed(
                    "theta_cliff", now
                ):
                    allow = False
                    block = "policy_gate"
                if block:
                    feed["entry_block"] = block
                self._paper_theta.on_frame(
                    now=now,
                    feed=feed,
                    book=self.book,
                    bars_1m=bars_1m,
                    option_symbol=option_symbol,
                    allow_entry=allow,
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("paper theta-cliff fence failed: %s", exc)
                await asyncio.sleep(1.0)

    def _paper_scalp_closes(self, now: datetime | None = None) -> list[float]:
        """Today's closed 1m NIFTY closes. Forming bar is not part of the impulse."""
        nb = self.notebooks.get("nifty")
        if not nb or not nb.bar_builder:
            return []
        now = now or datetime.now(IST)
        return session_spot_closes(
            nb.bar_builder.bars[-24:],
            now.strftime("%Y-%m-%d"),
        )

    def _paper_scalp_signal_minute(self, now: datetime | None = None) -> str:
        nb = self.notebooks.get("nifty")
        if not nb or not nb.bar_builder:
            return ""
        now = now or datetime.now(IST)
        return last_session_bar_minute(
            nb.bar_builder.bars[-24:],
            now.strftime("%Y-%m-%d"),
        )

    async def _paper_scalp_loop(self) -> None:
        """Paper 3-minute ATM impulse fade (long opposite wing). No broker orders."""
        if self._paper_scalp is None:
            return
        interval = max(1.0, STREAM_INTERVAL_MS / 1000.0)
        while True:
            try:
                now = datetime.now(IST)
                if now.weekday() >= 5 and self._paper_scalp.position is None:
                    await asyncio.sleep(30.0)
                    continue
                feed, ce, pe, atm = self._paper_option_ctx()
                # Manual/agent gates only — do not auto-disable when agent is open.
                allow = (
                    self._agent_gates.entries_allowed("impulse_fade", now)
                    if self._agent_gates
                    else True
                )
                self._paper_scalp.on_frame(
                    now=now,
                    feed=feed,
                    book=self.book,
                    ce_symbol=ce,
                    pe_symbol=pe,
                    atm=atm,
                    spot_closes=self._paper_scalp_closes(now),
                    signal_minute=self._paper_scalp_signal_minute(now),
                    allow_new_entries=allow,
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("paper ATM impulse fade failed: %s", exc)
                await asyncio.sleep(1.0)

    def _paper_combo_closed_bars(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Kite 1m series without the forming minute (COMBO votes on closed bars)."""
        nb = self.notebooks.get("nifty")
        if not nb or not nb.bar_builder:
            return []
        now = now or datetime.now(IST)
        current = now.strftime("%Y-%m-%d %H:%M")
        bars = self._kite_adx_series_bars(nb)
        return [
            b
            for b in bars
            if str(b.get("t") or "").replace("T", " ")[:16] < current
        ]

    def _cached_combo_row(self, now: datetime) -> tuple[dict[str, Any] | None, str]:
        """Recompute confluence only when the closed-bar tip changes."""
        from atlas_lite.combo import last_day_combo

        bars = self._paper_combo_closed_bars(now)
        day = now.strftime("%Y-%m-%d")
        tip = str(bars[-1].get("t") or "") if bars else ""
        key = (day, tip, len(bars))
        if key == self._combo_cache_key:
            return self._combo_cache_row, tip
        row = last_day_combo(bars, day) if bars else None
        self._combo_cache_key = key
        self._combo_cache_row = row
        return row, tip

    async def _paper_combo_loop(self) -> None:
        """Paper 1m COMBO confluence (long CE on B / PE on S). No broker orders."""
        if self._paper_combo is None:
            return
        interval = max(1.0, STREAM_INTERVAL_MS / 1000.0)
        while True:
            try:
                now = datetime.now(IST)
                if now.weekday() >= 5 and self._paper_combo.position is None:
                    await asyncio.sleep(30.0)
                    continue
                feed, ce, pe, atm = self._paper_option_ctx()
                # Manual/agent gates only — do not auto-disable when agent is open.
                allow = (
                    self._agent_gates.entries_allowed("combo", now)
                    if self._agent_gates
                    else True
                )
                combo_row, signal_minute = self._cached_combo_row(now)
                self._paper_combo.on_frame(
                    now=now,
                    feed=feed,
                    book=self.book,
                    ce_symbol=ce,
                    pe_symbol=pe,
                    atm=atm,
                    combo=combo_row,
                    signal_minute=signal_minute or None,
                    allow_new_entries=allow,
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("paper COMBO confluence failed: %s", exc)
                await asyncio.sleep(1.0)

    async def _paper_ict_loop(self) -> None:
        """Paper ICT ATM CE/PE from 15m bias + 5m sweep/FVG. No broker orders."""
        if self._paper_ict is None:
            return
        interval = max(1.0, STREAM_INTERVAL_MS / 1000.0)
        while True:
            try:
                now = datetime.now(IST)
                if now.weekday() >= 5 and self._paper_ict.position is None:
                    await asyncio.sleep(30.0)
                    continue
                feed, ce, pe, atm = self._paper_option_ctx()
                allow = (
                    self._agent_gates.entries_allowed("ict", now)
                    if self._agent_gates
                    else True
                )
                nb = self.notebooks.get("nifty")
                bars: list[dict[str, Any]] = []
                spot = None
                if nb is not None:
                    spot = self._spot(nb)
                    if spot is not None:
                        feed["spot"] = spot
                    bars = self._paper_combo_closed_bars(now) if nb.bar_builder else []
                self._paper_ict.on_frame(
                    now=now,
                    feed=feed,
                    book=self.book,
                    ce_symbol=ce,
                    pe_symbol=pe,
                    atm=atm,
                    bars_1m=bars,
                    spot=spot,
                    allow_new_entries=allow,
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("paper ICT failed: %s", exc)
                await asyncio.sleep(1.0)

    async def _paper_agent_loop(self) -> None:
        """Execute validated agent CE/PE intents. No broker orders."""
        if self._paper_agent is None:
            return
        interval = max(1.0, STREAM_INTERVAL_MS / 1000.0)
        while True:
            try:
                now = datetime.now(IST)
                if now.weekday() >= 5 and self._paper_agent.position is None:
                    await asyncio.sleep(30.0)
                    continue
                feed, ce, pe, atm = self._paper_option_ctx()
                allow = (
                    self._agent_gates.entries_allowed("agent", now)
                    if self._agent_gates
                    else True
                )
                nifty_nb = self.notebooks.get("nifty")
                self._paper_agent.on_frame(
                    now=now,
                    feed=feed,
                    book=self.book,
                    ce_symbol=ce,
                    pe_symbol=pe,
                    atm=atm,
                    allow_new_entries=allow,
                    spot=self._spot(nifty_nb) if nifty_nb else None,
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("paper agent book failed: %s", exc)
                await asyncio.sleep(1.0)

    async def _agent_loop(self) -> None:
        """Periodic agent advise (scorecard autopilot or LLM; not every tick)."""
        if self._agent is None:
            return
        while True:
            try:
                if self._agent.due():
                    await asyncio.to_thread(self._agent.advise)
                await asyncio.sleep(15.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("agent advise loop failed: %s", exc)
                await asyncio.sleep(30.0)

    def _run_book_policy(self, now: datetime | None = None) -> dict[str, Any]:
        """Regime allocation + kill switches onto agent_gates (manual wins)."""
        if self._agent_gates is None:
            return {"ok": False, "error": "gates_missing"}
        now = now or datetime.now(IST)
        adx_val = self.adx
        feed = self.build_feed("nifty") if "nifty" in self.notebooks else {}
        if isinstance(feed, dict) and feed.get("adx") is not None:
            try:
                adx_val = float(feed["adx"])
            except (TypeError, ValueError):
                pass
        if adx_val is not None and adx_val >= ADX_TREND:
            adx_regime = "trend"
        elif adx_val is not None and adx_val < ADX_RANGE:
            adx_regime = "range"
        else:
            adx_regime = "mixed"
        out = apply_book_policy(
            self._agent_gates,
            data_dir=self.data_dir,
            adx=adx_val,
            adx_regime=adx_regime,
            now=now,
        )
        self._last_book_policy = out
        if out.get("applied"):
            self._log.info(
                "book policy regime=%s applied=%s",
                out.get("regime"),
                [a.get("book") for a in out["applied"]],
            )
        return out

    async def _book_policy_loop(self) -> None:
        """Apply lean regime→books policy about once a minute."""
        while True:
            try:
                await asyncio.to_thread(self._run_book_policy)
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("book policy loop failed: %s", exc)
                await asyncio.sleep(60.0)

    def _agent_tape_ready(self) -> bool:
        nifty_nb = self.notebooks.get("nifty")
        spot = self._spot(nifty_nb) if nifty_nb else None
        feed, ce_sym, pe_sym, atm = self._paper_option_ctx()
        ce = feed.get("ce")
        pe = feed.get("pe")
        return spot is not None and atm is not None and ce is not None and pe is not None

    def _agent_market_context(self) -> dict[str, Any]:
        now = datetime.now(IST)
        nifty_nb = self.notebooks.get("nifty")
        feed = self.build_feed("nifty") if nifty_nb else {}
        frame = self.build_frame("nifty") if nifty_nb else {}
        spot = self._spot(nifty_nb) if nifty_nb else None
        atm = frame.get("atm_strike") if isinstance(frame, dict) else None
        ce = feed.get("ce")
        pe = feed.get("pe")
        skew = None
        if ce is not None and pe is not None:
            try:
                skew = round(float(ce) - float(pe), 2)
            except (TypeError, ValueError):
                skew = None
        square = datetime(now.year, now.month, now.day, 15, 14, tzinfo=IST)
        mins = max(0, int((square - now).total_seconds() // 60))
        paper = self.paper_snapshot()
        combo = (paper.get("combo") or {}) if isinstance(paper, dict) else {}
        impulse = (paper.get("impulse_fade") or {}) if isinstance(paper, dict) else {}
        chain = frame.get("chain") if isinstance(frame, dict) else None
        pcr = feed.get("pcr")
        max_pain = feed.get("max_pain")
        if isinstance(chain, dict):
            pcr = pcr if pcr is not None else chain.get("pcr")
            max_pain = max_pain if max_pain is not None else chain.get("max_pain")
        # Market tape cues always (from bars). Book pause only stops that book's trades.
        combo_entries_on = self._paper_combo is not None and (
            self._agent_gates is None or self._agent_gates.entries_allowed("combo", now)
        )
        impulse_entries_on = self._paper_scalp is not None and (
            self._agent_gates is None
            or self._agent_gates.entries_allowed("impulse_fade", now)
        )
        combo_row, _tip = self._cached_combo_row(now)
        combo_signal = None
        combo_side = None  # lasting confluence regime (B/S); signal is flip-only
        if isinstance(combo_row, dict):
            combo_signal = combo_row.get("signal") or None
            combo_side = combo_row.get("side") or None
        impulse_delta = None
        impulse_fade_side = None
        try:
            from atlas_lite.paper_impulse_fade import MIN_IMPULSE, fade_side, impulse_pts

            closes = self._paper_scalp_closes(now)
            impulse_delta = impulse_pts(closes)
            if impulse_delta is not None and abs(float(impulse_delta)) >= float(
                MIN_IMPULSE
            ):
                impulse_fade_side = fade_side(impulse_delta)
            else:
                impulse_fade_side = None
        except Exception:  # noqa: BLE001
            impulse_delta = None
            impulse_fade_side = None
        adx_val = feed.get("adx") if feed.get("adx") is not None else self.adx
        pdi_val = getattr(nifty_nb, "pdi", None) if nifty_nb is not None else None
        mdi_val = getattr(nifty_nb, "mdi", None) if nifty_nb is not None else None
        try:
            adx_f = float(adx_val) if adx_val is not None else None
        except (TypeError, ValueError):
            adx_f = None
        if adx_f is None:
            adx_regime = None
        elif adx_f >= 22:
            adx_regime = "trend"
        elif adx_f < 18:
            adx_regime = "range"
        else:
            adx_regime = "mixed"
        # Intraday direction vs session open (day chg vs yesterday is kept separately).
        spot_chg_open = None
        if nifty_nb is not None:
            try:
                spot_chg_open = quote_change_from_open_pct(
                    self.book.get(nifty_nb.config.symbol)
                )
            except Exception:  # noqa: BLE001
                spot_chg_open = None
        # Richness vs put-call parity: (CE-PE) − (F_opt−ATM).
        # Scale monthly fut basis down to weekly option DTE (not raw fut LTP).
        skew_raw = skew
        skew_rich = None
        atm_used = atm if atm is not None else feed.get("atm")
        fut = None
        days_to_expiry = None
        fut_days_to_expiry = None
        if nifty_nb is not None and getattr(nifty_nb, "universe", None) is not None:
            uni = nifty_nb.universe
            try:
                fut = quote_ltp(self.book.get(uni.fut_symbol))
            except Exception:  # noqa: BLE001
                fut = None
            try:
                days_to_expiry = max(0, (uni.expiry - now.date()).days)
            except Exception:  # noqa: BLE001
                days_to_expiry = None
            try:
                if getattr(uni, "fut_expiry", None) is not None:
                    fut_days_to_expiry = max(0, (uni.fut_expiry - now.date()).days)
            except Exception:  # noqa: BLE001
                fut_days_to_expiry = None
        forward = None
        carry_pts = None
        try:
            if spot is not None:
                forward, carry_pts = option_carry_from_fut(
                    float(spot),
                    float(fut) if fut is not None else None,
                    option_dte=days_to_expiry,
                    fut_dte=fut_days_to_expiry,
                )
            if (
                ce is not None
                and pe is not None
                and forward is not None
                and atm_used is not None
            ):
                skew_rich = round(
                    float(ce) - float(pe) - (float(forward) - float(atm_used)), 2
                )
        except (TypeError, ValueError):
            skew_rich = None
        structure = {
            "ok": False,
            "reason": "no_bars",
            "bias": "unknown",
            "trap": None,
            "cues": [],
        }
        try:
            builder = nifty_nb.bar_builder if nifty_nb is not None else None
            raw_bars = list(builder.bars) if builder is not None else []
            forming = None
            if builder is not None and getattr(builder, "_current_key", None):
                forming = str(builder._current_key)
            structure = build_structure_expert(
                raw_bars, spot=float(spot) if spot is not None else None, forming_key=forming
            )
        except Exception:  # noqa: BLE001
            structure = {
                "ok": False,
                "reason": "structure_build_failed",
                "bias": "unknown",
                "trap": None,
                "cues": [],
            }
        return {
            "as_of": now.isoformat(),
            "clock": {
                "ist": now.isoformat(),
                "weekday": now.weekday(),
                "minutes_to_square_off": mins,
            },
            "spot": spot,
            # Day chg = vs prior close (can stay green in an intraday selloff).
            "spot_chg_pct": feed.get("nifty_chg") or feed.get("index_nifty_chg"),
            # Intraday chg = vs today's open — primary direction for the agent.
            "spot_chg_open_pct": spot_chg_open,
            "atm": atm_used,
            "fut": fut,
            "forward": forward,
            "days_to_expiry": days_to_expiry,
            "fut_days_to_expiry": fut_days_to_expiry,
            "carry_pts": carry_pts,
            "adx": adx_val,
            "pdi": pdi_val,
            "mdi": mdi_val,
            "adx_hint": feed.get("adx_hint") or self.adx_hint,
            "adx_regime": adx_regime,
            "ivp": feed.get("ivp"),
            "pcr": pcr,
            "max_pain": max_pain,
            "ce": ce,
            "pe": pe,
            "ce_pe_skew": skew_raw,
            "ce_pe_skew_rich": skew_rich,
            "combo": {
                "enabled": self._paper_combo is not None,
                "entries_allowed": combo_entries_on,
                "position": combo.get("position"),
                "day_pnl": combo.get("day_pnl"),
                "entries_today": combo.get("entries_today"),
                "side": combo_side,
                "signal": combo_signal,
                "bull": combo_row.get("bull") if isinstance(combo_row, dict) else None,
                "bear": combo_row.get("bear") if isinstance(combo_row, dict) else None,
            },
            "impulse": {
                "enabled": self._paper_scalp is not None,
                "entries_allowed": impulse_entries_on,
                "position": impulse.get("position"),
                "day_pnl": impulse.get("day_pnl"),
                "entries_today": impulse.get("entries_today"),
                "delta": impulse_delta,
                "fade_side": impulse_fade_side,
            },
            # 1m candle / sweep / trap cues (code-computed; cite only these fields).
            "structure": structure,
        }

    def _agent_propose_entry(
        self,
        *,
        side: str,
        style: str = "long",
        reason: str = "",
        spot: float | None = None,
    ) -> dict[str, Any]:
        if self._paper_agent is None:
            return {"ok": False, "rejected": "agent_book_disabled"}
        if self._agent_gates is not None and not self._agent_gates.entries_allowed("agent"):
            return {"ok": False, "rejected": "agent_gate_blocked"}
        return self._paper_agent.propose_entry(
            side=side, style=style, reason=reason, spot=spot
        )

    def _agent_propose_exit(self, *, reason: str = "") -> dict[str, Any]:
        if self._paper_agent is None:
            return {"ok": False, "rejected": "agent_book_disabled"}
        mark = None
        pos = self._paper_agent.position
        if pos is not None and self.book is not None:
            # Same CE/PE resolution as PaperAgent.on_frame (not only pos.symbol).
            ce = quote_ltp(self.book.get(pos.ce_symbol))
            pe = quote_ltp(self.book.get(pos.pe_symbol))
            mark = ce if pos.side == "ce" else pe
            if mark is None:
                mark = quote_ltp(self.book.get(pos.symbol))
        return self._paper_agent.propose_exit(reason=reason, mark=mark)

    def agent_status(self) -> dict[str, Any]:
        if self._agent is None:
            body = {
                "ok": True,
                "enabled": False,
                "has_credentials": bool(read_llm_credentials()),
                "gates": self._agent_gates.snapshot().get("gates") if self._agent_gates else {},
            }
        else:
            body = self._agent.status()
        if self._last_book_policy is not None:
            body["book_policy"] = {
                "regime": self._last_book_policy.get("regime"),
                "adx": self._last_book_policy.get("adx"),
                "ts": self._last_book_policy.get("ts"),
                "applied": self._last_book_policy.get("applied") or [],
                "desired": self._last_book_policy.get("desired") or {},
            }
        return body

    def agent_advise(self, *, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
        if self._agent is None:
            return {"ok": False, "error": "agent_disabled"}
        return self._agent.advise(dry_run=dry_run, force=force)

    def set_agent_gate(
        self,
        book: str,
        mode: str,
        *,
        until: str | None = None,
        reason: str | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        if self._agent_gates is None:
            self._agent_gates = AgentGateStore(self.data_dir / "agent_gates.json")
        return self._agent_gates.set_gate(
            book, mode, until=until, reason=reason, source=source  # type: ignore[arg-type]
        )

    def paper_snapshot(self) -> dict[str, Any]:
        if self._paper is None:
            body: dict[str, Any] = {
                "ok": True,
                "mode": "paper",
                "enabled": False,
                "live_orders": False,
            }
        else:
            body = self._paper.snapshot(book=self.book)
            body["enabled"] = True
            feed = self.build_feed()
            now = datetime.now(IST)
            regime = evaluate_paper_regime(feed, now=now)
            long_ev = regime.get("long") or {}
            fly_ev = regime.get("fly") or {}
            body["tape_ready"] = bool(long_ev.get("ready"))
            body["tape_failing"] = long_ev.get("failing_gates") or []
            body["tape_missing"] = long_ev.get("missing_gates") or []
            body["fly_ready"] = bool(fly_ev.get("ready"))
            body["fly_failing"] = fly_ev.get("failing_gates") or []
            body["fly_missing"] = fly_ev.get("missing_gates") or []
            body["regime"] = regime.get("strategy")
            body["tape_in_window"] = in_paper_entry_window(now)
            body["realised_vol"] = regime.get("realised_vol")
            body["implied_vol"] = regime.get("implied_vol")
            body["straddle_edge"] = regime.get("straddle_edge")
            body["iv_chg_5d"] = feed.get("iv_chg_5d")
        if self._paper_vwap is None:
            body["vwap_long"] = {
                "ok": True,
                "mode": "paper",
                "enabled": False,
                "live_orders": False,
                "book": VWAP_STRATEGY,
            }
        else:
            nifty_nb = self.notebooks.get("nifty")
            spot = self._spot(nifty_nb) if nifty_nb else None
            vwap_body = self._paper_vwap.snapshot(spot=spot)
            vwap_body["enabled"] = True
            body["vwap_long"] = vwap_body
        if self._paper_short is None:
            body["short_straddle"] = {
                "ok": True,
                "mode": "paper",
                "enabled": False,
                "live_orders": False,
                "book": SHORT_STR_STRATEGY,
            }
        else:
            short_body = self._paper_short.snapshot(book=self.book)
            short_body["enabled"] = True
            body["short_straddle"] = short_body
        if self._paper_skew is None:
            body["skew_fade"] = {
                "ok": True,
                "mode": "paper",
                "enabled": False,
                "live_orders": False,
                "book": SKEW_FADE_STRATEGY,
            }
        else:
            skew_body = self._paper_skew.snapshot(book=self.book)
            skew_body["enabled"] = True
            body["skew_fade"] = skew_body
        if self._paper_lic is None:
            body["long_iron_condor"] = {
                "ok": True,
                "mode": "paper",
                "enabled": False,
                "live_orders": False,
                "book": LONG_IC_STRATEGY,
            }
        else:
            lic_body = self._paper_lic.snapshot(book=self.book)
            lic_body["enabled"] = True
            body["long_iron_condor"] = lic_body
        if self._paper_theta is None:
            body["theta_cliff"] = {
                "ok": True,
                "mode": "paper",
                "enabled": False,
                "live_orders": False,
                "book": THETA_CLIFF_STRATEGY,
            }
        else:
            theta_body = self._paper_theta.snapshot(book=self.book)
            theta_body["enabled"] = True
            body["theta_cliff"] = theta_body
        if self._paper_short_ic is None:
            body["short_iron_condor"] = {
                "ok": True,
                "mode": "paper",
                "enabled": False,
                "live_orders": False,
                "book": SHORT_IC_STRATEGY,
            }
        else:
            sic_body = self._paper_short_ic.snapshot(book=self.book)
            sic_body["enabled"] = True
            body["short_iron_condor"] = sic_body
        if self._paper_scalp is None:
            body["impulse_fade"] = {
                "ok": True,
                "mode": "paper",
                "enabled": False,
                "live_orders": False,
                "book": IMPULSE_FADE_STRATEGY,
            }
        else:
            scalp_body = self._paper_scalp.snapshot(book=self.book)
            scalp_body["enabled"] = True
            body["impulse_fade"] = scalp_body
        if self._paper_combo is None:
            body["combo"] = {
                "ok": True,
                "mode": "paper",
                "enabled": False,
                "live_orders": False,
                "book": COMBO_STRATEGY,
            }
        else:
            combo_body = self._paper_combo.snapshot(book=self.book)
            combo_body["enabled"] = True
            body["combo"] = combo_body
        if self._paper_ict is None:
            body["ict"] = {
                "ok": True,
                "mode": "paper",
                "enabled": False,
                "live_orders": False,
                "book": ICT_STRATEGY,
            }
        else:
            ict_body = self._paper_ict.snapshot(book=self.book)
            ict_body["enabled"] = True
            body["ict"] = ict_body
        if self._paper_agent is None:
            body["agent"] = {
                "ok": True,
                "mode": "paper",
                "enabled": False,
                "live_orders": False,
                "book": AGENT_STRATEGY,
            }
        else:
            nifty_nb = self.notebooks.get("nifty")
            spot = self._spot(nifty_nb) if nifty_nb else None
            agent_body = self._paper_agent.snapshot(book=self.book, spot=spot)
            agent_body["enabled"] = True
            body["agent"] = agent_body
        if self._agent_gates is not None:
            body["agent_gates"] = self._agent_gates.snapshot().get("gates")
        return body

    @staticmethod
    def _paper_trade_px(*candidates: Any) -> float | None:
        """First finite numeric premium among ledger field aliases."""
        for raw in candidates:
            if raw is None or raw == "":
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value == value:  # not NaN
                return value
        return None

    _PAPER_OPEN_EVENTS = frozenset({"open", "reentry"})
    _PAPER_CLOSE_EVENTS = frozenset({"close", "close_vertical", "close_set"})

    def list_paper_trades(
        self,
        *,
        limit: int = 200,
        day: str | None = None,
    ) -> dict[str, Any]:
        """Paper fills across books — paired open/close, consolidated PnL by book×day."""
        # close_set / reentry: short IC (and similar) book PnL per side, not on flatten close.
        want = self._PAPER_OPEN_EVENTS | self._PAPER_CLOSE_EVENTS
        day_key = (day or "").strip() or None
        rows: list[dict[str, Any]] = []
        all_days: set[str] = set()
        for path in sorted(self.data_dir.glob("paper_*.jsonl")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                raw = line.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") not in want:
                    continue
                event_day = str(event.get("day") or "").strip()
                if event_day:
                    all_days.add(event_day)
                if day_key and event_day != day_key:
                    continue
                stop = self._paper_trade_px(event.get("stop"), event.get("stop_loss"))
                # Prefer per-unit premiums. credit_entry before credit so short-IC
                # close_set shows the side credit, not the remaining combined credit.
                entry = self._paper_trade_px(
                    event.get("credit_entry"),
                    event.get("entry"),
                    event.get("credit"),
                    event.get("debit"),
                    event.get("straddle_entry"),
                    event.get("straddle"),
                )
                exit_px = self._paper_trade_px(
                    event.get("exit"),
                    event.get("credit_exit"),
                    event.get("straddle_exit"),
                    event.get("debit_exit"),
                )
                symbol = event.get("symbol")
                if not symbol:
                    side = str(event.get("side") or "").lower()
                    if side == "ce":
                        symbol = (
                            event.get("short_symbol")
                            or event.get("ce_symbol")
                            or event.get("ce_short_symbol")
                        )
                    elif side == "pe":
                        symbol = (
                            event.get("short_symbol")
                            or event.get("pe_symbol")
                            or event.get("pe_short_symbol")
                        )
                    else:
                        symbol = (
                            event.get("short_symbol")
                            or event.get("ce_symbol")
                            or event.get("pe_symbol")
                            or event.get("ce_short_symbol")
                            or event.get("pe_short_symbol")
                        )
                rows.append(
                    {
                        "ts": event.get("ts"),
                        "day": event.get("day"),
                        "event": event.get("event"),
                        "strategy": event.get("strategy") or event.get("book"),
                        "side": event.get("side"),
                        "symbol": symbol,
                        "atm": event.get("atm"),
                        "entry": entry,
                        "exit": exit_px,
                        "qty": event.get("qty"),
                        "pnl": event.get("pnl"),
                        "pnl_known": event.get("pnl_known"),
                        "reason": event.get("reason"),
                        "charges": event.get("charges"),
                        "stop": stop,
                        "target": event.get("target"),
                        "gates": event.get("gates"),
                        "impulse": event.get("impulse"),
                        "letter": event.get("letter"),
                        "bull": event.get("bull"),
                        "bear": event.get("bear"),
                        "hold_until": event.get("hold_until"),
                        "signal_minute": event.get("signal_minute"),
                        "opened_at": event.get("opened_at"),
                        "info": self._paper_trade_info(event),
                        "book_file": path.name,
                    }
                )
        self._backfill_open_opened_at(rows)
        paired = self._pair_paper_trade_rows(rows)
        by_book_day = self._paper_trades_by_book_day(paired)
        known_pnls = [
            float(r["pnl_total"]) for r in by_book_day if r.get("pnl_total") is not None
        ]
        pnl_total = round(sum(known_pnls), 2) if known_pnls else None
        paired.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
        capped = max(1, min(int(limit), 1000))
        days = sorted(all_days, reverse=True)
        open_n = sum(1 for r in paired if r.get("event") in self._PAPER_OPEN_EVENTS)
        closed_n = sum(1 for r in paired if r.get("event") in self._PAPER_CLOSE_EVENTS)
        return {
            "ok": True,
            "mode": "paper",
            "live_orders": False,
            "day": day_key,
            "count": len(paired),
            "open_count": open_n,
            "closed_count": closed_n,
            "pnl_total": pnl_total,
            "by_book_day": by_book_day,
            "days": days,
            "trades": paired[:capped],
        }

    @classmethod
    def _backfill_open_opened_at(cls, rows: list[dict[str, Any]]) -> None:
        """If an open omitted opened_at but closes reference its ts, align them.

        Historical theta_cliff opens only wrote ``ts``; close_vertical carried
        ``opened_at`` equal to that open ts. Promoting those opens into the
        lifetime key avoids a forever-stuck open row. Same-day books whose
        closes lack opened_at stay on the legacy matcher.
        """
        life_refs: set[tuple[str, str]] = set()
        for row in rows:
            kind = str(row.get("event") or "")
            if kind in cls._PAPER_OPEN_EVENTS:
                continue
            oa = str(row.get("opened_at") or "").strip()
            if not oa:
                continue
            book = str(row.get("book_file") or row.get("strategy") or "")
            life_refs.add((book, oa))
        for row in rows:
            kind = str(row.get("event") or "")
            if kind not in cls._PAPER_OPEN_EVENTS:
                continue
            if str(row.get("opened_at") or "").strip():
                continue
            ts = str(row.get("ts") or "").strip()
            if not ts:
                continue
            book = str(row.get("book_file") or row.get("strategy") or "")
            if (book, ts) in life_refs:
                row["opened_at"] = ts

    @staticmethod
    def _paper_trade_match_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
        """Legacy same-day key when ledger rows lack opened_at."""
        return (
            str(row.get("book_file") or row.get("strategy") or ""),
            str(row.get("day") or ""),
            str(row.get("side") or "").lower(),
            str(row.get("symbol") or ""),
            str(row.get("atm") or ""),
        )

    @classmethod
    def _merge_paper_open_into_close(
        cls, close_row: dict[str, Any], opened: dict[str, Any] | None
    ) -> dict[str, Any]:
        merged = dict(close_row)
        if opened is None:
            return merged
        if merged.get("entry") is None:
            merged["entry"] = opened.get("entry")
        if merged.get("symbol") is None:
            merged["symbol"] = opened.get("symbol")
        if merged.get("side") is None:
            merged["side"] = opened.get("side")
        if not merged.get("opened_at"):
            merged["opened_at"] = opened.get("opened_at")
        merged["open_ts"] = opened.get("ts")
        open_info = str(opened.get("info") or "").strip()
        close_info = str(merged.get("info") or "").strip()
        if open_info and close_info and open_info not in close_info:
            close_tail = "\n".join(
                ln
                for ln in close_info.split("\n")
                if not ln.startswith("Entry:") and not ln.startswith("Exit:")
            ).strip()
            if close_tail:
                sep = "\n" if ("\n" in open_info or "\n" in close_tail) else " · "
                merged["info"] = f"{open_info}{sep}{close_tail}"
            else:
                merged["info"] = open_info
        elif open_info and not close_info:
            merged["info"] = open_info
        return merged

    @classmethod
    def _pair_paper_trade_rows(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop opens that already have a matching close; keep still-open positions.

        Overnight multi-leg books (short IC, theta cliff) carry ``opened_at`` and book
        PnL on ``close_set`` / ``close_vertical``. Pair those by ledger + opened_at
        (and side for re-entries), not by calendar day.
        """
        by_life: dict[tuple[str, str], list[dict[str, Any]]] = {}
        legacy: list[dict[str, Any]] = []
        for row in rows:
            opened_at = str(row.get("opened_at") or "").strip()
            if opened_at:
                book = str(row.get("book_file") or row.get("strategy") or "")
                by_life.setdefault((book, opened_at), []).append(row)
            else:
                legacy.append(row)
        out: list[dict[str, Any]] = []
        out.extend(cls._pair_paper_trade_rows_legacy(legacy))
        for group in by_life.values():
            out.extend(cls._pair_paper_trade_rows_lifetime(group))
        return out

    @classmethod
    def _pair_paper_trade_rows_legacy(
        cls, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Same-day open/close pairing keyed by book×day×side×symbol×atm."""
        chrono = sorted(rows, key=lambda row: str(row.get("ts") or ""))
        stacks: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        out: list[dict[str, Any]] = []
        for row in chrono:
            kind = str(row.get("event") or "")
            key = cls._paper_trade_match_key(row)
            if kind in cls._PAPER_OPEN_EVENTS:
                stacks.setdefault(key, []).append(row)
                continue
            if kind in cls._PAPER_CLOSE_EVENTS:
                stack = stacks.get(key) or []
                opened = stack.pop() if stack else None
                out.append(cls._merge_paper_open_into_close(row, opened))
                continue
            out.append(row)
        for stack in stacks.values():
            out.extend(stack)
        return out

    @classmethod
    def _pair_paper_trade_rows_lifetime(
        cls, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Pair one position lifetime (shared opened_at) across days and sides."""
        chrono = sorted(rows, key=lambda row: str(row.get("ts") or ""))
        # Initial open seeds both wings; reentry seeds its side only.
        side_stacks: dict[str, list[dict[str, Any]]] = {"ce": [], "pe": []}
        out: list[dict[str, Any]] = []
        pnl_closes = 0

        def _pop_side(side: str) -> dict[str, Any] | None:
            stack = side_stacks.get(side) or []
            if stack:
                return stack.pop()
            return None

        for row in chrono:
            kind = str(row.get("event") or "")
            side = str(row.get("side") or "").lower()

            if kind == "open":
                # Combined multi-leg open — both wings outstanding until closed.
                side_stacks["ce"].append(row)
                side_stacks["pe"].append(row)
                continue

            if kind == "reentry":
                if side in side_stacks:
                    side_stacks[side].append(row)
                else:
                    # Unknown side — keep visible as an open-like row.
                    out.append(row)
                continue

            if kind in ("close_set", "close_vertical"):
                opened = _pop_side(side) if side in side_stacks else None
                merged = cls._merge_paper_open_into_close(row, opened)
                out.append(merged)
                if merged.get("pnl") is not None:
                    pnl_closes += 1
                continue

            if kind == "close":
                # Flatten / seal marker: skip when per-side closes already booked PnL.
                if row.get("pnl") is None and pnl_closes > 0:
                    side_stacks["ce"].clear()
                    side_stacks["pe"].clear()
                    continue
                opened = None
                if side in side_stacks:
                    opened = _pop_side(side)
                if opened is None:
                    opened = _pop_side("ce") or _pop_side("pe")
                # Closing the book — clear any twin wing still stacked on the same open.
                if opened is not None:
                    for wing, stack in side_stacks.items():
                        side_stacks[wing] = [r for r in stack if r is not opened]
                merged = cls._merge_paper_open_into_close(row, opened)
                out.append(merged)
                if merged.get("pnl") is not None:
                    pnl_closes += 1
                continue

            out.append(row)

        # Still-open wings: emit each distinct open/reentry once (shared open is one row).
        seen_open_ids: set[int] = set()
        for stack in side_stacks.values():
            for opened in stack:
                oid = id(opened)
                if oid in seen_open_ids:
                    continue
                seen_open_ids.add(oid)
                out.append(opened)
        return out

    @staticmethod
    def _paper_trades_by_book_day(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Consolidate closed PnL (and open count) by strategy × IST day."""
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        open_events = FeedEngine._PAPER_OPEN_EVENTS
        close_events = FeedEngine._PAPER_CLOSE_EVENTS
        for row in rows:
            day = str(row.get("day") or "").strip() or "—"
            book = str(row.get("strategy") or "—")
            key = (day, book)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = {
                    "day": day,
                    "strategy": book,
                    "closed": 0,
                    "open": 0,
                    "pnl_total": 0.0,
                    "pnl_known": False,
                    "pnl_estimated": False,
                    "info": FeedEngine._paper_book_legend(book),
                }
                buckets[key] = bucket
            kind = str(row.get("event") or "")
            if kind in open_events:
                bucket["open"] += 1
                continue
            if kind in close_events:
                bucket["closed"] += 1
                pnl = row.get("pnl")
                if pnl is not None:
                    try:
                        bucket["pnl_total"] = round(float(bucket["pnl_total"]) + float(pnl), 2)
                        bucket["pnl_known"] = True
                        # Intrinsic / force settles book a number with pnl_known=false.
                        if row.get("pnl_known") is False:
                            bucket["pnl_estimated"] = True
                    except (TypeError, ValueError):
                        pass
        out = list(buckets.values())
        for bucket in out:
            if not bucket["pnl_known"]:
                bucket["pnl_total"] = None
            else:
                bucket["pnl_total"] = round(float(bucket["pnl_total"]), 2)
        out.sort(key=lambda r: (str(r.get("day") or ""), str(r.get("strategy") or "")), reverse=True)
        return out

    @staticmethod
    def _paper_book_legend(strategy: str) -> str:
        key = str(strategy or "").strip()
        pair = PAPER_BOOK_GATES.get(key)
        if not pair:
            return ""
        entry, exit_gates = pair
        return f"Entry: {entry}\nExit: {exit_gates}"

    @staticmethod
    def _paper_metric_fmt(value: Any) -> str:
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, float):
            if value != value:  # NaN
                return ""
            text = f"{value:.4f}".rstrip("0").rstrip(".")
            return text or "0"
        return str(value)

    @classmethod
    def _paper_entry_metrics(cls, event: dict[str, Any]) -> str:
        """Metrics captured on the open (or carried on the fill) for hover."""
        labels: tuple[tuple[str, str], ...] = (
            ("spot", "spot"),
            ("atm", "atm"),
            ("credit", "credit"),
            ("debit", "debit"),
            ("straddle", "straddle"),
            ("straddle_entry", "straddle"),
            ("ce", "CE"),
            ("pe", "PE"),
            ("ce_credit", "CE credit"),
            ("pe_credit", "PE credit"),
            ("ce_wing", "CE wing"),
            ("pe_wing", "PE wing"),
            ("wing_pts", "wing"),
            ("max_loss_pts", "max loss pts"),
            ("skew", "skew"),
            ("min_skew", "min skew"),
            ("impulse", "impulse"),
            ("letter", "letter"),
            ("bull", "bull"),
            ("bear", "bear"),
            ("pop", "POP"),
            ("rr", "R/R"),
            ("expected_move", "EM"),
            ("sigma_pts", "σ"),
            ("morning_high", "AM high"),
            ("morning_low", "AM low"),
            ("rv_pct", "RV%"),
            ("vix_yesterday", "VIX yday"),
            ("vwap", "VWAP"),
            ("dn1", "−1σ"),
            ("pull_long", "pull"),
            ("bias", "bias"),
            ("style", "style"),
            ("realised_vol", "RV"),
            ("implied_vol", "IV"),
            ("straddle_edge", "IV−RV"),
            ("index_nifty_chg", "NIFTY chg%"),
            ("qty", "qty"),
            ("lots", "lots"),
            ("charges", "charges"),
            ("target_pnl", "tgt ₹"),
            ("stop_pnl", "stop ₹"),
            ("ce_target_rupees", "CE tgt ₹"),
            ("pe_target_rupees", "PE tgt ₹"),
            ("signal_minute", "signal"),
            ("reason", "thesis"),
        )
        parts: list[str] = []
        if event.get("bull") is not None or event.get("bear") is not None:
            parts.append(f"votes=B{event.get('bull') or 0}/S{event.get('bear') or 0}")
        for key, label in labels:
            if key in ("bull", "bear"):
                continue
            # Prefer one of straddle / straddle_entry.
            if key == "straddle_entry" and event.get("straddle") is not None:
                continue
            val = event.get(key)
            if val is None or val == "":
                continue
            text = cls._paper_metric_fmt(val)
            if not text:
                continue
            # key=value so the hover table can keep labels with spaces (CE credit=4.5).
            parts.append(f"{label}={text}")
        return " · ".join(parts)

    @classmethod
    def _paper_trade_info(cls, event: dict[str, Any]) -> str:
        """Entry/exit gate legend + metrics-at-entry for the trades hover tip."""
        strategy = str(event.get("strategy") or event.get("book") or "")
        sections: list[str] = []
        legend = cls._paper_book_legend(strategy)
        if legend:
            sections.append(legend)

        kind = str(event.get("event") or "")
        if kind in cls._PAPER_OPEN_EVENTS:
            metrics = cls._paper_entry_metrics(event)
            if metrics:
                sections.append(f"At entry: {metrics}")

        parts: list[str] = []
        if kind in cls._PAPER_OPEN_EVENTS:
            gates = event.get("gates")
            if gates:
                parts.append(f"entry={gates}")
            elif kind == "reentry":
                parts.append("entry=reentry")
            target = event.get("target")
            stop = event.get("stop") if event.get("stop") is not None else event.get("stop_loss")
            if target is not None:
                parts.append(f"tgt={target}")
            if stop is not None:
                parts.append(f"stop={stop}")
            hold = event.get("hold_until")
            if hold:
                parts.append(f"hold={hold}")
        else:
            reason = event.get("reason")
            if reason:
                parts.append(f"exit={reason}")
            target = event.get("target")
            stop = event.get("stop") if event.get("stop") is not None else event.get("stop_loss")
            if target is not None:
                parts.append(f"tgt={target}")
            if stop is not None:
                parts.append(f"stop={stop}")
            gates = event.get("gates")
            if gates:
                parts.append(f"gates={gates}")
        if parts:
            sections.append(" · ".join(parts))
        return "\n".join(sections) if sections else ""

    def build_feed(self, nb: str = "nifty") -> dict[str, Any]:
        """Build feed for a specific notebook (nifty or sensex)."""
        self._reset_session_if_new_day()
        runtime = self.notebooks.get(nb)
        if not runtime or not runtime.enabled:
            return {"error": f"Notebook {nb} not enabled"}
        
        feed: dict[str, Any] = {}
        warnings: list[str] = []

        for feed_key, symbol in INDEX_SYMBOLS.items():
            row = self.book.get(symbol)
            pct = quote_change_pct(row)
            pts = quote_change_pts(row)
            if pct is not None:
                feed[feed_key] = pct
            if pts is not None:
                feed[feed_key.replace("_chg", "_pts")] = pts

        spot = self._spot(runtime)
        if spot is not None:
            feed["spot"] = spot
            # Back-compat: keep nifty_ltp for nifty notebook (paper reads this)
            if nb == "nifty":
                feed["nifty_ltp"] = spot

        if runtime.universe:
            feed["expiry"] = runtime.universe.expiry.isoformat()
        if runtime.atm and runtime.universe:
            feed["atm"] = runtime.atm.strike
            feed["ce_symbol"] = runtime.atm.ce_symbol
            feed["pe_symbol"] = runtime.atm.pe_symbol
            ce_row = self.book.get(runtime.atm.ce_symbol)
            pe_row = self.book.get(runtime.atm.pe_symbol)
            ce_ltp = quote_ltp(ce_row)
            pe_ltp = quote_ltp(pe_row)
            if ce_ltp is not None:
                feed["ce"] = ce_ltp
            if pe_ltp is not None:
                feed["pe"] = pe_ltp
            ce_oi = quote_oi(ce_row)
            pe_oi = quote_oi(pe_row)
            if ce_oi is not None:
                feed["ce_oi"] = ce_oi
            if pe_oi is not None:
                feed["pe_oi"] = pe_oi
            iv = self._resolve_live_iv(
                runtime,
                ce_row,
                pe_row,
                spot,
                runtime.atm.strike,
                runtime.universe.expiry,
            )
            if iv is not None:
                feed["iv"] = iv

        if runtime.adx is not None:
            feed["adx"] = runtime.adx
        if runtime.atr is not None:
            feed["atr"] = runtime.atr
        if runtime.adx_hint:
            feed["adx_hint"] = runtime.adx_hint

        for vix_sym in VIX_SYMBOLS:
            vix_row = self.book.get(vix_sym)
            if not vix_row:
                continue
            vix_ltp = quote_ltp(vix_row)
            if vix_ltp is None:
                continue
            if self.session.vix_open is None:
                session_open = quote_session_open(vix_row)
                self.session.vix_open = session_open if session_open is not None else vix_ltp
            self.session.vix_last = float(vix_ltp)
            feed["vix"] = round(float(vix_ltp), 3)
            feed["vix_chg"] = round(vix_ltp - float(self.session.vix_open), 3)
            break

        if runtime.universe:
            fut_row = self.book.get(runtime.universe.fut_symbol)
            fut_oi = quote_oi(fut_row)
            if fut_oi is not None:
                feed["fut_oi"] = fut_oi
            if fut_oi is not None and fut_oi > 0:
                if runtime.session.fut_oi_baseline is None:
                    runtime.session.fut_oi_baseline = fut_oi
                base = runtime.session.fut_oi_baseline
                if base is not None:
                    feed["fut_oi_base"] = base
                if base and base > 0:
                    feed["oi_pct_chg"] = round((fut_oi - base) / base * 100, 2)
                kite_high = quote_oi_day_high(fut_row)
                high = update_oi_day_high(fut_oi, kite_high, runtime.session.fut_oi_day_high)
                if high is not None:
                    runtime.session.fut_oi_day_high = high
                    feed["fut_oi_day_high"] = high
                    pct = oi_pct_of_day_high(fut_oi, high)
                    if pct is not None:
                        feed["oi_vs_day_high"] = pct

        if runtime.atm and runtime.universe:
            strikes, ce_syms, pe_syms = self._metrics_chain(runtime)
            chain = chain_pcr_max_pain(
                strikes,
                [self.book.get(s) for s in ce_syms],
                [self.book.get(s) for s in pe_syms],
            )
            feed.update(chain)

        if feed.get("iv") is not None:
            iv = float(feed["iv"])
            if iv > 0:
                if runtime.session.iv_day_high is None or iv > runtime.session.iv_day_high:
                    runtime.session.iv_day_high = iv
                if runtime.session.iv_day_low is None or iv < runtime.session.iv_day_low:
                    runtime.session.iv_day_low = iv
                feed["iv_day_high"] = runtime.session.iv_day_high
                feed["iv_day_low"] = runtime.session.iv_day_low
                pct = iv_pct_of_day_low(iv, runtime.session.iv_day_low)
                if pct is not None:
                    feed["iv_vs_day_low"] = pct

        # IVP from notebook's IVP key
        samples = ivp_sample_values(self.iv_history, runtime.config.ivp_key)
        iv_for_ivp = None
        if runtime.atm and runtime.universe:
            iv_for_ivp = self._greeks_iv(
                runtime,
                self.book.get(runtime.atm.ce_symbol),
                self.book.get(runtime.atm.pe_symbol),
            )
        if iv_for_ivp is None and feed.get("iv") is not None:
            iv_for_ivp = float(feed["iv"])
        if iv_for_ivp is not None:
            ivp = compute_ivp(samples, iv_for_ivp)
            if ivp is not None:
                feed["ivp"] = ivp
            elif len(samples) >= IVP_MIN_SAMPLES:
                warnings.append("IVP unavailable for current IV")
            chg = iv_change_n_days(self.iv_history, iv_for_ivp, n=5)
            if chg is not None:
                feed["iv_chg_5d"] = chg
        ivp_stats = ivp_history_stats(self.iv_history, runtime.config.ivp_key)
        if ivp_stats["total"] > 0:
            feed["ivp_proxy_days"] = ivp_stats["proxy"]
            feed["ivp_real_days"] = ivp_stats["real"]
            if ivp_stats["real"] == 0:
                warnings.append(
                    "IVP history is VIX-scaled proxy — improves as EOD ATM IV is captured"
                )
        elif len(samples) < IVP_MIN_SAMPLES:
            if not self._iv_history_seeded:
                warnings.append("Loading IVP history from Kite…")
            else:
                warnings.append(
                    f"IVP needs {IVP_MIN_SAMPLES}+ daily IV samples ({len(samples)} so far)"
                )
        elif feed.get("iv") is None:
            warnings.append("Waiting for ATM IV (CE/PE)…")

        if spot is None:
            if self._kite_auth_error:
                warnings.append(self._kite_auth_error)
            elif not self.book.connected:
                warnings.append("Kite WebSocket disconnected — check network or refresh token")
            else:
                warnings.append(f"Waiting for {runtime.config.label} tick…")
        if runtime.adx is None:
            warnings.append("Warming ADX from Kite 1m closed bars…")

        runtime.adx_warnings = list(warnings)
        self.warnings = warnings
        log_feed_snapshot(self._log, feed)
        return feed

    def health_status(self) -> dict[str, Any]:
        """Lightweight health snapshot — does not mutate session baselines."""
        age = self.book.last_tick_age_s()
        auth_error = self._kite_auth_error
        connected = self.book.connected
        live = (
            connected
            and age is not None
            and age <= HEALTH_TICK_MAX_AGE_S
        )
        # Live WS ticks keep ok=True even when REST auth is sticky-failed
        # (token expired for historical/quote while the open socket still works).
        healthy = live or (connected and not auth_error)
        body: dict[str, Any] = {
            "ok": healthy,
            "spot": self._spot(),
            "atm_strike": self.atm.strike if self.atm else None,
            "ticker": {
                "path": "ws" if connected else "…",
                "connected": connected,
                "last_tick_age_s": age,
                "subscribed": self.book.subscribed,
            },
        }
        if auth_error:
            body["auth_error"] = auth_error
            if healthy:
                body["degraded"] = True
            else:
                body["error"] = auth_error
        return body

    def build_option_chain(
        self,
        nb: str = "nifty",
        *,
        wing_strikes: int | None = None,
        extras: bool = False,
    ) -> dict[str, Any]:
        """Live option chain snapshot from Kite WS quote book for a notebook."""
        runtime = self.notebooks.get(nb)
        if not runtime or not runtime.enabled or not runtime.universe:
            return {"ok": False, "error": f"Notebook {nb} not available"}
        
        spot = self._spot(runtime)
        atm_strike = runtime.atm.strike if runtime.atm else None
        strikes, ce_syms, pe_syms = self._metrics_chain(runtime)
        ce_rows = [self.book.get(s) for s in ce_syms]
        pe_rows = [self.book.get(s) for s in pe_syms]
        chain_metrics = chain_pcr_max_pain(strikes, ce_rows, pe_rows)
        rows = option_chain_rows(
            strikes,
            ce_rows,
            pe_rows,
            atm_strike=atm_strike,
            wing_strikes=wing_strikes,
            extras=extras,
            spot=spot,
            expiry=runtime.universe.expiry,
        )
        totals = chain_accumulated_totals(rows)
        return {
            "ok": True,
            "underlying": runtime.config.symbol,
            "expiry": runtime.universe.expiry.isoformat(),
            "spot": spot,
            "atm_strike": atm_strike,
            "pcr": chain_metrics.get("pcr"),
            "max_pain": chain_metrics.get("max_pain"),
            "strike_count": len(strikes),
            "rows": rows,
            "totals": totals,
            "extras": extras,
            "computed_at_ms": int(time.time() * 1000),
        }

    def build_frame(self, nb: str = "nifty") -> dict[str, Any]:
        """Build frame for a specific notebook (nifty or sensex)."""
        runtime = self.notebooks.get(nb)
        if not runtime or not runtime.enabled:
            return {
                "ok": False,
                "error": f"Notebook {nb} not enabled",
                "computed_at_ms": int(time.time() * 1000),
            }
        
        feed = self.build_feed(nb)
        warnings: list[str] = list(runtime.adx_warnings)
        spot = feed.get("spot")
        if spot is None and feed.get("nifty_ltp") is not None:
            spot = feed.get("nifty_ltp")
        if spot is None and f"Waiting for {runtime.config.label} tick…" not in warnings:
            warnings.append(f"Waiting for {runtime.config.label} tick…")
        age = self.book.last_tick_age_s()
        path = "ws" if self.book.connected else "…"
        hint = suggest_strategy(feed)
        return {
            "ok": True,
            "notebook": {
                "id": runtime.config.id,
                "label": runtime.config.label,
                "symbol": runtime.config.symbol,
            },
            "underlying": {"symbol": runtime.config.symbol, "label": runtime.config.label},
            "engine_enabled": True,
            "engine_computing": spot is None,
            "feed": feed,
            "specs": [dict(s) for s in SHEET_SPECS],
            "live_warnings": warnings,
            "spot": spot,
            "expiry": feed.get("expiry"),
            "atm_strike": feed.get("atm"),
            "ce_symbol": feed.get("ce_symbol"),
            "pe_symbol": feed.get("pe_symbol"),
            "adx_hint": runtime.adx_hint,
            "live_bars": self.live_chart_bars(nb, 2),
            "strategy_hint": hint,
            "indices": self._build_indices(),
            "ticker": {
                "path": path,
                "connected": self.book.connected,
                "last_tick_age_s": age,
                "subscribed": self.book.subscribed,
            },
            "stream_interval_ms": STREAM_INTERVAL_MS,
            "computed_at_ms": int(time.time() * 1000),
        }
