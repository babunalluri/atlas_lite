"""Test notebook config parsing and selection."""

from atlas_lite.notebook import (
    NOTEBOOK_IDS,
    NotebookConfig,
    get_notebook,
    notebook_list,
    parse_notebook,
)


def test_parse_notebook_returns_nifty_for_none() -> None:
    assert parse_notebook(None) == "nifty"


def test_parse_notebook_returns_nifty_for_empty() -> None:
    assert parse_notebook("") == "nifty"


def test_parse_notebook_returns_nifty_for_unknown() -> None:
    assert parse_notebook("unknown") == "nifty"
    assert parse_notebook("banknifty") == "nifty"


def test_parse_notebook_returns_nifty_lowercase() -> None:
    assert parse_notebook("nifty") == "nifty"
    assert parse_notebook("NIFTY") == "nifty"
    assert parse_notebook("NiFtY") == "nifty"


def test_parse_notebook_returns_sensex_lowercase() -> None:
    assert parse_notebook("sensex") == "sensex"
    assert parse_notebook("SENSEX") == "sensex"
    assert parse_notebook("SeNsEx") == "sensex"


def test_get_notebook_returns_config() -> None:
    cfg = get_notebook("nifty")
    assert isinstance(cfg, NotebookConfig)
    assert cfg.id == "nifty"
    assert cfg.symbol == "NSE:NIFTY 50"
    assert cfg.label == "NIFTY 50"
    assert cfg.option_name == "NIFTY"
    assert cfg.exchange == "NFO"
    assert cfg.strike_step == 50


def test_get_notebook_sensex_returns_config() -> None:
    cfg = get_notebook("sensex")
    assert isinstance(cfg, NotebookConfig)
    assert cfg.id == "sensex"
    assert cfg.symbol == "BSE:SENSEX"
    assert cfg.label == "SENSEX"
    assert cfg.option_name == "SENSEX"
    assert cfg.exchange == "BFO"
    assert cfg.strike_step == 100


def test_get_notebook_defaults_to_nifty() -> None:
    cfg = get_notebook(None)
    assert cfg.id == "nifty"
    cfg = get_notebook("")
    assert cfg.id == "nifty"
    cfg = get_notebook("unknown")
    assert cfg.id == "nifty"


def test_notebook_list_returns_both() -> None:
    notebooks = notebook_list()
    assert len(notebooks) == 2
    assert notebooks[0]["id"] == "nifty"
    assert notebooks[0]["symbol"] == "NSE:NIFTY 50"
    assert notebooks[1]["id"] == "sensex"
    assert notebooks[1]["symbol"] == "BSE:SENSEX"


def test_notebook_ids_tuple() -> None:
    assert NOTEBOOK_IDS == ("nifty", "sensex")
