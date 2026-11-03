"""Synchronous Kite order helpers for the live agent book.

Paper books stay on LTP. When the agent live flag is on, opens and closes go
through this client. Credentials are re-read from disk on each call so a morning
token refresh is picked up without restarting the process mid-order.

Callers must run this off the asyncio event loop (e.g. asyncio.to_thread).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from atlas_lite.config import read_kite_credentials
from atlas_lite.log_util import get_logger

KITE_BASE = "https://api.kite.trade"
KITE_VERSION = "3"
DEFAULT_PRODUCT = "MIS"
DEFAULT_VARIETY = "regular"
# Kite Connect requires market_protection on API MARKET / SL-M orders.
# -1 = exchange/system auto protection (converts to a protected limit band).
DEFAULT_MARKET_PROTECTION = -1
FILL_POLL_S = 0.25
FILL_TIMEOUT_S = 8.0
ORDER_TAG = "atlas_agent"
ARB_ORDER_TAG = "atlas_arb"

# Terminal Kite statuses only. Everything else (OPEN PENDING, CANCEL PENDING, …) is still working.
KITE_ORDER_FINISHED = frozenset({"COMPLETE", "REJECTED", "CANCELLED", "CANCELED"})
KITE_ORDER_DEAD = frozenset({"REJECTED", "CANCELLED", "CANCELED"})

_log = get_logger("atlas_lite.kite_orders")


class KiteOrderError(RuntimeError):
    """Broker rejected the order or the fill never completed."""

    def __init__(self, message: str, *, order_id: str = "") -> None:
        super().__init__(message)
        self.order_id = str(order_id or "")


def kite_order_finished(status: str) -> bool:
    return str(status or "").strip().upper() in KITE_ORDER_FINISHED


def kite_order_dead(status: str) -> bool:
    return str(status or "").strip().upper() in KITE_ORDER_DEAD


def split_nfo_symbol(symbol: str) -> tuple[str, str]:
    """Return (exchange, tradingsymbol) for a book symbol like NFO:NIFTY…CE."""
    raw = str(symbol or "").strip()
    if not raw:
        raise KiteOrderError("empty_symbol")
    if ":" in raw:
        exchange, tradingsymbol = raw.split(":", 1)
        exchange = exchange.strip().upper() or "NFO"
        tradingsymbol = tradingsymbol.strip().upper()
    else:
        exchange, tradingsymbol = "NFO", raw.upper()
    if not tradingsymbol:
        raise KiteOrderError("empty_tradingsymbol")
    return exchange, tradingsymbol


def transaction_for_open(style: str) -> str:
    return "SELL" if str(style).lower() == "short" else "BUY"


def transaction_for_close(style: str) -> str:
    return "BUY" if str(style).lower() == "short" else "SELL"


def _wrap_transport(exc: BaseException) -> KiteOrderError:
    return KiteOrderError(f"transport:{type(exc).__name__}:{exc}")


class KiteOrderClient:
    """Blocking market-order place + fill wait. One lot at a time for the agent."""

    def __init__(self, credentials_path: Path) -> None:
        self.credentials_path = Path(credentials_path)

    def _client(self) -> httpx.Client:
        try:
            api_key, access_token = read_kite_credentials(self.credentials_path)
        except (OSError, ValueError, FileNotFoundError) as exc:
            raise KiteOrderError(f"credentials:{exc}") from exc
        return httpx.Client(
            base_url=KITE_BASE,
            headers={
                "Authorization": f"token {api_key}:{access_token}",
                "X-Kite-Version": KITE_VERSION,
            },
            timeout=httpx.Timeout(20.0, connect=10.0),
        )

    def place_market(
        self,
        *,
        symbol: str,
        transaction_type: str,
        qty: int,
        product: str = DEFAULT_PRODUCT,
        tag: str = ORDER_TAG,
        market_protection: int = DEFAULT_MARKET_PROTECTION,
    ) -> str:
        exchange, tradingsymbol = split_nfo_symbol(symbol)
        side = str(transaction_type or "").strip().upper()
        if side not in ("BUY", "SELL"):
            raise KiteOrderError(f"bad_transaction_type:{transaction_type}")
        quantity = int(qty)
        if quantity <= 0:
            raise KiteOrderError("bad_qty")
        data = {
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": side,
            "order_type": "MARKET",
            "quantity": str(quantity),
            "product": product,
            "validity": "DAY",
            "market_protection": str(int(market_protection)),
            "tag": (tag or ORDER_TAG)[:20],
        }
        try:
            with self._client() as client:
                resp = client.post(f"/orders/{DEFAULT_VARIETY}", data=data)
                body = _json_body(resp)
                if resp.status_code >= 400:
                    raise KiteOrderError(_kite_error(body, resp.status_code))
                order_id = str((body.get("data") or {}).get("order_id") or "").strip()
                if not order_id:
                    raise KiteOrderError("missing_order_id")
                _log.warning(
                    "kite order placed id=%s %s %s qty=%s",
                    order_id,
                    side,
                    tradingsymbol,
                    quantity,
                )
                return order_id
        except KiteOrderError:
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise _wrap_transport(exc) from exc

    def order_history(self, order_id: str) -> list[dict[str, Any]]:
        try:
            with self._client() as client:
                resp = client.get(f"/orders/{order_id}")
                body = _json_body(resp)
                if resp.status_code >= 400:
                    raise KiteOrderError(_kite_error(body, resp.status_code))
                data = body.get("data")
                if isinstance(data, list):
                    return [row for row in data if isinstance(row, dict)]
                if isinstance(data, dict):
                    return [data]
                return []
        except KiteOrderError:
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise _wrap_transport(exc) from exc

    def _latest_status(self, order_id: str) -> dict[str, Any]:
        rows = self.order_history(order_id)
        return rows[-1] if rows else {}

    def wait_fill(
        self,
        order_id: str,
        *,
        timeout_s: float = FILL_TIMEOUT_S,
        poll_s: float = FILL_POLL_S,
    ) -> dict[str, Any]:
        """Poll until COMPLETE. On timeout, cancel then re-read status before failing."""
        deadline = time.monotonic() + float(timeout_s)
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self._latest_status(order_id)
            status = str(last.get("status") or "").upper()
            if status == "COMPLETE":
                return last
            if status in ("REJECTED", "CANCELLED", "CANCELED"):
                raise KiteOrderError(
                    f"order_{status.lower()}:{last.get('status_message') or last.get('status')}",
                    order_id=order_id,
                )
            time.sleep(float(poll_s))
        try:
            self.cancel(order_id)
        except KiteOrderError as exc:
            _log.warning("kite cancel after timeout failed id=%s: %s", order_id, exc)
        # Race: order may have filled while cancel was in flight.
        try:
            last = self._latest_status(order_id)
        except KiteOrderError as exc:
            raise KiteOrderError(
                f"fill_timeout:{order_id}:{last.get('status') or 'unknown'}:recheck_failed:{exc}",
                order_id=order_id,
            ) from exc
        status = str(last.get("status") or "").upper()
        if status == "COMPLETE":
            _log.warning("kite order completed after timeout cancel id=%s", order_id)
            return last
        if status in ("CANCELLED", "CANCELED", "REJECTED"):
            raise KiteOrderError(
                f"fill_timeout_{status.lower()}:{order_id}:{last.get('status_message') or status}",
                order_id=order_id,
            )
        raise KiteOrderError(
            f"fill_timeout:{order_id}:{status or 'unknown'}",
            order_id=order_id,
        )

    def cancel(self, order_id: str) -> None:
        try:
            with self._client() as client:
                resp = client.delete(f"/orders/{DEFAULT_VARIETY}/{order_id}")
                if resp.status_code >= 400:
                    body = _json_body(resp)
                    raise KiteOrderError(_kite_error(body, resp.status_code))
        except KiteOrderError:
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise _wrap_transport(exc) from exc

    def order_status(self, order_id: str) -> dict[str, Any]:
        """Latest status row for an order id."""
        return self._latest_status(order_id)

    def position_row(
        self, symbol: str, *, product: str = DEFAULT_PRODUCT
    ) -> dict[str, Any] | None:
        """Matching MIS row from Kite ``net`` positions only (not ``day`` — no double count).

        Returns quantity (signed), average_price, realised, and raw row fields.
        """
        exchange, tradingsymbol = split_nfo_symbol(symbol)
        product_u = str(product or DEFAULT_PRODUCT).upper()
        exchange_u = exchange.upper()
        try:
            with self._client() as client:
                resp = client.get("/portfolio/positions")
                body = _json_body(resp)
                if resp.status_code >= 400:
                    raise KiteOrderError(_kite_error(body, resp.status_code))
                data = body.get("data") if isinstance(body.get("data"), dict) else {}
                net_rows = data.get("net")
                rows = net_rows if isinstance(net_rows, list) else []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("tradingsymbol") or "").upper() != tradingsymbol:
                        continue
                    row_ex = str(row.get("exchange") or exchange_u).upper()
                    # Match the requested exchange (NFO / NSE / BSE). Empty exchange on the
                    # row is accepted only for NFO (legacy agent path).
                    if row_ex and row_ex != exchange_u:
                        continue
                    if not row_ex and exchange_u != "NFO":
                        continue
                    row_product = str(row.get("product") or "").upper()
                    if row_product and row_product != product_u:
                        continue
                    qty = int(float(row.get("quantity") or 0))
                    avg = _f(row.get("average_price"))
                    realised = _f(row.get("realised"))
                    return {
                        "quantity": qty,
                        "average_price": round(float(avg), 2) if avg is not None else None,
                        "realised": round(float(realised), 2) if realised is not None else None,
                        "tradingsymbol": tradingsymbol,
                        "exchange": row_ex or exchange_u,
                        "product": row_product or product_u,
                        "raw": row,
                    }
                return None
        except KiteOrderError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            raise _wrap_transport(exc) from exc

    def net_position_qty(self, symbol: str, *, product: str = DEFAULT_PRODUCT) -> int:
        """Net qty for this NFO tradingsymbol (positive = long, negative = short)."""
        row = self.position_row(symbol, product=product)
        return int(row["quantity"]) if row else 0

    def fill_from_order(self, order_id: str, *, fallback_qty: int = 0) -> dict[str, Any]:
        """Build a fill dict from a COMPLETE order status row."""
        fill = self.order_status(order_id)
        status = str(fill.get("status") or "").upper()
        if status != "COMPLETE":
            raise KiteOrderError(f"order_not_complete:{status or 'unknown'}", order_id=order_id)
        avg = _f(fill.get("average_price"))
        if avg is None or avg <= 0:
            raise KiteOrderError(f"bad_fill_price:{order_id}", order_id=order_id)
        return {
            "order_id": order_id,
            "average_price": round(float(avg), 2),
            "quantity": int(
                fill.get("filled_quantity") or fill.get("quantity") or fallback_qty or 0
            ),
            "status": "COMPLETE",
            "exchange": fill.get("exchange"),
            "tradingsymbol": fill.get("tradingsymbol"),
            "transaction_type": fill.get("transaction_type"),
        }

    def market_fill(
        self,
        *,
        symbol: str,
        transaction_type: str,
        qty: int,
        product: str = DEFAULT_PRODUCT,
        tag: str = ORDER_TAG,
    ) -> dict[str, Any]:
        """Place a market order and return the completed fill row (average_price, order_id)."""
        order_id = self.place_market(
            symbol=symbol,
            transaction_type=transaction_type,
            qty=qty,
            product=product,
            tag=tag,
        )
        try:
            fill = self.wait_fill(order_id)
        except KiteOrderError as exc:
            if not exc.order_id:
                exc.order_id = order_id
            raise
        avg = _f(fill.get("average_price"))
        if avg is None or avg <= 0:
            raise KiteOrderError(f"bad_fill_price:{order_id}", order_id=order_id)
        return {
            "order_id": order_id,
            "average_price": round(float(avg), 2),
            "quantity": int(fill.get("filled_quantity") or fill.get("quantity") or qty),
            "status": str(fill.get("status") or "COMPLETE"),
            "exchange": fill.get("exchange"),
            "tradingsymbol": fill.get("tradingsymbol"),
            "transaction_type": fill.get("transaction_type"),
        }

    def place_limit_ioc(
        self,
        *,
        symbol: str,
        transaction_type: str,
        qty: int,
        price: float,
        product: str = DEFAULT_PRODUCT,
        tag: str = ARB_ORDER_TAG,
    ) -> str:
        """Place a LIMIT IOC equity/F&O order. Unfilled qty is cancelled by the exchange."""
        exchange, tradingsymbol = split_nfo_symbol(symbol)
        side = str(transaction_type or "").strip().upper()
        if side not in ("BUY", "SELL"):
            raise KiteOrderError(f"bad_transaction_type:{transaction_type}")
        quantity = int(qty)
        if quantity <= 0:
            raise KiteOrderError("bad_qty")
        px = float(price)
        if px <= 0:
            raise KiteOrderError("bad_price")
        data = {
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": side,
            "order_type": "LIMIT",
            "price": f"{px:.2f}",
            "quantity": str(quantity),
            "product": product,
            "validity": "IOC",
            "tag": (tag or ARB_ORDER_TAG)[:20],
        }
        try:
            with self._client() as client:
                resp = client.post(f"/orders/{DEFAULT_VARIETY}", data=data)
                body = _json_body(resp)
                if resp.status_code >= 400:
                    raise KiteOrderError(_kite_error(body, resp.status_code))
                order_id = str((body.get("data") or {}).get("order_id") or "").strip()
                if not order_id:
                    raise KiteOrderError("missing_order_id")
                _log.warning(
                    "kite IOC limit id=%s %s %s qty=%s px=%s",
                    order_id,
                    side,
                    tradingsymbol,
                    quantity,
                    px,
                )
                return order_id
        except KiteOrderError:
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise _wrap_transport(exc) from exc

    def wait_ioc_done(
        self,
        order_id: str,
        *,
        timeout_s: float = FILL_TIMEOUT_S,
        poll_s: float = FILL_POLL_S,
    ) -> dict[str, Any]:
        """Poll until IOC finishes (COMPLETE / CANCELLED / REJECTED). Partial fill OK."""
        deadline = time.monotonic() + float(timeout_s)
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self._latest_status(order_id)
            status = str(last.get("status") or "").upper()
            if kite_order_finished(status):
                return last
            time.sleep(float(poll_s))
        try:
            self.cancel(order_id)
        except KiteOrderError as exc:
            _log.warning("kite IOC cancel after timeout failed id=%s: %s", order_id, exc)
        try:
            last = self._latest_status(order_id)
        except KiteOrderError as exc:
            raise KiteOrderError(
                f"ioc_timeout:{order_id}:{last.get('status') or 'unknown'}:recheck_failed:{exc}",
                order_id=order_id,
            ) from exc
        if kite_order_finished(str(last.get("status") or "")):
            return last
        raise KiteOrderError(
            f"ioc_timeout:{order_id}:{last.get('status') or 'unknown'}",
            order_id=order_id,
        )

    def equity_available(self) -> float:
        """Equity segment cash available for trading (₹)."""
        try:
            with self._client() as client:
                resp = client.get("/user/margins")
                body = _json_body(resp)
                if resp.status_code >= 400:
                    raise KiteOrderError(_kite_error(body, resp.status_code))
                data = body.get("data") if isinstance(body.get("data"), dict) else {}
                equity = data.get("equity") if isinstance(data.get("equity"), dict) else {}
                available = equity.get("available") if isinstance(equity.get("available"), dict) else {}
                for key in ("live_balance", "cash", "intraday_payin"):
                    val = _f(available.get(key))
                    if val is not None:
                        return float(val)
                net = _f(equity.get("net"))
                return float(net or 0.0)
        except KiteOrderError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            raise _wrap_transport(exc) from exc

    def basket_margin_required(
        self,
        orders: list[dict[str, Any]],
    ) -> float:
        """POST /margins/orders — total required margin for the basket (₹)."""
        if not orders:
            return 0.0
        try:
            with self._client() as client:
                resp = client.post("/margins/orders", json=orders)
                body = _json_body(resp)
                if resp.status_code >= 400:
                    raise KiteOrderError(_kite_error(body, resp.status_code))
                data = body.get("data")
                rows = data if isinstance(data, list) else []
                total = 0.0
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    final = row.get("final") if isinstance(row.get("final"), dict) else row
                    for key in ("total", "span", "exposure", "margin"):
                        val = _f(final.get(key) if isinstance(final, dict) else None)
                        if val is not None and key == "total":
                            total += float(val)
                            break
                    else:
                        val = _f(row.get("total"))
                        if val is not None:
                            total += float(val)
                return round(total, 2)
        except KiteOrderError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            raise _wrap_transport(exc) from exc

    def ioc_limit_fill(
        self,
        *,
        symbol: str,
        transaction_type: str,
        qty: int,
        price: float,
        product: str = DEFAULT_PRODUCT,
        tag: str = ARB_ORDER_TAG,
    ) -> dict[str, Any]:
        """Place LIMIT IOC and return fill qty/price (0 qty if nothing filled / rejected)."""
        order_id = self.place_limit_ioc(
            symbol=symbol,
            transaction_type=transaction_type,
            qty=qty,
            price=price,
            product=product,
            tag=tag,
        )
        try:
            row = self.wait_ioc_done(order_id)
        except KiteOrderError as exc:
            if not exc.order_id:
                exc.order_id = order_id
            # Still try to read any partial that landed before the error.
            try:
                row = self._latest_status(order_id)
            except KiteOrderError:
                raise exc from None
            if int(float(row.get("filled_quantity") or 0)) <= 0:
                raise
        status = str(row.get("status") or "").upper()
        filled = int(float(row.get("filled_quantity") or 0))
        avg = _f(row.get("average_price"))
        if filled > 0 and (avg is None or avg <= 0):
            raise KiteOrderError(f"bad_fill_price:{order_id}", order_id=order_id)
        return {
            "order_id": order_id,
            "average_price": round(float(avg), 2) if avg and filled else None,
            "quantity": filled,
            "status": status or "UNKNOWN",
            "exchange": row.get("exchange"),
            "tradingsymbol": row.get("tradingsymbol"),
            "transaction_type": row.get("transaction_type") or transaction_type,
            "status_message": row.get("status_message"),
        }


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        raw = resp.json()
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _kite_error(body: dict[str, Any], status: int) -> str:
    msg = body.get("message") or body.get("error_type") or body.get("status")
    if msg:
        return f"http_{status}:{msg}"
    return f"http_{status}"
