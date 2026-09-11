"""Test dual notebook functionality (NIFTY + SENSEX both warm)."""

from pathlib import Path
from unittest.mock import MagicMock

from atlas_lite.feed_engine import FeedEngine
from atlas_lite.kite_rest import KiteRest
from atlas_lite.minute_bars import MinuteBarBuilder
from atlas_lite.notebook import get_notebook
from atlas_lite.notebook_runtime import NotebookRuntime


def _mock_engine_with_notebooks() -> FeedEngine:
    """Create a FeedEngine with both nifty and sensex notebooks initialized."""
    rest = MagicMock(spec=KiteRest)
    eng = FeedEngine(rest=rest, data_dir=Path("data"))
    
    # Initialize NIFTY notebook
    nifty_cfg = get_notebook("nifty")
    nifty_nb = NotebookRuntime(config=nifty_cfg)
    nifty_nb.bar_builder = MinuteBarBuilder(symbol=nifty_cfg.symbol)
    nifty_nb.enabled = True
    eng.notebooks["nifty"] = nifty_nb
    
    # Initialize SENSEX notebook
    sensex_cfg = get_notebook("sensex")
    sensex_nb = NotebookRuntime(config=sensex_cfg)
    sensex_nb.bar_builder = MinuteBarBuilder(symbol=sensex_cfg.symbol)
    sensex_nb.enabled = True
    eng.notebooks["sensex"] = sensex_nb
    
    return eng


def test_engine_has_both_notebooks() -> None:
    eng = _mock_engine_with_notebooks()
    assert "nifty" in eng.notebooks
    assert "sensex" in eng.notebooks
    assert eng.notebooks["nifty"].enabled
    assert eng.notebooks["sensex"].enabled


def test_nb_runtime_returns_correct_notebook() -> None:
    eng = _mock_engine_with_notebooks()
    nifty = eng.nb_runtime("nifty")
    assert nifty.config.id == "nifty"
    assert nifty.config.symbol == "NSE:NIFTY 50"
    
    sensex = eng.nb_runtime("sensex")
    assert sensex.config.id == "sensex"
    assert sensex.config.symbol == "BSE:SENSEX"


def test_build_feed_uses_correct_notebook_symbol() -> None:
    eng = _mock_engine_with_notebooks()
    
    # Mock spot prices using merge method
    eng.book.merge("NSE:NIFTY 50", {"last_price": 24000.0})
    eng.book.merge("BSE:SENSEX", {"last_price": 79000.0})
    
    nifty_feed = eng.build_feed("nifty")
    assert "nifty_ltp" in nifty_feed  # back-compat for paper
    assert nifty_feed["spot"] == 24000.0
    
    sensex_feed = eng.build_feed("sensex")
    assert "nifty_ltp" not in sensex_feed  # sensex doesn't have nifty_ltp
    assert sensex_feed["spot"] == 79000.0


def test_build_frame_includes_notebook_metadata() -> None:
    eng = _mock_engine_with_notebooks()
    
    nifty_frame = eng.build_frame("nifty")
    assert nifty_frame["notebook"]["id"] == "nifty"
    assert nifty_frame["notebook"]["label"] == "NIFTY 50"
    assert nifty_frame["notebook"]["symbol"] == "NSE:NIFTY 50"
    
    sensex_frame = eng.build_frame("sensex")
    assert sensex_frame["notebook"]["id"] == "sensex"
    assert sensex_frame["notebook"]["label"] == "SENSEX"
    assert sensex_frame["notebook"]["symbol"] == "BSE:SENSEX"


def test_candles_uses_correct_notebook() -> None:
    eng = _mock_engine_with_notebooks()
    
    # Set up different bars for each notebook
    nifty_nb = eng.notebooks["nifty"]
    nifty_nb.kite_adx_bars = [
        {"t": "2026-09-11 10:00", "o": 24000, "h": 24010, "l": 23990, "c": 24005, "v": 100, "oi": 10},
    ]
    
    sensex_nb = eng.notebooks["sensex"]
    sensex_nb.kite_adx_bars = [
        {"t": "2026-09-11 10:00", "o": 79000, "h": 79100, "l": 78900, "c": 79050, "v": 200, "oi": 20},
    ]
    
    nifty_candles = eng.candles("nifty", limit=10)
    assert nifty_candles["ok"] is True
    assert nifty_candles["symbol"] == "NSE:NIFTY 50"
    assert nifty_candles["label"] == "NIFTY 50"
    assert len(nifty_candles["bars"]) == 1
    assert nifty_candles["bars"][0]["close"] == 24005.0
    
    sensex_candles = eng.candles("sensex", limit=10)
    assert sensex_candles["ok"] is True
    assert sensex_candles["symbol"] == "BSE:SENSEX"
    assert sensex_candles["label"] == "SENSEX"
    assert len(sensex_candles["bars"]) == 1
    assert sensex_candles["bars"][0]["close"] == 79050.0


def test_nifty_candles_wrapper_calls_candles_nifty() -> None:
    eng = _mock_engine_with_notebooks()
    nifty_nb = eng.notebooks["nifty"]
    nifty_nb.kite_adx_bars = [
        {"t": "2026-09-11 10:00", "o": 24000, "h": 24010, "l": 23990, "c": 24005, "v": 100, "oi": 10},
    ]
    
    result = eng.nifty_candles(limit=10)
    assert result["ok"] is True
    assert result["symbol"] == "NSE:NIFTY 50"


def test_back_compat_properties_delegate_to_nifty() -> None:
    eng = _mock_engine_with_notebooks()
    nifty_nb = eng.notebooks["nifty"]
    
    # Set values on nifty notebook
    nifty_nb.adx = 22.5
    nifty_nb.atr = 85.3
    nifty_nb.adx_hint = "test hint"
    
    # Back-compat properties should delegate
    assert eng.adx == 22.5
    assert eng.atr == 85.3
    assert eng.adx_hint == "test hint"


def test_paper_uses_nifty_universe_only() -> None:
    """Paper trading should only use nifty notebook (per requirements)."""
    eng = _mock_engine_with_notebooks()
    
    # Universe property should return nifty's universe
    assert eng.universe is None or eng.universe == eng.notebooks["nifty"].universe
    
    # ATM property should return nifty's ATM
    assert eng.atm is None or eng.atm == eng.notebooks["nifty"].atm


def test_build_option_chain_uses_correct_notebook() -> None:
    eng = _mock_engine_with_notebooks()
    
    # Mock universes
    from datetime import date
    from atlas_lite.instruments import IndexOptionUniverse
    
    nifty_nb = eng.notebooks["nifty"]
    nifty_nb.universe = IndexOptionUniverse(
        name="NIFTY",
        fut_symbol="NFO:NIFTY26SEPFUT",
        fut_token=123456,
        expiry=date(2026, 9, 25),
        prefix="NIFTY26SEP",
        exchange="NFO",
        strike_step=50,
        hysteresis_pts=8.0,
    )
    nifty_nb.chain_cache = ([24000, 24050, 24100], [], [])
    
    sensex_nb = eng.notebooks["sensex"]
    sensex_nb.universe = IndexOptionUniverse(
        name="SENSEX",
        fut_symbol="BFO:SENSEX26SEPFUT",
        fut_token=654321,
        expiry=date(2026, 9, 25),
        prefix="SENSEX26SEP",
        exchange="BFO",
        strike_step=100,
        hysteresis_pts=16.0,
    )
    sensex_nb.chain_cache = ([79000, 79100, 79200], [], [])
    
    nifty_chain = eng.build_option_chain("nifty")
    assert nifty_chain["ok"] is True
    assert nifty_chain["underlying"] == "NSE:NIFTY 50"
    
    sensex_chain = eng.build_option_chain("sensex")
    assert sensex_chain["ok"] is True
    assert sensex_chain["underlying"] == "BSE:SENSEX"


def test_disabled_notebook_returns_error() -> None:
    eng = _mock_engine_with_notebooks()
    eng.notebooks["sensex"].enabled = False
    
    feed = eng.build_feed("sensex")
    assert "error" in feed
    
    frame = eng.build_frame("sensex")
    assert frame["ok"] is False
    assert "error" in frame
    
    candles = eng.candles("sensex")
    assert candles["ok"] is False
    assert "error" in candles


def test_separate_adx_per_notebook() -> None:
    """Each notebook should maintain its own ADX/ATR values."""
    eng = _mock_engine_with_notebooks()
    
    nifty_nb = eng.notebooks["nifty"]
    nifty_nb.adx = 18.5
    nifty_nb.atr = 72.3
    
    sensex_nb = eng.notebooks["sensex"]
    sensex_nb.adx = 26.7
    sensex_nb.atr = 250.8
    
    assert nifty_nb.adx != sensex_nb.adx
    assert nifty_nb.atr != sensex_nb.atr
    
    # Feeds should have different ADX/ATR
    nifty_feed = eng.build_feed("nifty")
    assert nifty_feed.get("adx") == 18.5
    assert nifty_feed.get("atr") == 72.3
    
    sensex_feed = eng.build_feed("sensex")
    assert sensex_feed.get("adx") == 26.7
    assert sensex_feed.get("atr") == 250.8


def test_separate_bars_files_per_notebook() -> None:
    """Each notebook should use its own bars file."""
    eng = _mock_engine_with_notebooks()
    
    nifty_cfg = eng.notebooks["nifty"].config
    sensex_cfg = eng.notebooks["sensex"].config
    
    assert nifty_cfg.bars_file == "minute_bars.json"
    assert sensex_cfg.bars_file == "minute_bars_sensex.json"
    assert nifty_cfg.bars_file != sensex_cfg.bars_file


def test_separate_ivp_keys_per_notebook() -> None:
    """Each notebook should use its own IVP key in history file."""
    eng = _mock_engine_with_notebooks()
    
    nifty_cfg = eng.notebooks["nifty"].config
    sensex_cfg = eng.notebooks["sensex"].config
    
    assert nifty_cfg.ivp_key == "NSE:NIFTY 50"
    assert sensex_cfg.ivp_key == "BSE:SENSEX"
    assert nifty_cfg.ivp_key != sensex_cfg.ivp_key
