from atlas_lite.kite_ws import WS_MODE_FULL, WS_MODE_QUOTE, _ws_mode_for_token


def test_ws_mode_full_for_nfo_and_bfo_options():
    # NFO segment byte = 2, BFO = 5 — both need full packets for OI.
    assert _ws_mode_for_token(12107010) == WS_MODE_FULL  # NFO option
    assert _ws_mode_for_token(222131973) == WS_MODE_FULL  # BFO option


def test_ws_mode_quote_for_cash_index_segment():
    assert _ws_mode_for_token(256265) == WS_MODE_QUOTE  # NSE cash/index
