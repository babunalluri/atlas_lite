"""Kite Connect REST client (quotes, instruments, historical candles)."""

from __future__ import annotations

import hashlib
import time
from typing import Any, Awaitable, Callable

import httpx

from atlas_lite.log_util import get_logger, log_tick

KITE_BASE = "https://api.kite.trade"
KITE_VERSION = "3"


class KiteRest:
    def __init__(self, api_key: str, access_token: str) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self._log = get_logger("kite.rest")
        self._on_auth_error: Callable[[], Awaitable[None] | None] | None = None
        self._auth_error_handling = False
        self._client = httpx.AsyncClient(
            base_url=KITE_BASE,
            headers={
                "Authorization": f"token {api_key}:{access_token}",
                "X-Kite-Version": "3",
            },
            timeout=httpx.Timeout(20.0, connect=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def set_auth_error_handler(
        self,
        handler: Callable[[], Awaitable[None] | None],
    ) -> None:
        self._on_auth_error = handler

    def update_credentials(self, api_key: str, access_token: str) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self._client.headers["Authorization"] = f"token {api_key}:{access_token}"

    async def renew_access_token(
        self,
        api_secret: str,
        refresh_token: str,
    ) -> dict[str, Any]:
        """Exchange refresh_token for a new access_token (same-day Kite session)."""
        checksum = hashlib.sha256(
            f"{self.api_key}{refresh_token}{api_secret}".encode()
        ).hexdigest()
        # Do not send the expired session token — Kite only wants api_key + checksum.
        request = self._client.build_request(
            "POST",
            "/session/refresh_token",
            data={
                "api_key": self.api_key,
                "refresh_token": refresh_token,
                "checksum": checksum,
            },
            headers={"X-Kite-Version": KITE_VERSION},
        )
        request.headers.pop("Authorization", None)
        resp = await self._client.send(request)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("access_token"):
            raise RuntimeError("Kite refresh_token response missing access_token")
        return data

    async def _raise_for_response(self, resp: httpx.Response) -> None:
        if (
            resp.status_code == 403
            and self._on_auth_error is not None
            and not self._auth_error_handling
        ):
            self._auth_error_handling = True
            try:
                result = self._on_auth_error()
                if result is not None:
                    await result
            finally:
                self._auth_error_handling = False
        resp.raise_for_status()

    async def check_session(self) -> None:
        """Raise on expired/invalid Kite access token (probe — no auth-error hook)."""
        resp = await self._client.get("/quote", params=[("i", "NSE:NIFTY 50")])
        resp.raise_for_status()

    async def instruments_nfo_csv(self) -> str:
        return await self.instruments_csv("NFO")

    async def instruments_csv(self, exchange: str) -> str:
        t0 = time.perf_counter()
        resp = await self._client.get(f"/instruments/{exchange}")
        await self._raise_for_response(resp)
        ms = (time.perf_counter() - t0) * 1000
        self._log.info(
            "KITE REST instruments/%s bytes=%d elapsed_ms=%.0f",
            exchange,
            len(resp.text),
            ms,
        )
        return resp.text

    async def quote(self, symbols: list[str]) -> dict[str, Any]:
        if not symbols:
            return {}
        t0 = time.perf_counter()
        params: list[tuple[str, str]] = [("i", s) for s in symbols]
        resp = await self._client.get("/quote", params=params)
        await self._raise_for_response(resp)
        ms = (time.perf_counter() - t0) * 1000
        data = resp.json()
        if not isinstance(data, dict):
            return {}
        inner = data.get("data")
        result = inner if isinstance(inner, dict) else {}
        self._log.info(
            "KITE REST quote symbols=%d returned=%d elapsed_ms=%.0f",
            len(symbols),
            len(result),
            ms,
        )
        quotes = normalize_quote_map(result)
        for sym, row in quotes.items():
            if ":" in sym:
                log_tick(self._log, source="rest", symbol=sym, row=row)
        return result

    async def historical_minute(
        self,
        instrument_token: int,
        *,
        from_date: str,
        to_date: str,
        oi: int = 0,
    ) -> list[list[Any]]:
        return await self.historical(
            instrument_token,
            "minute",
            from_date=from_date,
            to_date=to_date,
            oi=oi,
        )

    async def historical(
        self,
        instrument_token: int,
        interval: str,
        *,
        from_date: str,
        to_date: str,
        continuous: int = 0,
        oi: int = 0,
    ) -> list[list[Any]]:
        path = f"/instruments/historical/{instrument_token}/{interval}"
        t0 = time.perf_counter()
        resp = await self._client.get(
            path,
            params={
                "from": from_date,
                "to": to_date,
                "continuous": continuous,
                "oi": oi,
            },
        )
        await self._raise_for_response(resp)
        ms = (time.perf_counter() - t0) * 1000
        data = resp.json()
        if not isinstance(data, dict):
            return []
        inner = data.get("data")
        if not isinstance(inner, dict):
            return []
        candles = inner.get("candles")
        out = candles if isinstance(candles, list) else []
        self._log.info(
            "KITE REST historical/%s token=%d candles=%d from=%s to=%s elapsed_ms=%.0f",
            interval,
            instrument_token,
            len(out),
            from_date,
            to_date,
            ms,
        )
        return out


def normalize_quote_map(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Ensure each row has exchange:symbol key."""
    out: dict[str, dict[str, Any]] = {}
    for key, row in raw.items():
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or key).strip()
        if ":" not in sym and key.startswith("NFO:"):
            sym = key
        elif ":" not in sym and key.startswith("NSE:"):
            sym = key
        elif ":" not in sym and key.startswith("BSE:"):
            sym = key
        merged = dict(row)
        merged["symbol"] = sym
        out[sym] = merged
        token = row.get("instrument_token")
        if token is not None:
            out[str(int(token))] = merged
    return out
