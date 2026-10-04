"""Kite WebSocket ticker — in-memory quote book (no Redis)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from atlas_lite.log_util import get_logger, log_tick

KITE_WS_URL = "wss://ws.kite.trade"
WS_MODE_FULL = "full"
WS_MODE_QUOTE = "quote"


def _price_divisor(instrument_token: int) -> float:
    segment = instrument_token & 0xFF
    if segment == 3:
        return 10_000_000.0
    if segment == 6:
        return 10_000.0
    return 100.0


def parse_binary_ticks(payload: bytes) -> list[dict[str, Any]]:
    if not payload or len(payload) < 2:
        return []
    if len(payload) == 1:
        return []
    offset = 0
    (packet_count,) = struct.unpack_from(">H", payload, offset)
    offset += 2
    ticks: list[dict[str, Any]] = []
    for _ in range(packet_count):
        if offset + 2 > len(payload):
            break
        (packet_len,) = struct.unpack_from(">H", payload, offset)
        offset += 2
        if packet_len <= 0 or offset + packet_len > len(payload):
            break
        packet = payload[offset : offset + packet_len]
        offset += packet_len
        parsed = _parse_quote_packet(packet)
        if parsed is not None:
            ticks.append(parsed)
    return ticks


def _parse_quote_packet(packet: bytes) -> dict[str, Any] | None:
    if len(packet) < 8:
        return None
    (token,) = struct.unpack_from(">i", packet, 0)
    divisor = _price_divisor(token)
    ltp = struct.unpack_from(">i", packet, 4)[0] / divisor
    row: dict[str, Any] = {"instrument_token": token, "last_price": ltp, "ltp": ltp}
    if len(packet) == 8:
        return row
    if len(packet) in {28, 32}:
        if len(packet) >= 28:
            high = struct.unpack_from(">i", packet, 8)[0] / divisor
            low = struct.unpack_from(">i", packet, 12)[0] / divisor
            open_ = struct.unpack_from(">i", packet, 16)[0] / divisor
            close = struct.unpack_from(">i", packet, 20)[0] / divisor
            row["ohlc"] = {"open": open_, "high": high, "low": low, "close": close}
            row["net_change"] = struct.unpack_from(">i", packet, 24)[0] / divisor
        return row
    if len(packet) >= 44:
        row["last_traded_quantity"] = struct.unpack_from(">I", packet, 8)[0]
        row["average_price"] = struct.unpack_from(">i", packet, 12)[0] / divisor
        row["volume"] = struct.unpack_from(">I", packet, 16)[0]
        row["buy_quantity"] = struct.unpack_from(">I", packet, 20)[0]
        row["sell_quantity"] = struct.unpack_from(">I", packet, 24)[0]
        open_ = struct.unpack_from(">i", packet, 28)[0] / divisor
        high = struct.unpack_from(">i", packet, 32)[0] / divisor
        low = struct.unpack_from(">i", packet, 36)[0] / divisor
        close = struct.unpack_from(">i", packet, 40)[0] / divisor
        row["ohlc"] = {"open": open_, "high": high, "low": low, "close": close}
    # Kite full mode is 184 bytes; OI + 5-level depth.
    if len(packet) == 184:
        # Always write bid/ask (None when a side is empty) so QuoteBook.merge
        # cannot keep a stale opposite-side price and produce a crossed book.
        last_trade_time = struct.unpack_from(">I", packet, 44)[0]
        exchange_timestamp = struct.unpack_from(">I", packet, 60)[0]
        row["last_trade_time"] = last_trade_time or None
        row["exchange_timestamp"] = exchange_timestamp or None
        row["open_interest"] = struct.unpack_from(">I", packet, 48)[0]
        row["oi"] = row["open_interest"]
        row["oi_day_high"] = struct.unpack_from(">I", packet, 52)[0]
        row["oi_day_low"] = struct.unpack_from(">I", packet, 56)[0]
        depth = _parse_depth(packet, divisor)
        row["depth"] = depth
        row["bid"] = _best_depth_price(depth.get("buy"))
        row["ask"] = _best_depth_price(depth.get("sell"))
    return row


def _best_depth_price(levels: list[dict[str, Any]] | None) -> float | None:
    if not levels:
        return None
    top = levels[0]
    price = top.get("price")
    qty = top.get("quantity") or 0
    if price is None or qty <= 0:
        return None
    return float(price)


def _parse_depth(packet: bytes, divisor: float) -> dict[str, list[dict[str, Any]]]:
    buy: list[dict[str, Any]] = []
    sell: list[dict[str, Any]] = []
    offset = 64
    for bag in (buy, sell):
        for _ in range(5):
            qty, raw_px, orders = struct.unpack_from(">iiH", packet, offset)
            offset += 12
            bag.append(
                {
                    "quantity": max(qty, 0),
                    "price": raw_px / divisor,
                    "orders": orders,
                }
            )
    return {"buy": buy, "sell": sell}


def _ws_mode_for_token(token: int) -> str:
    """Kite packet mode from instrument-token segment byte.

    NFO (2) and BFO (5) options need ``full`` for open interest; cash/index
    segments use ``quote`` (LTP/OHLC only).
    """
    segment = token & 0xFF
    return WS_MODE_FULL if segment in {2, 5} else WS_MODE_QUOTE


@dataclass
class QuoteBook:
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_tick_at: float = 0.0
    connected: bool = False
    subscribed: int = 0
    tick_seq: int = 0
    on_tick: Any = field(default=None, repr=False)

    def get(self, symbol: str) -> dict[str, Any] | None:
        return self.rows.get(symbol)

    def notify_update(self) -> None:
        self.tick_seq += 1

    async def wait_for_update(self, seen_seq: int, timeout_s: float) -> None:
        """Wait until tick_seq advances or timeout (poll — safe for many SSE clients)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while self.tick_seq == seen_seq:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.05, remaining))

    def merge(self, symbol: str, tick: dict[str, Any]) -> None:
        prev = self.rows.get(symbol) or {}
        merged = {**prev, **tick, "symbol": symbol}
        self.rows[symbol] = merged
        self.last_tick_at = time.time()
        if self.on_tick is not None:
            self.on_tick(symbol, merged)
        self.notify_update()

    def last_tick_age_s(self) -> float | None:
        if self.last_tick_at <= 0:
            return None
        return round(time.time() - self.last_tick_at, 3)


class KiteTicker:
    def __init__(self, api_key: str, access_token: str, book: QuoteBook) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.book = book
        self._log = get_logger("kite.ws")
        self._token_to_symbol: dict[int, str] = {}
        self._desired: set[int] = set()
        self._active: set[int] = set()
        self._stop = asyncio.Event()
        self._dirty = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def set_symbols(self, token_to_symbol: dict[int, str]) -> None:
        self._token_to_symbol = {int(k): str(v) for k, v in token_to_symbol.items() if k and v}
        self._desired = set(self._token_to_symbol.keys())
        self._dirty.set()

    def update_credentials(self, api_key: str, access_token: str) -> None:
        self.api_key = api_key
        self.access_token = access_token

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.book.connected = False

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.book.connected = False
                self._active = set()
                self._log.warning("KITE WS session error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _session(self) -> None:
        import websockets

        query = urlencode({"api_key": self.api_key, "access_token": self.access_token})
        url = f"{KITE_WS_URL}?{query}"
        async with websockets.connect(
            url,
            ping_interval=30,
            ping_timeout=60,
            close_timeout=10,
            max_size=2**22,
        ) as ws:
            self._active = set()
            self.book.connected = True
            self._log.info("KITE WS connected subscribed_pending=%d", len(self._desired))
            self._dirty.set()
            while not self._stop.is_set():
                recv = asyncio.create_task(ws.recv())
                dirty = asyncio.create_task(self._dirty.wait())
                stop = asyncio.create_task(self._stop.wait())
                done, pending = await asyncio.wait(
                    {recv, dirty, stop},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                if stop in done:
                    break
                if dirty in done:
                    self._dirty.clear()
                    await self._apply_subscriptions(ws)
                if recv in done:
                    message = recv.result()
                    if isinstance(message, bytes):
                        for tick in parse_binary_ticks(message):
                            token = int(tick.get("instrument_token") or 0)
                            symbol = self._token_to_symbol.get(token)
                            if symbol:
                                self.book.merge(symbol, tick)
                                log_tick(self._log, source="ws", symbol=symbol, row=tick)
        self.book.connected = False

    async def _apply_subscriptions(self, ws: Any) -> None:
        tokens = sorted(self._desired)
        self.book.subscribed = len(tokens)
        dropped = sorted(self._active - self._desired)
        if dropped:
            await ws.send(json.dumps({"a": "unsubscribe", "v": dropped}))
        if not tokens:
            self._active = set()
            return
        new_tokens = sorted(self._desired - self._active)
        if new_tokens:
            await ws.send(json.dumps({"a": "subscribe", "v": new_tokens}))
        by_mode: dict[str, list[int]] = {}
        for token in tokens:
            by_mode.setdefault(_ws_mode_for_token(token), []).append(token)
        for mode, mode_tokens in by_mode.items():
            await ws.send(json.dumps({"a": "mode", "v": [mode, mode_tokens]}))
        self._active = set(tokens)
        self._log.info(
            "KITE WS subscribe count=%d full=%d quote=%d dropped=%d",
            len(tokens),
            len(by_mode.get(WS_MODE_FULL, [])),
            len(by_mode.get(WS_MODE_QUOTE, [])),
            len(dropped),
        )
