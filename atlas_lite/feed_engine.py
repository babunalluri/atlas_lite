"""Build dual-notebook (NIFTY + SENSEX) sheet feed from Kite WS ticks.

Both notebooks stay warm; per-client selection via nb= argument to build_frame,
build_feed, candles, build_option_chain. Paper trading stays NIFTY-only.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from atlas_lite.config import STREAM_INTERVAL_MS, read_kite_credentials
from atlas_lite.frame_util import frame_revision
from atlas_lite.strategy_hint import suggest_strategy
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
    resolve_atm_legs,
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
    quote_change_pts,
    quote_ltp,
    quote_oi,
    quote_oi_day_high,
    quote_session_open,
    quote_volume,
    resolve_atm_iv,
    atm_ref_price,
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
from atlas_lite.minute_bars import (
    MinuteBarBuilder,
    bars_from_kite_candles,
    in_adx_seed_window,
    kite_adx_window_start,
    load_bars,
    ohlc_from_bar_dicts,
    save_bars,
)
from atlas_lite.recorder import SheetRecorder, is_record_session, record_dir, record_enabled
from atlas_lite.paper_straddle import (
    FLY_WING_PTS,
    PaperStraddle,
    evaluate_paper_regime,
    in_paper_entry_window,
    iron_fly_strikes,
    paper_enabled,
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
# Pause 403 recoveries so a dead token does not hit Kite refresh on every 2s quote.
AUTH_RECOVER_COOLDOWN_S = 30.0
# ADX/DMI parity with Kite (verified Sep 2026): Kite REST closed 1m bars + WS forming
# minute, Wilder DMI(14) in metrics.wilder_dmi_series. Do not change bar source,
# refresh cadence, or compute path without re-verifying against Kite 1m DMI.
ADX_REST_DAYS = 3
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


@dataclass
class SessionState:
    """Engine-level session state (VIX only; per-notebook state in NotebookRuntime.session)."""
    vix_open: float | None = None
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
    _token_index: dict[str, int] = field(default_factory=dict, repr=False)
    _instruments_day: str = field(default="", repr=False)
    _credentials_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _last_kite_auth_attempt_at: float = field(default=0.0, repr=False)
    _paper: PaperStraddle | None = field(default=None, repr=False)
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
        self._tasks = [
            asyncio.create_task(self._maintenance_loop()),
            asyncio.create_task(self._iv_greeks_loop()),
            asyncio.create_task(self._seed_iv_history_if_needed()),
        ]
        if self._recorder is not None:
            self._tasks.append(asyncio.create_task(self._record_loop()))
        if self._paper is not None:
            self._tasks.append(asyncio.create_task(self._paper_loop()))

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

    async def _try_load_instruments_csv(self, exchange: str, filename: str) -> str:
        try:
            return await self._load_instruments_csv(exchange, filename)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("instruments %s unavailable: %s", exchange, exc)
            return ""

    async def _load_instrument_universe(self) -> None:
        nfo_csv = await self._load_instruments_csv("NFO", NFO_INSTRUMENTS_FILE)
        nse_csv = await self._load_instruments_csv("NSE", NSE_INSTRUMENTS_FILE)
        bse_csv = await self._load_instruments_csv("BSE", BSE_INSTRUMENTS_FILE)
        bfo_csv = await self._try_load_instruments_csv("BFO", BFO_INSTRUMENTS_FILE)
        self._instrument_csvs = [nfo_csv, nse_csv, bse_csv]
        if bfo_csv:
            self._instrument_csvs.append(bfo_csv)
        self._token_index = build_symbol_token_index(self._instrument_csvs)
        self._instruments_day = self._today()
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
        nifty_nb.chain_cache = full_chain_symbols(nifty_universe, nfo_csv)
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

    async def _load_instruments_csv(self, exchange: str, filename: str) -> str:
        path = self.data_dir / filename
        meta_path = self.data_dir / NFO_INSTRUMENTS_META
        today = self._today()
        if path.is_file() and meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                cached_day = (meta.get("files") or {}).get(filename)
                if cached_day == today:
                    self._log.info("instruments cache hit %s day=%s", exchange, today)
                    return path.read_text(encoding="utf-8")
            except (json.JSONDecodeError, OSError):
                pass
        csv = await self.rest.instruments_csv(exchange)
        path.write_text(csv, encoding="utf-8")
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                raw = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    meta = raw
            except (json.JSONDecodeError, OSError):
                pass
        files = dict(meta.get("files") or {})
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
        nb.kite_adx_bars = bars_from_kite_candles(candles, include_forming=True)

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
        live = dict(live_tail[-1])
        live_ts = str(live.get("t") or "")
        if not live_ts:
            return bars
        if bars and bars[-1]["t"] == live_ts:
            bars[-1] = live
        elif not bars or live_ts > bars[-1]["t"]:
            bars.append(live)
        return bars

    def _bars_for_chart(self, nb: NotebookRuntime, limit: int) -> list[dict[str, Any]]:
        """Bars for chart + DMI (Kite REST closed, WS forming)."""
        return self._kite_adx_series_bars(nb)[-max(1, limit) :]

    def _bar_dict_to_candle(self, bar: dict[str, Any]) -> dict[str, Any] | None:
        try:
            t = datetime.strptime(str(bar.get("t") or "")[:16], "%Y-%m-%d %H:%M").replace(
                tzinfo=IST
            )
        except ValueError:
            return None
        return {
            "time": int(t.timestamp()),
            "open": float(bar["o"]),
            "high": float(bar["h"]),
            "low": float(bar["l"]),
            "close": float(bar["c"]),
            "volume": float(bar.get("v") or 0),
            "oi": float(bar.get("oi") or 0),
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
        raw = series[-max(1, limit) :]
        bars: list[dict[str, Any]] = []
        for bar in raw:
            candle = self._bar_dict_to_candle(bar)
            if candle is not None:
                bars.append(candle)
        all_candles: list[dict[str, Any]] = []
        for bar in series:
            candle = self._bar_dict_to_candle(bar)
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
            await asyncio.sleep(MAINTENANCE_LOOP_S)

    def _refresh_adx_from_bars(self, nb: NotebookRuntime, *, purge: bool = True) -> None:
        """ADX/ATR from Kite REST 1m bars (matches Kite chart; WS bars are chart-only)."""
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
                if not is_record_session():
                    self._record_last_revision = None
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
                seen_seq = self.book.tick_seq
                await self.book.wait_for_update(seen_seq, interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.warning("sheet record failed: %s", exc)
                await asyncio.sleep(1.0)

    async def _paper_loop(self) -> None:
        """Paper long overlay or short iron fly. No broker orders. Bell stays notebook."""
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
                atm = frame.get("atm_strike")
                wing_ce = wing_pe = None
                if self.universe is not None and atm is not None:
                    pe_k, ce_k = iron_fly_strikes(int(atm), wing_pts=FLY_WING_PTS)
                    wing_pe = self.universe.option_symbol(pe_k, "PE")
                    wing_ce = self.universe.option_symbol(ce_k, "CE")
                self._paper.on_frame(
                    now=now,
                    entry_ready=strategy is not None,
                    strategy=strategy,
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

    def paper_snapshot(self) -> dict[str, Any]:
        if self._paper is None:
            return {"ok": True, "mode": "paper", "enabled": False, "live_orders": False}
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
        return body

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

    def build_option_chain(self, nb: str = "nifty", *, wing_strikes: int | None = None) -> dict[str, Any]:
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
