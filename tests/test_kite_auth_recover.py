"""The minute loop's credential check: an unchanged token that Kite accepts again clears the auth error."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

from atlas_lite.feed_engine import FeedEngine


class Rest:
    def __init__(self, token: str, ok: bool) -> None:
        self.access_token, self.ok = token, ok

    async def check_session(self) -> None:
        if not self.ok:
            raise RuntimeError("403")


def engine(tmp_path, ok: bool):
    path = tmp_path / "kite_credentials"
    path.write_text(json.dumps({"api_key": "k", "access_token": "tok"}))
    return SimpleNamespace(credentials_path=path, rest=Rest("tok", ok), _credentials_lock=asyncio.Lock(),
                           _log=logging.getLogger("t"), _kite_auth_error="Kite access token expired")


def test_same_token_that_works_again_clears_the_error(tmp_path) -> None:
    e = engine(tmp_path, ok=True)
    asyncio.run(FeedEngine._try_recover_kite_credentials(e))
    assert e._kite_auth_error == ""


def test_same_token_still_refused_keeps_the_error(tmp_path) -> None:
    e = engine(tmp_path, ok=False)
    asyncio.run(FeedEngine._try_recover_kite_credentials(e))
    assert "expired" in e._kite_auth_error
